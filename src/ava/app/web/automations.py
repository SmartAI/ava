"""Durable schedules and run claims owned by the machine's persistent backend."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import Field, ValidationError, model_validator

from ava.agent import CancelCause
from ava.base import AvaError
from ava.llm import Item, Role, make_text_block

from .models import CreateChatBody, RequestBody, parse_body
from .registry import Chat, WebState
from .streaming import begin_drive


class AutomationConflict(ValueError):
    """An edit or launch conflicts with newer durable state."""


class Schedule(RequestBody):
    start_local: str
    timezone: str
    cadence: Literal["once", "minutes", "hours", "days", "weeks"] = "days"
    every: int = Field(default=1, ge=1, le=36500)
    count: int = Field(default=1, ge=1, le=1_000_000)

    @model_validator(mode="after")
    def valid_schedule(self) -> Schedule:
        try:
            start = datetime.fromisoformat(self.start_local)
            if start.tzinfo is not None:
                raise ValueError("Use a local date/time and choose its timezone separately.")
            ZoneInfo(self.timezone)
            self.start_local = start.isoformat(timespec="seconds")
            if self.cadence == "once" and self.count != 1:
                raise ValueError("A one-time task must have exactly one scheduled run.")
            self.occurrence(self.count - 1)
        except (ZoneInfoNotFoundError, OverflowError) as error:
            raise ValueError("Choose an IANA timezone and a schedule within the supported date range.") from error
        return self

    def occurrence(self, index: int) -> datetime:
        local = datetime.fromisoformat(self.start_local)
        zone = ZoneInfo(self.timezone)
        if self.cadence in ("days", "weeks"):
            local += timedelta(days=index * self.every * (7 if self.cadence == "weeks" else 1))
        # fold=0 chooses the first occurrence of a repeated local time. For a gap,
        # find the next valid minute (also covers zones that skipped an entire day).
        candidate = local.replace(tzinfo=zone, fold=0)
        while candidate.astimezone(UTC).astimezone(zone).replace(tzinfo=None) != local:
            local = local.replace(second=0, microsecond=0) + timedelta(minutes=1)
            candidate = local.replace(tzinfo=zone, fold=0)
        result = candidate.astimezone(UTC)
        if self.cadence in ("minutes", "hours"):
            result += timedelta(minutes=index * self.every * (60 if self.cadence == "hours" else 1))
        return result

    def due_through(self, first: int, now: datetime) -> int:
        """Greatest due occurrence, using logarithmic work even after long downtime."""
        low, high = first, self.count
        while low < high:
            middle = (low + high) // 2
            if self.occurrence(middle) <= now:
                low = middle + 1
            else:
                high = middle
        return low - 1


class AutomationDefinition(RequestBody):
    name: str = Field(min_length=1, max_length=120)
    project_id: str = Field(min_length=1)
    prompt: str = Field(min_length=1, max_length=64_000)
    schedule: Schedule
    workspace: Literal["current", "worktree"] = "current"
    base_ref: str = Field(default="HEAD", min_length=1, max_length=200)
    provider: str | None = Field(default=None, min_length=1)
    model: str | None = Field(default=None, min_length=1)
    effort: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def nonblank(self) -> AutomationDefinition:
        self.name, self.prompt = self.name.strip(), self.prompt.strip()
        if not self.name or not self.prompt:
            raise ValueError("Enter a name and a prompt for the task.")
        return self


class AutomationStore:
    """Short SQLite transactions; callers run these methods off the async/UI loop."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.is_symlink():
            raise ValueError("The automation database must not be a symbolic link.")
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False, timeout=5)
        self._db.row_factory = sqlite3.Row
        os.chmod(path, 0o600)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1):
            self._db.close()
            raise ValueError("This automation database needs a newer Ava backend.")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, definition TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1, deleted INTEGER NOT NULL DEFAULT 0,
                series INTEGER NOT NULL DEFAULT 1, version INTEGER NOT NULL DEFAULT 1,
                consumed INTEGER NOT NULL DEFAULT 0, skipped INTEGER NOT NULL DEFAULT 0,
                next_due REAL, created_at REAL NOT NULL, updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS tasks_due ON tasks(next_due) WHERE enabled=1 AND deleted=0;
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY, automation_id TEXT NOT NULL REFERENCES tasks(id),
                series INTEGER NOT NULL, occurrence INTEGER,
                scheduled_for REAL NOT NULL, created_at REAL NOT NULL,
                started_at REAL, finished_at REAL,
                status TEXT NOT NULL, session_id TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '', snapshot TEXT NOT NULL,
                UNIQUE(automation_id, series, occurrence)
            );
            CREATE INDEX IF NOT EXISTS runs_task ON runs(automation_id);
            CREATE INDEX IF NOT EXISTS runs_active ON runs(automation_id) WHERE status IN ('starting','running','paused');
            PRAGMA user_version=1;
        """)
        self.revision = 0

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _task(self, identity: str) -> sqlite3.Row:
        row = self._db.execute("SELECT * FROM tasks WHERE id=? AND deleted=0", (identity,)).fetchone()
        if row is None:
            raise KeyError("This automation is no longer available.")
        assert isinstance(row, sqlite3.Row)
        return row

    @staticmethod
    def _summary(row: sqlite3.Row) -> dict:
        definition = json.loads(row["definition"])
        return {"id": row["id"], **definition, "enabled": bool(row["enabled"]),
                "version": row["version"], "consumed": row["consumed"], "skipped": row["skipped"],
                "remaining": max(0, definition["schedule"]["count"] - row["consumed"]),
                "next_due": row["next_due"] if row["enabled"] else None,
                "created_at": row["created_at"], "updated_at": row["updated_at"]}

    def list_tasks(self) -> list[dict]:
        with self._lock:
            return [self._summary(row) for row in self._db.execute("""SELECT id,
                json_remove(definition,'$.prompt') AS definition,enabled,version,consumed,skipped,
                next_due,created_at,updated_at FROM tasks WHERE deleted=0 ORDER BY created_at DESC,id""")]

    def get(self, identity: str) -> dict:
        with self._lock:
            return self._summary(self._task(identity))

    def snapshot(self) -> dict:
        with self._lock:
            active = {run["automation_id"]: run["status"] for run in self.active_runs()}
            return {"automations": [{**task, "active_run": active.get(task["id"], "")} for task in self.list_tasks()], "revision": self.revision}

    def save(self, definition: AutomationDefinition, now: datetime, identity: str = "", version: int = 0, request_id: str = "") -> dict:
        encoded = definition.model_dump_json()
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            if not identity:
                identity = request_id or uuid4().hex
                existing = self._db.execute("SELECT * FROM tasks WHERE id=?", (identity,)).fetchone()
                if existing is not None:
                    if existing["deleted"] or existing["definition"] != encoded:
                        raise AutomationConflict("This request already created a task. Reload the task list before trying again.")
                    return self._summary(existing)
                self._db.execute("INSERT INTO tasks(id,definition,next_due,created_at,updated_at) VALUES(?,?,?,?,?)",
                                 (identity, encoded, definition.schedule.occurrence(0).timestamp(), now.timestamp(), now.timestamp()))
            else:
                previous = self._task(identity)
                if previous["version"] != version:
                    raise AutomationConflict("This task changed on another desktop. Reload before saving.")
                changed_schedule = json.loads(previous["definition"])["schedule"] != definition.schedule.model_dump()
                self._db.execute("""UPDATE tasks SET definition=?,version=version+1,updated_at=?,
                    series=series+?, consumed=?, skipped=?, next_due=? WHERE id=?""",
                    (encoded, now.timestamp(), int(changed_schedule),
                     0 if changed_schedule else previous["consumed"],
                     0 if changed_schedule else previous["skipped"],
                     definition.schedule.occurrence(0).timestamp() if changed_schedule else previous["next_due"], identity))
            result = self._summary(self._task(identity))
            self.revision += 1
        return result

    def set_enabled(self, identity: str, enabled: bool, now: datetime) -> dict:
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            row = self._task(identity)
            if enabled and not row["enabled"]:
                schedule = AutomationDefinition.model_validate_json(row["definition"]).schedule
                expired = schedule.due_through(row["consumed"], now) + 1
                self._advance(row, schedule, expired, expired - row["consumed"])
            self._db.execute("UPDATE tasks SET enabled=?,version=version+1,updated_at=? WHERE id=?",
                             (int(enabled), now.timestamp(), identity))
            result = self._summary(self._task(identity))
            self.revision += 1
        return result

    def remove(self, identity: str, now: datetime) -> None:
        with self._lock, self._db:
            self._task(identity)
            self._db.execute("UPDATE tasks SET deleted=1,enabled=0,version=version+1,updated_at=? WHERE id=?", (now.timestamp(), identity))
            self.revision += 1

    def _active(self, identity: str) -> bool:
        return self._db.execute("SELECT 1 FROM runs WHERE automation_id=? AND status IN ('starting','running','paused') LIMIT 1", (identity,)).fetchone() is not None

    def _insert_run(self, row: sqlite3.Row, occurrence: int | None, scheduled: datetime, now: datetime, identity: str = "") -> dict:
        identity = identity or uuid4().hex
        self._db.execute("""INSERT INTO runs(id,automation_id,series,occurrence,scheduled_for,created_at,status,snapshot)
                            VALUES(?,?,?,?,?,?,'starting',?)""",
                         (identity, row["id"], row["series"], occurrence, scheduled.timestamp(), now.timestamp(), row["definition"]))
        return dict(self._db.execute("SELECT rowid AS cursor,* FROM runs WHERE id=?", (identity,)).fetchone())

    def _advance(self, row: sqlite3.Row, schedule: Schedule, consumed: int, skipped: int) -> None:
        next_due = schedule.occurrence(consumed).timestamp() if consumed < schedule.count else None
        self._db.execute("UPDATE tasks SET consumed=?,skipped=skipped+?,next_due=? WHERE id=?", (consumed, skipped, next_due, row["id"]))

    def claim_due(self, now: datetime, capacity: int = 4) -> list[dict]:
        runs = []
        changed = False
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            active = self._db.execute("SELECT count(*) FROM runs WHERE status IN ('starting','running','paused') AND status!='paused'").fetchone()[0]
            slots = max(0, capacity - active)
            due = self._db.execute("SELECT * FROM tasks WHERE enabled=1 AND deleted=0 AND next_due<=? ORDER BY next_due,id LIMIT 128", (now.timestamp(),)).fetchall()
            for row in due:
                running = self._active(row["id"])
                if not running and not slots:
                    continue
                schedule = AutomationDefinition.model_validate_json(row["definition"]).schedule
                latest = schedule.due_through(row["consumed"], now)
                if not running:
                    runs.append(self._insert_run(row, latest, schedule.occurrence(latest), now))
                    slots -= 1
                self._advance(row, schedule, latest + 1, latest - row["consumed"] + int(running))
                changed = True
            if changed:
                self.revision += 1
        return runs

    def manual_run(self, identity: str, request_id: str, now: datetime) -> tuple[dict, bool]:
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            row = self._task(identity)
            previous = self._db.execute("SELECT rowid AS cursor,* FROM runs WHERE id=?", (request_id,)).fetchone()
            if previous:
                if previous["automation_id"] != identity:
                    raise AutomationConflict("This request already started a different task.")
                return dict(previous), False
            if self._active(identity):
                raise AutomationConflict("This task already has an active run. Open it to continue or stop it.")
            run = self._insert_run(row, None, now, now, request_id)
            self.revision += 1
        return run, True

    def active_runs(self) -> list[dict]:
        with self._lock:
            return [dict(row) for row in self._db.execute("SELECT id,automation_id,status,session_id,error FROM runs WHERE status IN ('starting','running','paused')")]

    def update_run(self, identity: str, *, status: str, session_id: str = "", error: str = "", now: datetime) -> None:
        terminal = status not in ("starting", "running", "paused")
        with self._lock, self._db:
            row = self._db.execute("SELECT * FROM runs WHERE id=?", (identity,)).fetchone()
            if row is None:
                raise KeyError("This run is no longer available.")
            session_id = session_id or row["session_id"]
            if (row["status"], row["session_id"], row["error"]) == (status, session_id, error):
                return
            self._db.execute("UPDATE runs SET status=?,session_id=?,error=?,started_at=?,finished_at=? WHERE id=?",
                             (status, session_id, error, row["started_at"] or (now.timestamp() if status == "running" else None),
                              now.timestamp() if terminal else None, identity))
            self.revision += 1

    def history(self, identity: str, before: int = 0, limit: int = 20) -> dict:
        with self._lock:
            rows = self._db.execute("SELECT rowid AS cursor,* FROM runs WHERE automation_id=? AND (?=0 OR rowid<?) ORDER BY rowid DESC LIMIT ?",
                                    (identity, before, before, limit + 1)).fetchall()
            return {"runs": [dict(row) for row in rows[:limit]], "has_more": len(rows) > limit}

    def get_run(self, identity: str) -> dict:
        with self._lock:
            row = self._db.execute("SELECT rowid AS cursor,* FROM runs WHERE id=?", (identity,)).fetchone()
            if row is None:
                raise KeyError("This run is no longer available.")
            return dict(row)


class Automations:
    """A bounded dispatcher in the backend; the desktop only configures and observes it."""

    def __init__(self, state: WebState, path: Path) -> None:
        self.state, self.path = state, path
        self.store: AutomationStore
        self.now: Callable[[], datetime] = lambda: datetime.now(UTC)
        self.wake = asyncio.Event()
        self.error = ""
        self._closing = False
        self._launches: dict[str, asyncio.Task] = {}
        self._cancelled: set[str] = set()
        self._dispatcher: asyncio.Task | None = None

    async def start(self) -> None:
        self.store = await asyncio.to_thread(AutomationStore, self.path)
        await self.reconcile()
        self._dispatcher = asyncio.create_task(self._dispatch())

    async def stop_dispatch(self) -> None:
        self._closing = True
        self.wake.set()
        if self._dispatcher:
            await self._dispatcher
        if self._launches:
            await asyncio.gather(*self._launches.values())

    async def close(self) -> None:
        try:
            if self._dispatcher is not None:
                await self.reconcile()
        finally:
            await asyncio.to_thread(self.store.close)

    def _chats(self) -> dict[str, Chat]:
        return {chat.automation_run: chat for project in self.state.registry.projects for chat in project.chats if chat.automation_run}

    async def reconcile(self) -> None:
        chats = self._chats()
        for run in await asyncio.to_thread(self.store.active_runs):
            if run["id"] in self._launches:
                continue
            chat = chats.get(run["id"])
            error = ""
            if chat and chat.status in ("running", "pausing", "aborting", "paused"):
                status = "paused" if chat.status == "paused" else "running"
            elif chat and chat.completion_seq >= 0:
                reason = chat.completion_reason
                status = "completed" if reason == "completed" else "stopped" if reason in ("user_abort", "user_pause") else "interrupted" if reason in ("interrupted", "shutdown") else "failed"
            else:
                status = "interrupted"
                error = "The backend stopped before this run's result was confirmed. Review it before running again."
            await asyncio.to_thread(self.store.update_run, run["id"], status=status,
                                    session_id=chat.session_id if chat else "", error=error, now=self.now())

    async def _dispatch(self) -> None:
        while not self._closing:
            self.wake.clear()
            try:
                await self.reconcile()
                for run in await asyncio.to_thread(self.store.claim_due, self.now()):
                    self._start_run(run)
                self.error = ""
            except (AvaError, OSError, ValueError, sqlite3.Error) as error:
                self.error = "Scheduled work could not be updated: " + str(error)
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=1)
            except TimeoutError:
                pass

    def _start_run(self, run: dict) -> None:
        task = asyncio.create_task(self._launch(run))
        self._launches[run["id"]] = task

        def finished(_: asyncio.Task) -> None:
            self._launches.pop(run["id"], None)
            self._cancelled.discard(run["id"])
            self.wake.set()

        task.add_done_callback(finished)

    async def _launch(self, run: dict) -> None:
        chat = None
        try:
            definition = AutomationDefinition.model_validate_json(run["snapshot"])
            project = self.state.registry.find_project(definition.project_id)
            if project is None or not project.path.is_dir():
                raise ValueError("The scheduled project's folder is unavailable.")
            body = CreateChatBody(project_id=project.id, workspace=definition.workspace, base_ref=definition.base_ref,
                                  branch="ava/automation-" + run["id"][:12], request_id=run["id"],
                                  provider=definition.provider, model=definition.model, effort=definition.effort)
            chat = await self.state.create_chat(project, body, run["id"], labels={
                "automation_id": run["automation_id"], "automation_run": run["id"],
                "automation_scheduled_for": datetime.fromtimestamp(run["scheduled_for"], UTC).isoformat(),
            })
            chat.title = definition.name
            self.state.registry.persist()
            if self._closing or run["id"] in self._cancelled:
                await asyncio.to_thread(self.store.update_run, run["id"], status="stopped", session_id=chat.session_id, now=self.now())
                return
            await asyncio.to_thread(self.store.update_run, run["id"], status="running", session_id=chat.session_id, now=self.now())
            await chat.agent.followup(Item(role=Role.user, blocks=[make_text_block(definition.prompt)]))
            begin_drive(chat)
        except Exception as error:
            message = error.message if isinstance(error, AvaError) else str(error)
            try:
                await asyncio.to_thread(self.store.update_run, run["id"], status="failed",
                                        session_id=chat.session_id if chat else "", error=message, now=self.now())
            except (OSError, sqlite3.Error):
                self.error = "The run could not start, and its status could not be saved. " + message

    def _visible_task(self, task: dict) -> None:
        project = self.state.registry.find_project(task["project_id"])
        if project is None or project.hidden:
            raise KeyError("This task's project is not displayed in Ava.")

    async def listing(self) -> dict:
        projects = {p.id for p in self.state.registry.projects if not p.hidden}
        snapshot = await asyncio.to_thread(self.store.snapshot)
        return {**snapshot, "automations": [task for task in snapshot["automations"] if task["project_id"] in projects], "error": self.error}

    def _public_run(self, run: dict) -> dict:
        chat = self._chats().get(run["id"])
        return {key: value for key, value in {**run, "chat_id": chat.id if chat else ""}.items() if key not in ("snapshot", "session_id")}

    async def detail(self, identity: str, before: int = 0) -> dict:
        task = await asyncio.to_thread(self.store.get, identity)
        self._visible_task(task)
        history = await asyncio.to_thread(self.store.history, identity, before)
        return {**task, **history, "runs": [self._public_run(run) for run in history["runs"]]}

    async def save(self, definition: AutomationDefinition, identity: str = "", version: int = 0, request_id: str = "") -> dict:
        self._visible_task(definition.model_dump())
        if identity:
            self._visible_task(await asyncio.to_thread(self.store.get, identity))
        result = await asyncio.to_thread(self.store.save, definition, self.now(), identity, version, request_id)
        self.wake.set()
        return result

    async def enabled(self, identity: str, enabled: bool) -> dict:
        self._visible_task(await asyncio.to_thread(self.store.get, identity))
        result = await asyncio.to_thread(self.store.set_enabled, identity, enabled, self.now())
        self.wake.set()
        return result

    async def remove(self, identity: str) -> dict:
        self._visible_task(await asyncio.to_thread(self.store.get, identity))
        await asyncio.to_thread(self.store.remove, identity, self.now())
        self.wake.set()
        return {"id": identity, "removed": True}

    async def run_now(self, identity: str, request_id: str) -> dict:
        self._visible_task(await asyncio.to_thread(self.store.get, identity))
        run, created = await asyncio.to_thread(self.store.manual_run, identity, request_id, self.now())
        if created:
            self._start_run(run)
        return self._public_run(run)

    async def stop_run(self, identity: str, run_id: str) -> dict:
        self._visible_task(await asyncio.to_thread(self.store.get, identity))
        run = await asyncio.to_thread(self.store.get_run, run_id)
        if run["automation_id"] != identity:
            raise KeyError("This run belongs to a different automation.")
        if run["status"] not in ("starting", "running", "paused"):
            return self._public_run(run)
        self._cancelled.add(run_id)
        chat = self._chats().get(run_id)
        if chat:
            chat.agent.cancel(CancelCause.user_abort)
        if run_id not in self._launches and (chat is None or chat.status == "idle"):
            await asyncio.to_thread(self.store.update_run, run_id, status="stopped", now=self.now())
            self._cancelled.discard(run_id)
        self.wake.set()
        return self._public_run(await asyncio.to_thread(self.store.get_run, run_id))


class EditAutomationBody(AutomationDefinition):
    version: int = Field(ge=1)


class CreateAutomationBody(AutomationDefinition):
    request_id: str = Field(default="", pattern=r"^([a-f0-9]{32})?$")


class EnableAutomationBody(RequestBody):
    enabled: bool


class RunAutomationBody(RequestBody):
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$")


def register_automation_routes(app: FastAPI, engine: Automations) -> None:
    async def respond(operation: Awaitable, status: int = 200) -> JSONResponse:
        try:
            return JSONResponse(await operation, status_code=status)
        except AutomationConflict as error:
            return JSONResponse({"error": str(error)}, status_code=409)
        except KeyError as error:
            return JSONResponse({"error": str(error.args[0])}, status_code=404)
        except (AvaError, OSError, sqlite3.Error) as error:
            return JSONResponse({"error": str(error)}, status_code=503)
        except ValueError as error:
            return JSONResponse({"error": str(error)}, status_code=400)

    @app.get("/api/automations")
    async def listing() -> JSONResponse:
        return await respond(engine.listing())

    @app.post("/api/automations/preview")
    async def preview(request: Request) -> JSONResponse:
        try:
            schedule = Schedule.model_validate(await request.json())
        except (ValidationError, ValueError, UnicodeError) as error:
            message = error.errors()[0]["msg"] if isinstance(error, ValidationError) else "Enter a valid schedule."
            return JSONResponse({"error": message.removeprefix("Value error, ")}, status_code=400)
        first = max(0, schedule.due_through(0, engine.now()))
        times = [schedule.occurrence(i) for i in range(first, min(first + 3, schedule.count))]
        return JSONResponse({"occurrences": [{"at": at.isoformat(), "label": at.astimezone(ZoneInfo(schedule.timezone)).strftime("%a, %b %d · %H:%M %Z")} for at in times], "skipped": first})

    @app.post("/api/automations")
    async def create(request: Request) -> JSONResponse:
        body = await parse_body(request, CreateAutomationBody)
        if body is None:
            return JSONResponse({"error": "Enter a name, prompt, project and valid schedule."}, status_code=400)
        return await respond(engine.save(AutomationDefinition.model_validate(body.model_dump()), request_id=body.request_id), 201)

    @app.get("/api/automations/{identity}")
    async def detail(identity: str, before: int = 0) -> JSONResponse:
        return await respond(engine.detail(identity, max(0, before)))

    @app.put("/api/automations/{identity}")
    async def edit(identity: str, request: Request) -> JSONResponse:
        body = await parse_body(request, EditAutomationBody)
        if body is None:
            return JSONResponse({"error": "Enter valid task settings and reload before editing."}, status_code=400)
        return await respond(engine.save(AutomationDefinition.model_validate(body.model_dump()), identity, body.version))

    @app.post("/api/automations/{identity}/enabled")
    async def enabled(identity: str, request: Request) -> JSONResponse:
        body = await parse_body(request, EnableAutomationBody)
        if body is None:
            return JSONResponse({"error": "Choose whether the task is enabled."}, status_code=400)
        return await respond(engine.enabled(identity, body.enabled))

    @app.delete("/api/automations/{identity}")
    async def remove(identity: str) -> JSONResponse:
        return await respond(engine.remove(identity))

    @app.post("/api/automations/{identity}/run")
    async def run(identity: str, request: Request) -> JSONResponse:
        body = await parse_body(request, RunAutomationBody)
        if body is None:
            return JSONResponse({"error": "A run request must have a unique request_id."}, status_code=400)
        return await respond(engine.run_now(identity, body.request_id), 202)

    @app.post("/api/automations/{identity}/runs/{run_id}/stop")
    async def stop(identity: str, run_id: str) -> JSONResponse:
        return await respond(engine.stop_run(identity, run_id))
