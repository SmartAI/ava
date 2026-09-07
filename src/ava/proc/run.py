"""Run a shell command in its own process group with a pinned environment.

Stdout and stderr share one pipe so their observed order is preserved. A sink returning False requests
process-group termination. After process exit, inherited output handles receive a resettable
100 ms idle grace; closing output alone never shortens the command deadline. Timeout and cancellation escalate from
SIGTERM to SIGKILL after a short grace period.
"""

from __future__ import annotations

import asyncio
import os
import signal
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ava.base import AvaError, CancelToken, ErrorKind
from ava.base.cancel import NEVER

TERMINATE_GRACE_SECONDS = 0.25
KILL_GRACE_SECONDS = 1.0
EXIT_STDIO_GRACE_SECONDS = 0.1

ENVIRONMENT_OVERRIDES: dict[str, str] = {
    "LC_ALL": "C",
    "LANG": "C",
    "TERM": "dumb",
    "NO_COLOR": "1",
    "CLICOLOR": "0",
    "CLICOLOR_FORCE": "0",
    "PAGER": "cat",
    "GIT_PAGER": "cat",
    "GH_PAGER": "cat",
    "RIPGREP_CONFIG_PATH": "/dev/null",
    "BASH_ENV": "/dev/null",
}

OutputSink = Callable[[str], bool]


@dataclass(slots=True)
class Completion:
    exit_code: int | None = None
    signal: int | None = None
    timed_out: bool = False


def pinned_environment() -> dict[str, str]:
    environment = {
        key: value for key, value in os.environ.items() if key not in ENVIRONMENT_OVERRIDES
    }
    environment.update(ENVIRONMENT_OVERRIDES)
    return environment


def _signal_group(pid: int, signum: int) -> None:
    try:
        os.killpg(pid, signum)
    except ProcessLookupError:
        pass


class _CommandProtocol(asyncio.SubprocessProtocol):
    """Observe process exit independently of inherited output handles."""

    def __init__(self, output_sink: OutputSink) -> None:
        self.loop = asyncio.get_running_loop()
        self.exited: asyncio.Future[None] = self.loop.create_future()
        self.finished: asyncio.Future[None] = self.loop.create_future()
        self.closed: asyncio.Future[None] = self.loop.create_future()
        self.stop: asyncio.Future[None] = self.loop.create_future()
        self.output_sink = output_sink
        self.decoder = _incremental_decoder()
        self.pipe_closed = False
        self.idle_timer: asyncio.TimerHandle | None = None
        self.error: Exception | None = None

    def finish(self) -> None:
        if self.idle_timer is not None:
            self.idle_timer.cancel()
        if not self.finished.done():
            self.finished.set_result(None)

    def arm_idle_timer(self) -> None:
        if self.idle_timer is not None:
            self.idle_timer.cancel()
        self.idle_timer = self.loop.call_later(EXIT_STDIO_GRACE_SECONDS, self.finish)

    def pipe_data_received(self, fd: int, data: bytes) -> None:
        if self.finished.done() or self.stop.done():
            return
        try:
            if not self.output_sink(self.decoder.decode(data)):
                self.stop.set_result(None)
        except Exception as error:
            self.error = error
            self.stop.set_result(None)
        if self.exited.done():
            self.arm_idle_timer()

    def pipe_connection_lost(self, fd: int, exc: Exception | None) -> None:
        self.pipe_closed = True
        if exc is not None:
            self.error = exc
            if not self.stop.done():
                self.stop.set_result(None)
        if self.exited.done():
            self.finish()

    def process_exited(self) -> None:
        self.exited.set_result(None)
        if self.pipe_closed:
            self.finish()
        else:
            self.arm_idle_timer()

    def connection_lost(self, exc: Exception | None) -> None:
        if not self.closed.done():
            self.closed.set_result(None)


async def run(
    command: str,
    cwd: Path,
    timeout_seconds: float,
    output_sink: OutputSink,
    cancel: CancelToken = NEVER,
) -> Completion:
    if cancel.cancelled:
        raise AvaError(ErrorKind.cancelled, "command cancelled")
    argv = (
        ["/bin/bash", "--noprofile", "--norc", "-c", command]
        if os.path.exists("/bin/bash")
        else ["/bin/sh", "-c", command]
    )
    loop = asyncio.get_running_loop()
    protocol = _CommandProtocol(output_sink)
    try:
        transport, _ = await loop.subprocess_exec(
            lambda: protocol,
            *argv,
            cwd=cwd,
            env=pinned_environment(),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as error:
        raise AvaError(ErrorKind.io, f"cannot start command: {error}") from error
    pid = transport.get_pid()
    completion = Completion()

    def on_cancel() -> None:
        if not protocol.stop.done():
            protocol.stop.set_result(None)

    remove = cancel.on_cancel(on_cancel)

    async def terminate() -> None:
        _signal_group(pid, signal.SIGTERM)
        # Allow the whole process group a grace period, even if its leader has exited.
        await asyncio.sleep(TERMINATE_GRACE_SECONDS)
        _signal_group(pid, signal.SIGKILL)
        await asyncio.wait_for(asyncio.shield(protocol.exited), KILL_GRACE_SECONDS)

    try:
        done, _ = await asyncio.wait(
            [protocol.finished, protocol.stop],
            timeout=timeout_seconds,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done or protocol.stop.done():
            completion.timed_out = not done
            if not protocol.stop.done():
                protocol.stop.set_result(None)
            await terminate()
        else:
            tail = protocol.decoder.decode(b"", final=True)
            if tail:
                output_sink(tail)
        if protocol.error is not None:
            raise protocol.error
    finally:
        remove()
        protocol.finish()
        if transport.get_returncode() is None:
            _signal_group(pid, signal.SIGKILL)
        transport.close()
        await asyncio.wait_for(asyncio.shield(protocol.closed), KILL_GRACE_SECONDS)
    if cancel.cancelled:
        raise AvaError(ErrorKind.cancelled, "command cancelled")
    status = transport.get_returncode()
    if status is None:
        raise AvaError(ErrorKind.internal, "command ended without an exit code or signal")
    if status < 0:
        completion.signal = -status
    else:
        completion.exit_code = status
    return completion


def _incremental_decoder():
    import codecs

    return codecs.getincrementaldecoder("utf-8")("replace")
