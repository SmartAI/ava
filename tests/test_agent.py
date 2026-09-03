"""The loop: claiming, steering, tools, pause/resume, abort repair, errors, and durability."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from ava.agent import Agent, CancelCause, CompactionOptions, Status
from ava.base import AvaError
from ava.llm import ModelCapabilities, Origin, StopReason, StreamEvent, StreamEventKind, Usage
from ava.llm.types import ContentBlockKind
from ava.session import (
    AssistantMessage,
    DriveError,
    Event,
    Log,
    OpenMode,
    StepClaimed,
    StepEnd,
    StepEndReason,
    ToolResult,
    TurnEnd,
    TurnEndReason,
)
from tests.conftest import (
    ScriptedProvider,
    message,
    provider_error,
    text_response,
    tool_call_response,
)


def kinds(events: list[Event]) -> list[str]:
    return [type(event.payload).__name__ for event in events]


def turn_ends(agent: Agent) -> list[TurnEndReason]:
    return [
        event.payload.reason
        for event in agent.state.session.events
        if isinstance(event.payload, TurnEnd)
    ]


def step_ends(agent: Agent) -> list[StepEndReason]:
    return [
        event.payload.reason
        for event in agent.state.session.events
        if isinstance(event.payload, StepEnd)
    ]


async def test_one_turn_claims_input_and_closes_cleanly(home: Path, project: Path):
    provider = ScriptedProvider([text_response("Hel", "lo", usage=Usage(input=10, output=2))])
    agent = Agent.create(provider, project)
    seen: list[str] = []
    agent.subscribe(lambda event: seen.append(type(event.payload).__name__))
    await agent.followup(message("hi"))
    assert agent.status == Status.idle
    await agent.drive()
    assert seen == [
        "SessionStart",
        "PromptResolved",
        "ToolsAdvertised",
        "InboxSpliced",
        "TurnStart",
        "StepStart",
        "StepClaimed",
        "Selection",
        "AssistantChunk",
        "AssistantChunk",
        "AssistantMessage",
        "Usage",
        "AttemptTiming",
        "StepEnd",
        "TurnEnd",
    ]
    context = provider.contexts[0]
    assert context.system_prompt.startswith("You are Ava")
    assert [tool.name for tool in context.tools] == ["read", "write", "edit", "bash"]
    assert [block.text for item in context.items for block in item.blocks] == ["hi"]
    assert agent.state.session.inbox().next_turn == []
    await agent.aclose()


async def test_tool_calls_run_in_order_and_feed_the_next_request(home: Path, project: Path):
    (project / "fact.txt").write_text("durable fact\n")
    provider = ScriptedProvider(
        [
            tool_call_response("c1", "read", json.dumps({"path": "fact.txt"}), text="Reading."),
            text_response("done"),
        ]
    )
    agent = Agent.create(provider, project)
    await agent.followup(message("read it"))
    await agent.drive()
    events = agent.state.session.events
    results = [event.payload for event in events if isinstance(event.payload, ToolResult)]
    assert len(results) == 1
    assert (
        results[0].item.blocks[0].text == "durable fact\n"
        and results[0].durations[0].call_id == "c1"
    )
    assert step_ends(agent) == [StepEndReason.completed, StepEndReason.completed]
    assert turn_ends(agent) == [TurnEndReason.completed]
    second = provider.contexts[1]
    roles = [item.role.value for item in second.items]
    assert roles == ["user", "assistant", "tool"]
    unknown = ScriptedProvider([tool_call_response("c9", "nope", "{}"), text_response("ok")])
    agent2 = Agent.create(unknown, project)
    await agent2.followup(message("x"))
    await agent2.drive()
    result = next(
        e.payload for e in agent2.state.session.events if isinstance(e.payload, ToolResult)
    )
    assert result.item.blocks[0].is_error and "unknown tool 'nope'" in result.item.blocks[0].text
    await agent.aclose()
    await agent2.aclose()


async def test_steer_lands_at_next_step_and_followup_opens_its_own_turn(home: Path, project: Path):
    provider = ScriptedProvider(
        [
            tool_call_response("c1", "bash", json.dumps({"command": "echo one"})),
            text_response("first done"),
            text_response("second done"),
        ]
    )
    provider.gate = asyncio.Event()
    agent = Agent.create(provider, project)
    await agent.followup(message("start"))
    drive = asyncio.create_task(agent.drive())
    await provider.started.wait()
    assert agent.status == Status.running and agent.turn_open
    await agent.steer(message("steer now"))
    await agent.followup(message("queue later"))
    provider.gate.set()
    await drive
    claims = [
        event.payload
        for event in agent.state.session.events
        if isinstance(event.payload, StepClaimed)
    ]
    described = [
        (claim.turn, claim.step, claim.target.value, claim.claimed[0].item.blocks[0].text)
        for claim in claims
    ]
    assert described == [
        (1, 1, "next_turn", "start"),
        (1, 2, "next_step", "steer now"),
        (2, 1, "next_turn", "queue later"),
    ]
    texts = [
        [b.text for i in ctx.items for b in i.blocks if b.kind == ContentBlockKind.text]
        for ctx in provider.contexts
    ]
    assert "steer now" in texts[1] and "queue later" not in texts[1]
    assert "queue later" in texts[2]
    assert turn_ends(agent) == [TurnEndReason.completed, TurnEndReason.completed]
    await agent.aclose()


async def test_pause_stops_at_step_seam_and_resume_continues_verbatim(home: Path, project: Path):
    provider = ScriptedProvider(
        [
            tool_call_response("c1", "bash", json.dumps({"command": "echo one"})),
            text_response("after resume"),
        ]
    )
    provider.gate = asyncio.Event()
    agent = Agent.create(provider, project)
    await agent.followup(message("go"))
    drive = asyncio.create_task(agent.drive())
    await provider.started.wait()
    agent.cancel(CancelCause.user_pause)
    assert agent.status == Status.pausing
    provider.gate.set()
    await drive
    assert agent.status == Status.paused
    assert turn_ends(agent) == [TurnEndReason.user_pause]
    assert step_ends(agent) == [StepEndReason.completed]
    assert provider.calls == 1
    # The tool ran; the model has not seen its result, so a step is owed.
    agent.resume()
    assert agent.status == Status.idle
    await agent.drive()
    assert provider.calls == 2
    assert turn_ends(agent) == [TurnEndReason.user_pause, TurnEndReason.completed]
    claims = [e.payload for e in agent.state.session.events if isinstance(e.payload, StepClaimed)]
    assert len(claims) == 1  # continuation has no synthetic user message
    second_roles = [item.role.value for item in provider.contexts[1].items]
    assert second_roles == ["user", "assistant", "tool"]
    await agent.aclose()


async def test_abort_during_stream_repairs_history(home: Path, project: Path):
    provider = ScriptedProvider([text_response("partial")])
    provider.gate = asyncio.Event()
    agent = Agent.create(provider, project)
    await agent.followup(message("go"))
    await agent.followup(message("queued"))
    drive = asyncio.create_task(agent.drive())
    await provider.started.wait()
    agent.cancel(CancelCause.user_abort)
    assert agent.status == Status.aborting
    await drive
    assert agent.status == Status.idle
    assert step_ends(agent) == [StepEndReason.user_abort]
    assert turn_ends(agent) == [TurnEndReason.user_abort]
    inbox = agent.state.session.inbox()
    assert inbox.next_turn == [] and inbox.next_step == []
    assert agent.cancel(CancelCause.user_abort) is None  # no-op when idle
    await agent.aclose()


async def test_abort_during_tool_marks_interrupted_and_skipped(home: Path, project: Path):
    provider = ScriptedProvider(
        [
            [
                StreamEvent(kind=StreamEventKind.tool_call_start, id="c1", name="bash"),
                StreamEvent(
                    kind=StreamEventKind.tool_call_delta,
                    id="c1",
                    text=json.dumps({"command": "sleep 30"}),
                ),
                StreamEvent(kind=StreamEventKind.tool_call_end, id="c1"),
                StreamEvent(kind=StreamEventKind.tool_call_start, id="c2", name="bash"),
                StreamEvent(
                    kind=StreamEventKind.tool_call_delta,
                    id="c2",
                    text=json.dumps({"command": "echo two"}),
                ),
                StreamEvent(kind=StreamEventKind.tool_call_end, id="c2"),
                StopReason.tool_use,
            ]
        ]
    )
    agent = Agent.create(provider, project)
    await agent.followup(message("go"))
    drive = asyncio.create_task(agent.drive())
    await provider.started.wait()
    await asyncio.sleep(0.3)
    agent.cancel(CancelCause.user_abort)
    await drive
    result = next(
        e.payload for e in agent.state.session.events if isinstance(e.payload, ToolResult)
    )
    origins = [(block.call_id, block.origin) for block in result.item.blocks]
    assert origins == [("c1", Origin.interrupted), ("c2", Origin.skipped)]
    assert turn_ends(agent) == [TurnEndReason.user_abort]
    await agent.aclose()


async def test_provider_error_is_contained_at_the_drive_boundary(home: Path, project: Path):
    provider = ScriptedProvider([provider_error("boom"), text_response("recovered")])
    agent = Agent.create(provider, project)
    await agent.followup(message("go"))
    await agent.followup(message("after"))
    with pytest.raises(AvaError) as info:
        await agent.drive()
    assert info.value.message == "boom"
    assert agent.status == Status.idle
    assert step_ends(agent) == [StepEndReason.provider_error]
    assert turn_ends(agent) == [TurnEndReason.provider_error]
    errors = [e.payload for e in agent.state.session.events if isinstance(e.payload, DriveError)]
    assert len(errors) == 1 and errors[0].message == "boom"
    # The queued follow-up stays pending for the next explicit drive.
    assert [m.item.blocks[0].text for m in agent.state.session.inbox().next_turn] == ["after"]
    await agent.drive()
    assert turn_ends(agent)[-1] == TurnEndReason.completed
    await agent.aclose()


async def test_reopen_restores_paused_state_and_repairs_a_killed_turn(home: Path, project: Path):
    provider = ScriptedProvider(
        [tool_call_response("c1", "bash", json.dumps({"command": "echo one"})), text_response("ok")]
    )
    provider.gate = asyncio.Event()
    agent = Agent.create(provider, project)
    path = agent.session_path
    await agent.followup(message("go"))
    drive = asyncio.create_task(agent.drive())
    await provider.started.wait()
    agent.cancel(CancelCause.user_pause)
    provider.gate.set()
    await drive
    await agent.aclose()
    reopened = Agent.reopen(ScriptedProvider([text_response("resumed")]), project, path)
    assert reopened.status == Status.paused
    reopened.resume()
    await reopened.drive()
    assert turn_ends(reopened)[-1] == TurnEndReason.completed
    await reopened.aclose()
    # A log whose process died mid-turn reopens balanced with exactly one interrupted closer.
    log = Log.open(path, OpenMode.read_only)
    plain_events = [event.payload for event in log.loaded_events]
    assert TurnEndReason.interrupted not in [
        p.reason for p in plain_events if isinstance(p, TurnEnd)
    ]


async def test_duplicate_driver_is_rejected(home: Path, project: Path):
    provider = ScriptedProvider([text_response("x")])
    provider.gate = asyncio.Event()
    agent = Agent.create(provider, project)
    await agent.followup(message("go"))
    drive = asyncio.create_task(agent.drive())
    await provider.started.wait()
    with pytest.raises(AvaError):
        await agent.drive()
    provider.gate.set()
    await drive
    with pytest.raises(AvaError):
        await agent.drive()  # nothing pending
    await agent.aclose()


async def test_compaction_seed_replaces_prefix_and_keeps_tail(home: Path, project: Path):
    long_text = "h" * 4000

    def summarize(context):
        # The summarization request ends with the checkpoint instruction.
        last = context.items[-1].blocks[0].text
        assert last.startswith("Condense the conversation")
        return text_response("## Goal\n- keep going\n\n## Next Steps\n- more\n")

    provider = ScriptedProvider(
        [
            text_response(long_text, usage=Usage(input=9000, output=10)),
            summarize,
            text_response("after compaction"),
        ]
    )
    agent = Agent.create(provider, project, CompactionOptions(enabled=True, threshold_percent=50))
    await agent.followup(message("first " + "x" * 3000))
    await agent.drive()
    await agent.followup(message("second"))
    await agent.drive()
    from ava.session import CompactionSeed

    seeds = [e.payload for e in agent.state.session.events if isinstance(e.payload, CompactionSeed)]
    assert len(seeds) == 1
    assert "## Files" in seeds[0].item.blocks[0].text
    context = agent.state.session.model_context()
    assert context.items[0] is seeds[0].item
    assert provider.contexts[2].items[0] is seeds[0].item
    assert not any(
        isinstance(e.payload, AssistantMessage) and e.payload.item is context.items[0]
        for e in agent.state.session.events
    )
    await agent.aclose()


async def test_status_watchers_and_effort_selection(home: Path, project: Path):
    provider = ScriptedProvider([text_response("x")])
    provider.gate = asyncio.Event()
    agent = Agent.create(provider, project)
    seen: list[tuple[str, bool]] = []
    unsubscribe = agent.watch_status(
        lambda status, turn_open: seen.append((status.value, turn_open))
    )
    await agent.followup(message("go"))
    drive = asyncio.create_task(agent.drive())
    await provider.started.wait()
    agent.cancel(CancelCause.user_pause)
    provider.gate.set()
    await drive
    agent.resume()
    assert seen == [
        ("running", False),
        ("running", True),
        ("pausing", True),
        ("pausing", False),
        ("paused", False),
        ("idle", False),
    ]
    unsubscribe()
    await agent.drive()
    assert seen[-1] == ("idle", False)  # nothing observed after unsubscribing
    provider.model_overrides[provider.selection.model] = ModelCapabilities(
        effort_values=["low", "high"]
    )
    agent.select_effort("high")
    assert agent.current_selection().effort == "high"
    with pytest.raises(AvaError):
        agent.select_effort("max")
    provider.selection.effort = "high"
    agent.select_effort(None)
    assert agent.current_selection().effort is None
    await agent.followup(message("apply cleared effort"))
    await agent.drive()
    assert provider.selection.effort is None
    await agent.aclose()
    assert provider.closed


async def test_reloading_credentials_closes_the_replaced_provider(
    home: Path, project: Path, monkeypatch: pytest.MonkeyPatch
):
    provider = ScriptedProvider([text_response("old")])
    replacement = ScriptedProvider([text_response("new")])
    agent = Agent.create(provider, project)
    monkeypatch.setattr(
        "ava.agent.agent.provider_from_environment", lambda *_args, **_kwargs: replacement
    )

    await agent.reload_credentials()

    assert provider.closed
    assert agent.state.provider is replacement
    await agent.aclose()
    assert replacement.closed
