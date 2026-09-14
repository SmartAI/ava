"""Bounded HTTP requests, cancellation, TLS verification, and SSE delivery.

Error responses never reach the SSE parser: a 4xx/5xx body can be valid SSE framing, and
delivering those events would mutate the consumer's state before the response is classified.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

from ava.base import AvaError, CancelToken, ErrorKind
from ava.base.cancel import NEVER
from ava.transport.sse import SseEvent, SseParser

MAX_RESPONSE_BYTES = 16 * 1024 * 1024
CONNECT_TIMEOUT_SECONDS = 10.0
MODEL_DISCOVERY_TIMEOUT_SECONDS = 30.0

SseSink = Callable[[SseEvent], None]
_LOG = logging.getLogger(__name__)
STREAM_RETRY_DELAYS = (1.0, 2.0)


@dataclass(slots=True)
class Request:
    url: str
    headers: list[tuple[str, str]] = field(default_factory=list)
    body: str = ""
    idle_timeout_seconds: float = 120.0


@dataclass(slots=True)
class Response:
    status: int = 0
    body: str = ""


class _HttpTransferError(AvaError):
    # Internal retry eligibility, distinct from AvaError.recoverable (drive semantics).
    retryable: bool = False


def _transport_error(exception: Exception) -> _HttpTransferError:
    if isinstance(exception, httpx.TimeoutException):
        return _HttpTransferError(ErrorKind.timeout, f"provider request failed: {exception}")
    return _HttpTransferError(ErrorKind.network, f"provider request failed: {exception}")


class Client:
    """One async HTTP client per provider instance. TLS verification is never configurable."""

    def __init__(self) -> None:
        self._client = httpx.AsyncClient(verify=True, follow_redirects=False)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def post_sse(
        self, request: Request, sink: SseSink, cancel: CancelToken = NEVER
    ) -> Response:
        cancel.raise_if_cancelled()
        return await cancel.guard(self._post_sse_with_retries(request, sink))

    async def _post_sse_with_retries(self, request: Request, sink: SseSink) -> Response:
        delivered = False

        def deliver(event: SseEvent) -> None:
            nonlocal delivered
            # Even metadata can mutate a provider adapter. Never replay after delivery.
            delivered = True
            sink(event)

        for attempt in range(len(STREAM_RETRY_DELAYS) + 1):
            try:
                return await self._transfer(
                    request, "POST", deliver, stream_timeout=True, attempt=attempt + 1
                )
            except _HttpTransferError as error:
                transient = isinstance(
                    error.__cause__,
                    (httpx.NetworkError, httpx.RemoteProtocolError, httpx.TimeoutException),
                )
                if (
                    not transient or not error.retryable or delivered
                    or attempt == len(STREAM_RETRY_DELAYS)
                ):
                    raise
                await asyncio.sleep(STREAM_RETRY_DELAYS[attempt])
        raise AssertionError("unreachable")

    async def post(self, request: Request, cancel: CancelToken = NEVER) -> Response:
        return await cancel.guard(self._transfer(request, "POST", None, stream_timeout=False))

    async def get(self, request: Request, cancel: CancelToken = NEVER) -> Response:
        return await cancel.guard(self._transfer(request, "GET", None, stream_timeout=False))

    async def _transfer(
        self, request: Request, method: str, sink: SseSink | None, *, stream_timeout: bool,
        attempt: int = 1,
    ) -> Response:
        if stream_timeout:
            # Idle timeout only: a total timeout kills healthy long streams.
            timeout = httpx.Timeout(
                connect=CONNECT_TIMEOUT_SECONDS,
                read=request.idle_timeout_seconds,
                write=request.idle_timeout_seconds,
                pool=CONNECT_TIMEOUT_SECONDS,
            )
        else:
            timeout = httpx.Timeout(
                MODEL_DISCOVERY_TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS
            )
        headers = dict(request.headers)
        content = request.body.encode("utf-8") if method == "POST" else None
        parser = SseParser()
        received = 0
        body_parts: list[bytes] = []
        started = time.monotonic()
        diagnostics: dict[str, object] = {"attempt": attempt}
        last_event: str | None = None
        terminal_received = False

        def deliver(event: SseEvent) -> None:
            nonlocal last_event, terminal_received
            # Never retain event payloads, URLs, or arbitrary response headers.
            kind = event.event
            try:
                value = json.loads(event.data)
            except (ValueError, TypeError):
                value = None
            if isinstance(value, dict) and isinstance(value.get("type"), str):
                kind = value["type"]
            last_event = kind[:80] if kind else None
            terminal_received |= kind in (
                "response.completed", "response.incomplete", "response.failed", "message_stop"
            ) or event.data.strip() == "[DONE]"
            assert sink is not None
            sink(event)

        try:
            async with self._client.stream(
                method, request.url, headers=headers, content=content, timeout=timeout
            ) as response:
                diagnostics.update(status=response.status_code, http_version=response.http_version)
                request_id = response.headers.get("x-request-id") or response.headers.get("request-id")
                if request_id:
                    diagnostics["request_id"] = request_id[:200]
                streaming = sink is not None and 200 <= response.status_code < 300
                decoder: _Utf8Decoder | None = _Utf8Decoder() if streaming else None
                async for chunk in response.aiter_bytes():
                    received += len(chunk)
                    if received > MAX_RESPONSE_BYTES:
                        raise AvaError(
                            ErrorKind.provider, "provider response exceeded the 16 MiB safety limit"
                        )
                    if decoder is not None and sink is not None:
                        for event in parser.feed(decoder.feed(chunk)):
                            deliver(event)
                    else:
                        body_parts.append(chunk)
                if decoder is not None and sink is not None:
                    tail = decoder.finish()
                    for event in parser.feed(tail) if tail else []:
                        deliver(event)
                    for event in parser.finish():
                        deliver(event)
                return Response(
                    status=response.status_code,
                    body=b"".join(body_parts).decode("utf-8", "replace"),
                )
        except AvaError:
            raise
        except httpx.HTTPError as exception:
            diagnostics.update(
                elapsed_ms=int((time.monotonic() - started) * 1000),
                bytes_received=received,
                last_sse_event=last_event,
                terminal_received=terminal_received,
                exception_type=type(exception).__name__,
            )
            detail = json.dumps(diagnostics, sort_keys=True)
            _LOG.warning("Provider HTTP attempt failed: %s", detail)
            error = _transport_error(exception)
            error.detail = detail
            status = diagnostics.get("status")
            error.retryable = status is None or (isinstance(status, int) and 200 <= status < 300)
            raise error from exception


class _Utf8Decoder:
    """Incremental UTF-8 decoding that keeps a split multibyte sequence across chunks."""

    def __init__(self) -> None:
        import codecs

        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")

    def feed(self, chunk: bytes) -> str:
        return self._decoder.decode(chunk)

    def finish(self) -> str:
        return self._decoder.decode(b"", final=True)
