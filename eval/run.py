"""Run small deterministic regression cases and compare paired evaluation results."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import platform
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ava.agent import Agent, CompactionOptions
from ava.agent.recording import Recording, replay_recording
from ava.base import AvaError, CancelToken, ErrorKind
from ava.base.cancel import NEVER
from ava.llm import Context, Provider, Selection, StopReason, StreamEvent, StreamSink
from ava.llm.types import Item, Role, make_text_block
from ava.session.inspect import inspect_session
from ava.tool import make_bash_tool, make_edit_tool, make_read_tool, make_write_tool

ROOT = Path(__file__).resolve().parents[1]


class Response(BaseModel):
    model_config = ConfigDict(extra="forbid")
    events: list[StreamEvent] = Field(default_factory=list)
    stop: StopReason = StopReason.end_turn
    error: str | None = None


class Case(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-z0-9-]+$")
    provenance: str
    prompt: str
    files: dict[str, str]
    responses: list[Response] = Field(min_length=1)
    expected_error: str | None = None
    check: str


class FixtureProvider(Provider):
    """Explicit synthetic responses exercise runtime behaviour, not model intelligence."""

    id = "fixture"

    def __init__(self, responses: list[Response]) -> None:
        super().__init__(Selection("fixture", "fixture-v1"))
        self.responses = iter(responses)

    async def stream(
        self,
        context: Context,
        selected: Selection,
        sink: StreamSink,
        cancel: CancelToken = NEVER,
    ) -> StopReason:
        response = next(self.responses, None)
        if response is None:
            raise AvaError(ErrorKind.provider, "fixture exhausted")
        for event in response.events:
            sink(event)
        if response.error:
            raise AvaError(ErrorKind.provider, response.error)
        return response.stop


def fingerprint() -> dict[str, Any]:
    source = hashlib.sha256()
    for path in sorted((ROOT / "src").rglob("*")):
        if path.is_file() and path.suffix in (".py", ".md"):
            source.update(str(path.relative_to(ROOT)).encode())
            source.update(path.read_bytes())
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return {
        "commit": commit,
        "source_sha256": source.hexdigest(),
        "python": platform.python_version(),
    }


async def run_case(case_path: Path, output: Path) -> dict[str, Any]:
    case = Case.model_validate_json(case_path.read_text())
    output.mkdir(parents=True, exist_ok=False)
    output = output.resolve()
    workspace = output / "workspace"
    workspace.mkdir()
    for name, content in case.files.items():
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"invalid fixture path: {name}")
        target = workspace / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    item = Item(role=Role.user, blocks=[make_text_block(case.prompt)])
    provider = FixtureProvider(case.responses)
    options = CompactionOptions(enabled=False)
    recording = Recording(output / "recording.jsonl", provider, item, options)
    started = time.monotonic()
    failure: AvaError | None = None
    try:
        async with Agent.create_at(
            recording.provider(),
            workspace,
            output / "session.jsonl.zst",
            options,
            tools=recording.tools(
                [
                    make_read_tool(workspace),
                    make_write_tool(workspace),
                    make_edit_tool(workspace),
                    make_bash_tool(workspace),
                ]
            ),
            system_prompt="You are Ava. Complete the requested Python change and verify it.",
        ) as agent:
            await agent.followup(item)
            try:
                await agent.drive()
            except AvaError as error:
                failure = error
        recording.finish(failure)
    finally:
        recording.close()
    elapsed = time.monotonic() - started
    # This verifier is Ava-owned, outside the agent workspace and outside the recording.
    check = subprocess.run(
        [sys.executable, "-c", case.check],
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=10,
    )
    (output / "verifier.log").write_text(check.stdout + check.stderr)
    try:
        replay = await replay_recording(output / "recording.jsonl")
    except AvaError as error:
        replay = {"matched": False, "error": str(error)}
    summary = inspect_session(output / "session.jsonl.zst")
    correct = (
        check.returncode == 0
        and (failure.message if failure else None) == case.expected_error
        and summary["tool_errors"] == 0
        and replay["matched"] is True
    )
    result = {
        "schema_version": 1,
        "case_id": case.id,
        "suite": "ava-regressions",
        "mode": "synthetic-runtime",
        "provenance": case.provenance,
        "case_sha256": hashlib.sha256(case_path.read_bytes()).hexdigest(),
        "evaluator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "runtime": fingerprint(),
        "model": "fixture-v1",
        "compaction": False,
        "correct": correct,
        "status": "passed" if correct else "failed",
        "elapsed_seconds": elapsed,
        "metrics": summary,
        "replay": replay,
        "artifacts": {
            "session": "session.jsonl.zst",
            "recording": "recording.jsonl",
            "verifier": "verifier.log",
        },
    }
    (output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def compare(baseline: Path, candidate: Path) -> dict[str, Any]:
    def read(path: Path) -> dict[str, dict[str, Any]]:
        rows = [json.loads(file.read_text()) for file in sorted(path.glob("*/result.json"))]
        indexed = {row["case_id"]: row for row in rows}
        if not rows or len(indexed) != len(rows):
            raise ValueError("comparison requires nonempty results with unique case IDs")
        return indexed

    left, right = read(baseline), read(candidate)
    if left.keys() != right.keys():
        raise ValueError("comparison requires exactly the same case IDs")
    for key in left:
        if left[key].get("correct") is None or right[key].get("correct") is None:
            raise ValueError(f"{key}: ungraded/infrastructure result prevents a matched comparison")
        for field in ("case_sha256", "evaluator_sha256", "suite", "mode", "model", "compaction"):
            if left[key][field] != right[key][field]:
                raise ValueError(
                    f"{key}: {field} differs; this is not a matched runtime comparison"
                )
    return {
        "cases": len(left),
        "baseline_passed": sum(row["correct"] is True for row in left.values()),
        "candidate_passed": sum(row["correct"] is True for row in right.values()),
        "regressions": [
            key
            for key in left
            if left[key]["correct"] is True and right[key]["correct"] is not True
        ],
        "improvements": [
            key for key in left if left[key]["correct"] is False and right[key]["correct"] is True
        ],
        "note": "Synthetic runtime checks. No model-quality or statistical-significance claim.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    regression = commands.add_parser("regression")
    regression.add_argument("--output", required=True, type=Path)
    worker = commands.add_parser("case")
    worker.add_argument("path", type=Path)
    worker.add_argument("--output", required=True, type=Path)
    comparison = commands.add_parser("compare")
    comparison.add_argument("baseline", type=Path)
    comparison.add_argument("candidate", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "compare":
            result = compare(args.baseline, args.candidate)
            print(json.dumps(result, indent=2))
            return int(bool(result["regressions"]))
        if args.command == "case":
            result = asyncio.run(run_case(args.path, args.output))
            print(json.dumps({"case": result["case_id"], "status": result["status"]}))
            return int(not result["correct"])
        args.output.mkdir(parents=True, exist_ok=False)
        failed = False
        for path in sorted((ROOT / "eval" / "cases").glob("*.json")):
            destination = args.output.resolve() / path.stem
            try:
                completed = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "eval.run",
                        "case",
                        str(path),
                        "--output",
                        str(destination),
                    ],
                    cwd=ROOT,
                    timeout=30,
                    check=False,
                )
                failed |= completed.returncode != 0
            except subprocess.TimeoutExpired:
                failed = True
                print(f"{path.stem}: infrastructure timeout", file=sys.stderr)
            if not (destination / "result.json").exists():
                failed = True
                destination.mkdir(parents=True, exist_ok=True)
                (destination / "result.json").write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "case_id": path.stem,
                            "status": "infrastructure_error",
                            "correct": None,
                            "case_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                            "suite": "ava-regressions",
                            "mode": "synthetic-runtime",
                            "model": "fixture-v1",
                            "compaction": False,
                        },
                        indent=2,
                    )
                    + "\n"
                )
        return int(failed)
    except (OSError, ValueError, AvaError) as error:
        print(f"eval: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
