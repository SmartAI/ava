"""Independent read-only replay navigation; never switches or drives the active chat."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

from PySide6.QtCore import Property, QObject, QTimer, Signal, Slot

if TYPE_CHECKING:
    from .connection import Connection
    from .controller import Controller


class SessionReplay(QObject):
    changed = Signal()
    opened = Signal()

    def __init__(self, owner: Controller) -> None:
        super().__init__(owner)
        self.owner = owner
        self._identity = ""
        self._connection: Connection | None = None
        self._generation = 0
        self._index_request = self._detail_request = 0
        self._data: dict[str, Any] = {}
        self._detail: dict[str, Any] = {}
        self._loading = self._detail_loading = False
        self._error = ""
        self._query = ""
        self._category = "all"
        self._view = "detail"
        self._selected = -1
        self._latest = -1
        self._mode = "trace"
        self._catalog: dict[str, Any] = {}
        self._catalog_category = "failures"
        self._catalog_query = ""
        self._scope = ""
        self._pending_select = -1
        self._timer = QTimer(self)
        self._timer.setInterval(3000)
        self._timer.timeout.connect(self._check_latest)
        owner.machinesChanged.connect(self._connection_changed)

    @Property(dict, notify=changed)
    def state(self) -> dict:
        return {
            "identity": self._identity,
            "data": self._data,
            "detail": self._detail,
            "loading": self._loading,
            "detail_loading": self._detail_loading,
            "error": self._error,
            "selected": self._selected,
            "view": self._view,
            "query": self._query,
            "category": self._category,
            "has_new": self._latest > self._data.get("through", self._latest),
            "mode": self._mode, "catalog": self._catalog,
            "catalog_category": self._catalog_category, "catalog_query": self._catalog_query,
            "scope": self._scope,
        }

    def open(self, identity: str, *, seq: int = -1, through: int | None = None) -> None:
        self.close()
        self._mode = "trace"
        self._catalog = {}
        self._pending_select = seq
        self._identity = identity
        self._connection = self.owner._machine_for(identity).connection
        self._data, self._detail = {}, {}
        self._query, self._category, self._view = "", "stops" if seq >= 0 else "all", "detail"
        self._selected = self._latest = -1
        self.changed.emit()
        self.opened.emit()
        self._load_index(0, through=through)
        self._timer.start()

    @Slot()
    def close(self) -> None:
        self._generation += 1
        self._loading = self._detail_loading = False
        self._timer.stop()

    def _connection_changed(self) -> None:
        if (
            self._identity
            and self._connection is not self.owner._machine_for(self._identity).connection
        ):
            self.close()
            self._connection = self.owner._machine_for(self._identity).connection
            self._error = "Connection changed. Refresh to load a new snapshot."
            self.changed.emit()

    def _call(self, params: dict, done, *, path: str | None = None) -> None:
        connection = self._connection
        if connection is None:
            done(None, "This session’s machine is offline. Reconnect and refresh.")
            return
        generation = self._generation

        def completed(payload, error):
            if generation == self._generation and connection is self._connection:
                # FastAPI's missing-route response is distinct from Ava's
                # {"error": "no such chat"}. Do not misdiagnose a deleted session.
                if error and payload == {"detail": "Not Found"}:
                    error = (
                        "This backend does not have the Session Replay API. "
                        "Update the backend and restart it after running tasks finish, "
                        "then choose Refresh. Reopening the desktop alone does not restart the backend."
                    )
                done(payload, error)

        connection.call(
            "GET", (path or f"/api/chats/{self._identity}/replay") + "?" + urlencode(params), None, completed
        )

    @Slot()
    def refresh(self) -> None:
        self._pending_select = -1
        self._generation += 1
        if self._mode == "catalog":
            self._connection = self.owner._machine_for(self._identity).connection
            self._load_catalog(0)
            return
        self._connection = self.owner._machine_for(self._identity).connection
        self._selected = -1
        self._detail = {}
        self._detail_loading = False
        self._data = {}
        self._load_index(0)
        self._timer.start()

    def _load_index(self, offset: int, *, through: int | None = None) -> None:
        self._index_request += 1
        request = self._index_request
        params: dict = {"query": self._query, "category": self._category, "offset": offset}
        if through is not None:
            params["through"] = through
        elif "through" in self._data:
            params["through"] = self._data["through"]
        self._loading, self._error = True, ""
        self.changed.emit()

        def loaded(payload, error):
            if request != self._index_request:
                return
            self._loading, self._error = False, error
            if not error:
                self._data = payload
            self.changed.emit()
            if not error and self._pending_select >= 0:
                selected, self._pending_select = self._pending_select, -1
                self.select(selected)

        self._call(params, loaded)

    @Slot()
    def findSessions(self, identity: str = "") -> None:
        self.close()
        self._identity = identity or self._identity
        machine = self.owner._machine_for(self._identity)
        self._connection = machine.connection
        self._scope = getattr(machine, "name", "This backend")
        self._mode, self._catalog = "catalog", {}
        self._catalog_category, self._catalog_query = "failures", ""
        self._error = ""
        self.changed.emit()
        self.opened.emit()
        self._load_catalog(0)

    @Slot(str, str, int)
    def searchSessions(self, query: str, category: str, offset: int) -> None:
        self._catalog_query, self._catalog_category = query, category
        self._load_catalog(max(0, offset))

    def _load_catalog(self, offset: int) -> None:
        self._index_request += 1
        request = self._index_request
        self._loading, self._error = True, ""
        self.changed.emit()
        connection = self._connection

        def loaded(payload, error):
            if request != self._index_request:
                return
            self._loading, self._error = False, error
            if not error:
                prefix = connection.prefix if connection else ""
                self._catalog = {**payload, "rows": [
                    {**row, "chat_id": prefix + row["chat_id"]} for row in payload["rows"]
                ]}
            self.changed.emit()

        self._call({"category": self._catalog_category, "query": self._catalog_query, "offset": offset},
                   loaded, path="/api/replay/stops")

    @Slot(str, int, int)
    def openStop(self, identity: str, seq: int, through: int) -> None:
        self.open(identity, seq=seq, through=through)

    @Slot(str, str)
    def filter(self, query: str, category: str) -> None:
        self._query, self._category = query, category
        self._load_index(0)

    @Slot(int)
    def page(self, offset: int) -> None:
        if offset >= 0:
            self._load_index(offset)

    @Slot(int)
    def select(self, seq: int) -> None:
        self._selected = seq
        self._detail_request += 1
        self._detail = {}
        self._detail_loading = False
        if seq < 0:
            self.changed.emit()
        else:
            self._load_detail(0)

    @Slot(int)
    def jumpIssue(self, direction: int) -> None:
        sequences = self._data.get("issue_sequences", [])
        if not sequences:
            return
        if direction > 0:
            target = next((seq for seq in sequences if seq > self._selected), sequences[0])
        else:
            target = next(
                (seq for seq in reversed(sequences) if seq < self._selected), sequences[-1]
            )
        self.select(target)

    @Slot(str)
    def view(self, name: str) -> None:
        self._view = name
        self._detail = {}
        if self._selected >= 0:
            self._load_detail(0)
        else:
            self.changed.emit()

    @Slot(int)
    def detailPage(self, offset: int) -> None:
        if self._selected >= 0 and offset >= 0:
            self._load_detail(offset)

    def _load_detail(self, offset: int) -> None:
        if "through" not in self._data:
            return
        self._detail_request += 1
        request = self._detail_request
        self._detail_loading, self._error = True, ""
        self.changed.emit()

        def loaded(payload, error):
            if request != self._detail_request:
                return
            self._detail_loading, self._error = False, error
            if not error:
                self._detail = payload
            self.changed.emit()

        self._call(
            {
                "through": self._data["through"],
                "seq": self._selected,
                "view": self._view,
                "offset": offset,
            },
            loaded,
        )

    def _check_latest(self) -> None:
        if self._loading or "through" not in self._data:
            return

        def loaded(payload, error):
            if not error:
                self._latest = payload["latest_sequence"]
                self.changed.emit()

        self._call({"head": "true"}, loaded)
