"""Goal lifecycle checks at the real Agent/session boundary (no model-quality claim)."""
from __future__ import annotations

import asyncio
import json

import pytest

from ava.agent import Agent, CancelCause, CompactionOptions
from ava.agent.goal import current
from ava.app.cli import _run_one_shot
from ava.base import AvaError
from ava.llm import Usage
from ava.session import CompactionSeed, Log, OpenMode
from ava.session.event import GoalChanged, GoalStatus, TurnStart
from tests.conftest import (
    ScriptedProvider,
    message,
    provider_error,
    text_response,
    tool_call_response,
)


def verdict(complete=False, progress=False, reason='Missing verified evidence.'):
    return text_response(json.dumps(dict(complete=complete, progress=progress, reason=reason)),
                         usage=Usage(input=3, output=2))


def agent_at(project, provider):
    return Agent.create(provider, project, CompactionOptions(enabled=False), system_prompt='Complete the task.')


async def test_goal_status_timing_uses_durable_events_and_excludes_pause(home, project, monkeypatch):
    from datetime import UTC, datetime, timedelta

    from ava.agent.goal import snapshot
    from ava.session.event import Event, TurnEnd, TurnEndReason

    async with agent_at(project, ScriptedProvider([text_response('done')])) as agent:
        start = datetime(2026, 9, 15, tzinfo=UTC)
        events = [
            (0, GoalChanged(id='g1', objective='Verify the build')),
            (1, TurnStart(turn=1)),
            (11, TurnEnd(turn=1, reason=TurnEndReason.user_pause)),
            (12, GoalChanged(id='g1', objective='Verify the build', status=GoalStatus.paused)),
            (100, GoalChanged(id='g1', objective='Verify the build')),
            (101, TurnStart(turn=2)),
            (121, TurnEnd(turn=2, reason=TurnEndReason.completed)),
            (125, GoalChanged(id='g1', objective='Verify the build', status=GoalStatus.complete)),
        ]
        # The projection uses only durable event times, independent of UI/reconnect clocks.
        agent.state.session.events[:] = [Event(i, start + timedelta(seconds=seconds), payload)
                                         for i, (seconds, payload) in enumerate(events)]
        result = snapshot(agent.state)
        assert result['elapsed_ms'] == 30_000
        assert result['timing_running'] is False
        assert result['status'] == 'complete'
        agent.state.session.events.append(Event(8, start + timedelta(seconds=130), GoalChanged(id='g2', objective='New goal')))
        assert snapshot(agent.state)['elapsed_ms'] == 0
        from ava.agent import Status

        agent.state.session.events.append(Event(9, start + timedelta(seconds=131), TurnStart(turn=3)))
        agent.state.drive_state.status = Status.running
        monkeypatch.setattr('ava.agent.goal.time.time', lambda: start.timestamp() + 136)
        assert snapshot(agent.state)['elapsed_ms'] == 5000
        assert snapshot(agent.state)['timing_running'] is True
        agent.state.drive_state.status = Status.idle


async def test_start_continue_verify_and_query(home, project):
    provider = ScriptedProvider([
        text_response('Not finished.', usage=Usage(input=5, output=2)), verdict(),
        tool_call_response('write-1', 'write', '{"path":"result.txt","content":"done"}'),
        text_response('Created result.txt.'), verdict(True, True, 'Write result proves the artifact exists.'),
    ])
    async with agent_at(project, provider) as agent:
        assert (await agent.goal_command())['start'] is False
        await agent.goal_command('Create result.txt containing done')
        await agent.drive()
        assert agent.goal.status == GoalStatus.complete
        assert agent.goal.turns == 2
        assert (project / 'result.txt').read_text() == 'done'
        assert provider.calls == 5
        assert provider.contexts[1].tools == []
        # The latest input after a tool call must remain its result, not another
        # "continue and recheck" instruction. A live CSV trial exposed this loop.
        assert provider.contexts[3].items[-1].role.value == 'tool'
        assert not (await agent.goal_command())['start']
        assert len([e for e in agent.state.session.events if isinstance(e.payload, TurnStart)]) == 2
        # A caller cannot mutate the persisted snapshot via the public property.
        agent.goal.objective = 'tampered'
        assert agent.goal.objective.startswith('Create')
        path = agent.session_path
    log = Log.open(path, OpenMode.read_only)
    try:
        goals = [e.payload for e in log.loaded_events if isinstance(e.payload, GoalChanged)]
        assert goals[-1].status == GoalStatus.complete
    finally:
        log.close()


async def test_false_verdict_cannot_bypass_required_check(home, project):
    provider = ScriptedProvider([text_response('Done!'), verdict(True)])
    async with agent_at(project, provider) as agent:
        await agent.goal_command('--check "test -f deliverable.txt" -- create deliverable.txt')
        await agent.drive()
        assert agent.goal.status == GoalStatus.blocked
        assert agent.goal.turns == 3
        assert 'Required check failed' in agent.goal.reason
        assert provider.calls == 6


async def test_no_progress_and_turn_budget(home, project):
    provider = ScriptedProvider([text_response('I will work next.'), verdict(False, True)])
    async with agent_at(project, provider) as agent:
        await agent.goal_command('Finish the migration')
        await agent.drive()
        assert agent.goal.status == GoalStatus.blocked
        assert agent.goal.no_progress == 3
        assert provider.calls == 6
        await agent.goal_command('resume')
        await agent.drive()
        assert agent.goal.turns == 6
        await agent.goal_command('--max-turns 1 -- Finish the migration')
        await agent.drive()
        assert agent.goal.status == GoalStatus.budget_limited
        assert agent.goal.turns == 1


@pytest.mark.parametrize('action', ['pause', 'clear', 'replacement objective'])
async def test_control_during_request_discards_stale_work(home, project, action):
    provider = ScriptedProvider([
        tool_call_response('stale', 'write', '{"path":"forbidden","content":"bad"}'),
        text_response('Nothing else required.'), verdict(True),
    ])
    async with agent_at(project, provider) as agent:
        await agent.goal_command('Original objective')
        provider.gate = asyncio.Event()
        driver = asyncio.create_task(agent.drive())
        await provider.started.wait()
        gate = provider.gate
        goal_id = agent.state.goal_turn_id
        with pytest.raises(AvaError, match='already running'):
            await agent.drive()
        assert agent.state.goal_turn_id == goal_id
        await agent.goal_command(action)
        gate.set()
        await driver
        assert not (project / 'forbidden').exists()
        if action == 'replacement objective':
            assert agent.goal.objective == action
            assert agent.goal.status == GoalStatus.complete
            assert agent.goal.turns == 1
        else:
            expected = GoalStatus.paused if action == 'pause' else GoalStatus.cleared
            assert agent.goal.status == expected
            assert provider.calls == 1


async def test_pause_clear_at_idle_notification_prevents_next_turn(home, project):
    provider = ScriptedProvider([text_response('Not yet.'), verdict()])
    async with agent_at(project, provider) as agent:
        await agent.goal_command('Do work')
        def pause_after_audit(event):
            if isinstance(event.payload, GoalChanged) and event.payload.no_progress == 1 and event.payload.status == GoalStatus.active:
                agent.cancel(CancelCause.user_pause)
        with agent.subscribe(pause_after_audit):
            await agent.drive()
        assert provider.calls == 2
        assert agent.goal.status == GoalStatus.paused
        await agent.goal_command('clear')
        assert agent.goal.status == GoalStatus.cleared


async def test_reopen_and_compaction_preserve_goal(home, project):
    provider = ScriptedProvider([])
    async with agent_at(project, provider) as agent:
        await agent.goal_command('Preserve the full objective and user constraints')
        path = agent.session_path
        original = agent.goal
        # Closing/reopening an active idle goal models a persisted-before-start crash.
    provider = ScriptedProvider([text_response('Checked.'), verdict(True)])
    async with Agent.reopen(provider, project, path, CompactionOptions(enabled=False)) as agent:
        assert agent.goal == original
        agent.state.acknowledge(CompactionSeed(covered_begin=0, covered_end=1,
            instruction='summary', item=message('Earlier context summarized.')))
        await agent.drive()
        assert agent.goal.status == GoalStatus.complete
        assert original.objective in provider.contexts[0].items[-1].blocks[0].text
    async with Agent.reopen(ScriptedProvider([]), project, path) as agent:
        assert agent.goal.status == GoalStatus.complete
        with pytest.raises(AvaError, match='without pending input'):
            await agent.drive()


async def test_crash_with_unreported_spend_fails_closed(home, project):
    from ava.session import StepStart

    async with agent_at(project, ScriptedProvider([])) as agent:
        await agent.goal_command('--tokens 100 -- Do the work')
        path = agent.session_path
        agent.state.acknowledge(TurnStart(turn=1))
        agent.state.acknowledge(StepStart(turn=1, step=1))
    async with Agent.reopen(ScriptedProvider([]), project, path) as agent:
        assert agent.goal.status == GoalStatus.budget_limited
        assert not agent.goal.usage_complete
        assert 'interrupted' in agent.goal.reason
        with pytest.raises(AvaError):
            await agent.goal_command('resume')


async def test_budget_includes_audit_and_blocks_next_tool(home, project):
    provider = ScriptedProvider([text_response('First turn', usage=Usage(input=5, output=1, reasoning=1)), verdict()])
    async with agent_at(project, provider) as agent:
        await agent.goal_command('--tokens 10 -- Finish it')
        await agent.drive()
        assert agent.goal.tokens_used == 12
        assert agent.goal.status == GoalStatus.budget_limited
        assert provider.calls == 2
    provider = ScriptedProvider([tool_call_response('write', 'write', '{"path":"forbidden","content":"bad"}')])
    async with agent_at(project, provider) as agent:
        await agent.goal_command('--tokens 10 -- Finish it')
        await agent.drive()
        assert agent.goal.status == GoalStatus.budget_limited
        assert not agent.goal.usage_complete
        assert not (project / 'forbidden').exists()


@pytest.mark.parametrize('response', [provider_error(), text_response('not JSON')])
async def test_audit_failure_stops_safely(home, project, response):
    provider = ScriptedProvider([text_response('Done'), response])
    async with agent_at(project, provider) as agent:
        await agent.goal_command('Finish it')
        await agent.drive()
        assert agent.goal.status == GoalStatus.blocked
        assert 'audit failed' in agent.goal.reason
        assert provider.calls == 2


async def test_worker_error_blocks_and_headless_commands(home, project, capsys):
    provider = ScriptedProvider([provider_error('Authentication failed')])
    async with agent_at(project, provider) as agent:
        await agent.goal_command('Finish it')
        with pytest.raises(AvaError, match='Authentication failed'):
            await agent.drive()
        assert agent.goal.status == GoalStatus.blocked
        assert (await agent.goal_command())['start'] is False
    provider = ScriptedProvider([text_response('Verified.'), verdict(True)])
    async with agent_at(project, provider) as agent:
        assert await _run_one_shot(agent, message('/goal Check the current state')) == 0
        assert agent.goal.status == GoalStatus.complete
        assert await _run_one_shot(agent, message('/goal')) == 0
        assert provider.calls == 2
        assert 'Goal complete' in capsys.readouterr().err


async def test_awaited_job_blocks_continuation_and_pause_stops_batch(home, project):
    from ava.llm import ToolDef
    from ava.tool import Output, Tool

    started, finish = asyncio.Event(), asyncio.Event()
    calls = []
    async def job(arguments, cancel):
        calls.append(arguments)
        started.set()
        await finish.wait()
        return Output('Job finished')

    script = tool_call_response('job', 'job', '{}')
    script += tool_call_response('forbidden', 'job', '{}')
    provider = ScriptedProvider([script])
    async with Agent.create(provider, project, tools=[Tool(ToolDef('job', 'Controlled awaited job'), job)],
                            system_prompt='Complete the task.') as agent:
        await agent.goal_command('Run the job')
        driver = asyncio.create_task(agent.drive())
        await started.wait()
        assert provider.calls == 1 and len(calls) == 1
        await agent.goal_command('pause')
        with pytest.raises(AvaError, match='in-flight'):
            await agent.goal_command('resume')
        finish.set()
        await driver
        assert len(calls) == 1 and provider.calls == 1
        assert agent.goal.status == GoalStatus.paused


async def test_tool_loop_cannot_evade_goal_turn_limit(home, project):
    (project / 'input.txt').write_text('Already known evidence.')
    provider = ScriptedProvider([lambda context: tool_call_response(
        f'read-{len(context.items)}', 'read', json.dumps({'path': 'input.txt'}))])
    async with agent_at(project, provider) as agent:
        await agent.goal_command('Finish without looping on the same evidence')
        await agent.drive()
        assert provider.calls == 50
        assert agent.goal.turns == 1
        assert agent.goal.status == GoalStatus.budget_limited
        assert '50-step' in agent.goal.reason


async def test_auditor_usage_does_not_replace_worker_context_measurement(home, project):
    provider = ScriptedProvider([text_response('Done', usage=Usage(input=50, output=5)), verdict(True)])
    async with agent_at(project, provider) as agent:
        await agent.goal_command('Verify current state')
        await agent.drive()
        assert agent.context_report().measured_input_tokens == 50
        assert agent.goal.tokens_used == 60


async def test_reject_invalid_commands_and_ephemeral_goals(home, project):
    async with agent_at(project, ScriptedProvider([])) as agent:
        for value in ('--max-turns 0 -- x', '--tokens -1 -- x', '--check', '--unknown 3 -- x', 'resume'):
            with pytest.raises(AvaError):
                await agent.goal_command(value)
        assert current(agent.state) is None
    async with Agent(ScriptedProvider([]), project) as agent:
        with pytest.raises(AvaError, match='durable'):
            await agent.goal_command('Do work')
