"""Derived tasks preserve graders and reject mutable or changed benchmark inputs."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from eval.benchmark import tree_hash
from eval.integrations.swebench_pro import prepare


def fixture(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "upstream"
    (source / "environment").mkdir(parents=True)
    (source / "tests").mkdir()
    (source / "solution").mkdir()
    (source / "environment/Dockerfile").write_text("FROM jefzda/sweap-images:fixture\nWORKDIR /app\n")
    (source / "tests/test.sh").write_text("#!/bin/bash\nprintf 'upstream grader'\n")
    (source / "solution/solve.sh").write_text("#!/bin/bash\nprintf 'reference'\n")
    (source / "instruction.md").write_text("Fix the project.\n")
    (source / "task.toml").write_text(
        'schema_version = "1.1"\n[task]\nname = "test/fixture"\n'
        '[verifier]\ntimeout_sec = 3000\n[environment]\ncpus = 1\nmemory_mb = 4096\n'
    )
    suite = tmp_path / "suite.json"
    suite.write_text(json.dumps({"name": "fixture", "tasks": [
        {"id": "fixture", "path": str(source), "sha256": tree_hash(source)},
    ]}))
    return source, suite


def test_preparation_preserves_upstream_and_separates_solution_history(tmp_path):
    source, suite = fixture(tmp_path)
    before = tree_hash(source)
    digest = "sha256:" + "a" * 64
    result = prepare(suite, tmp_path / "prepared", {"jefzda/sweap-images:fixture": digest})
    task = Path(json.loads(result.read_text())["tasks"][0]["path"])
    assert tree_hash(source) == before
    for path in ("instruction.md", "solution/solve.sh"):
        assert (task / path).read_bytes() == (source / path).read_bytes()
    assert (task / "tests/upstream-test.sh").read_bytes() == (source / "tests/test.sh").read_bytes()
    agent = (task / "environment/Dockerfile").read_text()
    verifier = (task / "tests/Dockerfile").read_text()
    assert digest in agent and digest in verifier
    assert "rm -rf .git" in agent and "rm -rf .git" not in verifier
    assert "COPY . /tests/" not in agent and "COPY . /tests/" in verifier
    config = tomllib.loads((task / "task.toml").read_text())
    assert config["verifier"]["environment_mode"] == "separate"
    assert config["verifier"]["environment"]["memory_mb"] == 4096
    assert config["verifier"]["timeout_sec"] == 3000


def test_preparation_rejects_unpinned_images_and_changed_sources_before_writing(tmp_path):
    source, suite = fixture(tmp_path)
    output = tmp_path / "prepared"
    with pytest.raises(ValueError, match="immutable image"):
        prepare(suite, output, {})
    assert not output.exists()
    (source / "tests/test.sh").write_text("different grader")
    with pytest.raises(ValueError, match="changed after suite freeze"):
        prepare(suite, output, {"jefzda/sweap-images:fixture": "sha256:" + "a" * 64})
    assert not output.exists()
