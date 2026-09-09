"""Native skill management backed by the selected execution machine."""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import urlencode

from PySide6.QtCore import Property, QObject, Signal, Slot

from .board import SummaryList

if TYPE_CHECKING:
    from .controller import Controller


class SkillsView(QObject):
    changed = Signal()
    optionsChanged = Signal()
    saved = Signal()
    used = Signal()

    def __init__(self, owner: Controller) -> None:
        super().__init__(owner)
        self.owner = owner
        self._rows = SummaryList(self)
        self._all: list[dict] = []
        self._projects: list[dict] = []
        self._project = ""
        self._selected = ""
        self._detail: dict = {}
        self._error = self._editor_error = self._search = self._filter = ""
        self._busy = self._active = self._editing = False
        self._epoch = self._detail_epoch = 0
        self._revisions: dict[str, tuple[str, str]] = {}
        self._chat_context: tuple[str, str] = ("", "")
        owner.navigationChanged.connect(self._navigation_changed)
        owner.machinesChanged.connect(self._navigation_changed)

    @Property(QObject, constant=True)
    def rows(self) -> SummaryList:
        return self._rows

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
        return self._detail

    @Property(str, notify=changed)
    def error(self) -> str:
        return self._error

    @Property(str, notify=changed)
    def editorError(self) -> str:
        return self._editor_error

    @Property(bool, notify=changed)
    def busy(self) -> bool:
        return self._busy or self._editing

    @Property(bool, notify=changed)
    def available(self) -> bool:
        return self._connection() is not None

    @Property(bool, notify=changed)
    def canUse(self) -> bool:
        option = self._option()
        return bool(self._detail.get("effective") and self.owner.chatId and self.owner.projectId == option.get("project")
                    and self.owner.workspacePath == option.get("cwd"))

    @Property(dict, notify=changed)
    def filters(self) -> dict:
        return {"search": self._search, "state": self._filter}

    def _option(self) -> dict:
        return next((p for p in self._projects if p["id"] == self._project), {})

    def _path(self, identity: str = "") -> str:
        option = self._option()
        suffix = "/" + identity if identity else ""
        return f"/api/projects/{option['project']}/skills{suffix}?" + urlencode({"cwd": option["cwd"]})

    def _machine(self):
        return self.owner._machines.get(self._option().get("machine", ""))

    def _connection(self):
        machine = self._machine()
        return machine.connection if machine and "skills" in machine.runtime.info.get("capabilities", []) else None

    def _navigation_changed(self) -> None:
        options = self.owner._workspace_options()
        if options != self._projects:
            self._projects = options
            self.optionsChanged.emit()
        if not any(p["id"] == self._project for p in options):
            current = next((p["id"] for p in options if p["project"] == self.owner.projectId and p["cwd"] == self.owner.workspacePath), "")
            self.chooseProject(current or (options[0]["id"] if options else ""))
        self.changed.emit()

    @Slot(bool)
    def activate(self, active: bool) -> None:
        self._active = active
        if active:
            self._navigation_changed()
            context = (self.owner.property("projectId"), self.owner.property("workspacePath"))
            if context != self._chat_context:
                self._chat_context = context
                current = next((p["id"] for p in self._projects if (p["project"], p["cwd"]) == context), "")
                if current:
                    self.chooseProject(current)
            self.refresh()

    def revision(self, machine_id: str, instance: str, stamp: str) -> None:
        value = (instance, stamp)
        if self._revisions.get(machine_id) == value:
            return
        self._revisions[machine_id] = value
        if machine_id == self.owner._active_machine:
            self.owner.reloadSkills()
        if self._active and self._machine() and self._machine().id == machine_id:
            self.refresh()

    @Slot(str)
    def chooseProject(self, identity: str) -> None:
        if self._editing or identity == self._project:
            return
        self._project = identity
        self._epoch += 1
        self._busy = False
        self._all = []
        self._rows.replace([])
        self.select("")
        self.changed.emit()
        if self._active:
            self.refresh()

    @Slot(str, str)
    def filter(self, key: str, value: str) -> None:
        if key == "search":
            self._search = value
        elif key == "state":
            self._filter = value
        self._rebuild()

    def _rebuild(self) -> None:
        query = self._search.casefold().strip()
        self._rows.replace([r for r in self._all
                            if (r["state"] == self._filter if self._filter else r["state"] != "removed")
                            and (not query or query in (r["name"] + " " + r["description"] + " " + r["source"]).casefold())])

    @Slot()
    def refresh(self) -> None:
        connection = self._connection()
        if not connection or self._busy or self._editing:
            return
        epoch = self._epoch
        self._busy = True
        self._error = ""
        self.changed.emit()

        def loaded(payload, error):
            if epoch != self._epoch:
                return
            self._busy = False
            self._error = error
            if not error:
                self._all = payload["skills"]
                self._rebuild()
                if self._selected:
                    self.select(self._selected if any(r["id"] == self._selected for r in self._all) else "")
            self.changed.emit()

        connection.call("GET", self._path(), None, loaded)

    @Slot(str)
    def select(self, identity: str) -> None:
        self._detail_epoch += 1
        epoch = self._detail_epoch
        if self._selected != identity:
            self._detail = {}
        self._selected = identity
        self.changed.emit()
        connection = self._connection()
        if not identity or not connection:
            return

        def loaded(payload, error):
            if epoch != self._detail_epoch:
                return
            self._error = error
            if not error:
                self._detail = payload
            self.changed.emit()

        connection.call("GET", self._path(identity), None, loaded)

    @Slot(str)
    def setState(self, state: str) -> None:
        connection = self._connection()
        if not connection or self.busy or not self._selected:
            return
        epoch = self._epoch
        self._busy = True
        self.changed.emit()

        def updated(payload, error):
            if epoch != self._epoch:
                return
            self._busy = False
            self._error = error
            if not error:
                self._detail = payload
                self.owner.reloadSkills()
                self.refresh()
            self.changed.emit()

        connection.call("POST", self._path(self._selected), {"state": state}, updated)

    @Slot()
    def beginCreate(self) -> None:
        self._editor_error = ""
        self.changed.emit()

    @Slot(dict)
    def create(self, draft: dict) -> None:
        connection = self._connection()
        if not connection or self.busy:
            return
        self._editing = True
        self._editor_error = ""
        self.changed.emit()

        def created(payload, error):
            self._editing = False
            self._editor_error = error
            if not error:
                self._search = self._filter = ""
                self.select(payload["id"])
                self.owner.reloadSkills()
                self.refresh()
                self.saved.emit()
            self.changed.emit()

        connection.call("POST", self._path(), draft, created)

    @Slot()
    def use(self) -> None:
        if self.canUse:
            self.owner._set_draft(f"${self._detail['name']} ")
            self.used.emit()
