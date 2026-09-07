"""Ava may propose a prompt change, but cannot rewrite its judge or accept its own score."""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import zipfile
from pathlib import Path

import pytest

from ava.llm.types import Item, Role, make_tool_call_block, make_tool_result_block
from ava.session.event import AssistantMessage, ToolResult
from ava.session.log import Log, OpenMode
from eval.benchmark import file_hash, write_json
from eval.diagnose import diagnose_session
from eval.evolve import (
    MAX_EVENT_CHARS,
    MAX_EXCERPT_CHARS,
    PROMPT_ENTRY,
    Proposal,
    candidate_wheel,
    propose,
    session_excerpt,
    validate_proposal,
)
from tests.conftest import ScriptedProvider, text_response, tool_call_response

BASELINE = "Use focused reads.\n{{environment}}\n{{scratchpad}}\n"
CANDIDATE = BASELINE + "When output is truncated, continue from the reported offset.\n"
RECORD = "ava-0.1.0.dist-info/RECORD"


def _wheel(path: Path, *, record: bool = True) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as wheel:
        wheel.writestr(PROMPT_ENTRY, BASELINE)
        wheel.writestr("ava/agent/agent.py", "# identical runtime code\n")
        wheel.writestr("ava-0.1.0.dist-info/METADATA", "Name: ava\nVersion: 0.1.0\n")
        if record:
            wheel.writestr(RECORD, f"{RECORD},,\n")


def _packet(tmp_path: Path) -> tuple[Path, dict]:
    directory = tmp_path / "packet"
    directory.mkdir()
    _wheel(directory / "baseline.whl")
    (directory / "baseline-system.md").write_text(BASELINE)
    session = directory / "session-000.jsonl.zst"
    log = Log.create_at(session, directory, "mock", "fixture-v1")
    log.append_batch(
        [
            AssistantMessage(
                attempt_id="fixture-read",
                item=Item(
                    role=Role.assistant,
                    blocks=[make_tool_call_block("read-1", "read", '{"path":"large.py"}')],
                ),
            ),
            ToolResult(
                item=Item(
                    role=Role.tool,
                    blocks=[
                        make_tool_result_block(
                            "read-1", "contents\n[Output truncated. Continue with offset=2.]", False
                        )
                    ],
                )
            ),
        ]
    )
    log.close()
    write_json(directory / "session-000.diagnosis.json", diagnose_session(session))
    packet = {
        "schema_version": 1,
        "baseline_wheel_sha256": file_hash(directory / "baseline.whl"),
        "baseline_prompt_sha256": file_hash(directory / "baseline-system.md"),
        "sessions": {
            "session-000": {
                "path": session.name,
                "sha256": file_hash(session),
                "diagnosis": "session-000.diagnosis.json",
                "last_sequence": 2,
            }
        },
    }
    write_json(directory / "packet.json", packet)
    return directory, packet


def _proposal(**updates) -> dict:
    return {
        "decision": "candidate",
        "hypothesis": "A truncation notice suggests checking continuation guidance.",
        "evidence": [{"session": "session-000", "sequences": [1, 2]}],
        "expected_improvement": "Fewer repeated full reads, subject to live evaluation.",
        "regression_risk": "Additional guidance may distract from small-file tasks.",
        "system_prompt": CANDIDATE,
        **updates,
    }


def test_candidate_wheel_only_changes_prompt_and_valid_record(tmp_path: Path):
    baseline, candidate = tmp_path / "baseline.whl", tmp_path / "candidate.whl"
    _wheel(baseline)
    before = baseline.read_bytes()
    candidate_wheel(baseline, candidate, CANDIDATE)
    with zipfile.ZipFile(baseline) as original, zipfile.ZipFile(candidate) as changed:
        assert original.namelist() == changed.namelist()
        differences = {
            name for name in original.namelist() if original.read(name) != changed.read(name)
        }
        assert differences == {PROMPT_ENTRY, RECORD}
        assert changed.read(PROMPT_ENTRY).decode() == CANDIDATE
        for name, digest, size in csv.reader(io.StringIO(changed.read(RECORD).decode())):
            if name == RECORD:
                assert digest == size == ""
            else:
                content = changed.read(name)
                expected = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=")
                assert digest == "sha256=" + expected.decode()
                assert int(size) == len(content)
    assert baseline.read_bytes() == before
    with pytest.raises(FileExistsError):
        candidate_wheel(baseline, candidate, CANDIDATE)
    malformed = tmp_path / "missing-record.whl"
    _wheel(malformed, record=False)
    with pytest.raises(ValueError):
        candidate_wheel(malformed, tmp_path / "invalid-candidate.whl", CANDIDATE)


def test_proposals_require_real_evidence_changed_prompt_and_required_fields(tmp_path: Path):
    directory, packet = _packet(tmp_path)
    validate_proposal(Proposal.model_validate(_proposal()), directory, packet)
    invalid = [
        _proposal(evidence=[]),
        _proposal(evidence=[{"session": "missing", "sequences": [1]}]),
        _proposal(evidence=[{"session": "session-000", "sequences": [999]}]),
        _proposal(system_prompt=BASELINE),
        _proposal(system_prompt="missing required environment and scratchpad fields"),
        _proposal(system_prompt=CANDIDATE + "{{environment}}"),
        _proposal(decision="no_change"),
    ]
    for value in invalid:
        with pytest.raises(ValueError):
            validate_proposal(Proposal.model_validate(value), directory, packet)
    validate_proposal(
        Proposal.model_validate(_proposal(decision="no_change", system_prompt=None)),
        directory,
        packet,
    )
    with (directory / "session-000.jsonl.zst").open("ab") as session:
        session.write(b"changed")
    with pytest.raises(ValueError, match="evidence changed"):
        validate_proposal(Proposal.model_validate(_proposal()), directory, packet)


def test_session_excerpt_reaches_semantic_evidence_and_pages_without_losing_results():
    events = [
        {"seq": 1, "kind": "step/claimed", "claimed": [{"role": "user", "text": "fix it"}]},
        *[
            {"seq": seq, "kind": "assistant/chunk", "delta": "streaming noise"}
            for seq in range(2, 1002)
        ],
        {"seq": 1002, "kind": "assistant/message", "item": {"role": "assistant"}},
        {"seq": 1003, "kind": "step/end", "reason": "completed"},
    ]
    for seq in range(1004, 1012):
        events.append({
            "seq": seq,
            "kind": "tool/result",
            "item": {"blocks": [{"text": 'HEAD evidence\n' + '\"中文\\\n' * 6000 + '\nTAIL error and continuation'}]},
        })
    events += [
        {"seq": 1012, "kind": "step/claimed", "claimed": []},
        {"seq": 1013, "kind": "step/end", "reason": "provider_error"},
        {"seq": 1014, "kind": "drive/error", "message": "provider unavailable"},
        {"seq": 1015, "kind": "turn/end", "reason": "provider_error"},
    ]
    seen, pages = [], []
    start = 1
    while True:
        page = session_excerpt(events, start, calls_remaining=6)
        rendered = json.dumps(page, ensure_ascii=False)
        assert len(rendered) <= MAX_EXCERPT_CHARS
        assert json.loads(rendered) == page
        assert page["events"] and page["model_calls_remaining"] == 6
        assert page["omitted_streaming_and_lifecycle_events"] == 1002
        for event in page["events"]:
            assert len(json.dumps(event, ensure_ascii=False)) <= MAX_EVENT_CHARS
            if event["kind"] == "tool/result":
                original = next(e for e in events if e["seq"] == event["seq"])
                assert event["truncated"]
                assert event["original_json_chars"] == len(json.dumps(original, ensure_ascii=False))
                assert event["omitted_json_chars"] > 0
                assert "HEAD evidence" in event["json_head"]
                assert "TAIL error and continuation" in event["json_tail"]
        seen.extend(event["seq"] for event in page["events"])
        pages.append(page)
        if page["next_seq"] is None:
            assert page["remaining_events"] == 0
            break
        assert page["next_seq"] > seen[-1]
        start = page["next_seq"]
    assert any(event["kind"] == "tool/result" for event in pages[0]["events"])
    assert len(pages) > 1  # The byte/character budget deferred real results, not chunks.
    assert seen == [1, 1002, *range(1004, 1012), 1013, 1014, 1015]
    assert session_excerpt(events, 1016, calls_remaining=1)["events"] == []
    with pytest.raises(ValueError, match="start_seq"):
        session_excerpt(events, -1, calls_remaining=1)


@pytest.mark.parametrize("max_calls", [3, 4])
async def test_real_agent_proposer_has_no_host_write_tool_and_never_accepts_candidate(
    tmp_path: Path, home: Path, max_calls: int
):
    directory, packet = _packet(tmp_path)
    grader = tmp_path / "grader.py"
    grader.write_text("independent acceptance criteria\n")
    before = {path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()}
    provider = ScriptedProvider(
        [
            tool_call_response(
                "blocked", "bash", json.dumps({"command": f"echo changed > {grader}"})
            ),
            tool_call_response(
                "inspect", "read_session", '{"session":"session-000","start_seq":1}'
            ),
            tool_call_response(
                "submit", "submit_proposal", json.dumps({"proposal": json.dumps(_proposal())})
            ),
            text_response("Submitted a candidate for independent evaluation."),
        ]
    )
    result = await propose(directory, provider, max_calls=max_calls, timeout=5)
    assert result["status"] == "proposed" and result["accepted"] is False
    assert result["model_calls"] == max_calls and provider.closed
    assert result["final_response_skipped"] is (max_calls == 3)
    if max_calls == 3:
        assert "model-call budget exhausted" in result["error"]
    else:
        assert result["error"] is None
    task = json.loads(provider.contexts[0].items[0].blocks[0].text)
    assert task["model_call_budget"]["total"] == max_calls
    assert task["model_call_budget"]["max_evidence_read_rounds"] == max_calls - 2
    assert all(
        {tool.name for tool in context.tools} == {"read_session", "submit_proposal"}
        for context in provider.contexts
    )
    assert grader.read_text() == "independent acceptance criteria\n"
    assert all((directory / name).read_bytes() == content for name, content in before.items())
    assert result["baseline_wheel_sha256"] == packet["baseline_wheel_sha256"]
    assert (directory / "proposal/ava-0.1.0-py3-none-any.whl").is_file()
    log = Log.open(directory / "proposal/session.jsonl.zst", OpenMode.read_only)
    try:
        blocked = [
            block
            for event in log.loaded_events
            if isinstance(event.payload, ToolResult)
            for block in event.payload.item.blocks
            if block.call_id == "blocked"
        ]
        assert len(blocked) == 1 and blocked[0].is_error and "unknown tool" in blocked[0].text
        inspected = [
            json.loads(block.text)
            for event in log.loaded_events
            if isinstance(event.payload, ToolResult)
            for block in event.payload.item.blocks
            if block.call_id == "inspect"
        ]
        assert len(inspected) == 1
        assert [event["seq"] for event in inspected[0]["events"]] == [1, 2]
        assert inspected[0]["model_calls_remaining"] == max_calls - 2
        assert inspected[0]["next_seq"] is None
    finally:
        log.close()


async def test_proposer_stops_at_model_budget_without_creating_candidate(
    tmp_path: Path, home: Path
):
    directory, _ = _packet(tmp_path)
    provider = ScriptedProvider(
        [tool_call_response("inspect", "read_session", '{"session":"session-000"}')]
    )
    result = await propose(directory, provider, max_calls=1, timeout=5)
    assert result["status"] == "incomplete" and result["accepted"] is False
    assert result["model_calls"] == provider.calls == 1
    assert "model-call budget exhausted" in result["error"]
    assert provider.closed
    assert not (directory / "proposal/ava-0.1.0-py3-none-any.whl").exists()
    assert not (directory / "proposal/proposal.json").exists()
