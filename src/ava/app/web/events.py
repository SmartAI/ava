"""The stable browser wire vocabulary: a JSON projection of session events.

Model-only bytes (image data, file text, opaque reasoning state, prompt text, tool schemas) never
reach the browser; the shell folds only this stream into DOM nodes.
"""

from __future__ import annotations

import json
from typing import Any

from ava.base import AvaError, ErrorKind
from ava.llm import ContentBlockKind, Item, Origin
from ava.session import (
    AssistantChunk,
    AssistantMessage,
    AttemptTiming,
    CompactionFailed,
    CompactionSeed,
    DriveError,
    Event,
    InboxSpliced,
    InboxTarget,
    Selection,
    StepClaimed,
    StepEnd,
    StepStart,
    ToolResult,
    TurnEnd,
    TurnStart,
    Unknown,
    Usage,
    UserMessage,
)


def blocks_json(item: Item) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for source in item.blocks:
        block: dict[str, Any] = {"kind": source.kind.value}
        match source.kind:
            case ContentBlockKind.text:
                block["text"] = source.text
            case ContentBlockKind.file_text:
                block["display_path"] = source.display_path
                block["byte_size"] = len(source.text.encode("utf-8"))
            case ContentBlockKind.image:
                block["display_path"] = source.display_path
                block["media_type"] = source.media_type
                block["byte_size"] = len(source.bytes)
            case ContentBlockKind.reasoning:
                pass  # opaque provider state is model-visible but never presentation-visible
            case ContentBlockKind.tool_call:
                block["call_id"] = source.call_id
                block["tool_name"] = source.tool_name
                block["arguments_json"] = source.arguments_json
            case ContentBlockKind.tool_result:
                block["call_id"] = source.call_id
                block["text"] = source.text
                block["is_error"] = source.is_error
        if source.origin != Origin.none:
            block["origin"] = source.origin.value
        blocks.append(block)
    return blocks


def _validate_claim(claimed: StepClaimed) -> None:
    invalid = (
        not claimed.claimed
        or (claimed.target is None and (len(claimed.claimed) != 1 or claimed.claimed[0].id))
        or (claimed.target == InboxTarget.next_turn and len(claimed.claimed) != 1)
        or (claimed.target is not None and any(not message.id for message in claimed.claimed))
    )
    if invalid:
        raise AvaError(
            ErrorKind.invalid_argument, "step/claimed messages must match their durable target"
        )


def event_dict(event: Event) -> dict[str, Any]:
    payload = event.payload
    out: dict[str, Any] = {"seq": event.seq, "kind": payload.kind}
    match payload:
        case Selection():
            out["provider"] = payload.provider
            out["model"] = payload.model
            if payload.effort is not None:
                out["effort"] = payload.effort
            if payload.warning is not None:
                out["warning"] = payload.warning
        case TurnStart():
            out["turn"] = payload.turn
        case StepStart():
            out["turn"] = payload.turn
            out["step"] = payload.step
        case StepClaimed():
            _validate_claim(payload)
            out["turn"] = payload.turn
            out["step"] = payload.step
            if payload.target is not None:
                out["target"] = payload.target.value
            out["messages"] = [
                {**({"id": message.id} if message.id else {}), "blocks": blocks_json(message.item)}
                for message in payload.claimed
            ]
        case InboxSpliced():
            out["target"] = payload.target.value
            out["index"] = payload.index
            out["removed"] = payload.removed
            out["inserted"] = [
                {"id": message.id, "blocks": blocks_json(message.item)}
                for message in payload.inserted
            ]
        case UserMessage():
            out["blocks"] = blocks_json(payload.item)
        case AssistantChunk():
            out["attempt_id"] = payload.attempt_id
            out["delta"] = payload.delta
        case AssistantMessage():
            out["attempt_id"] = payload.attempt_id
            out["blocks"] = blocks_json(payload.item)
        case Usage():
            out["attempt_id"] = payload.attempt_id
            for name in (
                "input",
                "cached_read",
                "cache_write",
                "cache_write_1h",
                "output",
                "reasoning",
            ):
                value = getattr(payload, name)
                if value is not None:
                    out[name] = value
        case AttemptTiming():
            out["attempt_id"] = payload.attempt_id
            if payload.ttft_ms is not None:
                out["ttft_ms"] = payload.ttft_ms
            if payload.ttft_text_ms is not None:
                out["ttft_text_ms"] = payload.ttft_text_ms
            out["elapsed_ms"] = payload.elapsed_ms
        case CompactionSeed():
            out["covered_begin"] = payload.covered_begin
            out["covered_end"] = payload.covered_end
            out["instruction"] = payload.instruction
            out["blocks"] = blocks_json(payload.item)
        case CompactionFailed():
            out["turn"] = payload.turn
            out["error_kind"] = payload.error_kind.value
            out["message"] = payload.message
        case ToolResult():
            out["blocks"] = blocks_json(payload.item)
            if payload.durations:
                out["durations"] = [
                    {"call_id": duration.call_id, "elapsed_ms": duration.elapsed_ms}
                    for duration in payload.durations
                ]
            out["truncated"] = payload.truncated
        case StepEnd():
            out["turn"] = payload.turn
            out["step"] = payload.step
            out["reason"] = payload.reason.value
        case TurnEnd():
            out["turn"] = payload.turn
            out["reason"] = payload.reason.value
            if payload.elapsed_ms is not None:
                out["elapsed_ms"] = payload.elapsed_ms
        case DriveError():
            out["turn"] = payload.turn
            out["error_kind"] = payload.error_kind.value
            out["message"] = payload.message
            out["detail"] = payload.detail
            out["recoverable"] = payload.recoverable
        case Unknown():
            out["kind"] = payload.wire_kind
        case _:
            pass  # session/start, prompt/resolved, tools/advertised carry only seq and kind
    return out


def event_json(event: Event) -> str:
    return json.dumps(event_dict(event), ensure_ascii=False, separators=(",", ":"))
