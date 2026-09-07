"""Session evidence stays read-only and distinguishes clues from proved inefficiency."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from ava.llm.types import (
    Item,
    Origin,
    Role,
    make_text_block,
    make_tool_call_block,
    make_tool_result_block,
)
from ava.session.event import AssistantMessage, CompactionSeed, ToolResult
from ava.session.log import Log
from eval.diagnose import diagnose_session


def _call(call_id: str, tool: str, arguments: str) -> AssistantMessage:
    return AssistantMessage(
        attempt_id=f"attempt-{call_id}",
        item=Item(role=Role.assistant, blocks=[make_tool_call_block(call_id, tool, arguments)]),
    )


def _result(call_id: str, text: str, *, error: bool = False) -> ToolResult:
    return ToolResult(
        item=Item(role=Role.tool, blocks=[make_tool_result_block(call_id, text, error)])
    )


def _log(path: Path, payloads: list) -> Path:
    log = Log.create_at(path, path.parent, "mock", "smoke-v1")
    try:
        log.append_batch(payloads)
    finally:
        log.close()
    return path


def test_repeats_preserve_evidence_and_account_for_intervening_mutations(tmp_path: Path):
    secret = "private source text"
    path = _log(
        tmp_path / "session.jsonl.zst",
        [
            _call("r1", "read", '{"path":"secret.py","offset":1}'),
            _result("r1", secret),
            _call("r2", "read", '{ "offset": 1, "path": "secret.py" }'),
            _result("r2", secret),
            _call("w1", "edit", '{"path":"secret.py","old_text":"x","new_text":"y"}'),
            _result("w1", "edited"),
            _call("r3", "read", '{"offset":1,"path":"secret.py"}'),
            _result("r3", "new " + secret),
            _call("b1", "bash", '{"command":"echo hi"}'),
            _result("b1", "hi"),
            _call("r4", "read", '{"offset":1,"path":"secret.py"}'),
            _result("r4", "new " + secret),
        ],
    )
    before = path.read_bytes()
    report = diagnose_session(path)
    immediate, after_edit, after_bash = report["repeat_candidates"]
    assert immediate["previous"] == {"seq": 1, "block_index": 0, "call_id": "r1"}
    assert immediate["current"] == {"seq": 3, "block_index": 0, "call_id": "r2"}
    assert immediate["other_calls_between"] == 0
    assert immediate["recorded_potential_mutation_since_previous"] is False
    assert after_edit["recorded_potential_mutation_since_previous"] is True
    assert after_bash["recorded_potential_mutation_since_previous"] is True
    assert report["per_tool"]["read"]["result_characters"] == 4 * len(secret) + 8
    assert report["summary"]["task_correctness"] is None
    assert secret not in json.dumps(report) and "secret.py" not in json.dumps(report)
    assert path.read_bytes() == before


def test_partial_logs_report_missing_and_skipped_results_without_repair(tmp_path: Path):
    skipped = _result("skip", "[Tool call skipped.]", error=True)
    skipped.item.blocks[0].origin = Origin.skipped
    path = _log(
        tmp_path / "partial.jsonl",
        [
            _call("read", "read", '{"path":"large.py"}'),
            _result("read", "some text\n[Output truncated. Continue with offset=2.]"),
            _call("fail", "read", '{"path":"missing.py"}'),
            _result("fail", "cannot read missing file", error=True),
            _call("skip", "bash", '{"command":"touch MUST_NOT_EXIST"}'),
            skipped,
            _result("orphan", "detached result"),
            _call("pending", "bash", '{"command":"touch MUST_NOT_EXIST"}'),
        ],
    )
    with path.open("ab") as destination:
        destination.write(b'{"seq":9,"kind":')
    before = path.read_bytes()
    report = diagnose_session(path)
    assert report["summary"]["error_results"] == 2
    assert report["summary"]["execution_error_signals"] == 1
    assert report["summary"]["missing_results"] == 1
    assert report["summary"]["unmatched_results"] == 1
    assert report["summary"]["results_with_truncation_clues"] == 1
    assert report["missing_result_evidence"] == [{"seq": 8, "block_index": 0, "call_id": "pending"}]
    assert report["truncation_evidence"][0]["truncation_clues"] == ["read_continuation_notice"]
    assert path.read_bytes() == before
    assert not (tmp_path / "MUST_NOT_EXIST").exists()


def test_successful_bash_truncation_is_not_an_execution_error(tmp_path: Path):
    path = _log(
        tmp_path / "truncated.jsonl",
        [
            _call("bash", "bash", '{"command":"build"}'),
            _result(
                "bash",
                "done\n[Output truncated; showing trailing output. Full output: /tmp/log]",
            ),
        ],
    )
    report = diagnose_session(path)
    assert report["summary"]["results_with_truncation_clues"] == 1
    assert report["summary"]["error_results"] == 0
    assert report["summary"]["execution_error_signals"] == 0


def test_final_visible_volume_uses_compaction_projection_without_claiming_request_cost(
    tmp_path: Path,
):
    path = _log(
        tmp_path / "compacted.jsonl.zst",
        [
            _call("old", "read", '{"path":"old.py"}'),
            _result("old", "large historical result"),
            CompactionSeed(
                covered_begin=1,
                covered_end=2,
                instruction="Summarize.",
                item=Item(role=Role.user, blocks=[make_text_block("summary")]),
            ),
            _call("new", "read", '{"path":"new.py"}'),
            _result("new", "你好"),
        ],
    )
    report = diagnose_session(path)
    assert report["summary"]["recorded_result_characters"] == len("large historical result") + 2
    assert report["summary"]["final_context_tool_result_characters"] == 2
    assert report["summary"]["cumulative_request_tool_result_characters"] is None


def test_cli_creates_new_report_only_and_enforces_bounded_input(tmp_path: Path, monkeypatch):
    path = _log(tmp_path / "session.jsonl.zst", [_call("c1", "read", '{"path":"a.py"}')])
    before = path.read_bytes()
    output = tmp_path / "report.json"
    command = [sys.executable, "-m", "eval.diagnose", str(path), "--output", str(output)]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stderr
    report_bytes = output.read_bytes()
    assert json.loads(report_bytes)["summary"]["missing_results"] == 1
    repeated = subprocess.run(command, capture_output=True, text=True, check=False)
    assert repeated.returncode == 2 and "File exists" in repeated.stderr
    assert output.read_bytes() == report_bytes and path.read_bytes() == before
    monkeypatch.setattr("eval.diagnose.MAX_STORED_BYTES", 1)
    with pytest.raises(ValueError, match="stored-input limit"):
        diagnose_session(path)
