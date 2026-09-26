import asyncio
import json
import ssl
from contextlib import asynccontextmanager

import httpx
import pytest

from ava.base import AvaError, ErrorKind
from ava.transport import Client, Request, SseParser


@asynccontextmanager
async def chunked_peer(responses):
    requests = []

    async def handle(reader, writer):
        try:
            headers = await reader.readuntil(b"\r\n\r\n")
            length = next(int(line.split(b":", 1)[1]) for line in headers.split(b"\r\n")
                          if line.lower().startswith(b"content-length:"))
            requests.append(await reader.readexactly(length))
            status, payload, complete = responses[min(len(requests) - 1, len(responses) - 1)]
            writer.write(
                f"HTTP/1.1 {status} Test\r\n".encode()
                + b"Content-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n"
                + b"X-Request-ID: fixture-request\r\nConnection: close\r\n\r\n"
            )
            if payload:
                writer.write(f"{len(payload):x}\r\n".encode() + payload + b"\r\n")
            if complete:
                writer.write(b"0\r\n\r\n")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    client = Client()
    try:
        yield client, Request(
            url=f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/responses?secret=query",
            headers=[("authorization", "Bearer secret-token")], body="secret-body",
        ), requests
    finally:
        await client.aclose()
        server.close()
        await server.wait_closed()


@pytest.mark.parametrize("mode", ["exhaust", "partial", "http_error", "terminal"])
async def test_stream_disconnect_single_attempt_and_diagnostics(mode, caplog):
    payload = b'data: {"type":"response.created"}\n\n'
    first = {
        "exhaust": (200, b"", False),
        "partial": (200, payload, False),
        "http_error": (401, b"secret-response", False),
        "terminal": (200, b'data: {"type":"response.completed"}\n\n', False),
    }[mode]
    async with chunked_peer([first]) as (client, request, requests):
        events = []
        with pytest.raises(AvaError, match="incomplete chunked read") as caught:
            await client.post_sse(request, events.append)
        assert len(requests) == 1, "The agent owns retries, not the HTTP layer"
        assert caught.value.retryable == (mode in ("exhaust", "partial"))
        assert len(events) == (1 if mode in ("partial", "terminal") else 0)
        detail = json.loads(caught.value.detail)
        assert detail["request_id"] == "fixture-request"
        assert detail["http_version"] == "HTTP/1.1"
        assert detail["bytes_received"] == len(first[1])
        assert detail["terminal_received"] == (mode == "terminal")
        assert detail["elapsed_ms"] >= 0
        assert "secret" not in caplog.text


@pytest.mark.parametrize('mode', ['headers', 'partial', 'certificate', 'get', 'post'])
async def test_raw_ssl_errors_are_normalized_without_retry(mode, caplog):
    failure = (ssl.SSLCertVerificationError(ssl.SSL_ERROR_SSL, 'certificate verify failed')
               if mode == 'certificate' else ssl.SSLError(ssl.SSL_ERROR_SSL, 'ssl/tls alert bad record mac'))
    attempts = 0
    closed = False
    payload = b'data: {"type":"response.created"}\n\n'

    class BrokenStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield payload
            raise failure

        async def aclose(self):
            nonlocal closed
            closed = True

    async def peer(request):
        nonlocal attempts
        attempts += 1
        if mode == 'partial':
            return httpx.Response(200, stream=BrokenStream())
        raise failure

    client = Client()
    await client._client.aclose()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(peer))
    request = Request('https://provider.invalid/?secret=query',
                      headers=[('authorization', 'Bearer secret-token')], body='secret-body')
    events = []
    try:
        with pytest.raises(AvaError) as caught:
            if mode == 'get':
                await client.get(request)
            elif mode == 'post':
                await client.post(request)
            else:
                await client.post_sse(request, events.append)
        assert caught.value.kind == ErrorKind.network
        assert caught.value.retryable == (mode != 'certificate')
        assert caught.value.__cause__ is failure
        assert str(failure) in caught.value.message
        diagnostics = json.loads(caught.value.detail)
        assert diagnostics['exception_type'] == type(failure).__name__
        assert diagnostics['attempt'] == 1
        assert diagnostics['elapsed_ms'] >= 0
        assert diagnostics['bytes_received'] == (len(payload) if mode == 'partial' else 0)
        assert diagnostics['terminal_received'] is False
        assert len(events) == (1 if mode == 'partial' else 0)
        assert attempts == 1, 'TLS failures must not introduce retries or replay delivered events'
        if mode == 'partial':
            assert closed
            assert diagnostics['last_sse_event'] == 'response.created'
        assert 'secret' not in caplog.text
        assert 'secret' not in caught.value.detail
    finally:
        await client.aclose()


@pytest.mark.parametrize('header', ['3', 'date', 'invalid', 'NaN'])
async def test_retry_after_and_status_are_propagated_to_request_owner(header):
    from datetime import UTC, datetime, timedelta
    from email.utils import format_datetime

    value = format_datetime(datetime.now(UTC) + timedelta(seconds=3), usegmt=True) if header == 'date' else header
    client = Client()
    await client._client.aclose()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(429, headers={'retry-after': value}, json={})
    ))
    try:
        with pytest.raises(AvaError) as caught:
            await client.post_sse(Request('https://fixture.invalid'), lambda event: None)
        assert caught.value.kind == ErrorKind.rate_limited and caught.value.retryable
        if header == '3':
            assert caught.value.retry_after == 3
        elif header == 'date':
            assert 0 < caught.value.retry_after <= 3
        else:
            assert caught.value.retry_after is None
    finally:
        await client.aclose()


def test_wrapped_certificate_errors_are_not_transient():
    from ava.transport.http import _transport_error

    error = httpx.ConnectError('TLS handshake failed')
    error.__cause__ = ssl.SSLCertVerificationError(ssl.SSL_ERROR_SSL, 'certificate verify failed')
    assert not _transport_error(error).retryable
    assert not _transport_error(ssl.SSLError(ssl.SSL_ERROR_SSL, 'wrong version number')).retryable


def _feed_in_chunks(text: str, size: int) -> list:
    parser = SseParser()
    events = []
    for index in range(0, len(text), size):
        events.extend(parser.feed(text[index : index + size]))
    events.extend(parser.finish())
    return [(event.event, event.data) for event in events]


def test_sse_events_split_across_every_chunk_boundary():
    text = 'event: message\ndata: {"a":1}\n\ndata: first\ndata: second\n\n: comment\n\nevent: only\n\ndata: tail'
    expected = [("message", '{"a":1}'), ("", "first\nsecond"), ("", "tail")]
    for size in range(1, len(text) + 1):
        assert _feed_in_chunks(text, size) == expected, size


def test_sse_crlf_and_bom():
    assert _feed_in_chunks("﻿data: a\r\n\r\ndata: b\r\n\r\n", 3) == [("", "a"), ("", "b")]


def test_sse_split_crlf_across_chunks():
    parser = SseParser()
    events = parser.feed("data: x\r")
    assert events == []
    events += parser.feed("\n\r\n")
    assert [(e.event, e.data) for e in events] == [("", "x")]
