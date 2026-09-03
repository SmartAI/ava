"""Persistent Web UI projects and chats, including orderly agent shutdown."""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ava.agent import Agent, CancelCause, CompactionOptions, Status
from ava.app.attach import valid_utf8_prefix
from ava.base import AvaError, ErrorKind, ava_home
from ava.llm import AuthRequirement, ContentBlockKind, Provider, SelectionOverride
from ava.llm import Selection as ProviderSelection
from ava.session import (
    InboxSpliced,
    Log,
    OpenMode,
    SessionStart,
    UserMessage,
    default_session_root,
    discover_all_sessions_in,
)
from ava.session import Selection as SelectionEvent

_WEB_STATE_VERSION = 1
_PROJECT_ID = re.compile(r"^p([1-9][0-9]*)$")
_CHAT_ID = re.compile(r"^c([1-9][0-9]*)$")

ProviderFactory = Callable[
    [SelectionOverride, ProviderSelection | None, AuthRequirement], Provider
]


def title_from_text(text: str) -> str:
    if not text:
        return "New chat"
    encoded = text.encode("utf-8")
    boundary = valid_utf8_prefix(encoded, 40)
    if boundary is None:
        return "New chat"
    if boundary == len(encoded):
        return text
    return encoded[:boundary].decode("utf-8") + "…"


@dataclass(slots=True)
class DriveHandoff:
    """Distinguishes a successful lost wake from input pending after a failure."""

    running: bool = False
    started: bool = False
    last_succeeded: bool = False
    restart_after_current: bool = False

    def acknowledge(self, followed_running_drive: bool, status: Status) -> bool:
        if status != Status.idle:
            return False
        if self.running:
            self.restart_after_current |= self.started
            return False
        return not followed_running_drive or self.last_succeeded

    def finish(self, succeeded: bool) -> bool:
        self.running = False
        self.started = False
        self.last_succeeded = succeeded
        restart, self.restart_after_current = self.restart_after_current, False
        return restart and succeeded


@dataclass(slots=True)
class Chat:
    id: str
    agent: Agent
    session_id: str
    title: str = ""
    archived: bool = False
    attachment_bytes: int = 0
    image_attachments: int = 0
    drive: DriveHandoff = field(default_factory=DriveHandoff)
    task: asyncio.Task[None] | None = None
    status_watchers: list[Callable[[], None]] = field(default_factory=list)

    def notify_status(self) -> None:
        for watcher in list(self.status_watchers):
            watcher()

    @property
    def status(self) -> str:
        if self.agent.status == Status.idle and self.drive.running:
            return "running"
        return self.agent.status.value

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "archived": self.archived,
        }


@dataclass(slots=True)
class Project:
    id: str
    name: str
    path: Path
    chats: list[Chat] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "path": str(self.path),
            "chats": [chat.summary() for chat in self.chats],
        }


class _UnavailableProvider(Provider):
    """Keeps history readable when a resumed session's provider cannot be configured."""

    def __init__(self, selection: ProviderSelection, error: AvaError) -> None:
        super().__init__(selection)
        self.id = selection.provider
        self._error = error

    async def stream(self, *args: Any, **kwargs: Any) -> Any:
        raise self._error


def _parse_error(path: Path) -> AvaError:
    return AvaError(ErrorKind.parse, f"cannot parse Web UI state file '{path}'")


def _durable_selection(log: Log) -> ProviderSelection:
    header = log.loaded_events[0].payload
    assert isinstance(header, SessionStart)
    selected = ProviderSelection(header.provider, header.model)
    for event in log.loaded_events:
        if isinstance(event.payload, SelectionEvent):
            payload = event.payload
            selected = ProviderSelection(payload.provider, payload.model, payload.effort)
    return selected


def _item_title(item: Any) -> str:
    text = next(
        (
            block.text
            for block in item.blocks
            if block.kind == ContentBlockKind.text and block.text
        ),
        "",
    )
    if text:
        return title_from_text(text)
    attachment = next(
        (
            block.display_path
            for block in item.blocks
            if block.kind in (ContentBlockKind.file_text, ContentBlockKind.image)
            and block.display_path
        ),
        "",
    )
    return title_from_text(attachment) if attachment else ""


def _restored_chat_details(log: Log) -> tuple[str, int, int]:
    """Derive display metadata and attachment quotas from durable user input."""
    title = ""
    attachment_bytes = 0
    image_attachments = 0
    seen_messages: set[str] = set()
    items: list[Any] = []
    for event in log.loaded_events:
        payload = event.payload
        if isinstance(payload, InboxSpliced):
            for message in payload.inserted:
                if message.id in seen_messages:
                    continue
                seen_messages.add(message.id)
                items.append(message.item)
        elif isinstance(payload, UserMessage):
            items.append(payload.item)
    for item in items:
        title = title or _item_title(item)
        for block in item.blocks:
            if block.kind == ContentBlockKind.image:
                attachment_bytes += len(block.bytes)
                image_attachments += 1
            elif block.kind == ContentBlockKind.file_text:
                attachment_bytes += len(block.text.encode("utf-8"))
    return title, attachment_bytes, image_attachments


class Registry:
    def __init__(self, cwd: Path) -> None:
        self._state_path = ava_home() / "web.json"
        self._stored_sessions: dict[str, dict[str, Any]] = {}
        self.projects: list[Project] = []
        self._next_project = 1
        self._next_chat = 1
        self._load(cwd)

    def _load(self, cwd: Path) -> None:
        document: dict[str, Any] = {}
        if self._state_path.exists():
            try:
                loaded = json.loads(self._state_path.read_text(encoding="utf-8"))
            except OSError as error:
                raise AvaError(
                    ErrorKind.io, f"cannot read Web UI state file '{self._state_path}'", str(error)
                ) from error
            except json.JSONDecodeError:
                raise _parse_error(self._state_path) from None
            if not isinstance(loaded, dict) or loaded.get("version") != _WEB_STATE_VERSION:
                raise _parse_error(self._state_path)
            document = loaded

        raw_projects = document.get("projects", [])
        raw_sessions = document.get("sessions", {})
        if not isinstance(raw_projects, list) or not isinstance(raw_sessions, dict):
            raise _parse_error(self._state_path)

        project_ids: set[str] = set()
        project_paths: set[Path] = set()
        for raw in raw_projects:
            if not isinstance(raw, dict):
                raise _parse_error(self._state_path)
            project_id, name, value = raw.get("id"), raw.get("name"), raw.get("path")
            if not isinstance(project_id, str) or not project_id:
                raise _parse_error(self._state_path)
            if not isinstance(name, str) or not name:
                raise _parse_error(self._state_path)
            if not isinstance(value, str) or not value:
                raise _parse_error(self._state_path)
            path = Path(value)
            try:
                path = path.resolve(strict=True)
            except OSError:
                continue
            if not path.is_dir():
                continue
            if project_id in project_ids or path in project_paths:
                raise _parse_error(self._state_path)
            project_ids.add(project_id)
            project_paths.add(path)
            self.projects.append(Project(id=project_id, name=name, path=path))

        chat_ids: set[str] = set()
        for session_id, raw in raw_sessions.items():
            if (
                not isinstance(session_id, str)
                or not session_id
                or not isinstance(raw, dict)
                or not isinstance(raw.get("id"), str)
                or not raw["id"]
                or not isinstance(raw.get("title", ""), str)
                or not isinstance(raw.get("archived", False), bool)
                or raw["id"] in chat_ids
            ):
                raise _parse_error(self._state_path)
            chat_ids.add(raw["id"])
            self._stored_sessions[session_id] = {
                "id": raw["id"],
                "title": raw.get("title", ""),
                "archived": raw.get("archived", False),
            }

        self._refresh_counters()
        if cwd not in project_paths:
            project_id = "workspace" if "workspace" not in project_ids else self.next_project_id()
            self.projects.insert(0, Project(id=project_id, name=cwd.name or str(cwd), path=cwd))
        self._refresh_counters()

    def _refresh_counters(self) -> None:
        for project in self.projects:
            match = _PROJECT_ID.match(project.id)
            if match:
                self._next_project = max(self._next_project, int(match.group(1)) + 1)
        for record in self._stored_sessions.values():
            match = _CHAT_ID.match(record["id"])
            if match:
                self._next_chat = max(self._next_chat, int(match.group(1)) + 1)

    def restore(
        self,
        options: CompactionOptions,
        selection: SelectionOverride,
        provider_factory: ProviderFactory,
    ) -> None:
        """Reopen all default logs and recreate projects missing from the Web UI index."""
        candidates = discover_all_sessions_in(default_session_root())
        projects_by_path = {str(project.path): project for project in self.projects}
        for candidate in candidates:
            if candidate.header.cwd in projects_by_path:
                continue
            path = Path(candidate.header.cwd)
            project = Project(
                id=self.next_project_id(), name=path.name or str(path), path=path
            )
            self.projects.append(project)
            projects_by_path[candidate.header.cwd] = project

        for candidate in candidates:
            project = projects_by_path[candidate.header.cwd]
            log = Log.open(candidate.path, OpenMode.repair, project.path)
            durable_selection = _durable_selection(log)
            derived_title, attachment_bytes, image_attachments = _restored_chat_details(log)
            try:
                provider = provider_factory(selection, durable_selection, AuthRequirement.allow_missing)
            except AvaError as error:
                provider = _UnavailableProvider(durable_selection, error)
            except BaseException:
                log.close()
                raise
            agent = Agent.reopen(provider, project.path, log, options)
            stored = self._stored_sessions.get(candidate.header.id, {})
            chat_id = stored["id"] if "id" in stored else self.next_chat_id()
            chat = Chat(
                id=chat_id,
                agent=agent,
                session_id=candidate.header.id,
                title=stored.get("title") or derived_title,
                archived=stored.get("archived", False),
                attachment_bytes=attachment_bytes,
                image_attachments=image_attachments,
            )
            project.chats.append(chat)
        self.persist()

    def persist(self) -> None:
        sessions = dict(self._stored_sessions)
        for project in self.projects:
            for chat in project.chats:
                sessions[chat.session_id] = {
                    "id": chat.id,
                    "title": chat.title,
                    "archived": chat.archived,
                }
        document = {
            "version": _WEB_STATE_VERSION,
            "projects": [
                {"id": project.id, "name": project.name, "path": str(project.path)}
                for project in self.projects
            ],
            "sessions": sessions,
        }
        encoded = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        home = self._state_path.parent
        created = not home.exists()
        try:
            home.mkdir(parents=True, exist_ok=True)
            if created:
                os.chmod(home, 0o700)
            temporary = self._state_path.with_name(
                f"{self._state_path.name}.tmp.{os.getpid()}.{secrets.token_hex(6)}"
            )
            with open(temporary, "x", encoding="utf-8") as output:
                output.write(encoded)
                output.flush()
                os.fsync(output.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self._state_path)
        except OSError as error:
            if "temporary" in locals():
                temporary.unlink(missing_ok=True)
            raise AvaError(
                ErrorKind.io, f"cannot replace Web UI state file '{self._state_path}'", str(error)
            ) from error
        self._stored_sessions = sessions

    def next_project_id(self) -> str:
        value, self._next_project = self._next_project, self._next_project + 1
        return f"p{value}"

    def next_chat_id(self) -> str:
        value, self._next_chat = self._next_chat, self._next_chat + 1
        return f"c{value}"

    def find_project(self, project_id: str) -> Project | None:
        return next((project for project in self.projects if project.id == project_id), None)

    def find_chat(self, chat_id: str) -> tuple[Project, Chat] | None:
        for project in self.projects:
            for chat in project.chats:
                if chat.id == chat_id:
                    return project, chat
        return None

    async def aclose(self) -> None:
        chats = [chat for project in self.projects for chat in project.chats]
        running = [chat.task for chat in chats if chat.task is not None and not chat.task.done()]
        for chat in chats:
            if chat.task is not None and not chat.task.done():
                chat.agent.cancel(CancelCause.user_abort)
        if running:
            await asyncio.gather(*running, return_exceptions=True)
        results = await asyncio.gather(
            *(chat.agent.aclose() for chat in chats), return_exceptions=True
        )
        failure = next((result for result in results if isinstance(result, BaseException)), None)
        if failure is not None:
            raise failure


@dataclass(slots=True)
class WebState:
    registry: Registry
    compaction: CompactionOptions
    selection: SelectionOverride
    provider_factory: ProviderFactory
