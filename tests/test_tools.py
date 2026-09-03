"""Each built-in tool's contract, including the shared output caps."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from ava.base import CancelToken
from ava.tool import make_bash_tool, make_edit_tool, make_read_tool, make_write_tool


def args(**values) -> str:
    return json.dumps(values)


async def test_read_returns_exact_text_and_reports_next_offset(project: Path):
    (project / "a.txt").write_text("one\ntwo\nthree\n")
    read = make_read_tool(project)
    out = await read.run(args(path="a.txt"), CancelToken())
    assert out.text == "one\ntwo\nthree\n" and not out.is_error
    out = await read.run(args(path="a.txt", offset=2, limit=1), CancelToken())
    assert out.text == "two\n\n[Output truncated. Continue with offset=3.]"
    out = await read.run(args(path="missing.txt"), CancelToken())
    assert out.is_error and "does not exist" in out.text
    out = await read.run(args(path="a.txt", offset=9), CancelToken())
    assert out.is_error and "past the end" in out.text
    out = await read.run("not json", CancelToken())
    assert out.is_error and out.text.startswith("invalid read arguments")


async def test_write_reports_created_then_overwrote(project: Path):
    write = make_write_tool(project)
    out = await write.run(args(path="nested/dir/new.txt", content="hello"), CancelToken())
    assert (
        out.text.startswith("created") and (project / "nested/dir/new.txt").read_text() == "hello"
    )
    out = await write.run(args(path="nested/dir/new.txt", content=""), CancelToken())
    assert out.text.startswith("overwrote") and (project / "nested/dir/new.txt").read_text() == ""
    out = await write.run(args(path="x.txt"), CancelToken())
    assert out.is_error and "missing 'content'" in out.text


async def test_edit_requires_unique_match_unless_replace_all(project: Path):
    (project / "f.py").write_text("a = 1\nb = 1\n")
    edit = make_edit_tool(project)
    out = await edit.run(args(path="f.py", old_string="= 1", new_string="= 2"), CancelToken())
    assert out.is_error and "matches 2 places" in out.text
    out = await edit.run(
        args(path="f.py", old_string="= 1", new_string="= 2", replace_all=True), CancelToken()
    )
    assert out.text == f"replaced 2 occurrences in '{project / 'f.py'}'"
    assert (project / "f.py").read_text() == "a = 2\nb = 2\n"
    out = await edit.run(args(path="f.py", old_string="nope", new_string="x"), CancelToken())
    assert out.is_error and "was not found" in out.text
    out = await edit.run(args(path="f.py", old_string="a = 2\n", new_string=""), CancelToken())
    assert (
        out.text.startswith("replaced 1 occurrence") and (project / "f.py").read_text() == "b = 2\n"
    )


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell")
async def test_bash_captures_ordered_output_and_status(project: Path):
    bash = make_bash_tool(project)
    out = await bash.run(args(command="echo out; echo err >&2; pwd"), CancelToken())
    assert out.text == f"out\nerr\n{project.resolve()}\n" and not out.is_error
    out = await bash.run(args(command="exit 3"), CancelToken())
    assert out.is_error and out.text == "[Command exited with code 3.]"
    out = await bash.run(args(command="true"), CancelToken())
    assert out.text == "(no output)"
    out = await bash.run(args(command="cd /; pwd"), CancelToken())
    assert out.text == "/\n"
    # Every call starts fresh in the invocation directory.
    out = await bash.run(args(command="pwd"), CancelToken())
    assert out.text == f"{project.resolve()}\n"
    out = await bash.run(args(command="echo $TERM $NO_COLOR $LC_ALL"), CancelToken())
    assert out.text == "dumb 1 C\n"


async def test_bash_timeout_stops_the_process_group(project: Path):
    bash = make_bash_tool(project)
    out = await bash.run(
        args(command="echo start; sleep 30; echo never", timeout_seconds=1), CancelToken()
    )
    assert (
        out.is_error and out.text.startswith("start\n") and "timed out after 1 second" in out.text
    )


async def test_bash_output_cap_stops_producer_and_keeps_head_and_tail(project: Path):
    bash = make_bash_tool(project)
    out = await bash.run(args(command="yes 'line' | head -n 100000"), CancelToken())
    assert out.is_error
    assert out.text.startswith("line\n")
    assert "[Output truncated and the command was stopped" in out.text
    assert len(out.text.encode()) <= 50 * 1024
    out = await bash.run(args(command="head -c 2000 /dev/zero | tr '\\0' 'x'; echo"), CancelToken())
    assert "... [line exceeded 500 bytes]" in out.text and out.is_error


async def test_bash_cancellation_reports_cancelled(project: Path):
    import asyncio

    from ava.base import AvaError

    bash = make_bash_tool(project)
    token = CancelToken()
    task = asyncio.create_task(bash.run(args(command="sleep 30"), token))
    await asyncio.sleep(0.2)
    token.cancel()
    with pytest.raises(AvaError) as info:
        await task
    assert info.value.kind.value == "cancelled"
