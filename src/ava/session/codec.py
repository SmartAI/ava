"""The public JSONL record format (format 1).

Every record carries ``seq``, ``at`` (RFC 3339, millisecond UTC), and ``kind``. Absent means
default: a field at its default is omitted, and readers treat absence as that default.
Enumerations serialize as their lower-snake names. Unknown kinds are preserved verbatim.
"""

from __future__ import annotations

import base64
import binascii
import json
from datetime import UTC, datetime
from typing import Any

from ava.base import AvaError, ErrorKind
from ava.llm.types import (
    ContentBlock,
    ContentBlockKind,
    Item,
    Origin,
    Provenance,
    Role,
    ToolDef,
    ToolParam,
    ToolParamType,
)
from ava.session.event import (
    AssistantChunk,
    AssistantMessage,
    AttemptTiming,
    CompactionFailed,
    CompactionSeed,
    DriveError,
    Event,
    EventPayload,
    InboxMessage,
    InboxSpliced,
    InboxTarget,
    PromptResolved,
    Selection,
    SessionStart,
    StepClaimed,
    StepEnd,
    StepEndReason,
    StepStart,
    ToolDuration,
    ToolResult,
    ToolsAdvertised,
    TurnEnd,
    TurnEndReason,
    TurnStart,
    Unknown,
    Usage,
    UserMessage,
)

MAX_RECORD_BYTES = 32_000_000
FORMAT_VERSION = 1

_MAX_TIME = datetime(9999, 12, 31, 23, 59, 59, 999000, tzinfo=UTC)


def _fail(message: str, detail: str = "", kind: ErrorKind = ErrorKind.parse) -> AvaError:
    return AvaError(kind, message, detail)


# ---- Timestamps -------------------------------------------------------------------------------


def encode_timestamp(at: datetime) -> str:
    if at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    at = at.astimezone(UTC)
    if at.year < 1 or at.year > 9999:
        raise _fail(
            "invalid event timestamp",
            "year must be between 0001 and 9999",
            ErrorKind.invalid_argument,
        )
    return f"{at.year:04}-{at.month:02}-{at.day:02}T{at.hour:02}:{at.minute:02}:{at.second:02}.{at.microsecond // 1000:03}Z"


def decode_timestamp(value: str) -> datetime:
    if (
        len(value) != 24
        or value[4] != "-"
        or value[7] != "-"
        or value[10] != "T"
        or value[13] != ":"
        or value[16] != ":"
        or value[19] != "."
        or value[23] != "Z"
    ):
        raise _fail("invalid event timestamp", "expected YYYY-MM-DDTHH:MM:SS.sssZ")
    try:
        return datetime(
            int(value[0:4]),
            int(value[5:7]),
            int(value[8:10]),
            int(value[11:13]),
            int(value[14:16]),
            int(value[17:19]),
            int(value[20:23]) * 1000,
            tzinfo=UTC,
        )
    except ValueError as error:
        raise _fail("invalid event timestamp", str(error)) from None


# ---- Items and blocks -------------------------------------------------------------------------


def _present(value: str) -> bool:
    return value != ""


def block_to_wire(block: ContentBlock) -> dict[str, Any]:
    wire: dict[str, Any] = {"kind": block.kind.value}
    match block.kind:
        case ContentBlockKind.text:
            if _present(block.text):
                wire["text"] = block.text
        case ContentBlockKind.file_text:
            if _present(block.display_path):
                wire["display_path"] = block.display_path
            if _present(block.text):
                wire["text"] = block.text
        case ContentBlockKind.image:
            if _present(block.display_path):
                wire["display_path"] = block.display_path
            if _present(block.media_type):
                wire["media_type"] = block.media_type
            if block.bytes:
                wire["base64"] = base64.b64encode(block.bytes).decode("ascii")
        case ContentBlockKind.reasoning:
            if _present(block.opaque_json):
                wire["opaque_json"] = block.opaque_json
        case ContentBlockKind.tool_call:
            if _present(block.call_id):
                wire["call_id"] = block.call_id
            if _present(block.tool_name):
                wire["name"] = block.tool_name
            if _present(block.arguments_json):
                wire["arguments"] = block.arguments_json
        case ContentBlockKind.tool_result:
            if _present(block.call_id):
                wire["call_id"] = block.call_id
            if _present(block.text):
                wire["text"] = block.text
            if block.is_error:
                wire["is_error"] = True
    if block.origin != Origin.none:
        wire["origin"] = block.origin.value
    return wire


def _decode_base64(value: str) -> bytes:
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise _fail("invalid image base64", "input is not strict padded base64") from None


def _string(wire: dict[str, Any], name: str) -> str:
    value = wire.get(name)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise _fail("invalid JSONL record", f"field '{name}' must be a string")
    return value


def block_from_wire(wire: dict[str, Any]) -> ContentBlock:
    if not isinstance(wire, dict):
        raise _fail("invalid JSONL record", "block must be an object")
    kind_name = wire.get("kind")
    if not isinstance(kind_name, str):
        raise _fail("invalid JSONL record", "block kind must be a string")
    try:
        kind = ContentBlockKind(kind_name)
    except ValueError:
        raise _fail("invalid JSONL record", f"unknown block kind {kind_name!r}") from None
    origin_name = wire.get("origin")
    origin = Origin.none
    if origin_name is not None:
        try:
            origin = Origin(origin_name)
        except ValueError:
            raise _fail("invalid JSONL record", f"unknown block origin {origin_name!r}") from None
    match kind:
        case ContentBlockKind.text:
            block = ContentBlock(kind=kind, text=_string(wire, "text"))
        case ContentBlockKind.file_text:
            block = ContentBlock(
                kind=kind, display_path=_string(wire, "display_path"), text=_string(wire, "text")
            )
        case ContentBlockKind.image:
            block = ContentBlock(
                kind=kind,
                display_path=_string(wire, "display_path"),
                media_type=_string(wire, "media_type"),
                bytes=_decode_base64(_string(wire, "base64")),
            )
        case ContentBlockKind.reasoning:
            block = ContentBlock(kind=kind, opaque_json=_string(wire, "opaque_json"))
        case ContentBlockKind.tool_call:
            block = ContentBlock(
                kind=kind,
                call_id=_string(wire, "call_id"),
                tool_name=_string(wire, "name"),
                arguments_json=_string(wire, "arguments"),
            )
        case _:
            block = ContentBlock(
                kind=kind,
                call_id=_string(wire, "call_id"),
                text=_string(wire, "text"),
                is_error=bool(wire.get("is_error", False)),
            )
    block.origin = origin
    return block


def item_to_wire(
    item: Item, message_id: str | None = None, source_id: str | None = None
) -> dict[str, Any]:
    wire: dict[str, Any] = {}
    if message_id:
        wire["id"] = message_id
    if source_id:
        wire["source_id"] = source_id
    wire["role"] = item.role.value
    wire["blocks"] = [block_to_wire(block) for block in item.blocks]
    if item.provenance.provider or item.provenance.model:
        provenance: dict[str, str] = {}
        if item.provenance.provider:
            provenance["provider"] = item.provenance.provider
        if item.provenance.model:
            provenance["model"] = item.provenance.model
        wire["provenance"] = provenance
    return wire


def item_from_wire(wire: dict[str, Any]) -> Item:
    if not isinstance(wire, dict):
        raise _fail("invalid JSONL record", "item must be an object")
    try:
        role = Role(wire.get("role", "user"))
    except ValueError:
        raise _fail("invalid JSONL record", f"unknown role {wire.get('role')!r}") from None
    blocks = wire.get("blocks", [])
    if not isinstance(blocks, list):
        raise _fail("invalid JSONL record", "blocks must be an array")
    item = Item(role=role, blocks=[block_from_wire(block) for block in blocks])
    provenance = wire.get("provenance")
    if isinstance(provenance, dict):
        item.provenance = Provenance(
            provider=_string(provenance, "provider"), model=_string(provenance, "model")
        )
    return item


def _tool_to_wire(tool: ToolDef) -> dict[str, Any]:
    params: list[dict[str, Any]] = []
    for param in tool.params:
        wire: dict[str, Any] = {
            "name": param.name,
            "description": param.description,
            "type": param.type.value,
        }
        if param.required:
            wire["required"] = True
        if param.minimum is not None:
            wire["minimum"] = param.minimum
        params.append(wire)
    return {"name": tool.name, "description": tool.description, "params": params}


def _tool_from_wire(wire: dict[str, Any]) -> ToolDef:
    params: list[ToolParam] = []
    for param in wire.get("params", []) or []:
        try:
            param_type = ToolParamType(param.get("type", "string"))
        except ValueError:
            raise _fail("invalid JSONL record", "unknown tool parameter type") from None
        params.append(
            ToolParam(
                name=_string(param, "name"),
                description=_string(param, "description"),
                type=param_type,
                required=bool(param.get("required", False)),
                minimum=param.get("minimum"),
            )
        )
    return ToolDef(
        name=_string(wire, "name"), description=_string(wire, "description"), params=params
    )


# ---- Validation shared by encode and decode -----------------------------------------------------


def validate_step_claimed_payload(value: StepClaimed) -> None:
    if value.target is None:
        if len(value.claimed) != 1 or value.claimed[0].id != "":
            raise _fail(
                "invalid step/claimed payload",
                "legacy claims require exactly one id-less item",
                ErrorKind.invalid_argument,
            )
        return
    if not value.claimed:
        raise _fail(
            "invalid step/claimed payload",
            "identified claims require at least one item",
            ErrorKind.invalid_argument,
        )
    if value.target == InboxTarget.next_turn and len(value.claimed) != 1:
        raise _fail(
            "invalid step/claimed payload",
            "next_turn claims require exactly one item",
            ErrorKind.invalid_argument,
        )
    if any(message.id == "" for message in value.claimed):
        raise _fail(
            "invalid step/claimed payload",
            "identified claims require nonempty ids",
            ErrorKind.invalid_argument,
        )


# ---- Payload encoding ---------------------------------------------------------------------------


def _optional(wire: dict[str, Any], name: str, value: Any) -> None:
    if value is not None:
        wire[name] = value


def payload_to_wire(payload: EventPayload) -> dict[str, Any]:
    wire: dict[str, Any] = {}
    match payload:
        case SessionStart():
            wire.update(
                id=payload.id,
                cwd=payload.cwd,
                provider=payload.provider,
                model=payload.model,
                format=payload.format,
            )
            if payload.labels:
                wire["labels"] = dict(payload.labels)
        case PromptResolved():
            if payload.system_prompt:
                wire["system"] = payload.system_prompt
        case ToolsAdvertised():
            wire["tools"] = [_tool_to_wire(tool) for tool in payload.tools]
        case Selection():
            if not payload.provider or not payload.model or payload.effort == "":
                raise _fail(
                    "invalid selection payload",
                    "provider, model, and any effort must be nonempty",
                    ErrorKind.invalid_argument,
                )
            wire.update(provider=payload.provider, model=payload.model)
            _optional(wire, "effort", payload.effort)
            _optional(wire, "warning", payload.warning)
        case TurnStart():
            wire["turn"] = payload.turn
        case StepStart():
            wire.update(turn=payload.turn, step=payload.step)
        case StepClaimed():
            validate_step_claimed_payload(payload)
            wire.update(turn=payload.turn, step=payload.step)
            if payload.target is not None:
                wire["target"] = payload.target.value
            wire["claimed"] = [
                item_to_wire(message.item, message.id or None, message.source_id or None)
                for message in payload.claimed
            ]
        case InboxSpliced():
            if any(message.id == "" for message in payload.inserted):
                raise _fail(
                    "invalid inbox/spliced payload",
                    "inserted messages require a nonempty id",
                    ErrorKind.invalid_argument,
                )
            wire.update(target=payload.target.value, index=payload.index, removed=payload.removed)
            wire["inserted"] = [
                item_to_wire(message.item, message.id, message.source_id or None)
                for message in payload.inserted
            ]
        case UserMessage():
            wire["item"] = item_to_wire(payload.item)
        case AssistantChunk():
            if payload.attempt_id:
                wire["attempt_id"] = payload.attempt_id
            if payload.delta:
                wire["delta"] = payload.delta
        case AssistantMessage():
            if payload.attempt_id:
                wire["attempt_id"] = payload.attempt_id
            wire["item"] = item_to_wire(payload.item)
        case Usage():
            if payload.attempt_id:
                wire["attempt_id"] = payload.attempt_id
            tokens: dict[str, int] = {}
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
                    tokens[name] = value
            wire["tokens"] = tokens
        case AttemptTiming():
            if payload.attempt_id:
                wire["attempt_id"] = payload.attempt_id
            _optional(wire, "ttft_ms", payload.ttft_ms)
            _optional(wire, "ttft_text_ms", payload.ttft_text_ms)
            wire["elapsed_ms"] = payload.elapsed_ms
        case CompactionSeed():
            wire.update(covered_begin=payload.covered_begin, covered_end=payload.covered_end)
            if payload.instruction:
                wire["instruction"] = payload.instruction
            wire["item"] = item_to_wire(payload.item)
        case CompactionFailed():
            wire.update(turn=payload.turn, error_kind=payload.error_kind.value)
            if payload.message:
                wire["message"] = payload.message
        case ToolResult():
            wire["item"] = item_to_wire(payload.item)
            if payload.durations:
                wire["durations"] = [
                    {
                        **({"call_id": duration.call_id} if duration.call_id else {}),
                        "elapsed_ms": duration.elapsed_ms,
                    }
                    for duration in payload.durations
                ]
            if payload.truncated:
                wire["truncated"] = True
        case StepEnd():
            wire.update(turn=payload.turn, step=payload.step, reason=payload.reason.value)
        case TurnEnd():
            wire.update(turn=payload.turn, reason=payload.reason.value)
            _optional(wire, "elapsed_ms", payload.elapsed_ms)
        case DriveError():
            wire.update(turn=payload.turn, error_kind=payload.error_kind.value)
            if payload.message:
                wire["message"] = payload.message
            if payload.detail:
                wire["detail"] = payload.detail
            if payload.recoverable:
                wire["recoverable"] = True
        case Unknown():
            raise _fail("unknown event bypassed raw encoding", kind=ErrorKind.internal)
    return wire


def encode_record(event: Event) -> str:
    """An LF-free JSON body for one event."""
    if isinstance(event.payload, Unknown):
        decoded = decode_record(event.payload.raw_line)
        if (
            decoded.seq != event.seq
            or decoded.at != event.at
            or not isinstance(decoded.payload, Unknown)
            or decoded.payload.wire_kind != event.payload.wire_kind
        ):
            raise _fail("invalid preserved unknown event", kind=ErrorKind.invalid_argument)
        return event.payload.raw_line
    body: dict[str, Any] = {
        "seq": event.seq,
        "at": encode_timestamp(event.at),
        "kind": event.payload.kind,
    }
    body.update(payload_to_wire(event.payload))
    encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_RECORD_BYTES:
        raise _fail(
            "JSONL record exceeds size limit",
            "record body is larger than 32000000 bytes",
            ErrorKind.invalid_argument,
        )
    return encoded


# ---- Payload decoding ---------------------------------------------------------------------------


def _int(wire: dict[str, Any], name: str, default: int | None = None) -> int:
    value = wire.get(name, default)
    if not isinstance(value, int) or isinstance(value, bool):
        raise _fail("invalid JSONL record", f"field '{name}' must be an integer")
    return value


def _optional_int(wire: dict[str, Any], name: str) -> int | None:
    value = wire.get(name)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise _fail("invalid JSONL record", f"field '{name}' must be an integer")
    return value


def _optional_string(wire: dict[str, Any], name: str) -> str | None:
    value = wire.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise _fail("invalid JSONL record", f"field '{name}' must be a string")
    return value


def _enum(enum_type: type, value: Any, name: str) -> Any:
    try:
        return enum_type(value)
    except ValueError:
        raise _fail("invalid JSONL record", f"unknown {name} {value!r}") from None


def _messages(wire_items: Any, require_id: bool) -> list[InboxMessage]:
    if not isinstance(wire_items, list):
        raise _fail("invalid JSONL record", "messages must be an array")
    messages: list[InboxMessage] = []
    for wire_item in wire_items:
        if not isinstance(wire_item, dict):
            raise _fail("invalid JSONL record", "message must be an object")
        message_id = _string(wire_item, "id")
        if require_id and not message_id:
            raise _fail("invalid inbox/spliced record", "inserted messages require a nonempty id")
        messages.append(
            InboxMessage(
                id=message_id,
                item=item_from_wire(wire_item),
                source_id=_string(wire_item, "source_id"),
            )
        )
    return messages


def decode_known(kind: str, wire: dict[str, Any]) -> EventPayload | None:
    match kind:
        case "session/start":
            format_version = wire.get("format")
            if format_version is None:
                raise _fail("invalid session/start record", "format is required")
            if format_version != FORMAT_VERSION:
                raise _fail(
                    "unsupported session format",
                    f"expected {FORMAT_VERSION}, found {format_version}",
                )
            labels = wire.get("labels") or {}
            if not isinstance(labels, dict):
                raise _fail("invalid session/start record", "labels must be an object")
            return SessionStart(
                id=_string(wire, "id"),
                cwd=_string(wire, "cwd"),
                provider=_string(wire, "provider"),
                model=_string(wire, "model"),
                format=format_version,
                labels={str(key): str(value) for key, value in labels.items()},
            )
        case "prompt/resolved":
            return PromptResolved(system_prompt=_string(wire, "system"))
        case "tools/advertised":
            tools = wire.get("tools", [])
            if not isinstance(tools, list):
                raise _fail("invalid JSONL record", "tools must be an array")
            return ToolsAdvertised(tools=[_tool_from_wire(tool) for tool in tools])
        case "selection":
            selection = Selection(
                provider=_string(wire, "provider"),
                model=_string(wire, "model"),
                effort=_optional_string(wire, "effort"),
                warning=_optional_string(wire, "warning"),
            )
            if not selection.provider or not selection.model or selection.effort == "":
                raise _fail(
                    "invalid selection record", "provider, model, and any effort must be nonempty"
                )
            return selection
        case "turn/start":
            return TurnStart(turn=_int(wire, "turn", 0))
        case "step/start":
            return StepStart(turn=_int(wire, "turn", 0), step=_int(wire, "step", 0))
        case "step/claimed":
            target_name = wire.get("target")
            target = (
                _enum(InboxTarget, target_name, "inbox target") if target_name is not None else None
            )
            claimed = StepClaimed(
                turn=_int(wire, "turn", 0),
                step=_int(wire, "step", 0),
                target=target,
                claimed=_messages(wire.get("claimed", []), require_id=False),
            )
            try:
                validate_step_claimed_payload(claimed)
            except AvaError as error:
                raise _fail("invalid step/claimed record", error.detail) from None
            return claimed
        case "inbox/spliced":
            target_name = wire.get("target")
            if target_name is None:
                raise _fail("invalid inbox/spliced record", "target is required")
            return InboxSpliced(
                target=_enum(InboxTarget, target_name, "inbox target"),
                index=_int(wire, "index", 0),
                removed=_int(wire, "removed", 0),
                inserted=_messages(wire.get("inserted", []), require_id=True),
            )
        case "user/message":
            return UserMessage(item=item_from_wire(wire.get("item", {})))
        case "assistant/chunk":
            return AssistantChunk(
                attempt_id=_string(wire, "attempt_id"), delta=_string(wire, "delta")
            )
        case "assistant/message":
            return AssistantMessage(
                attempt_id=_string(wire, "attempt_id"), item=item_from_wire(wire.get("item", {}))
            )
        case "usage":
            tokens = wire.get("tokens") or {}
            if not isinstance(tokens, dict):
                raise _fail("invalid JSONL record", "tokens must be an object")
            return Usage(
                attempt_id=_string(wire, "attempt_id"),
                input=_optional_int(tokens, "input"),
                cached_read=_optional_int(tokens, "cached_read"),
                cache_write=_optional_int(tokens, "cache_write"),
                cache_write_1h=_optional_int(tokens, "cache_write_1h"),
                output=_optional_int(tokens, "output"),
                reasoning=_optional_int(tokens, "reasoning"),
            )
        case "attempt/timing":
            return AttemptTiming(
                attempt_id=_string(wire, "attempt_id"),
                elapsed_ms=_int(wire, "elapsed_ms", 0),
                ttft_ms=_optional_int(wire, "ttft_ms"),
                ttft_text_ms=_optional_int(wire, "ttft_text_ms"),
            )
        case "compaction/seed":
            return CompactionSeed(
                covered_begin=_int(wire, "covered_begin", 0),
                covered_end=_int(wire, "covered_end", 0),
                instruction=_string(wire, "instruction"),
                item=item_from_wire(wire.get("item", {})),
            )
        case "compaction/failed":
            return CompactionFailed(
                turn=_int(wire, "turn", 0),
                error_kind=_enum(ErrorKind, wire.get("error_kind", "internal"), "error kind"),
                message=_string(wire, "message"),
            )
        case "tool/result":
            durations = wire.get("durations") or []
            if not isinstance(durations, list):
                raise _fail("invalid JSONL record", "durations must be an array")
            return ToolResult(
                item=item_from_wire(wire.get("item", {})),
                durations=[
                    ToolDuration(
                        call_id=_string(duration, "call_id"),
                        elapsed_ms=_int(duration, "elapsed_ms", 0),
                    )
                    for duration in durations
                ],
                truncated=bool(wire.get("truncated", False)),
            )
        case "step/end":
            return StepEnd(
                turn=_int(wire, "turn", 0),
                step=_int(wire, "step", 0),
                reason=_enum(StepEndReason, wire.get("reason", "completed"), "step end reason"),
            )
        case "turn/end":
            return TurnEnd(
                turn=_int(wire, "turn", 0),
                reason=_enum(TurnEndReason, wire.get("reason", "completed"), "turn end reason"),
                elapsed_ms=_optional_int(wire, "elapsed_ms"),
            )
        case "drive/error":
            return DriveError(
                turn=_int(wire, "turn", 0),
                error_kind=_enum(ErrorKind, wire.get("error_kind", "internal"), "error kind"),
                message=_string(wire, "message"),
                detail=_string(wire, "detail"),
                recoverable=bool(wire.get("recoverable", False)),
            )
    return None


def decode_record(raw_line: str) -> Event:
    if "\n" in raw_line:
        raise _fail("invalid JSONL record", "record contains a newline")
    try:
        wire = json.loads(raw_line)
    except json.JSONDecodeError as error:
        raise _fail("invalid JSONL record", str(error)) from None
    if not isinstance(wire, dict):
        raise _fail("invalid JSONL record", "record is not an object")
    for name in ("seq", "at", "kind"):
        if name not in wire:
            raise _fail("invalid JSONL record", f"missing required field '{name}'")
    seq = _int(wire, "seq")
    if seq < 0:
        raise _fail("invalid JSONL record", "seq must be non-negative")
    at = decode_timestamp(_string(wire, "at"))
    kind = _string(wire, "kind")
    payload = decode_known(kind, wire)
    if payload is None:
        payload = Unknown(wire_kind=kind, raw_line=raw_line)
    return Event(seq=seq, at=at, payload=payload)


def validate_step_claimed_record(target: InboxTarget, item: Item) -> None:
    """Worst-case sizing so an accepted inbox item cannot later fail its claim append."""
    maximum = 2**64 - 1
    encode_record(
        Event(
            seq=maximum,
            at=_MAX_TIME,
            payload=StepClaimed(
                turn=maximum,
                step=maximum,
                target=target,
                claimed=[InboxMessage(id=f"m-{maximum}", item=item, source_id=f"m-{maximum}")],
            ),
        )
    )
