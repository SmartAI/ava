"""Desktop-side browser handoff, trusted input and asynchronous viewport capture."""
from __future__ import annotations

import base64
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from PySide6.QtCore import (
    Property,
    QBuffer,
    QByteArray,
    QCoreApplication,
    QEvent,
    QIODevice,
    QObject,
    QPointF,
    QSize,
    Qt,
    QThreadPool,
    Signal,
    Slot,
)
from PySide6.QtGui import (
    QGuiApplication,
    QImage,
    QInputMethodEvent,
    QKeyEvent,
    QMouseEvent,
    QWheelEvent,
)
from PySide6.QtQuick import QQuickItem, QQuickItemGrabResult

from .connection import Connection


@dataclass
class _Tab:
    connection: Connection
    chat: str
    label: str
    identity: str = ""
    command: str = ""


class BrowserControl(QObject):
    changed = Signal()
    execute = Signal(int, dict)
    encoded = Signal(int, str, dict)

    def __init__(self, parent: QObject) -> None:
        super().__init__(parent)
        self._tabs: dict[int, _Tab] = {}
        self._views: dict[int, QQuickItem] = {}
        self._windows: set[QObject] = set()
        self._messages: dict[int, str] = {}
        self._captures: dict[str, object] = {}
        self._injecting = False
        self._closed = False
        self._script = Path(__file__).with_name("browser_actions.js").read_text()
        self.encoded.connect(self.complete)

    @Property("QVariantMap", notify=changed)  # type: ignore[arg-type]
    def tabs(self) -> dict:
        states: dict[str, dict[str, Any]] = {str(key): {"error": value} for key, value in self._messages.items()}
        states.update({str(key): {"active": bool(tab.identity), "connecting": not tab.identity,
                                 "chat": tab.chat, "label": tab.label, "working": bool(tab.command)}
                       for key, tab in self._tabs.items()})
        return states

    @Slot(int, QObject)
    def register(self, identity: int, view: QObject) -> None:
        if not isinstance(view, QQuickItem):
            return
        self._views[identity] = view
        window = view.window()
        if window is not None and window not in self._windows:
            self._windows.add(window)
            window.installEventFilter(self)
            window.destroyed.connect(lambda: self._windows.discard(window))
        # Keep the wrapper until QML destroys the item. Dropping it while its
        # scene is alive can invalidate ancestor wrappers in PySide.
        def destroyed():
            if self._views.get(identity) is view:
                self._views.pop(identity, None)
        view.destroyed.connect(destroyed)

    @Slot(int)
    def unregister(self, identity: int) -> None:
        self.release(identity)
        self._messages.pop(identity, None)

    def attach(self, identity: int, connection: Connection, chat: str, label: str) -> None:
        for other, tab in list(self._tabs.items()):
            if other == identity or (tab.connection is connection and tab.chat == chat):
                self.release(other)
        tab = _Tab(connection, chat, label)
        self._tabs[identity] = tab
        self._messages.pop(identity, None)
        self.changed.emit()

        def attached(payload, error):
            if self._tabs.get(identity) is not tab:
                if payload and payload.get("id"):
                    connection.call("DELETE", f"/api/chats/{chat}/browser/{payload['id']}", None, lambda *_: None)
                return
            if error:
                if isinstance(payload, dict) and payload.get("detail") == "Not Found":
                    error = "Update this machine's Ava backend to use browser control."
                self.release(identity, error)
                return
            tab.identity = payload["id"]
            self.changed.emit()
            self._poll(identity, tab)
        connection.call("POST", f"/api/chats/{chat}/browser", {}, attached)

    def _poll(self, identity: int, tab: _Tab) -> None:
        if self._tabs.get(identity) is not tab:
            return
        def received(payload, error):
            if self._tabs.get(identity) is not tab:
                return
            if error:
                self.release(identity, error)
                return
            if command := payload.get("command"):
                tab.command = command["id"]
                self.changed.emit()
                self.execute.emit(identity, command)
            self._poll(identity, tab)
        tab.connection.call("GET", f"/api/chats/{tab.chat}/browser/{tab.identity}", None, received)

    @Slot(int)
    @Slot(int, str)
    def release(self, identity: int, reason: str = "") -> None:
        tab = self._tabs.pop(identity, None)
        if reason:
            self._messages[identity] = reason
        else:
            self._messages.pop(identity, None)
        if tab and tab.identity and not self._closed:
            tab.connection.call("DELETE", f"/api/chats/{tab.chat}/browser/{tab.identity}", None, lambda *_: None)
        self.changed.emit()

    def connection_closed(self, connection: Connection) -> None:
        for identity, tab in list(self._tabs.items()):
            if tab.connection is connection:
                self.release(identity, "Connection closed. Hand this tab to a chat again when connected.")

    @Slot()
    def pauseForDialog(self) -> None:
        for identity in list(self._tabs):
            self.release(identity, "A dialog opened. Share this tab again when you are ready.")

    @Slot(int, str, result=bool)
    def accepts(self, identity: int, command: str) -> bool:
        tab = self._tabs.get(identity)
        return bool(tab and tab.identity and tab.command == command and not self._closed)

    @Slot(int, str, "QVariantMap")
    def complete(self, identity: int, command: str, result: dict) -> None:
        if not self.accepts(identity, command):
            return
        tab = self._tabs[identity]
        def sent(_, error):
            if self._tabs.get(identity) is not tab:
                return
            if error:
                self.release(identity, error)
            elif tab.command == command:
                tab.command = ""
                self.changed.emit()
        tab.connection.call("POST", f"/api/chats/{tab.chat}/browser/{tab.identity}", {"id": command, **result}, sent)

    @Slot("QVariantMap", result=str)
    def script(self, command: dict) -> str:
        return self._script + "\navaBrowserAction(" + json.dumps(command) + ")"

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if self._injecting or self._closed:
            return False
        for identity in list(self._tabs):
            view = self._views.get(identity)
            if view is None or not view.isVisible() or watched is not view.window():
                continue
            if event.type() in (QEvent.Type.MouseButtonPress, QEvent.Type.Wheel):
                position = cast(QMouseEvent | QWheelEvent, event).position()
                point = view.mapFromScene(position)
                if view.boundingRect().contains(point):
                    self.release(identity, "You took control of this tab.")
                elif event.type() == QEvent.Type.MouseButtonPress:
                    takeover = view.parentItem().findChild(QQuickItem, "browserTakeOver")
                    if takeover is not None and takeover.isVisible() and takeover.boundingRect().contains(takeover.mapFromScene(position)):
                        continue  # Let the button finish its click before its visibility changes.
                    composer = watched.findChild(QQuickItem, "chatComposerArea")
                    if composer is None or not composer.isVisible() or not composer.boundingRect().contains(composer.mapFromScene(position)):
                        self.release(identity, "You switched away from the shared tab.")
            elif event.type() in (QEvent.Type.KeyPress, QEvent.Type.InputMethod) and view.hasActiveFocus():
                self.release(identity, "You took control of this tab.")
        return False

    @Slot(int, str, "QVariantMap", result=str)
    def input(self, identity: int, command: str, action: dict) -> str:
        if not self.accepts(identity, command):
            return "Browser control has ended."
        view = self._views.get(identity)
        if view is None or not view.isVisible() or view.window() is None or not view.window().isExposed():
            return "Keep the browser tab visible while Ava uses it."
        window = view.window()
        if QGuiApplication.modalWindow() is not None:
            return "Close the dialog before asking Ava to operate this tab."
        self._injecting = True
        try:
            view.forceActiveFocus()
            if action["action"] in ("click", "fill"):
                point = QPointF(float(action["x"]), float(action["y"])) * float(view.property("zoomFactor"))
                if not view.boundingRect().contains(point):
                    return "The element moved outside the viewport. Take another snapshot."
                scene = view.mapToScene(point)
                screen = window.mapToGlobal(scene.toPoint())
                for kind, buttons in ((QEvent.Type.MouseButtonPress, Qt.MouseButton.LeftButton),
                                      (QEvent.Type.MouseButtonRelease, Qt.MouseButton.NoButton)):
                    QCoreApplication.sendEvent(window, QMouseEvent(kind, scene, QPointF(screen), Qt.MouseButton.LeftButton, buttons, Qt.KeyboardModifier.NoModifier))
            if action["action"] == "fill":
                modifier = Qt.KeyboardModifier.MetaModifier if sys.platform == "darwin" else Qt.KeyboardModifier.ControlModifier
                self._key(window, Qt.Key.Key_A, modifier)
                event = QInputMethodEvent()
                event.setCommitString(action.get("text", ""))
                QCoreApplication.sendEvent(window, event)
            elif action["action"] == "press":
                key = action.get("key", "")
                names = {"Enter": Qt.Key.Key_Return, "Tab": Qt.Key.Key_Tab, "Escape": Qt.Key.Key_Escape,
                         "Backspace": Qt.Key.Key_Backspace, "Delete": Qt.Key.Key_Delete,
                         "ArrowUp": Qt.Key.Key_Up, "ArrowDown": Qt.Key.Key_Down, "ArrowLeft": Qt.Key.Key_Left,
                         "ArrowRight": Qt.Key.Key_Right, "Home": Qt.Key.Key_Home, "End": Qt.Key.Key_End,
                         "PageUp": Qt.Key.Key_PageUp, "PageDown": Qt.Key.Key_PageDown}
                if key in ("Control+A", "Meta+A"):
                    self._key(window, Qt.Key.Key_A, Qt.KeyboardModifier.MetaModifier if key == "Meta+A" else Qt.KeyboardModifier.ControlModifier)
                elif key in names:
                    self._key(window, names[key], Qt.KeyboardModifier.NoModifier)
                else:
                    return "Unsupported key. Use a documented browser key."
            return ""
        finally:
            self._injecting = False

    @staticmethod
    def _key(window, key, modifier) -> None:
        for kind in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease):
            QCoreApplication.sendEvent(window, QKeyEvent(kind, key, modifier))

    @Slot(int, str)
    def capture(self, identity: int, command: str) -> None:
        if not self.accepts(identity, command):
            return
        view = self._views.get(identity)
        if view is None or not view.isVisible() or view.width() < 1 or view.height() < 1:
            self.complete(identity, command, {"error": "Keep this browser tab visible to capture it."})
            return
        ratio = min(1, 1200 / view.width(), 1000 / view.height())
        capture = cast(QQuickItemGrabResult | None, view.grabToImage(QSize(round(view.width() * ratio), round(view.height() * ratio))))
        if capture is None:
            self.complete(identity, command, {"error": "The browser viewport could not be captured."})
            return
        self._captures[command] = capture
        def ready():
            self._captures.pop(command, None)
            if not self.accepts(identity, command):
                return
            pixels = capture.image()
            def encode():
                image = pixels
                if image.width() > 1600 or image.height() > 1200:
                    image = image.scaled(1600, 1200, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
                image = image.convertToFormat(QImage.Format.Format_RGB888)
                data = QByteArray()
                buffer = QBuffer(data)
                buffer.open(QIODevice.OpenModeFlag.WriteOnly)
                ok = image.save(buffer, "PNG")  # type: ignore[call-overload]  # Qt accepts str; its generated stub declares bytes.
                buffer.close()
                result = {"text": "Captured browser viewport", "image": base64.b64encode(data.data()).decode()} if ok else {"error": "Could not encode this screenshot."}
                try:
                    self.encoded.emit(identity, command, result)
                except RuntimeError:
                    pass  # The window was closed while the worker encoded its last frame.
            QThreadPool.globalInstance().start(encode)
        capture.ready.connect(ready)

    def shutdown(self) -> None:
        for identity in list(self._tabs):
            self.release(identity)
        self._closed = True
        for window in self._windows:
            window.removeEventFilter(self)
        self._windows.clear()
