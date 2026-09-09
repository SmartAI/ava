"""Read-only session evidence, with unavailable measurements kept distinct from zero."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ava.llm.types import ContentBlockKind
from ava.session.event import (
    AssistantMessage,
    AttemptTiming,
    CompactionSeed,
    DriveError,
    Selection,
    SessionStart,
    ToolResult,
    TurnEnd,
    Usage,
)
from ava.session.log import Log, OpenMode
from ava.session.recovery import plan_lifecycle_repair
from ava.session.session import Session


def inspect_session(path: Path) -> dict[str, Any]:
    """Inspect the complete readable prefix; never repair or execute session content.

    Lifecycle repair is reported separately from outcome. A completed turn is not a
    correctness grade. Raw token breakdowns are null if any attempt omits that field.
    Inclusive totals restore the provider's total before summing attempts; the OpenAI
    parser leaves absent cache/reasoning components in input/output respectively.
    """
    log = Log.open(path, OpenMode.read_only)
    try:
        events = log.loaded_events
        header = events[0].payload
        assert isinstance(header, SessionStart)
        session = Session(list(events))
        inbox = session.inbox()
        repairs = plan_lifecycle_repair(events)
        usage = {e.payload.attempt_id: e.payload for e in events if isinstance(e.payload, Usage)}
        attempts = {
            e.payload.attempt_id for e in events if isinstance(e.payload, AttemptTiming)
        } | set(usage)
        tokens: dict[str, int | None] = {}
        for field in (
            "input",
            "output",
            "cached_read",
            "cache_write",
            "cache_write_1h",
            "reasoning",
        ):
            values = [
                getattr(usage[attempt], field) if attempt in usage else None for attempt in attempts
            ]
            tokens[field] = (
                sum(v for v in values if v is not None)
                if values and all(v is not None for v in values)
                else None
            )
        calls: Counter[str] = Counter()
        results = errors = 0
        for event in events:
            if isinstance(event.payload, AssistantMessage):
                calls.update(
                    block.tool_name
                    for block in event.payload.item.blocks
                    if block.kind == ContentBlockKind.tool_call
                )
            elif isinstance(event.payload, ToolResult):
                for block in event.payload.item.blocks:
                    if block.kind == ContentBlockKind.tool_result:
                        results += 1
                        errors += int(block.is_error)
        turns = [e.payload for e in events if isinstance(e.payload, TurnEnd)]
        selections = [e.payload for e in events if isinstance(e.payload, Selection)]
        providers = {header.provider, *(selection.provider for selection in selections)}
        inclusive: dict[str, int | None] = {"input": None, "output": None}
        # Mixed-provider sessions need per-attempt attribution, which the old log schema
        # does not guarantee. Never reinterpret the whole session using its final provider.
        if len(providers) == 1 and header.provider in {"openai", "codex", "anthropic"}:
            amounts: dict[str, list[int | None]] = {"input": [], "output": []}
            for attempt in attempts:
                item = usage.get(attempt)
                input_total = output_total = None
                if item is not None:
                    if header.provider in {"openai", "codex"}:
                        if item.input is not None:
                            input_total = item.input + (item.cached_read or 0)
                        if item.output is not None:
                            output_total = item.output + (item.reasoning or 0)
                    else:
                        if item.input is not None and item.cached_read is not None and item.cache_write is not None:
                            input_total = item.input + item.cached_read + item.cache_write
                        output_total = item.output
                amounts["input"].append(input_total)
                amounts["output"].append(output_total)
            for name, values in amounts.items():
                if values and all(value is not None for value in values):
                    inclusive[name] = sum(value for value in values if value is not None)
        return {
            "schema_version": 1,
            "session_id": header.id,
            "cwd": header.cwd,
            "provider": selections[-1].provider if selections else header.provider,
            "model": selections[-1].model if selections else header.model,
            "events": len(events),
            "last_sequence": events[-1].seq,
            "last_turn_reason": turns[-1].reason.value if turns else None,
            "lifecycle_repairs_needed": [payload.kind for payload in repairs],
            "pending_inputs": {
                "next_turn": len(inbox.next_turn),
                "next_step": len(inbox.next_step),
            },
            "model_attempts": len(attempts),
            "attempt_timings": [
                asdict(e.payload) for e in events if isinstance(e.payload, AttemptTiming)
            ],
            "tokens": tokens,
            "inclusive_tokens": inclusive,
            "cost_usd": None,
            "tool_calls": dict(sorted(calls.items())),
            "tool_results": results,
            "tool_errors": errors,
            "compactions": sum(isinstance(e.payload, CompactionSeed) for e in events),
            "drive_errors": [
                asdict(e.payload) for e in events if isinstance(e.payload, DriveError)
            ],
        }
    finally:
        log.close()
