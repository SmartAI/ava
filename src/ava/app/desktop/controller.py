"""The QML-facing application model; all agent work stays in the backend."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import Property, QObject, QSettings, QTimer, QUrl, Signal, Slot

from .connection import Connection
from .runtime import BackendProcess
from .transcript import Transcript


class Controller(QObject):
    changed = Signal()
    navigationChanged = Signal()
    draftChanged = Signal()
    closed = Signal()

    def __init__(self, cwd: Path, arguments: list[str], settings: QSettings) -> None:
        super().__init__()
        self.settings = settings
        self.runtime = BackendProcess(cwd, arguments, self)
        self.runtime.ready.connect(self._ready)
        self.runtime.failed.connect(self._failed)
        self.runtime.stopped.connect(self._stopped)
        self._connection: Connection | None = None
        self._transcript = Transcript(self)
        self._transcript.changed.connect(self.changed)
        self._projects: list[dict[str, Any]] = []
        self._project_id = ""
        self._chat_id = ""
        self._chat_title = "New conversation"
        self._model_name = ""
        self._status = "idle"
        self._connection_label = "Starting Ava…"
        self._connected = False
        self._busy = False
        self._error = ""
        self._drafts: dict[str, str] = {}
        self._epoch = 0
        self._quitting = False
        self._retry = QTimer(self)
        self._retry.setSingleShot(True)
        self._retry.setInterval(1500)
        self._retry.timeout.connect(self._reconnect)

    @Property(QObject, constant=True)
    def transcript(self) -> QObject:
        return self._transcript

    @Property(list, notify=navigationChanged)
    def projects(self) -> list:
        return self._projects

    def _project(self) -> dict[str, Any]:
        return next((p for p in self._projects if p["id"] == self._project_id), {})

    def _chats(self) -> list:
        return [c for c in self._project().get("chats", []) if not c["archived"]]

    chats = Property(list, _chats, notify=navigationChanged)

    @Property(str, notify=changed)
    def projectId(self) -> str:
        return self._project_id

    @Property(str, notify=changed)
    def projectName(self) -> str:
        return str(self._project().get("name", "Choose a project"))

    @Property(str, notify=changed)
    def projectPath(self) -> str:
        return str(self._project().get("path", ""))

    @Property(str, notify=changed)
    def chatId(self) -> str:
        return self._chat_id

    @Property(str, notify=changed)
    def chatTitle(self) -> str:
        return self._chat_title

    @Property(str, notify=changed)
    def modelName(self) -> str:
        return self._model_name

    @Property(str, notify=changed)
    def status(self) -> str:
        return self._status

    @Property(str, notify=changed)
    def connectionLabel(self) -> str:
        return self._connection_label

    @Property(bool, notify=changed)
    def online(self) -> bool:
        return self._connection is not None

    @Property(bool, notify=changed)
    def connected(self) -> bool:
        return self._connected

    @Property(bool, notify=changed)
    def busy(self) -> bool:
        return self._busy

    @Property(str, notify=changed)
    def error(self) -> str:
        return self._error

    @Property(str, notify=changed)
    def pendingText(self) -> str:
        return self._transcript.pending_text()

    def _get_draft(self) -> str:
        return self._drafts.get(self._chat_id, "")

    def _set_draft(self, value: str) -> None:
        if value != self._get_draft():
            self._drafts[self._chat_id] = value
            self.draftChanged.emit()

    draft = Property(str, _get_draft, _set_draft, notify=draftChanged)

    def _detach(self) -> None:
        self._retry.stop()
        self._epoch += 1
        if self._connection:
            self._connection.close()
            self._connection.deleteLater()
            self._connection = None
        self._connected = self._busy = False

    @Slot()
    def start(self) -> None:
        self._error = ""
        self._connection_label = "Starting Ava…"
        self.changed.emit()
        self.runtime.start()

    def _ready(self, port: int, token: str) -> None:
        if self._quitting:
            return
        self._project_id = ""
        self._chat_id = ""
        self._connection = Connection(port, token, self)
        self._connection.received.connect(self._event)
        self._connection.status.connect(self._snapshot)
        self._connection.disconnected.connect(self._disconnected)
        self._connection_label = "Connected"
        self.refresh()
        self.changed.emit()

    def _failed(self, error: str) -> None:
        self._detach()
        self._error = error
        self._connection_label = "Backend unavailable"
        self.changed.emit()

    def _stopped(self) -> None:
        self._detach()
        if self._quitting:
            self.closed.emit()
        else:
            self._connection_label = "Backend stopped"
            self.changed.emit()

    @Slot()
    def refresh(self) -> None:
        if not self._connection:
            return
        self._connection.call("GET", "/api/projects", None, self._refreshed)

    def _refreshed(self, payload: Any, error: str) -> None:
        if error:
            self._error = error
        else:
            self._projects = payload["projects"]
            self.navigationChanged.emit()
            if self._project_id not in [p["id"] for p in self._projects]:
                preferred = str(self.settings.value("project", ""))
                project = next(
                    (p for p in self._projects if p["id"] == preferred),
                    self._projects[0] if self._projects else None,
                )
                if project:
                    self.selectProject(project["id"])
            elif self._chat_id:
                chat = next((c for c in self._chats() if c["id"] == self._chat_id), None)
                if chat:
                    self._chat_title = chat["title"] or "New conversation"
        self.changed.emit()

    def _clear_chat(self) -> None:
        self._epoch += 1
        self._retry.stop()
        if self._connection:
            self._connection.close_stream()
        self._chat_id = ""
        self._chat_title = "New conversation"
        self._model_name = ""
        self._status = "idle"
        self._busy = self._connected = False
        self._transcript.clear()
        self._error = ""
        self.draftChanged.emit()

    @Slot(str)
    def selectProject(self, identity: str) -> None:
        if not any(p["id"] == identity for p in self._projects):
            return
        self._clear_chat()
        self._project_id = identity
        self.navigationChanged.emit()
        self.settings.setValue("project", identity)
        preferred = str(self.settings.value(f"chat/{identity}", ""))
        chat = next(
            (c for c in self._chats() if c["id"] == preferred),
            self._chats()[0] if self._chats() else None,
        )
        if chat and self._connection:
            self.openChat(chat["id"])
        self.changed.emit()

    @Slot(str)
    def addProject(self, path: str) -> None:
        if not self._connection:
            return
        url = QUrl(path)
        if url.isLocalFile():
            path = url.toLocalFile()

        def added(payload: Any, error: str) -> None:
            if error:
                self._error = error
            else:
                if not any(p["id"] == payload["id"] for p in self._projects):
                    self._projects.append(payload)
                self.navigationChanged.emit()
                self.selectProject(payload["id"])
            self.changed.emit()

        self._connection.call("POST", "/api/projects", {"path": path}, added)

    @Slot()
    def newChat(self) -> None:
        if not self._connection or not self._project_id or self._busy:
            return
        self._busy = True
        epoch = self._epoch
        project_id = self._project_id
        self.changed.emit()

        def created(payload: Any, error: str) -> None:
            if not error:
                project = next((p for p in self._projects if p["id"] == project_id), None)
                if project is not None and not any(
                    c["id"] == payload["id"] for c in project["chats"]
                ):
                    project["chats"].insert(0, payload)
                    self.navigationChanged.emit()
            if epoch != self._epoch:
                return
            self._busy = False
            if error:
                self._error = error
            else:
                self.openChat(payload["id"])
            self.changed.emit()

        self._connection.call("POST", "/api/chats", {"project_id": self._project_id}, created)

    @Slot(str)
    def openChat(self, identity: str) -> None:
        if not self._connection:
            return
        self._clear_chat()
        epoch = self._epoch
        self._busy = True
        self.changed.emit()

        def opened(payload: Any, error: str) -> None:
            if epoch != self._epoch:
                return
            self._busy = False
            if error:
                self._error = error
            elif self._connection:
                self._chat_id = identity
                self._chat_title = payload["title"] or "New conversation"
                self._status = payload["status"]
                self.settings.setValue(f"chat/{self._project_id}", identity)
                self._connection.stream(identity, -1)
            self.draftChanged.emit()
            self.changed.emit()

        self._connection.call("GET", f"/api/chats/{identity}", None, opened)

    def _event(self, event: dict) -> None:
        try:
            self._transcript.apply(event)
        except (KeyError, ValueError, TypeError):
            self._error = "Ava sent an unsupported event. Reopen this conversation to retry."
            self._connected = False
            if self._connection:
                self._connection.close_stream()
        self.changed.emit()

    def _snapshot(self, payload: dict) -> None:
        self._connected = True
        self._connection_label = "Connected"
        self._status = payload.get("status", "idle")
        self._model_name = " / ".join(str(payload.get(key) or "") for key in ("provider", "model"))
        self.changed.emit()

    def _disconnected(self, message: str) -> None:
        self._connected = False
        self._connection_label = message
        self.changed.emit()
        if self._connection and self._chat_id and not self._quitting:
            self._retry.start()

    def _reconnect(self) -> None:
        if self._connection and self._chat_id:
            self._connection.stream(self._chat_id, self._transcript.last_seq)

    @Slot()
    def send(self) -> None:
        text = self._get_draft()
        if (
            not self._connection
            or not self._chat_id
            or not self._connected
            or self._busy
            or not text.strip()
            or self._status in ("paused", "pausing", "aborting")
        ):
            return
        epoch, identity = self._epoch, self._chat_id
        self._busy = True
        self._error = ""
        self.changed.emit()

        def sent(payload: Any, error: str) -> None:
            if not error and self._drafts.get(identity) == text:
                self._drafts[identity] = ""
            if epoch != self._epoch:
                return
            self._busy = False
            if error:
                self._error = error + " Your draft was kept; check history before retrying."
            else:
                self._chat_title = payload["chat"]["title"] or "New conversation"
                self.refresh()
            self.draftChanged.emit()
            self.changed.emit()

        self._connection.call("POST", f"/api/chats/{identity}/messages", {"text": text}, sent)

    @Slot(str)
    def control(self, action: str) -> None:
        if (
            not self._connection
            or not self._connected
            or not self._chat_id
            or self._busy
            or action not in ("pause", "abort", "resume")
        ):
            return
        self._busy = True
        epoch = self._epoch
        self.changed.emit()

        def controlled(_: Any, error: str) -> None:
            if epoch != self._epoch:
                return
            self._busy = False
            self._error = error
            self.changed.emit()

        path = "resume" if action == "resume" else "cancel"
        self._connection.call(
            "POST",
            f"/api/chats/{self._chat_id}/{path}",
            None if action == "resume" else {"cause": action},
            controlled,
        )

    @Slot()
    def dismissError(self) -> None:
        self._error = ""
        self.changed.emit()

    @Slot()
    def shutdown(self) -> None:
        if self._quitting:
            return
        self._quitting = True
        self._detach()
        self._connection_label = "Saving and closing…"
        self.settings.sync()
        self.changed.emit()
        self.runtime.stop()
