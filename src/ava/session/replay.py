"""Read-only, bounded views of durable execution evidence, independent of any UI.

A sequence watermark freezes a view while a session continues. Detail/context is
computed on demand, never for every row. No recovery, environment lookup or execution.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict
from typing import Any

from ava.llm.types import ContentBlockKind, Item
from ava.session.codec import encode_record, payload_to_wire
from ava.session.event import (
    AssistantChunk,
    AssistantMessage,
    AttemptTiming,
    CompactionFailed,
    CompactionSeed,
    DriveError,
    Event,
    GoalChanged,
    GoalChecked,
    InboxSpliced,
    PromptResolved,
    RequestPrepared,
    RequestRetry,
    Selection,
    SessionStart,
    StepClaimed,
    StepEnd,
    StepStart,
    ToolResult,
    ToolsAdvertised,
    TurnEnd,
    TurnStart,
    Unknown,
    Usage,
    UserMessage,
)
from ava.session.session import Session
from ava.session.stops import error_evidence, session_stops, stop_links, stop_summary, stop_text

PAGE_SIZE = 200
TEXT_PAGE_SIZE = 24000


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def item_text(item: Item) -> str:
    parts = [f"{item.role.value.upper()}"]
    for block in item.blocks:
        if block.kind == ContentBlockKind.tool_call:
            parts.append(f"Tool: {block.tool_name} · {block.call_id}\n{block.arguments_json}")
        elif block.kind == ContentBlockKind.tool_result:
            parts.append(
                f"Result: {block.call_id} · {'error' if block.is_error else 'returned'}\n{block.text}"
            )
        elif block.kind in (ContentBlockKind.image, ContentBlockKind.pdf):
            parts.append(
                f"[{block.kind.value}: {block.display_path or block.media_type} · {len(block.bytes)} bytes; binary content in raw event]"
            )
        elif block.kind == ContentBlockKind.reasoning:
            parts.append(
                "Provider reasoning summary (not hidden model internals):\n"
                + (block.text or "No readable summary recorded.")
            )
        else:
            parts.append(
                (f"File: {block.display_path}\n" if block.display_path else "") + block.text
            )
        if block.origin.value != "none":
            parts.append(f"Origin: {block.origin.value}")
        if block.attachments:
            parts.append(
                f"[{len(block.attachments)} attached image(s); binary content in raw event]"
            )
    return "\n\n".join(parts)


def event_text(event: Event) -> str:
    p = event.payload
    if isinstance(p, AssistantMessage | ToolResult | UserMessage | CompactionSeed):
        body = item_text(p.item)
        if isinstance(p, CompactionSeed):
            body = (
                f"Summary replaces events #{p.covered_begin}–#{p.covered_end}.\nInstruction: {p.instruction}\n\n"
                + body
            )
        if isinstance(p, ToolResult):
            body += "\n\n" + _json(
                {"durations": [asdict(d) for d in p.durations], "truncated": p.truncated}
            )
        return body
    if isinstance(p, StepClaimed):
        return f"Inputs received by turn {p.turn}, step {p.step}:\n\n" + "\n\n".join(
            item_text(m.item) for m in p.claimed
        )
    if isinstance(p, InboxSpliced):
        return f"Pending inputs: {p.target.value}\nRemoved: {p.removed}\n\n" + "\n\n".join(
            item_text(m.item) for m in p.inserted
        )
    if isinstance(p, RequestRetry):
        return (f"Request attempt failed · {p.error_kind.value}\n{p.message}\n\n"
                f"Retry {p.next_attempt}/3 scheduled in {p.delay_ms / 1000:.1f} s. "
                "Scheduling is not proof the next request executed. Completed tools are not rerun.\n"
                "Missing usage is unknown, not zero.\n\n" + p.detail
                + "\n\nFAILED OUTPUT · not used in model context or executed\n" + item_text(p.item))
    if isinstance(p, DriveError):
        error = error_evidence(event)
        return str(error["summary"]) + "\n\nRecorded error kind: " + str(error["kind"]) + "\n\nProvider detail:\n" + (p.detail or "Not recorded.")
    if isinstance(p, PromptResolved):
        return p.system_prompt
    if isinstance(p, Unknown):
        return p.raw_line
    return _json(payload_to_wire(p))


def _title(event: Event) -> str:
    p = event.payload
    if isinstance(p, AssistantMessage):
        tools = [b.tool_name for b in p.item.blocks if b.kind == ContentBlockKind.tool_call]
        return ("Call " + ", ".join(tools))[:160] if tools else "Model response"
    if isinstance(p, ToolResult):
        return "Tool results"
    if isinstance(p, RequestRetry):
        return f"Request retry {p.next_attempt}/3 · {p.error_kind.value}"
    if isinstance(p, RequestPrepared):
        return f"Model request · {p.model}"[:160]
    if isinstance(p, DriveError):
        return str(error_evidence(event)["summary"])[:160]
    return {
        "session/start": "Session started",
        "turn/start": "Turn started",
        "turn/end": "Turn ended",
        "step/start": "Step started",
        "step/end": "Step ended",
        "step/claimed": "Inputs received",
        "inbox/spliced": "Pending inputs changed",
        "user/message": "User input",
        "prompt/resolved": "Instructions resolved",
        "tools/advertised": "Available tools changed",
        "selection": "Model selection",
        "usage": "Token usage",
        "attempt/timing": "Request timing",
        "compaction/seed": "Context compressed",
        "compaction/failed": "Compaction failed",
        "drive/error": "Runtime error",
        "skill/loaded": "Skill loaded",
        "goal/changed": "Goal changed",
        "goal/checked": "Goal verification",
        "goal/continued": "Goal continued",
        "assistant/chunk": "Streaming fragment",
    }.get(p.kind, p.kind)[:160]


def _category(event: Event) -> str:
    p = event.payload
    if isinstance(p, AssistantChunk):
        return "stream"
    if isinstance(p, RequestPrepared | RequestRetry | AssistantMessage | Usage | AttemptTiming):
        return "model"
    if isinstance(p, ToolResult | GoalChecked):
        return "tool"
    if isinstance(p, UserMessage | StepClaimed | InboxSpliced):
        return "input"
    return "state"


def _observations(events: list[Event]) -> dict[int, list[dict[str, str]]]:
    notes: dict[int, list[dict[str, str]]] = {}
    seen: dict[tuple[str, str], int] = {}

    def add(seq: int, level: str, text: str) -> None:
        items = notes.setdefault(seq, [])
        if len(items) < 8:
            items.append({"level": level, "text": text[:600] + ("…" if len(text) > 600 else "")})
        elif len(items) == 8:
            items.append(
                {
                    "level": "notice",
                    "text": "Additional observations in this event; inspect its full details and raw record.",
                }
            )

    for event in events:
        p = event.payload
        if isinstance(p, RequestRetry):
            add(event.seq, "notice", f"Request failed; retry {p.next_attempt}/3 scheduled. {p.message}")
        if isinstance(p, DriveError | CompactionFailed):
            add(event.seq, "error", error_evidence(event)["summary"] if isinstance(p, DriveError) else p.message)
        if isinstance(p, StepEnd | TurnEnd) and p.reason.value in {
            "provider_error",
            "tool_error",
            "interrupted",
        }:
            add(event.seq, "error", "Execution ended: " + p.reason.value)
        if isinstance(p, GoalChecked) and p.is_error:
            add(event.seq, "error", "Goal verification command failed.")
        if isinstance(p, ToolResult):
            errors = sum(
                b.is_error for b in p.item.blocks if b.kind == ContentBlockKind.tool_result
            )
            if errors:
                add(
                    event.seq,
                    "error",
                    f"{errors} tool result(s) reported an error. Check later steps for recovery.",
                )
            if p.truncated:
                add(
                    event.seq,
                    "notice",
                    "Tool output was truncated; the full output was not passed to the model.",
                )
            elif any(
                "[Output truncated" in b.text or "[MCP output truncated" in b.text
                for b in p.item.blocks
            ):
                add(
                    event.seq,
                    "notice",
                    "Tool output contains a truncation notice. Inspect the result for continuation or saved-output information.",
                )
        if isinstance(p, CompactionSeed):
            add(
                event.seq,
                "notice",
                f"Events #{p.covered_begin}–#{p.covered_end} were replaced by a summary in model context, not deleted from history.",
            )
        if isinstance(p, AssistantMessage):
            for b in p.item.blocks:
                if b.kind != ContentBlockKind.tool_call:
                    continue
                try:
                    args = json.dumps(json.loads(b.arguments_json), sort_keys=True)
                except ValueError:
                    args = b.arguments_json
                key = (b.tool_name, args)
                if key in seen:
                    add(
                        event.seq,
                        "notice",
                        f"{b.tool_name} uses the same arguments as event #{seen[key]}. Repetition is not necessarily waste.",
                    )
                seen[key] = event.seq
    return notes


def replay_index(
    events: list[Event], *, offset: int = 0, query: str = "", category: str = "all"
) -> dict[str, Any]:
    notes = _observations(events)
    stops = session_stops(events)
    stop_by_seq = {s["seq"]: s for s in stops}
    rows = []
    turn = step = 0
    query = query.casefold().strip()
    for event in events:
        p = event.payload
        if isinstance(p, TurnStart):
            turn, step = p.turn, 0
        if isinstance(p, StepStart):
            turn, step = p.turn, p.step
        kind = _category(event)
        if category == "all" and kind == "stream":
            continue
        if category == "issues" and event.seq not in notes:
            continue
        stop = stop_by_seq.get(event.seq)
        if category in {"stops", "failures", "followups", "unfinished"}:
            if stop is None or (category == "failures" and not stop["failure"]) or (category == "followups" and not stop["followups"]) or (category == "unfinished" and not stop["unfinished_hint"]):
                continue
        elif category not in {"all", "issues", "raw"} and kind != category:
            continue
        title = stop["label"] if stop else _title(event)
        body = stop_text(stop) if stop else event_text(event) if query else ""
        if query and query not in (title + "\n" + p.kind + "\n" + body).casefold():
            continue
        rows.append(
            {
                "seq": event.seq,
                "at": event.at.isoformat(),
                "kind": p.kind,
                "title": title,
                "turn": turn,
                "step": step,
                "category": kind,
                "observations": notes.get(event.seq, []),
                "stop_reason": stop["reason"] if stop else None,
                "followup_count": len(stop["followups"]) if stop else 0,
                "unfinished_hint": bool(stop and stop["unfinished_hint"]),
            }
        )
    turns = [e.payload for e in events if isinstance(e.payload, TurnEnd)]
    timings = [e for e in events if isinstance(e.payload, AttemptTiming)]
    slowest = max(
        timings,
        key=lambda e: e.payload.elapsed_ms if isinstance(e.payload, AttemptTiming) else 0,
        default=None,
    )
    return {
        "through": events[-1].seq if events else -1,
        "event_count": len(events),
        "total": len(rows),
        "offset": offset,
        "rows": rows[offset : offset + PAGE_SIZE],
        "next_offset": offset + PAGE_SIZE if offset + PAGE_SIZE < len(rows) else None,
        "issue_events": len(notes),
        "issue_sequences": list(notes),
        "slowest_request": {"seq": slowest.seq, "elapsed_ms": slowest.payload.elapsed_ms}
        if slowest and isinstance(slowest.payload, AttemptTiming)
        else None,
        "error_events": sum(any(n["level"] == "error" for n in ns) for ns in notes.values()),
        "requests": sum(isinstance(e.payload, RequestPrepared) for e in events),
        "request_boundaries_recorded": any(isinstance(e.payload, RequestPrepared) for e in events),
        "last_turn_reason": turns[-1].reason.value if turns else None,
        "stops": stop_summary(stops),
        "notice": "Read-only evidence. A completed turn is not a correctness grade. Streaming fragments are available under Raw events.",
    }


def _state(events: list[Event]) -> dict[str, Any]:
    state: dict[str, Any] = {
        "selection": None,
        "goal": None,
        "compaction": None,
        "lifecycle": "not recorded",
    }
    for event in events:
        p = event.payload
        if isinstance(p, SessionStart | Selection | RequestPrepared):
            state["selection"] = {
                "provider": p.provider,
                "model": p.model,
                "effort": getattr(p, "effort", None),
            }
        if isinstance(p, RequestPrepared):
            state["last_prepared_request"] = {
                "seq": event.seq,
                "attempt_id": p.attempt_id,
                "prefix_items": len(p.prefix_items),
            }
        if isinstance(p, GoalChanged):
            state["goal"] = asdict(p)
        if isinstance(p, CompactionSeed):
            state["compaction"] = {
                "seq": event.seq,
                "covered_begin": p.covered_begin,
                "covered_end": p.covered_end,
            }
        if isinstance(p, TurnStart | StepStart):
            state["lifecycle"] = p.kind
        if isinstance(p, StepEnd | TurnEnd):
            state["lifecycle"] = p.kind + ": " + p.reason.value
        if isinstance(p, PromptResolved):
            state["instructions_event"] = event.seq
        if isinstance(p, ToolsAdvertised):
            state["tools"] = [t.name for t in p.tools]
    session = Session(events)
    context = session.model_context()
    state["context"] = {
        "items": len(context.items),
        "blocks_by_kind": dict(
            Counter(b.kind.value for item in context.items for b in item.blocks)
        ),
        "message_text_characters": sum(
            len(b.text) + len(b.arguments_json) for item in context.items for b in item.blocks
        ),
    }
    inbox = session.inbox()
    state["pending_inputs"] = {
        "next_turn": [{"id": m.id, "text": item_text(m.item)} for m in inbox.next_turn],
        "next_step": [{"id": m.id, "text": item_text(m.item)} for m in inbox.next_step],
    }
    return state


def _links(events: list[Event], event: Event) -> list[dict[str, Any]]:
    p = event.payload
    attempt = getattr(p, "attempt_id", None)
    # Call ids can be reused in later responses. Link each result to the latest
    # preceding call, not every historical occurrence of the same provider id.
    call_owners: dict[str, int] = {}
    related_tools: set[int] = set()
    for other in events:
        q = other.payload
        if isinstance(q, AssistantMessage):
            for b in q.item.blocks:
                if b.kind == ContentBlockKind.tool_call:
                    call_owners[b.call_id] = other.seq
        elif isinstance(q, ToolResult):
            owners = {call_owners[b.call_id] for b in q.item.blocks if b.call_id in call_owners}
            if other.seq == event.seq:
                related_tools.update(owners)
            if event.seq in owners:
                related_tools.add(other.seq)
    links = []
    for other in events:
        if other.seq == event.seq or isinstance(other.payload, AssistantChunk):
            continue
        q = other.payload
        related = attempt and getattr(q, "attempt_id", None) == attempt
        related = related or other.seq in related_tools
        if isinstance(p, RequestRetry):
            related = related or getattr(q, "attempt_id", None) == p.next_attempt_id
        if isinstance(q, RequestRetry):
            related = related or attempt == q.next_attempt_id
        if related:
            links.append({"seq": other.seq, "title": _title(other)})
    # A tool result becomes available to the next normal request (possibly through compaction).
    if isinstance(p, ToolResult):
        following = next(
            (e for e in events if e.seq > event.seq and isinstance(e.payload, RequestPrepared)),
            None,
        )
        if following:
            links.append(
                {
                    "seq": following.seq,
                    "title": "Next request · inspect whether this result is retained",
                }
            )
    return links


def replay_detail(
    events: list[Event], seq: int, *, view: str = "detail", offset: int = 0
) -> dict[str, Any]:
    index = next((i for i, e in enumerate(events) if e.seq == seq), None)
    if index is None:
        raise ValueError("Event is not in this snapshot.")
    event = events[index]
    p = event.payload
    stops = session_stops(events)
    stop = next((s for s in stops if s["seq"] == seq), None)
    boundary = event if isinstance(p, RequestPrepared) else None
    attempt = getattr(p, "attempt_id", None)
    if boundary is None and attempt:
        boundary = next(
            (
                e
                for e in events[:index]
                if isinstance(e.payload, RequestPrepared) and e.payload.attempt_id == attempt
            ),
            None,
        )
    context_label = (
        f"Prepared request context · event #{boundary.seq}"
        if boundary
        else f"Reconstructed context before event #{seq}"
    )
    if view == "context":
        boundary_seq = boundary.seq if boundary else seq
        prefix = [e for e in events if e.seq < boundary_seq]
        context = Session(prefix).model_context()
        if boundary and isinstance(boundary.payload, RequestPrepared):
            context.items[:0] = boundary.payload.prefix_items
        tools = payload_to_wire(ToolsAdvertised(context.tools))
        text = (
            context_label
            + "\n\nProvider-neutral reconstruction, not a provider wire capture or hidden reasoning. Auxiliary compaction/goal-check requests do not have normal-turn request boundaries.\n\n"
        )
        if boundary is None:
            text += "No matching request boundary recorded. This event-position view must not be treated as the exact input of a historical request.\n\n"
        selection = (
            {
                "provider": boundary.payload.provider,
                "model": boundary.payload.model,
                "effort": boundary.payload.effort,
            }
            if boundary and isinstance(boundary.payload, RequestPrepared)
            else _state(prefix)["selection"]
        )
        text += "MODEL CONFIGURATION\n" + _json(selection) + "\n\n"
        text += "SYSTEM INSTRUCTIONS\n" + (context.system_prompt or "Not recorded.")
        text += "\n\nTOOL DEFINITIONS\n" + _json(tools)
        text += "\n\nMESSAGES\n" + "\n\n────────────────────\n\n".join(
            item_text(item) for item in context.items
        )
        text += "\n\nPENDING INPUTS (not received by the model)\n" + _json(
            _state(prefix)["pending_inputs"]
        )
    elif view == "state":
        before, after = _state(events[:index]), _state(events[: index + 1])
        changed = {
            key: {"before": before.get(key), "after": after.get(key)}
            for key in after
            if before.get(key) != after.get(key)
        }
        text = "CHANGES AT THIS EVENT\n" + (
            _json(changed) if changed else "No tracked state changed."
        )
        text += "\n\nSTATE AFTER THIS EVENT\n" + _json(after)
        text += "\n\nThis is recorded runtime state, not a filesystem snapshot or model internals."
    elif view == "raw":
        text = encode_record(event)
    else:
        text = stop_text(stop) if stop else event_text(event)
    links = _links(events, event)
    for record in stops:
        related = stop_links(record)
        if any(link["seq"] == seq for link in related):
            links.extend(related)
    links = list({link["seq"]: link for link in links if link["seq"] != seq}.values())
    return {
        "seq": seq,
        "title": stop["label"] if stop else _title(event),
        "kind": p.kind,
        "at": event.at.isoformat(),
        "context_label": context_label,
        "request_boundary": boundary.seq if boundary else None,
        "view": view,
        "offset": offset,
        "text": text[offset : offset + TEXT_PAGE_SIZE],
        "text_length": len(text),
        "next_offset": offset + TEXT_PAGE_SIZE if offset + TEXT_PAGE_SIZE < len(text) else None,
        "links": links,
        "observations": _observations(events).get(seq, []),
    }
