"""The ``StructuredSummary`` compaction strategy: planning, summarization context, and estimates.

Compaction appends a seed and never deletes. The summarized prefix and the verbatim tail are
disjoint, and the cut lands on a ``step/end`` so no tool call is separated from its result.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

from ava.base import AvaError, ErrorKind
from ava.llm.types import ContentBlockKind, Context, Item, Role, make_text_block
from ava.session.event import (
    AssistantMessage,
    CompactionSeed,
    Event,
    StepClaimed,
    StepEnd,
    Usage,
    model_item,
)
from ava.session.session import Session

CHARS_PER_TOKEN = 4
BLOCK_OVERHEAD_TOKENS = 4
ITEM_OVERHEAD_TOKENS = 4
IMAGE_BLOCK_TOKENS = 1_200

_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")


@cache
def summarization_instruction() -> str:
    prompt = (Path(__file__).parent / "prompts" / "checkpoint.md").read_text(encoding="utf-8")
    return prompt.rstrip("\r\n") if prompt.endswith("\n") else prompt


@dataclass(slots=True)
class FileLists:
    read: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)


def _tool_path(arguments_json: str) -> str | None:
    try:
        arguments = json.loads(arguments_json)
    except (json.JSONDecodeError, TypeError):
        return None
    path = arguments.get("path") if isinstance(arguments, dict) else None
    return path if isinstance(path, str) and path else None


def derive_files(session: Session, covered_end: int) -> FileLists:
    """Mechanically derived from structured tool calls; the summarizer cannot invent these."""
    files = FileLists()
    for event in session.events:
        if event.seq > covered_end:
            break
        if not isinstance(event.payload, AssistantMessage):
            continue
        for block in event.payload.item.blocks:
            if block.kind != ContentBlockKind.tool_call:
                continue
            path = _tool_path(block.arguments_json)
            if path is None:
                continue
            target = (
                files.read
                if block.tool_name == "read"
                else files.modified
                if block.tool_name in ("write", "edit")
                else None
            )
            if target is not None and path not in target:
                target.append(path)
    return files


def _files_section(files: FileLists) -> str:
    def listing(paths: list[str]) -> str:
        return "(none)" if not paths else "\n".join(f"- {path}" for path in paths)

    return f"## Files\n\n### Read\n{listing(files.read)}\n\n### Modified\n{listing(files.modified)}"


@dataclass(slots=True)
class SummarizationContext:
    instruction_item: Item
    context: Context


def build_summarization_context(
    session: Session, covered_end: int, instruction: str
) -> SummarizationContext:
    """Only the covered prefix is summarized, keeping the previous request's cache shape."""
    context = session.model_context()
    covered: set[int] = set()
    for event in session.events:
        if event.seq > covered_end:
            break
        if isinstance(event.payload, StepClaimed):
            covered.update(id(message.item) for message in event.payload.claimed)
            continue
        item = model_item(event.payload)
        if item is not None:
            covered.add(id(item))
    context.items = [item for item in context.items if id(item) in covered]
    instruction_item = Item(role=Role.user, blocks=[make_text_block(instruction)])
    context.items.append(instruction_item)
    return SummarizationContext(instruction_item=instruction_item, context=context)


def validate_summary(response: Item) -> str:
    text = ""
    for block in response.blocks:
        if block.kind == ContentBlockKind.tool_call:
            raise AvaError(
                ErrorKind.provider,
                f"compaction response called tool '{block.tool_name}' instead of returning prose",
                recoverable=True,
            )
        if block.kind == ContentBlockKind.text:
            text += block.text
    return text


def make_rejection_correction(tool_name: str) -> str:
    return (
        f"The tool call '{tool_name}' was rejected and was not run. Return the requested checkpoint "
        "as prose only. Do not call tools."
    )


def _next_steps_position(text: str) -> int | None:
    open_fence: tuple[str, int] | None = None
    offset = 0
    for line in text.split("\n"):
        stripped = line[:-1] if line.endswith("\r") else line
        match = _FENCE.match(stripped)
        marker: tuple[str, int, bool] | None = None
        if match and not (match.group(1)[0] == "`" and "`" in match.group(2)):
            marker = (match.group(1)[0], len(match.group(1)), match.group(2).strip(" \t") == "")
        if (
            open_fence
            and marker
            and marker[0] == open_fence[0]
            and marker[1] >= open_fence[1]
            and marker[2]
        ):
            open_fence = None
        elif not open_fence and marker:
            open_fence = (marker[0], marker[1])
        elif not open_fence and stripped == "## Next Steps":
            return offset
        offset += len(line) + 1
    return None


def assemble_seed(
    session: Session, covered_end: int, instruction: str, summary: str
) -> CompactionSeed:
    section = _files_section(derive_files(session, covered_end))
    position = _next_steps_position(summary)
    if position is not None:
        summary = summary[:position] + section + "\n\n" + summary[position:]
    else:
        if summary and not summary.endswith("\n"):
            summary += "\n"
        if summary:
            summary += "\n"
        summary += section
    return CompactionSeed(
        covered_begin=session.at(0).seq if len(session) else 0,
        covered_end=covered_end,
        instruction=instruction,
        item=Item(role=Role.user, blocks=[make_text_block(summary)]),
    )


def _estimate_text_tokens(text: str) -> int:
    return (len(text.encode("utf-8")) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN


def estimate_item_tokens(item: Item) -> int:
    tokens = ITEM_OVERHEAD_TOKENS
    for block in item.blocks:
        tokens += BLOCK_OVERHEAD_TOKENS
        tokens += _estimate_text_tokens(block.text)
        tokens += _estimate_text_tokens(block.arguments_json)
        tokens += _estimate_text_tokens(block.display_path)
        tokens += _estimate_text_tokens(block.call_id)
        tokens += _estimate_text_tokens(block.tool_name)
        if block.kind == ContentBlockKind.image:
            tokens += IMAGE_BLOCK_TOKENS
    return tokens


def estimate_event_tokens(event: Event) -> int:
    if isinstance(event.payload, StepClaimed):
        return sum(estimate_item_tokens(message.item) for message in event.payload.claimed)
    item = model_item(event.payload)
    return estimate_item_tokens(item) if item is not None else 0


def estimate_context_tokens(session: Session) -> int:
    """Provider-reported input categories for the newest post-seed attempt plus an estimated tail."""
    events = session.events
    newest_seed = next(
        (
            index
            for index in range(len(events) - 1, -1, -1)
            if isinstance(events[index].payload, CompactionSeed)
        ),
        None,
    )
    first_post_seed = newest_seed + 1 if newest_seed is not None else 0
    for index in range(len(events) - 1, first_post_seed - 1, -1):
        usage = events[index].payload
        if not isinstance(usage, Usage) or (
            usage.input is None and usage.cached_read is None and usage.cache_write is None
        ):
            continue
        estimate = (
            (usage.input or 0)
            + (usage.cached_read or 0)
            + (usage.cache_write or 0)
            + (usage.output or 0)
        )
        estimate += sum(estimate_event_tokens(event) for event in events[index + 1 :])
        return estimate
    if newest_seed is not None:
        seed_event = events[newest_seed]
        seed = seed_event.payload
        assert isinstance(seed, CompactionSeed)
        estimate = estimate_item_tokens(seed.item)
        for index, event in enumerate(events):
            visible_after_seed = event.seq > seed.covered_end or event.seq > seed_event.seq
            if (
                index != newest_seed
                and visible_after_seed
                and not isinstance(event.payload, CompactionSeed)
            ):
                estimate += estimate_event_tokens(event)
        return estimate
    return sum(estimate_event_tokens(event) for event in events)


def select_covered_end(session: Session, tail_budget_tokens: int) -> int | None:
    """Fill the tail budget at complete-step cuts, crossing the newest seed to fold it."""
    events = session.events
    newest_seed_seq: int | None = None
    previous_covered_end: int | None = None
    for event in reversed(events):
        if isinstance(event.payload, CompactionSeed):
            newest_seed_seq = event.seq
            previous_covered_end = event.payload.covered_end
            break
    first_new_item_seq: int | None = None
    for event in events:
        beyond = (
            previous_covered_end is None
            or event.seq > previous_covered_end
            or (newest_seed_seq is not None and event.seq > newest_seed_seq)
        )
        is_newest_seed = newest_seed_seq is not None and event.seq == newest_seed_seq
        if beyond and not is_newest_seed and model_item(event.payload) is not None:
            first_new_item_seq = event.seq
            break
    if first_new_item_seq is None:
        return None
    tail_tokens = 0
    best: int | None = None
    for event in reversed(events):
        folds_newest_seed = newest_seed_seq is None or event.seq > newest_seed_seq
        covers_new_item = event.seq >= first_new_item_seq
        if (
            isinstance(event.payload, StepEnd)
            and folds_newest_seed
            and covers_new_item
            and tail_tokens <= tail_budget_tokens
        ):
            best = event.seq
        event_tokens = estimate_event_tokens(event)
        if event_tokens > tail_budget_tokens - tail_tokens:
            break
        tail_tokens += event_tokens
    return best


def seed_shrinks_window(session: Session, seed: CompactionSeed) -> bool:
    context_ids = {id(item) for item in session.model_context().items}
    shadowed = 0
    for event in session.events:
        if event.seq > seed.covered_end:
            break
        if isinstance(event.payload, StepClaimed):
            for message in event.payload.claimed:
                if id(message.item) in context_ids:
                    shadowed += estimate_item_tokens(message.item)
            continue
        item = model_item(event.payload)
        if item is not None and id(item) in context_ids:
            shadowed += estimate_item_tokens(item)
    return estimate_item_tokens(seed.item) < shadowed
