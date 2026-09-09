"""On-demand, authenticated previews of immutable images in conversation history."""

from __future__ import annotations

import re
import tempfile
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import Property, QObject, QUrl, Signal, Slot

from ava.base.images import IMAGE_BYTE_LIMIT

from .connection import Connection


class AttachmentPreview(QObject):
    changed = Signal()
    opened = Signal()

    def __init__(self, parent: QObject) -> None:
        super().__init__(parent)
        self._temp = tempfile.TemporaryDirectory(prefix="ava-tool-images-")
        self._cache: OrderedDict[tuple, Path] = OrderedDict()
        self._cancel: Callable[[], None] | None = None
        self._generation = 0
        self._request: tuple[Connection, str, dict] | None = None
        self._state = {"name": "", "url": "", "loading": False, "error": ""}

    @Property("QVariantMap", notify=changed)  # type: ignore[arg-type]
    def state(self) -> dict:
        return self._state

    def open(self, connection: Connection, chat: str, image: dict) -> None:
        self.close()
        path = image.get("path", "")
        size = image.get("byte_size", 0)
        if not isinstance(path, str) or not re.fullmatch(r"images/\d+/\d+/\d+", path):
            return
        if type(size) is not int or not 0 < size <= IMAGE_BYTE_LIMIT:
            return
        self._request = connection, chat, dict(image)
        self._state = {"name": image.get("display_path", "Image"), "url": "", "loading": True, "error": ""}
        self.changed.emit()
        self.opened.emit()
        key = connection, chat, path
        if key in self._cache:
            self._cache.move_to_end(key)
            self._state.update(url=QUrl.fromLocalFile(str(self._cache[key])).toString(), loading=False)
            self.changed.emit()
            return
        generation = self._generation
        destination = Path(self._temp.name) / f"image-{generation}"

        def finished(error: str) -> None:
            if generation != self._generation:
                destination.unlink(missing_ok=True)
                return
            self._cancel = None
            self._state.update(loading=False, error=error)
            if error:
                destination.unlink(missing_ok=True)
            else:
                self._cache[key] = destination
                while len(self._cache) > 4:
                    self._cache.popitem(last=False)[1].unlink(missing_ok=True)
                self._state["url"] = QUrl.fromLocalFile(str(destination)).toString()
            self.changed.emit()

        self._cancel = connection.download(f"/api/chats/{chat}/{path}", destination, size, finished, lambda _: None)

    @Slot()
    def retry(self) -> None:
        if self._request:
            self.open(*self._request)

    @Slot()
    def close(self) -> None:
        self._generation += 1
        if self._cancel:
            self._cancel()
            self._cancel = None
        self._state.update(url="", loading=False)
        self.changed.emit()

    def shutdown(self) -> None:
        self.close()
        self._request = None
        self._cache.clear()
        self._temp.cleanup()
