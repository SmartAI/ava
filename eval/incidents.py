"""Materialize pinned Ava history as ordinary Harbor tasks, without exposing fixes."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from eval.benchmark import ROOT, file_hash, read_json, tree_hash, write_json

IMAGE = "python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7"


def git(*args: str) -> bytes:
    return subprocess.run(
        ["git", *args], cwd=ROOT, check=True, capture_output=True, timeout=30,
    ).stdout


def historical_files(commit: str, paths: list[str], destination: Path) -> None:
    records = git("ls-tree", "-rz", commit, "--", *paths).split(b"\0")
    for record in filter(None, records):
        meta, encoded_path = record.split(b"\t", 1)
        mode, kind, blob = meta.decode().split()
        path = Path(encoded_path.decode())
        if kind != "blob" or mode not in ("100644", "100755"):
            raise ValueError(f"only regular historical files are supported: {path}")
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("unsafe historical path")
        target = destination / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(git("cat-file", "blob", blob))
        target.chmod(0o755 if mode == "100755" else 0o644)


def prepare(manifest: Path, output: Path) -> dict[str, Any]:
    catalog = read_json(manifest)
    specs = catalog["tasks"]
    ids = [spec["id"] for spec in specs]
    if not ids or len(ids) != len(set(ids)):
        raise ValueError("nonempty unique incident IDs are required")
    families: dict[str, str] = {}
    for spec in specs:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", spec["id"]):
            raise ValueError("unsafe incident ID")
        if spec["split"] not in {"development", "validation", "test"}:
            raise ValueError("explicit split required")
        if families.setdefault(spec["family"], spec["split"]) != spec["split"]:
            raise ValueError("related incident families must stay in the same split")
        for field in ("base_commit", "fix_commit"):
            if not re.fullmatch(r"[0-9a-f]{40}", spec[field]):
                raise ValueError("pin full commit hashes")
            git("cat-file", "-e", f"{spec[field]}^{{commit}}")
        for name in spec["source_files"] + spec["test_files"]:
            if Path(name).is_absolute() or ".." in Path(name).parts:
                raise ValueError("unsafe source/test path")
        if any(not name.startswith("src/") for name in spec["source_files"]):
            raise ValueError("reference solutions may only replace source files")
        if any(not name.startswith("tests/") for name in spec["test_files"]):
            raise ValueError("graders must come from historical tests")
        for node in spec.get("exclude_tests", []):
            if "::" not in node or node.split("::", 1)[0] not in spec["test_files"]:
                raise ValueError("excluded test must belong to a selected historical test module")
    # Public history can leak a validation answer even without shared family tags.
    # Reject known validation repairs already present in a development snapshot.
    for reserved in (spec for spec in specs if spec["split"] != "development"):
        for development in (spec for spec in specs if spec["split"] == "development"):
            ancestry = subprocess.run(
                ["git", "merge-base", "--is-ancestor", reserved["fix_commit"], development["base_commit"]],
                cwd=ROOT, capture_output=True, timeout=30,
            )
            if ancestry.returncode == 0:
                raise ValueError("validation repair is already in a development snapshot")
            if ancestry.returncode != 1:
                raise ValueError("could not verify incident split ancestry")
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    tasks = []
    for spec in specs:
        task = output / "tasks" / spec["id"]
        workspace = task / "environment/workspace"
        # No Git object store, future commits, current checkout, or new eval code.
        historical_files(
            spec["base_commit"], ["src", "tests", "pyproject.toml", "uv.lock", "README.md", "LICENSE"],
            workspace,
        )
        environment = task / "environment"
        shutil.copyfile(ROOT / "eval/incident-constraints.txt", environment / "requirements.txt")
        (environment / "Dockerfile").write_text(
            f"FROM {IMAGE}\nCOPY requirements.txt /tmp/requirements.txt\n"
            "RUN pip install --no-cache-dir --require-hashes -r /tmp/requirements.txt\n"
            "ENV PYTHONPATH=/app/src\nWORKDIR /app\nCOPY workspace/ /app/\n"
        )
        (task / "instruction.md").write_text(
            spec["instruction"] + "\n\nThe repository is in /app. Use `python -m pytest` "
            "with the preinstalled dependencies. No external service is needed to test the fix.\n"
        )
        (task / "task.toml").write_text(
            'schema_version = "1.4"\n\n[metadata]\n'
            f'origin = "repository-history"\nsplit = "{spec["split"]}"\n'
            '[agent]\ntimeout_sec = 300.0\n[verifier]\ntimeout_sec = 120.0\n'
            '[environment]\nbuild_timeout_sec = 600.0\ncpus = 1\nmemory_mb = 2048\nstorage_mb = 4096\n'
        )
        checks = task / "tests/checks"
        historical_files(spec["base_commit"], ["tests"], checks)
        historical_files(spec["fix_commit"], spec["test_files"], checks)
        (checks / "pytest.ini").write_text(
            "[pytest]\nasyncio_mode = auto\ntimeout = 20\n"
            "asyncio_default_fixture_loop_scope = function\n"
        )
        (task / "tests/grade.py").write_text(GRADER)
        (task / "tests/test.sh").write_text(
            '#!/bin/sh\nset -eu\n'
            'test_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)\n'
            'exec python3 "$test_dir/grade.py"\n'
        )
        write_json(task / "tests/modules.json", [
            *spec["test_files"],
            *(f"--deselect={node}" for node in spec.get("exclude_tests", [])),
        ])
        historical_files(spec["fix_commit"], spec["source_files"], task / "solution/fixed")
        (task / "solution/solve.sh").write_text(
            '#!/bin/sh\nset -eu\n'
            'solution_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)\n'
            'cp -R "$solution_dir/fixed/src/." "${AVA_TASK_WORKSPACE:-/app}/src/"\n'
        )
        write_json(task / "provenance.json", {
            **spec, "instruction_origin": "reconstructed-from-public-fix",
            "manifest_sha256": file_hash(manifest),
            "limitations": "Retrospective public history; not hidden from this author or model pretraining. Not a sealed test set.",
        })
        tasks.append({
            "id": spec["id"], "path": str(task), "split": spec["split"],
            "origin": "repository-history", "family": spec["family"],
            "selection_reason": spec["selection_reason"],
        })
    for split in ("all", "development", "validation", "test"):
        selected = [task for task in tasks if split == "all" or task["split"] == split]
        if not selected:
            continue
        suite = output / f"{split}.json"
        write_json(suite, {
            "schema_version": 1, "name": f"{catalog['name']}-{split}",
            "description": catalog["description"], "tasks": selected,
        })
        write_json(output / f"controls-{split}.json", {
            "schema_version": 1, "name": f"incident-controls-{split}", "suite": str(suite),
            "agents": [{"id": "reference", "kind": "oracle"}, {"id": "unchanged", "kind": "nop"}],
            "repetitions": 1, "timeout_seconds": 300, "max_trials": 2 * len(selected),
        })
    result = {
        "schema_version": 1, "manifest_sha256": file_hash(manifest),
        "materializer_sha256": file_hash(Path(__file__)),
        "constraints_sha256": file_hash(ROOT / "eval/incident-constraints.txt"),
        "tasks": [{**task, "sha256": tree_hash(Path(task["path"]))} for task in tasks],
    }
    write_json(output / "prepared.json", result)
    return result


GRADER = '''"""Run evaluator-owned historical regression tests against the submitted source."""
import json
import os
import subprocess
import sys
from pathlib import Path

directory = Path(__file__).resolve().parent
workspace = Path(os.environ.get("AVA_TASK_WORKSPACE", "/app")).resolve()
reward = Path(os.environ.get("AVA_TASK_REWARD_DIR", "/logs/verifier"))
reward.mkdir(parents=True, exist_ok=True)
(reward / "reward.txt").unlink(missing_ok=True)
checks = directory / "checks"
env = {key: value for key, value in os.environ.items()
       if not key.startswith(("AVA_", "PYTEST_")) and key not in
       {"OPENAI_API_KEY", "ANTHROPIC_API_KEY", "PYTHONPATH"}}
env["PYTHONPATH"] = os.pathsep.join((str(workspace / "src"), str(checks)))
modules = json.loads((directory / "modules.json").read_text())
result = subprocess.run(
    [sys.executable, "-m", "pytest", "-q", "-c", str(checks / "pytest.ini"), *modules],
    cwd=checks, env=env, timeout=110, capture_output=True, text=True,
)
print(result.stdout, end="")
print(result.stderr, end="", file=sys.stderr)
# A broken submission can cause import/syntax errors during collection (exit 2).
# Pytest instead returns 4 when the submitted code breaks conftest imports.
# Reference/no-op controls validate the fixed evaluator and dependencies first.
source_import_error = (
    result.returncode == 4 and "ImportError while loading conftest" in result.stderr
)
if result.returncode not in (0, 1, 2) and not source_import_error:
    raise SystemExit("Verifier infrastructure/configuration error; no correctness grade")
(reward / "reward.txt").write_text("1\\n" if result.returncode == 0 else "0\\n")
'''


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.manifest, args.output), indent=2))


if __name__ == "__main__":
    main()
