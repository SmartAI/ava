"""Retry golden cases: recover safely, fail within budget, and never replay tools."""
from __future__ import annotations

import asyncio
import json
import ssl
from contextlib import asynccontextmanager

import httpx
import pytest

from ava.agent import Agent, CompactionOptions
from ava.base import AvaError, CancelToken, ErrorKind
from ava.llm import Selection
from ava.llm.openai import OpenAIProvider
from ava.session import AssistantMessage, RequestRetry, Usage
from ava.session.codec import decode_record, encode_record
from ava.tool import make_write_tool
from tests.conftest import message


def sse(value):
    return ('data: ' + json.dumps(value) + '\n\n').encode()


def tool_reply(path, call_id):
    return sse({'choices': [{'index': 0, 'delta': {'tool_calls': [{
        'index': 0, 'id': call_id, 'type': 'function', 'function': {
            'name': 'write', 'arguments': json.dumps({'path': path, 'content': 'once'})},
    }]}}]}) + sse({'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'tool_calls'}]})


@asynccontextmanager
async def provider_peer(peer):
    provider = OpenAIProvider(Selection('openai', 'fixture'), 'https://fixture.invalid', 'fixture-key')
    await provider._transport._client.aclose()
    provider._transport._client = httpx.AsyncClient(transport=httpx.MockTransport(peer))
    try:
        yield provider
    finally:
        await provider.aclose()


@pytest.mark.parametrize('partial', [False, True])
async def test_retry_rebuilds_response_and_executes_only_successful_tools(home, project, monkeypatch, partial):
    monkeypatch.setattr('ava.agent.step.RETRY_DELAYS', (0, 0))
    requests = []

    class BrokenStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield sse({'choices': [{'index': 0, 'delta': {'content': 'DISCARDED OUTPUT'}}]})
            yield tool_reply('must-not-exist.txt', 'discarded-call')
            raise ssl.SSLError(ssl.SSL_ERROR_SSL, 'ssl/tls alert bad record mac')

    async def peer(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            body = tool_reply('earlier.txt', 'earlier-call') + b'data: [DONE]\n\n'
        elif len(requests) == 2:
            if partial:
                return httpx.Response(200, stream=BrokenStream())
            raise httpx.ReadTimeout('temporarily unavailable')
        elif len(requests) == 3:
            body = tool_reply('later.txt', 'later-call') + b'data: [DONE]\n\n'
        else:
            body = sse({'choices': [{'index': 0, 'delta': {'content': 'Done'}}]}) + b'data: [DONE]\n\n'
        return httpx.Response(200, content=body)

    async with provider_peer(peer) as provider:
        async with Agent.create(provider, project, CompactionOptions(enabled=False), tools=[make_write_tool(project)]) as agent:
            await agent.followup(message('Write the two files'))
            await agent.drive()
            events = agent.state.session.events
            retry, = [e for e in events if isinstance(e.payload, RequestRetry)]
            assert decode_record(encode_record(retry)).payload == retry.payload
            assert retry.payload.next_attempt == 2
            assert len(requests) == 4
            assert requests[1] == requests[2], 'Retries must use the same logical input'
            assert 'DISCARDED OUTPUT' not in json.dumps(requests[2:])
            assert 'must-not-exist' not in json.dumps(requests[2:])
            assert (project / 'earlier.txt').read_text() == 'once'
            assert (project / 'later.txt').read_text() == 'once'
            assert not (project / 'must-not-exist.txt').exists()
            calls = [b.call_id for e in events if isinstance(e.payload, AssistantMessage)
                     for b in e.payload.item.blocks if b.kind.value == 'tool_call']
            assert calls == ['earlier-call', 'later-call']
            unknown = [e.payload for e in events if isinstance(e.payload, Usage) and e.payload.attempt_id == retry.payload.attempt_id]
            assert len(unknown) == 1 and unknown[0].input is None and unknown[0].output is None
            assert 'DISCARDED OUTPUT' not in str(agent.state.session.model_context())


@pytest.mark.parametrize('mode,expected', [
    ('exhaust', 3), ('certificate', 1), ('auth', 1), ('quota', 1),
    ('rate_limit', 3), ('unavailable', 3), ('long_retry_after', 1), ('unsafe_provider', 1),
])
async def test_retry_budget_and_nonretryable_failures(home, project, monkeypatch, mode, expected):
    monkeypatch.setattr('ava.agent.step.RETRY_DELAYS', (0, 0))
    requests = 0

    async def peer(request):
        nonlocal requests
        requests += 1
        if mode == 'certificate':
            raise ssl.SSLCertVerificationError(ssl.SSL_ERROR_SSL, 'certificate verify failed')
        if mode in ('auth', 'quota', 'rate_limit', 'unavailable', 'long_retry_after'):
            status = 401 if mode == 'auth' else 503 if mode == 'unavailable' else 429
            body = {'error': {'code': 'insufficient_quota'}} if mode == 'quota' else {}
            return httpx.Response(status, json=body, headers={'retry-after': '600' if mode == 'long_retry_after' else '0'})
        raise ssl.SSLError(ssl.SSL_ERROR_SSL, 'ssl/tls alert bad record mac')

    async with provider_peer(peer) as provider:
        if mode == 'unsafe_provider':
            provider.request_retries_safe = False
        async with Agent.create(provider, project) as agent:
            await agent.followup(message('Proceed'))
            with pytest.raises(AvaError):
                await agent.drive()
            assert requests == expected
            assert len([e for e in agent.state.session.events if isinstance(e.payload, RequestRetry)]) == expected - 1
            assert agent.state.session.events[-1].payload.kind == 'drive/error'


@pytest.mark.parametrize('mode', ['cancel', 'deadline'])
async def test_retry_wait_is_cancellable_and_recovery_time_is_bounded(home, project, monkeypatch, mode):
    from ava.agent.step import step
    from ava.llm import Context

    monkeypatch.setattr('ava.agent.step.RETRY_DELAYS', (60, 60) if mode == 'cancel' else (0, 0))
    monkeypatch.setattr('ava.agent.step.RETRY_WINDOW_SECONDS', 300 if mode == 'cancel' else 0.02)
    attempts = 0
    entered = asyncio.Event()

    async def peer(request):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectError('transient')
        await asyncio.Event().wait()

    async with provider_peer(peer) as provider:
        async with Agent.create(provider, project) as agent:
            token = CancelToken()
            subscription = agent.subscribe(lambda e: entered.set() if isinstance(e.payload, RequestRetry) else None)
            task = asyncio.create_task(step(agent.state, Context(), 'test-request', True, token))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                if mode == 'cancel':
                    token.cancel()
                result = await asyncio.wait_for(task, 2)
                assert result.error.kind == (ErrorKind.cancelled if mode == 'cancel' else ErrorKind.timeout)
                assert attempts == (1 if mode == 'cancel' else 2)
            finally:
                subscription.close()
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)


async def test_unknown_usage_does_not_bypass_goal_token_budget(home, project, monkeypatch):
    from ava.agent.goal import current
    from tests.conftest import ScriptedProvider

    monkeypatch.setattr('ava.agent.step.RETRY_DELAYS', (0, 0))
    provider = ScriptedProvider([AvaError(ErrorKind.network, 'lost usage', retryable=True)])
    provider.request_retries_safe = True
    async with Agent.create(provider, project) as agent:
        await agent.goal_command('--tokens 100 -- Finish the task')
        with pytest.raises(AvaError, match='lost usage'):
            await agent.drive()
        assert provider.calls == 1
        assert current(agent.state).status.value == 'budget_limited'
        assert not current(agent.state).usage_complete
        assert not any(isinstance(e.payload, RequestRetry) for e in agent.state.session.events)


async def test_retry_recording_replays_without_live_provider_calls(home, project, monkeypatch):
    from ava.agent.recording import Recording, replay_recording
    from tests.conftest import ScriptedProvider, text_response

    monkeypatch.setattr('ava.agent.step.RETRY_DELAYS', (0, 0))
    provider = ScriptedProvider([AvaError(ErrorKind.network, 'transient', retryable=True), text_response('Done')])
    provider.request_retries_safe = True
    item = message('Finish the task')
    options = CompactionOptions(enabled=False)
    path = project / 'recording.jsonl'
    recording = Recording(path, provider, item, options)
    try:
        async with Agent.create(recording.provider(), project, options, tools=[]) as agent:
            await agent.followup(item)
            await agent.drive()
        recording.finish(None)
    finally:
        recording.close()
    result = await replay_recording(path)
    assert result['matched'] and result['exchanges'] == 2
    assert provider.calls == 2
