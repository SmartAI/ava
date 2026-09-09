"""Interactive local/SSH PTYs with bounded asynchronous transport to xterm.js."""

from __future__ import annotations

import base64
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from time import monotonic

import psutil
from PySide6.QtCore import Property, QObject, QSocketNotifier, QTimer, Signal, Slot
from PySide6.QtGui import QGuiApplication


class TerminalSession(QObject):
    changed = Signal()
    outputReceived = Signal(str, int)
    pasteRequested = Signal(str)
    resetRequested = Signal()
    toggleRequested = Signal()
    closed = Signal()
    HIGH_WATER = 256 * 1024
    LOW_WATER = 64 * 1024
    INPUT_LIMIT = 1024 * 1024

    def __init__(self, root: str, parent: QObject, *, remote: dict | None = None, local_cwd: str = ""):
        super().__init__(parent)
        self._root = root
        self.remote = remote
        self._local_cwd = local_cwd or root
        self.project_id = ""
        self._columns, self._rows = 80, 24
        self._ready = False
        self._error = ""
        self._exit_code: int | None = None
        self._fd = -1
        self._process: subprocess.Popen | None = None
        self._reader: QSocketNotifier | None = None
        self._writer: QSocketNotifier | None = None
        self._inflight = 0
        self._pending = bytearray()
        self._closing = False
        self._closed = False
        self._targets: list[psutil.Process] = []
        self._close_deadline = 0.0
        self._poll = QTimer(self)
        self._poll.setInterval(100)
        self._poll.timeout.connect(self._check_process)
        self._kill_timer = QTimer(self)
        self._kill_timer.setSingleShot(True)
        self._kill_timer.setInterval(800)
        self._kill_timer.timeout.connect(self._kill)

    @Property(str, constant=True)
    def root(self) -> str:
        return self._root

    @Property(bool, notify=changed)
    def ready(self) -> bool:
        return self._ready

    @Property(bool, notify=changed)
    def running(self) -> bool:
        return self._process is not None and self._exit_code is None and not self._closing

    @Property(str, notify=changed)
    def status(self) -> str:
        if self._error:
            return self._error
        if self._closing:
            return "Closing…"
        if not self._ready and self._exit_code is None:
            return "Starting shell…"
        if self._exit_code is not None and self.remote:
            return f"SSH shell closed ({self._exit_code}). Open a new shell to reconnect."
        return "" if self._exit_code is None else f"Shell exited ({self._exit_code})"

    @Property(bool, notify=changed)
    def canReconnect(self) -> bool:
        return bool(self.remote and self._exit_code is not None and not self._closing)

    @Slot()
    def reconnect(self) -> None:
        if not self.canReconnect:
            return
        self._dispose_fd()
        self._process = None
        self._exit_code = None
        self._error = ""
        self._ready = False
        self.resetRequested.emit()
        self.start(self._columns, self._rows)

    @Slot(int, int)
    def start(self, columns: int, rows: int) -> None:
        if self._process or self._closing:
            return
        if os.name != "posix":
            self._error = "The integrated terminal currently requires macOS or Linux."
            self.changed.emit()
            return
        import termios

        slave = -1
        try:
            self._columns, self._rows = columns, rows
            if self.remote:
                from .ssh import workspace_command

                command = workspace_command(self.remote, self._root, "terminal")
            else:
                shell = shutil.which(os.environ.get("SHELL", "/bin/sh"))
                if not shell:
                    raise OSError("The configured shell could not be found.")
                command = [shell, "-l", "-i"]
            self._fd, slave = os.openpty()
            if self.remote:
                import tty

                # Preserve input typed during SSH startup and avoid a second local echo.
                tty.setraw(slave, termios.TCSANOW)
            termios.tcsetwinsize(slave, (max(2, min(rows, 500)), max(10, min(columns, 1000))))
            environment = dict(
                os.environ,
                TERM="xterm-256color",
                COLORTERM="truecolor",
                TERM_PROGRAM="Ava",
                PWD=self._local_cwd,
            )
            for name in ("COLUMNS", "LINES"):
                environment.pop(name, None)
            self._process = subprocess.Popen(
                [sys.executable, str(Path(__file__).with_name("_terminal_child.py")), *command],
                cwd=self._local_cwd,
                env=environment,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                start_new_session=True,
            )
            os.set_blocking(self._fd, False)
            self._reader = QSocketNotifier(self._fd, QSocketNotifier.Type.Read, self)
            self._reader.activated.connect(self._read)
            self._writer = QSocketNotifier(self._fd, QSocketNotifier.Type.Write, self)
            self._writer.setEnabled(False)
            self._writer.activated.connect(self._write)
            self._ready = True
            self._poll.start()
        except OSError as error:
            self._error = str(error)
            self._dispose_fd()
        finally:
            if slave >= 0:
                os.close(slave)
        self.changed.emit()

    @Slot()
    def _read(self) -> None:
        deadline = monotonic() + 0.004
        while self._fd >= 0 and self._inflight < self.HIGH_WATER and monotonic() < deadline:
            try:
                data = os.read(self._fd, min(32768, self.HIGH_WATER - self._inflight))
            except BlockingIOError:
                break
            except OSError:
                data = b""
            if not data:
                self._dispose_fd()
                break
            self._inflight += len(data)
            self.outputReceived.emit(base64.b64encode(data).decode("ascii"), len(data))
        if self._reader and self._inflight >= self.HIGH_WATER:
            self._reader.setEnabled(False)

    @Slot(int)
    def acknowledge(self, size: int) -> None:
        self._inflight = max(0, self._inflight - max(0, size))
        if self._reader and self._inflight <= self.LOW_WATER:
            self._reader.setEnabled(True)

    @Slot(str)
    def write(self, text: str) -> None:
        if self._fd < 0 or self._closing:
            return
        data = text.encode("utf-8")
        if len(self._pending) + len(data) > self.INPUT_LIMIT:
            self._error = "Paste is too large. Paste less than 1 MiB at a time."
            self.changed.emit()
            return
        self._pending.extend(data)
        self._write()

    @Slot()
    def _write(self) -> None:
        if self._fd < 0:
            return
        try:
            if self._pending:
                count = os.write(self._fd, self._pending)
                del self._pending[:count]
        except BlockingIOError:
            pass
        except OSError:
            self._dispose_fd()
        if self._writer:
            self._writer.setEnabled(bool(self._pending))

    @Slot(int, int)
    def resize(self, columns: int, rows: int) -> None:
        self._columns, self._rows = columns, rows
        if self._fd >= 0:
            import termios

            try:
                termios.tcsetwinsize(
                    self._fd, (max(2, min(rows, 500)), max(10, min(columns, 1000)))
                )
            except OSError:
                pass

    @Slot()
    def paste(self) -> None:
        self.pasteRequested.emit(QGuiApplication.clipboard().text())

    @Slot()
    def toggle(self) -> None:
        self.toggleRequested.emit()

    @Slot(str)
    def copy(self, text: str) -> None:
        QGuiApplication.clipboard().setText(text)

    def _dispose_fd(self) -> None:
        for notifier in (self._reader, self._writer):
            if notifier:
                notifier.setEnabled(False)
                notifier.deleteLater()
        self._reader = self._writer = None
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1
        self._pending.clear()

    @Slot()
    def _check_process(self) -> None:
        if self._process:
            result = self._process.poll()
            if result is None:
                return
            if self._exit_code is None:
                self._exit_code = result
                self.changed.emit()
        if self._closing and not self._kill_timer.isActive():
            for process in self._targets:
                try:
                    if (
                        process.is_running()
                        and process.status() != psutil.STATUS_ZOMBIE
                        and monotonic() < self._close_deadline
                    ):
                        return
                except psutil.Error:
                    pass
            self._finish_close()
        elif not self._closing:
            self._poll.stop()

    @Slot()
    def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._close_deadline = monotonic() + 2
        if self._process and self._process.poll() is None:
            try:
                shell = psutil.Process(self._process.pid)
                self._targets = [shell, *shell.children(recursive=True)]
            except psutil.NoSuchProcess:
                pass
            self._signal_processes(signal.SIGHUP)
            self._kill_timer.start()
            self._poll.start()
        else:
            self._finish_close()
        self._dispose_fd()

    def _signal_processes(self, sig: int) -> None:
        # Descendants may be in separate job-control groups. Process handles also
        # check creation times before signalling, protecting against PID reuse.
        for process in reversed(self._targets):
            try:
                process.send_signal(sig)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

    @Slot()
    def _kill(self) -> None:
        self._signal_processes(signal.SIGKILL)
        self._check_process()

    def _finish_close(self) -> None:
        if not self._closed:
            self._closed = True
            self._poll.stop()
            self.closed.emit()
