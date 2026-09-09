"""A lazy Qt tree and temporary previews attached to one remote project."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlencode
from uuid import uuid4

from PySide6.QtCore import (
    Property,
    QAbstractItemModel,
    QModelIndex,
    QObject,
    Qt,
    QTemporaryDir,
    QTimer,
    Signal,
    Slot,
)

from .connection import Connection
from .files import inspect_path


@dataclass
class Entry:
    values: dict
    parent: Entry | None = None
    row: int = 0
    children: list[Entry] = field(default_factory=list)
    cursor: str = ""
    offset: int | None = 0
    loading: bool = False
    expanded: bool = False


class RemoteFiles(QAbstractItemModel):
    changed = Signal()
    previewReady = Signal(dict)
    _roles = {int(Qt.ItemDataRole.UserRole) + i: key.encode() for i, key in enumerate(("fileName", "filePath", "directory"))}

    def __init__(self, project: str, root: str, connection: Callable[[], Connection | None], parent: QObject) -> None:
        super().__init__(parent)
        self._project, self._root, self._connection = project, root, connection
        self._entry = Entry({"fileName": Path(root).name, "filePath": root, "directory": True})
        self._epoch = 0
        self._preview_epoch = 0
        self._closed = False
        self._error = ""
        self._pending = 0
        self._temporary = QTemporaryDir()
        self._cache: dict[str, Path] = {}
        self._cancel: Callable[[], None] | None = None
        self._preview_path = ""
        self._preview_loading = False

    @Property(QModelIndex, constant=True)
    def rootIndex(self) -> QModelIndex:
        return QModelIndex()

    @Property(bool, constant=True)
    def remote(self) -> bool:
        return True

    @Property(str, notify=changed)
    def error(self) -> str:
        return self._error

    @Property(bool, notify=changed)
    def loading(self) -> bool:
        return self._pending > 0

    def roleNames(self) -> dict:
        return self._roles

    def _node(self, index: QModelIndex) -> Entry:
        return index.internalPointer() if index.isValid() else self._entry

    def index(self, row, column, parent=QModelIndex()):  # noqa: B008
        node = self._node(parent)
        if column or row < 0 or row >= len(node.children):
            return QModelIndex()
        return self.createIndex(row, column, node.children[row])

    def parent(self, index=None):
        if index is None:
            return super().parent()
        node = self._node(index).parent
        return self.createIndex(node.row, 0, node) if node is not None and node is not self._entry else QModelIndex()

    def columnCount(self, parent=QModelIndex()):  # noqa: B008
        return 1

    def rowCount(self, parent=QModelIndex()):  # noqa: B008
        return len(self._node(parent).children)

    def data(self, index, role=int(Qt.ItemDataRole.DisplayRole)):
        node = self._node(index)
        key = self._roles.get(role, b"fileName" if role == int(Qt.ItemDataRole.DisplayRole) else b"")
        return node.values.get(key.decode())

    def hasChildren(self, parent=QModelIndex()):  # noqa: B008
        node = self._node(parent)
        return bool(node.children) or bool(node.values["directory"] and node.offset is not None)

    def canFetchMore(self, parent):
        node = self._node(parent)
        return not self._closed and bool(node.values["directory"] and not node.loading and node.offset is not None)

    def fetchMore(self, parent):
        if not self.canFetchMore(parent):
            return
        node = self._node(parent)
        connection = self._connection()
        if connection is None:
            node.offset = None
            self._error = "Machine offline. Reconnect, then reload the file tree."
            self.changed.emit()
            return
        node.loading = True
        node.expanded = True
        self._pending += 1
        self._error = ""
        self.changed.emit()
        epoch = self._epoch

        def completed(payload, error):
            if self._closed or epoch != self._epoch:
                return
            node.loading = False
            self._pending -= 1
            if error:
                self._error = error
                node.offset = None
            else:
                node.cursor, node.offset = payload["cursor"], payload["next"]
                rows = payload["entries"]
                if rows:
                    index = self.createIndex(node.row, 0, node) if node is not self._entry else QModelIndex()
                    start = len(node.children)
                    self.beginInsertRows(index, start, start + len(rows) - 1)
                    node.children.extend(Entry(row, node, start + i) for i, row in enumerate(rows))
                    self.endInsertRows()
            self.changed.emit()
            # TreeView requests the initial page when expanding a directory;
            # subsequent pages arrive on separate event-loop turns. Collapsing
            # it stops paging, while the view still creates only visible rows.
            if node.offset is not None:
                def next_page():
                    if not self._closed and epoch == self._epoch and node.expanded:
                        index = self.createIndex(node.row, 0, node) if node is not self._entry else QModelIndex()
                        self.fetchMore(index)

                QTimer.singleShot(0, self, next_page)

        connection.call("GET", self._url("files", path=node.values["filePath"], cursor=node.cursor, offset=node.offset), None, completed)

    @Slot(QModelIndex, bool)
    def setExpanded(self, index: QModelIndex, expanded: bool) -> None:
        self._node(index).expanded = expanded
        if expanded:
            self.fetchMore(index)

    def _url(self, suffix: str, **query) -> str:
        return f"/api/projects/{self._project}/{suffix}?" + urlencode({**query, "workspace": self._root})

    @Slot()
    def refresh(self) -> None:
        self._epoch += 1
        self._pending = 0
        self.beginResetModel()
        self._entry = Entry({"fileName": Path(self._root).name, "filePath": self._root, "directory": True})
        self.endResetModel()
        self.fetchMore(QModelIndex())

    @Slot(str)
    def preview(self, path: str) -> None:
        self._preview_epoch += 1
        epoch = self._preview_epoch
        if self._cancel:
            cancel, self._cancel = self._cancel, None
            cancel()
        self._preview_path = path
        self._preview_loading = True
        connection = self._connection()

        def state(notice: str, kind: str = "loading", **values) -> None:
            if not self._closed and epoch == self._preview_epoch:
                self._preview_loading = kind == "loading"
                self.previewReady.emit({"path": path, "name": Path(path).name, "kind": kind, "notice": notice, **values})

        if connection is None:
            state("Machine offline. Reconnect, then reload the preview.", "unsupported")
            return
        state("Loading preview…")

        def metadata_ready(metadata, error):
            if self._closed or epoch != self._preview_epoch:
                return
            if error:
                state(error, "unsupported")
                return
            if metadata["kind"] == "unsupported":
                state(metadata["notice"], "unsupported")
                return
            key = hashlib.sha256((path + metadata["version"]).encode()).hexdigest()
            target = self._cache.get(key) or Path(self._temporary.path()) / (uuid4().hex + Path(path).suffix)

            def ready(error):
                if self._closed or epoch != self._preview_epoch:
                    if target not in self._cache.values():
                        target.unlink(missing_ok=True)
                    return
                self._cancel = None
                if error:
                    state(error, "unsupported")
                    return
                try:
                    result = inspect_path(target.parent, target)
                    result.update(path=path, name=metadata["name"], relative=metadata["relative"], attachmentPath=str(target))
                    self._cache.pop(key, None)
                    self._cache[key] = target
                    # Each explorer retains at most four validated previews. The
                    # active PDF is kept until the tab itself is released.
                    while len(self._cache) > 4 or sum(p.stat().st_size for p in self._cache.values()) > 512 * 1024 * 1024:
                        self._cache.pop(next(iter(self._cache))).unlink(missing_ok=True)
                    self._preview_loading = False
                    self.previewReady.emit(result)
                except (OSError, ValueError) as error:
                    state(str(error), "unsupported")

            if key in self._cache:
                ready("")
            else:
                try:
                    if not self._temporary.isValid():
                        raise OSError("Cannot create a temporary file for this preview.")
                    self._cancel = connection.download(self._url("file/content", path=path, version=metadata["version"]), target, metadata["size"], ready,
                                                       lambda percent: state(f"Downloading preview… {percent}%", progress=percent))
                except OSError as error:
                    state(str(error), "unsupported")

        connection.call("GET", self._url("file", path=path), None, metadata_ready)

    @Slot()
    def connectionChanged(self) -> None:
        if self._connection() is None:
            self._epoch += 1
            self._pending = 0
            self._error = "Machine offline. Reconnect, then reload the file tree."
            if self._preview_loading:
                self._preview_epoch += 1
                if self._cancel:
                    cancel, self._cancel = self._cancel, None
                    cancel()
                self._preview_loading = False
                self.previewReady.emit({"path": self._preview_path, "kind": "unsupported", "notice": "Download interrupted. Reconnect and retry."})
            self.changed.emit()

    def close(self) -> None:
        self._closed = True
        self._preview_epoch += 1
        if self._cancel:
            cancel, self._cancel = self._cancel, None
            cancel()
        self._cache.clear()
        self._temporary.remove()
