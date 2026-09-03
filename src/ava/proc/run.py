"""Run a shell command in its own process group with a pinned environment.

Stdout and stderr share one pipe so their observed order is preserved. The sink returns False when
its output budget is full, which stops the process group. Timeout and cancellation escalate from
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
READ_CHUNK_BYTES = 8192

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


async def run(
    command: str,
    cwd: Path,
    timeout_seconds: float,
    output_sink: OutputSink,
    cancel: CancelToken = NEVER,
) -> Completion:
    if cancel.cancelled:
        raise AvaError(ErrorKind.cancelled, "command cancelled")
    use_bash = os.path.exists("/bin/bash")
    if use_bash:
        argv = ["/bin/bash", "--noprofile", "--norc", "-c", command]
    else:
        argv = ["/bin/sh", "-c", command]
    try:
        child = await asyncio.create_subprocess_exec(
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
    assert child.stdout is not None
    pid = child.pid
    completion = Completion()
    cancelled = False
    stop_requested = False

    async def terminate(timed_out: bool) -> None:
        """SIGTERM the group, then SIGKILL after the grace period."""
        completion.timed_out = completion.timed_out or timed_out
        _signal_group(pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(child.wait(), TERMINATE_GRACE_SECONDS)
        except TimeoutError:
            _signal_group(pid, signal.SIGKILL)
            try:
                await asyncio.wait_for(child.wait(), KILL_GRACE_SECONDS)
            except TimeoutError:
                pass

    def on_cancel() -> None:
        nonlocal cancelled
        cancelled = True
        _signal_group(pid, signal.SIGTERM)

    remove = cancel.on_cancel(on_cancel)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    decoder = _incremental_decoder()
    try:
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                await terminate(True)
                break
            try:
                chunk = await asyncio.wait_for(child.stdout.read(READ_CHUNK_BYTES), remaining)
            except TimeoutError:
                await terminate(True)
                break
            if not chunk:
                break
            if not stop_requested and not output_sink(decoder.decode(chunk)):
                stop_requested = True
                await terminate(False)
                break
        # Drain whatever remains after a termination so the pipe closes and the child is reaped.
        while True:
            try:
                chunk = await asyncio.wait_for(
                    child.stdout.read(READ_CHUNK_BYTES), KILL_GRACE_SECONDS
                )
            except TimeoutError:
                _signal_group(pid, signal.SIGKILL)
                continue
            if not chunk:
                break
            if not stop_requested and not completion.timed_out and not cancelled:
                if not output_sink(decoder.decode(chunk)):
                    stop_requested = True
        tail = decoder.decode(b"", final=True)
        if tail and not stop_requested and not completion.timed_out and not cancelled:
            output_sink(tail)
        try:
            await asyncio.wait_for(child.wait(), KILL_GRACE_SECONDS)
        except TimeoutError:
            _signal_group(pid, signal.SIGKILL)
            await child.wait()
    finally:
        remove()
        if child.returncode is None:
            _signal_group(pid, signal.SIGKILL)
    if cancelled:
        raise AvaError(ErrorKind.cancelled, "command cancelled")
    status = child.returncode
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
