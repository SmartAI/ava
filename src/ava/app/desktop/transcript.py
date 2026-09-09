"""Incremental Qt model projected from the existing durable event vocabulary."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import (
    QAbstractListModel,
    QByteArray,
    QModelIndex,
    QPersistentModelIndex,
    Qt,
    Signal,
)


def block_text(blocks: list[dict[str, Any]]) -> str:
    return "\n".join(
        block.get("text", "")
        if block["kind"] == "text"
        else f"[{block.get('display_path') or 'Attachment'}]"
        for block in blocks
        if block["kind"] in ("text", "image", "file_text")
    )


class Transcript(QAbstractListModel):
    changed = Signal()
    roles = {
        Qt.ItemDataRole.UserRole + i: QByteArray(name.encode())
        for i, name in enumerate(("kind", "heading", "body", "detail"), start=1)
    }

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self.rows: list[dict[str, str]] = []
        self.last_seq = -1
        self.pending: dict[str, list[dict[str, Any]]] = {"next_step": [], "next_turn": []}
        self._inputs: set[str] = set()
        self._attempts: dict[str, int] = {}
        self._tools: dict[str, int] = {}

    def roleNames(self) -> dict[int, QByteArray]:
        return self.roles

    def rowCount(self, parent: QModelIndex | QPersistentModelIndex | None = None) -> int:
        return 0 if parent is not None and parent.isValid() else len(self.rows)

    def data(
        self, index: QModelIndex | QPersistentModelIndex, role: int = Qt.ItemDataRole.DisplayRole
    ) -> Any:
        if not index.isValid() or not 0 <= index.row() < len(self.rows):
            return None
        name = self.roles.get(role)
        return self.rows[index.row()].get(bytes(name.data()).decode()) if name else None

    def clear(self) -> None:
        self.beginResetModel()
        self.rows.clear()
        self.last_seq = -1
        self.pending = {"next_step": [], "next_turn": []}
        self._inputs.clear()
        self._attempts.clear()
        self._tools.clear()
        self.endResetModel()
        self.changed.emit()

    def append(self, kind: str, heading: str, body: str, detail: str = "") -> int:
        row = len(self.rows)
        self.beginInsertRows(QModelIndex(), row, row)
        self.rows.append(dict(kind=kind, heading=heading, body=body, detail=detail))
        self.endInsertRows()
        return row

    def update(self, row: int, **values: str) -> None:
        self.rows[row].update(values)
        self.dataChanged.emit(self.index(row), self.index(row), list(self.roles))

    def apply(self, event: dict[str, Any]) -> None:
        seq = int(event["seq"])
        if seq <= self.last_seq:
            return
        kind = event["kind"]
        blocks = event.get("blocks", [])
        if kind == "inbox/spliced":
            queue = self.pending[event["target"]]
            start = event["index"]
            queue[start : start + event["removed"]] = event["inserted"]
        elif kind == "step/claimed":
            for message in event["messages"]:
                identity = message.get("id", "")
                if not identity or identity not in self._inputs:
                    self.append("user", "You", block_text(message["blocks"]))
                if identity:
                    self._inputs.add(identity)
            for target, queue in self.pending.items():
                self.pending[target] = [m for m in queue if m["id"] not in self._inputs]
        elif kind == "user/message":
            self.append("user", "You", block_text(blocks))
        elif kind in ("assistant/chunk", "assistant/message"):
            attempt = event["attempt_id"]
            text = event["delta"] if kind == "assistant/chunk" else block_text(blocks)
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
                            "tool", block["tool_name"], "Running…", block["arguments_json"]
                        )
        elif kind == "tool/result":
            for block in blocks:
                row = self._tools.get(block.get("call_id"))
                if row is not None:
                    self.update(
                        row,
                        body=block.get("text") or "No output",
                        kind="error" if block.get("is_error") else "tool",
                    )
        elif kind in ("drive/error", "compaction/failed"):
            self.append("error", "Run failed", event["message"])
        elif kind == "turn/end":
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
