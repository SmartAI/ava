"""Bounded HTTP requests, cancellation, TLS verification, and SSE delivery.

Error responses never reach the SSE parser: a 4xx/5xx body can be valid SSE framing, and
delivering those events would mutate the consumer's state before the response is classified.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

from ava.base import AvaError, CancelToken, ErrorKind
from ava.base.cancel import NEVER
from ava.transport.sse import SseEvent, SseParser

MAX_RESPONSE_BYTES = 16 * 1024 * 1024
CONNECT_TIMEOUT_SECONDS = 10.0
MODEL_DISCOVERY_TIMEOUT_SECONDS = 5.0

SseSink = Callable[[SseEvent], None]


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


def _transport_error(exception: Exception) -> AvaError:
    if isinstance(exception, httpx.TimeoutException):
        return AvaError(ErrorKind.timeout, f"provider request failed: {exception}")
    return AvaError(ErrorKind.network, f"provider request failed: {exception}")


class Client:
    """One async HTTP client per provider instance. TLS verification is never configurable."""

    def __init__(self) -> None:
        self._client = httpx.AsyncClient(verify=True, follow_redirects=False)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def post_sse(
        self, request: Request, sink: SseSink, cancel: CancelToken = NEVER
    ) -> Response:
        return await cancel.guard(self._transfer(request, "POST", sink, stream_timeout=True))

    async def post(self, request: Request, cancel: CancelToken = NEVER) -> Response:
        return await cancel.guard(self._transfer(request, "POST", None, stream_timeout=False))

    async def get(self, request: Request, cancel: CancelToken = NEVER) -> Response:
        return await cancel.guard(self._transfer(request, "GET", None, stream_timeout=False))

    async def _transfer(
        self, request: Request, method: str, sink: SseSink | None, *, stream_timeout: bool
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
        try:
            async with self._client.stream(
                method, request.url, headers=headers, content=content, timeout=timeout
            ) as response:
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
                            sink(event)
                    else:
                        body_parts.append(chunk)
                if decoder is not None and sink is not None:
                    tail = decoder.finish()
                    for event in parser.feed(tail) if tail else []:
                        sink(event)
                    for event in parser.finish():
                        sink(event)
                return Response(
                    status=response.status_code,
                    body=b"".join(body_parts).decode("utf-8", "replace"),
                )
        except AvaError:
            raise
        except httpx.HTTPError as exception:
            raise _transport_error(exception) from exception


class _Utf8Decoder:
    """Incremental UTF-8 decoding that keeps a split multibyte sequence across chunks."""

    def __init__(self) -> None:
        import codecs

        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")

    def feed(self, chunk: bytes) -> str:
        return self._decoder.decode(chunk)

    def finish(self) -> str:
        return self._decoder.decode(b"", final=True)
