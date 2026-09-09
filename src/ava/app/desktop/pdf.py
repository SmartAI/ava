"""Lazy PDF images, with rasterization isolated from Qt's process-wide PDF lock."""

from __future__ import annotations

import json
import os
import struct
import subprocess
import sys
from threading import Lock, Thread
from typing import IO
from uuid import uuid4

from PySide6.QtCore import QSize, QUrl
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
