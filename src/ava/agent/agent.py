"""The ``Agent`` handle: the one seam between the headless layer and any frontend.

Frontends submit input with ``followup`` and ``steer``, control activity with ``cancel`` and
``resume``, observe through ``subscribe``, and run the driver with ``drive``.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

from ava.agent.compaction import CompactionOutcome, compact, select_compaction_end
from ava.agent.state import (
    AgentState,
    CancelCause,
    CompactionOptions,
    CompactNowOutcome,
    ModelChoices,
    Status,
    TurnOutcome,
    owed_step,
    validate_reopen,
)
from ava.agent.turn import TurnFailure, run_turn
from ava.base import AvaError, CancelToken, ErrorKind
from ava.llm import (
    AuthRequirement,
    Item,
    ModelCapabilities,
    Provider,
    Selection,
    provider_from_environment,
    resolve_model_alias,
    resolve_selection_model,
    sort_model_ids,
)
from ava.session import (
    AttemptTiming,
    DriveError,
    EventSink,
    InboxMessage,
    InboxSpliced,
    InboxTarget,
    SessionStart,
    Subscription,
    TurnEnd,
    TurnEndReason,
    TurnStart,
    Usage,
)
from ava.session.codec import validate_step_claimed_record
from ava.session.compaction import estimate_context_tokens
from ava.session.context_report import ContextReport, context_report
from ava.session.log import Log, OpenMode


class Agent:
    def __init__(
        self,
        provider: Provider,
        cwd: Path,
        options: CompactionOptions | None = None,
        log: Log | None = None,
    ) -> None:
        self._state = AgentState.create(provider, cwd, options or CompactionOptions(), log)

    # ---- construction ---------------------------------------------------------------------

    @classmethod
    def create(
        cls, provider: Provider, cwd: Path, options: CompactionOptions | None = None
    ) -> Agent:
        """Create and lock the default durable session before returning."""
        log = Log.create_default(cwd, provider.id, provider.selection.model)
        return cls(provider, cwd, options, log)

    @classmethod
    def create_at(
        cls,
        provider: Provider,
        cwd: Path,
        session_path: Path,
        options: CompactionOptions | None = None,
    ) -> Agent:
        log = Log.create_at(session_path, cwd, provider.id, provider.selection.model)
        return cls(provider, cwd, options, log)

    @classmethod
    def reopen(
        cls,
        provider: Provider,
        cwd: Path,
        log_or_path: Log | Path,
        options: CompactionOptions | None = None,
    ) -> Agent:
        """Reopen a durable session. Restores ``paused`` only when the newest turn ended with user_pause."""
        log = (
            Log.open(log_or_path, OpenMode.repair, cwd)
            if isinstance(log_or_path, Path)
            else log_or_path
        )
        if not log.ready_for_resume:
            raise AvaError(
                ErrorKind.invalid_argument,
                "session log is not ready for agent resume",
                "open it in repairing mode with the expected working directory",
            )
        paused = validate_reopen(log.loaded_events, cwd)
        agent = cls(provider, cwd, options, log)
        if paused:
            agent._state.drive_state.restore_paused()
        return agent

    async def aclose(self) -> None:
        """Close the provider and durable state. The agent must be idle."""
        state = self._state
        if state.drive_state.status not in (Status.idle, Status.paused):
            raise AvaError(ErrorKind.invalid_argument, "cannot close the agent while it is running")
        try:
            await state.provider.aclose()
        finally:
            state.close()

    async def __aenter__(self) -> Agent:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    # ---- observation ----------------------------------------------------------------------

    @property
    def status(self) -> Status:
        return self._state.drive_state.status

    @property
    def turn_open(self) -> bool:
        return self._state.drive_state.turn_open

    @property
    def cwd(self) -> Path:
        return self._state.cwd

    @property
    def session_path(self) -> Path | None:
        return self._state.writer.log.path if self._state.writer is not None else None

    @property
    def session_id(self) -> str | None:
        if not self._state.session.events:
            return None
        header = self._state.session.events[0].payload
        return header.id if isinstance(header, SessionStart) else None

    @property
    def provider_id(self) -> str:
        return self._state.provider.selection.provider

    @property
    def state(self) -> AgentState:
        """The runtime state, for tests and same-package tooling."""
        return self._state

    def subscribe(self, sink: EventSink) -> Subscription:
        return self._state.session.subscribe(sink)

    def prepare(self) -> None:
        self._state.initialize()

    # ---- input ---------------------------------------------------------------------------

    async def _enqueue(self, target: InboxTarget, item: Item) -> None:
        validate_step_claimed_record(target, item)
        state = self._state
        await state.inbox_gate.acquire()
        try:
            state.initialize()
            inbox = state.session.inbox()
            message_id = f"m-{state.next_message}"
            messages = inbox.target(target)
            if target == InboxTarget.next_step:
                # The prospective complete batch must fit one record with worst-case ordinals.
                from ava.session import StepClaimed
                from ava.session.codec import encode_record
                from ava.session.event import Event

                maximum = 2**64 - 1
                from ava.session.codec import _MAX_TIME

                encode_record(
                    Event(
                        seq=maximum,
                        at=_MAX_TIME,
                        payload=StepClaimed(
                            turn=maximum,
                            step=maximum,
                            target=InboxTarget.next_step,
                            claimed=[*messages, InboxMessage(id=message_id, item=item)],
                        ),
                    )
                )
            state.next_message += 1
            state.acknowledge(
                InboxSpliced(
                    target=target,
                    index=len(messages),
                    removed=0,
                    inserted=[InboxMessage(id=message_id, item=item)],
                )
            )
        finally:
            state.inbox_gate.release()

    async def followup(self, item: Item) -> None:
        """Returns only after the durable next-turn inbox record is complete."""
        await self._enqueue(InboxTarget.next_turn, item)

    async def steer(self, item: Item) -> None:
        """Returns only after the durable next-step inbox record is complete."""
        await self._enqueue(InboxTarget.next_step, item)

    # ---- control -------------------------------------------------------------------------

    def cancel(self, cause: CancelCause) -> None:
        if cause == CancelCause.user_pause:
            self._state.drive_state.request_pause()
        elif self._state.drive_state.request_abort():
            self._state.activity.cancel()

    def resume(self) -> None:
        self._state.drive_state.request_resume()

    def watch_status(self, listener: Callable[[Status, bool], None]) -> Callable[[], None]:
        """Observe transient control state (status, turn_open); returns an unsubscribe function."""
        return self._state.drive_state.watch(listener)

    # ---- model selection -----------------------------------------------------------------

    async def model_choices(self) -> ModelChoices:
        state = self._state
        state.model_catalog_operations += 1
        try:
            try:
                listed = await state.provider.list_models()
                available = True
            except AvaError:
                listed = []
                available = False
        finally:
            state.model_catalog_operations -= 1
        current = self.current_selection().model
        models = [*listed, current, *state.provider.model_aliases.values()]
        sort_model_ids(models)
        deduplicated = list(dict.fromkeys(models))
        return ModelChoices(
            models=deduplicated, current=current, provider_catalog_available=available
        )

    def select_model(self, model: str) -> None:
        if not model:
            raise AvaError(ErrorKind.invalid_argument, "model id must not be empty")
        current = self.current_selection()
        self._state.pending_selection = Selection(current.provider, model, None)
        self._state.model_revision += 1

    def current_selection(self) -> Selection:
        pending = self._state.pending_selection
        selected = pending or self._state.provider.selection
        return Selection(selected.provider, selected.model, selected.effort)

    def current_capabilities(self) -> ModelCapabilities:
        """Capabilities for the pending or active model selection."""
        return self._state.provider.capabilities(self.current_selection().model)

    def context_report(self, *, prepare: bool = False) -> ContextReport:
        """Describe the provider-neutral context without exposing runtime state."""
        if prepare:
            self.prepare()
        state = self._state
        return context_report(
            state.session,
            state.provider.context_window,
            state.compaction_options.threshold_percent,
        )

    def status_snapshot(self) -> dict[str, object]:
        """Return the stable frontend status contract from agent-owned state."""
        state = self._state
        selected = self.current_selection()
        report = self.context_report()
        used_tokens = (
            report.estimated_tokens
            if report.measured_input_tokens is None
            else estimate_context_tokens(state.session)
        )
        capabilities = state.provider.capabilities(selected.model)
        context_window = capabilities.context_window_tokens
        if context_window is None and selected.model == state.provider.selection.model:
            context_window = state.provider.context_window
        context_window = context_window or 0
        remaining = (
            round(max(0, context_window - used_tokens) * 100 / context_window)
            if context_window
            else None
        )

        input_tokens: int | None = None
        output_tokens: int | None = None
        cached_read = 0
        cache_reported = False
        ttft_ms: int | None = None
        for event in state.session.events:
            match event.payload:
                case Usage(
                    input=input,
                    cached_read=cached_read_tokens,
                    cache_write=cache_write,
                    output=output,
                ):
                    input_parts = (input, cached_read_tokens, cache_write)
                    if any(value is not None for value in input_parts):
                        input_tokens = (input_tokens or 0) + sum(value or 0 for value in input_parts)
                    if output is not None:
                        output_tokens = (output_tokens or 0) + output
                    if cached_read_tokens is not None:
                        cached_read += cached_read_tokens
                        cache_reported = True
                case AttemptTiming(ttft_ms=latest_ttft):
                    # The newest attempt describes the latency the user most recently felt.
                    ttft_ms = latest_ttft

        cache_hit_percent = (
            cached_read * 100 // input_tokens if cache_reported and input_tokens else None
        )
        return {
            "status": self.status.value,
            "turn_open": self.turn_open,
            "cwd": str(self.cwd),
            "provider": selected.provider,
            "model": selected.model,
            "effort": selected.effort,
            "context_used_tokens": used_tokens,
            "context_window_tokens": context_window,
            "context_remaining_percent": remaining,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_hit_percent": cache_hit_percent,
            "ttft_ms": ttft_ms,
        }

    async def cycle_effort(self) -> str:
        state = self._state
        revision = state.model_revision
        selected = self.current_selection()
        capabilities: ModelCapabilities = state.provider.capabilities(selected.model)
        if capabilities.effort_values is None:
            state.model_catalog_operations += 1
            try:
                models = await state.provider.list_models()
            finally:
                state.model_catalog_operations -= 1
            if revision != state.model_revision:
                raise AvaError(
                    ErrorKind.cancelled,
                    "reasoning effort change was superseded by a newer model selection",
                )
            selected = self.current_selection()
            concrete = resolve_model_alias(selected.model, models, state.provider.model_aliases)
            capabilities = state.provider.capabilities(concrete)
        if not capabilities.effort_values:
            raise AvaError(
                ErrorKind.invalid_argument, "the current model does not support reasoning effort"
            )
        efforts = capabilities.effort_values
        try:
            index = efforts.index(selected.effort) if selected.effort is not None else -1
        except ValueError:
            index = -1
        next_effort = efforts[0] if index < 0 or index + 1 >= len(efforts) else efforts[index + 1]
        state.pending_selection = Selection(selected.provider, selected.model, next_effort)
        return next_effort

    def select_effort(self, effort: str | None) -> None:
        """Set (or clear) the reasoning effort applied at the next step boundary."""
        if effort is not None:
            if not effort:
                raise AvaError(ErrorKind.invalid_argument, "effort must not be empty")
            capabilities = self._state.provider.capabilities(self.current_selection().model)
            if capabilities.effort_values is not None and effort not in capabilities.effort_values:
                raise AvaError(
                    ErrorKind.invalid_argument,
                    f"the current model does not advertise reasoning effort '{effort}'; "
                    f"choose one of {', '.join(capabilities.effort_values) or 'none'}",
                )
        selected = self.current_selection()
        self._state.pending_selection = Selection(selected.provider, selected.model, effort)

    async def reload_credentials(
        self, auth_requirement: AuthRequirement = AuthRequirement.required
    ) -> None:
        state = self._state
        if (
            state.drive_state.status != Status.idle
            or state.maintenance_running
            or state.model_catalog_operations
        ):
            raise AvaError(ErrorKind.invalid_argument, "cannot reload credentials while busy")
        old_provider = state.provider
        new_provider = provider_from_environment(None, old_provider.selection, auth_requirement)
        state.provider = new_provider
        if new_provider is not old_provider:
            await old_provider.aclose()

    # ---- the driver ----------------------------------------------------------------------

    async def drive(self) -> None:
        """Run turns until the inbox is empty. Exactly one driver at a time."""
        state = self._state
        drive = state.drive_state
        if state.maintenance_running:
            raise AvaError(
                ErrorKind.invalid_argument,
                "cannot start the driver without pending input or while it is already running",
            )
        gate = state.inbox_gate
        owns = False
        continuation_turn = False
        try:
            while True:
                await gate.acquire()
                holding = True
                try:
                    state.initialize()
                    inbox = state.session.inbox()
                    has_pending = bool(inbox.next_turn or inbox.next_step)
                    if not owns:
                        resuming = drive.resume_requested
                        continuation_turn = resuming and (
                            owed_step(state.session) or bool(inbox.next_step)
                        )
                        if state.maintenance_running or not drive.begin(has_pending):
                            raise AvaError(
                                ErrorKind.invalid_argument,
                                "cannot start the driver without pending input or while it is already running",
                            )
                        owns = True
                        state.activity = CancelToken()
                        gate.release()
                        holding = False
                        await resolve_selection_model(state.provider, state.activity)
                        continue
                    if not drive.turn_open and drive.pause_requested:
                        if not drive.finish_pause():
                            raise AvaError(ErrorKind.internal, "cannot enter the paused state")
                        owns = False
                        return
                    if not drive.turn_open and drive.abort_requested:
                        if not (has_pending or continuation_turn):
                            if not drive.finish_abort():
                                raise AvaError(
                                    ErrorKind.internal, "cannot release the aborted driver"
                                )
                            owns = False
                            return
                        # Queued work is cleared by a zero-step turn closed with user_abort.
                        if not drive.open_turn():
                            raise AvaError(ErrorKind.internal, "cannot open the aborted turn")
                        turn_number = state.next_turn
                        state.next_turn += 1
                        state.append(TurnStart(turn=turn_number))
                        if not drive.finish_abort():
                            raise AvaError(ErrorKind.internal, "cannot finish the aborted turn")
                        state.acknowledge(
                            TurnEnd(turn=turn_number, reason=TurnEndReason.user_abort)
                        )
                        state.sync()
                        owns = False
                        return
                    if not has_pending and not continuation_turn:
                        if not drive.finish():
                            raise AvaError(ErrorKind.internal, "cannot release the idle driver")
                        owns = False
                        return
                    if not drive.open_turn():
                        raise AvaError(ErrorKind.internal, "cannot open the next durable turn")
                    turn_started = time.monotonic()
                    input = (
                        inbox.next_turn[0] if (not continuation_turn and inbox.next_turn) else None
                    )
                    steering = list(inbox.next_step)
                    continuation_turn = False
                    turn_number = state.next_turn
                    state.next_turn += 1
                    state.append(TurnStart(turn=turn_number))
                    holding = False  # run_turn owns the gate from here and releases it at the first claim.
                    try:
                        outcome = await run_turn(
                            state, turn_number, input, steering, state.activity
                        )
                    except TurnFailure as failure:
                        await self._finish_failed_turn(turn_number, failure.reason, failure.error)
                        owns = False
                        raise failure.error from None
                    except AvaError as error:
                        await self._finish_failed_turn(
                            turn_number, TurnEndReason.provider_error, error
                        )
                        owns = False
                        raise
                    if outcome in (TurnOutcome.paused, TurnOutcome.aborted):
                        state.sync()
                        owns = False
                        return
                    if not drive.close_turn():
                        close_error = AvaError(
                            ErrorKind.internal,
                            "cannot close a turn while tool results remain outstanding",
                        )
                        await self._finish_failed_turn(
                            turn_number, TurnEndReason.completed, close_error
                        )
                        owns = False
                        raise close_error
                    elapsed_ms = int((time.monotonic() - turn_started) * 1000)
                    state.append(
                        TurnEnd(
                            turn=turn_number, reason=TurnEndReason.completed, elapsed_ms=elapsed_ms
                        )
                    )
                    state.sync()
                finally:
                    if holding:
                        gate.release()
        finally:
            if owns:
                drive.reset_after_error()

    async def _finish_failed_turn(
        self, turn_number: int, reason: TurnEndReason, error: AvaError
    ) -> None:
        """Contain a failed turn at the drive boundary: reset, then report."""
        state = self._state
        await state.inbox_gate.acquire()
        try:
            state.drive_state.reset_after_error()
            state.append(TurnEnd(turn=turn_number, reason=reason))
            state.append(
                DriveError(
                    turn=turn_number,
                    error_kind=error.kind,
                    message=error.message,
                    detail=error.detail,
                    recoverable=error.recoverable,
                )
            )
            state.sync()
        finally:
            state.inbox_gate.release()

    # ---- maintenance ---------------------------------------------------------------------

    async def compact_now(self) -> CompactNowOutcome:
        """Manual compaction claims the idle seam so it cannot race a model request."""
        state = self._state
        if state.drive_state.status != Status.idle or state.maintenance_running:
            raise AvaError(
                ErrorKind.invalid_argument,
                "cannot compact while the agent is busy",
                recoverable=True,
            )
        if not state.compaction_options.enabled:
            return CompactNowOutcome.disabled
        state.maintenance_running = True
        await state.inbox_gate.acquire()
        try:
            state.initialize()
            covered_end = select_compaction_end(state)
            if covered_end is None:
                return CompactNowOutcome.nothing_to_compact
            maintenance = state.next_maintenance
            state.next_maintenance += 1
            prefix = f"compact-{state.next_turn}-maintenance-{maintenance}"
            outcome = await compact(
                state, state.next_turn, prefix, covered_end, None, CancelToken()
            )
            return {
                CompactionOutcome.compacted: CompactNowOutcome.compacted,
                CompactionOutcome.skipped: CompactNowOutcome.nothing_to_compact,
                CompactionOutcome.failed: CompactNowOutcome.failed,
            }[outcome]
        finally:
            state.maintenance_running = False
            state.inbox_gate.release()
