import asyncio
import json
from contextlib import asynccontextmanager

import pytest

from ava.base import AvaError, CancelToken, ErrorKind
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


@pytest.mark.parametrize("mode", ["recover", "exhaust", "partial", "http_error", "terminal"])
async def test_stream_disconnect_recovery_and_diagnostics(mode, monkeypatch, caplog):
    monkeypatch.setattr("ava.transport.http.STREAM_RETRY_DELAYS", (0, 0))
    payload = b'data: {"type":"response.created"}\n\n'
    first = {
        "recover": (200, b": heartbeat\n\n", False),
        "exhaust": (200, b"", False),
        "partial": (200, payload, False),
        "http_error": (401, b"secret-response", False),
        "terminal": (200, b'data: {"type":"response.completed"}\n\n', False),
    }[mode]
    responses = [first, (200, payload, True)] if mode == "recover" else [first]
    async with chunked_peer(responses) as (client, request, requests):
        events = []
        if mode == "recover":
            assert (await client.post_sse(request, events.append)).status == 200
            assert len(requests) == 2
            assert len(events) == 1
            assert requests == [b"secret-body"] * 2
        else:
            with pytest.raises(AvaError, match="incomplete chunked read") as caught:
                await client.post_sse(request, events.append)
            assert len(requests) == (3 if mode == "exhaust" else 1)
            assert len(events) == (1 if mode in ("partial", "terminal") else 0)
            detail = json.loads(caught.value.detail)
            assert detail["request_id"] == "fixture-request"
            assert detail["http_version"] == "HTTP/1.1"
            assert detail["bytes_received"] == len(first[1])
            assert detail["terminal_received"] == (mode == "terminal")
            assert detail["elapsed_ms"] >= 0
        assert "secret" not in caplog.text


async def test_cancel_during_stream_retry_backoff(monkeypatch):
    monkeypatch.setattr("ava.transport.http.STREAM_RETRY_DELAYS", (60, 60))
    async with chunked_peer([(200, b"", False)]) as (client, request, requests):
        token = CancelToken()
        task = asyncio.create_task(client.post_sse(request, lambda event: None, token))
        async with asyncio.timeout(2):
            while not requests:
                await asyncio.sleep(0.001)
            await asyncio.sleep(0.05)
            token.cancel()
            with pytest.raises(AvaError) as caught:
                await task
        assert caught.value.kind == ErrorKind.cancelled
        assert len(requests) == 1


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
