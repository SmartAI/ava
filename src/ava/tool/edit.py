"""Exact, non-overlapping text replacement that fails on a missing or ambiguous match."""

from __future__ import annotations

from pathlib import Path

from ava.base import CancelToken
from ava.llm.types import ToolDef, ToolParam, ToolParamType
from ava.tool.api import Output, Tool, error_output, parse_arguments, resolve_path

MAX_EDIT_BYTES = 4 * 1024 * 1024

EDIT_PARAMS = [
    ToolParam(
        "path",
        "Path relative to the invocation directory, or an absolute path.",
        ToolParamType.string,
        True,
    ),
    ToolParam(
        "old_string",
        "Exact text to find. It must be unique unless replace_all is true.",
        ToolParamType.string,
        True,
    ),
    ToolParam(
        "new_string",
        "Exact replacement text. An empty string deletes the match.",
        ToolParamType.string,
        True,
    ),
    ToolParam(
        "replace_all",
        "Replace every non-overlapping match instead of requiring uniqueness.",
        ToolParamType.boolean,
    ),
]


def run_edit(cwd: Path, arguments_json: str) -> Output:
    arguments = parse_arguments(arguments_json)
    if isinstance(arguments, str):
        return error_output(
            f"invalid edit arguments: {arguments}. Use path, old_string, new_string, and optional replace_all"
        )
    raw_path = arguments.get("path")
    if not isinstance(raw_path, str) or not raw_path:
        return error_output("missing 'path' argument; call edit with the file to change")
    old = arguments.get("old_string")
    if old is None:
        return error_output("missing 'old_string' argument; provide exact text copied from read")
    if not isinstance(old, str) or old == "":
        return error_output("'old_string' must be non-empty; provide exact text copied from read")
    new = arguments.get("new_string")
    if new is None:
        return error_output(
            "missing 'new_string' argument; provide replacement text or an empty string to delete"
        )
    if not isinstance(new, str):
        return error_output("invalid edit arguments: 'new_string' must be a string")
    if old == new:
        return error_output("'old_string' and 'new_string' are identical; no edit is needed")
    replace_all = bool(arguments.get("replace_all", False))

    path = resolve_path(cwd, raw_path)
    if not path.exists():
        return error_output(
            f"cannot edit '{path}': the file does not exist. Check the path and call edit again"
        )
    if not path.is_file():
        return error_output(
            f"cannot edit '{path}': it is not a regular text file. Provide a file path"
        )
    try:
        data = path.read_bytes()
    except OSError:
        return error_output(
            f"cannot read '{path}' before editing. Check file permissions and call edit again"
        )
    if len(data) > MAX_EDIT_BYTES:
        return error_output(f"cannot edit '{path}': the file exceeds the 4 MiB edit limit")
    if b"\0" in data:
        return error_output(f"cannot edit '{path}' as text because it contains NUL bytes")
    original = data.decode("utf-8", "surrogateescape")
    matches = original.count(old)
    if matches == 0:
        return error_output(
            f"'old_string' was not found in '{path}'. Read the file again and copy its exact current text"
        )
    if matches > 1 and not replace_all:
        return error_output(
            f"'old_string' matches {matches} places in '{path}'. Include more surrounding text to make it "
            "unique, or set replace_all to true"
        )
    updated = original.replace(old, new)
    try:
        path.write_bytes(updated.encode("utf-8", "surrogateescape"))
    except OSError:
        return error_output(
            f"cannot open '{path}' for editing. Check file permissions and call edit again"
        )
    plural = "" if matches == 1 else "s"
    return Output(text=f"replaced {matches} occurrence{plural} in '{path}'")


def make_edit_tool(cwd: Path) -> Tool:
    definition = ToolDef(
        name="edit",
        description=(
            "Replace exact text in a regular file. old_string must match exactly once unless "
            "replace_all is true; failures explain how to correct a missing or ambiguous match."
        ),
        params=list(EDIT_PARAMS),
    )

    async def run(arguments_json: str, cancel: CancelToken) -> Output:
        return run_edit(cwd, arguments_json)

    return Tool(definition=definition, run=run)
