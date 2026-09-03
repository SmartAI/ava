"""Compaction inside the loop: attempts, guards, and the seed or failure record."""

from __future__ import annotations

from enum import StrEnum

from ava.agent.state import AgentState, DriveState
from ava.agent.step import append_accounting, step
from ava.base import AvaError, CancelToken, ErrorKind
from ava.llm import Item, Role, StopReason, make_text_block
from ava.llm.types import ContentBlockKind
from ava.session import AttemptTiming, CompactionFailed, InboxSpliced, Selection, Usage
from ava.session import compaction as strategy

TAIL_BUDGET_PERCENT = 25
MAX_COMPACTION_ATTEMPTS = 4


class CompactionOutcome(StrEnum):
    skipped = "skipped"
    compacted = "compacted"
    failed = "failed"


def select_compaction_end(state: AgentState) -> int | None:
    tail_budget = state.provider.context_window * TAIL_BUDGET_PERCENT // 100
    return strategy.select_covered_end(state.session, tail_budget)


def _fail(state: AgentState, turn_number: int, kind: ErrorKind, reason: str) -> CompactionOutcome:
    state.append(
        CompactionFailed(turn=turn_number, error_kind=kind, message=f"compaction failed: {reason}")
    )
    state.drain()
    return CompactionOutcome.failed


def _suffix_is_compaction_local(state: AgentState, size_before: int, attempt_id: str) -> bool:
    """A summary computed against a conversation that changed in flight is rejected."""
    saw_usage = saw_timing = saw_selection = False
    for index in range(size_before, len(state.session)):
        payload = state.session.at(index).payload
        if isinstance(payload, InboxSpliced):
            continue
        if isinstance(payload, Selection) and not saw_selection and not saw_usage:
            saw_selection = True
            continue
        if isinstance(payload, Usage):
            if payload.attempt_id != attempt_id or saw_usage or saw_timing:
                return False
            saw_usage = True
            continue
        if isinstance(payload, AttemptTiming):
            if payload.attempt_id != attempt_id or not saw_usage or saw_timing:
                return False
            saw_timing = True
            continue
        return False
    return saw_usage and saw_timing


async def compact(
    state: AgentState,
    turn_number: int,
    attempt_prefix: str,
    covered_end: int | None,
    drive_state: DriveState | None,
    cancel: CancelToken,
) -> CompactionOutcome:
    if covered_end is None:
        return CompactionOutcome.skipped
    instruction = strategy.summarization_instruction()
    request = strategy.build_summarization_context(state.session, covered_end, instruction)
    for attempt in range(1, MAX_COMPACTION_ATTEMPTS + 1):
        if drive_state is not None and drive_state.abort_requested:
            raise AvaError(ErrorKind.cancelled, "provider request was cancelled")
        attempt_id = f"{attempt_prefix}-attempt-{attempt}"
        size_before = len(state.session)
        response = await step(state, request.context, attempt_id, False, cancel)
        append_accounting(
            state, attempt_id, response.usage, response.timing, append_empty_usage=True
        )
        state.drain()
        if drive_state is not None and drive_state.abort_requested:
            raise AvaError(ErrorKind.cancelled, "provider request was cancelled")
        if not _suffix_is_compaction_local(state, size_before, attempt_id):
            return _fail(
                state,
                turn_number,
                ErrorKind.internal,
                "the session changed while the summary was generated",
            )
        if response.error is not None:
            if response.error.kind == ErrorKind.cancelled:
                raise response.error
            return _fail(state, turn_number, response.error.kind, response.error.message)
        try:
            summary = strategy.validate_summary(response.assistant)
        except AvaError as rejected:
            call = next(
                (
                    block
                    for block in response.assistant.blocks
                    if block.kind == ContentBlockKind.tool_call
                ),
                None,
            )
            if attempt == MAX_COMPACTION_ATTEMPTS or call is None:
                return _fail(state, turn_number, rejected.kind, rejected.message)
            # Each retry appends an explicit correction so the attempts differ from one another.
            request.context.items.append(
                Item(
                    role=Role.user,
                    blocks=[make_text_block(strategy.make_rejection_correction(call.tool_name))],
                )
            )
            continue
        if response.stop_reason != StopReason.end_turn:
            return _fail(
                state, turn_number, ErrorKind.provider, "the summary response was incomplete"
            )
        seed = strategy.assemble_seed(state.session, covered_end, instruction, summary)
        if not strategy.seed_shrinks_window(state.session, seed):
            return _fail(
                state,
                turn_number,
                ErrorKind.provider,
                "the summary was not smaller than the context it replaced",
            )
        state.append(seed)
        state.drain()
        return CompactionOutcome.compacted
    raise AssertionError("unreachable")
