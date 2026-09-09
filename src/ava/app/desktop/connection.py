"""Qt HTTP and incremental SSE transport for the existing Ava API."""

from __future__ import annotations

import codecs
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

from ava.transport.sse import SseParser

MAX_REPLY_BYTES = 16 * 1024 * 1024


class Connection(QObject):
    received = Signal(dict)
    status = Signal(dict)
    disconnected = Signal(str)

    def __init__(self, port: int, token: str, parent: QObject | None = None, *, authority: str = "", prefix: str = "") -> None:
        super().__init__(parent)
        self._base = f"http://127.0.0.1:{port}"
        self._token = token
        self.prefix = prefix
        self._authority = authority
        self._network = QNetworkAccessManager(self)
        self._stream: QNetworkReply | None = None
        self._requests: set[QNetworkReply] = set()

    def _request(self, path: str) -> QNetworkRequest:
        if self.prefix:
            path = path.replace("/" + self.prefix, "/")
        request = QNetworkRequest(QUrl(self._base + path))
        request.setRawHeader(b"Authorization", f"Bearer {self._token}".encode())
        if self._authority:
            request.setRawHeader(b"Host", self._authority.encode("ascii"))
        request.setAttribute(
            QNetworkRequest.Attribute.RedirectPolicyAttribute,
            QNetworkRequest.RedirectPolicy.ManualRedirectPolicy,
        )
        return request

    def _identifiers(self, path: str, value: Any) -> Any:
        # Only API resource identities are scoped. Tool IDs, events and text remain untouched.
        if not self.prefix or not isinstance(value, dict):
            return value
        if path.startswith("/api/automations"):
            scoped = {**value, **{key: self.prefix + value[key] for key in ("id", "project_id", "chat_id", "automation_id") if value.get(key)}}
            for key in ("automations", "runs"):
                if key in scoped:
                    scoped[key] = [self._identifiers("/api/automations", row) for row in scoped[key]]
            return scoped
        if path == "/api/projects" and "projects" in value:
            return {**value, "projects": [self._identifiers("/api/projects", p) for p in value["projects"]]}
        if path == "/api/projects" and "id" in value:
            return {**value, "id": self.prefix + value["id"], "chats": [self._identifiers("/api/chats", c) for c in value["chats"]]}
        if path == "/api/chats" or (path.startswith("/api/chats/") and (path.count("/") == 3 or path.endswith("/review"))):
            return {**value, **{key: self.prefix + value[key] for key in ("id", "project_id", "automation_id", "automation_run") if key in value}}
        return value

    def call(
        self, method: str, path: str, body: dict[str, Any] | None, done: Callable[[Any, str], None]
    ) -> None:
        request = self._request(path)
        if body is not None and self.prefix:
            body = {**body, **{key: body[key].removeprefix(self.prefix) for key in ("project_id", "chat_id") if isinstance(body.get(key), str)}}
        request.setTransferTimeout(30_000)
        request.setHeader(QNetworkRequest.KnownHeaders.ContentTypeHeader, "application/json")
        reply = self._network.sendCustomRequest(
            request, method.encode(), json.dumps(body).encode() if body is not None else b""
        )
        self._requests.add(reply)
        data = bytearray()

        def read() -> None:
            if not reply.isOpen():
                return
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
            # Success headers can arrive before the socket closes mid-response.
            if not error and reply.error() != QNetworkReply.NetworkError.NoError:
                error = reply.errorString()
            if not error and payload is None and code != 204:
                error = "Ava returned an empty response. Reconnect and retry."
            if len(data) > MAX_REPLY_BYTES:
                error = "Ava returned a response larger than 16 MiB."
            reply.deleteLater()
            done(self._identifiers(path, payload), error)

        reply.readyRead.connect(read)
        reply.finished.connect(finished)

    def download(self, path: str, destination: Path, size: int, done: Callable[[str], None], progress: Callable[[int], None]) -> Callable[[], None]:
        """Stream into a private temporary file; never accumulate PDF bytes in RAM."""
        stream = destination.open("wb", buffering=0)
        request = self._request(path)
        request.setTransferTimeout(30_000)
        reply = self._network.get(request)
        reply.setReadBufferSize(256 * 1024)
        self._requests.add(reply)
        received = 0
        failure = ""
        last_percent = -1

        def read() -> None:
            nonlocal received, failure, last_percent
            if failure or not reply.isOpen():
                return
            code = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
            if code != 200:
                reply.readAll()
                return
            try:
                data = bytes(reply.readAll().data())
                received += len(data)
                if received > size:
                    raise ValueError("File changed. Reload the preview.")
                stream.write(data)
                percent = round(received * 100 / max(1, size))
                if percent != last_percent:
                    last_percent = percent
                    progress(percent)
            except (OSError, ValueError) as error:
                failure = str(error)
                reply.abort()

        def finished() -> None:
            read()
            self._requests.discard(reply)
            stream.close()
            code = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
            error = failure
            if not error and (code != 200 or reply.error() != QNetworkReply.NetworkError.NoError or received != size):
                error = "File changed. Reload the preview." if code == 409 else "Download interrupted. Reconnect and retry."
            if error:
                destination.unlink(missing_ok=True)
            reply.deleteLater()
            done(error)

        reply.readyRead.connect(read)
        reply.finished.connect(finished)
        return reply.abort

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
