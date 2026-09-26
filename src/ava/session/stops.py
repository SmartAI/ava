"""Stop evidence, not a task-completion classifier. Pure, read-only log projection.

A completed turn is not a completed task. Continuation language and unfinished-work
language are explicit heuristics; user pauses and aborts are not failures. Errors
are joined by turn even when the driver writes them *after* turn/end.
"""
from __future__ import annotations

import json
import re
from typing import Any

from ava.llm.types import ContentBlockKind, Item
from ava.session.event import (
    AssistantChunk,
    AssistantMessage,
    DriveError,
    Event,
    InboxSpliced,
    RequestPrepared,
    StepClaimed,
    TurnEnd,
    TurnStart,
    UserMessage,
)
from ava.session.session import Session

STOP_LABELS = {
    "completed": "Turn ended normally · task completion not verified",
    "provider_error": "Model request failed",
    "tool_error": "Tool execution failed",
    "interrupted": "Execution interrupted · cause not recorded",
    "shutdown": "Stopped for shutdown",
    "user_abort": "Stopped by user",
    "user_pause": "Paused by user",
    "blocked": "Execution blocked",
}
FAILURE_REASONS = {"provider_error", "tool_error", "interrupted", "shutdown", "blocked"}
_CONTINUE = re.compile(
    r"^(?:(?:ok(?:ay)?|好的?|嗯)[\s,，。!！]*)?(?:请\s*)?"
    r"(?:继续|接着做|接着干|continue\b|please\s+continue\b)", re.I,
)
_UNFINISHED = re.compile(
    r"(?:尚未|没有|还没|还未|未能)(?:全部|完全|最终)?完成"
    r"|\bnot (?:yet )?(?:complete|completed|finished|done)\b", re.I,
)


def _text(item: Item) -> str:
    # Never treat file attachments, tool output or opaque reasoning as user intent.
    return "\n".join(b.text for b in item.blocks if b.kind == ContentBlockKind.text).strip()


def continuation_text(item: Item) -> str | None:
    text = _text(item)
    return text if len(text) <= 500 and _CONTINUE.search(text) else None


def error_evidence(event: Event) -> dict[str, Any]:
    p = event.payload
    assert isinstance(p, DriveError)
    try:
        detail = json.loads(p.detail)
    except ValueError:
        detail = {}
    if not isinstance(detail, dict):
        detail = {}
    # Only scalar diagnostic fields cross the summary boundary; the raw event is
    # still available for arbitrary provider detail, large bodies and future keys.
    diagnostics = {key: detail[key] for key in (
        "exception_type", "status", "elapsed_ms", "bytes_received",
        "terminal_received", "last_sse_event", "attempt",
    ) if key in detail and isinstance(detail[key], (str, int, float, bool, type(None)))}
    diagnostics = {k: v[:200] if isinstance(v, str) else v for k, v in diagnostics.items()}
    kind = p.error_kind.value
    message = p.message.strip()
    summary = {"timeout": "Request timed out", "network": "Network request failed"}.get(kind, kind.replace("_", " ").capitalize())
    if diagnostics.get("exception_type"):
        summary += " · " + str(diagnostics["exception_type"])
    status = diagnostics.get("status")
    if isinstance(status, int) and not isinstance(status, bool) and status >= 400:
        summary += f" · HTTP {status}"
    if message and message != "provider request failed:":
        summary += " — " + message
    return {"seq": event.seq, "kind": kind, "summary": summary[:700],
            "diagnostics": diagnostics, "recoverable": p.recoverable}


def session_stops(events: list[Event]) -> list[dict[str, Any]]:
    stops: list[dict[str, Any]] = []
    by_turn: dict[int, dict[str, Any]] = {}
    errors: dict[int, list[dict[str, Any]]] = {}
    activities: dict[int, list[Event]] = {}
    queued: dict[str, dict[str, Any]] = {}
    candidates: list[dict[str, Any]] = []
    open_turn: int | None = None
    turn = 0
    last_response: Event | None = None

    def candidate(event: Event, item: Item, identity: str = "", *, legacy: bool = False) -> dict[str, Any] | None:
        value = continuation_text(item)
        if not value or not stops or (open_turn is not None and not legacy):
            return None
        record = {"stop_seq": stops[-1]["seq"], "input_seq": event.seq,
                  "text": value, "id": identity, "claimed_seq": None,
                  "turn": None, "next_activity_seq": None, "next_activity_kind": None,
                  "legacy": legacy}
        candidates.append(record)
        return record

    for event in events:
        p = event.payload
        if isinstance(p, TurnStart):
            open_turn = turn = p.turn
            last_response = None
        elif isinstance(p, AssistantMessage | AssistantChunk | RequestPrepared):
            activities.setdefault(turn, []).append(event)
            if isinstance(p, AssistantMessage):
                last_response = event
        elif isinstance(p, DriveError):
            errors.setdefault(p.turn, []).append(error_evidence(event))
        elif isinstance(p, TurnEnd):
            response = _text(last_response.payload.item) if last_response and isinstance(last_response.payload, AssistantMessage) else ""
            # Restrict the hint to the final response's opening paragraph. Quoted
            # test output deeper in a response is not a reliable task-state signal.
            opening = response.split("\n\n", 1)[0][:500].replace("**", "")
            unfinished = p.reason.value == "completed" and bool(_UNFINISHED.search(opening))
            stop = {"seq": event.seq, "at": event.at.isoformat(), "turn": p.turn,
                    "reason": p.reason.value, "label": STOP_LABELS[p.reason.value],
                    "failure": p.reason.value in FAILURE_REASONS,
                    "last_response_seq": last_response.seq if last_response else None,
                    "last_response_excerpt": response[:700],
                    "unfinished_hint": unfinished,
                    "errors": [], "followups": []}
            stops.append(stop)
            by_turn[p.turn] = stop
            if open_turn == p.turn:
                open_turn = None
        elif isinstance(p, InboxSpliced):
            for message in p.inserted:
                record = candidate(event, message.item, message.id)
                if record is not None:
                    queued[message.id] = record
        elif isinstance(p, StepClaimed):
            for message in p.claimed:
                record = queued.get(message.id) if message.id else None
                if not message.id and p.step == 1:
                    record = candidate(event, message.item, legacy=True)
                if record is not None:
                    record["claimed_seq"], record["turn"] = event.seq, p.turn
        elif isinstance(p, UserMessage):
            record = candidate(event, p.item, legacy=True)
            if record is not None:
                record["claimed_seq"], record["turn"] = event.seq, turn

    inbox = Session(events).inbox()
    pending = {m.id for m in inbox.next_turn + inbox.next_step}
    by_seq = {stop["seq"]: stop for stop in stops}
    for record in candidates:
        received = record["claimed_seq"] is not None
        if not received and record["id"] not in pending:
            continue  # Deleted/revised queued input never restarted work.
        if received:
            next_event = next((e for e in activities.get(record["turn"], []) if e.seq > record["claimed_seq"]), None)
            if next_event:
                record["next_activity_seq"] = next_event.seq
                record["next_activity_kind"] = next_event.payload.kind
        record["received"] = received
        record.pop("id")
        by_seq[record["stop_seq"]]["followups"].append(record)
    for number, stop in by_turn.items():
        stop["errors"] = errors.get(number, [])
    return stops


def stop_summary(stops: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "total": len(stops),
        "failures": sum(s["failure"] for s in stops),
        "followups": sum(len(s["followups"]) for s in stops),
        "unfinished_hints": sum(s["unfinished_hint"] for s in stops),
        "latest": {key: stops[-1][key] for key in ("seq", "at", "reason", "label")} if stops else None,
    }


def stop_links(stop: dict[str, Any]) -> list[dict[str, Any]]:
    links = [{"seq": stop["seq"], "title": f"Stop · {stop['label']}"}]
    if stop["last_response_seq"] is not None:
        links.append({"seq": stop["last_response_seq"], "title": "Last model response before this stop"})
    links.extend({"seq": e["seq"], "title": e["summary"][:160]} for e in stop["errors"])
    for f in stop["followups"]:
        links.append({"seq": f["input_seq"], "title": "User requested continuation"})
        if f["claimed_seq"] is not None:
            links.append({"seq": f["claimed_seq"], "title": "Continuation input received by agent"})
        if f["next_activity_seq"] is not None:
            links.append({"seq": f["next_activity_seq"], "title": "Following activity · " + f["next_activity_kind"]})
    return list({link["seq"]: link for link in links}.values())


def stop_text(stop: dict[str, Any]) -> str:
    parts = [f"WHY THIS TURN STOPPED\n{stop['label']}\nTurn {stop['turn']} · event #{stop['seq']}",
             "This is an execution outcome, not a task-completion verdict."]
    for error in stop["errors"]:
        parts += [f"ERROR EVIDENCE · #{error['seq']}\n{error['summary']}",
                  json.dumps(error["diagnostics"], ensure_ascii=False, indent=2)]
    if stop["last_response_seq"] is not None:
        parts.append(f"LAST MODEL RESPONSE · #{stop['last_response_seq']}\n" + (stop["last_response_excerpt"] or "No readable text in this response. Inspect the event for tool calls or other blocks."))
    if stop["unfinished_hint"]:
        parts.append("UNFINISHED-WORK LANGUAGE · heuristic\nThe final response mentions unfinished work. Inspect the original instructions; this does not prove the agent should have continued.")
    for f in stop["followups"]:
        parts.append(f"USER REQUESTED CONTINUATION · #{f['input_seq']}\n{f['text']}")
        if f["received"]:
            parts.append(f"Received at #{f['claimed_seq']}. " + (f"Following activity: #{f['next_activity_seq']} ({f['next_activity_kind']})." if f["next_activity_seq"] is not None else "No following model activity recorded in this snapshot."))
        else:
            parts.append("Still queued at this snapshot; not yet received by the agent.")
        parts.append("Continuation wording is a search heuristic, not proof that earlier work was unfinished. A prepared request is not proof of execution or success.")
    return "\n\n".join(parts)
