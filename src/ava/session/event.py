"""The durable session event vocabulary. Values only; no I/O and no wire formats."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import ClassVar

from ava.base import ErrorKind
from ava.llm.types import Item, ToolDef


@dataclass(slots=True)
class SessionStart:
    kind: ClassVar[str] = "session/start"
    id: str
    cwd: str
    provider: str
    model: str
    format: int = 1
    labels: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class PromptResolved:
    kind: ClassVar[str] = "prompt/resolved"
    system_prompt: str


@dataclass(slots=True)
class ToolsAdvertised:
    kind: ClassVar[str] = "tools/advertised"
    tools: list[ToolDef]


@dataclass(slots=True)
class Selection:
    kind: ClassVar[str] = "selection"
    provider: str
    model: str
    effort: str | None = None
    warning: str | None = None


@dataclass(slots=True)
class TurnStart:
    kind: ClassVar[str] = "turn/start"
    turn: int


@dataclass(slots=True)
class StepStart:
    kind: ClassVar[str] = "step/start"
    turn: int
    step: int


class InboxTarget(StrEnum):
    next_turn = "next_turn"
    next_step = "next_step"


@dataclass(slots=True)
class InboxMessage:
    id: str
    item: Item


@dataclass(slots=True)
class InboxSpliced:
    kind: ClassVar[str] = "inbox/spliced"
    target: InboxTarget
    index: int
    removed: int
    inserted: list[InboxMessage]


@dataclass(slots=True)
class StepClaimed:
    kind: ClassVar[str] = "step/claimed"
    turn: int
    step: int
    target: InboxTarget | None
    claimed: list[InboxMessage]


@dataclass(slots=True)
class UserMessage:
    kind: ClassVar[str] = "user/message"
    item: Item


@dataclass(slots=True)
class AssistantChunk:
    kind: ClassVar[str] = "assistant/chunk"
    attempt_id: str
    delta: str


@dataclass(slots=True)
class AssistantMessage:
    kind: ClassVar[str] = "assistant/message"
    attempt_id: str
    item: Item


@dataclass(slots=True)
class Usage:
    kind: ClassVar[str] = "usage"
    attempt_id: str
    input: int | None = None
    cached_read: int | None = None
    cache_write: int | None = None
    cache_write_1h: int | None = None
    output: int | None = None
    reasoning: int | None = None


@dataclass(slots=True)
class AttemptTiming:
    kind: ClassVar[str] = "attempt/timing"
    attempt_id: str
    elapsed_ms: int
    ttft_ms: int | None = None
    ttft_text_ms: int | None = None


@dataclass(slots=True)
class CompactionSeed:
    kind: ClassVar[str] = "compaction/seed"
    covered_begin: int
    covered_end: int
    instruction: str
    item: Item


@dataclass(slots=True)
class CompactionFailed:
    kind: ClassVar[str] = "compaction/failed"
    turn: int
    error_kind: ErrorKind
    message: str


@dataclass(slots=True)
class ToolDuration:
    call_id: str
    elapsed_ms: int


@dataclass(slots=True)
class ToolResult:
    kind: ClassVar[str] = "tool/result"
    item: Item
    durations: list[ToolDuration] = field(default_factory=list)
    truncated: bool = False


class StepEndReason(StrEnum):
    completed = "completed"
    provider_error = "provider_error"
    tool_error = "tool_error"
    user_abort = "user_abort"
    shutdown = "shutdown"
    interrupted = "interrupted"  # crash recovery only


@dataclass(slots=True)
class StepEnd:
    kind: ClassVar[str] = "step/end"
    turn: int
    step: int
    reason: StepEndReason


class TurnEndReason(StrEnum):
    completed = "completed"
    blocked = "blocked"
    user_pause = "user_pause"
    user_abort = "user_abort"
    shutdown = "shutdown"
    provider_error = "provider_error"
    tool_error = "tool_error"
    interrupted = "interrupted"  # crash recovery only; never emitted by a live loop


@dataclass(slots=True)
class TurnEnd:
    kind: ClassVar[str] = "turn/end"
    turn: int
    reason: TurnEndReason
    elapsed_ms: int | None = None


@dataclass(slots=True)
class DriveError:
    kind: ClassVar[str] = "drive/error"
    turn: int
    error_kind: ErrorKind
    message: str
    detail: str = ""
    recoverable: bool = False


@dataclass(slots=True)
class Unknown:
    """A future event kind, preserved byte-identically so it survives a read-write cycle."""

    wire_kind: str
    raw_line: str

    @property
    def kind(self) -> str:
        return self.wire_kind


EventPayload = (
    SessionStart
    | PromptResolved
    | ToolsAdvertised
    | Selection
    | TurnStart
    | StepStart
    | StepClaimed
    | InboxSpliced
    | UserMessage
    | AssistantChunk
    | AssistantMessage
    | Usage
    | AttemptTiming
    | CompactionSeed
    | CompactionFailed
    | ToolResult
    | StepEnd
    | TurnEnd
    | DriveError
    | Unknown
)

KNOWN_PAYLOAD_TYPES: tuple[type, ...] = (
    SessionStart,
    PromptResolved,
    ToolsAdvertised,
    Selection,
    TurnStart,
    StepStart,
    StepClaimed,
    InboxSpliced,
    UserMessage,
    AssistantChunk,
    AssistantMessage,
    Usage,
    AttemptTiming,
    CompactionSeed,
    CompactionFailed,
    ToolResult,
    StepEnd,
    TurnEnd,
    DriveError,
)


def now_ms() -> datetime:
    at = datetime.now(UTC)
    return at.replace(microsecond=at.microsecond // 1000 * 1000)


@dataclass(slots=True)
class Event:
    """Sequence and time belong to the append, never to callers constructing payloads."""

    seq: int
    at: datetime
    payload: EventPayload


def model_item(payload: EventPayload) -> Item | None:
    """The single model-visible item a payload carries, or None. Claims may carry several."""
    if isinstance(payload, UserMessage | AssistantMessage | ToolResult | CompactionSeed):
        return payload.item
    if isinstance(payload, StepClaimed):
        return payload.claimed[0].item if payload.claimed else None
    return None
