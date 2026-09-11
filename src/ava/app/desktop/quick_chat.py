"""System-wide macOS quick-chat shortcut, without accessibility permissions."""
from __future__ import annotations

import ctypes
import sys

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QCursor, QGuiApplication


class _EventType(ctypes.Structure):
    _fields_ = [("event_class", ctypes.c_uint32), ("event_kind", ctypes.c_uint32)]


class _HotKeyID(ctypes.Structure):
    _fields_ = [("signature", ctypes.c_uint32), ("id", ctypes.c_uint32)]


class QuickChatShortcut(QObject):
    """Own the native registration and callback for exactly the app lifetime."""

    activated = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._carbon: ctypes.CDLL | None = None
        self._handler = ctypes.c_void_p()
        self._hotkey = ctypes.c_void_p()
        self.error = ""

    def register(self) -> bool:
        if sys.platform != "darwin":
            self.error = "Global quick chat is currently supported on macOS only."
            return False
        carbon = ctypes.CDLL("/System/Library/Frameworks/Carbon.framework/Carbon")
        callback_type = ctypes.CFUNCTYPE(ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
        carbon.GetApplicationEventTarget.restype = ctypes.c_void_p
        carbon.InstallEventHandler.argtypes = [ctypes.c_void_p, callback_type, ctypes.c_uint32, ctypes.POINTER(_EventType), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        carbon.InstallEventHandler.restype = ctypes.c_int32
        carbon.RegisterEventHotKey.argtypes = [ctypes.c_uint32, ctypes.c_uint32, _HotKeyID, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)]
        carbon.RegisterEventHotKey.restype = ctypes.c_int32
        carbon.UnregisterEventHotKey.argtypes = [ctypes.c_void_p]
        carbon.RemoveEventHandler.argtypes = [ctypes.c_void_p]
        self._carbon = carbon

        def pressed(_handler, _event, _data):
            self.activated.emit()
            return 0

        self._callback = callback_type(pressed)
        event = _EventType(int.from_bytes(b"keyb", "big"), 6)
        target = carbon.GetApplicationEventTarget()
        status = carbon.InstallEventHandler(target, self._callback, 1, ctypes.byref(event), None, ctypes.byref(self._handler))
        if not status:
            # ANSI space, cmdKey | optionKey. Carbon hotkeys need no input monitoring.
            status = carbon.RegisterEventHotKey(49, (1 << 8) | (1 << 11), _HotKeyID(int.from_bytes(b"AvaQ", "big"), 1), target, 0, ctypes.byref(self._hotkey))
        if status:
            self.error = f"Could not register ⌘⌥Space (macOS error {status}); it may be in use by another app."
            self.close()
            return False
        return True

    def close(self) -> None:
        if self._carbon:
            if self._hotkey.value:
                self._carbon.UnregisterEventHotKey(self._hotkey)
                self._hotkey = ctypes.c_void_p()
            if self._handler.value:
                self._carbon.RemoveEventHandler(self._handler)
                self._handler = ctypes.c_void_p()


def toggle_quick_chat(window) -> None:
    """Center on the pointer's display, including displays with negative origins."""
    if window.isVisible():
        window.hide()
        return
    screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
    if screen:
        window.setScreen(screen)
        rect = screen.availableGeometry()
        window.setWidth(min(window.width(), rect.width()))
        window.setHeight(min(window.height(), rect.height()))
        window.setPosition(rect.x() + (rect.width() - window.width()) // 2,
                           rect.y() + (rect.height() - window.height()) // 2)
    window.show()
    window.raise_()
    window.requestActivate()
