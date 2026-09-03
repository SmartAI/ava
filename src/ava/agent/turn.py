"""One durable turn: steps until nothing is owed, with boundaries, abort repair, and tools.

The inbox gate serializes acknowledgements, claims, and boundary decisions. A step claims one
next-turn message (first step only) and every pending next-step message, in one durable record
each. Every exit path leaves history well-formed.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from ava.agent.compaction import CompactionOutcome, compact, select_compaction_end
from ava.agent.state import AgentState, TurnOutcome
from ava.agent.step import StepResult, append_accounting, step
from ava.base import AvaError, CancelToken, ErrorKind
from ava.llm import Item, Role, StopReason, is_context_overflow, resolve_selection_model
from ava.llm.types import ContentBlockKind, Origin, make_tool_result_block
from ava.session import (
    AssistantMessage,
    InboxMessage,
    InboxTarget,
    StepClaimed,
    StepEnd,
    StepEndReason,
    StepStart,
    ToolDuration,
    ToolResult,
    TurnEnd,
    TurnEndReason,
)
from ava.session import compaction as strategy
from ava.tool import Output

INTERRUPTED_TOOL_TEXT = "[Tool call interrupted by user abort before it finished.]"
SKIPPED_TOOL_TEXT = "[Tool call skipped: the turn was aborted before it started.]"
FAILED_TOOL_TEXT = "[Tool call failed before Ava could record a result.]"


class TurnFailure(Exception):
    """A turn ended with an error; carries the durable reason for its closing record."""

    def __init__(self, error: AvaError, reason: TurnEndReason) -> None:
        super().__init__(error.message)
        self.error = error
        self.reason = reason


@dataclass(slots=True)
class _Turn:
    state: AgentState
    turn_number: int
    cancel: CancelToken
    input: InboxMessage | None
    steering: list[InboxMessage]
    holds_gate: bool
    compaction_failed: bool = False
    overflow_recovered: bool = False
    retrying_after_overflow: bool = False
    step_scope: StepStart = field(default_factory=lambda: StepStart(turn=0, step=0))

    @property
    def drive(self):
        return self.state.drive_state

    # ---- boundary records ------------------------------------------------------------------

    def end_step(self, reason: StepEndReason) -> None:
        self.state.append(
            StepEnd(turn=self.step_scope.turn, step=self.step_scope.step, reason=reason)
        )
        self.state.drain()

    def release_gate(self) -> None:
        if self.holds_gate:
            self.state.inbox_gate.release()
            self.holds_gate = False

    async def acquire_gate(self) -> None:
        if not self.holds_gate:
            await self.state.inbox_gate.acquire()
            self.holds_gate = True

    async def finish_aborted_turn(self) -> TurnOutcome:
        await self.acquire_gate()
        try:
            if not self.drive.finish_abort():
                raise AvaError(ErrorKind.internal, "cannot finish the aborted turn")
            self.state.acknowledge(TurnEnd(turn=self.turn_number, reason=TurnEndReason.user_abort))
        finally:
            self.release_gate()
        return TurnOutcome.aborted

    async def finish_aborted_step(self) -> TurnOutcome:
        self.end_step(StepEndReason.user_abort)
        return await self.finish_aborted_turn()

    async def pause_at_boundary(self) -> bool:
        """Called while holding the gate after a completed step."""
        if not self.drive.pause_requested:
            return False
        if not self.drive.close_turn():
            raise AvaError(ErrorKind.internal, "cannot pause while tool results remain owed")
        if not self.drive.finish_pause():
            raise AvaError(ErrorKind.internal, "cannot enter the paused state")
        try:
            self.state.acknowledge(TurnEnd(turn=self.turn_number, reason=TurnEndReason.user_pause))
        finally:
            self.release_gate()
        return True

    async def prepare_next_step(self) -> None:
        """Acquire the gate and snapshot every pending next-step message for the next claim."""
        await self.acquire_gate()
        self.steering = list(self.state.session.inbox().next_step)

    async def boundary_after_completed_step(self) -> TurnOutcome | None:
        """Decide at a completed step seam: abort, pause, or continue. Returns None to continue."""
        await self.prepare_next_step()
        if self.drive.abort_requested:
            return await self.finish_aborted_turn()
        if await self.pause_at_boundary():
            return TurnOutcome.paused
        return None

    def claim_step_messages(self) -> None:
        try:
            if self.input is not None:
                self.state.acknowledge(
                    StepClaimed(
                        turn=self.step_scope.turn,
                        step=self.step_scope.step,
                        target=InboxTarget.next_turn,
                        claimed=[self.input],
                    )
                )
                self.input = None
            if self.steering:
                self.state.acknowledge(
                    StepClaimed(
                        turn=self.step_scope.turn,
                        step=self.step_scope.step,
                        target=InboxTarget.next_step,
                        claimed=list(self.steering),
                    )
                )
                self.steering = []
        finally:
            self.release_gate()
        self.state.drain()

    # ---- selection and compaction ----------------------------------------------------------

    async def apply_pending_selection(self) -> None:
        provider = self.state.provider
        while self.state.pending_selection is not None:
            pending, self.state.pending_selection = self.state.pending_selection, None
            provider.selection = pending
            provider.selection_model_may_be_alias = (
                pending.model in provider.model_aliases or _is_alias(pending.model)
            )
            await resolve_selection_model(provider, self.cancel)
            provider.context_window = (
                provider.capabilities(provider.selection.model).context_window_tokens or 0
            )
            if self.drive.abort_requested:
                return

    async def maybe_compact_at_seam(self) -> TurnOutcome | None:
        options = self.state.compaction_options
        provider = self.state.provider
        bypass = self.retrying_after_overflow
        self.retrying_after_overflow = False
        if (
            not options.enabled
            or bypass
            or self.compaction_failed
            or provider.context_window == 0
            or strategy.estimate_context_tokens(self.state.session)
            < provider.context_window * options.threshold_percent // 100
        ):
            return None
        prefix = f"compact-{self.turn_number}-step-{self.step_scope.step}"
        try:
            outcome = await compact(
                self.state,
                self.turn_number,
                prefix,
                select_compaction_end(self.state),
                self.drive,
                self.cancel,
            )
        except AvaError:
            if self.drive.abort_requested:
                return await self.finish_aborted_step()
            raise
        self.compaction_failed = outcome == CompactionOutcome.failed
        return None

    # ---- provider error path ---------------------------------------------------------------

    async def handle_provider_error(
        self, attempt_id: str, result: StepResult
    ) -> TurnOutcome | None:
        """Returns an outcome when the turn ends; None when overflow recovery prepared a retry."""
        assert result.error is not None
        error = result.error
        overflow = is_context_overflow(error)
        options = self.state.compaction_options
        recover = (
            options.enabled
            and self.state.provider.context_window != 0
            and not self.overflow_recovered
            and overflow
        )
        # Selecting before step/end keeps the rejected request in the verbatim retry tail.
        covered_end = select_compaction_end(self.state) if recover else None
        append_accounting(
            self.state, attempt_id, result.usage, result.timing, append_empty_usage=overflow
        )
        self.end_step(StepEndReason.provider_error)
        if self.drive.abort_requested:
            return await self.finish_aborted_turn()
        if not recover:
            raise TurnFailure(error, TurnEndReason.provider_error)
        prefix = f"compact-{self.turn_number}-step-{self.step_scope.step + 1}"
        try:
            outcome = await compact(
                self.state, self.turn_number, prefix, covered_end, self.drive, self.cancel
            )
        except AvaError as failure:
            if self.drive.abort_requested:
                return await self.finish_aborted_turn()
            raise TurnFailure(failure, TurnEndReason.provider_error) from failure
        if self.drive.abort_requested:
            return await self.finish_aborted_turn()
        if outcome != CompactionOutcome.compacted:
            raise TurnFailure(error, TurnEndReason.provider_error)
        await self.prepare_next_step()
        self.overflow_recovered = True
        self.retrying_after_overflow = True
        return None

    # ---- abort repair ----------------------------------------------------------------------

    async def abort_provider_step(self, attempt_id: str, result: StepResult) -> TurnOutcome:
        """Keep partial text marked interrupted, drop the open call, skip emitted calls."""
        blocks = result.assistant.blocks
        if result.open_tool_call is not None and result.open_tool_call < len(blocks):
            del blocks[result.open_tool_call]
        skipped = Item(role=Role.tool)
        for block in blocks:
            if block.kind == ContentBlockKind.text and result.stop_reason is None:
                block.origin = Origin.interrupted
            elif block.kind == ContentBlockKind.tool_call:
                repair = make_tool_result_block(block.call_id, SKIPPED_TOOL_TEXT, True)
                repair.origin = Origin.skipped
                skipped.blocks.append(repair)
        if blocks:
            self.state.append(AssistantMessage(attempt_id=attempt_id, item=result.assistant))
        if skipped.blocks:
            self.state.append(ToolResult(item=skipped))
        append_accounting(self.state, attempt_id, result.usage, result.timing)
        return await self.finish_aborted_step()

    async def abort_tool_step(
        self,
        assistant: Item,
        calls: list[int],
        first_unfinished: int,
        active_interrupted: bool,
        results: Item,
    ) -> TurnOutcome:
        for position in range(first_unfinished, len(calls)):
            call = assistant.blocks[calls[position]]
            interrupted = active_interrupted and position == first_unfinished
            repair = make_tool_result_block(
                call.call_id, INTERRUPTED_TOOL_TEXT if interrupted else SKIPPED_TOOL_TEXT, True
            )
            repair.origin = Origin.interrupted if interrupted else Origin.skipped
            results.blocks.append(repair)
        self.state.append(ToolResult(item=results))
        self.drive.tool_results_owed = False
        return await self.finish_aborted_step()

    # ---- tools -----------------------------------------------------------------------------

    async def execute_tool_calls(self, assistant: Item, calls: list[int]) -> TurnOutcome | None:
        """Sequential dispatch in request order. Returns None when the turn continues."""
        self.drive.tool_results_owed = True
        results = Item(role=Role.tool)
        durations: list[ToolDuration] = []
        for position, call_index in enumerate(calls):
            if self.drive.abort_requested:
                return await self.abort_tool_step(assistant, calls, position, False, results)
            call = assistant.blocks[call_index]
            started = time.monotonic()
            tool = self.state.find_tool(call.tool_name)
            output: Output | None = None
            failure: AvaError | None = None
            if tool is None:
                names = " ".join(candidate.name for candidate in self.state.tools)
                output = Output(
                    text=f"unknown tool '{call.tool_name}'. Available tools: {names}", is_error=True
                )
            else:
                try:
                    output = await tool.run(call.arguments_json, self.cancel)
                except AvaError as error:
                    failure = error
            if self.drive.abort_requested:
                if output is not None:
                    results.blocks.append(
                        make_tool_result_block(call.call_id, output.text, output.is_error)
                    )
                    return await self.abort_tool_step(
                        assistant, calls, position + 1, False, results
                    )
                return await self.abort_tool_step(assistant, calls, position, True, results)
            if failure is not None:
                for pending_position in range(position, len(calls)):
                    pending = assistant.blocks[calls[pending_position]]
                    skipped = pending_position != position
                    repair = make_tool_result_block(
                        pending.call_id,
                        SKIPPED_TOOL_TEXT if skipped else FAILED_TOOL_TEXT,
                        True,
                    )
                    if skipped:
                        repair.origin = Origin.skipped
                    results.blocks.append(repair)
                self.state.append(ToolResult(item=results, durations=durations))
                self.drive.tool_results_owed = False
                self.end_step(StepEndReason.tool_error)
                raise TurnFailure(failure, TurnEndReason.tool_error)
            assert output is not None
            durations.append(
                ToolDuration(
                    call_id=call.call_id, elapsed_ms=int((time.monotonic() - started) * 1000)
                )
            )
            results.blocks.append(
                make_tool_result_block(call.call_id, output.text, output.is_error)
            )
        self.state.append(ToolResult(item=results, durations=durations))
        self.drive.tool_results_owed = False
        self.end_step(StepEndReason.completed)
        return await self.boundary_after_completed_step()

    # ---- the middle loop -------------------------------------------------------------------

    async def run(self) -> TurnOutcome:
        step_number = 0
        while True:
            step_number += 1
            self.step_scope = StepStart(turn=self.turn_number, step=step_number)
            self.state.append(self.step_scope)
            self.claim_step_messages()
            if self.drive.abort_requested:
                return await self.finish_aborted_step()
            await self.apply_pending_selection()
            if self.drive.abort_requested:
                return await self.finish_aborted_step()
            aborted = await self.maybe_compact_at_seam()
            if aborted is not None:
                return aborted
            if self.drive.abort_requested:
                return await self.finish_aborted_step()

            attempt_id = f"turn-{self.turn_number}-step-{step_number}"
            result = await step(
                self.state, self.state.session.model_context(), attempt_id, True, self.cancel
            )
            if self.drive.abort_requested:
                return await self.abort_provider_step(attempt_id, result)
            if result.error is not None:
                outcome = await self.handle_provider_error(attempt_id, result)
                if outcome is not None:
                    return outcome
                continue

            calls: list[int] = []
            for index, block in enumerate(result.assistant.blocks):
                if block.kind == ContentBlockKind.tool_call:
                    calls.append(index)
                elif block.kind not in (ContentBlockKind.text, ContentBlockKind.reasoning):
                    self.end_step(StepEndReason.provider_error)
                    if self.drive.abort_requested:
                        return await self.finish_aborted_turn()
                    raise TurnFailure(
                        AvaError(
                            ErrorKind.internal,
                            "assistant assembly produced a block kind unavailable from the provider",
                        ),
                        TurnEndReason.provider_error,
                    )
            assistant = result.assistant
            self.state.append(AssistantMessage(attempt_id=attempt_id, item=assistant))
            append_accounting(self.state, attempt_id, result.usage, result.timing)
            self.state.drain()
            if self.drive.abort_requested:
                if not calls:
                    return await self.finish_aborted_step()
                return await self.abort_tool_step(assistant, calls, 0, False, Item(role=Role.tool))

            if result.stop_reason == StopReason.max_tokens:
                self.end_step(StepEndReason.provider_error)
                if self.drive.abort_requested:
                    return await self.finish_aborted_turn()
                raise TurnFailure(
                    AvaError(
                        ErrorKind.provider,
                        "model response reached the output-token limit and may be incomplete; retry with a narrower task",
                    ),
                    TurnEndReason.provider_error,
                )
            if result.stop_reason == StopReason.end_turn:
                if calls:
                    self.end_step(StepEndReason.provider_error)
                    if self.drive.abort_requested:
                        return await self.finish_aborted_turn()
                    raise TurnFailure(
                        AvaError(
                            ErrorKind.parse,
                            "provider ended the turn after requesting a tool; check provider compatibility",
                        ),
                        TurnEndReason.provider_error,
                    )
                self.end_step(StepEndReason.completed)
                outcome = await self.boundary_after_completed_step()
                if outcome is not None:
                    return outcome
                if not self.steering:
                    self.release_gate()
                    return TurnOutcome.completed
                continue
            if not calls:
                self.end_step(StepEndReason.provider_error)
                if self.drive.abort_requested:
                    return await self.finish_aborted_turn()
                raise TurnFailure(
                    AvaError(ErrorKind.parse, "provider requested tool use without a tool call"),
                    TurnEndReason.provider_error,
                )
            outcome = await self.execute_tool_calls(assistant, calls)
            if outcome is not None:
                return outcome


def _is_alias(model: str) -> bool:
    from ava.llm import is_model_alias_candidate

    return is_model_alias_candidate(model)


async def run_turn(
    state: AgentState,
    turn_number: int,
    input: InboxMessage | None,
    steering: list[InboxMessage],
    cancel: CancelToken,
) -> TurnOutcome:
    """Run one turn. The caller holds the inbox gate; the first claim releases it."""
    turn = _Turn(
        state=state,
        turn_number=turn_number,
        cancel=cancel,
        input=input,
        steering=steering,
        holds_gate=True,
    )
    try:
        return await turn.run()
    finally:
        turn.release_gate()
