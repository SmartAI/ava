"""Each built-in tool's contract, including the shared output caps."""

from __future__ import annotations

import json
import shlex
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


@pytest.mark.parametrize(
    ("case", "offset", "limit", "expected", "is_error"),
    [
        ("long-before-range", 3, 1, "suffix\n", False),
        (
            "long-after-range",
            1,
            1,
            "prefix\n\n[Output truncated. Continue with offset=2.]",
            False,
        ),
        ("long-first-in-range", 2, 1, "line 2", True),
        (
            "long-later-in-range",
            1,
            3,
            "prefix\n\n[Output truncated. Continue with offset=2.]",
            False,
        ),
        ("past-eof", 4, 1, "past the end", True),
        ("empty", 1, 1, "", False),
        ("empty-past-eof", 2, 1, "past the end", True),
    ],
)
async def test_read_long_lines_only_affect_requested_range(
    project: Path, case: str, offset: int, limit: int, expected: str, is_error: bool
):
    content = "" if case.startswith("empty") else "prefix\n" + "x" * 60_000 + "\nsuffix\n"
    (project / "range.txt").write_text(content)
    out = await make_read_tool(project).run(
        args(path="range.txt", offset=offset, limit=limit), CancelToken()
    )
    assert out.is_error is is_error
    if is_error:
        assert expected in out.text
        if case == "long-first-in-range":
            assert "exceeds the 50 KiB output limit" in out.text
    else:
        assert out.text == expected
    assert len(out.text.encode()) <= 50 * 1024


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
    assert out.is_error and "ambiguous" in out.text
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


async def test_edit_batches_disjoint_original_matches(project: Path):
    path = project / "config.txt"
    path.write_bytes(b"red\r\nuntouched\r\nblue\r\n")
    out = await make_edit_tool(project).run(
        args(
            path="config.txt",
            edits=[
                {"oldText": "red", "newText": "blue"},
                {"oldText": "blue", "newText": "green"},
            ],
        ),
        CancelToken(),
    )
    assert not out.is_error
    assert path.read_bytes() == b"blue\r\nuntouched\r\ngreen\r\n"
    invalid = await make_edit_tool(project).run("not json", CancelToken())
    assert invalid.is_error and "invalid edit arguments" in invalid.text


@pytest.mark.parametrize(
    "second",
    [
        {"oldText": "missing", "newText": "x"},
        {"oldText": "red\nblue", "newText": "x"},
        {"oldText": "green", "newText": "x"},
    ],
)
async def test_edit_validates_whole_batch_before_writing(project: Path, second: dict):
    path = project / "config.txt"
    original = b"red\nblue\n"
    path.write_bytes(original)
    out = await make_edit_tool(project).run(
        args(path="config.txt", edits=[{"oldText": "red", "newText": "green"}, second]),
        CancelToken(),
    )
    assert out.is_error and path.read_bytes() == original


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


@pytest.mark.parametrize(
    "payload", ["x" * 600, "Progress 50%\r" * 100, "row\n" * 3000, "界" * 20000]
)
async def test_bash_display_truncation_preserves_completion(
    project: Path, monkeypatch, payload: str
):
    monkeypatch.setattr("tempfile.tempdir", str(project))
    script = (
        "import sys,time;from pathlib import Path;"
        f"sys.stdout.write({payload!r});sys.stdout.flush();time.sleep(.05);"
        "Path('finished').write_text('done');print('FINAL')"
    )
    out = await make_bash_tool(project).run(
        args(command=f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"), CancelToken()
    )
    assert not out.is_error and (project / "finished").read_text() == "done"
    assert "FINAL" in out.text and len(out.text.encode()) <= 50 * 1024
    assert len(out.text.split("\n")) <= 2000
    logs = list(project.glob("ava-bash-*.log"))
    if logs:
        assert len(logs) == 1 and logs[0].read_bytes() == (payload + "FINAL\n").encode()
        assert f"Full output: {logs[0]}" in out.text
        if payload.startswith("row"):
            read = await make_read_tool(project).run(
                args(path=str(logs[0]), limit=2), CancelToken()
            )
            assert not read.is_error and read.text.startswith("row\nrow\n")
    else:
        assert out.text == payload + "FINAL\n"


async def test_bash_log_cap_preserves_exit_status_and_reports_partial_log(
    project: Path, monkeypatch
):
    monkeypatch.setattr("tempfile.tempdir", str(project))
    monkeypatch.setattr("ava.tool.bash.BASH_MAX_LOG_BYTES", 60 * 1024)
    script = "import sys;print('x'*70000);sys.exit(7)"
    out = await make_bash_tool(project).run(
        args(command=f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"), CancelToken()
    )
    assert out.is_error and "Command exited with code 7" in out.text
    assert "Log contains only the first 61440 bytes" in out.text
    assert "Full output:" not in out.text and len(out.text.encode()) <= 50 * 1024
    log = next(project.glob("ava-bash-*.log"))
    assert log.stat().st_size == 60 * 1024


async def test_bash_archive_failure_reports_possible_partial_execution(project: Path, monkeypatch):
    def unavailable(**kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("tempfile.mkstemp", unavailable)
    script = "print('x'*60000)"
    out = await make_bash_tool(project).run(
        args(command=f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"), CancelToken()
    )
    assert out.is_error and "cannot retain command output" in out.text
    assert "may have partially executed" in out.text


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


@pytest.mark.parametrize("shape", ["object", "string"])
async def test_edit_recovers_argument_shape_without_changing_text(project: Path, shape: str):
    path = project / "shape.txt"
    path.write_bytes(b"alpha\r\nuntouched\r\n")
    edit = {"oldText": "alpha", "newText": "ALPHA"}
    value = edit if shape == "object" else json.dumps([edit])
    result = await make_edit_tool(project).run(args(path="shape.txt", edits=value), CancelToken())
    assert not result.is_error
    assert path.read_bytes() == b"ALPHA\r\nuntouched\r\n"


@pytest.mark.parametrize("factory", [make_read_tool, make_write_tool, make_edit_tool])
async def test_cancelled_file_tool_does_not_execute(project: Path, factory):
    from ava.base import AvaError

    path = project / "cancelled.txt"
    path.write_text("original")
    cancel = CancelToken()
    cancel.cancel()
    with pytest.raises(AvaError):
        await factory(project).run(
            args(
                path="cancelled.txt",
                content="changed",
                edits=[{"oldText": "original", "newText": "changed"}],
            ),
            cancel,
        )
    assert path.read_text() == "original"


@pytest.mark.parametrize("active", [False, True])
async def test_bash_waits_for_process_exit_then_output_idle(project: Path, active: bool):
    child = (
        "import time; [(print('tick',flush=True),time.sleep(.04)) for _ in range(8)]; "
        "print('FINAL',flush=True); time.sleep(2)"
        if active
        else "import time; time.sleep(2)"
    )
    script = f'import subprocess,sys; subprocess.Popen([sys.executable,"-c",{child!r}]); print("parent",flush=True)'
    result = await make_bash_tool(project).run(
        args(command=shlex.join([sys.executable, "-c", script]), timeout_seconds=1), CancelToken()
    )
    assert not result.is_error and "parent" in result.text
    if active:
        assert "FINAL" in result.text


@pytest.mark.parametrize("timeout,expected", [(3, "code 7"), (1, "timed out")])
async def test_bash_closed_output_preserves_command_deadline(
    project: Path, timeout: int, expected: str
):
    script = "import os,time; os.close(1); os.close(2); time.sleep(1.5); raise SystemExit(7)"
    result = await make_bash_tool(project).run(
        args(command=shlex.join([sys.executable, "-c", script]), timeout_seconds=timeout),
        CancelToken(),
    )
    assert result.is_error and expected in result.text
