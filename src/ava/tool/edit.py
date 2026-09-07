"""Exact, non-overlapping text replacement that fails on a missing or ambiguous match."""

from __future__ import annotations

import json
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
        "edits",
        "One or more targeted replacements against the original file. Each oldText must be unique; edits must not overlap. Merge nearby changes into one edit.",
        ToolParamType.array,
        True,
        items={
            "type": "object",
            "properties": {"oldText": {"type": "string"}, "newText": {"type": "string"}},
            "required": ["oldText", "newText"],
            "additionalProperties": False,
        },
    ),
]


def run_edit(cwd: Path, arguments_json: str) -> Output:
    arguments = parse_arguments(arguments_json)
    if isinstance(arguments, str):
        return error_output(
            f"invalid edit arguments: {arguments}. Use path and edits: [{{oldText, newText}}]"
        )
    raw_path = arguments.get("path")
    if not isinstance(raw_path, str) or not raw_path:
        return error_output("missing 'path' argument; call edit with the file to change")
    edits = arguments.get("edits")
    if isinstance(edits, str):
        try:
            edits = json.loads(edits)
        except json.JSONDecodeError:
            pass
    if isinstance(edits, dict):
        edits = [edits]
    legacy = edits is None and "old_string" in arguments
    if legacy:
        edits = [{"oldText": arguments.get("old_string"), "newText": arguments.get("new_string")}]
    if not isinstance(edits, list) or not edits:
        return error_output("'edits' must be a non-empty array of {oldText, newText} replacements")
    for edit_index, edit in enumerate(edits):
        if (
            not isinstance(edit, dict)
            or not isinstance(edit.get("oldText"), str)
            or not edit["oldText"]
            or not isinstance(edit.get("newText"), str)
        ):
            return error_output(
                f"edits[{edit_index}] requires non-empty oldText and string newText"
            )
        if edit["oldText"] == edit["newText"]:
            return error_output("oldText and newText are identical; no edit is needed")
    replace_all = legacy and bool(arguments.get("replace_all", False))

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
    spans: list[tuple[int, int, str]] = []
    for edit_index, edit in enumerate(edits):
        old, new = edit["oldText"], edit["newText"]
        positions = []
        offset = 0
        while (index := original.find(old, offset)) >= 0:
            positions.append(index)
            offset = index + len(old)
        if not positions:
            return error_output(
                f"edits[{edit_index}].oldText was not found. Read the current file and copy exact text; no changes were written"
            )
        if len(positions) != 1 and not replace_all:
            return error_output(
                f"edits[{edit_index}].oldText is ambiguous. Include enough context to match once; no changes were written"
            )
        spans.extend((index, index + len(old), new) for index in positions)
    spans.sort(key=lambda span: span[0])
    if any(left[1] > right[0] for left, right in zip(spans, spans[1:], strict=False)):
        return error_output(
            "Edits overlap in the original file. Merge them into one replacement; no changes were written"
        )
    updated = original
    for start, end, new in reversed(spans):
        updated = updated[:start] + new + updated[end:]
    matches = len(spans)

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
            "Edit one file with targeted exact replacements. Batch separate locations in one edits array. "
            "Each oldText must match a unique, non-overlapping region of the original file. Keep it "
            "as small as possible while unique; merge nearby or overlapping changes. Validate all edits before writing once."
        ),
        params=list(EDIT_PARAMS),
    )

    async def run(arguments_json: str, cancel: CancelToken) -> Output:
        cancel.raise_if_cancelled()
        return run_edit(cwd, arguments_json)

    return Tool(definition=definition, run=run)
