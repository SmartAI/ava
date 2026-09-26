"""Small, anonymized stop-diagnosis golden set: timeout, continuation, and controls."""
from __future__ import annotations

import json

import pytest

from ava.base import ErrorKind
from ava.llm.types import Role
from ava.session.event import (
    AssistantMessage,
    DriveError,
    InboxMessage,
    InboxSpliced,
    InboxTarget,
    RequestPrepared,
    SessionStart,
    StepClaimed,
    StepEnd,
    StepEndReason,
    StepStart,
    TurnEnd,
    TurnEndReason,
    TurnStart,
)
from ava.session.replay import replay_detail, replay_index
from ava.session.session import Session
from ava.session.stops import error_evidence, session_stops, stop_summary
from tests.test_session_replay import text


def stop_payloads():
    """Same ordering as the observed timeout: end, error, user input, next turn."""
    continuation = InboxMessage("continue-1", text("continue"))
    return [
        TurnStart(1), StepStart(1, 1),
        AssistantMessage("a1", text("Investigating the report.", Role.assistant)),
        StepEnd(1, 1, StepEndReason.provider_error),
        TurnEnd(1, TurnEndReason.provider_error),
        DriveError(1, ErrorKind.timeout, "provider request failed: ",
                   json.dumps({"exception_type": "ReadTimeout", "elapsed_ms": 161065,
                               "status": 200, "terminal_received": False,
                               "last_sse_event": "response.function_call_arguments.delta"})),
        InboxSpliced(InboxTarget.next_turn, 0, 0, [continuation]),
        TurnStart(2), StepStart(2, 1),
        StepClaimed(2, 1, InboxTarget.next_turn, [continuation]),
        RequestPrepared("a2", "desktop-test", "fixture"),
        AssistantMessage("a2", text("**没有全部完成。**\n\nThe final report is still missing.", Role.assistant)),
        StepEnd(2, 1, StepEndReason.completed), TurnEnd(2, TurnEndReason.completed),
    ]


def stop_history():
    session = Session()
    session.append(SessionStart("stops", "/not-accessed", "desktop-test", "fixture"))
    for payload in stop_payloads():
        session.append(payload)
    return session


def test_stop_diagnosis_joins_late_error_and_links_continuation_without_claiming_success():
    events = stop_history().events
    stops = session_stops(events)
    first, second = stops
    assert first["seq"] == 5 and first["failure"]
    error = first["errors"][0]
    assert error["seq"] == 6
    assert error["summary"] == "Request timed out · ReadTimeout"
    assert error["diagnostics"]["elapsed_ms"] == 161065
    assert "HTTP 200" not in error["summary"], "Transport success is not a completed streamed request"
    followup, = first["followups"]
    assert (followup["input_seq"], followup["claimed_seq"], followup["next_activity_seq"]) == (7, 10, 11)
    assert followup["next_activity_kind"] == "request/prepared"
    assert second["errors"] == [], "Do not attribute a previous turn's timeout to this turn"
    assert second["unfinished_hint"] and not second["failure"]
    assert stop_summary(stops)["followups"] == 1
    detail = replay_detail(events, 5)
    assert "ReadTimeout" in detail["text"] and "not a task-completion verdict" in detail["text"]
    assert {link["seq"] for link in detail["links"]} == {3, 6, 7, 10, 11}
    assert 5 in {link["seq"] for link in replay_detail(events, 7)["links"]}
    for category, seqs in [("stops", [5, 14]), ("failures", [5]), ("followups", [5]), ("unfinished", [14])]:
        assert [r["seq"] for r in replay_index(events, category=category)["rows"]] == seqs
    assert replay_index(events, category="stops", query="ReadTimeout")["total"] == 1
    assert "Request timed out" in replay_detail(events, 6)["text"]


@pytest.mark.parametrize("reason", [TurnEndReason.completed, TurnEndReason.user_abort, TurnEndReason.user_pause])
def test_continuation_is_not_a_failure_or_task_incompletion_verdict(reason):
    session = Session()
    session.append(TurnStart(1))
    session.append(AssistantMessage("a1", text("Committed the requested change.\n\nTest fixture text: not finished", Role.assistant)))
    session.append(TurnEnd(1, reason))
    continuation = InboxMessage("c", text("OK, continue"))
    session.append(InboxSpliced(InboxTarget.next_turn, 0, 0, [continuation]))
    pending = session_stops(session.events)[0]
    assert not pending["failure"] and not pending["unfinished_hint"]
    assert pending["followups"][0]["received"] is False
    # A deleted request must not look like an actual manual continuation.
    session.append(InboxSpliced(InboxTarget.next_turn, 0, 1, []))
    assert not session_stops(session.events)[0]["followups"]
    session.append(TurnStart(2))
    running = InboxMessage("running", text("继续"))
    session.append(InboxSpliced(InboxTarget.next_step, 0, 0, [running]))
    session.append(StepClaimed(2, 1, InboxTarget.next_step, [running]))
    assert not session_stops(session.events)[0]["followups"], "Input during an open turn is not evidence of a prior stop"


def test_stop_snapshot_does_not_borrow_future_errors_claims_or_activity():
    events = stop_history().events
    stopped = session_stops(events[:6])[0]
    assert stopped["errors"] == [] and stopped["followups"] == []
    queued = session_stops(events[:8])[0]["followups"][0]
    assert not queued["received"] and queued["next_activity_seq"] is None
    claimed = session_stops(events[:11])[0]["followups"][0]
    assert claimed["received"] and claimed["next_activity_seq"] is None
    event = Session().append(DriveError(1, ErrorKind.network, "connection closed", "not JSON"))
    assert error_evidence(event)["diagnostics"] == {}


def test_legacy_claims_and_empty_final_outputs_keep_evidence_limits():
    session = Session()
    session.append(TurnStart(1))
    session.append(TurnEnd(1, TurnEndReason.interrupted))
    session.append(TurnStart(2))
    session.append(StepClaimed(2, 1, None, [InboxMessage("", text("继续完成报告"))]))
    session.append(AssistantMessage("a2", text("", Role.assistant)))
    session.append(TurnEnd(2, TurnEndReason.completed))
    first, second = session_stops(session.events)
    assert first["failure"] and first["followups"][0]["legacy"]
    assert first["followups"][0]["next_activity_seq"] == 4
    assert not second["unfinished_hint"] and second["last_response_excerpt"] == ""
