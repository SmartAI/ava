"""A real tool run must replay without touching the workspace or calling a provider."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from ava.agent import Agent, CompactionOptions
from ava.agent.recording import Recording, replay_recording
from ava.base import AvaError
from ava.session.inspect import inspect_session
from ava.tool import make_write_tool
from tests.conftest import (
    ScriptedProvider,
    message,
    provider_error,
    text_response,
    tool_call_response,
)


async def record_run(project: Path, *, fail: bool = False) -> Path:
    item = message("Write the answer to answer.txt")
    provider = ScriptedProvider(
        [
            tool_call_response(
                "write-1", "write", json.dumps({"path": "answer.txt", "content": "42\n"})
            ),
            provider_error("injected outage") if fail else text_response("Done"),
        ]
    )
    options = CompactionOptions(enabled=False)
    recording_path = project / "recording.jsonl"
    recording = Recording(recording_path, provider, item, options)
    failure = None
    try:
        async with Agent.create_at(
            recording.provider(),
            project,
            project / "session.jsonl",
            options,
            tools=recording.tools([make_write_tool(project)]),
        ) as agent:
            await agent.followup(item)
            try:
                await agent.drive()
            except AvaError as error:
                failure = error
        recording.finish(failure)
    finally:
        recording.close()
    assert (project / "answer.txt").read_text() == "42\n"
    return recording_path


@pytest.mark.parametrize("fail", [False, True])
async def test_recorded_drive_replays_without_live_io(home: Path, project: Path, fail: bool):
    path = await record_run(project, fail=fail)
    # A live write replay would overwrite this sentinel. Do not rely just on a mocked tool.
    (project / "answer.txt").write_text("must survive replay")
    result = await replay_recording(path)
    assert result["matched"] and result["exchanges"] == 3
    assert bool(result["reproduced_error"]) is fail
    assert (project / "answer.txt").read_text() == "must survive replay"
    assert path.stat().st_mode & 0o777 == 0o600
    summary = inspect_session(project / "session.jsonl")
    assert summary["tool_calls"] == {"write": 1}
    assert summary["last_turn_reason"] == ("provider_error" if fail else "completed")
    assert summary["tokens"]["input"] is None


async def test_replay_rejects_changed_input_and_incomplete_recording(home: Path, project: Path):
    path = await record_run(project)
    records = path.read_text().splitlines()
    header = json.loads(records[0])
    header["input"]["blocks"][0]["text"] = "different task"
    changed = project / "changed.jsonl"
    changed.write_text("\n".join([json.dumps(header), *records[1:]]) + "\n")
    with pytest.raises(AvaError, match=r"exchange 1: request.items"):
        await replay_recording(changed)
    changed.write_text("\n".join(records[:-1]) + "\n")
    with pytest.raises(AvaError, match="incomplete"):
        await replay_recording(changed)
    model = json.loads(records[1])
    del model["request"]["system_prompt"]
    changed.write_text("\n".join([records[0], json.dumps(model), *records[2:]]) + "\n")
    with pytest.raises(AvaError, match="cannot read recording"):
        await replay_recording(changed)


def test_cli_can_record_and_replay_a_new_mock_run(home: Path, project: Path, monkeypatch):
    script = project / "mock.txt"
    script.write_text("text done\ndone\n")
    monkeypatch.setenv("AVA_MOCK_SCRIPT", str(script))
    command = [
        sys.executable,
        "-m",
        "ava.app.cli",
        "-p",
        "--provider",
        "mock",
        "--model",
        "smoke-v1",
        "--session",
        "session.jsonl",
        "--record",
        "recording.jsonl",
        "hello",
    ]
    first = subprocess.run(command, cwd=project, capture_output=True, text=True, timeout=15)
    assert first.returncode == 0, first.stderr
    assert first.stdout.strip() == "done"
    replay = subprocess.run(
        [sys.executable, "-m", "ava.app.cli", "session", "replay", "recording.jsonl"],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert replay.returncode == 0, replay.stderr
    assert json.loads(replay.stdout)["matched"]
    before = (project / "recording.jsonl").read_bytes()
    again = subprocess.run(command, cwd=project, capture_output=True, text=True, timeout=15)
    assert again.returncode == 2
    assert (project / "recording.jsonl").read_bytes() == before


async def test_cli_inspection_and_replay_are_read_only(home: Path, project: Path):
    path = await record_run(project)
    before = (project / "session.jsonl").read_bytes()
    for command, file in (("inspect", project / "session.jsonl"), ("replay", path)):
        completed = subprocess.run(
            [sys.executable, "-m", "ava.app.cli", "session", command, str(file)],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert completed.returncode == 0, completed.stderr
        assert json.loads(completed.stdout)["schema_version"] == 1
    assert (project / "session.jsonl").read_bytes() == before
