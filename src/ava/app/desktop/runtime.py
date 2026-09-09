"""Qt ownership of the backend process, including bounded shutdown."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, QTimer, Signal


class BackendProcess(QObject):
    ready = Signal(int, str)
    failed = Signal(str)
    stopped = Signal()

    def __init__(self, cwd: Path, arguments: list[str], parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.process = QProcess(self)
        self.process.setWorkingDirectory(str(cwd))
        self.process.setProgram(sys.executable)
        self.process.setArguments(["-m", "ava.app.desktop.backend", *arguments])
        self.process.readyReadStandardOutput.connect(self._read_ready)
        self.process.readyReadStandardError.connect(self._read_error)
        self.process.errorOccurred.connect(self._process_error)
        self.process.finished.connect(self._finished)
        self._deadline = QTimer(self)
        self._deadline.setSingleShot(True)
        self._deadline.timeout.connect(self._timeout)
        self._stdout = b""
        self._stderr = b""
        self._stopping = False
        self._ready = False

    def start(self) -> None:
        if self.process.state() != QProcess.ProcessState.NotRunning:
            return
        self._stdout = self._stderr = b""
        self._ready = self._stopping = False
        self.process.start()
        self._deadline.start(30_000)

    def _read_ready(self) -> None:
        data = self.process.readAllStandardOutput().data()
        if self._ready or self._stopping:
            return
        self._stdout += data
        if len(self._stdout) > 4096:
            self.failed.emit("The backend returned an invalid startup response.")
            self.stop()
        elif b"\n" in self._stdout:
            try:
                info = json.loads(self._stdout.split(b"\n", 1)[0])
                port, token = info["port"], info["token"]
                if not isinstance(port, int) or not 0 < port < 65536:
                    raise ValueError("invalid port")
                if not isinstance(token, str) or not token:
                    raise ValueError("missing token")
            except (ValueError, KeyError, TypeError):
                self.failed.emit("The backend returned an invalid startup response.")
                self.stop()
                return
            self._stdout = b""
            self._ready = True
            self._deadline.stop()
            self.ready.emit(port, token)

    def _read_error(self) -> None:
        self._stderr = (self._stderr + self.process.readAllStandardError().data())[-4096:]

    def _process_error(self, error: QProcess.ProcessError) -> None:
        if error == QProcess.ProcessError.FailedToStart:
            self._deadline.stop()
            self.failed.emit(f"Cannot start Ava: {self.process.errorString()}")
            self.stopped.emit()

    def _finished(self, *_: object) -> None:
        self._deadline.stop()
        self._read_error()
        if not self._stopping:
            detail = self._stderr.decode("utf-8", "replace").strip()
            self.failed.emit("Ava exited unexpectedly." + (f"\n{detail}" if detail else ""))
        self.stopped.emit()

    def _timeout(self) -> None:
        if self._stopping:
            self.failed.emit("Ava did not stop in time and was terminated.")
            self.process.kill()
        else:
            self.failed.emit("Ava did not become ready within 30 seconds.")
            self.stop()

    def stop(self) -> None:
        self._stopping = True
        if self.process.state() == QProcess.ProcessState.NotRunning:
            self._deadline.stop()
            self.stopped.emit()
            return
        self.process.closeWriteChannel()
        self._deadline.start(10_000)

    def ensure_stopped(self) -> None:
        """Final cleanup also covers QML load failure before the GUI event loop starts."""
        if self.process.state() != QProcess.ProcessState.NotRunning:
            self.stop()
            if not self.process.waitForFinished(11_000):
                self.process.kill()
                self.process.waitForFinished(2000)
