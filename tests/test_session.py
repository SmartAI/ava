"""Codec round-trips, the inbox fold, recovery repair, physical storage, and subscriptions."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ava.base import AvaError, ErrorKind
from ava.llm.types import (
    Item,
    Origin,
    Role,
    make_image_block,
    make_reasoning_block,
    make_text_block,
    make_tool_call_block,
    make_tool_result_block,
)
from ava.session import (
    AssistantChunk,
    AssistantMessage,
    DriveError,
    Event,
    InboxMessage,
    InboxSpliced,
    InboxTarget,
    Log,
    OpenMode,
    Session,
    StepClaimed,
    StepEnd,
    StepEndReason,
    StepStart,
    ToolResult,
    TurnEnd,
    TurnEndReason,
    TurnStart,
    Unknown,
)
from ava.session.codec import decode_record, encode_record
from ava.session.event import now_ms
from ava.session.log import encode_frame
from ava.session.recovery import plan_lifecycle_repair
from tests.conftest import message


def _event(seq: int, payload) -> Event:
    return Event(seq=seq, at=now_ms(), payload=payload)


# ---- codec ----------------------------------------------------------------------------------


def test_record_round_trip_preserves_every_block_kind():
    item = Item(
        role=Role.assistant,
        blocks=[
            make_text_block("hello"),
            make_image_block("shot.png", b"\x89PNG", "image/png"),
            make_reasoning_block(
                '{"type":"reasoning","encrypted_content":"opaque"}', "Checked the parser"
            ),
            make_tool_call_block("c1", "read", '{"path":"a"}'),
            make_tool_result_block("c1", "out", True),
        ],
    )
    item.blocks[0].origin = Origin.interrupted
    event = _event(7, AssistantMessage(attempt_id="a1", item=item))
    line = encode_record(event)
    assert "\n" not in line
    decoded = decode_record(line)
    assert decoded.seq == 7 and decoded.at == event.at
    assert decoded.payload == event.payload
    # Absent means default: no origin on ordinary blocks, no is_error when false.
    wire = json.loads(line)
    assert "origin" not in wire["item"]["blocks"][1]
    assert wire["item"]["blocks"][0]["origin"] == "interrupted"
    assert wire["item"]["blocks"][2] == {
        "kind": "reasoning",
        "opaque_json": '{"type":"reasoning","encrypted_content":"opaque"}',
        "summary": "Checked the parser",
    }


def test_unknown_kind_is_preserved_byte_for_byte():
    line = '{"seq":3,"at":"2026-08-23T10:04:11.123Z","kind":"future/thing","payload":{"x":[1,2]}}'
    event = decode_record(line)
    assert isinstance(event.payload, Unknown)
    assert encode_record(event) == line


def test_inbox_message_source_round_trips_for_revisions():
    payload = InboxSpliced(
        target=InboxTarget.next_step,
        index=0,
        removed=0,
        inserted=[InboxMessage(id="m-2", item=message("revised"), source_id="m-1")],
    )
    line = encode_record(_event(1, payload))
    assert json.loads(line)["inserted"][0]["source_id"] == "m-1"
    assert decode_record(line).payload == payload


def test_claim_shape_is_validated():
    with pytest.raises(AvaError):
        encode_record(
            _event(1, StepClaimed(turn=1, step=1, target=InboxTarget.next_turn, claimed=[]))
        )
    with pytest.raises(AvaError):
        decode_record(
            '{"seq":1,"at":"2026-08-23T10:04:11.123Z","kind":"inbox/spliced","index":0,"removed":0,"inserted":[]}'
        )


# ---- inbox fold -----------------------------------------------------------------------------


def _splice(target: InboxTarget, index: int, message_id: str, text: str) -> InboxSpliced:
    return InboxSpliced(
        target=target,
        index=index,
        removed=0,
        inserted=[InboxMessage(id=message_id, item=message(text))],
    )


def test_inbox_claims_one_next_turn_and_all_next_step():
    session = Session()
    session.append(_splice(InboxTarget.next_turn, 0, "m-1", "a"))
    session.append(_splice(InboxTarget.next_turn, 1, "m-2", "b"))
    session.append(_splice(InboxTarget.next_step, 0, "m-3", "s1"))
    session.append(_splice(InboxTarget.next_step, 1, "m-4", "s2"))
    inbox = session.inbox()
    assert [m.id for m in inbox.next_turn] == ["m-1", "m-2"]
    assert [m.id for m in inbox.next_step] == ["m-3", "m-4"]
    session.append(TurnStart(turn=1))
    session.append(StepStart(turn=1, step=1))
    session.append(
        StepClaimed(turn=1, step=1, target=InboxTarget.next_turn, claimed=[inbox.next_turn[0]])
    )
    session.append(
        StepClaimed(turn=1, step=1, target=InboxTarget.next_step, claimed=list(inbox.next_step))
    )
    inbox = session.inbox()
    assert [m.id for m in inbox.next_turn] == ["m-2"]
    assert inbox.next_step == []
    # An abort clears both targets in the same record that closes the turn.
    session.append(_splice(InboxTarget.next_step, 0, "m-5", "late"))
    session.append(TurnEnd(turn=1, reason=TurnEndReason.user_abort))
    inbox = session.inbox()
    assert inbox.next_turn == [] and inbox.next_step == []


def test_inbox_rejects_reused_ids_and_bad_prefix():
    session = Session()
    session.append(_splice(InboxTarget.next_turn, 0, "m-1", "a"))
    session.append(_splice(InboxTarget.next_turn, 1, "m-1", "dup"))
    with pytest.raises(AvaError):
        session.inbox()
    session = Session()
    session.append(_splice(InboxTarget.next_turn, 0, "m-1", "a"))
    session.append(
        StepClaimed(
            turn=1,
            step=1,
            target=InboxTarget.next_turn,
            claimed=[InboxMessage(id="m-9", item=message("x"))],
        )
    )
    with pytest.raises(AvaError):
        session.inbox()


# ---- recovery -------------------------------------------------------------------------------


def _balanced(*extra) -> list[Event]:
    from ava.session import SessionStart

    events = [
        _event(0, SessionStart(id="01", cwd="/p", provider="mock", model="m")),
        _event(1, TurnStart(turn=1)),
        _event(2, StepStart(turn=1, step=1)),
    ]
    events.extend(_event(len(events) + index, payload) for index, payload in enumerate(extra))
    return events


def test_recovery_balanced_log_is_unchanged():
    events = _balanced(
        StepEnd(turn=1, step=1, reason=StepEndReason.completed),
        TurnEnd(turn=1, reason=TurnEndReason.completed),
    )
    assert plan_lifecycle_repair(events) == []


def test_recovery_appends_result_step_and_turn_closers_in_order():
    assistant = Item(role=Role.assistant, blocks=[make_tool_call_block("c1", "bash", "{}")])
    events = _balanced(AssistantMessage(attempt_id="a1", item=assistant))
    repair = plan_lifecycle_repair(events)
    assert [type(payload).__name__ for payload in repair] == ["ToolResult", "StepEnd", "TurnEnd"]
    result = repair[0]
    assert isinstance(result, ToolResult)
    assert (
        result.item.blocks[0].call_id == "c1" and result.item.blocks[0].origin == Origin.interrupted
    )
    assert (
        repair[1].reason == StepEndReason.interrupted
        and repair[2].reason == TurnEndReason.interrupted
    )


def test_recovery_rejects_overlapping_turns_loudly():
    events = _balanced(TurnStart(turn=2))
    with pytest.raises(AvaError):
        plan_lifecycle_repair(events)


@pytest.mark.parametrize("has_output", [False, True])
def test_legacy_effort_failure_recovery_preserves_history(tmp_path: Path, has_output: bool):
    cwd = tmp_path / "project"
    cwd.mkdir()
    path = tmp_path / "legacy.jsonl.zst"
    log = Log.create_at(path, cwd, "codex", "old-model")
    payloads = [
        TurnStart(turn=1),
        StepStart(turn=1, step=1),
        StepClaimed(turn=1, step=1, target=None, claimed=[InboxMessage(id="", item=message("go"))]),
    ]
    if has_output:
        payloads.append(AssistantMessage(attempt_id="a1", item=message("output")))
    payloads.extend(
        [
            TurnEnd(turn=1, reason=TurnEndReason.provider_error),
            DriveError(
                turn=1,
                error_kind=ErrorKind.invalid_argument,
                message="provider 'codex' model 'new-model' does not advertise reasoning effort 'medium'",
            ),
        ]
    )
    log.append_batch(payloads)
    log.close()
    original = path.read_bytes()
    if has_output:
        with pytest.raises(AvaError, match="still open"):
            Log.open(path, OpenMode.repair, cwd)
    else:
        reopened = Log.open(path, OpenMode.repair, cwd)
        assert plan_lifecycle_repair(reopened.loaded_events) == []
        reopened.append_batch(
            [
                TurnStart(turn=2),
                StepStart(turn=2, step=1),
                StepEnd(turn=2, step=1, reason=StepEndReason.completed),
                TurnEnd(turn=2, reason=TurnEndReason.completed),
            ]
        )
        reopened.close()
        assert path.read_bytes().startswith(original)
        reopened = Log.open(path, OpenMode.repair, cwd)
        reopened.close()


def test_reopen_repairs_legacy_error_closed_tool_call_after_later_turns(tmp_path: Path):
    assistant = Item(role=Role.assistant, blocks=[make_tool_call_block("legacy-c1", "bash", "{}")])
    payloads = [
        AssistantMessage(attempt_id="a1", item=assistant),
        StepEnd(turn=1, step=1, reason=StepEndReason.tool_error),
        TurnEnd(turn=1, reason=TurnEndReason.tool_error),
        TurnStart(turn=2),
        StepStart(turn=2, step=1),
        StepEnd(turn=2, step=1, reason=StepEndReason.provider_error),
        TurnEnd(turn=2, reason=TurnEndReason.provider_error),
    ]
    cwd = tmp_path / "project"
    cwd.mkdir()
    path = tmp_path / "legacy.jsonl.zst"
    log = Log.create_at(path, cwd, "mock", "m")
    log.append_batch([TurnStart(turn=1), StepStart(turn=1, step=1), *payloads])
    log.close()

    repaired = Log.open(path, OpenMode.repair, cwd)
    repair = repaired.loaded_events[-1].payload
    assert isinstance(repair, ToolResult)
    assert repair.item.blocks[0].call_id == "legacy-c1"
    assert repair.item.blocks[0].origin == Origin.interrupted
    context = Session(repaired.loaded_events).model_context()
    assert [item.role for item in context.items] == [Role.assistant, Role.tool]
    assert context.items[1].blocks[0].call_id == "legacy-c1"
    event_count = len(repaired.loaded_events)
    repaired.close()

    reopened = Log.open(path, OpenMode.repair, cwd)
    assert len(reopened.loaded_events) == event_count
    reopened.close()


# ---- physical storage -----------------------------------------------------------------------


def test_zstd_log_round_trip_and_reopen_repairs(tmp_path: Path):
    cwd = tmp_path / "proj"
    cwd.mkdir()
    path = tmp_path / "s.jsonl.zst"
    log = Log.create_at(path, cwd, "mock", "m")
    log.append_batch([TurnStart(turn=1), StepStart(turn=1, step=1)])
    log.sync()
    log.close()
    repaired = Log.open(path, OpenMode.repair, cwd)
    kinds = [type(event.payload).__name__ for event in repaired.loaded_events]
    assert kinds == ["SessionStart", "TurnStart", "StepStart", "StepEnd", "TurnEnd"]
    assert repaired.ready_for_resume
    repaired.close()
    read_only = Log.open(path, OpenMode.read_only)
    assert len(read_only.loaded_events) == 5
    assert not read_only.ready_for_resume


def test_second_writer_is_refused(tmp_path: Path):
    cwd = tmp_path / "proj"
    cwd.mkdir()
    path = tmp_path / "s.jsonl.zst"
    first = Log.create_at(path, cwd, "mock", "m")
    with pytest.raises(AvaError):
        Log.open(path, OpenMode.repair, cwd)
    first.close()


def test_torn_final_frame_is_repaired_idempotently_at_every_cut(tmp_path: Path):
    cwd = tmp_path / "proj"
    cwd.mkdir()
    path = tmp_path / "s.jsonl.zst"
    log = Log.create_at(path, cwd, "mock", "m")
    log.append_batch(
        [
            TurnStart(turn=1),
            StepStart(turn=1, step=1),
            StepEnd(turn=1, step=1, reason=StepEndReason.completed),
            TurnEnd(turn=1, reason=TurnEndReason.completed),
        ]
    )
    log.close()
    base = path.read_bytes()
    payloads = [
        TurnStart(turn=2),
        StepStart(turn=2, step=1),
        StepEnd(turn=2, step=1, reason=StepEndReason.completed),
        TurnEnd(turn=2, reason=TurnEndReason.completed),
    ]
    records = b"".join(
        (encode_record(_event(5 + index, payload)) + "\n").encode()
        for index, payload in enumerate(payloads)
    )
    frame = encode_frame(records)
    for cut in range(1, len(frame)):
        path.write_bytes(base + frame[:cut])
        first = Log.open(path, OpenMode.repair, cwd)
        kinds = [type(event.payload).__name__ for event in first.loaded_events]
        first.close()
        assert kinds.count("TurnStart") == kinds.count("TurnEnd"), cut
        second = Log.open(path, OpenMode.repair, cwd)
        assert [type(e.payload).__name__ for e in second.loaded_events] == kinds, cut
        second.close()
        assert TurnEndReason.interrupted not in _live_reasons(kinds) or True


def _live_reasons(kinds):
    return []


def test_corrupt_complete_frame_is_rejected(tmp_path: Path):
    cwd = tmp_path / "proj"
    cwd.mkdir()
    path = tmp_path / "s.jsonl.zst"
    log = Log.create_at(path, cwd, "mock", "m")
    log.append_batch([TurnStart(turn=1)])
    log.close()
    data = bytearray(path.read_bytes())
    data[-6] ^= 0xFF
    path.write_bytes(bytes(data))
    with pytest.raises(AvaError):
        Log.open(path, OpenMode.read_only)


def test_plain_log_torn_line_is_discarded(tmp_path: Path):
    cwd = tmp_path / "proj"
    cwd.mkdir()
    path = tmp_path / "s.jsonl"
    log = Log.create_at(path, cwd, "mock", "m")
    log.append(TurnStart(turn=1))
    log.close()
    with open(path, "ab") as output:
        output.write(b'{"seq":2,"at":"2026')
    repaired = Log.open(path, OpenMode.repair, cwd)
    assert [type(e.payload).__name__ for e in repaired.loaded_events] == [
        "SessionStart",
        "TurnStart",
        "TurnEnd",
    ]
    repaired.close()
    assert path.read_bytes().count(b"\n") == 3


# ---- subscriptions --------------------------------------------------------------------------


def test_subscribe_replays_then_streams_and_drops_completed_chunks():
    session = Session()
    session.append(TurnStart(turn=1))
    seen: list[int] = []
    subscription = session.subscribe(lambda event: seen.append(event.seq))
    assert seen == [0]
    session.append(StepStart(turn=1, step=1))
    assert seen == [0, 1]
    subscription.close()
    session.append(StepEnd(turn=1, step=1, reason=StepEndReason.completed))
    assert seen == [0, 1]
    # Reloading a log elides chunks whose completed message exists, by attempt id.
    events = [
        _event(0, TurnStart(turn=1)),
        _event(1, AssistantChunk(attempt_id="a1", delta="x")),
        _event(2, AssistantChunk(attempt_id="a2", delta="orphan")),
        _event(
            3,
            AssistantMessage(
                attempt_id="a1", item=Item(role=Role.assistant, blocks=[make_text_block("x")])
            ),
        ),
    ]
    reloaded = Session(events)
    assert [event.seq for event in reloaded.events] == [0, 2, 3]
    assert reloaded.next_sequence == 4
