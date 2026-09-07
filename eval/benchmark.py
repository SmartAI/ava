"""Frozen task experiments through Harbor, with paired outcomes and honest missing metrics."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import random
import re
import shutil
import statistics
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ROOT = Path(__file__).resolve().parents[1]
HARBOR_VERSION = "0.22.0"
MAX_JSON_BYTES = 16_000_000


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class AgentSpec(StrictModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    kind: Literal["ava", "pi", "oracle", "nop", "codex", "claude-code"]
    model: str | None = None
    effort: str = "off"
    compaction: bool | None = None
    wheel: str | None = None
    system_prompt: str | None = None
    version: str | None = None

    @model_validator(mode="after")
    def check_model(self) -> AgentSpec:
        if self.kind in ("ava", "pi"):
            if self.compaction is None:
                self.compaction = False
        elif self.compaction is not None:
            raise ValueError(
                "compaction override is implemented only for Ava and Pi; product defaults are native"
            )
        if self.kind in ("ava", "pi") and self.version is not None:
            raise ValueError(
                "Ava/Pi versions come from the frozen wheel/lockfile, not a version override"
            )
        if self.kind in ("oracle", "nop"):
            if self.model is not None:
                raise ValueError("controls must not specify a model")
        elif (
            not self.model
            or "/" not in self.model
            or any(
                value in self.model.lower() for value in ("your-", "<", ">", "latest", "model-id")
            )
        ):
            raise ValueError("use provider/concrete-model-id, not a placeholder or latest alias")
        elif not all(self.model.split("/", 1)):
            raise ValueError("model provider and ID must both be nonempty")
        if self.kind != "ava" and (self.wheel or self.system_prompt):
            raise ValueError("wheel and system_prompt are Ava candidate inputs")
        if self.kind in ("codex", "claude-code") and not self.version:
            raise ValueError("product baselines require an explicit CLI version")
        if self.version and self.version in ("latest", "main", "HEAD"):
            raise ValueError("pin a released agent version")
        if self.kind in ("codex", "claude-code") and self.effort == "off":
            raise ValueError("product baselines require their native explicit effort (e.g. low)")
        return self


class Price(StrictModel):
    input: float = Field(ge=0)
    cached_input: float = Field(ge=0)
    output: float = Field(ge=0)
    source: str = Field(min_length=1)
    as_of: str = Field(min_length=1)


class Experiment(StrictModel):
    schema_version: Literal[1] = 1
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    suite: str
    agents: list[AgentSpec] = Field(min_length=1)
    repetitions: int = Field(default=3, ge=1, le=100)
    timeout_seconds: int = Field(default=180, ge=1, le=7200)
    schedule_seed: int = 42
    max_trials: int = Field(default=30, ge=1, le=10000)
    pricing: dict[str, Price] = Field(default_factory=dict)
    stop_after_estimated_usd: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def check_agents(self) -> Experiment:
        if len({agent.id for agent in self.agents}) != len(self.agents):
            raise ValueError("agent IDs must be unique")
        if self.stop_after_estimated_usd is not None:
            for agent in self.agents:
                if agent.model and not agent.model.startswith("mock/"):
                    if agent.model not in self.pricing:
                        raise ValueError("a spending stop requires prices for each live model")
        return self


def read_json(path: Path) -> Any:
    if path.stat().st_size > MAX_JSON_BYTES:
        raise ValueError(f"JSON exceeds {MAX_JSON_BYTES} bytes: {path.name}")
    return json.loads(path.read_text())


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: Any) -> None:
    # Caller owns the fresh output directory; evidence files themselves are exclusive.
    with path.open("x") as stream:
        stream.write(json.dumps(value, indent=2, allow_nan=False) + "\n")


def tree_hash(path: Path) -> str:
    files = []
    for file in sorted(path.rglob("*")):
        if file.is_symlink():
            raise ValueError(f"task inputs must not contain symlinks: {file}")
        if file.is_file() and not any(p in (".git", "__pycache__") for p in file.parts):
            files.append((str(file.relative_to(path)), file_hash(file)))
    if not files:
        raise ValueError(f"empty task directory: {path}")
    return digest(files)


def plan(config: Experiment) -> dict[str, Any]:
    """All paths in experiment/suite files are repository-relative or absolute."""
    suite_path = (ROOT / config.suite).resolve()
    suite = read_json(suite_path)
    tasks = []
    for item in suite["tasks"]:
        if not isinstance(item.get("id"), str) or not re.fullmatch(
            r"[a-z0-9][a-z0-9-]{0,63}", item["id"]
        ):
            raise ValueError("task IDs must be safe lowercase names, never paths")
        if item.get("split") not in ("development", "validation", "test"):
            raise ValueError("every task needs an explicit development/validation/test split")
        path = (ROOT / item["path"]).resolve()
        for required in ("instruction.md", "task.toml", "tests/test.sh"):
            if not (path / required).is_file():
                raise ValueError(f"not a materialized Harbor task: {path / required}")
        if not item.get("origin") and not suite.get("upstream"):
            raise ValueError("task provenance must be explicit")
        tasks.append(
            {
                **item,
                "path": str(path),
                "sha256": tree_hash(path),
                "origin": item.get("origin", "public"),
            }
        )
    if not tasks or len({task["id"] for task in tasks}) != len(tasks):
        raise ValueError("suite requires nonempty, unique task IDs")
    schedule: list[dict[str, Any]] = []
    rng = random.Random(config.schedule_seed)
    pairs = [(task["id"], attempt) for attempt in range(config.repetitions) for task in tasks]
    rng.shuffle(pairs)
    for task, attempt in pairs:
        agents = list(config.agents)
        rng.shuffle(agents)
        schedule.extend({"task": task, "attempt": attempt, "agent": a.id} for a in agents)
    if len(schedule) > config.max_trials:
        raise ValueError(
            f"{len(schedule)} trials exceeds configured max_trials={config.max_trials}"
        )
    return {
        "schema_version": 1,
        "experiment": config.model_dump(),
        "suite_name": suite["name"],
        "suite_sha256": file_hash(suite_path),
        "tasks": tasks,
        "task_set_sha256": digest([{k: t[k] for k in ("id", "sha256", "split")} for t in tasks]),
        "schedule": schedule,
        "harbor_version": HARBOR_VERSION,
        "evaluator_sha256": file_hash(Path(__file__)),
        "host": {"system": platform.system(), "machine": platform.machine()},
        "note": "Schedule seed controls trial order, not provider determinism. Fresh environments; no trajectory replay.",
    }


def agent_config(spec: AgentSpec, inputs: Path, timeout: int) -> dict[str, Any]:
    value: dict[str, Any] = {"model_name": spec.model, "override_timeout_sec": timeout}
    if spec.kind in ("ava", "pi"):
        module = "harbor_agent:AvaAgent" if spec.kind == "ava" else "pi_agent:PiAgent"
        value["import_path"] = f"eval.integrations.{module}"
        kwargs: dict[str, Any] = {"effort": spec.effort, "compaction": spec.compaction}
        if spec.kind == "ava":
            kwargs["wheel_path"] = str(inputs / spec.id / "ava-0.1.0-py3-none-any.whl")
            if spec.system_prompt:
                kwargs["system_prompt_path"] = str(inputs / spec.id / "system.md")
        value["kwargs"] = kwargs
    else:
        value["name"] = spec.kind
        if spec.kind in ("codex", "claude-code"):
            value["kwargs"] = {"version": spec.version, "reasoning_effort": spec.effort}
    return value


def seconds(phase: dict[str, Any] | None) -> float | None:
    if not phase or not phase.get("started_at") or not phase.get("finished_at"):
        return None
    return max(
        0.0,
        (
            datetime.fromisoformat(phase["finished_at"])
            - datetime.fromisoformat(phase["started_at"])
        ).total_seconds(),
    )


def normalize(
    raw: dict[str, Any], scheduled: dict[str, Any], task: dict[str, Any]
) -> dict[str, Any]:
    """Keep execution errors, missing grades and failed deliverables distinguishable."""
    context = raw.get("agent_result") or {}
    exception = raw.get("exception_info")
    reward = ((raw.get("verifier_result") or {}).get("rewards") or {}).get("reward")
    if exception and exception["exception_type"] == "AgentTimeoutError":
        status, correct = "agent_timeout", False
    elif (context.get("metadata") or {}).get("agent_failed"):
        status, correct = "agent_error", False
    elif exception:
        if exception["exception_type"] == "AgentTimeoutError":
            status, correct = "agent_timeout", False
        else:
            status, correct = "infrastructure_error", None
    elif isinstance(reward, (float, int)) and not isinstance(reward, bool) and reward in (0, 1):
        status, correct = ("passed", True) if reward == 1 else ("failed", False)
    else:
        status, correct = "ungraded", None
    session = (context.get("metadata") or {}).get("session") or {}
    calls = session.get("tool_calls")
    tool_calls = (
        sum(calls.values())
        if isinstance(calls, dict) and all(type(v) is int and v >= 0 for v in calls.values())
        else None
    )
    return {
        **scheduled,
        "task_sha256": task["sha256"],
        "split": task["split"],
        "origin": task["origin"],
        "correct": correct,
        "status": status,
        "reward": reward,
        "exception_type": exception["exception_type"] if exception else None,
        "agent_info": raw.get("agent_info"),
        "agent_seconds": seconds(raw.get("agent_execution")),
        "environment_seconds": seconds(raw.get("environment_setup")),
        "agent_setup_seconds": seconds(raw.get("agent_setup")),
        "total_seconds": seconds(raw),
        "input_tokens": context.get("n_input_tokens"),
        "cached_input_tokens": context.get("n_cache_tokens"),
        "output_tokens": context.get("n_output_tokens"),
        "reported_cost_usd": context.get("cost_usd"),
        "estimated_cost_usd": None,
        "tool_calls": tool_calls,
        "tool_errors": session.get("tool_errors"),
        "model_attempts": session.get("model_attempts"),
        "compactions": session.get("compactions"),
        "metadata": context.get("metadata"),
        "harbor_task_checksum": raw.get("task_checksum"),
    }


def verify_identity(row: dict[str, Any], spec: AgentSpec, inputs: dict[str, str]) -> None:
    info = row.get("agent_info") or {}
    metadata = row.get("metadata") or {}
    if spec.model:
        provider, model = spec.model.split("/", 1)
        actual = info.get("model_info") or {}
        if (actual.get("provider"), actual.get("name")) != (provider, model):
            raise ValueError("recorded agent model differs from the experiment")
    if spec.kind == "ava":
        if metadata.get("wheel_sha256") != inputs.get("wheel_sha256") or not metadata.get(
            "wheel_sha256"
        ):
            raise ValueError("Ava's recorded wheel does not match the frozen candidate")
        if metadata.get("system_prompt_sha256") != inputs.get("system_prompt_sha256"):
            raise ValueError("Ava's recorded prompt does not match the frozen candidate")
        session = metadata.get("session") or {}
        if spec.model and (session.get("provider"), session.get("model")) != tuple(
            spec.model.split("/", 1)
        ):
            raise ValueError("Ava's session selected a different model")
    if spec.version and info.get("version") != spec.version:
        raise ValueError("recorded agent version differs from the pinned version")


def price_row(row: dict[str, Any], price: Price | None) -> float | None:
    if price is None:
        return None
    values = [row[k] for k in ("input_tokens", "cached_input_tokens", "output_tokens")]
    if any(
        not isinstance(v, (float, int)) or isinstance(v, bool) or not math.isfinite(v) or v < 0
        for v in values
    ):
        return None
    total, cached, output = values
    if cached > total:
        return None
    # Cache creation can have separate provider rates. Do not pretend a three-rate table covers it.
    if (row.get("metadata") or {}).get("cache_write_tokens") != 0:
        return None
    return float(
        ((total - cached) * price.input + cached * price.cached_input + output * price.output)
        / 1_000_000
    )


def summarize(rows: list[dict[str, Any]], expected: int) -> dict[str, Any]:
    successes = sum(row["correct"] is True for row in rows)
    missing = expected - len(rows)
    graded = sum(row["correct"] is not None for row in rows)
    values: dict[str, Any] = {}
    for metric in (
        "agent_seconds",
        "total_seconds",
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "estimated_cost_usd",
        "reported_cost_usd",
        "tool_calls",
        "tool_errors",
        "model_attempts",
        "compactions",
    ):
        observed = [row[metric] for row in rows if row.get(metric) is not None]
        values[metric] = {
            "observed_trials": len(observed),
            "mean": statistics.mean(observed) if observed else None,
            "total": sum(observed) if len(observed) == expected else None,
        }
    cost = values["estimated_cost_usd"]["total"]
    return {
        "expected_trials": expected,
        "completed_trials": len(rows),
        "missing_trials": missing,
        "solved": successes,
        "graded_trials": graded,
        "ungraded_or_infrastructure": len(rows) - graded,
        "success_rate": successes / expected if graded == expected else None,
        "observed_solved_fraction": successes / expected,
        "cost_per_solved_task_usd": cost / successes if cost is not None and successes else None,
        "metrics": values,
    }


def run(config: Experiment, output: Path, harbor: Path) -> dict[str, Any]:
    frozen = plan(config)
    output = output.resolve()
    harbor = harbor.absolute()
    # Verify the optional runtime before producing an experiment or touching Docker.
    version = subprocess.run([str(harbor), "--version"], capture_output=True, text=True, check=True)
    if HARBOR_VERSION not in version.stdout:
        raise ValueError(f"expected harbor {HARBOR_VERSION}; found {version.stdout.strip()}")
    output.mkdir(parents=True, exist_ok=False)
    inputs = output / "inputs"
    inputs.mkdir()
    agents = {a.id: a for a in config.agents}
    fingerprints: dict[str, dict[str, str]] = {}
    for agent in config.agents:
        folder = inputs / agent.id
        folder.mkdir()
        fingerprints[agent.id] = {}
        if agent.kind == "ava":
            wheel = ROOT / (agent.wheel or "eval/cache/wheels/ava-0.1.0-py3-none-any.whl")
            destination = folder / "ava-0.1.0-py3-none-any.whl"
            shutil.copyfile(wheel, destination)
            fingerprints[agent.id]["wheel_sha256"] = file_hash(destination)
            if agent.system_prompt:
                shutil.copyfile(ROOT / agent.system_prompt, folder / "system.md")
                fingerprints[agent.id]["system_prompt_sha256"] = file_hash(folder / "system.md")
    tasks = {t["id"]: t for t in frozen["tasks"]}
    for task in tasks.values():
        dest = inputs / "tasks" / task["id"]
        shutil.copytree(task["path"], dest, ignore=shutil.ignore_patterns(".git", "__pycache__"))
        if tree_hash(dest) != task["sha256"]:
            raise ValueError("task changed during snapshot")
    frozen["agent_inputs"] = fingerprints
    frozen["created_at"] = datetime.now(UTC).isoformat()
    # Run the frozen adapters too, so later edits to this checkout cannot change an active run.
    runner = inputs / "runner"
    package = runner / "eval"
    package.mkdir(parents=True)
    shutil.copyfile(ROOT / "eval/__init__.py", package / "__init__.py")
    shutil.copyfile(ROOT / "eval/constraints.txt", package / "constraints.txt")
    shutil.copytree(
        ROOT / "eval/integrations",
        package / "integrations",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    frozen["adapter_sha256"] = tree_hash(package)
    write_json(output / "experiment.json", frozen)
    rows: list[dict[str, Any]] = []
    stop_reason = None
    env = {**os.environ, "PYTHONPATH": str(runner), "HARBOR_TELEMETRY_ENABLED": "false"}
    for index, scheduled in enumerate(frozen["schedule"]):
        spec = agents[scheduled["agent"]]
        task = tasks[scheduled["task"]]
        name = f"trial-{index:04d}"
        job = {
            "job_name": name,
            "jobs_dir": str(output / "jobs"),
            "n_attempts": 1,
            "n_concurrent_trials": 1,
            "quiet": True,
            "retry": {"max_retries": 0},
            "environment": {"type": "docker", "delete": True},
            "agents": [agent_config(spec, inputs, config.timeout_seconds)],
            "tasks": [{"path": str(inputs / "tasks" / task["id"])}],
        }
        job_path = output / f"{name}.json"
        write_json(job_path, job)
        print(
            f"{index + 1}/{len(frozen['schedule'])}: {spec.id} / {task['id']} / repeat {scheduled['attempt'] + 1}",
            flush=True,
        )
        with (output / f"{name}.log").open("x") as log:
            completed = subprocess.run(
                [str(harbor), "run", "--config", str(job_path)],
                cwd=runner,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        files = list((output / "jobs" / name).glob("*/result.json"))
        if len(files) == 1:
            row = normalize(read_json(files[0]), scheduled, task)
            row["artifact_dir"] = str(files[0].parent.relative_to(output))
        else:
            row = normalize(
                {"exception_info": {"exception_type": "MissingTrialResult"}}, scheduled, task
            )
            row["artifact_dir"] = None
        row["harbor_exit_code"] = completed.returncode
        if row["correct"] is not None and row["status"] not in ("agent_error", "agent_timeout"):
            try:
                verify_identity(row, spec, fingerprints[spec.id])
            except ValueError:
                row.update(
                    correct=None,
                    status="infrastructure_error",
                    exception_type="AgentIdentityMismatch",
                )
        row["estimated_cost_usd"] = price_row(row, config.pricing.get(spec.model or ""))
        rows.append(row)
        # Append after every attempt, so interruption preserves completed evidence.
        with (output / "trials.jsonl").open("a") as stream:
            stream.write(json.dumps(row, allow_nan=False) + "\n")
        if row["status"] in ("infrastructure_error", "ungraded"):
            stop_reason = (
                "missing grade or infrastructure failure; fix it before spending on more trials"
            )
        if (
            config.stop_after_estimated_usd is not None
            and spec.model
            and not spec.model.startswith("mock/")
        ):
            if row["estimated_cost_usd"] is None:
                stop_reason = "cost unavailable; spending stop cannot be evaluated"
            elif sum(r["estimated_cost_usd"] or 0 for r in rows) >= config.stop_after_estimated_usd:
                stop_reason = "estimated spending threshold reached (checked between trials, not a hard API cap)"
        if stop_reason:
            break
    expected = len(tasks) * config.repetitions
    report = {
        "schema_version": 1,
        "stop_reason": stop_reason,
        "agents": {
            a.id: summarize([r for r in rows if r["agent"] == a.id], expected)
            for a in config.agents
        },
        "note": "Controls and mock runs are not model-quality scores. Small development subsets do not establish general superiority.",
    }
    write_json(output / "summary.json", report)
    return report


def load_run(path: Path, agent_id: str) -> tuple[dict[str, Any], AgentSpec, list[dict[str, Any]]]:
    frozen = read_json(path / "experiment.json")
    config = Experiment.model_validate(frozen["experiment"])
    agent = next((a for a in config.agents if a.id == agent_id), None)
    if agent is None:
        raise ValueError(f"unknown agent: {agent_id}")
    rows = [
        json.loads(line)
        for line in (path / "trials.jsonl").read_text().splitlines()
        if line.strip()
    ]
    selected = [row for row in rows if row["agent"] == agent_id]
    keys = [(row["task"], row["attempt"]) for row in selected]
    expected = {(t["id"], n) for t in frozen["tasks"] for n in range(config.repetitions)}
    if len(keys) != len(set(keys)) or set(keys) != expected:
        raise ValueError("comparison requires all scheduled attempts, with no duplicates")
    tasks = {t["id"]: t for t in frozen["tasks"]}
    for row in selected:
        task = tasks[row["task"]]
        if row["task_sha256"] != task["sha256"] or row["split"] != task["split"]:
            raise ValueError("trial does not match frozen task contents/split")
        if row["correct"] is not None and type(row["correct"]) is not bool:
            raise ValueError("correct must be boolean or null")
        if row["correct"] is True and row["status"] != "passed":
            raise ValueError("passing grade conflicts with trial status")
        if row["correct"] is False and row["status"] not in (
            "failed",
            "agent_error",
            "agent_timeout",
        ):
            raise ValueError("failed grade conflicts with trial status")
        if row["status"] not in ("agent_error", "agent_timeout"):
            verify_identity(row, agent, frozen.get("agent_inputs", {}).get(agent_id, {}))
    if any(row["correct"] is None for row in selected):
        raise ValueError("ungraded/infrastructure trials prevent a complete comparison")
    return frozen, agent, selected


def compare_runs(
    baseline: Path, candidate: Path, baseline_id: str, candidate_id: str, mode: str
) -> dict[str, Any]:
    left, a, lrows = load_run(baseline, baseline_id)
    right, b, rrows = load_run(candidate, candidate_id)
    for field in (
        "task_set_sha256",
        "harbor_version",
        "evaluator_sha256",
        "adapter_sha256",
        "host",
    ):
        if left[field] != right[field]:
            raise ValueError(f"comparison cannot align {field}")
    for field in ("timeout_seconds", "repetitions", "pricing"):
        if left["experiment"][field] != right["experiment"][field]:
            raise ValueError(f"comparison cannot align {field}")
    if mode not in ("version", "harness", "product"):
        raise ValueError("comparison mode must be version, harness, or product")
    if mode != "product" and (a.model, a.effort) != (b.model, b.effort):
        raise ValueError(
            "same-model comparison requires identical model and effort; use product otherwise"
        )
    if mode == "version" and a.kind != b.kind:
        raise ValueError("version comparison requires the same agent kind")
    if a.kind in ("oracle", "nop") or b.kind in ("oracle", "nop"):
        raise ValueError("reference/negative controls are not harness baselines")
    if any((row.get("metadata") or {}).get("mode") == "smoke" for row in lrows + rrows):
        raise ValueError("mock smoke results are not agent capability comparisons")
    lmap = {(r["task"], r["attempt"]): r for r in lrows}
    rmap = {(r["task"], r["attempt"]): r for r in rrows}
    if lmap.keys() != rmap.keys():
        raise ValueError("attempt identities differ")
    regressions: list[dict[str, Any]] = []
    improvements: list[dict[str, Any]] = []
    for key in sorted(lmap):
        if lmap[key]["task_sha256"] != rmap[key]["task_sha256"]:
            raise ValueError("task contents changed")
        if lmap[key]["correct"] != rmap[key]["correct"]:
            (improvements if rmap[key]["correct"] else regressions).append(
                {"task": key[0], "attempt": key[1]}
            )
    # Task is the resampling unit: repeated attempts within a task aren't independent tasks.
    task_ids = sorted({r["task"] for r in lrows})
    deltas = [
        statistics.mean(int(r["correct"]) for r in rrows if r["task"] == t)
        - statistics.mean(int(r["correct"]) for r in lrows if r["task"] == t)
        for t in task_ids
    ]
    interval = None
    if len(deltas) >= 2:
        rng = random.Random(0)
        samples = sorted(statistics.mean(rng.choices(deltas, k=len(deltas))) for _ in range(2000))
        interval = [samples[49], samples[1949]]
    return {
        "schema_version": 1,
        "mode": mode,
        "baseline": a.model_dump(),
        "candidate": b.model_dump(),
        "baseline_summary": summarize(lrows, len(lrows)),
        "candidate_summary": summarize(rrows, len(rrows)),
        "success_rate_delta": statistics.mean(deltas),
        "task_bootstrap_95_percent_interval": interval,
        "regressions": regressions,
        "improvements": improvements,
        "note": "Descriptive paired results. A small development sample or degenerate bootstrap interval does not establish significance. Product comparisons include model and configuration differences; same model does not guarantee identical provider semantics.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("plan", "run"):
        p = sub.add_parser(command)
        p.add_argument("experiment", type=Path)
        if command == "run":
            p.add_argument("--output", type=Path, required=True)
            p.add_argument("--harbor", type=Path, default=Path(".venv-eval/bin/harbor"))
    p = sub.add_parser("compare")
    p.add_argument("baseline", type=Path)
    p.add_argument("candidate", type=Path)
    p.add_argument("--baseline-agent", required=True)
    p.add_argument("--candidate-agent", required=True)
    p.add_argument("--mode", choices=("version", "harness", "product"), required=True)
    p.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "compare":
            result = compare_runs(
                args.baseline, args.candidate, args.baseline_agent, args.candidate_agent, args.mode
            )
            if args.output:
                write_json(args.output, result)
            failed = bool(result["regressions"])
        else:
            config = Experiment.model_validate(read_json(args.experiment))
            result = (
                plan(config) if args.command == "plan" else run(config, args.output, args.harbor)
            )
            failed = bool(result.get("stop_reason"))
        print(json.dumps(result, indent=2, allow_nan=False))
        return int(failed)
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        print(f"benchmark: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
