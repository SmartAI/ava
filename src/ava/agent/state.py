"""Driver control state, the inbox mutation gate, and the shared agent runtime state."""

from __future__ import annotations

import asyncio
import re
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from ava.base import AvaError, CancelToken, ErrorKind
from ava.llm import Provider, Selection
from ava.session import (
    AssistantMessage,
    Event,
    EventPayload,
    InboxSpliced,
    PromptResolved,
    Session,
    SessionStart,
    SessionWriter,
    StepStart,
    ToolResult,
    ToolsAdvertised,
    TurnEnd,
    TurnEndReason,
    TurnStart,
    Usage,
)
from ava.session.log import Log, canonical_working_directory
from ava.tool import Tool, make_bash_tool, make_edit_tool, make_read_tool, make_write_tool


class Status(StrEnum):
    idle = "idle"
    running = "running"
    pausing = "pausing"
    aborting = "aborting"
    paused = "paused"


class CancelCause(StrEnum):
    user_pause = "user_pause"
    user_abort = "user_abort"


COMPACT_THRESHOLD_PERCENT = 85


@dataclass(slots=True)
class CompactionOptions:
    enabled: bool = True
    threshold_percent: int = COMPACT_THRESHOLD_PERCENT


class CompactNowOutcome(StrEnum):
    compacted = "compacted"
    nothing_to_compact = "nothing_to_compact"
    failed = "failed"
    disabled = "disabled"


class TurnOutcome(StrEnum):
    completed = "completed"
    paused = "paused"
    aborted = "aborted"


@dataclass(slots=True)
class ModelChoices:
    models: list[str]
    current: str
    provider_catalog_available: bool = False


StatusListener = Callable[[Status, bool], None]


class DriveState:
    """The phase machine. Public status tracks user-driven work only.

    Listeners observe every change of ``status`` or ``turn_open`` so a frontend can render the
    transient control state without polling; durable events remain the authority for history.
    """

    def __init__(self) -> None:
        self._status = Status.idle
        self._turn_open = False
        self.tool_results_owed = False
        self.resume_requested = False
        self._listeners: list[StatusListener] = []

    @property
    def status(self) -> Status:
        return self._status

    @status.setter
    def status(self, value: Status) -> None:
        if value != self._status:
            self._status = value
            self._notify()

    @property
    def turn_open(self) -> bool:
        return self._turn_open

    @turn_open.setter
    def turn_open(self, value: bool) -> None:
        if value != self._turn_open:
            self._turn_open = value
            self._notify()

    def watch(self, listener: StatusListener) -> Callable[[], None]:
        self._listeners.append(listener)

        def remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return remove

    def _notify(self) -> None:
        for listener in list(self._listeners):
            listener(self._status, self._turn_open)

    def begin(self, has_pending: bool) -> bool:
        if self.status != Status.idle or (not has_pending and not self.resume_requested):
            return False
        self.status = Status.running
        self.resume_requested = False
        return True

    def open_turn(self) -> bool:
        if self.status not in (Status.running, Status.aborting) or self.turn_open:
            return False
        self.turn_open = True
        return True

    def request_pause(self) -> None:
        if self.status == Status.running:
            self.status = Status.pausing

    def request_abort(self) -> bool:
        if self.status not in (Status.running, Status.pausing):
            return False
        self.status = Status.aborting
        return True

    def request_resume(self) -> None:
        if self.status == Status.paused:
            self.status = Status.idle
            self.resume_requested = True

    def restore_paused(self) -> None:
        self.status = Status.paused

    def reset_after_error(self) -> None:
        self.status = Status.idle
        self.turn_open = False
        self.tool_results_owed = False
        self.resume_requested = False

    def close_turn(self) -> bool:
        if (
            self.status not in (Status.running, Status.pausing)
            or not self.turn_open
            or self.tool_results_owed
        ):
            return False
        self.turn_open = False
        return True

    def finish(self) -> bool:
        if self.status != Status.running or self.turn_open or self.tool_results_owed:
            return False
        self.status = Status.idle
        return True

    def finish_pause(self) -> bool:
        if self.status != Status.pausing or self.turn_open or self.tool_results_owed:
            return False
        self.status = Status.paused
        return True

    def finish_abort(self) -> bool:
        if self.status != Status.aborting or self.tool_results_owed:
            return False
        self.status = Status.idle
        self.turn_open = False
        return True

    @property
    def pause_requested(self) -> bool:
        return self.status == Status.pausing

    @property
    def abort_requested(self) -> bool:
        return self.status == Status.aborting


def owed_step(session: Session) -> bool:
    """A step is owed when the newest turn closed with user_pause and its last model-visible
    record is a tool result rather than an assistant message."""
    events = session.events
    paused_turn: int | None = None
    index = len(events)
    while index > 0:
        index -= 1
        payload = events[index].payload
        if isinstance(payload, TurnEnd):
            if payload.reason != TurnEndReason.user_pause:
                return False
            paused_turn = payload.turn
            break
    if paused_turn is None:
        return False
    while index > 0:
        index -= 1
        payload = events[index].payload
        if isinstance(payload, ToolResult):
            return True
        if isinstance(payload, AssistantMessage):
            return False
        if isinstance(payload, TurnStart) and payload.turn == paused_turn:
            return False
    return False


class InboxGate:
    """One private mutation gate for acknowledgements, claims, and boundary decisions."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        await self._lock.acquire()

    def release(self) -> None:
        if self._lock.locked():
            self._lock.release()

    @property
    def held(self) -> bool:
        return self._lock.locked()


# ---- Ordinal recovery -----------------------------------------------------------------------

_MESSAGE_ID = re.compile(r"^m-([1-9][0-9]*)$")
_MAINTENANCE_ID = re.compile(r"^compact-[1-9][0-9]*-maintenance-([1-9][0-9]*)-attempt-[1-9][0-9]*$")


@dataclass(slots=True)
class RecoveredOrdinals:
    next_message: int = 1
    next_turn: int = 1
    next_maintenance: int = 1


def recover_ordinals(session: Session) -> RecoveredOrdinals:
    result = RecoveredOrdinals()
    for event in session.events:
        payload = event.payload
        if isinstance(payload, InboxSpliced):
            for message in payload.inserted:
                match = _MESSAGE_ID.match(message.id)
                if match:
                    result.next_message = max(result.next_message, int(match.group(1)) + 1)
        elif isinstance(payload, TurnStart | StepStart):
            result.next_turn = max(result.next_turn, payload.turn + 1)
        elif isinstance(payload, Usage):
            match = _MAINTENANCE_ID.match(payload.attempt_id)
            if match:
                result.next_maintenance = max(result.next_maintenance, int(match.group(1)) + 1)
    return result


def validate_reopen(events: list[Event], cwd: Path) -> bool:
    """Verify the header directory; return whether the agent must reopen paused."""
    if not events:
        raise AvaError(ErrorKind.parse, "cannot resume a session without a header")
    header = events[0].payload
    canonical = canonical_working_directory(cwd)
    if not isinstance(header, SessionStart) or header.cwd != str(canonical):
        raise AvaError(
            ErrorKind.invalid_argument,
            "session belongs to a different working directory",
            "session/start is missing" if not isinstance(header, SessionStart) else header.cwd,
        )
    newest: TurnEndReason | None = None
    for event in events:
        if isinstance(event.payload, TurnEnd):
            newest = event.payload.reason
    return newest == TurnEndReason.user_pause


# ---- Shared runtime state --------------------------------------------------------------------


def create_scratchpad() -> Path | None:
    try:
        return Path(tempfile.mkdtemp(prefix="ava-scratch-"))
    except OSError:
        return None


@dataclass(slots=True)
class AgentState:
    provider: Provider
    session: Session
    writer: SessionWriter | None
    cwd: Path
    compaction_options: CompactionOptions
    tools: list[Tool] = field(default_factory=list)
    startup: list[EventPayload] = field(default_factory=list)
    drive_state: DriveState = field(default_factory=DriveState)
    activity: CancelToken = field(default_factory=CancelToken)
    consumed_selection: Selection | None = None
    scratchpad: Path | None = None
    inbox_gate: InboxGate = field(default_factory=InboxGate)
    next_message: int = 1
    next_turn: int = 1
    next_maintenance: int = 1
    model_revision: int = 0
    model_catalog_operations: int = 0
    maintenance_running: bool = False
    ordinals_recovered: bool = False
    initialized: bool = False
    pending_model: str | None = None
    pending_effort: str | None = None

    @classmethod
    def create(
        cls, provider: Provider, cwd: Path, options: CompactionOptions, log: Log | None
    ) -> AgentState:
        from ava.agent.prompt import make_system_prompt

        if log is not None:
            session = Session(log.take_loaded_events())
            writer: SessionWriter | None = SessionWriter(session, log)
        else:
            session = Session()
            writer = None
        state = cls(
            provider=provider,
            session=session,
            writer=writer,
            cwd=cwd,
            compaction_options=options,
            scratchpad=create_scratchpad(),
        )
        newest_prompt: PromptResolved | None = None
        newest_tools: ToolsAdvertised | None = None
        for event in session.events:
            if isinstance(event.payload, PromptResolved):
                newest_prompt = event.payload
            elif isinstance(event.payload, ToolsAdvertised):
                newest_tools = event.payload
        prompt = make_system_prompt(cwd, state.scratchpad)
        if newest_prompt is None or newest_prompt.system_prompt != prompt:
            state.startup.append(PromptResolved(system_prompt=prompt))
        state.tools = [
            make_read_tool(cwd),
            make_write_tool(cwd),
            make_edit_tool(cwd),
            make_bash_tool(cwd),
        ]
        definitions = [tool.definition for tool in state.tools]
        if newest_tools is None or newest_tools.tools != definitions:
            state.startup.append(ToolsAdvertised(tools=definitions))
        return state

    def append(self, payload: EventPayload) -> None:
        if self.writer is not None:
            self.writer.enqueue(payload)
        else:
            self.session.append(payload)

    def acknowledge(self, payload: EventPayload) -> None:
        if self.writer is not None:
            self.writer.acknowledge(payload)
        else:
            self.session.append(payload)

    def drain(self) -> None:
        if self.writer is not None:
            self.writer.drain()

    def sync(self) -> None:
        if self.writer is not None:
            self.writer.sync()

    def initialize(self) -> None:
        if not self.ordinals_recovered:
            self.session.inbox()
            recovered = recover_ordinals(self.session)
            self.next_message = recovered.next_message
            self.next_turn = recovered.next_turn
            self.next_maintenance = recovered.next_maintenance
            self.ordinals_recovered = True
        if self.initialized:
            return
        for payload in self.startup:
            self.append(payload)
        self.startup.clear()
        self.initialized = True
        self.drain()

    def find_tool(self, name: str) -> Tool | None:
        return next((tool for tool in self.tools if tool.name == name), None)

    def close(self) -> None:
        if self.scratchpad is not None:
            shutil.rmtree(self.scratchpad, ignore_errors=True)
            self.scratchpad = None
        if self.writer is not None:
            self.writer.close()
