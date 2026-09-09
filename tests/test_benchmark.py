"""Trustworthy experiment evidence: outcomes, unknown costs, and frozen paired samples."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from eval.benchmark import (
    AgentSpec,
    Experiment,
    Price,
    compare_runs,
    normalize,
    plan,
    price_row,
    run,
    summarize,
    verify_identity,
)


def _suite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr("eval.benchmark.ROOT", tmp_path)
    tasks = []
    for name in ("one", "two"):
        directory = tmp_path / name
        (directory / "tests").mkdir(parents=True)
        (directory / "instruction.md").write_text(f"Repair {name}.\n")
        (directory / "task.toml").write_text('schema_version = "1.4"\n')
        (directory / "tests/test.sh").write_text("#!/bin/sh\nexit 0\n")
        tasks.append({"id": name, "path": name, "split": "development", "origin": "authored"})
    suite = tmp_path / "suite.json"
    suite.write_text(json.dumps({"schema_version": 1, "name": "tests", "tasks": tasks}))
    return suite


def _experiment() -> Experiment:
    return Experiment(
        name="test",
        suite="suite.json",
        agents=[AgentSpec(id="ava", kind="ava", model="openai/gpt-4.1-2025-04-14")],
        repetitions=1,
        max_trials=2,
    )


def test_plan_hashes_inputs_and_rejects_unsafe_task_destinations(tmp_path: Path, monkeypatch):
    suite = _suite(tmp_path, monkeypatch)
    first = plan(_experiment())
    assert first["schedule"] == plan(_experiment())["schedule"]
    (tmp_path / "one/instruction.md").write_text("Changed task.\n")
    assert first["task_set_sha256"] != plan(_experiment())["task_set_sha256"]
    content = json.loads(suite.read_text())
    content["tasks"][0]["id"] = "../outside-experiment"
    suite.write_text(json.dumps(content))
    with pytest.raises(ValueError):
        plan(_experiment())
    assert not (tmp_path / "outside-experiment").exists()


def test_pi_identity_rejects_version_or_lockfile_drift():
    spec = AgentSpec(id="pi", kind="pi", model="codex/gpt-6-astra", effort="medium")
    row = {"agent_info": {"version": "0.85.1", "model_info": {"provider": "codex", "name": "gpt-6-astra"}},
           "metadata": {"version": "0.85.1", "lock_sha256": "frozen-lock"}}
    inputs = {"version": "0.85.1", "lock_sha256": "frozen-lock"}
    verify_identity(row, spec, inputs)
    row["metadata"]["lock_sha256"] = "changed-lock"
    with pytest.raises(ValueError, match="lockfile"):
        verify_identity(row, spec, inputs)
    row["metadata"]["lock_sha256"] = "frozen-lock"
    row["metadata"]["version"] = "0.84.0"
    with pytest.raises(ValueError, match="version"):
        verify_identity(row, spec, inputs)


@pytest.mark.parametrize("kind,reward,reason", [("oracle", 0, "reference solution"), ("nop", 1, "unchanged workspace")])
def test_bad_control_stops_before_remaining_trials(tmp_path, monkeypatch, kind, reward, reason):
    _suite(tmp_path, monkeypatch)
    package = tmp_path / "eval"
    (package / "integrations").mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "constraints.txt").write_text("")
    (package / "integrations/__init__.py").write_text("")
    calls = []

    def execute(args, **kwargs):
        if "--version" in args:
            return subprocess.CompletedProcess(args, 0, stdout="harbor 0.22.0")
        job = json.loads(Path(args[-1]).read_text())
        calls.append(job)
        trial = Path(job["jobs_dir"]) / job["job_name"] / "task"
        trial.mkdir(parents=True)
        (trial / "result.json").write_text(json.dumps({"verifier_result": {"rewards": {"reward": reward}}}))
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr("eval.benchmark.subprocess.run", execute)
    config = Experiment(name="controls", suite="suite.json", agents=[AgentSpec(id=kind, kind=kind)], repetitions=1)
    report = run(config, tmp_path / "results", tmp_path / "harbor")
    assert reason in report["stop_reason"]
    assert len(calls) == 1
    assert report["agents"][kind]["completed_trials"] == 1
    assert report["agents"][kind]["expected_trials"] == 2


def _row(correct: bool | None = False, *, task: dict | None = None) -> dict:
    raw = {
        "agent_info": {
            "name": "ava",
            "version": "0.1.0",
            "model_info": {"provider": "openai", "name": "gpt-4.1-2025-04-14"},
        },
        "agent_result": {
            "n_input_tokens": 1_000,
            "n_cache_tokens": 100,
            "n_output_tokens": 500,
            "metadata": {
                "mode": "live",
                "cache_write_tokens": 0,
                "wheel_sha256": "wheel-hash",
                "session": {"provider": "openai", "model": "gpt-4.1-2025-04-14"},
            },
        },
        "verifier_result": {"rewards": {"reward": int(correct)}} if correct is not None else None,
    }
    return normalize(
        raw,
        {"task": task["id"] if task else "one", "attempt": 0, "agent": "ava"},
        task or {"sha256": "task-hash", "split": "development", "origin": "authored"},
    )


def test_normalize_distinguishes_agent_failure_infrastructure_and_missing_grade():
    scheduled = {"task": "one", "attempt": 0, "agent": "ava"}
    task = {"sha256": "task-hash", "split": "development", "origin": "authored"}
    assert _row(True)["correct"] is True
    assert _row(False)["correct"] is False
    assert _row(None)["correct"] is None
    partial_verifier = normalize({"verifier_result": {"rewards": None}}, scheduled, task)
    assert partial_verifier["status"] == "ungraded" and partial_verifier["correct"] is None
    timeout = normalize(
        {"exception_info": {"exception_type": "AgentTimeoutError"}}, scheduled, task
    )
    assert timeout["status"] == "agent_timeout" and timeout["correct"] is False
    infrastructure = normalize(
        {"exception_info": {"exception_type": "EnvironmentStartError"}}, scheduled, task
    )
    assert infrastructure["status"] == "infrastructure_error" and infrastructure["correct"] is None
    reported_failure = normalize(
        {
            "agent_result": {"metadata": {"agent_failed": True}},
            "verifier_result": {"rewards": {"reward": 1}},
        },
        scheduled,
        task,
    )
    assert reported_failure["status"] == "agent_error"
    assert reported_failure["correct"] is False


def test_prices_require_complete_accounting_and_partial_results_have_no_success_rate():
    row = _row(True)
    price = Price(input=3, cached_input=0.3, output=15, source="test rate card", as_of="2026-09-05")
    assert price_row(row, price) == pytest.approx(0.01023)
    assert price_row({**row, "cached_input_tokens": None}, price) is None
    assert price_row({**row, "cached_input_tokens": 2_000}, price) is None
    assert price_row({**row, "metadata": {"cache_write_tokens": 1}}, price) is None
    assert price_row({**row, "metadata": {}}, price) is None
    report = summarize([row, _row(None)], expected=3)
    assert report["missing_trials"] == 1
    assert report["success_rate"] is None
    assert report["metrics"]["input_tokens"]["total"] is None
    assert report["cost_per_solved_task_usd"] is None


def test_session_efficiency_metrics_preserve_unknowns_and_include_failed_attempts():
    task = {"sha256": "task-hash", "split": "development", "origin": "authored"}
    scheduled = {"task": "one", "attempt": 0, "agent": "ava"}
    raw = {
        "agent_result": {"metadata": {"session": {
            "tool_calls": {"read": 4, "bash": 2}, "tool_errors": 1,
            "model_attempts": 5, "compactions": 0,
        }}},
        "verifier_result": {"rewards": {"reward": 0}},
    }
    failed = normalize(raw, scheduled, task)
    assert failed["tool_calls"] == 6 and failed["correct"] is False
    summary = summarize([failed], 1)
    assert summary["metrics"]["tool_calls"]["total"] == 6
    assert summary["metrics"]["compactions"]["total"] == 0
    assert _row()["tool_calls"] is None
    assert summarize([failed, _row()], 2)["metrics"]["tool_calls"]["total"] is None


def _write_run(path: Path, frozen: dict, outcomes: list[bool]) -> list[dict]:
    path.mkdir()
    (path / "experiment.json").write_text(json.dumps(frozen))
    rows = [
        _row(correct, task=task) for task, correct in zip(frozen["tasks"], outcomes, strict=True)
    ]
    _write_rows(path, rows)
    return rows


def _write_rows(path: Path, rows: list[dict]) -> None:
    (path / "trials.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))


def test_compare_requires_complete_unique_samples_matching_frozen_tasks(
    tmp_path: Path, monkeypatch
):
    _suite(tmp_path, monkeypatch)
    frozen = plan(_experiment())
    frozen["adapter_sha256"] = "adapter-hash"
    frozen["agent_inputs"] = {"ava": {"wheel_sha256": "wheel-hash"}}
    baseline, candidate = tmp_path / "baseline", tmp_path / "candidate"
    left = _write_run(baseline, frozen, [False, False])
    right = _write_run(candidate, frozen, [True, False])
    report = compare_runs(baseline, candidate, "ava", "ava", "version")
    assert report["success_rate_delta"] == 0.5
    assert report["improvements"] == [{"task": "one", "attempt": 0}]
    assert report["regressions"] == []
    for invalid in (right[:1], [*right, right[0]], [right[0], {**right[1], "correct": None}]):
        _write_rows(candidate, invalid)
        with pytest.raises(ValueError):
            compare_runs(baseline, candidate, "ava", "ava", "version")
    # Equal row hashes do not suffice: both still must match the frozen task manifest.
    left[0]["task_sha256"] = right[0]["task_sha256"] = "stale-task-hash"
    _write_rows(baseline, left)
    _write_rows(candidate, right)
    with pytest.raises(ValueError):
        compare_runs(baseline, candidate, "ava", "ava", "version")
