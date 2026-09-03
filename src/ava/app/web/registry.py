"""In-memory Web UI projects and chats, including orderly agent shutdown."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ava.agent import Agent, CancelCause, CompactionOptions, Status
from ava.llm import Provider, SelectionOverride


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


class Registry:
    def __init__(self, cwd: Path) -> None:
        self.projects = [Project(id="workspace", name=cwd.name or str(cwd), path=cwd)]
        self._next_project = 1
        self._next_chat = 1

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


ProviderFactory = Callable[[SelectionOverride], Provider]


@dataclass(slots=True)
class WebState:
    registry: Registry
    compaction: CompactionOptions
    selection: SelectionOverride
    provider_factory: ProviderFactory
