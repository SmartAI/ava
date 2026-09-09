"""Benchmark controls must affect real runs and preserve honest usage accounting."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ava.session.event import Selection, Usage
from ava.session.inspect import inspect_session
from ava.session.log import Log
from eval.integrations.agent_config import (
    ava_arguments,
    ava_usage,
    model_parts,
    pi_arguments,
    pi_session_summary,
)


def test_baseline_controls_allow_project_instructions_but_not_extensions():
    assert model_parts("anthropic/claude-sonnet-4-6", "off") == ("anthropic", "claude-sonnet-4-6")
    with pytest.raises(ValueError, match="requires effort='off'"):
        model_parts("anthropic/claude-sonnet-4-6", "high")
    with pytest.raises(ValueError, match="exact model ID"):
        model_parts("openai/default", "off")
    args = ava_arguments("openai", "gpt-5.4", "/logs/agent", effort="off", compaction=False)
    assert args[args.index("--effort") + 1] == "none"
    assert "--no-compact" in args and "--record" not in args
    for model in ("gpt-4.1", "gpt-4.1-mini", "gpt-4o-2024-08-06"):
        ordinary = ava_arguments("openai", model, "/logs/agent", effort="off", compaction=False)
        assert "--effort" not in ordinary
        with pytest.raises(ValueError, match="Non-reasoning"):
            model_parts("openai/" + model, "high")
    pi = pi_arguments("openai", "gpt-5.4", "/logs/agent", effort="off")
    assert pi[pi.index("--tools") + 1] == "read,edit,write,bash"
    assert "--no-context-files" not in pi
    for flag in ("--no-extensions", "--no-skills", "--no-approve", "--offline"):
        assert flag in pi


def test_codex_baseline_uses_native_oauth_provider_and_explicit_effort():
    assert model_parts("codex/gpt-6-astra", "medium") == ("codex", "gpt-6-astra")
    ava = ava_arguments("codex", "gpt-6-astra", "/logs", effort="medium", compaction=True)
    pi = pi_arguments("codex", "gpt-6-astra", "/logs", effort="medium")
    assert ava[ava.index("--provider") + 1] == "codex"
    assert ava[ava.index("--effort") + 1] == "medium"
    assert "--no-compact" not in ava
    assert pi[pi.index("--provider") + 1] == "openai-codex"


def test_usage_includes_cache_reasoning_and_compaction_without_double_counting():
    summary = {
        "provider": "anthropic", "tokens": {
            "input": 100, "cached_read": 40, "cache_write": 20,
            "cache_write_1h": 10, "output": 30, "reasoning": 15,
        },
    }
    ava = ava_usage(summary)
    assert (ava["n_input_tokens"], ava["n_output_tokens"], ava["n_cache_tokens"]) == (160, 30, 40)
    assert ava["cost_usd"] is None
    summary["provider"] = "openai"
    summary["tokens"]["cache_write"] = None
    openai = ava_usage(summary)
    assert (openai["n_input_tokens"], openai["n_output_tokens"]) == (140, 45)
    summary["tokens"]["cached_read"] = None
    assert ava_usage(summary)["n_input_tokens"] is None
    usage = {"input": 100, "output": 45, "cacheRead": 40, "cacheWrite": 20, "cost": {"total": 0.03}}
    records = [
        {"type": "model_change", "provider": "openai", "modelId": "gpt-5.4"},
        {"type": "thinking_level_change", "thinkingLevel": "off"},
        {"type": "compaction", "usage": usage},
        {"type": "message", "message": {
            "role": "assistant", "model": "gpt-5.4", "provider": "openai",
            "usage": usage, "stopReason": "stop", "content": [],
        }},
    ]
    pi = pi_session_summary("\n".join(map(json.dumps, records)), provider="openai", model="gpt-5.4", effort="off")
    assert (pi["n_input_tokens"], pi["n_output_tokens"], pi["n_cache_tokens"]) == (320, 90, 80)
    assert pi["cost_usd"] == 0.06 and pi["compactions"] == 1
    assert pi["completed"]
    with pytest.raises(ValueError, match="different model"):
        pi_session_summary("\n".join(map(json.dumps, records)), provider="openai", model="gpt-5.4-mini", effort="off")
    records[-1]["message"]["stopReason"] = "error"
    records[-1]["message"].pop("usage")
    failed = pi_session_summary("\n".join(map(json.dumps, records)), provider="openai", model="gpt-5.4", effort="off")
    assert not failed["completed"] and failed["cost_usd"] is None


@pytest.mark.parametrize("provider", ["openai", "codex"])
def test_inspection_restores_provider_totals_before_aggregating_optional_splits(project: Path, provider: str):
    for mixed in (False, True):
        path = project / f"usage-{mixed}.jsonl"
        log = Log.create_at(path, project, provider, "gpt-5.4")
        try:
            log.append(Usage(attempt_id="a", input=100, cached_read=40, output=30, reasoning=10))
            log.append(Usage(attempt_id="b", input=100, output=30))
            if mixed:
                log.append(Selection(provider="anthropic", model="claude-sonnet-4-6"))
        finally:
            log.close()
        summary = inspect_session(path)
        assert summary["tokens"]["reasoning"] is None
        assert summary["tokens"]["cached_read"] is None
        if mixed:
            assert summary["inclusive_tokens"] == {"input": None, "output": None}
        else:
            usage = ava_usage(summary)
            assert (usage["n_input_tokens"], usage["n_output_tokens"]) == (240, 70)
            assert usage["n_cache_tokens"] is None  # insufficient split to estimate cached pricing


def test_candidate_prompt_is_used_in_new_cli_run_and_cannot_override_resume(
    home: Path, project: Path, monkeypatch,
):
    mock = project / "mock.txt"
    mock.write_text("text done\ndone\n")
    monkeypatch.setenv("AVA_MOCK_SCRIPT", str(mock))
    prompt = project / "candidate.md"
    prompt.write_text("Only use precise file slices.\n")
    command = [
        sys.executable, "-m", "ava.app.cli", "-p", "--provider", "mock", "--model", "smoke-v1",
        "--session", "session.jsonl", "--record", "recording.jsonl",
        "--system-prompt-file", str(prompt), "hello",
    ]
    first = subprocess.run(command, cwd=project, capture_output=True, text=True, timeout=15)
    assert first.returncode == 0, first.stderr
    exchanges = [json.loads(line) for line in (project / "recording.jsonl").read_text().splitlines()]
    request = next(record["request"] for record in exchanges if record["kind"] == "model")
    assert request["system_prompt"] == prompt.read_text()
    replay = subprocess.run(
        [sys.executable, "-m", "ava.app.cli", "session", "replay", "recording.jsonl"],
        cwd=project, capture_output=True, text=True, timeout=15,
    )
    assert replay.returncode == 0, replay.stderr
    original = (project / "session.jsonl").read_bytes()
    resume = [arg for arg in command if arg not in ("--record", "recording.jsonl")]
    rejected = subprocess.run(resume, cwd=project, capture_output=True, text=True, timeout=15)
    assert rejected.returncode == 2 and "requires a new session" in rejected.stderr
    assert (project / "session.jsonl").read_bytes() == original


def test_invalid_candidate_prompt_fails_before_creating_session(home: Path, project: Path):
    prompt = project / "empty.md"
    prompt.write_text(" \n")
    result = subprocess.run(
        [sys.executable, "-m", "ava.app.cli", "-p", "--system-prompt-file", str(prompt),
         "--session", "session.jsonl", "hello"],
        cwd=project, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 2 and "must not be empty" in result.stderr
    assert not (project / "session.jsonl").exists()


def test_benchmark_cli_ignores_task_package_shadowing(home: Path, project: Path):
    # Real CLI run: both the working directory and PYTHONPATH contain a decoy ava.
    package = project / "ava"
    package.mkdir()
    (package / "__init__.py").write_text('raise RuntimeError("task source shadowed runtime")\n')
    mock = project / "mock.txt"
    mock.write_text("text isolated runtime\ndone\n")
    args = ava_arguments("mock", "smoke-v1", str(project), effort="off", compaction=False)
    args[0] = sys.executable
    result = subprocess.run(
        [*args, "hello"], cwd=project,
        env={**os.environ, "PYTHONPATH": str(project), "AVA_MOCK_SCRIPT": str(mock)},
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "isolated runtime" in result.stdout
    summary = inspect_session(project / "session.jsonl.zst")
    assert summary["provider"] == "mock" and summary["model_attempts"] == 1
