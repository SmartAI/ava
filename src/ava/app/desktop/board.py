"""Session-board summaries and incremental, viewport-friendly Qt lists."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import (
    Property,
    QAbstractListModel,
    QByteArray,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    Qt,
    Signal,
    Slot,
)


class SummaryList(QAbstractListModel):
    changed = Signal()
    entry_role = Qt.ItemDataRole.UserRole + 1

    def __init__(self, parent: QObject) -> None:
        super().__init__(parent)
        self.rows: list[dict] = []

    def roleNames(self) -> dict:
        return {self.entry_role: QByteArray(b"entry")}

    def rowCount(self, parent: QModelIndex | QPersistentModelIndex | None = None) -> int:
        return 0 if parent is not None and parent.isValid() else len(self.rows)

    def data(self, index: QModelIndex | QPersistentModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if index.isValid() and 0 <= index.row() < len(self.rows) and role == self.entry_role:
            return self.rows[index.row()]
        return None

    def replace(self, rows: list[dict]) -> None:
        if self.rows == rows:
            return
        wanted = {row["id"] for row in rows}
        for index in range(len(self.rows) - 1, -1, -1):
            if self.rows[index]["id"] not in wanted:
                self.beginRemoveRows(QModelIndex(), index, index)
                self.rows.pop(index)
                self.endRemoveRows()
        for index, row in enumerate(rows):
            if index >= len(self.rows) or self.rows[index]["id"] != row["id"]:
                previous = next((i for i in range(index, len(self.rows)) if self.rows[i]["id"] == row["id"]), -1)
                if previous < 0:
                    self.beginInsertRows(QModelIndex(), index, index)
                    self.rows.insert(index, row)
                    self.endInsertRows()
                else:
                    self.beginMoveRows(QModelIndex(), previous, previous, QModelIndex(), index)
                    self.rows.insert(index, self.rows.pop(previous))
                    self.endMoveRows()
            if self.rows[index] != row:
                self.rows[index] = row
                self.dataChanged.emit(self.index(index), self.index(index), [self.entry_role])
        self.changed.emit()


class SessionBoard(QObject):
    changed = Signal()
    filtersChanged = Signal()
    optionsChanged = Signal()
    reviewRequested = Signal(str, int)

    def __init__(self, parent: QObject) -> None:
        super().__init__(parent)
        self._active = SummaryList(self)
        self._pending = SummaryList(self)
        self._reviewed = SummaryList(self)
        self._machines: list[dict] = []
        self._projects: list[dict] = []
        self._machine = self._project = self._search = self._outcome = ""
        self._limits = {"review": 20, "reviewed": 20}
        self._totals = [0, 0, 0]
        self._saving: set[str] = set()
        self._error = ""
        self._unsupported: list[str] = []

    @Property(QObject, constant=True)
    def activeSessions(self) -> SummaryList:
        return self._active

    @Property(QObject, constant=True)
    def needsReview(self) -> SummaryList:
        return self._pending

    @Property(QObject, constant=True)
    def reviewedSessions(self) -> SummaryList:
        return self._reviewed

    @Property(list, notify=changed)
    def totals(self) -> list:
        return self._totals

    @Property(dict, notify=filtersChanged)
    def filters(self) -> dict:
        return {"machine": self._machine, "project": self._project, "search": self._search, "outcome": self._outcome}

    @Property(list, notify=optionsChanged)
    def machineOptions(self) -> list:
        return [{"id": "", "name": "All machines"}, *[{"id": m["id"], "name": m["name"]} for m in self._machines]]

    @Property(list, notify=optionsChanged)
    def projectOptions(self) -> list:
        return [{"id": "", "name": "All projects"}, *[{"id": p["id"], "name": p["name"] + " · " + p["machine_name"]}
                for p in self._projects if not self._machine or p["machine"] == self._machine]]

    @Property(str, notify=changed)
    def error(self) -> str:
        return self._error

    @Property(str, notify=changed)
    def notice(self) -> str:
        return ("Review tracking needs a backend update on " + ", ".join(self._unsupported) + ".") if self._unsupported else ""

    def update(self, machines: list[dict], projects: list[dict]) -> None:
        previous_filters = self.property("filters")
        previous_options = self.property("machineOptions"), self.property("projectOptions")
        self._machines, self._projects = machines, projects
        if self._machine and not any(m["id"] == self._machine for m in machines):
            self._machine = self._project = ""
        if self._project and not any(p["id"] == self._project for p in projects):
            self._project = ""
        if previous_options != (self.property("machineOptions"), self.property("projectOptions")):
            self.optionsChanged.emit()
        if previous_filters != self.property("filters"):
            self.filtersChanged.emit()
        self._refresh()

    @Slot(str, str)
    def filter(self, kind: str, value: str) -> None:
        if self.property("filters").get(kind) == value:
            return
        if kind == "machine":
            self._machine, self._project = value, ""
            self.optionsChanged.emit()
        elif kind == "project":
            self._project = value
        elif kind == "search":
            self._search = value
        elif kind == "outcome":
            self._outcome = value
        else:
            return
        self.filtersChanged.emit()
        self._limits = {"review": 20, "reviewed": 20}
        self._refresh()

    @Slot(str)
    def more(self, column: str) -> None:
        if column in self._limits:
            self._limits[column] += 20
            self._refresh()

    @Slot(str, int)
    def review(self, identity: str, through: int) -> None:
        if identity in self._saving:
            return
        self._saving.add(identity)
        self._error = ""
        self._refresh()
        self.reviewRequested.emit(identity, through)

    def reviewed(self, identity: str, error: str) -> None:
        self._saving.discard(identity)
        self._error = error
        self._refresh()

    def _refresh(self) -> None:
        columns: list[list[dict]] = [[], [], []]
        machines = {m["id"]: m for m in self._machines}
        unsupported: set[str] = set()
        active_states = {"running", "pausing", "aborting"}
        labels = {"running": "Running", "pausing": "Pausing", "paused": "Paused", "aborting": "Stopping",
                  "completed": "Completed", "user_abort": "Stopped", "shutdown": "Interrupted", "interrupted": "Interrupted",
                  "user_pause": "Paused", "blocked": "Blocked"}
        for project in self._projects:
            if self._machine and project["machine"] != self._machine or self._project and project["id"] != self._project:
                continue
            machine = machines.get(project["machine"])
            if machine is None:
                continue
            for chat in project["chats"]:
                if chat.get("archived"):
                    continue
                if "completion_seq" not in chat:
                    unsupported.add(machine["name"])
                complete = chat.get("completion_seq", -1)
                # Paused becomes observable just before its TurnEnd record is durable. Keep it
                # active only across that gap; once the result exists it belongs in review.
                active = chat["status"] in active_states or chat["status"] == "paused" and complete < 0
                if not active and complete < 0:
                    continue  # Blank chats have no execution/result to track yet.
                text = f"{chat['title']} {project['name']} {machine['name']}".casefold()
                if self._search and not all(word in text for word in self._search.casefold().split()):
                    continue
                outcome = chat["status"] if active else chat.get("completion_reason", "")
                failed = outcome in {"error", "provider_error", "tool_error", "blocked", "interrupted", "shutdown"}
                if self._outcome == "attention" and not (failed or outcome in {"paused", "user_pause"}):
                    continue
                column = 0 if active else 1 if complete > chat.get("reviewed_through", -1) else 2
                columns[column].append({**chat, "project": project["name"], "project_id": project["id"],
                    "machine": machine["name"], "online": machine["online"], "saving": chat["id"] in self._saving,
                    "label": labels.get(outcome, "Failed"), "attention": failed or outcome == "paused",
                    "time": chat.get("started_at" if active else "completed_at", "")})
        for rows in columns:
            rows.sort(key=lambda row: (row["time"], row["id"]), reverse=True)
        self._totals = [len(rows) for rows in columns]
        self._unsupported = sorted(unsupported)
        self._active.replace(columns[0])
        self._pending.replace(columns[1][:self._limits["review"]])
        self._reviewed.replace(columns[2][:self._limits["reviewed"]])
        self.changed.emit()
