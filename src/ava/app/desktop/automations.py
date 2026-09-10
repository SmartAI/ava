"""Native automation workspace, using the selected machine's authenticated backend."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from PySide6.QtCore import Property, QObject, QTimeZone, Signal, Slot

from .board import SummaryList

if TYPE_CHECKING:
    from .connection import Connection
    from .controller import Controller


def _integers(values: dict) -> dict:
    # QML JavaScript numbers cross the QVariant boundary as doubles.
    return {key: int(value) if isinstance(value, float) and value.is_integer() else value for key, value in values.items()}


class AutomationView(QObject):
    changed = Signal()
    optionsChanged = Signal()
    editorChanged = Signal()
    editRequested = Signal(dict)
    saved = Signal()

    def __init__(self, owner: Controller) -> None:
        super().__init__(owner)
        self.owner = owner
        self._rows = SummaryList(self)
        self._runs = SummaryList(self)
        self._project_options: list[dict] = []
        self._machine_options: list[dict] = []
        self._snapshots: dict[str, list[dict]] = {}
        self._revisions: dict[str, tuple[str, int, str]] = {}
        self._fetched: dict[str, tuple[str, int, str]] = {}
        self._pending: dict[str, tuple[Connection, object]] = {}
        self._active = False
        self._selected = ""
        self._detail: dict = {"runs": []}
        self._search = self._filter = ""
        self._error = ""
        self._busy = False
        self._detail_epoch = 0
        self._editor_busy = False
        self._editor_error = ""
        self._preview: dict = {}
        self._preview_epoch = 0
        self._manual_ids: dict[str, str] = {}
        self._create_id = ""
        owner.machinesChanged.connect(self._navigation_changed)
        owner.navigationChanged.connect(self._navigation_changed)

    @Property(QObject, constant=True)
    def rows(self) -> SummaryList:
        return self._rows

    @Property(QObject, constant=True)
    def runRows(self) -> SummaryList:
        return self._runs

    @Property(dict, notify=changed)
    def detail(self) -> dict:
        return self._detail

    @Property(str, notify=changed)
    def selected(self) -> str:
        return self._selected

    @Property(str, notify=changed)
    def error(self) -> str:
        return self._error or " · ".join(f"{self.owner._machines[key].name}: {error}"
                                       for key, (_, _, error) in self._fetched.items() if error and key in self.owner._machines)

    @Property(bool, notify=changed)
    def busy(self) -> bool:
        return self._busy

    @Property(bool, notify=changed)
    def loading(self) -> bool:
        return bool(self._pending)

    @Property(list, notify=optionsChanged)
    def projects(self) -> list:
        return self._project_options

    @Property(list, notify=optionsChanged)
    def machines(self) -> list:
        return self._machine_options

    @Property(dict, notify=changed)
    def filters(self) -> dict:
        return {"search": self._search, "state": self._filter}

    @Property(str, notify=changed)
    def notice(self) -> str:
        unavailable = [machine.name for key, machine in self.owner._machines.items() if not self._connection(key)]
        return ("Connect or update Ava to manage automations on " + ", ".join(unavailable) + ".") if unavailable else ""

    @Property(bool, notify=changed)
    def canCreate(self) -> bool:
        return any(self._connection(project["machine"]) for project in self.owner._projects)

    @Property(dict, notify=editorChanged)
    def editorState(self) -> dict:
        return {"busy": self._editor_busy, "error": self._editor_error, "preview": self._preview}

    def _connection(self, machine_id: str) -> Connection | None:
        machine = self.owner._machines.get(machine_id)
        if machine and "automations" in machine.runtime.info.get("capabilities", []):
            return machine.connection
        return None

    def _machine_id(self, identity: str) -> str:
        return next((key for key, rows in self._snapshots.items() if any(row["id"] == identity for row in rows)), "")

    @Slot(bool)
    def activate(self, active: bool) -> None:
        self._active = active
        if active:
            self._navigation_changed()
            for machine in self.owner._machines:
                self._refresh(machine)

    def revision(self, machine_id: str, instance: str, revision: int, error: str = "") -> None:
        self._revisions[machine_id] = (instance, revision, error)
        if self._active:
            self._refresh(machine_id)

    def _navigation_changed(self) -> None:
        projects = [{key: project[key] for key in ("id", "name", "machine")} for project in self.owner._projects]
        machines = [{"id": machine.id, "name": machine.name} for machine in self.owner._machines.values()]
        if (projects, machines) != (self._project_options, self._machine_options):
            self._project_options, self._machine_options = projects, machines
            self.optionsChanged.emit()
        self._rebuild()
        if self._active:
            for key in self.owner._machines:
                self._refresh(key)

    @Slot()
    def refresh(self) -> None:
        self._error = ""
        self._fetched.clear()
        for key in self.owner._machines:
            self._refresh(key)

    def _refresh(self, machine_id: str) -> None:
        connection = self._connection(machine_id)
        if not connection or (machine_id in self._pending and self._pending[machine_id][0] is connection):
            return
        current = self.owner._machines[machine_id].runtime.info["instance_id"]
        expected = self._revisions.get(machine_id, (current, -1, ""))
        if self._fetched.get(machine_id) == expected and expected[0] == current:
            return
        pending = (connection, object())
        self._pending[machine_id] = pending
        self.changed.emit()

        def loaded(payload: Any, error: str) -> None:
            if self._pending.get(machine_id) is not pending:
                return
            self._pending.pop(machine_id, None)
            if self._connection(machine_id) is not connection:
                return
            if error:
                self._error = error
            else:
                self._snapshots[machine_id] = payload["automations"]
                self._fetched[machine_id] = (current, payload["revision"], payload.get("error", ""))
                self._revisions[machine_id] = self._fetched[machine_id]
                self._error = ""
                self._rebuild()
                if self._selected and self._machine_id(self._selected) == machine_id:
                    self._load_detail()
            self.changed.emit()

        connection.call("GET", "/api/automations", None, loaded)

    def _rebuild(self) -> None:
        projects = {p["id"]: p for p in self.owner._projects}
        rows = []
        for machine_id, tasks in self._snapshots.items():
            machine = self.owner._machines.get(machine_id)
            if not machine:
                continue
            for task in tasks:
                project = projects.get(task["project_id"])
                if not project:
                    continue
                state = "completed" if not task["remaining"] else "active" if task["enabled"] else "paused"
                text = f"{task['name']} {project['name']} {machine.name}".casefold()
                if self._filter and state != self._filter or not all(word in text for word in self._search.casefold().split()):
                    continue
                rows.append({**task, "machine_id": machine_id, "machine": machine.name, "project": project["name"],
                             "online": bool(self._connection(machine_id)), "state": state})
        rows.sort(key=lambda row: (row["created_at"], row["id"]), reverse=True)
        self._rows.replace(rows)
        if self._selected and not any(row["id"] == self._selected for row in rows):
            self._selected = ""
            self._detail = {"runs": []}
            self._runs.replace([])
            self._detail_epoch += 1
        if self._selected:
            match = next(row for row in rows if row["id"] == self._selected)
            self._detail.update({key: match.get(key, "") for key in ("online", "machine", "machine_id", "project", "state", "active_run")})
        self.changed.emit()

    @Slot(str, str)
    def filter(self, kind: str, value: str) -> None:
        if kind == "search":
            self._search = value
        elif kind == "state":
            self._filter = value
        self._rebuild()

    @Slot(str)
    def select(self, identity: str) -> None:
        self._selected = identity
        row = next((row for row in self._rows.rows if row["id"] == identity), None)
        self._detail = {**(row or {}), "runs": []}
        self._runs.replace([])
        self._load_detail()
        self.changed.emit()

    def _load_detail(self, before: int = 0) -> None:
        identity = self._selected
        connection = self._connection(self._machine_id(identity))
        if not connection:
            return
        self._detail_epoch += 1
        epoch = self._detail_epoch

        def loaded(payload: Any, error: str) -> None:
            if epoch != self._detail_epoch or identity != self._selected:
                return
            if error:
                self._error = error
            else:
                previous = self._detail.get("runs", [])
                runs = {run["id"]: run for run in previous}
                runs.update((run["id"], run) for run in payload["runs"])
                if not before and len(previous) > 20:
                    payload["has_more"] = self._detail.get("has_more", False)
                self._detail.update({**payload, "runs": sorted(runs.values(), key=lambda run: run["cursor"], reverse=True)})
                self._runs.replace(self._detail["runs"])
                if connection.prefix + self._manual_ids.get(identity, "") in runs:
                    self._manual_ids.pop(identity, None)
            self.changed.emit()

        connection.call("GET", f"/api/automations/{identity}" + (f"?before={before}" if before else ""), None, loaded)

    @Slot()
    def more(self) -> None:
        runs = self._detail.get("runs", [])
        if runs:
            self._load_detail(runs[-1]["cursor"])

    @Slot(str)
    def edit(self, identity: str = "") -> None:
        self._editor_busy, self._editor_error, self._preview = False, "", {}
        self._preview_epoch += 1
        self.editorChanged.emit()
        if not identity:
            self._create_id = uuid4().hex
            zone = bytes(QTimeZone.systemTimeZoneId().data()).decode() or "UTC"
            local = (datetime.now(ZoneInfo(zone)) + timedelta(minutes=5)).replace(second=0, microsecond=0, tzinfo=None)
            project = next((p for p in self.owner._projects if p["id"] == self.owner._project_id), next(iter(self.owner._projects), {}))
            self.editRequested.emit({"id": "", "name": "", "prompt": "", "project_id": project.get("id", ""),
                                     "machine_id": project.get("machine", "local"), "workspace": "current", "base_ref": "HEAD",
                                     "schedule": {"start_local": local.isoformat(), "timezone": zone, "cadence": "days", "every": 1, "count": 3}})
            return
        machine = self._machine_id(identity)
        connection = self._connection(machine)
        if connection:
            def loaded(payload: Any, error: str) -> None:
                if error:
                    self._error = error
                    self.changed.emit()
                else:
                    self.editRequested.emit({**payload, "machine_id": machine})
            connection.call("GET", f"/api/automations/{identity}", None, loaded)

    @Slot(str, dict)
    def preview(self, machine_id: str, schedule: dict) -> None:
        self._preview_epoch += 1
        epoch = self._preview_epoch
        connection = self._connection(machine_id)
        if not connection:
            self._preview = {"error": "Connect to the execution machine to preview this schedule."}
            self.editorChanged.emit()
            return

        def loaded(payload: Any, error: str) -> None:
            if epoch == self._preview_epoch:
                self._preview = {"error": error} if error else payload
                self.editorChanged.emit()
        connection.call("POST", "/api/automations/preview", _integers(schedule), loaded)

    @Slot(str, str, dict)
    def save(self, machine_id: str, identity: str, definition: dict) -> None:
        definition = {**_integers(definition), "schedule": _integers(definition["schedule"])}
        if not identity:
            definition["request_id"] = self._create_id or uuid4().hex
        connection = self._connection(machine_id)
        if not connection or self._editor_busy:
            self._editor_error = "Connect to the execution machine before saving."
            self.editorChanged.emit()
            return
        self._editor_busy, self._editor_error = True, ""
        self.editorChanged.emit()

        def finished(payload: Any, error: str) -> None:
            self._editor_busy, self._editor_error = False, error
            self.editorChanged.emit()
            if not error:
                if not identity:
                    self._search = self._filter = ""
                self._selected = payload["id"]
                rows = self._snapshots.setdefault(machine_id, [])
                rows[:] = [row for row in rows if row["id"] != payload["id"]]
                rows.append(payload)
                self._detail = {"runs": []}
                self._runs.replace([])
                self._rebuild()
                self._load_detail()
                self.saved.emit()
                self._pending.pop(machine_id, None)
                self.refresh()

        connection.call("PUT" if identity else "POST", "/api/automations" + ("/" + identity if identity else ""), definition, finished)

    @Slot(str)
    def action(self, action: str) -> None:
        identity = self._selected
        machine_id = self._machine_id(identity)
        connection = self._connection(machine_id)
        if not connection or self._busy:
            return
        path, method = f"/api/automations/{identity}", "POST"
        body: dict | None = None
        if action == "run":
            path += "/run"
            body = {"request_id": self._manual_ids.setdefault(identity, uuid4().hex)}
        elif action == "pause":
            path += "/enabled"
            body = {"enabled": not self._detail.get("enabled", False)}
        elif action == "remove":
            method = "DELETE"
        elif action.startswith("stop:"):
            path += "/runs/" + action.removeprefix("stop:") + "/stop"
        else:
            return
        self._busy, self._error = True, ""
        self.changed.emit()

        def finished(payload: Any, error: str) -> None:
            self._busy, self._error = False, error
            if not error:
                if action == "run":
                    self._manual_ids.pop(identity, None)
                if action == "remove":
                    self._selected, self._detail = "", {"runs": []}
                    self._runs.replace([])
                self._pending.pop(machine_id, None)
                self.refresh()
            self.changed.emit()

        connection.call(method, path, body, finished)
