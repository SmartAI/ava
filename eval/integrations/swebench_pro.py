"""Prepare SWE-bench Pro tasks with clean agent history and a separate upstream grader.

Requires materialized Harbor tasks and explicitly pinned image digests.
Upstream tasks remain untouched; derived tasks and provenance belong in ignored local storage.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import tomllib
from pathlib import Path

from eval.benchmark import ROOT, file_hash, read_json, tree_hash, write_json

# Preserve the tracked source baseline, including ignored tracked files, without old Git objects.
CLEAN_HISTORY = """
RUN set -eu; cd /app; git ls-files -z > /tmp/benchmark-tracked; \\
    rm -rf .git; git -c init.templateDir= init -q; \\
    git add --force --pathspec-from-file=/tmp/benchmark-tracked --pathspec-file-nul; \\
    git -c user.name=Benchmark -c user.email=benchmark@example.invalid commit -qm baseline; \\
    git update-ref refs/benchmark/base HEAD; rm /tmp/benchmark-tracked
"""

COLLECT_PATCH = (
    "cd /app && git add -N . && "
    "git diff --no-ext-diff --binary refs/benchmark/base -- > /tmp/benchmark-prediction.patch"
)

GRADE_PATCH = """#!/bin/bash
set -euo pipefail
mkdir -p /logs/verifier
cd /app
test -f /tmp/benchmark-prediction.patch
if [ -s /tmp/benchmark-prediction.patch ] && ! git apply /tmp/benchmark-prediction.patch; then
    echo 0 > /logs/verifier/reward.txt
    exit 0
fi
exec bash /tests/upstream-test.sh
"""


def prepare(suite_path: Path, output: Path, image_digests: dict[str, str]) -> Path:
    suite = read_json(suite_path)
    prepared = []
    recipes = []
    # Validate every source before creating an output directory.
    for task in suite["tasks"]:
        source = (ROOT / task["path"]).resolve()
        config = tomllib.loads((source / "task.toml").read_text())
        if config.get("verifier", {}).get("environment") or config.get("artifacts"):
            raise ValueError("Expected unmodified shared-verifier SWE-bench Pro task")
        dockerfile = (source / "environment/Dockerfile").read_text()
        images = re.findall(r"^FROM (\S+)\s*$", dockerfile, re.MULTILINE)
        if len(images) != 1 or not images[0].startswith("jefzda/sweap-images:"):
            raise ValueError("Expected one upstream SWE-bench Pro image")
        image = images[0]
        digest = image_digests.get(image, "")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise ValueError(f"Missing immutable image digest for {image}")
        original_hash = tree_hash(source)
        if task.get("sha256") and task["sha256"] != original_hash:
            raise ValueError("Upstream task changed after suite freeze")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", task["id"]):
            raise ValueError("Unsafe task ID")
        recipes.append((task, source, config, dockerfile, image, digest, original_hash))

    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    for task, source, config, dockerfile, image, digest, original_hash in recipes:
        target = output / "tasks" / task["id"]
        shutil.copytree(source, target)
        # The agent build context never contains tests, solutions, or original Git history.
        pinned = dockerfile.replace(f"FROM {image}", f"FROM docker.io/jefzda/sweap-images@{digest}")
        (target / "environment/Dockerfile").write_text(pinned + CLEAN_HISTORY)
        (target / "tests/Dockerfile").write_text(pinned + "\nCOPY . /tests/\n")
        (target / "tests/test.sh").rename(target / "tests/upstream-test.sh")
        (target / "tests/test.sh").write_text(GRADE_PATCH)
        original = (source / "task.toml").read_text()
        # Insert root-level artifacts before any TOML table.
        first_table = original.index("[")
        original = original[:first_table] + (
            'artifacts = [{source = "/tmp/benchmark-prediction.patch"}]\n\n'
        ) + original[first_table:]
        original = original.replace("[verifier]\n", '[verifier]\nenvironment_mode = "separate"\n', 1)
        env = config["environment"]
        verifier_env = {key: value for key, value in env.items()
                        if key in {"cpus", "memory_mb", "storage_mb", "gpus", "build_timeout_sec"}}
        original += "\n[verifier.environment]\n"
        for key, value in verifier_env.items():
            original += f"{key} = {json.dumps(value)}\n"
        original += '\n[[verifier.collect]]\ncommand = ' + json.dumps(COLLECT_PATCH) + '\n'
        (target / "task.toml").write_text(original)
        provenance = {
            "upstream_id": task.get("upstream_id", source.name),
            "upstream_task_sha256": original_hash,
            "image_tag": image, "image_digest": digest,
            "upstream_grader_sha256": file_hash(source / "tests/test.sh"),
            "preparer_sha256": file_hash(Path(__file__)),
            "changes": ["remove agent Git history", "collect patch", "grade in fresh upstream image"],
        }
        write_json(target / "provenance.json", provenance)
        prepared.append({**task, "path": str(target), "sha256": tree_hash(target)})
    manifest = output / "suite.json"
    write_json(manifest, {
        **suite, "name": suite["name"] + "-isolated", "tasks": prepared,
        "adaptation": "Separate verifier; same upstream instructions, reference solution and grader",
        "source_suite_sha256": file_hash(suite_path),
    })
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("suite", type=Path)
    parser.add_argument("--image-digests", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    print(prepare(args.suite, args.output, read_json(args.image_digests)))


if __name__ == "__main__":
    main()
