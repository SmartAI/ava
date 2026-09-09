"""Native MCP settings and runtime status for each execution workspace."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from urllib.parse import urlencode
from uuid import uuid4

from PySide6.QtCore import Property, QObject, Signal, Slot

from .board import SummaryList

if TYPE_CHECKING:
    from .controller import Controller


class MCPView(QObject):
    changed = Signal()
    optionsChanged = Signal()
    editRequested = Signal(dict)
    saved = Signal()

    def __init__(self, owner: Controller) -> None:
        super().__init__(owner)
        self.owner = owner
        self._rows, self._tools = SummaryList(self), SummaryList(self)
        self._all: list[dict] = []
        self._projects: list[dict] = []
        self._project = self._selected = self._error = self._editor_error = ""
        self._search = self._tool_search = self._schema = ""
        self._detail: dict = {}
        self._active = self._pending = self._saving = False
        self._epoch = 0
        self._create_id = ""
        self._revisions: dict[str, tuple[str, str]] = {}
        self._context = ("", "")
        owner.navigationChanged.connect(self._navigation_changed)
        owner.machinesChanged.connect(self._navigation_changed)

    @Property(QObject, constant=True)
    def rows(self) -> SummaryList:
        return self._rows

    @Property(QObject, constant=True)
    def toolRows(self) -> SummaryList:
        return self._tools

    @Property(list, notify=optionsChanged)
    def projects(self) -> list:
        return self._projects

    @Property(str, notify=changed)
    def project(self) -> str:
        return self._project

    @Property(str, notify=changed)
    def selected(self) -> str:
        return self._selected

    @Property(dict, notify=changed)
    def detail(self) -> dict:
        # QML reads this property for each binding. Keep schemas in Python until opened.
        return {key: value for key, value in self._detail.items() if key != "tools"}

    @Property(str, notify=changed)
    def error(self) -> str:
        return self._error

    @Property(str, notify=changed)
    def editorError(self) -> str:
        return self._editor_error

    @Property(str, notify=changed)
    def schema(self) -> str:
        return self._schema

    @Property(bool, notify=changed)
    def saving(self) -> bool:
        return self._saving

    @Property(bool, notify=changed)
    def loading(self) -> bool:
        return self._pending

    @Property(bool, notify=changed)
    def available(self) -> bool:
        return self._connection() is not None

    def _option(self) -> dict:
        return next((p for p in self._projects if p["id"] == self._project), {})

    def _connection(self):
        machine = self.owner._machines.get(self._option().get("machine", ""))
        return machine.connection if machine and "mcp" in machine.runtime.info.get("capabilities", []) else None

    def _path(self, identity: str = "", action: str = "") -> str:
        option = self._option()
        suffix = ("/" + identity if identity else "") + ("/" + action if action else "")
        return f"/api/projects/{option['project']}/mcp{suffix}?" + urlencode({"cwd": option["cwd"]})

    def _navigation_changed(self) -> None:
        options = self.owner._workspace_options()
        if options != self._projects:
            self._projects = options
            self.optionsChanged.emit()
        if not self._option():
            self.chooseProject(options[0]["id"] if options else "")
        self.changed.emit()

    @Slot(bool)
    def activate(self, active: bool) -> None:
        self._active = active
        if active:
            self._navigation_changed()
            context = (self.owner.property("projectId"), self.owner.property("workspacePath"))
            if context != self._context:
                self._context = context
                identity = next((p["id"] for p in self._projects if (p["project"], p["cwd"]) == context), "")
                if identity:
                    self.chooseProject(identity)
            self.refresh()

    def revision(self, machine: str, instance: str, revision: str) -> None:
        value = (instance, revision)
        if self._revisions.get(machine) != value:
            self._revisions[machine] = value
            if self._active and machine == self._option().get("machine"):
                self.refresh()

    @Slot(str)
    def chooseProject(self, identity: str) -> None:
        if self._saving or self._project == identity:
            return
        self._project = identity
        self._epoch += 1
        self._pending = False
        self._all = []
        self._rows.replace([])
        self.select("")
        if self._active:
            self.refresh()
        self.changed.emit()

    @Slot()
    def refresh(self) -> None:
        connection = self._connection()
        if not connection or self._pending:
            return
        epoch = self._epoch
        self._pending = True
        self.changed.emit()

        def loaded(payload, error):
            if epoch != self._epoch:
                return
            self._pending = False
            self._error = error
            if not error:
                self._all = payload["servers"]
                self.filter(self._search)
                self.select(self._selected)
            self.changed.emit()

        connection.call("GET", self._path(), None, loaded)

    @Slot(str)
    def filter(self, value: str) -> None:
        self._search = value
        query = value.strip().casefold()
        self._rows.replace([{**{key: value for key, value in row.items() if key != "tools"}, "tool_count": len(row["tools"])}
                            for row in self._all if not query or query in (row["name"] + " " + row["command_line"] + " " + row["url"]).casefold()])

    @Slot(str)
    def select(self, identity: str) -> None:
        if identity != self._selected:
            self._schema = ""
            self._tool_search = ""
        self._selected = identity
        self._detail = next((row for row in self._all if row["id"] == identity), {})
        if not self._detail:
            self._selected = ""
        self.filterTools(self._tool_search)
        self.changed.emit()

    @Slot(str)
    def filterTools(self, value: str) -> None:
        self._tool_search = value
        query = value.casefold().strip()
        self._tools.replace([{"id": tool["name"], "name": tool["name"], "title": tool.get("title"), "description": tool.get("description")}
                             for tool in self._detail.get("tools", [])
                             if not query or query in (tool["name"] + " " + (tool.get("description") or "")).casefold()])

    @Slot(str)
    def showSchema(self, name: str) -> None:
        tool = next((tool for tool in self._detail.get("tools", []) if tool["name"] == name), None)
        self._schema = json.dumps(tool["input_schema"], ensure_ascii=False, indent=2) if tool else ""
        self.changed.emit()

    @Slot(bool)
    def edit(self, existing: bool) -> None:
        self._editor_error = ""
        self._create_id = uuid4().hex
        self.editRequested.emit(dict(self._detail) if existing else {})
        self.changed.emit()

    @Slot(dict)
    def save(self, draft: dict) -> None:
        identity = draft.pop("id", "")
        if isinstance(draft.get("version"), float):
            draft["version"] = int(draft["version"])
        if not identity:
            draft["request_id"] = self._create_id
        self._write("POST", identity, "", draft, editor=True)

    @Slot(str)
    def action(self, action: str) -> None:
        if not self._selected:
            return
        body = {"version": self._detail["version"]}
        if action == "toggle":
            body["enabled"] = not self._detail["enabled"]
        self._write("DELETE" if action == "remove" else "POST", self._selected,
                    "" if action == "remove" else action, body)

    def _write(self, method: str, identity: str, action: str, body: dict, *, editor: bool = False) -> None:
        connection = self._connection()
        if not connection or self._saving:
            return
        self._saving = True
        self._error = self._editor_error = ""
        self.changed.emit()

        def saved(payload, error):
            self._saving = False
            if editor:
                self._editor_error = error
            else:
                self._error = error
            if not error:
                self._epoch += 1
                self._pending = False
                if editor:
                    self._selected = payload["id"]
                    self.saved.emit()
                self.refresh()
            self.changed.emit()

        connection.call(method, self._path(identity, action), body, saved)
