"""Qt HTTP and incremental SSE transport for the existing Ava API."""

from __future__ import annotations

import codecs
import json
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

from ava.transport.sse import SseParser

MAX_REPLY_BYTES = 16 * 1024 * 1024


class Connection(QObject):
    received = Signal(dict)
    status = Signal(dict)
    disconnected = Signal(str)

    def __init__(self, port: int, token: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._base = f"http://127.0.0.1:{port}"
        self._token = token
        self._network = QNetworkAccessManager(self)
        self._stream: QNetworkReply | None = None
        self._requests: set[QNetworkReply] = set()

    def _request(self, path: str) -> QNetworkRequest:
        request = QNetworkRequest(QUrl(self._base + path))
        request.setRawHeader(b"Authorization", f"Bearer {self._token}".encode())
        request.setAttribute(
            QNetworkRequest.Attribute.RedirectPolicyAttribute,
            QNetworkRequest.RedirectPolicy.ManualRedirectPolicy,
        )
        return request

    def call(
        self, method: str, path: str, body: dict[str, Any] | None, done: Callable[[Any, str], None]
    ) -> None:
        request = self._request(path)
        request.setTransferTimeout(30_000)
        request.setHeader(QNetworkRequest.KnownHeaders.ContentTypeHeader, "application/json")
        reply = self._network.sendCustomRequest(
            request, method.encode(), json.dumps(body).encode() if body is not None else b""
        )
        self._requests.add(reply)
        data = bytearray()

        def read() -> None:
            data.extend(reply.readAll().data())
            if len(data) > MAX_REPLY_BYTES:
                reply.abort()

        def finished() -> None:
            if reply not in self._requests:
                return
            read()
            self._requests.remove(reply)
            code = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute) or 0
            try:
                payload = json.loads(data) if data else None
                error = (
                    ""
                    if 200 <= code < 300
                    else (
                        payload.get("error", reply.errorString())
                        if isinstance(payload, dict)
                        else reply.errorString()
                    )
                )
            except (ValueError, UnicodeError):
                payload, error = None, "Ava returned an unreadable response."
            if len(data) > MAX_REPLY_BYTES:
                error = "Ava returned a response larger than 16 MiB."
            reply.deleteLater()
            done(payload, error)

        reply.readyRead.connect(read)
        reply.finished.connect(finished)

    def stream(self, chat_id: str, last_seq: int) -> None:
        self.close_stream()
        request = self._request(f"/api/chats/{chat_id}/events")
        request.setRawHeader(b"Accept", b"text/event-stream")
        if last_seq >= 0:
            request.setRawHeader(b"Last-Event-ID", str(last_seq).encode())
        reply = self._network.get(request)
        self._stream = reply
        parser = SseParser()
        decoder = codecs.getincrementaldecoder("utf-8")()

        def read() -> None:
            if reply is not self._stream:
                return
            raw = reply.readAll().data()
            code = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
            if code != 200:
                return
            try:
                for event in parser.feed(decoder.decode(raw)):
                    payload = json.loads(event.data)
                    if not isinstance(payload, dict):
                        raise ValueError("expected an object")
                    (self.status if event.event == "status" else self.received).emit(payload)
                if len(parser._pending) + len(parser._current.data) > MAX_REPLY_BYTES:
                    raise ValueError("event too large")
            except (ValueError, UnicodeError):
                self.close_stream()
                self.disconnected.emit("The event stream was unreadable. Reconnecting…")

        def finished() -> None:
            if reply is self._stream:
                read()
                if reply is self._stream:
                    self._stream = None
                    self.disconnected.emit("Connection lost. Reconnecting…")
            reply.deleteLater()

        reply.readyRead.connect(read)
        reply.finished.connect(finished)

    def close_stream(self) -> None:
        reply, self._stream = self._stream, None
        if reply is not None:
            reply.abort()

    def close(self) -> None:
        self.close_stream()
        requests, self._requests = self._requests, set()
        for reply in requests:
            reply.abort()
            reply.deleteLater()
