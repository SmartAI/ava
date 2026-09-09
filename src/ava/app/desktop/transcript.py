"""Incremental Qt model projected from the existing durable event vocabulary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import (
    Property,
    QAbstractListModel,
    QByteArray,
    QModelIndex,
    QPersistentModelIndex,
    Qt,
    Signal,
    Slot,
)


def block_text(blocks: list[dict[str, Any]]) -> str:
    return "\n".join(
        block.get("text", "")
        if block["kind"] == "text"
        else f"[{block.get('display_path') or 'Attachment'}]"
        for block in blocks
        if block["kind"] in ("text", "image", "file_text")
    )


_TOOL_ACTIVITY = {
    "bash": "Running commands",
    "edit": "Editing files",
    "read": "Reading files",
    "write": "Editing files",
}


def tool_activity(names: list[str]) -> str:
    labels = list(dict.fromkeys(_TOOL_ACTIVITY.get(name, "Using tools") for name in names))
    return ", ".join(label if index == 0 else label[0].lower() + label[1:]
                     for index, label in enumerate(labels))


@dataclass
class ActivityGroup:
    start: int
    end: int
    expanded: bool = False
    running: int = 0
    failed: int = 0


class Transcript(QAbstractListModel):
    changed = Signal()
    roles = {
        Qt.ItemDataRole.UserRole + i: QByteArray(name.encode())
        for i, name in enumerate(
            (
                "kind",
                "heading",
                "body",
                "detail",
                "attachments",
                "sourceRow",
                "groupCount",
                "groupExpanded",
                "groupRunning",
                "groupFailed",
                "outputExpanded",
            ),
            start=1,
        )
    }

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self.rows: list[dict[str, Any]] = []
        self._visible: list[int] = []
        self._positions: dict[int, int] = {}
        self._groups: list[ActivityGroup | None] = []
        self._expanded_outputs: set[int] = set()
        self.last_seq = -1
        self.pending: dict[str, list[dict[str, Any]]] = {"next_step": [], "next_turn": []}
        self._inputs: set[str] = set()
        self._attempts: dict[str, int] = {}
        self._tools: dict[str, int] = {}
        self._activity = ""

    @Property(str, notify=changed)
    def activity(self) -> str:
        return self._activity

    def _running_tool_activity(self) -> str:
        names = [
            row["heading"]
            for row in self.rows
            if row["kind"] == "tool" and row["body"] == "Running…"
        ]
        return tool_activity(names) if names else ""

    def roleNames(self) -> dict[int, QByteArray]:
        return self.roles

    def rowCount(self, parent: QModelIndex | QPersistentModelIndex | None = None) -> int:
        return 0 if parent is not None and parent.isValid() else len(self._visible)

    def data(
        self, index: QModelIndex | QPersistentModelIndex, role: int = Qt.ItemDataRole.DisplayRole
    ) -> Any:
        if not index.isValid() or not 0 <= index.row() < len(self._visible):
            return None
        role_name = self.roles.get(role)
        if role_name is None:
            return None
        name = bytes(role_name.data()).decode()
        source = self._visible[index.row()]
        if name == "sourceRow":
            return source
        if name == "outputExpanded":
            return source in self._expanded_outputs
        group = self._groups[source]
        if name == "groupCount":
            return group.end - group.start if group and group.start == source else 0
        if name == "groupExpanded":
            return bool(group and group.expanded)
        if name == "groupRunning":
            return group.running if group else 0
        if name == "groupFailed":
            return group.failed if group else 0
        return self.rows[source].get(name)

    @Slot(int)
    def toggleGroup(self, source: int) -> None:
        if not 0 <= source < len(self.rows):
            return
        group = self._groups[source]
        if not group or source != group.start or group.end - source < 2:
            return
        first = self._positions[source] + 1
        count = group.end - source - 1
        if group.expanded:
            self.beginRemoveRows(QModelIndex(), first, first + count - 1)
            del self._visible[first : first + count]
        else:
            self.beginInsertRows(QModelIndex(), first, first + count - 1)
            self._visible[first:first] = range(source + 1, group.end)
        self._positions = {row: index for index, row in enumerate(self._visible)}
        group.expanded = not group.expanded
        if group.expanded:
            self.endInsertRows()
        else:
            self.endRemoveRows()
        self._notify_row(source)

    @Slot(int)
    def toggleOutput(self, source: int) -> None:
        if not 0 <= source < len(self.rows):
            return
        if source in self._expanded_outputs:
            self._expanded_outputs.remove(source)
        else:
            self._expanded_outputs.add(source)
        self._notify_row(source)

    def _notify_row(self, source: int) -> None:
        position = self._positions.get(source)
        if position is not None:
            index = self.index(position)
            self.dataChanged.emit(index, index, list(self.roles))

    def clear(self) -> None:
        self.beginResetModel()
        self.rows.clear()
        self._visible.clear()
        self._positions.clear()
        self._groups.clear()
        self._expanded_outputs.clear()
        self.last_seq = -1
        self.pending = {"next_step": [], "next_turn": []}
        self._inputs.clear()
        self._attempts.clear()
        self._tools.clear()
        self._activity = ""
        self.endResetModel()
        self.changed.emit()

    def append(
        self, kind: str, heading: str, body: str, detail: str = "", attachments: list | None = None
    ) -> int:
        row = len(self.rows)
        compact = kind in ("tool", "reasoning") or kind == "error" and bool(detail)
        group = self._groups[-1] if compact and row else None
        if compact and group is None:
            group = ActivityGroup(row, row)
        visible = not group or group.start == row or group.expanded
        if visible:
            position = len(self._visible)
            self.beginInsertRows(QModelIndex(), position, position)
            self._positions[row] = position
            self._visible.append(row)
        self.rows.append(
            dict(
                kind=kind, heading=heading, body=body, detail=detail, attachments=attachments or []
            )
        )
        self._groups.append(group)
        if group:
            group.end = row + 1
            group.running += kind == "tool" and body == "Running…"
            group.failed += kind == "error"
        if visible:
            self.endInsertRows()
        if group and group.start != row:
            self._notify_row(group.start)
        return row

    def append_user(self, blocks: list[dict[str, Any]]) -> None:
        attachments = [block for block in blocks if block["kind"] in ("image", "file_text")]
        text = "\n".join(block.get("text", "") for block in blocks if block["kind"] == "text")
        self.append("user", "You", text, attachments=attachments)

    def update(self, row: int, **values: Any) -> None:
        group = self._groups[row]
        previous = self.rows[row]
        if group:
            group.running -= previous["kind"] == "tool" and previous["body"] == "Running…"
            group.failed -= previous["kind"] == "error"
        self.rows[row].update(values)
        if group:
            group.running += previous["kind"] == "tool" and previous["body"] == "Running…"
            group.failed += previous["kind"] == "error"
        self._notify_row(row)
        if group and group.start != row:
            self._notify_row(group.start)

    def apply(self, event: dict[str, Any]) -> None:
        seq = int(event["seq"])
        if seq <= self.last_seq:
            return
        kind = event["kind"]
        blocks = event.get("blocks", [])
        if kind in ("turn/start", "step/start"):
            self._activity = "Thinking"
        if kind == "inbox/spliced":
            queue = self.pending[event["target"]]
            start = event["index"]
            queue[start : start + event["removed"]] = event["inserted"]
        elif kind == "step/claimed":
            for message in event["messages"]:
                identity = message.get("id", "")
                if not identity or identity not in self._inputs:
                    self.append_user(message["blocks"])
                if identity:
                    self._inputs.add(identity)
            for target, queue in self.pending.items():
                self.pending[target] = [m for m in queue if m["id"] not in self._inputs]
        elif kind == "user/message":
            self.append_user(blocks)
        elif kind in ("assistant/chunk", "assistant/message"):
            attempt = event["attempt_id"]
            text = event["delta"] if kind == "assistant/chunk" else block_text(blocks)
            if text:
                self._activity = "Responding"
            if text:
                row = self._attempts.get(attempt)
                if row is None:
                    self._attempts[attempt] = self.append("assistant", "Ava", text)
                else:
                    self.update(
                        row,
                        body=self.rows[row]["body"] + text if kind == "assistant/chunk" else text,
                    )
            if kind == "assistant/message":
                for block in blocks:
                    if block["kind"] == "reasoning" and block.get("summary"):
                        self.append("reasoning", "Reasoning", block["summary"])
                    elif block["kind"] == "tool_call":
                        self._tools[block["call_id"]] = self.append(
                            "tool", block.get("tool_title") or block["tool_name"], "Running…", block["arguments_json"]
                        )
                self._activity = self._running_tool_activity() or self._activity
        elif kind == "tool/result":
            for block in blocks:
                row = self._tools.get(block.get("call_id"))
                if row is not None:
                    self.update(
                        row,
                        body=block.get("text") or "No output",
                        kind="error" if block.get("is_error") else "tool",
                        attachments=block.get("attachments", []),
                    )
            self._activity = self._running_tool_activity() or "Thinking"
        elif kind in ("drive/error", "compaction/failed"):
            self.append("error", "Run failed", event["message"])
        elif kind == "turn/end":
            self._activity = ""
            reason = event["reason"]
            if reason == "user_abort":
                self.pending = {"next_step": [], "next_turn": []}
                self.append("notice", "Stopped", "The run was cancelled. Your history is saved.")
            elif reason in ("interrupted", "user_pause"):
                self.append(
                    "notice",
                    "Paused" if reason == "user_pause" else "Interrupted",
                    "You can continue this conversation.",
                )
        elif kind == "compaction/seed":
            self.append("notice", "Context compacted", "Earlier messages remain in your history.")
        self.last_seq = seq
        self.changed.emit()

    def pending_text(self) -> str:
        return "\n".join(block_text(m["blocks"]) for q in self.pending.values() for m in q)
