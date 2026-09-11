"""The QML-facing application model; all agent work stays in the backend."""

from __future__ import annotations

import json
import re
import shlex
import sys
import uuid
import weakref
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PySide6.QtCore import (
    Property,
    QBuffer,
    QIODevice,
    QObject,
    QProcess,
    QSettings,
    QTimer,
    QUrl,
    Signal,
    Slot,
)
from PySide6.QtGui import (
    QColor,
    QGuiApplication,
    QImageWriter,
    QTextBlockFormat,
    QTextCursor,
    QTextFormat,
)
from PySide6.QtQuick import QQuickTextDocument

from .analytics import AnalyticsView
from .automations import AutomationView
from .board import SessionBoard
from .browser import BrowserSession
from .browser_control import BrowserControl
from .connection import Connection
from .files import FILE_ERRORS, attachment, inspect_path, local_path
from .git_review import GitReview
from .mcp import MCPView
from .pdf import PdfImages
from .runtime import BackendProcess
from .skills import SkillsView
from .terminal import TerminalSession
from .transcript import Transcript

COMMANDS = {
    "model": "Choose a model · /model [ID]",
    "effort": "Reasoning effort · /effort [LEVEL]",
    "skills": "Browse and use project and personal skills",
    "compact": "Summarize older history now",
    "context": "Inspect what the model sees",
    "files": "Browse project files",
    "diff": "Review project changes, stage files and commit",
    "terminal": "Open the project terminal",
    "browser": "Open a web page · /browser [URL]",
    "new": "Start a new conversation",
    "clear": "Start a new conversation",
    "pause": "Pause after the current step",
    "abort": "Stop the run now",
    "resume": "Continue a paused run",
    "copy": "Copy the last answer · /copy [code]",
    "theme": "Switch between light and dark",
    "login": "Store an API key · /login [PROVIDER]",
    "logout": "Remove a stored API key · /logout [PROVIDER]",
    "help": "Show all commands",
}


@dataclass
class Machine:
    id: str
    name: str
    runtime: BackendProcess
    machine_id: str = ""
    connection: Connection | None = None
    projects: list[dict] = field(default_factory=list)
    revision: int = -1
    checking: bool = False
    status: str = "Connecting…"
    error: str = ""
    reconnect: bool = False
    restarting: bool = False


class Controller(QObject):
    changed = Signal()
    navigationChanged = Signal()
    projectRemoved = Signal(str, str, str)
    serviceStateChanged = Signal()
    draftChanged = Signal()
    closed = Signal()
    panelRequested = Signal(str)
    dialogRequested = Signal(str, str, list)
    contextRequested = Signal(dict)
    skillsRequested = Signal()
    loginRequested = Signal(str)
    providerSettingsChanged = Signal()
    providerSettingsLoaded = Signal(dict)
    modelSelectionSaved = Signal()
    providerKeyRemoved = Signal()
    readingSizeChanged = Signal()
    browserRequested = Signal(str)
    fileRequested = Signal(str, str)
    machinesChanged = Signal()
    hostKeyChanged = Signal()
    themeRequested = Signal()
    clipboardChanged = Signal()

    def __init__(self, cwd: Path, arguments: list[str], settings: QSettings) -> None:
        super().__init__()
        self.settings = settings
        QGuiApplication.clipboard().dataChanged.connect(self.clipboardChanged)
        self.pdf_images = PdfImages()
        self._cwd = cwd
        self._browser = BrowserSession(self)
        self._browser_control = BrowserControl(self)
        from .attachments import AttachmentPreview
        self._attachment_preview = AttachmentPreview(self)
        self.runtime = BackendProcess(cwd, self)
        self._new_chat_selection = {
            name.removeprefix("--"): value
            for name, value in zip(arguments[::2], arguments[1::2], strict=True)
        }
        self._connection: Connection | None = None
        self._transcript = Transcript(self)
        self._transcript.changed.connect(self.changed)
        self._projects: list[dict[str, Any]] = []
        self._navigation_revision = -1
        self._expanded_session_projects: set[str] = set()
        self._project_id = ""
        self._chat_cwd = ""
        self._chat_branch = ""
        self._chat_ready: Callable[[], None] | None = None
        self._chat_id = ""
        self._chat_title = "New conversation"
        self._model_name = ""
        self._status = "idle"
        self._connection_label = "Starting Ava…"
        self._conversation_visible = True
        self._connected = False
        self._busy = False
        self._error = ""
        self._drafts: dict[str, str] = {}
        self._attachments: dict[str, list[dict]] = {}
        self._models: dict[str, Any] = {}
        self._model_revision = 0
        self._skills: list[dict] = []
        self._selection: dict[str, Any] = {}
        self._provider_settings_state: dict[str, Any] = {
            "loading": False,
            "saving": False,
            "error": "",
            "notice": "",
        }
        self._selecting = False
        self._files: dict[str, Any] = {}
        self._preview_objects: list[QObject] = []
        self._reviews: set[GitReview] = set()
        self._terminals: set[TerminalSession] = set()
        self._closed_emitted = False
        self._epoch = 0
        self._auto_reviewing: set[str] = set()
        self._quitting = False
        self._retry = QTimer(self)
        self._retry.setSingleShot(True)
        self._retry.setInterval(1500)
        self._retry.timeout.connect(self._reconnect)
        self._heartbeat = QTimer(self)
        self._heartbeat.setInterval(2000)
        self._heartbeat.timeout.connect(self._check_machines)
        self._checking_backend = False
        self._service_process: QProcess | None = None
        self._service_state: dict[str, Any] = {
            "loading": False, "busy": False, "installed": False, "active": False,
            "autostart": False, "error": "", "supported": sys.platform == "darwin" or sys.platform.startswith("linux"),
        }
        self._active_machine = "local"
        self._started = False
        self._machines: dict[str, Machine] = {}
        self._host_key_machine = ""
        self._register_machine(Machine("local", "This Mac" if sys.platform == "darwin" else "This computer", self.runtime))
        try:
            saved = json.loads(str(settings.value("machines", "[]")))
            for item in saved:
                if not all(isinstance(item.get(key), str) for key in ("id", "name", "host", "machine_id")):
                    continue
                runtime = BackendProcess(cwd, self, host=item["host"])
                machine = Machine(item["id"], item["name"], runtime, item["machine_id"])
                machine.projects = item.get("projects", [])
                self._register_machine(machine)
        except (ValueError, TypeError, AttributeError):
            self._error = "Saved machines could not be loaded. Add the SSH connection again."
        preferred_machine = str(settings.value("activeMachine", "local"))
        if preferred_machine in self._machines:
            self._active_machine = preferred_machine
            self.runtime = self._machines[preferred_machine].runtime
        self._board = SessionBoard(self)
        self._board.reviewRequested.connect(self._review_board_chat)
        self.navigationChanged.connect(self._refresh_board)
        self.machinesChanged.connect(self._refresh_board)
        self._refresh_board()
        self._automations = AutomationView(self)
        self._skill_view = SkillsView(self)
        self._mcp_view = MCPView(self)
        self._analytics = AnalyticsView(self)

    @Property(QObject, constant=True)
    def analytics(self) -> AnalyticsView:
        return self._analytics

    @Property(QObject, constant=True)
    def mcpView(self) -> MCPView:
        return self._mcp_view

    @Property(QObject, constant=True)
    def skillView(self) -> SkillsView:
        return self._skill_view

    @Slot()
    def reloadSkills(self) -> None:
        if self._chat_id and self._connection:
            def loaded(payload: dict) -> None:
                self._skills = payload["skills"]
                self.draftChanged.emit()
            self._chat_call("GET", "skills", None, loaded)

    @Property(QObject, constant=True)
    def automations(self) -> AutomationView:
        return self._automations

    @Property(QObject, constant=True)
    def board(self) -> SessionBoard:
        return self._board

    def _refresh_board(self) -> None:
        self._board.update(self.property("machines"), self._projects)

    def _review_board_chat(self, identity: str, through: int) -> None:
        machine = self._machine_for(identity)
        if not machine.connection:
            self._board.reviewed(identity, "Reconnect to this machine before marking a result reviewed.")
            return

        def finished(payload: Any, error: str) -> None:
            if not error:
                self._chat_metadata(payload, "")
            self._board.reviewed(identity, error)

        machine.connection.call("POST", f"/api/chats/{identity}/review", {"through": through}, finished)

    def _auto_review_result(self, identity: str, through: int) -> None:
        """Acknowledge a result the user is already looking at in the conversation."""
        if through < 0 or identity in self._auto_reviewing:
            return
        chat = next((c for project in self._projects for c in project["chats"] if c["id"] == identity), None)
        if chat is None or through <= chat.get("reviewed_through", -1):
            return
        machine = self._machine_for(identity)
        if not machine.connection:
            return
        self._auto_reviewing.add(identity)

        def finished(payload: Any, error: str) -> None:
            self._auto_reviewing.discard(identity)
            if not error:
                self._chat_metadata(payload, "")

        machine.connection.call("POST", f"/api/chats/{identity}/review", {"through": through}, finished)

    def _register_machine(self, machine: Machine) -> None:
        self._machines[machine.id] = machine
        owner = weakref.proxy(self)
        machine.runtime.ready.connect(lambda port, token: owner._machine_ready(machine, port, token))
        machine.runtime.failed.connect(lambda error: owner._machine_failed(machine, error))
        machine.runtime.stopped.connect(lambda: owner._machine_stopped(machine))
        machine.runtime.progress.connect(lambda message: owner._machine_progress(machine, message))
        machine.runtime.hostKeyChanged.connect(lambda: owner._host_key_changed(machine))

    def _host_key_changed(self, machine: Machine) -> None:
        if machine.runtime.host_key:
            machine.reconnect = False
            machine.status = "Verify SSH host"
        current = self._machines.get(self._host_key_machine)
        if current is None or not current.runtime.host_key:
            self._host_key_machine = next((m.id for m in self._machines.values() if m.runtime.host_key), "")
        self.machinesChanged.emit()
        self.hostKeyChanged.emit()

    @Property(dict, notify=hostKeyChanged)
    def hostKeyRequest(self) -> dict:
        # Concurrent connections queue naturally; answering one cannot approve another.
        machine = self._machines.get(self._host_key_machine)
        if machine and machine.runtime.host_key:
            return {**machine.runtime.host_key, "machine": machine.id,
                    "name": machine.name, "alias": machine.runtime.host}
        return {}

    @Slot(str, str, bool)
    def answerHostKey(self, identity: str, request: str, accept: bool) -> None:
        if machine := self._machines.get(identity):
            machine.runtime.answer_host_key(request, accept)

    def _save_machines(self) -> None:
        self.settings.setValue("machines", json.dumps([
            {"id": m.id, "name": m.name, "host": m.runtime.host, "machine_id": m.machine_id, "projects": m.projects}
            for m in self._machines.values() if m.runtime.host
        ]))

    @Property(list, notify=machinesChanged)
    def machines(self) -> list:
        return [{"id": m.id, "name": m.name, "host": m.runtime.host, "online": m.connection is not None,
                 "status": "Restarting backend…" if m.restarting else m.status,
                 "state": "connecting" if m.restarting else "online" if m.connection else "offline" if m.error else "connecting",
                 "error": m.error, "active": m.id == self._active_machine,
                 "restarting": m.restarting,
                 "update_pending": bool(m.runtime.info.get("service", {}).get("update_pending")),
                 "busy": m.restarting or (m.runtime.process.state() != QProcess.ProcessState.NotRunning and not m.connection)}
                for m in self._machines.values()]

    @Property(str, notify=changed)
    def machineName(self) -> str:
        return self._machines[self._active_machine].name

    @Property(bool, notify=changed)
    def remoteMachine(self) -> bool:
        return bool(self.runtime.host)

    @Slot(str, str)
    def addMachine(self, host: str, name: str) -> None:
        from .ssh import ssh_arguments

        host, name = host.strip(), name.strip()
        try:
            ssh_arguments(host)
        except ValueError as error:
            self._notice(str(error))
            return
        if any(m.runtime.host == host for m in self._machines.values()):
            self._notice("This SSH connection is already in Machines.")
            return
        runtime = BackendProcess(self._cwd, self, host=host)
        self._register_machine(Machine(uuid.uuid4().hex, name or host, runtime))
        self._save_machines()
        self.machinesChanged.emit()
        runtime.start()

    @Slot(str)
    def reconnectMachine(self, identity: str) -> None:
        if (machine := self._machines.get(identity)) and not machine.connection and not machine.restarting:
            machine.error = ""
            machine.status = "Connecting…"
            self.machinesChanged.emit()
            machine.runtime.start()

    @Slot(str)
    def updateMachine(self, identity: str) -> None:
        machine = self._machines.get(identity)
        if not machine or not machine.runtime.host or not machine.connection or not machine.runtime.info.get("service", {}).get("update_pending"):
            return
        self._detach(machine)
        machine.error, machine.status = "", "Updating backend…"
        if machine.id == self._active_machine:
            self._connection_label = "Updating backend…"
        self._restart_machine(machine)
        self.machinesChanged.emit()
        self.changed.emit()

    @Slot(str)
    def removeMachine(self, identity: str) -> None:
        if identity == "local" or identity not in self._machines or self._machines[identity].restarting:
            return
        machine = self._machines[identity]
        machine.reconnect = False
        if self._active_machine == identity:
            self.selectMachine("local")
        self._detach(machine)
        # Keep ownership until the helper exits, so window shutdown can reap it.
        machine.runtime.stopped.connect(lambda: self._forget_machine(machine))
        machine.runtime.stop()

    def _forget_machine(self, machine: Machine) -> None:
        projects = {project["id"] for project in machine.projects}
        workspaces: list[TerminalSession | GitReview] = [*self._terminals, *self._reviews]
        for workspace in workspaces:
            if workspace.project_id in projects:
                workspace.close()
        self._machines.pop(machine.id, None)
        machine.runtime.deleteLater()
        self._save_machines()
        self._rebuild_projects()
        self.machinesChanged.emit()

    def _machine_for(self, identity: str) -> Machine:
        return next((m for m in self._machines.values() if any(p["id"] == identity or any(c["id"] == identity for c in p["chats"]) for p in m.projects)), self._machines[self._active_machine])

    @Slot(str, result=bool)
    def resourceOnline(self, identity: str) -> bool:
        return self._machine_for(identity).connection is not None

    @Slot(str, result=str)
    @Slot(str, str, result=str)
    def workspaceLabel(self, project: str, root: str = "") -> str:
        for machine in self._machines.values():
            for item in machine.projects:
                if item["id"] == project:
                    branch = next((c.get("worktree", "") for c in item["chats"] if root and c.get("cwd") == root), "")
                    return f"{machine.name} · {item['name']}" + (f" · {branch}" if branch else "")
        return "Project unavailable"

    @Slot(str)
    def selectMachine(self, identity: str) -> None:
        machine = self._machines.get(identity)
        if machine is None:
            return
        self._activate_machine(machine)
        if machine.projects:
            preferred = str(self.settings.value(f"machineProject/{machine.id}", ""))
            project = next((p for p in machine.projects if p["id"] == preferred), machine.projects[0])
            self.selectProject(project["id"])
        self.changed.emit()

    def _activate_machine(self, machine: Machine) -> None:
        if machine.id == self._active_machine:
            return
        self._clear_chat()
        self._active_machine = machine.id
        self.settings.setValue("activeMachine", machine.id)
        self.runtime, self._connection = machine.runtime, machine.connection
        self._project_id, self._files = "", {}
        self._connection_label = machine.status
        self._error = machine.error
        self._provider_settings_state.update(loading=False, saving=False, error="", notice="")
        self.providerSettingsChanged.emit()
        self.machinesChanged.emit()
        self.changed.emit()

    def _rebuild_projects(self) -> None:
        self._projects = [p for m in self._machines.values() for p in m.projects]
        self.navigationChanged.emit()

    @Property(dict, notify=serviceStateChanged)
    def serviceState(self) -> dict:
        return self._service_state

    @Slot()
    def loadServiceState(self) -> None:
        self._service_command("service-status")

    @Slot(bool)
    def configureStartup(self, enabled: bool) -> None:
        self._service_command("service-install" if enabled else "service-uninstall")

    @Slot(str)
    def restartMachine(self, identity: str) -> None:
        machine = self._machines.get(identity)
        if machine is not None and not machine.restarting:
            self._service_command("service-restart", machine)

    def _service_command(self, action: str, machine: Machine | None = None) -> None:
        if self._service_process is not None or self._quitting or not self._service_state["supported"]:
            return
        process = QProcess(self)
        self._service_process = process
        machine = machine or self._machines[self._active_machine]
        restarting = action == "service-restart"
        if restarting:
            machine.restarting = True
            machine.error = ""
            self.machinesChanged.emit()
        changing = action != "service-status"
        self._service_state.update(loading=True, busy=changing, error="")
        self.serviceStateChanged.emit()
        output, errors = bytearray(), bytearray()
        timer = QTimer(process)
        timer.setSingleShot(True)
        timer.setInterval(90_000)
        timer.timeout.connect(process.kill)

        def read() -> None:
            output.extend(process.readAllStandardOutput().data())
            errors.extend(process.readAllStandardError().data())
            if len(output) + len(errors) > 8192:
                process.kill()

        def finished(code: int = 1) -> None:
            if self._service_process is not process:
                return
            timer.stop()
            read()
            self._service_process = None
            self._service_state.update(loading=False, busy=False)
            try:
                if code:
                    detail = errors.decode("utf-8", "replace").strip().removeprefix("ava-backend: ")
                    detail = detail.replace("Stop them first or use --force.", "Stop them from their conversations before restarting or changing background startup.")
                    raise ValueError(detail or "Background operation did not finish. Refresh to check its status.")
                payload = json.loads(output)
                if not isinstance(payload, dict) or not isinstance(payload.get("installed"), bool):
                    raise ValueError("Invalid background service status.")
                self._service_state.update(payload)
            except ValueError as error:
                self._service_state["error"] = str(error)
            if restarting:
                machine.restarting = False
                machine.error = self._service_state["error"]
                self.machinesChanged.emit()
            self.serviceStateChanged.emit()
            process.deleteLater()
            if changing and not self._service_state["error"] and not self._quitting:
                self._detach(machine)
                self._restart_machine(machine)
            self._finish_shutdown()

        process.readyReadStandardOutput.connect(read)
        process.readyReadStandardError.connect(read)
        process.finished.connect(lambda code, _: finished(code))
        process.errorOccurred.connect(lambda error: finished() if error == QProcess.ProcessError.FailedToStart else None)
        if machine.runtime.host:
            from .ssh import ssh_arguments

            python = machine.runtime.info.get("python")
            if not python:
                self._service_process = None
                machine.restarting = False
                machine.error = "Connect to this machine first."
                self.machinesChanged.emit()
                self._service_state.update(loading=False, busy=False, error=machine.error)
                self.serviceStateChanged.emit()
                process.deleteLater()
                return
            args = ssh_arguments(machine.runtime.host)
            process.setProgram(args[0])
            process.setArguments([*args[1:], machine.runtime.host, shlex.join([python, "-m", "ava.app.backend", action, *(["--machine-id", machine.machine_id] if restarting else [])])])
        else:
            process.setProgram(sys.executable)
            process.setArguments(["-m", "ava.app.backend", action])
        process.start()
        timer.start()

    @Property(QObject, constant=True)
    def transcript(self) -> QObject:
        return self._transcript

    @Property(dict, notify=providerSettingsChanged)
    def providerSettingsState(self) -> dict:
        return self._provider_settings_state

    @Property(int, notify=readingSizeChanged)
    def readingSize(self) -> int:
        try:
            value = int(str(self.settings.value("ui/readingSize", 15)))
        except ValueError:
            return 15
        return value if value in (13, 15, 17) else 15

    @Slot(int)
    def saveReadingSize(self, size: int) -> None:
        if size in (13, 15, 17):
            self.settings.setValue("ui/readingSize", size)
            self.readingSizeChanged.emit()

    @Slot()
    def loadProviderSettings(self) -> None:
        if not self._connection:
            self._provider_settings_state.update(error="Ava is offline. Reconnect and try again.")
            self.providerSettingsChanged.emit()
            return
        if self._provider_settings_state["saving"] or self._provider_settings_state["loading"]:
            return
        self._provider_settings_state = {
            "loading": True,
            "saving": False,
            "error": "",
            "notice": "",
        }
        self.providerSettingsChanged.emit()
        machine_id = self._active_machine

        def loaded(payload: Any, error: str) -> None:
            if machine_id != self._active_machine:
                return
            self._provider_settings_state.update(loading=False, error=error)
            if not error:
                self.providerSettingsLoaded.emit(payload)
            self.providerSettingsChanged.emit()

        self._connection.call("GET", "/api/settings", None, loaded)

    @Slot("QVariantMap")
    def saveProviderSettings(self, values: dict) -> None:
        if (
            not self._connection
            or self._provider_settings_state["saving"]
            or self._provider_settings_state["loading"]
        ):
            return
        self._provider_settings_state = {
            "loading": False,
            "saving": True,
            "error": "",
            "notice": "",
        }
        self.providerSettingsChanged.emit()
        machine_id = self._active_machine

        def saved(payload: Any, error: str) -> None:
            if machine_id != self._active_machine:
                return
            self._provider_settings_state.update(saving=False, error=error)
            if not error:
                entry: dict[str, Any] = next((item for item in payload.get("providers", []) if item["id"] == payload.get("saved_provider")), {})
                self._provider_settings_state["notice"] = "Connection saved. " + entry.get("message", "")
                self.providerSettingsLoaded.emit(payload)
            self.providerSettingsChanged.emit()

        self._connection.call("PUT", "/api/settings", values, saved)

    @Slot(str)
    def removeProviderKey(self, provider: str) -> None:
        if (
            not self._connection
            or self._provider_settings_state["saving"]
            or not re.fullmatch(r"[a-z][a-z0-9-]*", provider)
        ):
            return
        self._provider_settings_state = {
            "loading": False,
            "saving": True,
            "error": "",
            "notice": "",
        }
        self.providerSettingsChanged.emit()
        machine_id = self._active_machine

        def removed(payload: Any, error: str) -> None:
            if machine_id != self._active_machine:
                return
            self._provider_settings_state.update(saving=False, error=error)
            if not error:
                failures = payload.get("failed", {}) if payload else {}
                self._provider_settings_state["notice"] = f"Stored key removed for {provider}." + (
                    f" Some chats could not reload credentials: {failures}" if failures else ""
                )
                self.providerKeyRemoved.emit()
            self.providerSettingsChanged.emit()

        self._connection.call("DELETE", "/api/credentials/" + provider, None, removed)

    @Property(QObject, constant=True)
    def browserSession(self) -> QObject:
        return self._browser

    @Property(QObject, constant=True)
    def browserControl(self) -> QObject:
        return self._browser_control

    @Slot(int)
    def handoffBrowser(self, identity: int) -> None:
        if self._connection is not None and self._chat_id and self._connected:
            self._browser_control.attach(identity, self._connection, self._chat_id, self._chat_title)

    @Property(QObject, constant=True)
    def attachmentPreview(self) -> QObject:
        return self._attachment_preview

    @Slot("QVariantMap")
    def openToolImage(self, image: dict) -> None:
        if self._connection is not None and self._chat_id:
            self._attachment_preview.open(self._connection, self._chat_id, image)
        else:
            self._error = "Reconnect to the session's machine to view this image."
            self.changed.emit()

    def _workspace_options(self) -> list[dict]:
        options: list[dict] = []
        for project in self._projects:
            base = {"id": project["id"], "project": project["id"], "machine": project["machine"],
                    "cwd": project["path"], "name": self._machines[project["machine"]].name + " · " + project["name"]}
            options.append(base)
            seen = {project["path"]}
            for chat in project["chats"]:
                if chat.get("cwd") and chat["cwd"] not in seen:
                    seen.add(chat["cwd"])
                    options.append({**base, "id": chat["id"], "cwd": chat["cwd"],
                                    "name": base["name"] + " · " + (chat.get("worktree") or "Worktree")})
        return options

    @Property(list, notify=navigationChanged)
    def projects(self) -> list:
        return self._projects

    @Property(list, notify=navigationChanged)
    def sessionRows(self) -> list:
        rows: list[dict] = []
        pinned: list[dict] = []
        multiple = len(self._machines) > 1
        for machine in self._machines.values():
            expanded_machine = self.preference(f"machines/{machine.id}", True)
            if multiple:
                rows.append({"kind": "machine", "id": machine.id, "title": machine.name,
                             "expanded": expanded_machine, "online": machine.connection is not None,
                             "path": machine.runtime.host or "Local", "status": machine.status})
            for project in machine.projects:
                online = machine.connection is not None
                chats = [{**chat, "kind": "chat", "project_id": project["id"],
                          "project": project["name"] + (" · " + machine.name if multiple else ""),
                          "machine": machine.id, "online": online,
                          "pinned": self.preference(f"pinned/{chat['id']}", False)}
                         for chat in project["chats"] if not chat["archived"]]
                pinned.extend(chat for chat in chats if chat["pinned"])
                if multiple and not expanded_machine:
                    continue
                expanded = self.preference(f"groups/{project['id']}", True)
                rows.append({"kind": "project", "id": project["id"], "title": project["name"],
                             "path": project["path"], "expanded": expanded, "count": len(chats),
                             "machine": machine.id, "online": online})
                if expanded:
                    unpinned = [chat for chat in chats if not chat["pinned"]]
                    show_all = project["id"] in self._expanded_session_projects
                    rows.extend(unpinned if show_all else unpinned[:5])
                    if len(unpinned) > 5:
                        rows.append({"kind": "more", "id": "more:" + project["id"],
                                     "project_id": project["id"], "expanded": show_all,
                                     "title": "Show fewer" if show_all else f"Show more ({len(unpinned) - 5})"})
        return ([{"kind": "section", "id": "pinned", "title": "Pinned"}, *pinned] if pinned else []) + [
            {"kind": "section", "id": "projects", "title": "Machines" if multiple else "Projects"}
        ] + rows

    @Slot(str)
    def toggleProjectSessions(self, identity: str) -> None:
        if identity in self._expanded_session_projects:
            self._expanded_session_projects.remove(identity)
        elif any(project["id"] == identity for project in self._projects):
            self._expanded_session_projects.add(identity)
        self.navigationChanged.emit()

    def _reveal_sidebar_chat(self, identity: str) -> None:
        for project in self._projects:
            unpinned = [chat["id"] for chat in project["chats"]
                        if not chat["archived"] and not self.preference(f"pinned/{chat['id']}", False)]
            if identity not in unpinned:
                continue
            self.savePreference(f"groups/{project['id']}", True)
            self.savePreference(f"machines/{project.get('machine', 'local')}", True)
            if unpinned.index(identity) >= 5:
                self._expanded_session_projects.add(project["id"])
            self.navigationChanged.emit()
            break

    @Slot(str)
    def toggleMachineGroup(self, identity: str) -> None:
        self.savePreference(f"machines/{identity}", not self.preference(f"machines/{identity}", True))
        self.navigationChanged.emit()

    @Slot(str)
    def toggleProjectGroup(self, identity: str) -> None:
        key = f"groups/{identity}"
        self.savePreference(key, not self.preference(key, True))
        self.navigationChanged.emit()

    @Slot(str, bool, result=list)
    def searchChats(self, query: str, archived: bool) -> list:
        words = query.casefold().split()
        matches = []
        for project in self._projects:
            for chat in project["chats"]:
                label = f"{chat['title']} {project['name']} {project['path']}".casefold()
                if chat["archived"] == archived and all(word in label for word in words):
                    matches.append(
                        {
                            **chat,
                            "project": project["name"],
                            "project_id": project["id"],
                            "pinned": self.preference(f"pinned/{chat['id']}", False),
                        }
                    )
        return sorted(matches, key=lambda chat: not chat["pinned"])

    @Slot(str)
    def toggleChatPin(self, identity: str) -> None:
        key = f"pinned/{identity}"
        pinned = not self.preference(key, False)
        self.savePreference(key, pinned)
        if not pinned:
            self._reveal_sidebar_chat(identity)
        self.navigationChanged.emit()

    def _chat_metadata(self, payload: dict, error: str) -> None:
        if error:
            self._error = error
        else:
            for project in self._projects:
                for chat in project["chats"]:
                    if chat["id"] == payload["id"]:
                        chat.update(payload)
            if payload["id"] == self._chat_id:
                if payload["archived"]:
                    self._clear_chat()
                else:
                    self._chat_title = payload["title"] or "New conversation"
            self.navigationChanged.emit()
        self.changed.emit()

    def _discard_current_empty_chat(self) -> None:
        if not self._connection or not self._chat_id or self._status != "idle":
            return
        identity = self._chat_id
        found = next(
            (
                (project, chat)
                for project in self._projects
                for chat in project["chats"]
                if chat["id"] == identity
            ),
            None,
        )
        if (
            found is None
            or found[1]["title"]
            or found[1]["archived"]
            or found[1].get("worktree")
            or found[1].get("model_configured")
            or self.preference(f"pinned/{identity}", False)
            or self._drafts.get(identity, "")
            or self._attachments.get(identity, [])
        ):
            return
        project_id = found[0]["id"]

        def discarded(_: Any, error: str) -> None:
            if error:
                self._error = f"Could not remove unused chat: {error}"
                self.changed.emit()
                return
            for project in self._projects:
                project["chats"] = [chat for chat in project["chats"] if chat["id"] != identity]
            self._drafts.pop(identity, None)
            self._attachments.pop(identity, None)
            self.settings.remove(f"ui/pinned/{identity}")
            self.settings.remove(f"ui/worktree/{identity}")
            self.settings.remove(f"worktreeRequest/{identity}")
            if str(self.settings.value(f"chat/{project_id}", "")) == identity:
                self.settings.remove(f"chat/{project_id}")
            self.navigationChanged.emit()

        self._connection.call("DELETE", f"/api/chats/{identity}", None, discarded)

    @Slot(str, str)
    def renameChat(self, identity: str, title: str) -> None:
        if connection := self._machine_for(identity).connection:
            connection.call(
                "PATCH", f"/api/chats/{identity}", {"title": title}, self._chat_metadata
            )

    @Slot(str, bool)
    def archiveChat(self, identity: str, archived: bool) -> None:
        if connection := self._machine_for(identity).connection:
            connection.call(
                "POST",
                f"/api/chats/{identity}/archive",
                {"archived": archived},
                self._chat_metadata,
            )

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
    def workspacePath(self) -> str:
        return self._workspace_path()

    @Property(str, notify=changed)
    def workspaceBranch(self) -> str:
        return self._chat_branch

    def _workspace_path(self) -> str:
        return self._chat_cwd or str(self._project().get("path", ""))

    def _can_configure_session(self) -> bool:
        return bool(self._chat_id and self._connected and not self._busy and not self._selecting
                    and self._status == "idle" and not self._transcript.rows and not self._chat_branch)

    canConfigureSession = Property(bool, _can_configure_session, notify=changed)

    @Property(list, notify=navigationChanged)
    def sessionProjects(self) -> list:
        return [p for p in self._projects if p.get("machine", "local") == self._active_machine]

    def _session_worktree(self) -> bool:
        return bool(self._chat_branch) or self.preference(f"worktree/{self._chat_id}", False)

    sessionWorktree = Property(bool, _session_worktree, notify=changed)

    @Slot(bool)
    def setSessionWorktree(self, enabled: bool) -> None:
        if not self._can_configure_session():
            return
        self.savePreference(f"worktree/{self._chat_id}", enabled)
        self._error = ""
        self.changed.emit()

    @Slot(str)
    def changeSessionProject(self, project_id: str) -> None:
        if self._can_configure_session() and project_id != self._project_id:
            if any(p["id"] == project_id and p.get("machine", "local") == self._active_machine for p in self._projects):
                self._create_chat(project_id, replace_draft=True)

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

    def _get_conversation_visible(self) -> bool:
        return self._conversation_visible

    def _set_conversation_visible(self, value: bool) -> None:
        if value != self._conversation_visible:
            self._conversation_visible = value
            self.changed.emit()

    conversationVisible = Property(bool, _get_conversation_visible, _set_conversation_visible, notify=changed)

    @Slot()
    def reviewCurrentChat(self) -> None:
        if self._conversation_visible and self._chat_id:
            chat = next((c for project in self._projects for c in project["chats"] if c["id"] == self._chat_id), None)
            if chat is not None:
                self._auto_review_result(self._chat_id, chat.get("completion_seq", -1))

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

    @Property(list, notify=draftChanged)
    def attachments(self) -> list:
        return self._attachments.get(self._chat_id, [])

    @Property("QVariantMap", notify=changed)  # type: ignore[arg-type]
    def modelChoices(self) -> dict:
        return self._models

    @Property("QVariantMap", notify=changed)  # type: ignore[arg-type]
    def selection(self) -> dict:
        return self._selection

    @Property(bool, notify=changed)
    def selecting(self) -> bool:
        return self._selecting

    @Property("QVariantMap", notify=changed)  # type: ignore[arg-type]
    def fileState(self) -> dict:
        return self._files

    @Slot(str, bool, result=bool)
    def preference(self, name: str, default: bool) -> bool:
        return bool(self.settings.value(f"ui/{name}", default, type=bool))

    @Slot(str, bool)
    def savePreference(self, name: str, value: bool) -> None:
        self.settings.setValue(f"ui/{name}", value)

    @Slot(str, int, result=int)
    def panelWidth(self, name: str, default: int) -> int:
        value = self.settings.value(f"ui/{name}Width", default)
        try:
            return int(str(value))
        except ValueError:
            return default

    @Slot(int, int)
    def savePanelWidths(self, left: int, right: int) -> None:
        if left > 0:
            self.settings.setValue("ui/leftWidth", left)
        if right > 0:
            self.settings.setValue("ui/rightWidth", right)

    @Property(int, constant=True)
    def terminalHeight(self) -> int:
        try:
            return max(140, int(str(self.settings.value("ui/terminalHeight", 280))))
        except ValueError:
            return 280

    @Slot(int)
    def saveTerminalHeight(self, height: int) -> None:
        self.settings.setValue("ui/terminalHeight", height)

    @Slot(list)
    def addAttachments(self, paths: list) -> None:
        self._add_attachments(paths)

    @Slot(str, str)
    def addPreviewAttachment(self, path: str, name: str) -> None:
        self._add_attachments([path], name)

    def _add_attachments(self, paths: list, name: str = "") -> None:
        if not self._chat_id:
            return
        staged = self._attachments.setdefault(self._chat_id, [])
        try:
            additions: list[dict] = []
            for value in paths:
                path = local_path(str(value))
                if not path.is_file():
                    raise ValueError("Choose a regular file to attach.")
                with path.open("rb") as stream:
                    data = stream.read(8 * 1024 * 1024 + 1)
                additions.append(attachment(name or path.name, data, [*staged, *additions]))
            staged.extend(additions)
            self._error = ""
        except FILE_ERRORS as error:
            self._error = str(error)
        self.draftChanged.emit()
        self.changed.emit()

    @Slot(str)
    def removeAttachment(self, identity: str) -> None:
        self._attachments[self._chat_id] = [
            a for a in self._attachments.get(self._chat_id, []) if a["id"] != identity
        ]
        self.draftChanged.emit()

    @Property(bool, notify=clipboardChanged)
    def clipboardHasText(self) -> bool:
        mime = QGuiApplication.clipboard().mimeData()
        return bool(mime and mime.hasText() and mime.text())

    @Property(bool, notify=clipboardChanged)
    def clipboardHasAttachments(self) -> bool:
        mime = QGuiApplication.clipboard().mimeData()
        return bool(mime and (mime.hasImage() or any(url.isLocalFile() for url in mime.urls())))

    @Slot(result=bool)
    def pasteAttachments(self) -> bool:
        if not self._chat_id:
            return False
        clipboard = QGuiApplication.clipboard()
        mime = clipboard.mimeData()
        if mime.hasUrls():
            paths = [url.toString() for url in mime.urls() if url.isLocalFile()]
            if paths:
                self.addAttachments(paths)
                return True
        if not mime.hasImage():
            return False
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        if not QImageWriter(buffer, b"png").write(clipboard.image()):
            return False
        try:
            staged = self._attachments.setdefault(self._chat_id, [])
            staged.append(attachment("Pasted image.png", bytes(buffer.data().data()), staged))
            self._error = ""
        except FILE_ERRORS as error:
            self._error = str(error)
        self.draftChanged.emit()
        self.changed.emit()
        return True

    @Slot(str)
    def browseFiles(self, value: str = "") -> None:
        if self.remoteMachine:
            remote_root = self._workspace_path()
            if remote_root:
                self.fileRequested.emit(remote_root, value)
            return
        if not self._project().get("path"):
            return
        root = Path(self._workspace_path())
        try:
            path = local_path(value) if value else root
            if not path.is_absolute():
                path = root / path
            self._files = inspect_path(root, path)
            self._error = ""
        except FILE_ERRORS as error:
            self._error = str(error)
        self.fileRequested.emit(str(root), str(path))
        self.changed.emit()

    @Slot(str)
    def workspaceUnavailable(self, kind: str) -> None:
        feature = {"files": "file browsing", "changes": "Git review", "terminal": "terminals", "toggleTerminal": "terminals"}.get(kind, kind)
        self._notice(f"Remote {feature} is not available yet. Agent tools run on the selected remote machine.")

    @Slot(str)
    def openLink(self, value: str) -> None:
        url = QUrl(value)
        if url.scheme() in ("http", "https") and url.host():
            self.browserRequested.emit(url.toString())
        elif url.scheme() in ("", "file"):
            self.browseFiles(value)
        else:
            self._error = "Open an http(s) URL or a file in this project."
            self.changed.emit()

    def _chat_call(self, method: str, suffix: str, body: dict | None, done: Any) -> None:
        if not self._connection or not self._chat_id:
            return
        epoch = self._epoch

        def completed(payload: Any, error: str) -> None:
            if epoch != self._epoch:
                return
            if error:
                self._error = error
            else:
                done(payload)
            self.changed.emit()

        self._connection.call(method, f"/api/chats/{self._chat_id}/{suffix}", body, completed)

    @Slot()
    def loadModels(self) -> None:
        if self._selecting or not self._connection or not self._chat_id:
            return
        self._model_revision += 1
        revision = self._model_revision
        epoch = self._epoch
        self._models = {"loading": True}
        self._error = ""
        self.changed.emit()

        def loaded(payload: Any, error: str) -> None:
            if revision != self._model_revision or epoch != self._epoch:
                return
            self._models = payload if not error else {}
            self._error = error
            if not error:
                self._update_selection(payload)
            self.changed.emit()

        self._connection.call("GET", f"/api/chats/{self._chat_id}/models", None, loaded)

    def _update_selection(self, payload: dict) -> None:
        self._selection = {key: payload.get(key) for key in ("provider", "model", "effort")}
        self._model_name = " / ".join(str(payload.get(key) or "") for key in ("provider", "model"))

    def _select(self, body: dict) -> None:
        if not self._connection or not self._chat_id or self._selecting:
            return
        self._selecting = True
        self._model_revision += 1
        self.changed.emit()
        epoch = self._epoch

        def selected(payload: Any, error: str) -> None:
            if epoch != self._epoch:
                return
            self._selecting = False
            self._error = error
            if not error:
                self._update_selection(payload)
                for project in self._projects:
                    for chat in project["chats"]:
                        if chat["id"] == self._chat_id:
                            chat["model_configured"] = True
                self.modelSelectionSaved.emit()
            self.changed.emit()

        self._connection.call("POST", f"/api/chats/{self._chat_id}/model", body, selected)

    @Slot(str, str, str)
    def selectConversationModel(self, provider: str, model: str, effort: str) -> None:
        self._select({"provider": provider, "model": model, "effort": effort or None})

    @Slot(str)
    def selectModel(self, model: str) -> None:
        self._select({"model": model})

    @Slot(str)
    def selectEffort(self, effort: str) -> None:
        self._select({"effort": None if effort == "none" else effort})

    @Property(list, notify=draftChanged)
    def commandCandidates(self) -> list:
        draft = self._get_draft()
        if not re.fullmatch(r"/[^\s]*", draft):
            return []
        query = draft[1:].casefold()
        commands = [
            {"name": name, "description": description, "kind": "command"}
            for name, description in COMMANDS.items()
            if name.startswith(query)
        ]
        return commands + [
            {**skill, "kind": "skill"}
            for skill in self._skills
            if query in skill["name"].casefold()
        ]

    @Slot(str, str)
    def chooseCommand(self, name: str, kind: str) -> None:
        if kind == "skill":
            skill = next((s for s in self._skills if s["name"] == name), None)
            if skill:
                self._set_draft(f"${name} ")
        else:
            self._set_draft(f"/{name} ")

    def _notice(self, text: str) -> None:
        self.dialogRequested.emit("Ava", text, [])

    @Slot(str)
    def copyText(self, text: str) -> None:
        QGuiApplication.clipboard().setText(text)

    @Slot(QQuickTextDocument, QColor, QColor, str, result=list)
    def formatMarkdown(
        self,
        quick_document: QQuickTextDocument,
        link_color: QColor,
        code_background: QColor,
        code_font: str,
    ) -> list:
        """Style Qt's parsed document and expose the decorations TextEdit omits."""
        document = quick_document.textDocument()
        cursor = QTextCursor(document)
        cursor.beginEditBlock()
        decorations: list[dict] = []
        block = document.begin()
        while block.isValid():
            original = block.blockFormat()
            formatting = QTextBlockFormat(original)
            formatting.setLineHeight(150, QTextBlockFormat.LineHeightTypes.ProportionalHeight.value)
            formatting.setTopMargin(12 if formatting.headingLevel() and block.position() else 0)
            # Qt tags both indented and fenced code with BlockCodeLanguage.
            code_block = formatting.hasProperty(QTextFormat.Property.BlockCodeLanguage)
            language = formatting.stringProperty(QTextFormat.Property.BlockCodeLanguage)
            formatting.setBottomMargin(4 if block.textList() or code_block else 10)
            quote = formatting.intProperty(QTextFormat.Property.BlockQuoteLevel)
            decoration_kind = "code" if code_block else "quote" if quote else ""
            if decoration_kind:
                previous = block.previous()
                continuing = bool(
                    decorations
                    and decorations[-1]["kind"] == decoration_kind
                    and decorations[-1]["end"] == previous.position() + previous.length() - 1
                    and (not code_block or decorations[-1]["language"] == language)
                    and (
                        not code_block
                        or previous.blockFormat().hasProperty(QTextFormat.Property.BlockCodeFence)
                        == formatting.hasProperty(QTextFormat.Property.BlockCodeFence)
                    )
                )
                if continuing:
                    decorations[-1]["end"] = block.position() + block.length() - 1
                    decorations[-1]["text"] += "\n" + block.text()
                else:
                    decorations.append(
                        {
                            "kind": decoration_kind,
                            "start": block.position(),
                            "end": block.position() + block.length() - 1,
                            "text": block.text(),
                            "language": language,
                        }
                    )
                if code_block:
                    formatting.clearBackground()
                    formatting.setTopMargin(0 if continuing else 10)
                    next_format = block.next().blockFormat()
                    next_is_code = (
                        block.next().isValid()
                        and next_format.hasProperty(QTextFormat.Property.BlockCodeLanguage)
                        and next_format.stringProperty(QTextFormat.Property.BlockCodeLanguage)
                        == language
                        and next_format.hasProperty(QTextFormat.Property.BlockCodeFence)
                        == formatting.hasProperty(QTextFormat.Property.BlockCodeFence)
                    )
                    formatting.setBottomMargin(0 if next_is_code else 14)
                    formatting.setLineHeight(
                        125, QTextBlockFormat.LineHeightTypes.ProportionalHeight.value
                    )
                    formatting.setLeftMargin(12)
                    formatting.setRightMargin(12)
                else:
                    formatting.setLeftMargin(20)
                    formatting.setRightMargin(8)
            table = QTextCursor(block).currentTable()
            if table:
                formatting.setTopMargin(0)
                formatting.setBottomMargin(0)
                formatting.setLeftMargin(0)
                formatting.setRightMargin(0)
                cell = table.cellAt(block.position())
                if not block.text() and block.position() < cell.lastPosition():
                    # Qt may insert an empty leading block in the first Markdown cell.
                    formatting.setLineHeight(0, QTextBlockFormat.LineHeightTypes.FixedHeight.value)
            if formatting != original:
                cursor.setPosition(block.position())
                cursor.setBlockFormat(formatting)
            fragments = block.begin()
            while not fragments.atEnd():
                fragment = fragments.fragment()
                style = fragment.charFormat()
                original_style = fragment.charFormat()
                if style.isAnchor():
                    style.setForeground(link_color)
                if (
                    code_block
                    or style.fontFixedPitch()
                    or "monospace" in (style.fontFamilies() or [])
                ):
                    style.setFontFamilies([code_font])
                    if code_block:
                        style.clearBackground()
                    else:
                        style.setBackground(code_background)
                if style != original_style:
                    cursor.setPosition(fragment.position())
                    cursor.setPosition(
                        fragment.position() + fragment.length(), QTextCursor.MoveMode.KeepAnchor
                    )
                    cursor.setCharFormat(style)
                fragments += 1
            block = block.next()
        from PySide6.QtGui import QTextCharFormat, QTextLength, QTextTable

        frames = list(document.rootFrame().childFrames())
        while frames:
            frame = frames.pop()
            frames.extend(frame.childFrames())
            if isinstance(frame, QTextTable):
                table_format = frame.format()
                table_format.setCellPadding(8)
                table_format.setCellSpacing(0)
                table_format.setBorder(1)
                table_format.setTopMargin(12)
                table_format.setBottomMargin(12)
                table_format.setBorderBrush(code_background)
                table_format.setWidth(QTextLength(QTextLength.Type.PercentageLength, 100))
                frame.setFormat(table_format)
                for column in range(frame.columns()):
                    cell = frame.cellAt(0, column)
                    header = cell.firstCursorPosition()
                    header.setPosition(
                        cell.lastCursorPosition().position(), QTextCursor.MoveMode.KeepAnchor
                    )
                    header_style = QTextCharFormat()
                    header_style.setFontWeight(600)
                    header.mergeCharFormat(header_style)
        # Token colors only: keep Qt's native paragraph, link and selection layout.
        from pygments.lexers import get_lexer_by_name
        from pygments.styles import get_style_by_name
        from pygments.util import ClassNotFound

        dark = code_background.lightnessF() < 0.5
        syntax_style = get_style_by_name("native" if dark else "friendly")
        lexers: dict = {}
        token_formats: dict = {}
        for decoration in decorations:
            if decoration["kind"] != "code" or not decoration["language"]:
                continue
            try:
                language = decoration["language"]
                if language not in lexers:
                    lexers[language] = get_lexer_by_name(language)
                lexer = lexers[language]
            except ClassNotFound:
                continue
            position = decoration["start"]
            for _, token, value in lexer.get_tokens_unprocessed(decoration["text"]):
                end = position + len(value.encode("utf-16-le")) // 2
                token_format = token_formats.get(token)
                if token_format is None:
                    color = syntax_style.style_for_token(token)["color"] or (
                        "ececec" if dark else "202123"
                    )
                    token_format = QTextCharFormat()
                    token_format.setForeground(QColor("#" + color))
                    token_formats[token] = token_format
                cursor.setPosition(position)
                cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
                cursor.mergeCharFormat(token_format)
                position = end
        cursor.endEditBlock()
        return decorations

    def _remote_workspace(self, project: str) -> dict | None:
        machine = self._machine_for(project or self._project_id)
        if machine.id == "local":
            return None
        return {"host": machine.runtime.host, "python": machine.runtime.info.get("python", "python3"),
                "machine_id": machine.machine_id}

    @Slot(str, result=QObject)
    @Slot(str, str, result=QObject)
    def createGitReview(self, root: str, project: str = "") -> QObject:
        review = GitReview(root, self, remote=self._remote_workspace(project), local_cwd=str(self._cwd))
        review.project_id = project or self._project_id
        self._reviews.add(review)

        def closed():
            self._reviews.discard(review)
            review.deleteLater()
            self._finish_shutdown()

        review.closed.connect(closed)
        return review

    def _finish_shutdown(self) -> None:
        if (
            self._quitting
            and not self._reviews
            and not self._terminals
            and not self._closed_emitted
            and self._service_process is None
            and all(m.runtime.process.state() == QProcess.ProcessState.NotRunning for m in self._machines.values())
        ):
            self._closed_emitted = True
            self.closed.emit()

    @Slot(str, result=QObject)
    @Slot(str, str, result=QObject)
    def createTerminal(self, root: str, project: str = "") -> QObject:
        remote = self._remote_workspace(project)
        terminal = TerminalSession(root, self, remote=remote, local_cwd=str(self._cwd) if remote else root)
        terminal.project_id = project or self._project_id
        terminal.toggleRequested.connect(lambda: self.panelRequested.emit("toggleTerminal"))
        self._terminals.add(terminal)

        def closed():
            self._terminals.discard(terminal)
            terminal.deleteLater()
            self._finish_shutdown()

        terminal.closed.connect(closed)
        return terminal

    @Slot(str, result=QObject)
    @Slot(str, str, result=QObject)
    def createFileModel(self, root: str, project: str = "") -> QObject:
        from .files import ProjectFiles
        from .remote_files import RemoteFiles

        owner = self._machine_for(project or self._project_id)
        model: QObject
        if owner and owner.id != "local":
            controller = weakref.proxy(self)
            identity = owner.id

            def connection():
                machine = controller._machines.get(identity)
                return machine.connection if machine else None

            model = RemoteFiles(project or self._project_id, root, connection, self)
            self.machinesChanged.connect(model.connectionChanged)
        else:
            model = ProjectFiles(root, self)
        self._preview_objects.append(model)
        return model

    @Slot(str, result=QObject)
    def createPdfSource(self, source: str) -> QObject:
        from .pdf import PdfSource

        snapshot = PdfSource(source, self)
        self._preview_objects.append(snapshot)
        return snapshot

    @Slot(str, str, result=str)
    def registerPdf(self, source: str, password: str) -> str:
        return self.pdf_images.add(source, password)

    @Slot(str)
    def releasePdf(self, key: str) -> None:
        self.pdf_images.remove(key)

    @Slot(result=QObject)
    def createCodeDocument(self) -> QObject:
        from .highlight import CodeDocument

        document = CodeDocument(self)
        self._preview_objects.append(document)
        return document

    @Slot(QObject)
    def releasePreview(self, preview: QObject) -> None:
        if preview in self._preview_objects:
            self._preview_objects.remove(preview)
            if hasattr(preview, "close"):
                preview.close()
            preview.deleteLater()

    @Slot(str, str, result="QVariantMap")
    def previewFile(self, root: str, path: str) -> dict:
        try:
            state = inspect_path(Path(root), local_path(path))
            self._files = state
            self._error = ""
        except FILE_ERRORS as error:
            state = {"kind": "unsupported", "notice": str(error), "path": path}
            self._error = str(error)
        self.changed.emit()
        return state

    @Slot(str)
    def runCommand(self, line: str) -> None:
        name, _, argument = line.strip().lstrip("/").partition(" ")
        argument = argument.strip()
        if name == "model":
            if argument:
                self.selectModel(argument)
            else:
                self.panelRequested.emit("model")
        elif name == "effort":
            if argument:
                self.selectEffort(argument)
            else:
                self.panelRequested.emit("model")
        elif name == "skills":
            self.skillsRequested.emit()
        elif name in ("new", "clear"):
            self.newChat()
        elif name in ("pause", "abort", "resume"):
            self.control(name)
        elif name == "diff":
            self.panelRequested.emit("changes")
        elif name == "terminal":
            self.panelRequested.emit("terminal")
        elif name == "files":
            self.browseFiles(argument)
        elif name == "browser":
            if argument:
                self.openLink(argument if "://" in argument else "https://" + argument)
            else:
                self.panelRequested.emit("browser")
        elif name == "theme":
            self.themeRequested.emit()
        elif name == "compact":
            self._chat_call("POST", "compact", None, lambda p: self._notice(p["message"]))
        elif name == "context":
            self._chat_call("GET", "context", None, self.contextRequested.emit)
        elif name == "help":
            self.dialogRequested.emit(
                "Commands",
                "↑ ↓ to navigate · Tab to insert · Enter to run",
                [
                    {
                        "label": "/" + command,
                        "detail": description,
                        "action": "command",
                        "value": command,
                    }
                    for command, description in COMMANDS.items()
                ],
            )
        elif name == "copy":
            text = next(
                (r["body"] for r in reversed(self._transcript.rows) if r["kind"] == "assistant"), ""
            )
            if argument == "code":
                blocks = re.findall(r"```[^\n]*\n(.*?)```", text, re.DOTALL)
                text = blocks[-1] if blocks else ""
            if text:
                self.copyText(text)
            else:
                self._notice("Nothing to copy yet.")
        elif name in ("login", "logout"):
            provider = argument or self._selection.get("provider") or ""
            if name == "login":
                self.loginRequested.emit(provider)
            elif self._connection and provider:
                self._connection.call(
                    "DELETE",
                    "/api/credentials/" + provider,
                    None,
                    lambda _, error: self._notice(
                        error or f"Removed the stored key for {provider}."
                    ),
                )
        else:
            skill = next((s for s in self._skills if s["name"] == name), None)
            if skill:
                self.chooseCommand(name, "skill")
                self._set_draft(self._get_draft() + argument)
            else:
                self._error = f"Unknown command /{name}; try /help."
                self.changed.emit()

    @Slot(str, str)
    def login(self, provider: str, key: str) -> None:
        if self._connection and provider and key:

            def saved(payload: Any, error: str) -> None:
                failures = payload.get("failed", {}) if payload else {}
                self._notice(
                    error
                    or (
                        f"Stored a key for {provider}."
                        + (f" Reload failed: {failures}" if failures else "")
                    )
                )

            self._connection.call(
                "POST", "/api/credentials", {"provider": provider, "key": key}, saved
            )

    def _detach(self, machine: Machine | None = None) -> None:
        machine = machine or self._machines[self._active_machine]
        machine.checking = False
        if machine.connection:
            self._browser_control.connection_closed(machine.connection)
            machine.connection.close()
            machine.connection.deleteLater()
            machine.connection = None
        if machine.id != self._active_machine:
            return
        self._retry.stop()
        self._checking_backend = False
        self._epoch += 1
        self._connection = None
        self._connected = self._busy = False
        if self._provider_settings_state["loading"] or self._provider_settings_state["saving"]:
            self._provider_settings_state.update(
                loading=False, saving=False, error="Connection closed. Reopen settings to retry."
            )
            self.providerSettingsChanged.emit()

    @Slot()
    def start(self) -> None:
        self._retry.stop()
        self._error = ""
        self._connection_label = "Starting Ava…"
        self.changed.emit()
        self._heartbeat.start()
        if not self._started:
            self._started = True
            for machine in self._machines.values():
                machine.runtime.start()
        else:
            self.runtime.start()

    def _machine_ready(self, machine: Machine, port: int, token: str) -> None:
        if self._quitting:
            return
        actual = machine.runtime.info["machine_id"]
        if machine.machine_id and machine.machine_id != actual:
            machine.reconnect = False
            self._machine_failed(machine, "This SSH host now identifies a different Ava data store. Remove and add it again to review the new identity.")
            machine.runtime.stop()
            return
        if any(m is not machine and m.machine_id == actual for m in self._machines.values()):
            machine.reconnect = False
            self._machine_failed(machine, "This Ava data store is already connected under another machine.")
            machine.runtime.stop()
            return
        self._detach(machine)
        machine.machine_id = actual
        machine.reconnect = True
        machine.error, machine.status = "", "Connected"
        if machine.runtime.info.get("service", {}).get("update_pending"):
            machine.status = "Connected · update available"
        machine.revision = -1
        connection = Connection(port, token, self, authority=machine.runtime.info.get("authority", ""),
                                prefix=actual + "~" if machine.runtime.host else "")
        machine.connection = connection
        connection.received.connect(lambda event: self._event(event) if machine.id == self._active_machine else None)
        connection.status.connect(lambda value: self._snapshot(value) if machine.id == self._active_machine else None)
        connection.disconnected.connect(lambda message: self._disconnected(message) if machine.id == self._active_machine else None)
        if machine.id == self._active_machine:
            self._project_id = self._chat_id = ""
            self._connection = connection
            self._connection_label = "Connected"
        self._refresh_machine(machine)
        self._save_machines()
        self.machinesChanged.emit()
        self.navigationChanged.emit()
        self.changed.emit()

    def _failed(self, error: str) -> None:
        self._machine_failed(self._machines[self._active_machine], error)

    def _machine_failed(self, machine: Machine, error: str) -> None:
        self._detach(machine)
        machine.error, machine.status = error, "Offline"
        if machine.id == self._active_machine:
            self._error = error
            self._connection_label = "Backend unavailable"
        self.machinesChanged.emit()
        self.navigationChanged.emit()
        self.changed.emit()

    def _machine_progress(self, machine: Machine, message: str) -> None:
        machine.status = message
        self.machinesChanged.emit()

    def _machine_stopped(self, machine: Machine) -> None:
        self._detach(machine)
        if self._quitting:
            self._finish_shutdown()
        elif machine.id == self._active_machine:
            self._connection_label = "Disconnected"
            self.changed.emit()
        if machine.reconnect and machine.runtime.retryable and not self._quitting:
            def retry():
                if not self._quitting and machine.id in self._machines and machine.connection is None and machine.reconnect and machine.runtime.retryable:
                    machine.runtime.start()
            QTimer.singleShot(1500, retry)

    @Slot()
    def refresh(self) -> None:
        self._refresh_machine(self._machines[self._active_machine])

    def _refresh_machine(self, machine: Machine) -> None:
        if machine.connection:
            machine.connection.call("GET", "/api/projects", None, lambda value, error: self._refreshed(value, error, machine))

    def _refreshed(self, payload: Any, error: str, machine: Machine | None = None) -> None:
        machine = machine or self._machines[self._active_machine]
        if error:
            machine.error = error
            if machine.id == self._active_machine:
                self._error = error
        else:
            revision = payload.get("revision", 0)
            if revision < machine.revision:
                return
            machine.revision = revision
            machine.projects = [{**p, "machine": machine.id, "machine_name": machine.name} for p in payload["projects"]]
            self._rebuild_projects()
            self._save_machines()
            if machine.id != self._active_machine:
                return
            self._navigation_revision = revision
            if self._project_id not in [p["id"] for p in machine.projects]:
                self._clear_chat()
                self._project_id = ""
                self._files = {}
                preferred = str(self.settings.value(f"machineProject/{machine.id}", self.settings.value("project", "")))
                project = next(
                    (p for p in machine.projects if p["id"] == preferred),
                    machine.projects[0] if machine.projects else None,
                )
                if project:
                    self.selectProject(project["id"])
            elif self._chat_id:
                chat = next((c for c in self._chats() if c["id"] == self._chat_id), None)
                if chat:
                    self._chat_title = chat["title"] or "New conversation"
            self.navigationChanged.emit()
        self.changed.emit()

    def _clear_chat(self) -> None:
        self._epoch += 1
        self._chat_ready = None
        self._retry.stop()
        if self._connection:
            self._connection.close_stream()
        self._chat_id = ""
        self._chat_cwd = self._chat_branch = ""
        self._chat_title = "New conversation"
        self._model_name = ""
        self._selection = {}
        self._models = {}
        self._skills = []
        self._selecting = False
        self._status = "idle"
        self._busy = self._connected = False
        self._transcript.clear()
        self._error = ""
        self.draftChanged.emit()

    @Slot(str)
    def selectProject(self, identity: str) -> None:
        self._select_project(identity, True)

    def _select_project(self, identity: str, open_chat: bool, discard_empty: bool | None = None) -> None:
        if not any(p["id"] == identity for p in self._projects):
            return
        should_discard = identity != self._project_id if discard_empty is None else discard_empty
        if should_discard:
            self._discard_current_empty_chat()
        self._activate_machine(self._machine_for(identity))
        self._clear_chat()
        self._set_project(identity)
        if open_chat:
            preferred = str(self.settings.value(f"chat/{identity}", ""))
            chat = next(
                (c for c in self._chats() if c["id"] == preferred),
                self._chats()[0] if self._chats() else None,
            )
            if chat and self._connection:
                self.openChat(chat["id"])
        self.changed.emit()

    def _set_project(self, identity: str) -> None:
        self._project_id = identity
        self._files = {}
        try:
            root = Path(self._project()["path"])
            if not self.remoteMachine:
                self._files = inspect_path(root, root)
        except FILE_ERRORS as error:
            self._error = str(error)
        self.navigationChanged.emit()
        self.settings.setValue("project", identity)
        self.settings.setValue(f"machineProject/{self._active_machine}", identity)

    @Slot(str)
    def addProject(self, path: str) -> None:
        self._add_project(path)

    @Slot(str)
    def newChatInFolder(self, path: str) -> None:
        source_id = self._chat_id
        machine_id = self._active_machine

        def added(identity: str) -> None:
            if self._chat_id == source_id and self._active_machine == machine_id:
                self._create_chat(identity, replace_draft=self._can_configure_session())

        self._add_project(path, added)

    def _add_project(self, path: str, after: Callable[[str], None] | None = None) -> None:
        if not self._connection:
            return
        machine = self._machines[self._active_machine]
        url = QUrl(path)
        if url.isLocalFile():
            path = url.toLocalFile()

        def added(payload: Any, error: str) -> None:
            if error:
                self._error = error
            else:
                machine.revision = max(machine.revision, payload["revision"])
                if not any(p["id"] == payload["id"] for p in machine.projects):
                    machine.projects.append({**payload, "machine": machine.id, "machine_name": machine.name})
                self._rebuild_projects()
                if after is None:
                    self._select_project(payload["id"], True)
                else:
                    after(payload["id"])
            self.changed.emit()

        self._connection.call("POST", "/api/projects", {"path": path}, added)

    @Slot(str)
    def removeProject(self, identity: str) -> None:
        machine = self._machine_for(identity)
        if not machine.connection:
            return
        project = next((p for p in self._projects if p["id"] == identity), None)
        if project is None:
            return

        def removed(payload: Any, error: str) -> None:
            if error:
                self._error = error
                self.changed.emit()
                return
            # Apply immediately without deleting sessions, drafts, pins, or disk files.
            self._refreshed({
                "projects": [p for p in machine.projects if p["id"] != identity],
                "revision": payload["revision"],
            }, "", machine)
            self.projectRemoved.emit(project["name"], project["path"], machine.id)
            self._refresh_machine(machine)

        machine.connection.call("POST", f"/api/projects/{identity}/hide", {}, removed)

    @Slot()
    @Slot(str)
    def newChat(self, project_id: str = "") -> None:
        self._create_chat(project_id)

    def _create_chat(
        self, project_id: str, workspace: dict | None = None, *,
        replace_draft: bool = False, after: Callable[[], None] | None = None,
    ) -> None:
        project_id = project_id or self._project_id
        source_id = self._chat_id
        selection = dict(self._selection) if replace_draft else self._new_chat_selection
        worktree = self._session_worktree() if replace_draft else False
        machine = self._machine_for(project_id)
        if not machine.connection:
            self._error = "Machine disconnected. Reconnect and retry."
            self.changed.emit()
            return
        self._activate_machine(machine)
        if (
            not self._connection
            or self._busy
            or not any(p["id"] == project_id for p in self._projects)
        ):
            return
        self._busy = True
        self._error = ""
        epoch = self._epoch
        self.changed.emit()

        def created(payload: Any, error: str) -> None:
            if not error:
                project = next((p for p in self._projects if p["id"] == project_id), None)
                if project is not None and not any(
                    c["id"] == payload["id"] for c in project["chats"]
                ):
                    project["chats"].insert(0, payload)
                    self.savePreference(f"groups/{project_id}", True)
                    self.navigationChanged.emit()
            if epoch != self._epoch:
                return
            self._busy = False
            if error:
                self._error = error
            else:
                if replace_draft:
                    self._drafts[payload["id"]] = self._drafts.pop(source_id, "")
                    self._attachments[payload["id"]] = self._attachments.pop(source_id, [])
                    self.savePreference(f"worktree/{payload['id']}", worktree)
                self.openChat(payload["id"], after=after)
            self.changed.emit()

        self._connection.call(
            "POST", "/api/chats", {"project_id": project_id, **selection, **(workspace or {})}, created
        )

    @Slot(str)
    def openChat(self, identity: str, *, after: Callable[[], None] | None = None) -> None:
        machine = self._machine_for(identity)
        if not machine.connection:
            return
        if identity == self._chat_id:
            self.reviewCurrentChat()
            return
        self._discard_current_empty_chat()
        self._activate_machine(machine)
        self._clear_chat()
        self._chat_ready = after
        project = next((p for p in machine.projects if any(c["id"] == identity for c in p["chats"])), None)
        if project is not None and project["id"] != self._project_id:
            self._set_project(project["id"])
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
                if self._project_id != payload["project_id"]:
                    self._set_project(payload["project_id"])
                self._chat_id = identity
                self._reveal_sidebar_chat(identity)
                self._chat_cwd = payload["cwd"]
                self._chat_branch = payload.get("worktree", "")
                self._chat_title = payload["title"] or "New conversation"
                self._status = payload["status"]
                chat = next((c for project in self._projects for c in project["chats"] if c["id"] == identity), None)
                if self._conversation_visible and chat is not None:
                    self._auto_review_result(identity, chat.get("completion_seq", -1))
                self.settings.setValue(f"chat/{self._project_id}", identity)
                self._connection.stream(identity, -1)

                def loaded_skills(value: dict) -> None:
                    self._skills = value["skills"]
                    self.draftChanged.emit()

                self._chat_call("GET", "skills", None, loaded_skills)
            self.draftChanged.emit()
            self.changed.emit()

        machine.connection.call("GET", f"/api/chats/{identity}", None, opened)

    def _event(self, event: dict) -> None:
        try:
            self._transcript.apply(event)
            if event["kind"] == "selection":
                self._update_selection(event)
            if self._conversation_visible and self._chat_id and event["kind"] in {"turn/end", "drive/error"}:
                self._auto_review_result(self._chat_id, int(event["seq"]))
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
        self._update_selection(payload)
        self.changed.emit()
        after, self._chat_ready = self._chat_ready, None
        if after is not None:
            after()

    def _disconnected(self, message: str) -> None:
        self._connected = False
        self._connection_label = message
        self.changed.emit()
        if self._connection and self._chat_id and not self._quitting:
            self._retry.start()

    def _reconnect(self) -> None:
        if self._connection:
            self._check_backend(replay=True)
        elif not self._quitting:
            self.start()

    def _check_backend(self, replay: bool = False) -> None:
        self._check_machine(self._machines[self._active_machine], replay)

    def _check_machines(self) -> None:
        for machine in list(self._machines.values()):
            self._check_machine(machine)

    def _restart_machine(self, machine: Machine) -> None:
        if machine.runtime.process.state() == QProcess.ProcessState.NotRunning:
            machine.runtime.start()
        else:
            def retry():
                machine.runtime.stopped.disconnect(retry)
                if not self._quitting and machine.id in self._machines:
                    machine.runtime.start()
            machine.runtime.stopped.connect(retry)
            machine.runtime.stop()

    def _check_machine(self, machine: Machine, replay: bool = False) -> None:
        connection = machine.connection
        if connection is None or machine.checking or machine.restarting or self._quitting:
            return
        machine.checking = True

        def checked(payload: Any, error: str) -> None:
            if connection is not machine.connection:
                return
            machine.checking = False
            if machine.restarting:
                return
            identity = machine.runtime.info
            if error or not isinstance(payload, dict) or any(
                payload.get(key) != identity.get(key)
                for key in ("machine_id", "instance_id", "protocol")
            ):
                self._machine_failed(machine, "Backend connection lost. Reconnecting…")
                self._restart_machine(machine)
            else:
                self._mcp_view.revision(machine.id, payload["instance_id"], payload.get("mcp_revision", ""))
                self._skill_view.revision(machine.id, payload["instance_id"], payload.get("skill_revision", ""))
                self._automations.revision(machine.id, payload["instance_id"], payload.get("automation_revision", -1), payload.get("automation_error", ""))
                if payload.get("navigation_revision", -1) != machine.revision:
                    self._refresh_machine(machine)
                if replay and self._chat_id and machine.id == self._active_machine:
                    connection.stream(self._chat_id, self._transcript.last_seq)

        connection.call("GET", "/api/system", None, checked)

    @Slot()
    def send(self) -> None:
        self.submit(False)

    @Slot(bool)
    def submit(self, followup: bool) -> None:
        text = self._get_draft()
        attachments = list(self._attachments.get(self._chat_id, []))
        if text.lstrip().startswith("/") and not attachments:
            self._set_draft("")
            self.runCommand(text)
            return
        if self._status == "paused" and not text.strip() and not attachments and not followup:
            self.control("resume")
            return
        if (
            not self._connection
            or not self._chat_id
            or not self._connected
            or self._busy
            or (not text.strip() and not attachments)
            or self._status == "aborting"
        ):
            return
        if self._can_configure_session() and self._session_worktree():
            # Keep a stable key for a lost response or a failed checkout. Toggling
            # the checkbox does not create unused branches or directories.
            setting = f"worktreeRequest/{self._chat_id}"
            key = str(self.settings.value(setting, "")) or uuid.uuid4().hex
            self.settings.setValue(setting, key)
            self._create_chat(self._project_id, {
                "workspace": "worktree", "branch": "ava/" + key,
                "base_ref": "HEAD", "request_id": key,
            }, replace_draft=True, after=lambda: self._send_message(text, attachments, followup))
            return
        self._send_message(text, attachments, followup)

    def _send_message(self, text: str, attachments: list[dict], followup: bool) -> None:
        assert self._connection is not None
        epoch, identity = self._epoch, self._chat_id
        resume = self._status == "paused" and not followup
        delivery = "followup" if followup or self._status == "idle" else "steer"
        self._busy = True
        self._error = ""
        self.changed.emit()

        def sent(payload: Any, error: str) -> None:
            if not error and self._drafts.get(identity) == text:
                self._drafts[identity] = ""
            if not error:
                sent_ids = {a["id"] for a in attachments}
                self._attachments[identity] = [
                    a for a in self._attachments.get(identity, []) if a["id"] not in sent_ids
                ]
            if epoch != self._epoch:
                return
            self._busy = False
            if error:
                self._error = error + " Your draft was kept; check history before retrying."
            else:
                self._chat_title = payload["chat"]["title"] or "New conversation"
                self.refresh()
                if resume:
                    self.control("resume")
            self.draftChanged.emit()
            self.changed.emit()

        self._connection.call(
            "POST",
            f"/api/chats/{identity}/messages",
            {
                "text": text,
                "delivery": delivery,
                "attachments": [
                    {key: a[key] for key in ("name", "kind", "data_base64")} for a in attachments
                ],
            },
            sent,
        )

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
        self._mcp_view.cancelAuth()
        self._heartbeat.stop()
        self.pdf_images.close()
        self._browser.shutdown()
        self._browser_control.shutdown()
        self._attachment_preview.shutdown()
        for review in list(self._reviews):
            review.close()
        for terminal in list(self._terminals):
            terminal.close()
        for preview in self._preview_objects:
            if hasattr(preview, "close"):
                preview.close()
        for machine in list(self._machines.values()):
            self._detach(machine)
        self._connection_label = "Saving and closing…"
        self.settings.sync()
        self.changed.emit()
        for machine in list(self._machines.values()):
            machine.runtime.stop()
