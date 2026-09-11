"""Short-lived loopback OAuth callback on the desktop, including for SSH backends."""

from __future__ import annotations

import secrets
from urllib.parse import parse_qs, urlsplit

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtNetwork import QHostAddress, QTcpServer, QTcpSocket


class OAuthCallbackListener(QObject):
    received = Signal(dict)
    failed = Signal(str)

    def __init__(self, parent: QObject):
        super().__init__(parent)
        self.server = QTcpServer(self)
        self.server.newConnection.connect(self._accept)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._timeout)
        self.sockets: set[QTcpSocket] = set()
        self.state = ""
        self.port = 0

    def start(self, redirect_uri: str) -> str:
        self.close()
        try:
            parsed = urlsplit(redirect_uri)
            if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.path != "/oauth/callback"
                    or not parsed.port or parsed.username or parsed.password or parsed.query or parsed.fragment):
                raise ValueError
            self.port = parsed.port
        except ValueError:
            return "Configure a valid loopback OAuth callback URL first."
        if not self.server.listen(QHostAddress.SpecialAddress.LocalHost, self.port):
            return f"OAuth callback port {self.port} is in use. Close the other sign-in, or change the callback URL in settings and at your OAuth provider."
        self.timer.start(300_000)
        return ""

    def _timeout(self):
        self.close()
        self.failed.emit("OAuth sign-in timed out. Try again.")

    def close(self):
        self.server.close()
        self.timer.stop()
        self.state = ""
        for socket in list(self.sockets):
            socket.abort()

    def _accept(self):
        while self.server.hasPendingConnections():
            socket = self.server.nextPendingConnection()
            if socket is None:
                continue
            if len(self.sockets) >= 8:
                socket.abort()
                socket.deleteLater()
                continue
            self.sockets.add(socket)
            socket.setReadBufferSize(20_481)
            data = bytearray()
            deadline = QTimer(socket)
            deadline.setSingleShot(True)
            deadline.timeout.connect(lambda socket=socket: socket.abort())
            deadline.start(5000)

            def disconnected(socket=socket, deadline=deadline):
                deadline.stop()
                self.sockets.discard(socket)
                socket.deleteLater()

            socket.disconnected.connect(disconnected)
            socket.readyRead.connect(lambda socket=socket, data=data: self._read(socket, data))

    def _reply(self, socket: QTcpSocket, status: int, message: str):
        body = message.encode("utf-8")
        reason = "OK" if status == 200 else "Bad Request"
        socket.write((f"HTTP/1.1 {status} {reason}\r\nContent-Type: text/plain; charset=utf-8\r\n"
                      f"Content-Length: {len(body)}\r\nCache-Control: no-store\r\nReferrer-Policy: no-referrer\r\n"
                      "Content-Security-Policy: default-src 'none'\r\nConnection: close\r\n\r\n").encode() + body)
        socket.disconnectFromHost()

    def _read(self, socket: QTcpSocket, data: bytearray):
        data.extend(socket.readAll().data())
        if len(data) > 20_480:
            self._reply(socket, 400, "OAuth callback is too large.")
            return
        if b"\r\n\r\n" not in data:
            return
        try:
            lines = bytes(data).split(b"\r\n")
            method, target, protocol = lines[0].decode("ascii").split(" ")
            headers = dict(line.decode("ascii").split(":", 1) for line in lines[1:] if line)
            headers = {key.lower(): value.strip() for key, value in headers.items()}
            url = urlsplit(target)
            params = parse_qs(url.query, max_num_fields=10)
            if (method != "GET" or not protocol.startswith("HTTP/1.") or url.scheme or url.netloc
                    or url.path != "/oauth/callback" or headers.get("host") != f"127.0.0.1:{self.port}"
                    or any(len(values) != 1 for values in params.values()) or not self.state
                    or not secrets.compare_digest(params.get("state", [""])[0].encode(), self.state.encode())
                    or not (params.get("code") or params.get("error"))):
                raise ValueError
        except (ValueError, UnicodeError):
            self._reply(socket, 400, "This callback does not match an active Ava sign-in. Return to Ava and try again.")
            return
        payload = {key: params[key][0] for key in ("code", "state", "iss", "error") if key in params}
        self.sockets.discard(socket)  # Let the accepted reply drain before disposing of its socket.
        self._reply(socket, 200, "Authorization received. Return to Ava to finish signing in. You can close this tab.")
        self.close()
        self.received.emit(payload)
