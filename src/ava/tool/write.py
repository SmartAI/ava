"""Whole-file creation or replacement, reporting created versus overwritten."""

from __future__ import annotations

from pathlib import Path

from ava.base import CancelToken
from ava.llm.types import ToolDef, ToolParam, ToolParamType
from ava.tool.api import Output, Tool, error_output, parse_arguments, resolve_path

WRITE_PARAMS = [
    ToolParam(
        "path",
        "Path relative to the invocation directory, or an absolute path.",
        ToolParamType.string,
        True,
    ),
    ToolParam(
        "content",
        "Complete file content. Existing content is replaced exactly.",
        ToolParamType.string,
        True,
    ),
]


def run_write(cwd: Path, arguments_json: str) -> Output:
    arguments = parse_arguments(arguments_json)
    if isinstance(arguments, str):
        return error_output(
            f"invalid write arguments: {arguments}. Use string path and content values"
        )
    raw_path = arguments.get("path")
    if not isinstance(raw_path, str) or not raw_path:
        return error_output(
            "missing 'path' argument; call write with the file to create or replace"
        )
    content = arguments.get("content")
    if content is None:
        return error_output(
            "missing 'content' argument; provide the complete file content, including an empty "
            "string when the file should be empty"
        )
    if not isinstance(content, str):
        return error_output("invalid write arguments: 'content' must be a string")
    path = resolve_path(cwd, raw_path)
    existed = path.exists()
    if existed and not path.is_file():
        return error_output(f"cannot write '{path}': it is not a regular file. Provide a file path")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        return error_output(f"cannot create parent directories for '{path}': {error.strerror}")
    encoded = content.encode("utf-8")
    try:
        path.write_bytes(encoded)
    except OSError:
        return error_output(
            f"cannot open '{path}' for writing. Check the path and permissions, then call write again"
        )
    verb = "overwrote" if existed else "created"
    return Output(text=f"{verb} '{path}' ({len(encoded)} bytes)")


def make_write_tool(cwd: Path) -> Tool:
    definition = ToolDef(
        name="write",
        description=(
            "Create or completely overwrite a regular file with exact content. Missing parent "
            "directories are created automatically; use edit for a targeted change."
        ),
        params=list(WRITE_PARAMS),
    )

    async def run(arguments_json: str, cancel: CancelToken) -> Output:
        return run_write(cwd, arguments_json)

    return Tool(definition=definition, run=run)
