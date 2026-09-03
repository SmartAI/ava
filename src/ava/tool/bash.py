"""Foreground shell execution with a head-plus-tail output cap enforced in memory.

Three independent caps bound every command: total bytes, total lines, and bytes per line. The
producer is stopped when a cap is reached, and truncation is always reported.
"""

from __future__ import annotations

from collections import deque
from pathlib import Path

from ava.base import AvaError, CancelToken, ErrorKind
from ava.llm.types import ToolDef, ToolParam, ToolParamType
from ava.proc import run as run_process
from ava.tool.api import Output, Tool, error_output, optional_int, parse_arguments

BASH_MAX_OUTPUT_BYTES = 50 * 1024
BASH_MAX_OUTPUT_LINES = 2000
MAX_BASH_LINE_BYTES = 500
BASH_NOTICE_RESERVE = 1024
BASH_BODY_BYTES = BASH_MAX_OUTPUT_BYTES - BASH_NOTICE_RESERVE
BASH_BODY_LINES = BASH_MAX_OUTPUT_LINES - 4
BASH_HEAD_BYTES = BASH_BODY_BYTES // 8
BASH_HEAD_LINES = BASH_BODY_LINES // 8
BASH_TAIL_BYTES = BASH_BODY_BYTES - BASH_HEAD_BYTES
BASH_TAIL_LINES = BASH_BODY_LINES - BASH_HEAD_LINES
DEFAULT_BASH_TIMEOUT_SECONDS = 120
MAX_BASH_TIMEOUT_SECONDS = 3600
LINE_TRUNCATION_SUFFIX = "... [line exceeded 500 bytes]"
TRUNCATION_NOTICE = (
    "[Output truncated and the command was stopped after reaching the model-output budget of "
    "50 KiB, 2000 lines, or 500 bytes per line. Narrow the command output and run it again.]\n"
)

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
    """A fixed head buffer and a fixed tail ring; everything between is counted and discarded."""

    def __init__(self) -> None:
        self._head: list[str] = []
        self._tail: deque[str] = deque()
        self._current: list[str] = []
        self._head_bytes = 0
        self._tail_bytes = 0
        self._total_bytes = 0
        self._total_lines = 0
        self._current_bytes = 0
        self._head_complete = False
        self._has_partial_line = False
        self._line_truncated = False
        self.limit_hit = False

    def append(self, chunk: str) -> bool:
        self._total_bytes += len(chunk.encode("utf-8"))
        if self._total_bytes > BASH_BODY_BYTES:
            self.limit_hit = True
        for character in chunk:
            if character == "\n":
                self._finish_line(True)
                continue
            self._has_partial_line = True
            if self._total_lines >= BASH_BODY_LINES:
                self.limit_hit = True
            self._current_bytes += len(character.encode("utf-8"))
            if len("".join(self._current).encode("utf-8")) < MAX_BASH_LINE_BYTES:
                self._current.append(character)
            if self._current_bytes > MAX_BASH_LINE_BYTES:
                self._line_truncated = True
                self.limit_hit = True
        return not self.limit_hit

    def render(self) -> str:
        if self._has_partial_line:
            self._finish_line(False)
        result = "".join(self._head)
        if self.limit_hit:
            if result and not result.endswith("\n"):
                result += "\n"
            result += TRUNCATION_NOTICE
        result += "".join(self._tail)
        return result

    def _finish_line(self, had_newline: bool) -> None:
        self._total_lines += 1
        if self._total_lines > BASH_BODY_LINES:
            self.limit_hit = True
        line = "".join(self._current)
        if self._line_truncated:
            prefix_bytes = MAX_BASH_LINE_BYTES - len(LINE_TRUNCATION_SUFFIX)
            line = (
                line.encode("utf-8")[:prefix_bytes].decode("utf-8", "ignore")
                + LINE_TRUNCATION_SUFFIX
            )
        if had_newline:
            line += "\n"
        self._retain_line(line)
        self._current = []
        self._current_bytes = 0
        self._has_partial_line = False
        self._line_truncated = False

    def _retain_line(self, line: str) -> None:
        size = len(line.encode("utf-8"))
        if (
            not self._head_complete
            and len(self._head) < BASH_HEAD_LINES
            and self._head_bytes + size <= BASH_HEAD_BYTES
        ):
            self._head_bytes += size
            self._head.append(line)
            return
        self._head_complete = True
        self._tail_bytes += size
        self._tail.append(line)
        while len(self._tail) > BASH_TAIL_LINES or self._tail_bytes > BASH_TAIL_BYTES:
            self._tail_bytes -= len(self._tail.popleft().encode("utf-8"))
            self.limit_hit = True


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
    completion = await run_process(command, cwd, float(timeout), captured.append, cancel)
    text = captured.render()
    is_error = False
    if captured.limit_hit:
        is_error = True
    elif completion.timed_out:
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
            "Run a foreground shell command in the invocation directory and return combined stdout "
            "and stderr. The default timeout is 120 seconds. Output above 50 KiB, 2000 lines, or 500 "
            "bytes per line is truncated and the process group is stopped; narrow verbose commands "
            "before retrying."
        ),
        params=list(BASH_PARAMS),
    )

    async def run(arguments_json: str, cancel: CancelToken) -> Output:
        return await run_bash(cwd, arguments_json, cancel)

    return Tool(definition=definition, run=run)
