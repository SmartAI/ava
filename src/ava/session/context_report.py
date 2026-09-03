"""What the model sees, by kind and size: a pure projection over the session.

The report is computed from the same `model_context()` the next request would use, so it can
never disagree with what is sent. Sizes use the compaction estimator (bytes / 4 plus framing);
the newest provider-measured input for the current window is reported beside the estimate.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from ava.llm.types import ContentBlockKind, Item, Role, ToolDef
from ava.session.compaction import (
    ITEM_OVERHEAD_TOKENS,
    estimate_block_tokens,
    estimate_text_tokens,
)
from ava.session.event import CompactionSeed, Usage
from ava.session.session import Session

# Stable section ids in display order, with their labels.
SECTION_LABELS: dict[str, str] = {
    "system": "System prompt",
    "environment": "Environment",
    "agents_md": "AGENTS.md instructions",
    "skills": "Skills catalog",
    "tools": "Tool schemas",
    "compaction_seed": "Compaction summary",
    "user_text": "Your messages",
    "attachment_files": "Attached files",
    "attachment_images": "Attached images",
    "assistant_text": "Assistant text",
    "reasoning": "Reasoning",
    "tool_calls": "Tool calls",
    "tool_results": "Tool results",
    "framing": "Message framing",
}

_PROMPT_HEADINGS: dict[str, str] = {
    "Environment": "environment",
    "Scratchpad": "environment",
    "Agent instructions": "agents_md",
    "Skills": "skills",
}


@dataclass(slots=True)
class ContextSection:
    kind: str
    label: str
    tokens: int
    bytes: int
    count: int


@dataclass(slots=True)
class ContextReport:
    sections: list[ContextSection]
    estimated_tokens: int
    measured_input_tokens: int | None
    context_window: int
    threshold_percent: int
    compacted: bool


class _Tally:
    def __init__(self) -> None:
        self.tokens: dict[str, int] = {}
        self.bytes: dict[str, int] = {}
        self.count: dict[str, int] = {}

    def add(self, kind: str, tokens: int, size: int, count: int = 1) -> None:
        self.tokens[kind] = self.tokens.get(kind, 0) + tokens
        self.bytes[kind] = self.bytes.get(kind, 0) + size
        self.count[kind] = self.count.get(kind, 0) + count

    def sections(self) -> list[ContextSection]:
        return [
            ContextSection(
                kind=kind,
                label=label,
                tokens=self.tokens[kind],
                bytes=self.bytes[kind],
                count=self.count[kind],
            )
            for kind, label in SECTION_LABELS.items()
            if kind in self.tokens
        ]


def _prompt_sections(prompt: str) -> list[tuple[str, str]]:
    """Split the resolved prompt at top-level headings and map each to a section kind."""
    sections: list[tuple[str, str]] = []
    current_kind = "system"
    current: list[str] = []
    for line in prompt.splitlines(keepends=True):
        if line.startswith("# "):
            if current:
                sections.append((current_kind, "".join(current)))
            heading = line[2:].strip()
            current_kind = _PROMPT_HEADINGS.get(heading, "system")
            current = [line]
        else:
            current.append(line)
    if current:
        sections.append((current_kind, "".join(current)))
    return sections


def _tool_schema_text(tool: ToolDef) -> str:
    """The provider-neutral schema, serialized the way the adapters serialize it."""
    schema = {
        "name": tool.name,
        "description": tool.description,
        "params": [
            {
                "name": param.name,
                "description": param.description,
                "type": param.type.value,
                "required": param.required,
                "minimum": param.minimum,
            }
            for param in tool.params
        ],
    }
    return json.dumps(schema, ensure_ascii=False, separators=(",", ":"))


def _block_kind(role: Role, kind: ContentBlockKind) -> str:
    match kind:
        case ContentBlockKind.file_text:
            return "attachment_files"
        case ContentBlockKind.image:
            return "attachment_images"
        case ContentBlockKind.reasoning:
            return "reasoning"
        case ContentBlockKind.tool_call:
            return "tool_calls"
        case ContentBlockKind.tool_result:
            return "tool_results"
        case _:
            return "assistant_text" if role == Role.assistant else "user_text"


def _block_bytes(block) -> int:
    if block.kind == ContentBlockKind.image:
        return len(block.bytes)
    if block.kind == ContentBlockKind.reasoning:
        return len(block.opaque_json.encode("utf-8"))
    return len(
        (block.text + block.arguments_json + block.display_path + block.tool_name).encode("utf-8")
    )


def _measured_input(session: Session) -> int | None:
    """The provider's input-side count for the newest attempt inside the current window."""
    events = session.events
    first = 0
    for index in range(len(events) - 1, -1, -1):
        if isinstance(events[index].payload, CompactionSeed):
            first = index + 1
            break
    for index in range(len(events) - 1, first - 1, -1):
        usage = events[index].payload
        if isinstance(usage, Usage) and any(
            value is not None for value in (usage.input, usage.cached_read, usage.cache_write)
        ):
            return (usage.input or 0) + (usage.cached_read or 0) + (usage.cache_write or 0)
    return None


def context_report(session: Session, context_window: int, threshold_percent: int) -> ContextReport:
    context = session.model_context()
    tally = _Tally()
    for kind, text in _prompt_sections(context.system_prompt):
        tally.add(kind, estimate_text_tokens(text), len(text.encode("utf-8")))
    for tool in context.tools:
        schema = _tool_schema_text(tool)
        tally.add("tools", estimate_text_tokens(schema), len(schema.encode("utf-8")))

    seed_item: Item | None = None
    for event in reversed(session.events):
        if isinstance(event.payload, CompactionSeed):
            seed_item = event.payload.item
            break
    for item in context.items:
        if item is seed_item:
            tally.add(
                "compaction_seed",
                ITEM_OVERHEAD_TOKENS + sum(estimate_block_tokens(block) for block in item.blocks),
                sum(_block_bytes(block) for block in item.blocks),
            )
            continue
        tally.add("framing", ITEM_OVERHEAD_TOKENS, 0)
        for block in item.blocks:
            tally.add(
                _block_kind(item.role, block.kind),
                estimate_block_tokens(block),
                _block_bytes(block),
            )
    sections = tally.sections()
    return ContextReport(
        sections=sections,
        estimated_tokens=sum(section.tokens for section in sections),
        measured_input_tokens=_measured_input(session),
        context_window=context_window,
        threshold_percent=threshold_percent,
        compacted=seed_item is not None,
    )
