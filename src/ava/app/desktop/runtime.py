"""Asynchronous bootstrap of a persistent backend; the desktop owns only the helper."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, QTimer, Signal

from ava.app.backend_state import PROTOCOL_VERSION


class BackendProcess(QObject):
    ready = Signal(int, str)
    failed = Signal(str)
    stopped = Signal()
    progress = Signal(str)
    hostKeyChanged = Signal()

    def __init__(self, cwd: Path, parent: QObject | None = None, *, host: str = "") -> None:
        super().__init__(parent)
        self.process = QProcess(self)
        self.process.setWorkingDirectory(str(cwd))
        self.process.setProgram(sys.executable)
        self.process.setArguments(["-m", "ava.app.desktop.ssh", "--host", host] if host else ["-m", "ava.app.backend", "connect", "--project", str(cwd)])
        self.host = host
        self.info: dict = {}
        self.host_key: dict = {}
        self.retryable = True
        self.process.readyReadStandardOutput.connect(self._read_ready)
        self.process.readyReadStandardError.connect(self._read_error)
        self.process.errorOccurred.connect(self._process_error)
        self.process.finished.connect(self._finished)
        self._deadline = QTimer(self)
        self._deadline.setSingleShot(True)
        self._deadline.timeout.connect(self._timeout)
        self._stdout = b""
        self._stderr = b""
        self._error_line = b""
        self._trust_rejected = False
        self._stopping = False
        self._ready = False

    def start(self) -> None:
        if self.process.state() != QProcess.ProcessState.NotRunning:
            return
        self._stdout = self._stderr = b""
        self._error_line = b""
        self._trust_rejected = False
        self.retryable = True
        self._ready = self._stopping = False
        self.process.start()
        self._deadline.start(300_000 if self.host else 30_000)

    def _read_ready(self) -> None:
        data = self.process.readAllStandardOutput().data()
        if self._ready or self._stopping:
            return
        self._stdout += data
        while b"\n" in self._stdout:
            line, self._stdout = self._stdout.split(b"\n", 1)
            try:
                if len(line) > 4096:
                    raise ValueError("oversized response")
                info = json.loads(line)
                if not isinstance(info, dict):
                    raise ValueError("invalid response")
                if info.get("event") == "host_key":
                    if not self.host or self.host_key or not all(isinstance(info.get(key), str) for key in ("request", "host", "algorithm", "fingerprint")):
                        raise ValueError("invalid host verification")
                    if not re.fullmatch(r"[a-f0-9]{32}", info["request"]) or not re.fullmatch(r"SHA256:[A-Za-z0-9+/]{43}", info["fingerprint"]):
                        raise ValueError("invalid fingerprint")
                    self.host_key = info
                    self._deadline.start(180_000)
                    self.hostKeyChanged.emit()
                    continue
                port, token = info["port"], info["token"]
                if not isinstance(port, int) or not 0 < port < 65536:
                    raise ValueError("invalid port")
                if not isinstance(token, str) or not token:
                    raise ValueError("missing token")
                if info.get("protocol") != PROTOCOL_VERSION or not info.get("instance_id"):
                    raise ValueError("incompatible backend")
            except (ValueError, KeyError, TypeError):
                self.failed.emit("The backend returned an invalid startup response.")
                self.stop()
                return
            self._stdout = b""
            self.info = info
            self._ready = True
            self._deadline.stop()
            self.ready.emit(port, token)
            return
        if len(self._stdout) > 4096:
            self.failed.emit("The backend returned an invalid startup response.")
            self.stop()

    def answer_host_key(self, request: str, accept: bool) -> None:
        if self.host_key.get("request") != request or self._stopping:
            return
        self._trust_rejected = not accept
        self.process.write((json.dumps({"request": request, "accept": accept}) + "\n").encode())
        self._clear_host_key()
        self._deadline.start(300_000)

    def _clear_host_key(self) -> None:
        if self.host_key:
            self.host_key = {}
            self.hostKeyChanged.emit()

    def _read_error(self) -> None:
        data = bytes(self.process.readAllStandardError().data())
        self._error_line += data
        while b"\n" in self._error_line:
            line, self._error_line = self._error_line.split(b"\n", 1)
            if line.startswith(b"AVA_PROGRESS "):
                self.progress.emit(line.removeprefix(b"AVA_PROGRESS ").decode("utf-8", "replace"))
            else:
                self._stderr = (self._stderr + line + b"\n")[-4096:]
        self._error_line = self._error_line[-4096:]

    def _process_error(self, error: QProcess.ProcessError) -> None:
        if error == QProcess.ProcessError.FailedToStart:
            self._deadline.stop()
            self.failed.emit(f"Cannot start Ava: {self.process.errorString()}")
            self.stopped.emit()

    def _finished(self, exit_code: int, _: QProcess.ExitStatus) -> None:
        self._deadline.stop()
        self._read_ready()
        self._read_error()
        self._clear_host_key()
        if not self._stopping and (exit_code or not self._ready or self.host):
            detail = (self._stderr + self._error_line).decode("utf-8", "replace").strip()
            if self._trust_rejected:
                self.retryable = False
                message = "Connection cancelled. The SSH host was not trusted."
            elif "REMOTE HOST IDENTIFICATION HAS CHANGED" in detail or "REVOKED HOST KEY" in detail:
                self.retryable = False
                message = "The SSH host key has changed or was revoked. Verify it with the server administrator and update your SSH known_hosts entry before reconnecting."
            elif "incompatible protocol" in detail or "missing required capabilities" in detail:
                self.retryable = False
                message = "This backend is incompatible with this desktop. Existing tasks were not stopped. Use a compatible Ava version, or update the backend after tasks finish."
            else:
                self.retryable = not ("Host key verification failed" in detail or "Permission denied" in detail)
                detail = re.sub(r"^(?:ava-ssh: |ava-backend: )+", "", detail)
                message = "Could not connect to Ava." + (f"\n{detail}" if detail else "")
            self.failed.emit(message)
        if self._stopping or not self._ready or self.host:
            self.stopped.emit()

    def _timeout(self) -> None:
        if self._stopping:
            self.failed.emit("Ava's connection helper did not stop in time and was terminated.")
            self.process.kill()
        else:
            self.retryable = not bool(self.host_key)
            self.failed.emit("Host verification timed out. Reconnect to try again." if self.host_key else "The remote connection timed out. Check SSH and retry." if self.host else "Ava did not become ready within 30 seconds.")
            self.stop()

    def stop(self) -> None:
        self._stopping = True
        self._clear_host_key()
        if self.process.state() == QProcess.ProcessState.NotRunning:
            self._deadline.stop()
            self.stopped.emit()
            return
        self.process.terminate()
        self._deadline.start(2000)

    def ensure_stopped(self) -> None:
        """Reap the connection helper; running agents belong to the persistent backend."""
        if self.process.state() != QProcess.ProcessState.NotRunning:
            self.stop()
            if not self.process.waitForFinished(11_000):
                self.process.kill()
                self.process.waitForFinished(2000)
