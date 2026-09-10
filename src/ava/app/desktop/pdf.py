"""Lazy PDF images, with rasterization isolated from Qt's process-wide PDF lock."""

from __future__ import annotations

import json
import os
import struct
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Lock, Thread
from typing import IO
from uuid import uuid4

from PySide6.QtCore import Property, QObject, QSize, QUrl, Signal, Slot
from PySide6.QtGui import QImage
from PySide6.QtQuick import QQuickImageProvider


def _read(stream: IO[bytes], length: int) -> bytes:
    data = bytearray()
    while len(data) < length:
        part = stream.read(length - len(data))
        if not part:
            raise EOFError("PDF renderer exited")
        data.extend(part)
    return bytes(data)


def _stop(process: subprocess.Popen[bytes] | None) -> None:
    if process is None:
        return
    try:
        process.terminate()
    except OSError:
        pass

    def reap() -> None:
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        for stream in (process.stdin, process.stdout):
            if stream:
                try:
                    stream.close()
                except OSError:
                    pass  # Termination may interrupt a buffered request write.

    Thread(target=reap, name="ava-pdf-exit", daemon=True).start()


def _placeholder(size: QSize) -> QImage:
    result = QImage(1, 1, QImage.Format.Format_ARGB32)
    result.fill(0)
    size.setWidth(1)
    size.setHeight(1)
    return result


class PdfImages(QQuickImageProvider):
    def __init__(self) -> None:
        super().__init__(QQuickImageProvider.ImageType.Image,
                         QQuickImageProvider.Flag.ForceAsynchronousImageLoading)
        self._sources: dict[str, tuple[str, str]] = {}
        self._lock = Lock()
        self._render_lock = Lock()
        self._process: subprocess.Popen[bytes] | None = None
        self._render_key = ""
        self._closed = False

    def add(self, source: str, password: str) -> str:
        key = uuid4().hex
        with self._lock:
            if self._closed:
                return ""
            self._sources[key] = (QUrl(source).toLocalFile(), password)
        return key

    def remove(self, key: str) -> None:
        process = None
        with self._lock:
            self._sources.pop(key, None)
            if not self._sources or key == self._render_key:
                process, self._process = self._process, None
        _stop(process)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._sources.clear()
            process, self._process = self._process, None
        _stop(process)

    def requestImage(self, id: str, size: QSize, requestedSize: QSize) -> QImage:
        key, _, location = id.partition("/")
        page = location.partition("/")[0]
        # Qt calls this off the GUI thread. Only one framed request is in flight;
        # closing its preview terminates it without waiting on the GUI.
        with self._render_lock:
            process = None
            try:
                with self._lock:
                    source = self._sources.get(key)
                    if source is None or not page.isdecimal():
                        return _placeholder(size)
                    if self._process is None:
                        self._process = subprocess.Popen(
                            [sys.executable, "-m", "ava.app.desktop._pdf_worker"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
                        )
                    process = self._process
                    self._render_key = key
                request = json.dumps([*source, int(page), max(1, min(4096, requestedSize.width())),
                                      max(1, min(4096, requestedSize.height()))]).encode() + b"\n"
                if len(request) > 65536:
                    return QImage()
                assert process.stdin and process.stdout
                process.stdin.write(request)
                process.stdin.flush()
                width, height, length = struct.unpack("!III", _read(process.stdout, 12))
                if width == height == length == 0:
                    return QImage()
                if not (0 < width <= 4096 and 0 < height <= 4096 and length == width * height * 4):
                    raise ValueError("Invalid PDF image dimensions")
                pixels = _read(process.stdout, length)
                result = QImage(pixels, width, height, width * 4, QImage.Format.Format_RGBA8888).copy()
                size.setWidth(width)
                size.setHeight(height)
                return result
            except (OSError, EOFError, ValueError):
                with self._lock:
                    canceled = key not in self._sources
                    if self._process is not process:
                        process = None  # Its owner already requested cleanup.
                    else:
                        self._process = None
                _stop(process)
                return _placeholder(size) if canceled else QImage()
            finally:
                with self._lock:
                    self._render_key = ""


class PdfSource(QObject):
    """Copy on a worker so Qt never parses a file another process can truncate."""

    changed = Signal()
    _completed = Signal(object, str)

    def __init__(self, source: str, parent: QObject) -> None:
        super().__init__(parent)
        self._closed = Event()
        self._temporary: TemporaryDirectory | None = None
        self._source = ""
        self._error = ""
        self._completed.connect(self._loaded)
        Thread(target=self._copy, args=(source,), name="ava-pdf-source", daemon=True).start()

    @Property(str, notify=changed)
    def source(self) -> str:
        return self._source

    @Property(str, notify=changed)
    def error(self) -> str:
        return self._error

    def _copy(self, source: str) -> None:
        temporary = TemporaryDirectory(prefix="ava-pdf-")
        error = ""
        try:
            path = Path(QUrl(source).toLocalFile())
            with path.open("rb") as incoming, (Path(temporary.name) / "document.pdf").open("wb") as output:
                before = os.fstat(incoming.fileno())
                header = incoming.read(1024)
                incoming.seek(max(0, before.st_size - 65536))
                if b"%PDF-" not in header or b"%%EOF" not in incoming.read(65536):
                    raise ValueError("The file is incomplete or invalid.")
                incoming.seek(0)
                while not self._closed.is_set():
                    data = incoming.read(1024 * 1024)
                    if not data:
                        break
                    output.write(data)
                after = os.fstat(incoming.fileno())
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    raise ValueError("The file changed while opening. Refresh the preview.")
        except (OSError, ValueError) as failure:
            error = "Cannot open this PDF. " + str(failure)
        if self._closed.is_set():
            temporary.cleanup()
            return
        try:
            self._completed.emit(temporary, error)
        except RuntimeError:
            temporary.cleanup()

    @Slot(object, str)
    def _loaded(self, temporary: TemporaryDirectory, error: str) -> None:
        if self._closed.is_set():
            temporary.cleanup()
            return
        self._temporary = temporary
        self._error = error
        if not error:
            self._source = QUrl.fromLocalFile(str(Path(temporary.name) / "document.pdf")).toString()
        self.changed.emit()

    def close(self) -> None:
        self._closed.set()
        if self._temporary is not None:
            self._temporary.cleanup()
            self._temporary = None
