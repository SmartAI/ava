"""Generic historical-task boundaries, using only temporary synthetic Git history."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from eval import incidents


@pytest.fixture
def history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    repository = tmp_path / "repository"
    repository.mkdir()

    def git(*args: str) -> str:
        return subprocess.run(
            [
                "git", "-c", "user.name=Synthetic Fixture", "-c",
                "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false",
                "-c", "core.hooksPath=/dev/null", *args,
            ],
            cwd=repository, check=True, capture_output=True, text=True, timeout=10,
        ).stdout.strip()

    git("init", "-q")
    for name, content in {
        "src/example.py": "def increment(value):\n    return value\n",
        "tests/test_example.py": "from example import increment\n\ndef test_zero():\n    assert increment(0) == 1\n",
        "pyproject.toml": "[tool.pytest.ini_options]\n",
        "uv.lock": "# Synthetic history; no package installation is performed.\n",
        "README.md": "Synthetic repository fixture.\n",
        "LICENSE": "Synthetic test fixture.\n",
        "eval/incident-constraints.txt": "# No installation in this synthetic test.\n",
    }.items():
        path = repository / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    git("add", ".")
    git("commit", "-qm", "Create synthetic baseline")
    base = git("rev-parse", "HEAD")
    (repository / "src/example.py").write_text("def increment(value):\n    return value + 1\n")
    with (repository / "tests/test_example.py").open("a") as handle:
        handle.write("\ndef test_positive():\n    assert increment(4) == 5\n")
    git("add", ".")
    git("commit", "-qm", "Repair synthetic function")
    fixed = git("rev-parse", "HEAD")
    (repository / "README.md").write_text("Later synthetic snapshot.\n")
    git("add", ".")
    git("commit", "-qm", "Create later snapshot")
    later = git("rev-parse", "HEAD")
    monkeypatch.setattr(incidents, "ROOT", repository)
    task = {
        "id": "example", "split": "development", "family": "arithmetic",
        "base_commit": base, "fix_commit": fixed,
        "source_files": ["src/example.py"], "test_files": ["tests/test_example.py"],
        "instruction": "Make increment return its input plus one.",
        "selection_reason": "Synthetic materializer contract.",
    }
    manifest = {"schema_version": 1, "name": "synthetic", "description": "Synthetic fixture", "tasks": [task]}
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    return {"manifest": manifest, "path": path, "fixed": fixed, "later": later}


def test_materialized_task_has_independent_grader(history: dict[str, Any], tmp_path: Path):
    pack = tmp_path / "pack"
    incidents.prepare(history["path"], pack)
    task = pack / "tasks/example"
    original = task / "environment/workspace"
    assert not any((original / name).exists() for name in (".git", "eval", "solution"))
    assert "return value + 1" not in (original / "src/example.py").read_text()
    assert "test_positive" not in (original / "tests/test_example.py").read_text()
    for mode, expected in (("unchanged", "0"), ("reference", "1"), ("broken-import", "0")):
        workspace = tmp_path / mode / "workspace"
        shutil.copytree(original, workspace)
        shutil.rmtree(workspace / "tests")
        (workspace / "pyproject.toml").write_text("[tool.pytest.ini_options]\naddopts='--bad-option'\n")
        if mode == "reference":
            subprocess.run(
                ["sh", str(task / "solution/solve.sh")], check=True, capture_output=True,
                env={**os.environ, "AVA_TASK_WORKSPACE": str(workspace)}, timeout=10,
            )
        elif mode == "broken-import":
            (workspace / "src/example.py").write_text("def invalid(:\n")
        reward = tmp_path / mode / "reward"
        result = subprocess.run(
            [sys.executable, str(task / "tests/grade.py")],
            env={**os.environ, "AVA_TASK_WORKSPACE": str(workspace), "AVA_TASK_REWARD_DIR": str(reward)},
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert (reward / "reward.txt").read_text().strip() == expected, result.stdout + result.stderr


def test_families_cannot_cross_splits(history: dict[str, Any], tmp_path: Path):
    manifest = history["manifest"]
    manifest["tasks"].append({**manifest["tasks"][0], "id": "other", "split": "validation"})
    history["path"].write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="same split"):
        incidents.prepare(history["path"], tmp_path / "invalid")
    assert not (tmp_path / "invalid").exists()


def test_validation_answer_cannot_appear_in_development_ancestry(
    history: dict[str, Any], tmp_path: Path,
):
    manifest = history["manifest"]
    validation = {**manifest["tasks"][0], "id": "reserved", "family": "reserved-family", "split": "validation"}
    manifest["tasks"][0].update(base_commit=history["fixed"], fix_commit=history["later"])
    manifest["tasks"].append(validation)
    history["path"].write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="already in a development snapshot"):
        incidents.prepare(history["path"], tmp_path / "leaked")
    assert not (tmp_path / "leaked").exists()


def test_cli_requires_explicit_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sys, "argv", ["incidents", "--output", str(tmp_path / "pack")])
    with pytest.raises(SystemExit) as error:
        incidents.main()
    assert error.value.code == 2
    assert not (tmp_path / "pack").exists()
