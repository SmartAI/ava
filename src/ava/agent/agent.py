"""The ``Agent`` handle: the one seam between the headless layer and any frontend.

Frontends submit input with ``followup`` and ``steer``, control activity with ``cancel`` and
``resume``, observe through ``subscribe``, and run the driver with ``drive``.
"""

from __future__ import annotations

import time
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
    DriveError,
    EventSink,
    InboxMessage,
    InboxSpliced,
    InboxTarget,
    Subscription,
    TurnEnd,
    TurnEndReason,
    TurnStart,
)
from ava.session.codec import validate_step_claimed_record
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

    def close(self) -> None:
        self._state.close()

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
        current = state.pending_model or state.provider.selection.model
        models = [*listed, current, *state.provider.model_aliases.values()]
        sort_model_ids(models)
        deduplicated = list(dict.fromkeys(models))
        return ModelChoices(
            models=deduplicated, current=current, provider_catalog_available=available
        )

    def select_model(self, model: str) -> None:
        if not model:
            raise AvaError(ErrorKind.invalid_argument, "model id must not be empty")
        self._state.pending_model = model
        self._state.pending_effort = None
        self._state.model_revision += 1

    def current_selection(self) -> Selection:
        provider = self._state.provider
        selection = Selection(
            provider.selection.provider, provider.selection.model, provider.selection.effort
        )
        if self._state.pending_model is not None:
            selection.model = self._state.pending_model
            selection.effort = None
        if self._state.pending_effort is not None:
            selection.effort = self._state.pending_effort
        return selection

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
        state.pending_effort = next_effort
        return next_effort

    def reload_credentials(
        self, auth_requirement: AuthRequirement = AuthRequirement.required
    ) -> None:
        state = self._state
        if (
            state.drive_state.status != Status.idle
            or state.maintenance_running
            or state.model_catalog_operations
        ):
            raise AvaError(ErrorKind.invalid_argument, "cannot reload credentials while busy")
        state.provider = provider_from_environment(None, state.provider.selection, auth_requirement)

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
                        error = AvaError(
                            ErrorKind.internal,
                            "cannot close a turn while tool results remain outstanding",
                        )
                        await self._finish_failed_turn(turn_number, TurnEndReason.completed, error)
                        owns = False
                        raise error
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
