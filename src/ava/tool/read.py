"""Bounded text-file reads for a model that must decide its next call.

Exact text with no injected line numbers, so it can be copied into ``edit`` unchanged.
"""

from __future__ import annotations

from pathlib import Path

from ava.base import CancelToken
from ava.llm.types import ToolDef, ToolParam, ToolParamType
from ava.tool.api import Output, Tool, error_output, optional_int, parse_arguments, resolve_path

READ_MAX_OUTPUT_BYTES = 50 * 1024
READ_MAX_OUTPUT_LINES = 2000
TRUNCATION_NOTICE_RESERVE = 96

READ_PARAMS = [
    ToolParam(
        "path",
        "Path relative to the invocation directory, or an absolute path.",
        ToolParamType.string,
        True,
    ),
    ToolParam(
        "offset", "1-indexed first line to return. Defaults to 1.", ToolParamType.integer, minimum=1
    ),
    ToolParam("limit", "Maximum number of lines to return.", ToolParamType.integer, minimum=1),
]


def run_read(cwd: Path, arguments_json: str) -> Output:
    arguments = parse_arguments(arguments_json)
    if isinstance(arguments, str):
        return error_output(
            f"invalid read arguments: {arguments}. Use path plus optional integer offset and limit"
        )
    raw_path = arguments.get("path")
    if not isinstance(raw_path, str) or not raw_path:
        return error_output("missing 'path' argument; call read with the file to inspect")
    offset, problem = optional_int(arguments, "offset")
    if problem:
        return error_output(f"invalid read arguments: {problem}")
    limit, problem = optional_int(arguments, "limit")
    if problem:
        return error_output(f"invalid read arguments: {problem}")
    offset = offset if offset is not None else 1
    if offset < 1:
        return error_output("'offset' must be at least 1 because read uses 1-indexed lines")
    if limit is not None and limit < 1:
        return error_output("'limit' must be at least 1; omit it to use the default cap")

    path = resolve_path(cwd, raw_path)
    if not path.exists():
        return error_output(
            f"cannot read '{path}': the file does not exist. Check the path and call read again"
        )
    if not path.is_file():
        return error_output(
            f"cannot read '{path}': it is not a regular text file. Provide a file path"
        )
    try:
        data = path.read_bytes()
    except OSError:
        return error_output(f"cannot read '{path}'. Check file permissions and call read again")
    if b"\0" in data:
        return error_output(f"cannot read '{path}' as text because it contains NUL bytes")

    lines = data.split(b"\n")
    had_final_newline = data.endswith(b"\n")
    if had_final_newline:
        lines.pop()
    for index, line in enumerate(lines, start=1):
        if len(line) > READ_MAX_OUTPUT_BYTES - TRUNCATION_NOTICE_RESERVE:
            if index < offset or index >= offset:
                return error_output(f"line {index} in '{path}' exceeds the 50 KiB output limit")
    if offset > len(lines):
        if not lines and offset == 1:
            return Output(text="")
        extent = "the file has no lines" if not lines else f"the last line is {len(lines)}"
        return error_output(f"offset {offset} is past the end of '{path}'; {extent}")

    output = bytearray()
    returned = 0
    line_number = offset
    truncated = False
    cap = min(READ_MAX_OUTPUT_LINES, limit) if limit is not None else READ_MAX_OUTPUT_LINES
    while returned < cap and line_number <= len(lines):
        rendered = lines[line_number - 1]
        if line_number < len(lines) or had_final_newline:
            rendered = rendered + b"\n"
        if len(rendered) > READ_MAX_OUTPUT_BYTES - TRUNCATION_NOTICE_RESERVE - len(output):
            truncated = True
            break
        output += rendered
        returned += 1
        line_number += 1
    if not truncated:
        truncated = line_number <= len(lines)
    text = output.decode("utf-8", "replace")
    if truncated:
        notice = f"[Output truncated. Continue with offset={line_number}.]"
        if text:
            text += "\n" if text.endswith("\n") else "\n\n"
        text += notice
    return Output(text=text)


def make_read_tool(cwd: Path) -> Tool:
    definition = ToolDef(
        name="read",
        description=(
            "Read the exact contents of a regular text file. Optional 1-indexed offset and limit "
            "select a line range; truncated output reports the next offset."
        ),
        params=list(READ_PARAMS),
    )

    async def run(arguments_json: str, cancel: CancelToken) -> Output:
        return run_read(cwd, arguments_json)

    return Tool(definition=definition, run=run)
