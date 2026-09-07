"""Shell execution with bounded display output and separately capped overflow logs.

Display truncation does not stop the command or override its actual exit status.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import BinaryIO

from ava.base import AvaError, CancelToken, ErrorKind
from ava.llm.types import ToolDef, ToolParam, ToolParamType
from ava.proc import run as run_process
from ava.tool.api import Output, Tool, error_output, optional_int, parse_arguments

BASH_MAX_OUTPUT_BYTES = 50 * 1024
BASH_MAX_OUTPUT_LINES = 2000
BASH_NOTICE_RESERVE = 1024
BASH_BODY_BYTES = BASH_MAX_OUTPUT_BYTES - BASH_NOTICE_RESERVE
BASH_BODY_LINES = BASH_MAX_OUTPUT_LINES - 4
BASH_MAX_LOG_BYTES = 64 * 1024 * 1024
DEFAULT_BASH_TIMEOUT_SECONDS = 120
MAX_BASH_TIMEOUT_SECONDS = 3600

BASH_PARAMS = [
    ToolParam(
        "command",
        "Shell command to run. Each call starts in the invocation directory; cd does not persist across calls.",
        ToolParamType.string,
        True,
    ),
    ToolParam(
        "timeout_seconds",
        "Timeout from 1 to 3600 seconds. Defaults to 120 seconds.",
        ToolParamType.integer,
        minimum=1,
    ),
]


class BashOutput:
    """Bounded display tail; overflow is archived without stopping the command."""

    def __init__(self) -> None:
        self._tail = b""
        self._log: BinaryIO | None = None
        self._log_bytes = 0
        self.log_path: Path | None = None
        self.log_truncated = False
        self.error: OSError | None = None

    def append(self, chunk: str) -> bool:
        data = chunk.encode("utf-8")
        combined = self._tail + data
        tail = combined[-BASH_BODY_BYTES:]
        lines = tail.count(b"\n") + int(bool(tail) and not tail.endswith(b"\n"))
        if lines > BASH_BODY_LINES:
            tail = tail.split(b"\n", lines - BASH_BODY_LINES)[-1]
        try:
            if self._log is not None:
                self._write_log(data)
            elif len(tail) < len(combined):
                descriptor, name = tempfile.mkstemp(prefix="ava-bash-", suffix=".log")
                self.log_path = Path(name)
                self._log = os.fdopen(descriptor, "wb")
                self._write_log(combined)
        except OSError as error:
            self.error = error
            # Use the runner's normal termination/drain path on storage failure.
            return False
        self._tail = tail
        # Presentation limits must not interrupt a command's side effects.
        return True

    def _write_log(self, data: bytes) -> None:
        assert self._log is not None
        retained = data[: max(0, BASH_MAX_LOG_BYTES - self._log_bytes)]
        self._log.write(retained)
        self._log_bytes += len(retained)
        self.log_truncated |= len(retained) < len(data)

    def close(self) -> None:
        if self._log is not None:
            try:
                self._log.close()
            except OSError as error:
                self.error = error

    def render(self) -> str:
        # Byte truncation may start inside a UTF-8 character; omit that fragment.
        text = self._tail.decode("utf-8", "ignore")
        if self.log_path is not None:
            archive = (
                f"Log contains only the first {BASH_MAX_LOG_BYTES} bytes: {self.log_path}"
                if self.log_truncated
                else f"Full output: {self.log_path}"
            )
            text = _append_status(text, f"[Output truncated; showing trailing output. {archive}]")
        return text


def _append_status(text: str, status: str) -> str:
    if text:
        text += "\n" if text.endswith("\n") else "\n\n"
    return text + status


async def run_bash(cwd: Path, arguments_json: str, cancel: CancelToken) -> Output:
    if cancel.cancelled:
        raise AvaError(ErrorKind.cancelled, "command cancelled")
    arguments = parse_arguments(arguments_json)
    if isinstance(arguments, str):
        return error_output(
            f"invalid bash arguments: {arguments}. Use command plus optional integer timeout_seconds"
        )
    command = arguments.get("command")
    if not isinstance(command, str) or not command:
        return error_output("missing 'command' argument; call bash with the shell command to run")
    timeout, problem = optional_int(arguments, "timeout_seconds")
    if problem:
        return error_output(f"invalid bash arguments: {problem}")
    timeout = timeout if timeout is not None else DEFAULT_BASH_TIMEOUT_SECONDS
    if timeout < 1 or timeout > MAX_BASH_TIMEOUT_SECONDS:
        return error_output(
            "'timeout_seconds' must be from 1 to 3600; omit it to use the 120-second default"
        )

    captured = BashOutput()
    try:
        completion = await run_process(command, cwd, float(timeout), captured.append, cancel)
    finally:
        captured.close()
    if captured.error is not None:
        return error_output(
            f"cannot retain command output: {captured.error.strerror}. "
            "The command may have partially executed; inspect its effects before retrying."
        )
    text = captured.render()
    is_error = False
    if completion.timed_out:
        unit = "second" if timeout == 1 else "seconds"
        text = _append_status(
            text, f"[Command timed out after {timeout} {unit} and its process group was stopped.]"
        )
        is_error = True
    elif completion.signal is not None:
        text = _append_status(text, f"[Command stopped by signal {completion.signal}.]")
        is_error = True
    elif completion.exit_code is not None and completion.exit_code != 0:
        text = _append_status(text, f"[Command exited with code {completion.exit_code}.]")
        is_error = True
    elif completion.exit_code is None:
        raise AvaError(ErrorKind.internal, "command ended without an exit code or signal")
    if not text:
        text = "(no output)"
    return Output(text=text, is_error=is_error)


def make_bash_tool(cwd: Path) -> Tool:
    definition = ToolDef(
        name="bash",
        description=(
            "Run a shell command in the invocation directory and return combined stdout and stderr. "
            "The default timeout is 120 seconds. Return the trailing output within 50 KiB and "
            "2000 lines; truncation does not stop the command. Truncated output is saved to a "
            "temporary log (up to 64 MiB per command); the result reports its path and any log cap."
        ),
        params=list(BASH_PARAMS),
    )

    async def run(arguments_json: str, cancel: CancelToken) -> Output:
        return await run_bash(cwd, arguments_json, cancel)

    return Tool(definition=definition, run=run)
