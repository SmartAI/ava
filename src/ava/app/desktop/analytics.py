"""Native analytics summaries. Remote machines send aggregates, never chat histories."""
from __future__ import annotations

from collections import OrderedDict
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from PySide6.QtCore import Property, QObject, QTimer, QTimeZone, Signal, Slot

from ava.app.analytics import TOKEN_FIELDS, merge_intervals, totals

if TYPE_CHECKING:
    from .connection import Connection
    from .controller import Controller


class AnalyticsView(QObject):
    changed = Signal()
    filtersChanged = Signal()
    optionsChanged = Signal()

    def __init__(self, owner: Controller) -> None:
        super().__init__(owner)
        self.owner = owner
        self._machine = self._project = ""
        self._days = 7
        self._zone = bytes(QTimeZone.systemTimeZoneId().data()).decode() or "UTC"
        self._active = False
        self._cache: OrderedDict[tuple, dict] = OrderedDict()
        self._pending: dict[str, Connection] = {}
        self._errors: dict[str, str] = {}
        self._data: dict = {"days": [], "totals": totals([]), "tools": [], "skills": [], "reports": 0, "notice": ""}
        self._projects: list[dict] = []
        self._machines: list[dict] = []
        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self.refresh)
        owner.navigationChanged.connect(self._navigation)
        owner.machinesChanged.connect(self._navigation)
        self._navigation()

    @Property(dict, notify=changed)
    def data(self) -> dict:
        return self._data

    @Property(dict, notify=filtersChanged)
    def filters(self) -> dict:
        return {"machine": self._machine, "project": self._project, "days": self._days, "timezone": self._zone}

    @Property(list, notify=optionsChanged)
    def machines(self) -> list:
        return [{"id": "", "name": "All machines"}, *self._machines]

    @Property(list, notify=optionsChanged)
    def projects(self) -> list:
        return [{"id": "", "name": "All projects"}, *[p for p in self._projects if not self._machine or p["machine"] == self._machine]]

    @Property(bool, notify=changed)
    def loading(self) -> bool:
        return bool(self._pending) and not self._data["reports"]

    def _navigation(self) -> None:
        # Closing a connection discards its HTTP callbacks. Do not let a
        # cancelled request prevent this machine from refreshing after reconnect.
        self._pending = {identity: connection for identity, connection in self._pending.items()
                         if (machine := self.owner._machines.get(identity)) and machine.connection is connection}
        projects = [{"id": p["id"], "name": p["name"], "machine": p["machine"]} for p in self.owner._projects]
        machines = [{"id": m.id, "name": m.name} for m in self.owner._machines.values()]
        if projects != self._projects or machines != self._machines:
            if {p["id"] for p in projects} != {p["id"] for p in self._projects}:
                self._cache.clear()  # Project removal must not leave hidden contributions on screen.
            self._projects, self._machines = projects, machines
            if self._project and not any(p["id"] == self._project for p in projects):
                self._project = ""
            if self._machine and not any(m["id"] == self._machine for m in machines):
                self._machine = self._project = ""
            self.optionsChanged.emit()
            self.filtersChanged.emit()
        self._assemble()
        if self._active:
            self.refresh()

    @Slot(bool)
    def activate(self, active: bool) -> None:
        self._active = active
        if active:
            self._timer.start()
            self.refresh()
        else:
            self._timer.stop()

    @Slot(str, str)
    def filter(self, name: str, value: str) -> None:
        if name == "days" and value in ("7", "30"):
            self._days = int(value)
        elif name == "machine":
            self._machine, self._project = value, ""
            self.optionsChanged.emit()
        elif name == "project":
            self._project = value
        elif name == "timezone":
            self._zone = value
        else:
            return
        self.filtersChanged.emit()
        self._assemble()
        if name != "days":
            self.refresh()

    def _wanted(self):
        project_machine = next((p["machine"] for p in self._projects if p["id"] == self._project), "")
        return [m for m in self.owner._machines.values() if (not self._machine or m.id == self._machine)
                and (not project_machine or m.id == project_machine)]

    def _key(self, machine) -> tuple:
        return (machine.machine_id or machine.id, self._project, self._zone, datetime.now(ZoneInfo(self._zone)).date().isoformat())

    @Slot()
    def refresh(self) -> None:
        if not self._active:
            return
        for machine in self._wanted():
            connection = machine.connection
            if not connection or machine.id in self._pending or "analytics" not in machine.runtime.info.get("capabilities", []):
                continue
            key = self._key(machine)
            self._pending[machine.id] = connection
            query = urlencode({"days": 30, "timezone": self._zone, "project": self._project.removeprefix(connection.prefix), "revision": self._cache.get(key, {}).get("revision", "")})
            def loaded(payload, error, machine=machine, connection=connection, key=key):
                if self._pending.get(machine.id) is connection:
                    self._pending.pop(machine.id, None)
                if machine.connection is not connection:
                    self._assemble()
                    return
                if error:
                    self._errors[machine.id] = error
                elif payload.get("machine_id") != machine.machine_id:
                    self._errors[machine.id] = "Statistics came from a different Ava data store. Reconnect this machine."
                else:
                    self._errors.pop(machine.id, None)
                    if payload.get("unchanged"):
                        if key in self._cache:
                            self._cache[key]["as_of"] = payload["as_of"]
                    else:
                        self._cache[key] = payload
                    if key not in self._cache:
                        self._assemble()
                        return
                    self._cache.move_to_end(key)
                    while len(self._cache) > 64:
                        self._cache.popitem(last=False)
                self._assemble()
            connection.call("GET", "/api/analytics?" + query, None, loaded)
        self._assemble()

    def _assemble(self) -> None:
        days: dict[str, dict] = {}
        notices: list[str] = []
        reports = 0
        seen: set[str] = set()
        last = datetime.now(ZoneInfo(self._zone)).date()
        first = (last-timedelta(days=self._days-1)).isoformat()
        for machine in self._wanted():
            if machine.machine_id in seen:
                continue
            if machine.machine_id:
                seen.add(machine.machine_id)
            report = self._cache.get(self._key(machine))
            if not machine.connection:
                if report is None:
                    report = next((value for key, value in reversed(self._cache.items()) if key[:3] == self._key(machine)[:3]), None)
                saved = datetime.fromisoformat(report["as_of"]).astimezone(ZoneInfo(self._zone)).strftime("%b %d, %H:%M") if report else ""
                notices.append(machine.name + " is offline" + ("; showing statistics saved at " + saved if report else "; no cached statistics"))
            elif "analytics" not in machine.runtime.info.get("capabilities", []):
                notices.append("Update the Ava backend on " + machine.name + " to view analytics")
            elif error := self._errors.get(machine.id):
                notices.append(machine.name + ": " + error)
            if not report:
                continue
            reports += 1
            if report["indexing"]:
                notices.append(machine.name + ": indexing history; totals are still updating")
            if report["unavailable"] or report["incomplete"] or report.get("error"):
                notices.append(machine.name + ": some history is unavailable or incomplete")
            for source in report["days"][-self._days:]:
                if not first <= source["date"] <= last.isoformat():
                    continue
                target = days.setdefault(source["date"], {"date": source["date"], "intervals": [], "tool_counts": [], "skill_counts": []})
                for name in (*TOKEN_FIELDS, "tokens", "responses", "missing_usage", "tools", "tool_errors", "skills", "runs", "run_ms"):
                    target[name] = target.get(name, 0) + source[name]
                for name in ("intervals", "tool_counts", "skill_counts"):
                    target[name].extend(source[name])
        series = sorted(days.values(), key=lambda day: day["date"])
        for day in series:
            day["active_ms"] = sum(b-a for a, b in merge_intervals(day.pop("intervals")))
        result = {"days": series, "totals": totals(series), "reports": reports, "notice": ". ".join(notices), "loading": bool(self._pending) and not reports}
        for kind in ("tool", "skill"):
            entries: dict[str, dict] = {}
            for day in series:
                for row in day.pop(kind + "_counts"):
                    entry = entries.setdefault(row["name"], {"name": row["name"], "count": 0, "errors": 0})
                    entry["count"] += row["count"]
                    entry["errors"] += row.get("errors", 0)
            result[kind + "s"] = sorted(entries.values(), key=lambda row: (-row["count"], row["name"]))
        if self._data != result:
            self._data = result
            self.changed.emit()
