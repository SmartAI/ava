"""Append-only semantic repair for a session interrupted at its durable tail.

Recovery validates every lifecycle transition and repairs by appending closers marked
``interrupted``; it never truncates a complete record.
"""

from __future__ import annotations

from ava.base import AvaError, ErrorKind
from ava.llm.types import ContentBlockKind, Item, Origin, Role, make_tool_result_block
from ava.session.event import (
    AssistantChunk,
    AssistantMessage,
    CompactionSeed,
    Event,
    EventPayload,
    SessionStart,
    StepClaimed,
    StepEnd,
    StepEndReason,
    StepStart,
    ToolResult,
    TurnEnd,
    TurnEndReason,
    TurnStart,
    UserMessage,
)

INTERRUPTED_TOOL_TEXT = (
    "[Tool call interrupted because the previous Ava process exited before recording a result.]"
)


def _lifecycle_error(detail: str) -> AvaError:
    return AvaError(ErrorKind.parse, "cannot resume invalid session lifecycle", detail)


class _LifecycleScan:
    def __init__(self) -> None:
        self.turn: TurnStart | None = None
        self.step: StepStart | None = None
        self.last_turn: int | None = None
        self.last_step: int | None = None
        self.required_turn_reason: TurnEndReason | None = None
        self.unanswered_calls: list[str] = []

    def consume(self, event: Event) -> None:
        payload = event.payload
        match payload:
            case TurnStart():
                self._start_turn(payload)
            case TurnEnd():
                self._end_turn(payload)
            case StepStart():
                self._start_step(payload)
            case StepEnd():
                self._end_step(payload)
            case StepClaimed():
                self._claim_step(payload)
            case UserMessage() | CompactionSeed():
                self._check_model_item()
            case AssistantChunk():
                self._check_chunk(payload)
            case AssistantMessage():
                self._add_calls(payload)
            case ToolResult():
                self._answer_calls(payload)

    def finish(self) -> list[EventPayload]:
        repair: list[EventPayload] = []
        if self.unanswered_calls:
            item = Item(role=Role.tool)
            for call_id in self.unanswered_calls:
                block = make_tool_result_block(call_id, INTERRUPTED_TOOL_TEXT, True)
                block.origin = Origin.interrupted
                item.blocks.append(block)
            repair.append(ToolResult(item=item))
        if self.step is not None:
            repair.append(
                StepEnd(turn=self.step.turn, step=self.step.step, reason=StepEndReason.interrupted)
            )
        if self.turn is not None:
            repair.append(TurnEnd(turn=self.turn.turn, reason=TurnEndReason.interrupted))
        return repair

    def _start_turn(self, started: TurnStart) -> None:
        if self.turn is not None:
            raise _lifecycle_error(
                f"turn {started.turn} started while turn {self.turn.turn} is still open"
            )
        if self.step is not None:
            raise _lifecycle_error("a turn started while a step is still open")
        if self.unanswered_calls:
            raise _lifecycle_error(
                f"a new turn started before tool call '{self.unanswered_calls[0]}' received a result"
            )
        if self.last_turn is not None and started.turn <= self.last_turn:
            raise _lifecycle_error(
                f"turn {started.turn} does not advance the previous turn ordinal {self.last_turn}"
            )
        self.turn = started
        self.last_turn = started.turn
        self.last_step = None

    def _end_turn(self, ended: TurnEnd) -> None:
        if self.turn is None:
            raise _lifecycle_error(f"turn {ended.turn} ended without a matching turn/start")
        if self.turn.turn != ended.turn:
            raise _lifecycle_error(
                f"turn/end {ended.turn} does not match open turn {self.turn.turn}"
            )
        if self.step is not None:
            raise _lifecycle_error(
                f"turn {ended.turn} ended while step {self.step.step} is still open"
            )
        if self.unanswered_calls and ended.reason not in (
            TurnEndReason.provider_error,
            TurnEndReason.tool_error,
        ):
            raise _lifecycle_error(f"turn {ended.turn} ended without results for every tool call")
        if self.required_turn_reason is not None and self.required_turn_reason != ended.reason:
            raise _lifecycle_error(
                f"turn {ended.turn} end reason does not match its preceding error step"
            )
        self.turn = None
        if not self.unanswered_calls:
            self.required_turn_reason = None

    def _start_step(self, started: StepStart) -> None:
        if self.turn is None:
            raise _lifecycle_error(
                f"step {started.step} of turn {started.turn} started outside an open turn"
            )
        if self.turn.turn != started.turn:
            raise _lifecycle_error(
                f"step belongs to turn {started.turn} while open turn is {self.turn.turn}"
            )
        if self.step is not None:
            raise _lifecycle_error(
                f"step {started.step} started while step {self.step.step} is still open"
            )
        if self.unanswered_calls:
            raise _lifecycle_error(
                f"a new step started before tool call '{self.unanswered_calls[0]}' received a result"
            )
        if self.required_turn_reason is not None:
            raise _lifecycle_error("a new step started while the turn must end after error repair")
        if self.last_step is not None and started.step <= self.last_step:
            raise _lifecycle_error(
                f"step {started.step} of turn {started.turn} does not advance the previous step "
                f"ordinal {self.last_step}"
            )
        self.step = started
        self.last_step = started.step

    def _end_step(self, ended: StepEnd) -> None:
        if self.step is None:
            raise _lifecycle_error(
                f"step {ended.step} of turn {ended.turn} ended without a matching step/start"
            )
        if self.step.turn != ended.turn or self.step.step != ended.step:
            raise _lifecycle_error(
                f"step/end does not match open step {self.step.step} of turn {self.step.turn}"
            )
        if self.unanswered_calls and ended.reason not in (
            StepEndReason.provider_error,
            StepEndReason.tool_error,
        ):
            raise _lifecycle_error(f"step {ended.step} ended without results for every tool call")
        if self.unanswered_calls:
            self.required_turn_reason = (
                TurnEndReason.provider_error
                if ended.reason == StepEndReason.provider_error
                else TurnEndReason.tool_error
            )
        self.step = None

    def _claim_step(self, claimed: StepClaimed) -> None:
        if self.step is None or self.step.turn != claimed.turn or self.step.step != claimed.step:
            raise _lifecycle_error("step/claimed does not belong to the open step")
        if self.unanswered_calls:
            raise _lifecycle_error("step/claimed appears after an unanswered tool call")

    def _check_model_item(self) -> None:
        if self.unanswered_calls:
            raise _lifecycle_error("model-visible content appears after an unanswered tool call")
        if self.required_turn_reason is not None:
            raise _lifecycle_error(
                "model-visible content appears while the turn must end after error repair"
            )

    def _check_chunk(self, chunk: AssistantChunk) -> None:
        if self.step is None:
            raise _lifecycle_error("assistant/chunk appears outside an open step")
        if not chunk.attempt_id:
            raise _lifecycle_error("assistant/chunk has an empty attempt id")

    def _add_calls(self, assistant: AssistantMessage) -> None:
        if self.step is None:
            raise _lifecycle_error("assistant/message appears outside an open step")
        if not assistant.attempt_id:
            raise _lifecycle_error("assistant/message has an empty attempt id")
        if self.unanswered_calls:
            raise _lifecycle_error("assistant/message appears after an unanswered tool call")
        for block in assistant.item.blocks:
            if block.kind != ContentBlockKind.tool_call:
                continue
            if not block.call_id or block.call_id in self.unanswered_calls:
                raise _lifecycle_error(
                    "assistant/message contains an empty or duplicate tool call id"
                )
            self.unanswered_calls.append(block.call_id)

    def _answer_calls(self, result: ToolResult) -> None:
        for block in result.item.blocks:
            if block.kind != ContentBlockKind.tool_result:
                continue
            if block.call_id not in self.unanswered_calls:
                raise _lifecycle_error(f"tool/result answers unknown call '{block.call_id}'")
            self.unanswered_calls.remove(block.call_id)
        if not self.unanswered_calls:
            if (
                self.required_turn_reason is not None
                and self.turn is not None
                and self.step is None
            ):
                self.required_turn_reason = TurnEndReason.interrupted
            else:
                self.required_turn_reason = None


def plan_lifecycle_repair(events: list[Event]) -> list[EventPayload]:
    """Validate the full lifecycle and return the closers a recoverable tail needs."""
    if not events or not isinstance(events[0].payload, SessionStart):
        raise _lifecycle_error("session/start is missing")
    scan = _LifecycleScan()
    for event in events[1:]:
        scan.consume(event)
    return scan.finish()
