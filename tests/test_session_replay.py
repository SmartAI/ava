"""Golden execution evidence shared by projection and desktop acceptance checks."""

from __future__ import annotations

import json

from ava.llm.types import (
    Item,
    Role,
    ToolDef,
    make_text_block,
    make_tool_call_block,
    make_tool_result_block,
)
from ava.session.codec import decode_record, encode_record
from ava.session.event import (
    AssistantChunk,
    AssistantMessage,
    AttemptTiming,
    CompactionSeed,
    InboxMessage,
    InboxSpliced,
    InboxTarget,
    PromptResolved,
    RequestPrepared,
    SessionStart,
    StepClaimed,
    StepEnd,
    StepEndReason,
    StepStart,
    ToolDuration,
    ToolResult,
    ToolsAdvertised,
    TurnEnd,
    TurnEndReason,
    TurnStart,
    Unknown,
    Usage,
)
from ava.session.replay import PAGE_SIZE, TEXT_PAGE_SIZE, replay_detail, replay_index
from ava.session.session import Session


def text(value: str, role: Role = Role.user) -> Item:
    return Item(role=role, blocks=[make_text_block(value)])


def replay_payloads():
    first = InboxMessage("first", text("Investigate login failure"))
    late = InboxMessage("late", text("Also inspect the expired cookie"))
    return [
        PromptResolved("Historical instructions — never execute replay content."),
        ToolsAdvertised([ToolDef("read", "Read a file")]),
        TurnStart(1),
        StepStart(1, 1),
        InboxSpliced(InboxTarget.next_turn, 0, 0, [first]),
        StepClaimed(1, 1, InboxTarget.next_turn, [first]),
        RequestPrepared("a1", "desktop-test", "fixture", "high"),
        InboxSpliced(InboxTarget.next_step, 0, 0, [late]),
        AssistantChunk("a1", "Checking the file"),
        AssistantMessage(
            "a1",
            Item(Role.assistant, [make_tool_call_block("call1", "read", '{"path":"auth.py"}')]),
        ),
        Usage("a1", input=100, output=20),
        AttemptTiming("a1", 3200, 150),
        ToolResult(
            Item(Role.tool, [make_tool_result_block("call1", "File unavailable", True)]),
            [ToolDuration("call1", 80)],
            True,
        ),
        StepEnd(1, 1, StepEndReason.completed),
        StepStart(1, 2),
        StepClaimed(1, 2, InboxTarget.next_step, [late]),
        CompactionSeed(5, 13, "Summarize the investigation", text("Summary: first read failed")),
        RequestPrepared(
            "a2", "desktop-test", "fixture", prefix_items=[text("Restored active goal")]
        ),
        AssistantMessage(
            "a2",
            Item(Role.assistant, [make_tool_call_block("call2", "read", '{ "path": "auth.py" }')]),
        ),
        ToolResult(
            Item(Role.tool, [make_tool_result_block("call2", "Cookie expires immediately", False)])
        ),
        StepEnd(1, 2, StepEndReason.completed),
        StepStart(1, 3),
        RequestPrepared("a3", "desktop-test", "fixture"),
        AssistantMessage(
            "a3", text("Found the expiration configuration. No changes made.", Role.assistant)
        ),
        StepEnd(1, 3, StepEndReason.completed),
        TurnEnd(1, TurnEndReason.completed),
    ]


def history() -> Session:
    session = Session()
    session.append(SessionStart("golden", "/not-accessed", "desktop-test", "fixture"))
    for payload in replay_payloads():
        session.append(payload)
    return session


def test_replay_request_boundary_queue_compaction_and_state_are_historical():
    events = history().events
    for event in events:
        assert decode_record(encode_record(event)).payload == event.payload
    first = replay_detail(events, 10, view="context")
    assert first["request_boundary"] == 7
    assert "Investigate login failure" in first["text"]
    assert "expired cookie" not in first["text"]
    assert '"effort": "high"' in first["text"]
    assert "File unavailable" not in first["text"]
    second = replay_detail(events, 19, view="context")
    assert second["request_boundary"] == 18
    assert "Restored active goal" in second["text"]
    assert "expired cookie" in second["text"]
    assert "Summary: first read failed" in second["text"]
    assert "Investigate login failure" not in second["text"]
    state = replay_detail(events, 16, view="state")["text"]
    assert '"before"' in state and "expired cookie" in state and '"next_step": []' in state
    legacy = [e for e in events if not isinstance(e.payload, RequestPrepared)]
    fallback = replay_detail(legacy, 10, view="context")
    assert fallback["request_boundary"] is None
    assert "must not be treated as the exact input" in fallback["text"]
    assert not replay_index(legacy)["request_boundaries_recorded"]


def test_replay_observations_links_search_and_full_raw_evidence():
    events = history().events
    index = replay_index(events)
    assert index["error_events"] == 1
    assert index["issue_events"] == 3
    assert index["issue_sequences"] == [13, 17, 19]
    assert index["slowest_request"] == {"seq": 12, "elapsed_ms": 3200}
    assert all(r["kind"] != "assistant/chunk" for r in index["rows"])
    assert replay_index(events, query="expired cookie")["total"] == 2
    assert replay_index(events, category="issues")["total"] == 3
    assert "not necessarily waste" in replay_detail(events, 19)["observations"][0]["text"]
    assert {link["seq"] for link in replay_detail(events, 13)["links"]} == {10, 18}
    assert {link["seq"] for link in replay_detail(events, 10)["links"]} == {7, 11, 12, 13}
    assert json.loads(replay_detail(events, 9, view="raw")["text"])["delta"] == "Checking the file"
    assert replay_index(events, category="raw")["total"] == len(events)
    assert "No changes made" in replay_detail(events, 24)["text"]
    # Provider call ids are not globally unique across the session.
    events[19].payload.item.blocks[0].call_id = "call1"
    events[20].payload.item.blocks[0].call_id = "call1"
    assert {link["seq"] for link in replay_detail(events, 10)["links"]} == {7, 11, 12, 13}
    assert {link["seq"] for link in replay_detail(events, 20)["links"]} == {19, 23}


def test_replay_pages_large_text_and_preserves_unknown_records():
    session = history()
    big = "证据\n" * TEXT_PAGE_SIZE
    event = session.append(PromptResolved(big))
    parts = []
    offset = 0
    while True:
        detail = replay_detail(session.events, event.seq, offset=offset)
        assert len(detail["text"]) <= TEXT_PAGE_SIZE
        parts.append(detail["text"])
        if detail["next_offset"] is None:
            break
        offset = detail["next_offset"]
    assert "".join(parts) == big
    for _ in range(PAGE_SIZE):
        session.append(AssistantChunk("unfinished", "partial"))
    first = replay_index(session.events, category="raw")
    rest = replay_index(session.events, category="raw", offset=first["next_offset"])
    assert len(first["rows"]) == PAGE_SIZE
    assert len(first["rows"] + rest["rows"]) == len(session.events)
    line = '{"seq":900,"at":"2026-08-23T10:04:11.123Z","kind":"future/evidence","payload":{"value":42}}'
    unknown = decode_record(line)
    assert isinstance(unknown.payload, Unknown)
    assert replay_detail([unknown], 900, view="raw")["text"] == line
