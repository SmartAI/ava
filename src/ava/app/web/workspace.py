"""Authenticated, bounded reads of a project's files for remote desktop views."""

from __future__ import annotations

import asyncio
import hashlib
import os
import stat
import time
from collections import OrderedDict
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .registry import Registry

PAGE_SIZE = 256
MAX_ENTRIES = 100_000
PDF_LIMIT = 256 * 1024 * 1024
IMAGE_LIMIT = 8 * 1024 * 1024
TEXT_LIMIT = 1024 * 1024


def project_path(root: Path, value: str) -> Path:
    requested = Path(value)
    path = (requested if requested.is_absolute() else root / requested).resolve()
    if not path.is_relative_to(root):
        raise ValueError("Choose a file inside this project.")
    return path


def file_version(info: os.stat_result) -> str:
    return hashlib.sha256(str((info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)).encode()).hexdigest()


def open_file(root: Path, path: Path, *, directory: bool = False) -> tuple[int, os.stat_result]:
    # Open each resolved component relative to its parent descriptor. A directory
    # changed into a symlink between validation and open cannot escape the root.
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        parts = path.relative_to(root).parts
        if not parts and not directory:
            raise ValueError("Choose a regular file to preview.")
        for i, part in enumerate(parts):
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            if directory or i < len(parts) - 1:
                flags |= os.O_DIRECTORY
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        info = os.fstat(fd)
        if not directory and not stat.S_ISREG(info.st_mode):
            raise ValueError("Choose a regular file to preview.")
        return fd, info
    except BaseException:
        os.close(fd)
        raise


def file_info(root: Path, value: str) -> dict:
    path = project_path(root, value)
    fd, info = open_file(root, path)
    os.close(fd)
    suffix = path.suffix.lower()
    kind, limit = "text", TEXT_LIMIT
    if suffix == ".pdf":
        kind, limit = "pdf", PDF_LIMIT
    elif suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
        kind, limit = "image", IMAGE_LIMIT
    elif suffix in {".md", ".markdown"}:
        kind = "markdown"
    notice = ""
    if info.st_size > limit:
        notice = f"This file exceeds the {limit // 1024 // 1024} MiB {kind} preview limit."
        kind = "unsupported"
    return {"path": str(path), "name": path.name, "relative": str(path.relative_to(root)),
            "kind": kind, "size": info.st_size, "version": file_version(info), "limit": limit,
            "notice": notice}


def directory_entries(root: Path, value: str) -> tuple[str, list[dict]]:
    path = project_path(root, value)
    rows: list[dict] = []
    fd, _ = open_file(root, path, directory=True)
    try:
        with os.scandir(fd) as entries:
            for entry in entries:
                if len(rows) >= MAX_ENTRIES:
                    raise ValueError("This directory has more than 100,000 entries. Open a smaller subdirectory.")
                directory = entry.is_dir(follow_symlinks=False)
                entry_path = str(path / entry.name)
                if entry.is_symlink():
                    try:
                        target = project_path(root, entry_path)
                        directory = target.is_dir()
                    except (OSError, ValueError, RuntimeError):
                        directory = False
                rows.append({"fileName": entry.name, "filePath": entry_path, "directory": directory})
    finally:
        os.close(fd)
    rows.sort(key=lambda row: (not row["directory"], row["fileName"].casefold(), row["fileName"]))
    return str(path), rows


def register_workspace_routes(app: FastAPI, registry: Registry) -> None:
    # Snapshot cursors keep later pages stable when the directory changes. They
    # expire after two minutes and share a bounded metadata budget per backend.
    snapshots: OrderedDict[str, tuple[Path, str, list[dict], float]] = OrderedDict()
    readers = asyncio.Semaphore(2)

    def root_for(project_id: str, workspace: str) -> Path:
        project = registry.find_project(project_id)
        if project is None or project.hidden:
            raise ValueError("This project is no longer available.")
        if not workspace or workspace == str(project.path):
            return project.path
        for chat in project.chats:
            if str(chat.agent.cwd) == workspace:
                return chat.agent.cwd
        raise ValueError("This workspace does not belong to the project.")

    def error_response(error: Exception, status: int = 400) -> JSONResponse:
        return JSONResponse({"error": str(error)}, status_code=status, headers={"Cache-Control": "no-store"})

    @app.get("/api/projects/{project_id}/files")
    async def files(project_id: str, path: str = "", cursor: str = "", offset: int = 0, workspace: str = "") -> Response:
        try:
            root = root_for(project_id, workspace)
            if cursor:
                snapshot = snapshots.get(cursor)
                if snapshot is None or time.monotonic() - snapshot[3] > 120:
                    raise ValueError("Directory listing expired. Reload the file tree.")
                if snapshot[0] != root or snapshot[1] != path:
                    raise ValueError("Invalid directory cursor.")
                _, path, entries, _ = snapshot
            else:
                if offset:
                    raise ValueError("Invalid directory cursor.")
                async with readers:
                    path, entries = await asyncio.to_thread(directory_entries, root, path)
                cursor = uuid4().hex
                while snapshots and (len(snapshots) >= 32 or sum(len(s[2]) for s in snapshots.values()) + len(entries) > MAX_ENTRIES):
                    snapshots.popitem(last=False)
                snapshots[cursor] = (root, path, entries, time.monotonic())
            if offset < 0 or offset > len(entries):
                raise ValueError("Invalid directory offset.")
            return JSONResponse({"path": path, "entries": entries[offset:offset + PAGE_SIZE], "cursor": cursor,
                                 "next": offset + PAGE_SIZE if offset + PAGE_SIZE < len(entries) else None,
                                 "total": len(entries)}, headers={"Cache-Control": "no-store"})
        except (OSError, ValueError, RuntimeError) as error:
            return error_response(error)

    @app.get("/api/projects/{project_id}/file")
    async def info(project_id: str, path: str, workspace: str = "") -> Response:
        try:
            root = root_for(project_id, workspace)
            async with readers:
                result = await asyncio.to_thread(file_info, root, path)
            return JSONResponse(result, headers={"Cache-Control": "no-store"})
        except (OSError, ValueError, RuntimeError) as error:
            return error_response(error)

    @app.get("/api/projects/{project_id}/file/content")
    async def content(project_id: str, path: str, version: str, workspace: str = "") -> Response:
        fd = -1
        try:
            root = root_for(project_id, workspace)
            async with readers:
                metadata = await asyncio.to_thread(file_info, root, path)
                if metadata["kind"] == "unsupported":
                    raise ValueError(metadata["notice"])
                fd, before = await asyncio.to_thread(open_file, root, Path(metadata["path"]))
            if file_version(before) != version:
                os.close(fd)
                return error_response(ValueError("File changed. Reload the preview."), 409)
        except (OSError, ValueError, RuntimeError) as error:
            if fd >= 0:
                os.close(fd)
            return error_response(error)

        async def chunks():
            try:
                remaining = before.st_size
                while remaining:
                    data = await asyncio.to_thread(os.read, fd, min(64 * 1024, remaining))
                    if not data:
                        raise OSError("File changed while downloading.")
                    remaining -= len(data)
                    # Validate before yielding the final chunk, so a concurrent
                    # write fails the HTTP transfer rather than caching mixed data.
                    if not remaining and file_version(os.fstat(fd)) != version:
                        raise OSError("File changed while downloading.")
                    yield data
            finally:
                os.close(fd)

        return StreamingResponse(chunks(), media_type="application/octet-stream",
                                 headers={"Content-Length": str(before.st_size), "ETag": version, "Cache-Control": "no-store"})
