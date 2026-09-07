"""Pinned SWE-bench instances, isolated Ava prediction generation, official grading.

Requires eval/requirements.txt and Docker. Gold patches and test patches remain
on the host and are supplied only to the official grader, never to Ava.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shlex
import subprocess
import sys
import tarfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def prepare(manifest_path: Path, cache: Path) -> list[dict]:
    from datasets import load_dataset

    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / f"{hashlib.sha256(manifest_bytes).hexdigest()}.json"
    if path.exists():
        cached = json.loads(path.read_text())
        if not isinstance(cached, list) or any(not isinstance(row, dict) for row in cached):
            raise ValueError("cached dataset must contain a list of objects")
        return [dict(row) for row in cached]
    dataset = load_dataset(
        manifest["dataset"], split=manifest["split"], revision=manifest["revision"]
    )
    ids = {task["id"] for task in manifest["tasks"]}
    rows = [dict(row) for row in dataset if row["instance_id"] in ids]
    if {row["instance_id"] for row in rows} != ids:
        raise ValueError("pinned dataset does not contain every selected instance")
    path.write_text(json.dumps(rows, indent=2) + "\n")
    return rows


def docker_environment() -> dict[str, str]:
    env = dict(os.environ)
    if not env.get("DOCKER_HOST"):
        env["DOCKER_HOST"] = subprocess.run(
            ["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    return env


def upload(container, path: Path, name: str) -> None:
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w") as archive:
        archive.add(path, arcname=name)
    container.put_archive("/tmp", data.getvalue())


def download(container, remote: str, destination: Path) -> None:
    stream, _ = container.get_archive(remote)
    with tarfile.open(fileobj=io.BytesIO(b"".join(stream))) as archive:
        member = archive.extractfile(Path(remote).name)
        if member is None:
            raise ValueError(f"missing artifact: {remote}")
        destination.write_bytes(member.read())


def predict(
    instance: dict, output: Path, provider: str, model: str, timeout: int, *, manifest: Path
) -> Path:
    import docker
    from swebench.harness.test_spec.test_spec import make_test_spec

    env = docker_environment()
    wheel = Path(os.environ.get("AVA_EVAL_WHEEL", ROOT / "cache/wheels/ava-0.1.0-py3-none-any.whl"))
    if not wheel.is_file():
        raise ValueError("build the current Ava wheel first")
    credential = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}.get(provider)
    if credential and not env.get(credential):
        raise ValueError(f"{credential} is required for live evaluation")
    output.mkdir(parents=True, exist_ok=False)
    spec = make_test_spec(instance, namespace="swebench", arch="x86_64")
    client = docker.from_env(environment=env)
    container = None
    manifest_bytes = manifest.read_bytes()
    result = {
        "instance_id": instance["instance_id"],
        "mode": "smoke" if provider == "mock" else "live",
        "base_commit": instance["base_commit"],
        "suite_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "dataset_revision": json.loads(manifest_bytes)["revision"],
        "resources": {"cpus": 2, "memory_gib": 4, "platform": "linux/amd64"},
        "provider": provider,
        "model": model,
        "timeout_seconds": timeout,
        "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
        "status": "infrastructure_error",
        "correct": None,
    }
    started = time.monotonic()
    try:
        image = client.images.pull(spec.instance_image_key, platform="linux/amd64")
        result["image_id"] = image.id
        result["image_digests"] = image.attrs.get("RepoDigests", [])
        container = client.containers.run(
            image.id,
            command="sleep infinity",
            detach=True,
            platform="linux/amd64",
            working_dir="/testbed",
            mem_limit="4g",
            nano_cpus=2_000_000_000,
        )
        # Some upstream images carry permission changes from their build. Restore
        # the task base inside this disposable container before attributing a diff
        # to Ava. Preserve ignored build products needed by the repository tests.
        for reset_command in (
            ["git", "reset", "--hard", instance["base_commit"]],
            ["git", "clean", "-fd"],
        ):
            cleaned = container.exec_run(reset_command, workdir="/testbed")
            if cleaned.exit_code:
                raise RuntimeError("could not restore the benchmark starting checkout")
        baseline = container.exec_run(["git", "status", "--porcelain"], workdir="/testbed")
        if baseline.exit_code or baseline.output.strip():
            raise RuntimeError("benchmark starting checkout is not clean")
        upload(container, wheel, "ava-0.1.0-py3-none-any.whl")
        upload(container, ROOT / "constraints.txt", "ava-constraints.txt")
        upload(container, ROOT / "integrations/install.sh", "ava-install.sh")
        install = subprocess.run(
            ["docker", "exec", container.id, "sh", "/tmp/ava-install.sh"],
            env=env,
            capture_output=True,
            timeout=600,
        )
        (output / "install.log").write_bytes(install.stdout + install.stderr)
        if install.returncode:
            raise RuntimeError("Ava installation failed; see install.log")
        args = ["docker", "exec", "-i", "-e", "AVA_HOME=/tmp/ava-home"]
        if credential:
            args += ["-e", credential]
        if provider == "mock":
            script = output / "mock.txt"
            script.write_text("text Ava benchmark plumbing smoke test.\ndone\n")
            upload(container, script, "ava-mock.txt")
            args += ["-e", "AVA_MOCK_SCRIPT=/tmp/ava-mock.txt"]
        command = shlex.join(
            [
                "/opt/ava-venv/bin/ava",
                "-p",
                "--provider",
                provider,
                "--model",
                model,
                "--no-compact",
                "--session",
                "/tmp/ava-session.jsonl.zst",
                "--record",
                "/tmp/ava-recording.jsonl",
            ]
        )
        args += [
            container.id,
            "bash",
            "-c",
            "source /opt/miniconda3/bin/activate testbed && " + command,
        ]
        try:
            execution = subprocess.run(
                args,
                input=instance["problem_statement"].encode(),
                env=env,
                capture_output=True,
                timeout=timeout,
            )
            (output / "agent.log").write_bytes(execution.stdout + execution.stderr)
            result["status"] = "completed" if execution.returncode == 0 else "agent_error"
            result["agent_exit_code"] = execution.returncode
        except subprocess.TimeoutExpired as error:
            result["status"] = "agent_timeout"
            (output / "agent.log").write_bytes((error.stdout or b"") + (error.stderr or b""))
            raise
        finally:
            for remote, local in (
                ("ava-session.jsonl.zst", "session.jsonl.zst"),
                ("ava-recording.jsonl", "recording.jsonl"),
            ):
                try:
                    download(container, f"/tmp/{remote}", output / local)
                except docker.errors.NotFound:
                    pass
        # Include tracked, staged, committed, and new files against the original base.
        diff = container.exec_run(
            [
                "bash",
                "-c",
                "git add -N -- . && git diff --binary " + shlex.quote(instance["base_commit"]),
            ],
            workdir="/testbed",
        )
        if diff.exit_code:
            raise RuntimeError("could not collect the generated patch")
        prediction = {
            "instance_id": instance["instance_id"],
            "model_name_or_path": f"ava-{provider}-{model}",
            "model_patch": diff.output.decode(),
        }
        path = output / "predictions.jsonl"
        path.write_text(json.dumps(prediction) + "\n")
        return path
    finally:
        result["elapsed_seconds_including_setup"] = time.monotonic() - started
        (output / "run.json").write_text(json.dumps(result, indent=2) + "\n")
        if container:
            container.remove(force=True)
        client.close()


def grade(rows: list[dict], output: Path, predictions: str) -> bool:
    import docker
    from swebench.harness.test_spec.test_spec import make_test_spec

    output.mkdir(parents=True, exist_ok=False)
    dataset = output.resolve() / "grader-dataset.json"
    dataset.write_text(json.dumps(rows) + "\n")
    # The official harness's pull omits platform. Pre-pull its x86 image explicitly
    # so the same grader also runs on Apple Silicon with Docker emulation enabled.
    client = docker.from_env(environment=docker_environment())
    images = {}
    try:
        for row in rows:
            spec = make_test_spec(row, namespace="swebench", arch="x86_64")
            image = client.images.pull(spec.instance_image_key, platform="linux/amd64")
            images[row["instance_id"]] = {
                "id": image.id,
                "digests": image.attrs.get("RepoDigests", []),
            }
    finally:
        client.close()
    (output / "images.json").write_text(json.dumps(images, indent=2) + "\n")
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "swebench.harness.run_evaluation",
            "--dataset_name",
            str(dataset),
            "--predictions_path",
            predictions,
            "--max_workers",
            "1",
            "--run_id",
            "ava-eval",
            "--timeout",
            "900",
            "--namespace",
            "swebench",
            "--report_dir",
            str(output.resolve()),
        ],
        cwd=output.resolve(),
        env=docker_environment(),
        check=False,
    )
    if completed.returncode:
        return False
    reports = [json.loads(path.read_text()) for path in output.glob("*.ava-eval.json")]
    return any(
        isinstance(report, dict) and report.get("resolved_instances") == len(rows)
        for report in reports
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "oracle", "predict", "grade"))
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--cache", type=Path, default=ROOT / "cache/swebench")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--provider", choices=("anthropic", "openai", "mock"), default="mock")
    parser.add_argument("--model", default="smoke-v1")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--predictions", type=Path)
    args = parser.parse_args()
    rows = prepare(args.manifest, args.cache)
    if args.command == "prepare":
        print(json.dumps({"instances": [row["instance_id"] for row in rows]}))
        return 0
    if args.output is None:
        parser.error("--output is required")
    if args.command == "oracle":
        return int(not grade(rows, args.output, "gold"))
    if args.command == "grade":
        if args.predictions is None:
            parser.error("--predictions is required")
        return int(not grade(rows, args.output, str(args.predictions.resolve())))
    if len(rows) != 1:
        parser.error("this initial prediction runner supports one selected instance")
    predict(rows[0], args.output, args.provider, args.model, args.timeout, manifest=args.manifest)
    result = json.loads((args.output / "run.json").read_text())
    return int(result["status"] != "completed")


if __name__ == "__main__":
    raise SystemExit(main())
