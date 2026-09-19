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


def selected_text() -> str:
    """Read the foreground selection without copying or prompting for permission."""
    if sys.platform != "darwin":
        return ""
    ax = ctypes.CDLL("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
    cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    ax.AXIsProcessTrusted.restype = ctypes.c_bool
    if not ax.AXIsProcessTrusted():
        return ""
    pointer = ctypes.c_void_p
    ax.AXUIElementCreateSystemWide.restype = pointer
    ax.AXUIElementSetMessagingTimeout.argtypes = [pointer, ctypes.c_float]
    ax.AXUIElementCopyAttributeValue.argtypes = [pointer, pointer, ctypes.POINTER(pointer)]
    ax.AXUIElementCopyAttributeValue.restype = ctypes.c_int32
    cf.CFStringCreateWithCString.argtypes = [pointer, ctypes.c_char_p, ctypes.c_uint32]
    cf.CFStringCreateWithCString.restype = pointer
    cf.CFRelease.argtypes = [pointer]
    cf.CFGetTypeID.argtypes = [pointer]
    cf.CFGetTypeID.restype = ctypes.c_ulong
    cf.CFStringGetTypeID.restype = ctypes.c_ulong
    cf.CFStringGetLength.argtypes = [pointer]
    cf.CFStringGetLength.restype = ctypes.c_long
    cf.CFStringGetMaximumSizeForEncoding.argtypes = [ctypes.c_long, ctypes.c_uint32]
    cf.CFStringGetMaximumSizeForEncoding.restype = ctypes.c_long
    cf.CFStringGetCString.argtypes = [pointer, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
    cf.CFStringGetCString.restype = ctypes.c_bool
    owned = []
    utf8 = 0x08000100

    def attribute(element, name):
        key = cf.CFStringCreateWithCString(None, name, utf8)
        owned.append(key)
        value = pointer()
        status = ax.AXUIElementCopyAttributeValue(element, key, ctypes.byref(value))
        if value.value:
            owned.append(value)
        return value if status == 0 else None

    try:
        system = ax.AXUIElementCreateSystemWide()
        owned.append(system)
        # An unresponsive foreground app must not stall the shortcut indefinitely.
        ax.AXUIElementSetMessagingTimeout(system, 0.2)
        focused = attribute(system, b"AXFocusedUIElement")
        if not focused:
            return ""
        ax.AXUIElementSetMessagingTimeout(focused, 0.2)
        text = attribute(focused, b"AXSelectedText")
        if not text or cf.CFGetTypeID(text) != cf.CFStringGetTypeID():
            return ""
        size = cf.CFStringGetMaximumSizeForEncoding(cf.CFStringGetLength(text), utf8) + 1
        buffer = ctypes.create_string_buffer(size)
        return buffer.value.decode("utf-8") if cf.CFStringGetCString(text, buffer, size, utf8) else ""
    finally:
        for value in reversed(owned):
            if value:
                cf.CFRelease(value)


def quick_chat_text() -> str:
    text = selected_text()
    if not text.strip():
        text = QGuiApplication.clipboard().text()
    return text if text.strip() else ""


def toggle_quick_chat(window, *, capture_text: bool = False) -> None:
    """Center on the pointer's display, including displays with negative origins."""
    if window.isVisible():
        window.hide()
        return
    # Capture before showing the panel changes the foreground accessibility element.
    window.setProperty("pendingText", quick_chat_text() if capture_text else "")
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
