"""Bounded background indexing and authenticated analytics queries."""
from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from zoneinfo import ZoneInfoNotFoundError

from fastapi import FastAPI

from ava.app.analytics import AnalyticsIndex

from .registry import Registry
from .routes import error_response


class Analytics:
    def __init__(self, registry: Registry, path: Path) -> None:
        self.registry, self.path = registry, path
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ava-analytics")
        self.index: AnalyticsIndex | None = None
        self.task: asyncio.Task | None = None
        self.error = ""
        self.stopping = False

    async def run(self, function, *args):
        return await asyncio.get_running_loop().run_in_executor(self.executor, partial(function, *args))

    def sources(self) -> dict:
        return {str(chat.agent.session_path): (project.id, chat.status == "running")
                for project in self.registry.projects if not project.hidden for chat in project.chats if chat.agent.session_path}

    async def start(self) -> None:
        try:
            self.index = await self.run(self._open)
        except (OSError, sqlite3.Error) as error:
            self.error = "Analytics cache is unavailable. Fix the storage problem and restart the backend: " + str(error)
            return
        self.task = asyncio.create_task(self._update())

    def _open(self) -> AnalyticsIndex:
        try:
            return AnalyticsIndex(self.path)
        except sqlite3.DatabaseError:
            # This file contains only derived statistics. Preserve a damaged copy
            # for diagnosis; never alter source logs or other application databases.
            quarantine = self.path.name + f".invalid-{time.time_ns()}"
            for suffix in ("", "-wal", "-shm"):
                source = self.path.with_name(self.path.name + suffix)
                if source.exists():
                    source.replace(self.path.with_name(quarantine + suffix))
            return AnalyticsIndex(self.path)

    async def _update(self) -> None:
        while not self.stopping:
            try:
                assert self.index is not None
                await self.run(self.index.scan, self.sources())
                self.error = ""
            except Exception as error:
                self.error = str(error)
            await asyncio.sleep(.25)

    async def close(self) -> None:
        self.stopping = True
        if self.task:
            await self.task
        if self.index:
            await self.run(self.index.close)
        self.executor.shutdown(wait=False)


def register_analytics_routes(app: FastAPI, analytics: Analytics) -> None:
    @app.get("/api/analytics")
    async def report(days: int = 7, timezone: str = "UTC", project: str = "", revision: str = ""):
        if project and not any(p.id == project and not p.hidden for p in analytics.registry.projects):
            return error_response(404, "Project not found.")
        if analytics.index is None:
            return error_response(503, analytics.error or "Analytics is starting.")
        try:
            # Refresh source membership before the query, so hidden projects disappear immediately.
            await analytics.run(analytics.index.scan, analytics.sources())
            result = await analytics.run(analytics.index.query, days, timezone, project)
            result.update(error=analytics.error, machine_id=app.state.backend.info["machine_id"])
            current = hashlib.sha256(json.dumps({key: value for key, value in result.items() if key != "as_of"}, sort_keys=True).encode()).hexdigest()[:24]
            if revision == current:
                return {"unchanged": True, "revision": current, "as_of": result["as_of"], "machine_id": result["machine_id"]}
            return {**result, "revision": current}
        except (ValueError, ZoneInfoNotFoundError) as error:
            return error_response(400, str(error))
