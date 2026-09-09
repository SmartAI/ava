"""Bounded local file reads for the desktop composer and project inspector."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any
from uuid import uuid4

from PySide6.QtCore import Property, QDir, QModelIndex, QObject, QPersistentModelIndex, Qt, QUrl
from PySide6.QtWidgets import QFileSystemModel

from ava.app.web.routes import decode_attachments
from ava.base import AvaError

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


def local_path(value: str) -> Path:
    url = QUrl(value)
    return Path(url.toLocalFile() if url.isLocalFile() else value).expanduser()


def attachment(name: str, data: bytes, staged: list[dict]) -> dict[str, Any]:
    entry = {
        "id": uuid4().hex,
        "name": name,
        "kind": "image" if Path(name).suffix.lower() in IMAGE_SUFFIXES else "file",
        "data_base64": base64.b64encode(data).decode(),
        "size": len(data),
        "preview": "",
    }
    # Keep the exact same type, byte, and count limits as the Web UI.
    decoded = decode_attachments([*staged, entry], 0, 0)
    if entry["kind"] == "image":
        entry["preview"] = f"data:{decoded.blocks[-1].media_type};base64,{entry['data_base64']}"
    return entry


def inspect_path(root: Path, requested: Path) -> dict[str, Any]:
    root, path = root.resolve(), requested.resolve()
    if not path.is_relative_to(root):
        raise ValueError("Choose a file inside this project.")
    state: dict[str, Any] = {
        "path": str(path),
        "name": path.name,
        "relative": str(path.relative_to(root)),
        "parent": str(path.parent) if path != root else "",
        "entries": [],
        "kind": "directory",
        "text": "",
        "source": "",
        "notice": "",
    }
    # QFileSystemModel fetches directory contents asynchronously. The GUI thread
    # only inspects the selected path; opening a project never scans its entries.
    if path.is_dir():
        return state
    else:
        if not path.is_file():
            raise ValueError("Choose a regular file to preview.")
        if path.suffix.lower() == ".pdf":
            # Qt PDF reads and renders pages on demand; never decode the document
            # as text or read it into a Python buffer before handing it to Qt.
            state.update(kind="pdf", source=QUrl.fromLocalFile(str(path)).toString())
            return state
        with path.open("rb") as stream:
            data = stream.read(1024 * 1024 + 1)
        if path.suffix.lower() in IMAGE_SUFFIXES:
            if path.stat().st_size > 8 * 1024 * 1024:
                raise ValueError("Image preview is limited to 8 MiB.")
            state.update(kind="image", source=QUrl.fromLocalFile(str(path)).toString())
        elif len(data) > 1024 * 1024:
            state.update(
                kind="unsupported", notice="This file exceeds the 1 MiB text preview limit."
            )
        else:
            try:
                if b"\x00" in data:
                    raise ValueError("binary file")
                state.update(
                    kind="markdown" if path.suffix.lower() in {".md", ".markdown"} else "text",
                    text=data.decode("utf-8"),
                )
            except (UnicodeError, ValueError):
                state.update(
                    kind="unsupported",
                    notice="Preview is available for PDF, UTF-8 text and PNG, JPEG, GIF or WebP images.",
                )
    return state


FILE_ERRORS = (OSError, ValueError, RuntimeError, AvaError)


class ProjectFiles(QFileSystemModel):
    """Qt owns directory fetching, caching, change watching, and the model hierarchy."""

    DIRECTORY_ROLE = int(Qt.ItemDataRole.UserRole) + 20

    @Property(bool, constant=True)
    def remote(self) -> bool:
        return False

    def __init__(self, root: str, parent: QObject) -> None:
        super().__init__(parent)
        self._root = Path(root).resolve()
        self.setOption(QFileSystemModel.Option.DontUseCustomDirectoryIcons)
        # The QML tree supplies its own icons. Skip costly native per-file icon
        # lookups; Qt continues gathering file metadata on its worker thread.
        self.setIconProvider(None)  # type: ignore[arg-type]  # Qt accepts nullptr; PySide stubs omit it.
        self.setFilter(QDir.Filter.AllEntries | QDir.Filter.NoDotAndDotDot | QDir.Filter.Hidden)
        self.setReadOnly(True)
        self._root_index = self.setRootPath(root)
        self.sort(0)

    @Property(QModelIndex, constant=True)
    def rootIndex(self) -> QModelIndex:
        return self._root_index

    def roleNames(self) -> dict:
        return {**super().roleNames(), self.DIRECTORY_ROLE: b"directory"}

    def data(
        self,
        index: QModelIndex | QPersistentModelIndex,
        role: int = int(Qt.ItemDataRole.DisplayRole),
    ) -> Any:
        if role == self.DIRECTORY_ROLE:
            return self.isDir(index)
        return super().data(index, role)

    def _can_browse(self, index: QModelIndex | QPersistentModelIndex) -> bool:
        info = self.fileInfo(index)
        if not info.isSymLink():
            return True
        try:
            return Path(info.symLinkTarget()).resolve().is_relative_to(self._root)
        except (OSError, RuntimeError):
            return False

    def hasChildren(self, parent=QModelIndex()) -> bool:  # noqa: B008
        return self._can_browse(parent) and super().hasChildren(parent)

    def canFetchMore(self, parent: QModelIndex | QPersistentModelIndex) -> bool:
        return self._can_browse(parent) and super().canFetchMore(parent)
