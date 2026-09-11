"""Qt UI and persistent backend acceptance scenarios, using a gated local model server."""

from __future__ import annotations

import gc
import json
import os
import plistlib
import re
import shlex
import sqlite3
import subprocess
import sys
import threading
import time
import weakref
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from signal import SIGKILL
from typing import Any, cast

import httpx
import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import (  # noqa: E402
    Q_ARG,
    QCoreApplication,
    QEvent,
    QEventLoop,
    QLocale,
    QMetaObject,
    QObject,
    QPersistentModelIndex,
    QPoint,
    QPointF,
    QProcess,
    QRectF,
    QSettings,
    Qt,
    QTimer,
    QUrl,
)
from PySide6.QtGui import (  # noqa: E402
    QGuiApplication,
    QImage,
    QInputMethodEvent,
    QKeySequence,
    QTextTable,
)
from PySide6.QtQml import QJSValue  # noqa: E402
from PySide6.QtQuick import QQuickWindow  # noqa: E402
from PySide6.QtQuickControls2 import QQuickStyle  # noqa: E402
from PySide6.QtTest import QAbstractItemModelTester, QSignalSpy, QTest  # noqa: E402
from PySide6.QtWebEngineQuick import QtWebEngineQuick  # noqa: E402
from shiboken6 import getCppPointer, isValid  # noqa: E402

from ava.app.backend import stop as stop_backend  # noqa: E402
from ava.app.desktop.application import create_engine  # noqa: E402
from ava.app.desktop.controller import Controller  # noqa: E402
from ava.app.desktop.transcript import Transcript  # noqa: E402


def until(predicate, *signals, timeout=10_000):
    """Advance the GUI loop on real signals; the timer only bounds a failing test."""
    if predicate():
        return
    started = time.monotonic()
    loop = QEventLoop()
    deadline = QTimer()
    deadline.setSingleShot(True)
    deadline.timeout.connect(loop.quit)

    def check(*_):
        if predicate():
            loop.quit()

    for signal in signals:
        signal.connect(check)
    deadline.start(timeout)
    try:
        if not predicate():
            loop.exec()
    finally:
        deadline.stop()
        for signal in signals:
            signal.disconnect(check)
    assert predicate(), f"condition did not become true before the loop ended ({time.monotonic() - started:.3f}s / {timeout / 1000:.3f}s deadline)"


@pytest.fixture(scope="module")
def qt_app():
    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        os.environ.setdefault("QT_QUICK_BACKEND", "software")
    QtWebEngineQuick.initialize()
    app = QGuiApplication.instance() or QGuiApplication([])
    assert isinstance(app, QGuiApplication)
    # Match the product: closing a prior test window must not quit a later
    # nested event loop while its asynchronous QML components are loading.
    app.setQuitOnLastWindowClosed(False)
    QQuickStyle.setStyle("Basic")
    yield app


@dataclass
class Exchange:
    request: dict
    release: threading.Event = field(default_factory=threading.Event)


@pytest.fixture
def model_server(home, monkeypatch):
    monkeypatch.setenv("AVA_DESKTOP_TEST_KEY", "local-test-only")
    exchanges: list[Exchange] = []
    shutting_down = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def handle(self):
            try:
                super().handle()
            except (BrokenPipeError, ConnectionResetError):
                pass  # Browser navigation/closing may cancel an in-flight request.

        def do_GET(self):
            if self.path == "/failed-navigation":
                self.send_response(302)
                self.send_header("Location", "/failed-navigation")
                self.end_headers()
                return
            self.send_response(200)
            if self.path == "/pending-analytics":
                # Keep a real Qt HTTP request pending while its backend reconnects.
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                shutting_down.wait()
            elif self.path == "/api/projects":
                # The server exits after success headers, before its response body arrives.
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "100")
                self.end_headers()
            elif self.path in ("/browser-h264.mp4", "/browser-vp9.webm"):
                self.send_header(
                    "Content-Type", "video/mp4" if self.path.endswith("mp4") else "video/webm"
                )
                self.end_headers()
                self.wfile.write((Path(__file__).parent / "fixtures" / self.path[1:]).read_bytes())
            elif self.path in ("/video-mp4", "/video-webm"):
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                source = "/browser-h264.mp4" if self.path == "/video-mp4" else "/browser-vp9.webm"
                self.wfile.write(
                    (
                        "<title>Loading video</title><video controls muted onloadeddata=\"document.title='Video ready'\" onerror=\"document.title='Media failed'\" src='"
                        + source
                        + "'></video><script>console.warn('VIDEOJS: WARN: Using the tech directly can be dangerous.');</script>"
                    ).encode()
                )
            elif self.path == "/agent-guards":
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(
                    b'<title>Guarded form</title><style>body{font:16px system-ui;padding:20px}input,button{padding:12px}#cover{position:fixed;inset:0;z-index:10;background:white;padding:20px;display:none}</style>'
                    b'<input aria-label="Guarded field"><button onclick="document.querySelector(\'#cover\').style.display=\'block\'">Cover form</button>'
                    b'<div id="cover"><button onclick="this.parentElement.style.display=\'none\'">Dismiss overlay</button></div>'
                )
            elif self.path == "/agent-browser":
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(
                    b'<title>Browser task</title><style>body{font:16px system-ui;padding:18px}input,button{font:inherit;padding:10px;max-width:100%;box-sizing:border-box}</style>'
                    b'<h1>Browser task</h1><form onsubmit="event.preventDefault();document.title=\'Browser done\';'
                    b'document.querySelector(\'output\').textContent=document.querySelector(\'input\').value+\' / input \'+window.inputTrusted+\' / click \'+window.clickTrusted">'
                    b'<label>Your name<input oninput="window.inputTrusted=event.isTrusted"></label>'
                    b'<button onclick="window.clickTrusted=event.isTrusted">Submit</button></form><output></output>'
                    b'<p><label>Priority<select onchange="document.querySelector(\'#choice\').textContent=this.value"><option>Normal</option><option>High</option></select></label><span id="choice"></span></p>'
                    b'<div id="shadow"></div><script>document.querySelector("#shadow").attachShadow({mode:"open"}).innerHTML=\'<input aria-label="Shadow note" oninput="this.setAttribute(\\\'data-trusted\\\',event.isTrusted)">\';</script>'
                    b'<iframe title="Form frame" src="/agent-frame" style="width:95%;height:100px;margin-top:14px"></iframe><p><a href="/next">Next page</a></p>'
                )
            elif self.path == "/agent-frame":
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(b'<label>Frame note<input oninput="this.setAttribute(\'data-trusted\',event.isTrusted)"></label>')
            elif self.path == "/text-menu":
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(
                    ("<title>Text menus ready</title><style>body{font:16px system-ui}"
                     "textarea{position:absolute;left:20px;top:20px;width:80%;height:80px}"
                     "p{position:absolute;left:20px;top:130px}</style>"
                     "<textarea oninput='document.title=this.value' "
                     "onselect=\"if(this.selectionStart!==this.selectionEnd)document.title='Selected:'+this.value\">网页文字 selection</textarea>"
                     "<p>只读网页 Read only content</p><script>document.onselectionchange=()=>{"
                     "if(document.activeElement.tagName!=='TEXTAREA' && !getSelection().isCollapsed)document.title='Selected page'"
                     "}</script>").encode()
                )
            elif self.path == "/v1/models":
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"data":[{"id":"fixture"},{"id":"fixture-reasoning"}]}')
            elif self.path == "/account":
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                authenticated = "fixture_session=imported-session" in self.headers.get("Cookie", "")
                title = "Account ready" if authenticated else "Sign in"
                self.wfile.write(
                    (
                        f"<title>{title}</title><h1>{title}</h1>"
                        "<script>if(document.cookie.includes('fixture_session')) document.title='HttpOnly lost'</script>"
                    ).encode()
                )
            else:
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(
                    b'<html><title>Loading</title><body style="font-family:system-ui;padding:24px"><h1>Browser preview</h1><p>A real local web page.</p><a href="/next">Next page</a><script>document.title=location.pathname === "/next" ? "Next page" : "Browser ready"</script></body></html>'
                )

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            exchange = Exchange(request)
            exchanges.append(exchange)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()

            def chunk(delta, reason=None):
                frame = {"choices": [{"index": 0, "delta": delta, "finish_reason": reason}]}
                self.wfile.write(f"data: {json.dumps(frame)}\n\n".encode())
                self.wfile.flush()

            try:
                if request.get("model") == "fixture-browser-guards":
                    results = [message for message in request["messages"] if message["role"] == "tool"]
                    number = len(results)
                    action = None
                    if number == 0:
                        action = {"action": "navigate", "url": f"http://127.0.0.1:{self.server.server_port}/agent-guards"}
                    elif number in (1, 2, 3, 4, 6):
                        source = {1: 0, 2: 1, 3: 1, 4: 3, 6: 4}[number]
                        name = {1: 'Cover form', 2: 'Guarded field', 3: 'Dismiss overlay', 4: 'Guarded field', 6: 'Guarded field'}[number]
                        page = json.loads(results[source]['content'])
                        ref = next(element['ref'] for element in page['elements'] if element['name'] == name)
                        action = {"action": "click", "ref": ref} if number in (1, 3) else {"action": "fill", "ref": ref, "text": "Accepted"}
                    elif number == 5:
                        action = {"action": "navigate", "url": f"http://127.0.0.1:{self.server.server_port}/next"}
                    elif number == 7:
                        action = {"action": "navigate", "url": f"http://127.0.0.1:{self.server.server_port}/failed-navigation"}
                    if action:
                        chunk({"tool_calls": [{"index": 0, "id": f"guard-{number}", "type": "function", "function": {"name": "browser", "arguments": json.dumps(action)}}]})
                        chunk({}, "tool_calls")
                        self.wfile.write(b"data: [DONE]\n\n")
                        self.wfile.flush()
                        return
                if request.get("model") == "fixture-browser":
                    results = [message for message in request["messages"] if message["role"] == "tool"]
                    wait_mode = any(message["role"] == "user" and any(part.get("text") == "Wait for the missing page text." for part in message["content"]) for message in request["messages"])
                    action = None
                    if wait_mode:
                        if not any(result.get("tool_call_id") == "browser-wait" for result in results):
                            action = {"action": "wait", "text": "This text never arrives"}
                    elif not results:
                        action = {"action": "navigate", "url": f"http://127.0.0.1:{self.server.server_port}/agent-browser"}
                    elif len(results) < 3:
                        page = json.loads(results[-1]["content"])
                        name = "Your name" if len(results) == 1 else "Submit"
                        ref = next((element["ref"] for element in page["elements"] if element["name"] == name), None)
                        assert ref is not None, (name, page)
                        action = {"action": "fill", "ref": ref, "text": "Ava 中文"} if len(results) == 1 else {"action": "click", "ref": ref}
                    elif len(results) == 3:
                        action = {"action": "screenshot"}
                    elif len(results) == 4:
                        action = {"action": "snapshot"}
                    elif len(results) in (5, 6, 7, 10):
                        name = {5: "Shadow note", 6: "Priority", 7: "Frame note", 10: "Next page"}[len(results)]
                        page = json.loads(results[-1]["content"])
                        ref = next(element["ref"] for element in page["elements"] if element["name"] == name)
                        action = {"action": "select", "ref": ref, "value": "High"} if len(results) == 6 else {"action": "click", "ref": ref} if len(results) == 10 else {"action": "fill", "ref": ref, "text": "Nested input"}
                    elif len(results) == 8:
                        action = {"action": "press", "key": "Tab"}
                    elif len(results) == 9:
                        action = {"action": "scroll", "y": -10000}
                    elif len(results) == 11:
                        action = {"action": "back"}
                    if action:
                        call_id = "browser-wait" if wait_mode else f"browser-{len(results)}"
                        chunk({"tool_calls": [{"index": 0, "id": call_id, "type": "function", "function": {"name": "browser", "arguments": json.dumps(action)}}]})
                        chunk({}, "tool_calls")
                        self.wfile.write(b"data: [DONE]\n\n")
                        self.wfile.flush()
                        return
                if request.get("model") == "fixture-mcp-image" and not any(message.get("role") == "tool" for message in request.get("messages", [])):
                    function = next(tool["function"] for tool in request["tools"] if "inspect_page" in tool["function"]["name"])
                    chunk({"tool_calls": [{"index": 0, "id": "browser-image", "type": "function", "function": {"name": function["name"], "arguments": "{}"}}]})
                    chunk({}, "tool_calls")
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                    return
                if request.get("model") == "fixture-mcp-tools" and not any(message.get("role") == "tool" for message in request.get("messages", [])):
                    function = next(tool["function"] for tool in request["tools"] if "record_change" in tool["function"]["name"])
                    arguments = {"change": {"title": "Native MCP check", "labels": ["ux", "tests", "refresh-tools"], "approved": True}}
                    chunk({"tool_calls": [{"index": 0, "id": "mcp-proof", "type": "function", "function": {"name": function["name"], "arguments": json.dumps(arguments)}}]})
                    chunk({}, "tool_calls")
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                    return
                if request.get("model") == "fixture-remote-tools" and not any(message.get("role") == "tool" for message in request.get("messages", [])):
                    chunk({"tool_calls": [{"index": 0, "id": "remote-proof", "type": "function", "function": {"name": "bash", "arguments": json.dumps({"command": "pwd; printf 'remote-fixture' > remote-test-proof.txt"})}}]})
                    chunk({}, "tool_calls")
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                    return
                if request.get("model") == "fixture-analytics" and not any(message.get("role") == "tool" for message in request.get("messages", [])):
                    chunk({"tool_calls": [{"index": 0, "id": "skill-proof", "type": "function", "function": {"name": "read", "arguments": json.dumps({"path": ".agents/skills/review/SKILL.md"})}}]})
                    chunk({}, "tool_calls")
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                    return
                chunk(
                    {
                        "content": "## A clearer workspace\n\n### 清晰、自然的中文阅读体验\n\n使用苹方显示中文，保留熟悉的系统字体。文件、图片和技能都可以直接加入对话，让每一次交流更轻松。\n\n**Native controls**, with room to focus.\n\n- Files and images\n- Commands and skills\n\n```python\nprint('hello')\n```\n\n[Open preview](http://127.0.0.1:"
                        + str(cast(ThreadingHTTPServer, self.server).server_port)
                        + "/preview)\n\n"
                        if request.get("model") == "fixture-reasoning"
                        else "Hello "
                    }
                )
                # Tests own completion. A wall-clock timeout used to end the
                # stream during longer SSH/GUI flows, before they released it.
                if shutting_down.is_set():
                    exchange.release.set()
                if exchange.release.wait():
                    chunk({"content": "世界"})
                    chunk({}, "stop")
                    if request.get("model") == "fixture-analytics":
                        self.wfile.write(b'data: {"choices":[],"usage":{"prompt_tokens":1200,"completion_tokens":80,"prompt_tokens_details":{"cached_tokens":200}}}\n\n')
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass  # Cancellation closes the provider connection.

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    settings = {
        "provider": "desktop-test",
        "model": "fixture",
        "providers": {
            "desktop-test": {
                "family": "openai",
                "api_key_env": "AVA_DESKTOP_TEST_KEY",
                "base_url": f"http://127.0.0.1:{server.server_port}/v1",
                "models": {
                    "fixture": {"context_window": 10000},
                    "fixture-reasoning": {
                        "context_window": 10000,
                        "effort_values": ["low", "high"],
                    },
                },
            }
        },
    }
    (home / "settings.json").write_text(json.dumps(settings))
    try:
        yield exchanges
    finally:
        shutting_down.set()
        for exchange in exchanges:
            exchange.release.set()
        server.shutdown()
        server.server_close()
        thread.join(2)


@pytest.fixture
def desktop(qt_app, home, project, capfd):
    controller = Controller(
        project, [], QSettings(str(home / "desktop.ini"), QSettings.Format.IniFormat)
    )
    engine = create_engine(controller)
    assert engine.rootObjects()
    window = engine.rootObjects()[0]
    assert isinstance(window, QQuickWindow)
    yield controller, window
    pdf_worker = controller.pdf_images._process
    controller.shutdown()
    until(
        lambda: controller._closed_emitted,
        controller.runtime.stopped,
        controller.closed,
        timeout=15_000,
    )
    if pdf_worker is not None:
        pdf_worker.wait(timeout=2)
    window.hide()
    engine.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    stderr = capfd.readouterr().err
    assert "Traceback (most recent call last)" not in stderr, stderr
    assert "Binding loop" not in stderr, stderr
    assert "QTextCursor::setPosition" not in stderr, stderr
    assert "Release of profile requested" not in stderr, stderr
    assert "QProcess: Destroyed" not in stderr, stderr
    assert "Cannot do an immediate re-layout" not in stderr, stderr
    assert not any(
        "/desktop/qml/" in line and any(word in line for word in ("Error:", "Cannot", "Unable"))
        for line in stderr.splitlines()
    ), stderr


def visible_rect(window, item):
    rect = item.mapRectToScene(item.boundingRect()).intersected(
        QRectF(0, 0, window.width(), window.height())
    )
    parent = item.parentItem()
    while parent is not None:
        if parent.clip():
            rect = rect.intersected(parent.mapRectToScene(parent.boundingRect()))
        parent = parent.parentItem()
    return rect


def find_item(window, name):
    # Keep wrappers alive while QML owns the items, including those beneath Loaders.
    # WebEngine's scene graph is private: only inspect its public root item.
    retained = window.__dict__.setdefault("_test_items", {})
    fallback = None
    pending = [window.contentItem()]
    while pending:
        item = pending.pop()
        if not isValid(item):
            continue
        retained[getCppPointer(item)[0]] = item
        if item.objectName() == name:
            if item.isVisible() and not visible_rect(window, item).isEmpty():
                return item
            fallback = item
        if item.objectName() not in ("webBrowser", "terminalWeb"):
            pending.extend(reversed(item.childItems()))
    return fallback


def click(window, name):
    # isActive() also includes a transient dialog; activate the actual target window.
    if QGuiApplication.focusWindow() != window:
        window.requestActivate()
    # A newly created native window may not be exposed when the backend is ready.
    assert QTest.qWaitForWindowExposed(window, 2000), "target window was not exposed"
    # Hit-test the rendered layout, after asynchronous models and pane changes settle.
    frame = QSignalSpy(window.frameSwapped)
    window.update()
    assert frame.count() or frame.wait(2000), (
        f"target window did not render: exposed={window.isExposed()}, "
        f"active={window.isActive()}, app={QGuiApplication.applicationState()}"
    )
    until(
        lambda: (target := find_item(window, name)) is not None and target.isVisible()
        and target.property("enabled") and not visible_rect(window, target).isEmpty(),
        window.frameSwapped,
        timeout=3000,
    )
    item = find_item(window, name)
    assert item is not None, name
    assert item.property("enabled"), name
    QCoreApplication.processEvents()
    ancestors = []
    parent = item
    while parent is not None:
        ancestors.append(parent)
        parent = parent.parentItem()
    for parent in reversed(ancestors):
        parent.ensurePolished()
    rect = visible_rect(window, item)
    assert not rect.isEmpty(), f"{name} is outside the visible viewport"
    position = rect.center().toPoint()
    QTest.mouseClick(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, position)


def type_message(window, text, *, append=False):
    composer = find_item(window, "composer")
    before = composer.property("text") if append else ""
    composer.forceActiveFocus()
    event = QInputMethodEvent()
    event.setCommitString(text)
    QCoreApplication.sendEvent(composer, event)
    assert composer.property("text") == before + text


@pytest.mark.parametrize("stage", ["idle", "streaming", "inspector", "terminal", "automation"])
def test_desktop_entrypoint_exits_after_window_close(home, project, model_server, stage):
    # Exercise app.exec()/app.quit(), not just backend termination in a nested test loop.
    script = """
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from PySide6.QtCore import QTimer
from PySide6.QtQuick import QQuickWindow
from ava.app.desktop import application

stage = sys.argv.pop()
create_engine = application.create_engine
def close_when_ready(controller):
    engine = create_engine(controller)
    window = engine.rootObjects()[0]
    assert isinstance(window, QQuickWindow)
    if stage == "idle":
        controller.runtime.ready.connect(lambda *_: QTimer.singleShot(0, window.close))
    else:
        started = sent = False
        def advance():
            nonlocal started, sent
            if controller.projects and controller.projectPath and not started:
                started = True
                if stage == "automation":
                    controller.automations.saved.connect(lambda: QTimer.singleShot(0, window.close))
                    controller.automations.save("local", "", {
                        "name": "After the desktop closes", "prompt": "Summarize recent changes.",
                        "project_id": controller.projectId,
                        "schedule": {"start_local": (datetime.now(UTC) + timedelta(seconds=2)).replace(tzinfo=None).isoformat(), "timezone": "UTC", "cadence": "once", "count": 1},
                    })
                elif stage == "inspector":
                    controller.browseFiles("")
                    QTimer.singleShot(0, window.close)
                elif stage == "terminal":
                    terminal = controller.createTerminal(controller.projectPath)
                    terminal.outputReceived.connect(lambda _, size: terminal.acknowledge(size))
                    terminal.start(80, 24)
                    terminal.write("sleep 60 & echo $! > closing-child.pid; wait\\r")
                    timer = QTimer(window)
                    timer.setInterval(20)
                    marker = Path(controller.projectPath) / "closing-child.pid"
                    def close_terminal_window():
                        if marker.exists() and marker.stat().st_size:
                            timer.stop()
                            window.close()
                    timer.timeout.connect(close_terminal_window)
                    timer.start()
                else:
                    QTimer.singleShot(0, controller.newChat)
            elif controller.connected and not sent:
                sent = True
                controller._set_draft("Reply with OK.")
                QTimer.singleShot(0, controller.send)
            elif any(row["kind"] == "assistant" for row in controller._transcript.rows):
                QTimer.singleShot(0, window.close)
        controller.changed.connect(advance)
    return engine

application.create_engine = close_when_ready
raise SystemExit(application.run())
"""
    with subprocess.Popen(
        [sys.executable, "-c", script, "--project", str(project), stage],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen", "QT_QUICK_BACKEND": "software"},
    ) as process:
        try:
            _, stderr = process.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            _, stderr = process.communicate(timeout=5)
            pytest.fail(f"Desktop did not exit after closing its window: {stderr}")
    assert process.returncode == 0, stderr
    assert "TypeError:" not in stderr and "Binding loop" not in stderr, stderr
    if stage == "automation":
        import signal
        from uuid import uuid4

        import httpx

        from ava.app.backend import connect
        from tests.test_backend import command

        endpoint = connect(home)
        assert endpoint is not None, "The backend must outlive the desktop"
        with httpx.Client(base_url=f"http://127.0.0.1:{endpoint['port']}", trust_env=False,
                          headers={"Authorization": "Bearer " + endpoint["token"]}) as client:
            identity = client.get("/api/automations").json()["automations"][0]["id"]
            deadline = time.monotonic() + 8
            while not model_server and time.monotonic() < deadline:
                time.sleep(0.02)
            assert len(model_server) == 1
            model_server[0].release.set()
            while time.monotonic() < deadline:
                result = client.get(f"/api/automations/{identity}").json()
                if result["runs"] and result["runs"][0]["status"] == "completed":
                    break
                time.sleep(0.02)
            assert result["remaining"] == 0 and result["runs"][0]["status"] == "completed"
            completed_chat = result["runs"][0]["chat_id"]
            assert client.post(f"/api/automations/{identity}/run", json={"request_id": uuid4().hex}).status_code == 202
            deadline = time.monotonic() + 8
            while len(model_server) < 2 and time.monotonic() < deadline:
                time.sleep(0.02)
            assert len(model_server) == 2
        os.kill(endpoint["pid"], signal.SIGKILL)
        restarted = command(project, "connect")
        assert restarted.returncode == 0, restarted.stderr
        second = json.loads(restarted.stdout)
        assert second["instance_id"] != endpoint["instance_id"]
        with httpx.Client(base_url=f"http://127.0.0.1:{second['port']}", trust_env=False,
                          headers={"Authorization": "Bearer " + second["token"]}) as client:
            recovered = client.get(f"/api/automations/{identity}").json()
            assert [run["status"] for run in recovered["runs"]] == ["interrupted", "completed"]
            assert recovered["runs"][1]["chat_id"] == completed_chat
            assert recovered["remaining"] == 0
            model_server[1].release.set()
            time.sleep(1.2)  # Cross a dispatcher tick and confirm there is no automatic replay.
            assert len(model_server) == 2
    else:
        assert len(model_server) == (1 if stage == "streaming" else 0)
    if stage == "terminal":
        child = int((project / "closing-child.pid").read_text())
        with pytest.raises(ProcessLookupError):
            os.kill(child, 0)


@pytest.mark.parametrize("failure", ["settings", "credentials"])
def test_desktop_restored_codex_chat_reports_configuration_error(
    desktop, home, project, monkeypatch, failure
):
    from ava.session import Log, Selection

    log = Log.create_default(project, "codex", "gpt-5.6-sol")
    log.append(Selection(provider="codex", model="gpt-5.6-sol", effort="xhigh"))
    log.close()
    monkeypatch.setenv("CODEX_HOME", str(home / "missing-codex"))
    if failure == "settings":
        (home / "settings.json").write_text(
            '{"provider": "codex", "model": "gpt-6-astra", effort="high"}'
        )
        expected = "cannot parse settings file"
    else:
        expected = "Codex credentials are unavailable; run 'codex login' and restart Ava"

    controller, window = desktop
    controller.start()
    until(lambda: bool(controller.chats), controller.changed)
    controller.openChat(controller.chats[0]["id"])
    until(lambda: controller.connected, controller.changed)
    assert not controller.error
    type_message(window, "Reply with OK.")
    QTest.keyClick(window, Qt.Key.Key_Return)
    until(
        lambda: any(row["kind"] == "error" for row in controller._transcript.rows),
        controller.changed,
    )
    errors = [row["body"] for row in controller._transcript.rows if row["kind"] == "error"]
    assert len(errors) == 1
    assert expected in errors[0]
    assert "does not advertise" not in errors[0]


@pytest.mark.skipif(os.environ.get("AVA_SERVICE_TESTS") != "1", reason="Opt in to isolated OS service testing")
def test_desktop_background_startup_can_be_enabled_and_disabled(desktop, model_server, home, project):
    from ava.app.backend_service import uninstall_service
    from ava.llm.credentials import save_api_key

    save_api_key("desktop-test", "local-test-only")
    controller, window = desktop
    controller.start()
    until(lambda: bool(controller.projects), controller.changed)
    click(window, "settingsButton")
    try:
        assert find_item(window, "backgroundStartupButton") is not None, "Background startup needs a settings entry"
        until(lambda: not controller.serviceState["loading"], controller.serviceStateChanged)
        scroll = find_item(window, "settingsGeneralScroll")
        scroll.setProperty("contentY", max(0, scroll.property("contentHeight") - scroll.height()))
        click(window, "backgroundStartupButton")
        until(lambda: controller.serviceState.get("active") and not controller.serviceState["busy"], controller.serviceStateChanged, timeout=40_000)
        assert controller.serviceState["autostart"]
        until(lambda: controller.online and controller.projectId, controller.changed)
        save_screenshot(window, "background-startup-enabled")
        QTest.keyClick(window, Qt.Key.Key_Escape)
        until(
            lambda: not (button := find_item(window, "closeSettingsButton")) or not button.isVisible(),
            window.frameSwapped,
        )
        click(window, "newChatButton")
        until(lambda: controller.connected or bool(controller.error), controller.changed)
        assert controller.connected, controller.error
        type_message(window, "A task using saved credentials in the supervised backend")
        QTest.keyClick(window, Qt.Key.Key_Return)
        until(lambda: any(row["body"] == "Hello " for row in controller._transcript.rows), controller.changed)
        click(window, "settingsButton")
        until(lambda: not controller.serviceState["loading"], controller.serviceStateChanged)
        scroll.setProperty("contentY", max(0, scroll.property("contentHeight") - scroll.height()))
        click(window, "backgroundStartupButton")
        until(lambda: bool(controller.serviceState["error"]), controller.serviceStateChanged)
        assert "Tasks are active" in controller.serviceState["error"]
        assert controller.serviceState["installed"] and controller.status == "running"
        window.setProperty("dark", True)
        window.setWidth(800)
        save_screenshot(window, "background-startup-running-protected")
        model_server[0].release.set()
        until(lambda: controller.status == "idle", controller.changed)
        assert any(row["body"] == "Hello 世界" for row in controller._transcript.rows)
        click(window, "backgroundStartupButton")
        until(lambda: not controller.serviceState.get("installed") and not controller.serviceState["busy"], controller.serviceStateChanged, timeout=40_000)
        assert not controller.serviceState["error"]
        until(lambda: controller.online, controller.changed)
        save_screenshot(window, "background-startup-disabled")
    finally:
        uninstall_service(home, force=True)


def test_desktop_controller_is_released_after_shutdown(qt_app, home, project):
    controller = Controller(project, [], QSettings(str(home / "lifetime.ini"), QSettings.Format.IniFormat))
    reference = weakref.ref(controller)
    controller.shutdown()
    del controller
    gc.collect()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert reference() is None, "Connection callbacks must not retain a closed controller"


def test_desktop_machine_management_shows_local_connection(desktop):
    controller, window = desktop
    controller.start()
    until(lambda: bool(controller.projects), controller.changed)
    assert find_item(window, "machinesButton") is not None, "Machines need a visible management entry"
    click(window, "machinesButton")
    assert find_item(window, "machineHostField") is not None
    assert controller.machines[0]["name"] == ("This Mac" if sys.platform == "darwin" else "This computer")
    assert controller.machines[0]["online"]
    save_screenshot(window, "machines-local")
    QTest.keyClick(window, Qt.Key.Key_Escape)
    window.setProperty("dark", True)
    target = find_item(window, "settingsButton")
    QTest.mouseMove(window, visible_rect(window, target).center().toPoint())
    until(lambda: (tip := find_item(window, "nativeToolTipText")) is not None and tip.isVisible(), window.frameSwapped)
    assert find_item(window, "nativeToolTipText").property("color").lightnessF() > 0.6
    assert find_item(window, "nativeToolTipBackground").property("color").lightnessF() < 0.3
    save_screenshot(window, "native-tooltip-dark")


@pytest.mark.skipif(os.environ.get("AVA_DESKTOP_SSH_TESTS") != "1" or not os.environ.get("AVA_SSH_TEST_CONTAINER"), reason="Requires the isolated Fedora SSH fixture")
def test_desktop_remote_machine_host_trust(desktop, tmp_path, monkeypatch):
    # Use a separate trust store; the real user's known_hosts is never involved.
    original = Path(os.environ["AVA_SSH_CONFIG"]).read_text()
    config = tmp_path / "ssh_config"
    known = tmp_path / "known_hosts"
    known.write_text("")
    config.write_text("\n".join(
        f'    UserKnownHostsFile "{known}"' if line.strip().startswith("UserKnownHostsFile ") else line
        for line in original.splitlines()
    ) + "\n")
    config.write_text(config.read_text() + config.read_text().replace("Host ava-test", "Host ava-second").replace("HostKeyAlias ava-service-fixture", "HostKeyAlias ava-second-fixture"))
    monkeypatch.setenv("AVA_SSH_CONFIG", str(config))
    container = os.environ["AVA_SSH_TEST_CONTAINER"]

    def inspect(*arguments):
        return subprocess.run(["docker", "exec", container, *arguments], check=True,
                              capture_output=True, text=True, timeout=10).stdout.strip()

    fingerprint = inspect("ssh-keygen", "-lf", "/etc/ssh/ssh_host_ed25519_key.pub", "-E", "sha256").split()[1]
    deployments = inspect("python3", "-c", "from pathlib import Path; print(sorted(str(p) for p in Path('/home/ava-test/.local/share/ava/backends').glob('*')))")
    controller, window = desktop
    controller.start()
    until(lambda: bool(controller.projects), controller.changed)
    click(window, "machinesButton")
    find_item(window, "machineHostField").setProperty("text", "ava-test")
    find_item(window, "machineNameField").setProperty("text", "Fedora 首次连接")
    click(window, "addMachineAction")
    until(lambda: bool(find_item(window, "hostKeyFingerprint")) or bool(controller.machines[1]["error"]), window.frameSwapped, controller.machinesChanged, timeout=15_000)
    save_screenshot(window, "ssh-first-trust")
    assert find_item(window, "hostKeyFingerprint") is not None, controller.machines[1]["error"]
    assert find_item(window, "hostKeyFingerprint").property("text") == fingerprint
    identity = controller.machines[1]["id"]
    # Closing the prompt is a refusal, not an implicit acceptance.
    QTest.keyClick(window, Qt.Key.Key_Escape)
    until(lambda: not controller.machines[1]["busy"], controller.machinesChanged)
    assert known.read_text() == ""
    assert deployments == inspect("python3", "-c", "from pathlib import Path; print(sorted(str(p) for p in Path('/home/ava-test/.local/share/ava/backends').glob('*')))")
    assert "cancel" in controller.machines[1]["error"].lower()
    # Saved connections may prompt concurrently during app startup. A later
    # arrival must never replace the identity the user is currently reviewing.
    controller.addMachine("ava-second", "Another Fedora")
    until(lambda: controller.hostKeyRequest.get("alias") == "ava-second", controller.hostKeyChanged)
    second = controller.hostKeyRequest["machine"]
    controller.reconnectMachine(identity)
    until(lambda: bool(controller._machines[identity].runtime.host_key), controller.hostKeyChanged)
    assert controller.hostKeyRequest["machine"] == second
    click(window, "cancelHostKeyButton")
    until(lambda: controller.hostKeyRequest.get("machine") == identity, controller.hostKeyChanged)
    click(window, "cancelHostKeyButton")
    until(lambda: not controller.hostKeyRequest and all(not m["busy"] for m in controller.machines), controller.machinesChanged)
    QTest.wheelEvent(window, visible_rect(window, find_item(window, "machineConnectionsList")).center(), QPoint(0, -1200))
    until(lambda: not find_item(window, "machineConnectionsList").property("moving"), window.frameSwapped)
    click(window, "removeMachine_" + second)
    until(lambda: len(controller.machines) == 2, controller.machinesChanged)
    assert known.read_text() == ""
    click(window, "selectMachine_" + identity)
    until(lambda: (item := find_item(window, "hostKeyFingerprint")) is not None and item.isVisible(), window.frameSwapped)
    window.setProperty("dark", True)
    window.setWidth(800)
    window.setHeight(600)
    click(window, "copyHostKeyButton")
    assert QGuiApplication.clipboard().text() == fingerprint
    save_screenshot(window, "ssh-first-trust-dark")
    click(window, "trustHostKeyButton")
    until(lambda: controller.machines[1]["online"] or bool(controller.machines[1]["error"]), controller.machinesChanged, timeout=180_000)
    assert controller.machines[1]["online"], controller.machines[1]["error"]
    trusted = known.read_bytes()
    assert trusted
    runtime = controller._machines[identity].runtime
    first_instance = runtime.info["instance_id"]
    runtime.process.terminate()
    until(lambda: not controller.machines[1]["online"], controller.machinesChanged)
    until(lambda: controller.machines[1]["online"], controller.machinesChanged, timeout=40_000)
    assert controller.machines[1]["online"], controller.machines[1]["error"]
    assert runtime.info["instance_id"] == first_instance
    assert not controller.hostKeyRequest
    assert known.read_bytes() == trusted

    # A different recorded key must be blocked without a Trust action.
    wrong_key = tmp_path / "wrong-key"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(wrong_key)], check=True, timeout=10)
    known.write_text("ava-service-fixture " + wrong_key.with_suffix(".pub").read_text())
    changed = known.read_bytes()
    controller._machines[identity].reconnect = False
    runtime.process.terminate()
    until(lambda: not controller.machines[1]["online"] and not controller.machines[1]["busy"], controller.machinesChanged)
    click(window, "selectMachine_" + identity)
    until(lambda: not controller.machines[1]["busy"] and bool(controller.machines[1]["error"]), controller.machinesChanged, timeout=20_000)
    assert "host key" in controller.machines[1]["error"].lower(), controller.machines[1]["error"]
    assert not controller.hostKeyRequest
    assert not controller.machines[1]["online"]
    assert known.read_bytes() == changed
    save_screenshot(window, "ssh-changed-key")


@pytest.mark.skipif(os.environ.get("AVA_DESKTOP_SSH_TESTS") != "1" or not os.environ.get("AVA_SSH_TEST_CONTAINER"), reason="Requires the isolated Fedora SSH fixture")
def test_desktop_remote_machine_defers_update_during_active_task(desktop, model_server, home):
    from ava.app.desktop.ssh import ssh_arguments

    ssh = ssh_arguments("ava-test")

    def remote(code, *arguments, data=None):
        result = subprocess.run([*ssh, "ava-test", shlex.join(["/opt/ava/bin/python", "-c", code, *arguments])],
                                input=data, capture_output=True, text=True, timeout=45)
        assert result.returncode == 0, result.stderr
        return result.stdout

    config = json.loads((home / "settings.json").read_text())
    config["providers"]["desktop-test"]["models"]["fixture-mcp-tools"] = {"context_window": 10000}
    local_port = config["providers"]["desktop-test"]["base_url"].split(":")[-1].split("/")[0]
    port = remote("import socket; s=socket.socket(); s.bind(('127.0.0.1',0)); print(s.getsockname()[1]); s.close()").strip()
    config["providers"]["desktop-test"]["base_url"] = f"http://127.0.0.1:{port}/v1"
    remote("from pathlib import Path; from ava.llm.credentials import save_api_key; import sys; p=Path.home(); (p/'upgrade project').mkdir(exist_ok=True); (p/'.ava').mkdir(exist_ok=True); (p/'.ava/settings.json').write_text(sys.stdin.read()); save_api_key('desktop-test','local-test-only')", data=json.dumps(config))
    model_tunnel = subprocess.Popen([*ssh, "-N", "-o", "ExitOnForwardFailure=yes", "-R", f"127.0.0.1:{port}:127.0.0.1:{local_port}", "ava-test"],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    controller, window = desktop
    try:
        # Start a real task in a distinct installation before the desktop connects.
        original = json.loads(remote("""
import json
from pathlib import Path
import httpx
from ava.app.backend import ensure_running
from ava.app.backend_service import install_service
from ava.base import ava_home
install_service(ava_home())
info = ensure_running(None)
with httpx.Client(base_url=f"http://127.0.0.1:{info['port']}", headers={'Authorization': 'Bearer '+info['token']}, trust_env=False) as client:
    project = client.post('/api/projects', json={'path':str(Path.home()/'upgrade project')}).json()
    chat = client.post('/api/chats', json={'project_id':project['id']}).json()
    response = client.post('/api/chats/'+chat['id']+'/messages', json={'text':'Keep this task running while the desktop connects'})
    response.raise_for_status()
    print(json.dumps({'instance_id': info['instance_id'], 'chat_id': chat['id']}))
"""))
        controller.start()
        until(lambda: bool(controller.projects) and len(model_server) == 1, controller.changed, window.frameSwapped)
        click(window, "machinesButton")
        find_item(window, "machineHostField").setProperty("text", "ava-test")
        find_item(window, "machineNameField").setProperty("text", "Fedora development")
        click(window, "addMachineAction")
        until(lambda: len(controller.machines) == 2 and (controller.machines[1]["online"] or controller.machines[1]["error"]), controller.machinesChanged, timeout=180_000)
        state = controller.machines[1]
        save_screenshot(window, "remote-update-pending")
        assert state["online"], state["error"]
        assert state["update_pending"]
        identity = state["id"]
        machine = controller._machines[identity]
        assert machine.runtime.info["instance_id"] == original["instance_id"]
        window.setProperty("dark", True)
        window.setWidth(800)
        window.setHeight(600)
        save_screenshot(window, "remote-update-pending-dark")
        click(window, "selectMachine_" + identity)
        until(lambda: controller.connected and controller.status == "running", controller.changed)
        chat_id = controller.chatId
        assert chat_id.endswith("~" + original["chat_id"])
        type_message(window, "Keep this draft through the update")
        inventory_code = "from pathlib import Path; import json; p=Path.home()/'.local/share/ava/backends'; print(json.dumps({str(f.relative_to(p)):f.stat().st_mtime_ns for f in p.glob('*/*') if f.name=='.ready' or f.suffix=='.whl'}))"
        inventory = remote(inventory_code)
        click(window, "machinesButton")
        click(window, "updateMachine_" + identity)
        until(lambda: machine.connection is None, controller.machinesChanged)
        until(lambda: controller.connected and machine.connection is not None, controller.changed, controller.machinesChanged, timeout=45_000)
        assert machine.runtime.info["instance_id"] == original["instance_id"]
        assert controller.machines[1]["update_pending"]
        assert controller.status == "running" and controller.chatId == chat_id
        assert remote(inventory_code) == inventory
        assert len(model_server) == 1
        QTest.keyClick(window, Qt.Key.Key_Escape)
        model_server[0].release.set()
        until(lambda: controller.status == "idle" and any(row["body"] == "Hello 世界" for row in controller._transcript.rows), controller.changed)
        click(window, "machinesButton")
        click(window, "updateMachine_" + identity)
        until(lambda: machine.connection is None, controller.machinesChanged)
        until(lambda: controller.connected and machine.connection is not None, controller.changed, controller.machinesChanged, timeout=70_000)
        assert not controller.machines[1]["update_pending"]
        upgraded_instance = machine.runtime.info["instance_id"]
        assert upgraded_instance != original["instance_id"]
        assert controller.chatId == chat_id and controller.draft == "Keep this draft through the update"
        # The stream's connection status precedes its history replay over SSH.
        until(lambda: any(row["body"] == "Hello 世界" for row in controller._transcript.rows), controller.changed)
        assert len(model_server) == 1 and remote(inventory_code) == inventory
        save_screenshot(window, "remote-update-complete")
        QTest.keyClick(window, Qt.Key.Key_Escape)
        # The upgraded backend projects its historical result into the cross-machine board.
        click(window, "sessionBoardButton")
        board = controller.board
        until(lambda: any(row["id"] == chat_id for row in board.needsReview.rows), board.changed)
        assert board.needsReview.rows[0]["machine"] == "Fedora development"
        save_screenshot(window, "remote-board-needs-review")
        click(window, "reviewBoardChat_" + chat_id)
        until(lambda: any(row["id"] == chat_id for row in board.reviewedSessions.rows), board.changed)
        click(window, "boardColumnTab_2")
        click(window, "openBoardChat_reviewed_" + chat_id)
        until(lambda: controller.connected and controller.chatId == chat_id, controller.changed)
        assert controller.draft == "Keep this draft through the update"
        controller._restart_machine(machine)
        until(lambda: machine.connection is None, controller.machinesChanged)
        until(lambda: controller.connected and machine.connection is not None, controller.changed, controller.machinesChanged, timeout=45_000)
        assert machine.runtime.info["instance_id"] == upgraded_instance
        assert not controller.machines[1]["update_pending"]
        assert len(model_server) == 1
        assert any(row["id"] == chat_id for row in board.reviewedSessions.rows)
        # Automations execute on the selected SSH machine and survive losing its GUI tunnel.
        click(window, "automationsButton")
        click(window, "newAutomationButton")
        assert find_item(window, "automationMachineChoice").property("currentValue") == identity
        find_item(window, "automationNameField").setProperty("text", "Fedora scheduled brief")
        find_item(window, "automationPromptField").setProperty("text", "Summarize this remote project.")
        click(window, "saveAutomationButton")
        automation = controller.automations
        until(lambda: bool(automation.rows.rows) or bool(automation.editorState["error"]), automation.changed, automation.editorChanged)
        assert not automation.editorState["error"]
        assert automation.rows.rows[0]["machine_id"] == identity
        click(window, "runAutomationNowButton")
        until(lambda: len(model_server) == 2 and bool(automation.detail["runs"]), automation.changed, window.frameSwapped)
        machine.runtime.process.terminate()
        until(lambda: machine.connection is None, controller.machinesChanged)
        model_server[1].release.set()
        until(lambda: machine.connection is not None and automation.detail["runs"][0]["status"] == "completed", automation.changed, controller.machinesChanged, timeout=45_000)
        assert automation.detail["remaining"] == 3
        remote_run = automation.detail["runs"][0]
        assert machine.connection.prefix and remote_run["chat_id"].startswith(machine.connection.prefix)
        assert remote("import sqlite3; from ava.base import ava_home; db=sqlite3.connect('file:'+str(ava_home()/'automations.sqlite3')+'?mode=ro',uri=True); print(db.execute('SELECT count(*) FROM tasks WHERE deleted=0').fetchone()[0]); db.close()").strip() == "1"
        save_screenshot(window, "remote-automation-complete")
        history = find_item(window, "automationRunHistory")
        history.setProperty("contentY", max(0, history.property("contentHeight") - history.height()))
        click(window, "openAutomationRun_" + remote_run["id"])
        until(lambda: controller.connected and controller.chatId == remote_run["chat_id"] and any(row["body"] == "Hello 世界" for row in controller._transcript.rows), controller.changed)
        assert controller.remoteMachine
        # Incompatible metadata must not become a successful deferred connection.
        click(window, "skillsButton")
        skills = controller.skillView
        assert skills.project == controller.projectId
        click(window, "newSkillButton")
        find_item(window, "skillNameField").setProperty("text", "remote-check")
        find_item(window, "skillDescriptionField").setProperty("text", "Check Fedora changes using the sapphire workflow.")
        find_item(window, "skillBodyField").setProperty("text", "# Remote checks\n\nInspect this machine's project.")
        click(window, "saveSkillButton")
        until(lambda: skills.detail.get("name") == "remote-check" or bool(skills.editorError), skills.changed)
        assert not skills.editorError
        assert skills.detail["path"].startswith("/home/ava-test/")
        assert not (home / "skills/remote-check").exists()
        click(window, "toggleSkillButton")
        until(lambda: skills.detail.get("state") == "disabled", skills.changed)
        machine.runtime.process.terminate()
        until(lambda: machine.connection is None, controller.machinesChanged)
        until(lambda: machine.connection is not None and controller.connected, controller.changed, controller.machinesChanged, timeout=45_000)
        click(window, "refreshSkillsButton")
        until(lambda: not skills.busy, skills.changed)
        assert skills.detail["state"] == "disabled"
        click(window, "toggleSkillButton")
        until(lambda: skills.detail.get("effective"), skills.changed)
        save_screenshot(window, "skills-fedora")
        click(window, "useSkillButton")
        type_message(window, "Check this workspace.", append=True)
        click(window, "sendButton")
        until(lambda: len(model_server) == 3, controller.changed)
        assert "sapphire workflow" in model_server[2].request["messages"][0]["content"]
        model_server[2].release.set()
        until(lambda: controller.status == "idle", controller.changed)
        # MCP programs and credentials belong to the upgraded execution machine.
        from ava.tool.mcp import MCPServers
        from tests.test_mcp import FIXTURE

        peer = json.loads(remote(r"""
import json, sys
from pathlib import Path
from ava.app.backend_state import read_endpoint
p = Path.home()
info = read_endpoint(p / '.ava')
python = Path(f"/proc/{info['pid']}/cmdline").read_bytes().split(b'\0')[0].decode()
script = p / 'MCP acceptance server.py'
script.write_text(sys.stdin.read())
print(json.dumps({'python': python, 'script': str(script)}))
""", data=FIXTURE.read_text()))
        previous_chat = controller.chatId
        controller.newChat()
        until(lambda: controller.connected and not controller.busy and controller.chatId != previous_chat, controller.changed)
        controller.selectModel("fixture-mcp-tools")
        until(lambda: controller.selection.get("model") == "fixture-mcp-tools", controller.changed)
        click(window, "mcpButton")
        click(window, "addMcpButton")
        find_item(window, "mcpNameField").setProperty("text", "Fedora workspace tools")
        find_item(window, "mcpCommandField").setProperty("text", shlex.join([peer["python"], peer["script"]]))
        click(window, "saveMcpButton")
        mcp = controller.mcpView
        until(lambda: mcp.detail.get("status") in {"connected", "error"} or bool(mcp.editorError), mcp.changed, timeout=25000)
        assert mcp.detail.get("status") == "connected", mcp.detail or mcp.editorError
        assert not MCPServers(home).configs()
        save_screenshot(window, "mcp-fedora")
        click(window, "closeMcpButton")
        type_message(window, "Record a remote change using the MCP server.")
        click(window, "sendButton")
        until(lambda: len(model_server) >= 4 or bool(controller.error), controller.changed, timeout=25000)
        assert not controller.error
        assert model_server[3].request["model"] == "fixture-mcp-tools"
        until(lambda: len(model_server) == 5, controller.changed, timeout=25000)
        proof = json.loads(remote("from pathlib import Path; print((Path.home()/'upgrade project/mcp-proof.json').read_text())"))
        assert proof["cwd"] == "/home/ava-test/upgrade project" and proof["change"]["approved"]
        assert any(message["role"] == "tool" and "Native MCP check" in message["content"] for message in model_server[4].request["messages"])
        model_server[4].release.set()
        until(lambda: controller.status == "idle", controller.changed)
        click(window, "mcpButton")
        until(lambda: mcp.toolRows.rowCount() == 3, mcp.changed)
        click(window, "toggleMcpButton")
        until(lambda: mcp.detail.get("status") == "disabled", mcp.changed)
        machine.runtime.process.terminate()
        until(lambda: machine.connection is None, controller.machinesChanged)
        until(lambda: machine.connection is not None and controller.connected, controller.changed, controller.machinesChanged, timeout=45000)
        click(window, "refreshMcpButton")
        until(lambda: not mcp.loading, mcp.changed)
        assert mcp.detail["status"] == "disabled"
        assert remote("import os,sys; print(os.path.exists('/proc/'+sys.argv[1]))", str(proof["pid"])).strip() == "False"
        click(window, "closeMcpButton")
        # Incompatible metadata must not become a successful deferred connection.
        try:
            remote("from ava.app.backend_state import read_endpoint,write_private_json; from ava.base import ava_home; p=ava_home(); info=read_endpoint(p); info['protocol']=999; write_private_json(p/'backend.json',info)")
            controller._restart_machine(machine)
            until(lambda: machine.connection is None and bool(machine.error), controller.machinesChanged, timeout=45_000)
            assert "incompatible with this desktop" in machine.error
            assert not machine.runtime.retryable
            assert find_item(window, "runPhase").property("text") == "Disconnected"
            assert find_item(window, "reconnectBackendButton").property("text") == "Reconnect"
            still_running = remote("from ava.app.backend_state import read_endpoint; from ava.base import ava_home; import httpx; info=read_endpoint(ava_home()); r=httpx.get(f\"http://127.0.0.1:{info['port']}/api/system\",headers={'Authorization':'Bearer '+info['token']},trust_env=False); r.raise_for_status(); print(r.json()['instance_id'])")
            assert still_running.strip() == upgraded_instance
            save_screenshot(window, "remote-update-incompatible")
        finally:
            remote("from ava.app.backend_state import read_endpoint,write_private_json,PROTOCOL_VERSION; from ava.base import ava_home; p=ava_home(); info=read_endpoint(p); info['protocol']=PROTOCOL_VERSION; write_private_json(p/'backend.json',info)")
        click(window, "reconnectBackendButton")
        until(lambda: controller.connected and machine.connection is not None, controller.changed, controller.machinesChanged, timeout=45_000)
        assert machine.runtime.info["instance_id"] == upgraded_instance
        # A reconnect must also respect turning background startup off in Settings.
        click(window, "settingsButton")
        until(lambda: not controller.serviceState["loading"], controller.serviceStateChanged)
        assert controller.serviceState["installed"]
        scroll = find_item(window, "settingsGeneralScroll")
        scroll.setProperty("contentY", max(0, scroll.property("contentHeight") - scroll.height()))
        click(window, "backgroundStartupButton")
        until(lambda: not controller.serviceState["busy"] and not controller.serviceState["installed"], controller.serviceStateChanged, timeout=45_000)
        until(lambda: controller.connected and machine.connection is not None, controller.changed, controller.machinesChanged, timeout=45_000)
        assert remote("from ava.app.backend_service import service_status; from ava.base import ava_home; print(service_status(ava_home())['installed'])").strip() == "False"
        save_screenshot(window, "remote-update-startup-disabled")
    finally:
        try:
            controller.shutdown()
            until(lambda: controller._closed_emitted, controller.closed, timeout=15_000)
            remote("from ava.app.backend_service import uninstall_service; from ava.base import ava_home; uninstall_service(ava_home(),force=True)")
        finally:
            model_tunnel.terminate()
            model_tunnel.wait(timeout=5)
            if model_tunnel.stderr:
                model_tunnel.stderr.close()


@pytest.mark.skipif(os.environ.get("AVA_DESKTOP_SSH_TESTS") != "1" or not os.environ.get("AVA_SSH_CONFIG"), reason="Requires the isolated Fedora SSH fixture")
def test_desktop_remote_machine_sessions_are_isolated_and_reconnect(desktop, model_server, home):
    import base64

    from ava.app.desktop.ssh import ssh_arguments

    ssh = ssh_arguments("ava-test")

    def remote(code, *arguments, data=None):
        result = subprocess.run([*ssh, "ava-test", shlex.join(["/opt/ava/bin/python", "-c", code, *arguments])],
                                input=data, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        return result.stdout

    config = json.loads((home / "settings.json").read_text())
    config["model"] = "fixture-remote-tools"
    config["providers"]["desktop-test"]["models"]["fixture-remote-tools"] = {"context_window": 10000}
    local_port = config["providers"]["desktop-test"]["base_url"].split(":")[-1].split("/")[0]
    port = remote("import socket; s=socket.socket(); s.bind(('127.0.0.1',0)); print(s.getsockname()[1]); s.close()").strip()
    config["providers"]["desktop-test"]["base_url"] = f"http://127.0.0.1:{port}/v1"
    remote("from pathlib import Path; from ava.llm.credentials import save_api_key; import sys; p=Path.home(); (p/'remote 项目').mkdir(exist_ok=True); (p/'.ava').mkdir(exist_ok=True); (p/'.ava/settings.json').write_text(sys.stdin.read()); save_api_key('desktop-test','local-test-only')", data=json.dumps(config))
    remote("from pathlib import Path; import base64,sys; (Path.home()/'remote 项目/报告.pdf').write_bytes(base64.b64decode(sys.stdin.read()))", data=base64.b64encode((Path(__file__).parent/'fixtures/preview.pdf').read_bytes()).decode())
    remote("from pathlib import Path; import subprocess; p=Path.home()/'remote 项目'; (p/'.gitignore').write_text('remote-test-proof.txt\\nterminal*\\n*.pid\\n'); (p/'计算.py').write_text('answer = 1\\n'); run=lambda *a:subprocess.run(['git',*a],cwd=p,check=True,capture_output=True); run('init','-q','-b','main'); run('config','user.name','Ava remote fixture'); run('config','user.email','ava@example.invalid'); run('config','commit.gpgsign','false'); run('add','.'); run('commit','-qm','Initial fixture'); (p/'计算.py').write_text('answer = 42\\n'); (p/'[draft].md').write_text('# Remote draft\\n')")
    model_tunnel = subprocess.Popen([*ssh, "-N", "-o", "ExitOnForwardFailure=yes", "-R", f"127.0.0.1:{port}:127.0.0.1:{local_port}", "ava-test"],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    controller, window = desktop
    try:
        controller.start()
        until(lambda: bool(controller.projects), controller.changed)
        click(window, "newChatButton")
        until(lambda: controller.connected, controller.changed)
        local_chat = controller.chatId
        type_message(window, "Local draft stays here")
        click(window, "machinesButton")
        find_item(window, "machineHostField").setProperty("text", "ava-test")
        find_item(window, "machineNameField").setProperty("text", "Fedora 测试")
        pulses = [time.perf_counter()]
        heartbeat = QTimer()
        heartbeat.setInterval(10)
        heartbeat.timeout.connect(lambda: pulses.append(time.perf_counter()))
        heartbeat.start()
        click(window, "addMachineAction")
        until(lambda: len(controller.machines) == 2 and (controller.machines[1]["online"] or controller.machines[1]["error"]), controller.machinesChanged, timeout=180_000)
        heartbeat.stop()
        longest_pause = max((b - a) * 1000 for a, b in zip(pulses, pulses[1:], strict=False))
        assert longest_pause < 150, longest_pause
        print("SSH_INSTALL_GUI_MAX_PAUSE_MS", round(longest_pause))
        machine = controller.machines[1]
        assert machine["online"], machine["error"]
        identity = machine["id"]
        click(window, "selectMachine_" + identity)
        assert controller.remoteMachine
        click(window, "addProject_" + identity)
        find_item(window, "remoteProjectPath").setProperty("text", "~/remote 项目")
        click(window, "addRemoteProjectAction")
        until(lambda: "remote 项目" in controller.projectPath or bool(controller.error), controller.changed)
        assert "remote 项目" in controller.projectPath, controller.error
        assert len(controller.projects) == 2
        click(window, "newChatButton")
        until(lambda: controller.connected, controller.changed)
        remote_chat = controller.chatId
        assert remote_chat.endswith("~" + local_chat), (local_chat, remote_chat)
        assert controller.draft == ""
        type_message(window, "Remote work continues while I use the local project")
        QTest.keyClick(window, Qt.Key.Key_Return)
        until(lambda: any(row["body"] == "Hello " for row in controller._transcript.rows) or bool(controller.error), controller.changed)
        assert controller.status == "running", controller.error
        assert remote("from pathlib import Path; print((Path.home()/'remote 项目/remote-test-proof.txt').read_text())").strip() == "remote-fixture"
        assert not (controller._cwd / "remote-test-proof.txt").exists()
        controller.browseFiles("")
        until(lambda: bool(find_item(window, "file_remote-test-proof.txt")), window.frameSwapped)
        click(window, "file_remote-test-proof.txt")
        file_pane = find_item(window, "filePane")
        file_model = file_pane.property("treeModel")
        until(lambda: file_pane.property("fileState").get("text") == "remote-fixture", file_model.previewReady)
        click(window, "attachPreviewButton")
        assert controller.attachments[-1]["name"] == "remote-test-proof.txt"
        assert base64.b64decode(controller.attachments[-1]["data_base64"]) == b"remote-fixture"
        controller.removeAttachment(controller.attachments[-1]["id"])
        click(window, "file_报告.pdf")
        until(lambda: bool(find_item(window, "pdfView")), window.frameSwapped)
        assert find_item(window, "pdfPreview").property("pageCount") == 3
        until(lambda: find_item(window, "pdfView").property("currentPageRenderingStatus") == 1, window.frameSwapped)
        save_screenshot(window, "ssh-remote-pdf")
        click(window, "closeInspectorButton")
        click(window, "reviewChangesButton")
        until(lambda: bool(find_item(window, "reviewPane")), window.frameSwapped)
        review = find_item(window, "reviewPane").property("review")
        until(lambda: len(review.files) == 2 or bool(review.state["error"]), review.changed, review.filesChanged)
        assert not review.state["error"], review.state
        click(window, "change_worktree:计算.py")
        until(lambda: "+answer = 42" in review.state["diff"], review.changed)
        click(window, "stageFileButton")
        until(lambda: any(row["id"] == "staged:计算.py" for row in review.files), review.filesChanged)
        click(window, "change_staged:计算.py")
        click(window, "unstageFileButton")
        until(lambda: any(row["id"] == "worktree:计算.py" for row in review.files), review.filesChanged)
        for name in ("[draft].md", "计算.py"):
            click(window, "change_worktree:" + name)
            click(window, "stageFileButton")
            until(lambda name=name: any(row["id"] == "staged:" + name for row in review.files), review.filesChanged)
        hook = "from pathlib import Path; p=Path.home()/'remote 项目/.git/hooks/pre-commit'; p.write_text('#!/bin/sh\\necho Remote-hook-failure >&2\\nexit 1\\n'); p.chmod(0o755)"
        remote(hook)
        click(window, "commitChangesButton")
        message = "Remote 中文 'quotes' $(touch injected.txt)\n\nLiteral body"
        find_item(window, "commitMessageField").setProperty("text", message)
        click(window, "confirmCommitButton")
        until(lambda: not review.state["busy"] and bool(review.state["error"]), review.changed)
        assert "Remote-hook-failure" in review.state["error"]
        assert find_item(window, "commitMessageField").property("text") == message
        remote("from pathlib import Path; (Path.home()/'remote 项目/.git/hooks/pre-commit').unlink()")
        click(window, "confirmCommitButton")
        until(lambda: not review.files and not review.state["busy"], review.changed)
        committed = remote("from pathlib import Path; import subprocess; p=Path.home()/'remote 项目'; print(subprocess.check_output(['git','log','-1','--format=%B'],cwd=p,text=True)); assert not (p/'injected.txt').exists()")
        assert committed.strip() == message
        assert not (controller._cwd / ".git").exists()
        save_screenshot(window, "remote-git-committed")
        click(window, "closeInspectorButton")

        click(window, "toggleTerminalButton")
        until(lambda: bool(find_item(window, "terminalPane")), window.frameSwapped)
        terminal_pane = find_item(window, "terminalPane")
        terminal = terminal_pane.property("session")
        until(lambda: terminal.ready, terminal.changed)
        output = bytearray()
        terminal.outputReceived.connect(lambda encoded, _: output.extend(base64.b64decode(encoded)))

        def terminal_command(command, marker):
            QGuiApplication.clipboard().setText(command)
            click(window, "terminalWeb")
            terminal_evaluate(terminal_pane, "window.avaTerminal.paste()")
            QTest.keyClick(window, Qt.Key.Key_Return)
            try:
                until(lambda: marker in output, terminal.outputReceived, terminal.changed, timeout=15_000)
            except AssertionError:
                save_screenshot(window, "remote-terminal-failure")
                pytest.fail(repr({"output": output.decode("utf-8", errors="replace")[-10000:], "status": terminal.status, "error": terminal._error}))

        terminal_command("pwd > terminal.cwd; printf '\\033[32mREMOTE_READY\\033[0m\\n'", b"\x1b[32mREMOTE_READY\x1b[0m")
        assert remote("from pathlib import Path; print((Path.home()/'remote 项目/terminal.cwd').read_text())").strip() == controller.projectPath
        assert not (controller._cwd / "terminal.cwd").exists()
        size_before = (terminal._columns, terminal._rows)
        window.setWidth(1450)
        until(lambda: (terminal._columns, terminal._rows) != size_before, window.frameSwapped)
        terminal_command("stty size > terminal.size; printf '\\033[32mRESIZED\\033[0m\\n'", b"\x1b[32mRESIZED\x1b[0m")
        remote_size = [int(n) for n in remote("from pathlib import Path; print((Path.home()/'remote 项目/terminal.size').read_text())").split()]
        assert remote_size == list(reversed(terminal_evaluate(terminal_pane, "window.avaTerminal.size()")))
        window.setWidth(1280)
        produced = "import sys; sys.stdout.write(('REMOTE_LINE 中文 '+ 'x'*80+'\\n')*40000); print('REMOTE_OUTPUT_FINISHED')"
        pulses = [time.perf_counter()]
        heartbeat.start()
        started = time.perf_counter()
        terminal_command(shlex.join([controller.runtime.info["python"], "-c", produced]), b"\r\nREMOTE_OUTPUT_FINISHED\r\n")
        until(lambda: terminal._inflight == 0, window.frameSwapped)
        assert "REMOTE_OUTPUT_FINISHED" in terminal_evaluate(terminal_pane, "window.avaTerminal.text()")
        heartbeat.stop()
        duration = (time.perf_counter() - started) * 1000
        max_pause = max((b-a)*1000 for a,b in zip(pulses,pulses[1:],strict=False))
        assert duration < 8000 and max_pause < 150, (duration, max_pause)
        print("REMOTE_TERMINAL_BENCHMARK", json.dumps({"output_bytes": len(output), "duration_ms": round(duration), "max_gui_ms": round(max_pause)}))
        save_screenshot(window, "remote-terminal-output")
        foreground = "import time; print('\\x1b[32mFOREGROUND_READY\\x1b[0m',flush=True); time.sleep(60)"
        terminal_command(shlex.join([controller.runtime.info["python"], "-c", foreground]), b"\x1b[32mFOREGROUND_READY\x1b[0m")
        QTest.keyClick(window, Qt.Key.Key_C, Qt.KeyboardModifier.MetaModifier if QGuiApplication.platformName() == "cocoa" else Qt.KeyboardModifier.ControlModifier)
        terminal_command("printf '\\033[32mINTERRUPTED\\033[0m\\n'", b"\x1b[32mINTERRUPTED\x1b[0m")
        # Existing terminals keep their owner when another machine is selected.
        controller.selectMachine("local")
        assert terminal.remote and terminal.project_id != controller.projectId
        controller.selectMachine(identity)
        assert find_item(window, "terminalPane").property("session") is terminal
        terminal_command("sleep 60 & echo $! > terminal-child.pid; printf '\\033[32mCHILD_READY\\033[0m\\n'", b"\x1b[32mCHILD_READY\x1b[0m")
        child_pid = int(remote("from pathlib import Path; print((Path.home()/'remote 项目/terminal-child.pid').read_text())").strip())
        terminal._process.terminate()  # Drop this terminal's SSH channel, keeping the chat tunnel.
        until(lambda: terminal.canReconnect, terminal.changed)
        remote("import psutil,sys,time; pid=int(sys.argv[1]); deadline=time.monotonic()+4; exec('while psutil.pid_exists(pid) and psutil.Process(pid).status()!=psutil.STATUS_ZOMBIE and time.monotonic()<deadline: time.sleep(.05)'); assert not psutil.pid_exists(pid) or psutil.Process(pid).status()==psutil.STATUS_ZOMBIE", str(child_pid))
        click(window, "reconnectTerminalButton")
        until(lambda: terminal.ready and not terminal.canReconnect, terminal.changed)
        terminal_command("printf '\\033[32mRECONNECTED\\033[0m\\n'", b"\x1b[32mRECONNECTED\x1b[0m")
        assert controller.status == "running"
        until(lambda: terminal._inflight == 0, window.frameSwapped)
        fresh_screen = terminal_evaluate(terminal_pane, "window.avaTerminal.text()")
        assert "RECONNECTED" in fresh_screen and "REMOTE_LINE" not in fresh_screen
        save_screenshot(window, "remote-terminal-reconnected")
        click(window, "closeTerminalTab_0")
        until(lambda: terminal not in controller._terminals, terminal.closed)
        click(window, "sessionMenu_" + remote_chat)
        click(window, "pinChatAction")
        assert controller.sessionRows[1]["id"] == remote_chat
        click(window, "machineGroup_" + identity)
        assert find_item(window, "session_" + remote_chat).isVisible()
        click(window, "session_" + local_chat)
        until(lambda: controller.connected and controller.chatId == local_chat, controller.changed)
        assert controller.draft == "Local draft stays here"
        assert not controller.remoteMachine
        assert not any("Remote work" in row["body"] for row in controller._transcript.rows)
        remote_machine = controller._machines[identity]
        instance = remote_machine.runtime.info["instance_id"]
        remote_machine.runtime.process.terminate()  # Lose only the owned SSH helper/tunnel.
        until(lambda: remote_machine.connection is None, controller.machinesChanged)
        until(lambda: remote_machine.connection is not None, controller.machinesChanged, timeout=40_000)
        assert remote_machine.runtime.info["instance_id"] == instance
        model_server[-1].release.set()
        click(window, "session_" + remote_chat)
        try:
            until(lambda: controller.connected and controller.status == "idle" and any(row["body"] == "Hello 世界" for row in controller._transcript.rows), controller.changed)
        except AssertionError:
            save_screenshot(window, "machines-reconnect-failure")
            pytest.fail(json.dumps({"chat": controller.chatId, "project": controller.projectId, "status": controller.status,
                                    "connected": controller.connected, "error": controller.error, "rows": controller._transcript.rows,
                                    "machines": controller.machines}, ensure_ascii=False))
        assert len(model_server) == 2
        remote_project_id = controller.projectId
        click(window, "newChatOptionsButton")
        click(window, "newWorktreeAction")
        find_item(window, "worktreeBranchField").setProperty("text", "ava/remote-worktree")
        click(window, "createWorktreeChatButton")
        until(lambda: controller.connected and controller.chatId != remote_chat, controller.changed)
        worktree_chat = controller.chatId
        worktree_root = controller.workspacePath
        assert controller.remoteMachine and controller.projectId == remote_project_id
        assert worktree_root != controller.projectPath and len(controller.projects) == 2
        assert not Path(worktree_root).exists()
        type_message(window, "Work inside the Fedora worktree")
        QTest.keyClick(window, Qt.Key.Key_Return)
        until(lambda: any(row["body"] == "Hello " for row in controller._transcript.rows), controller.changed)
        model_server[-1].release.set()
        until(lambda: controller.status == "idle" and any(row["body"] == "Hello 世界" for row in controller._transcript.rows), controller.changed)
        assert remote("from pathlib import Path; import sys; print((Path(sys.argv[1])/'remote-test-proof.txt').read_text())", worktree_root).strip() == "remote-fixture"
        controller.browseFiles("remote-test-proof.txt")
        until(lambda: (pane := find_item(window, "filePane")) is not None and pane.property("rootPath") == worktree_root and pane.property("fileState").get("text") == "remote-fixture", window.frameSwapped)
        click(window, "closeInspectorButton")
        click(window, "reviewChangesButton")
        until(lambda: (pane := find_item(window, "reviewPane")) is not None and pane.property("review") is not None and pane.property("review").state.get("branch") == "ava/remote-worktree", window.frameSwapped)
        click(window, "closeInspectorButton")
        click(window, "toggleTerminalButton")
        until(lambda: (pane := find_item(window, "terminalPane")) is not None and pane.property("session") is not None and pane.property("session").ready, window.frameSwapped)
        worktree_terminal = find_item(window, "terminalPane").property("session")
        assert worktree_terminal.root == worktree_root
        worktree_output = bytearray()
        def worktree_received(encoded, count):
            worktree_output.extend(base64.b64decode(encoded))
        worktree_terminal.outputReceived.connect(worktree_received)
        worktree_terminal.write("pwd; pwd > terminal.cwd\n")
        until(lambda: worktree_root.encode() in worktree_output and worktree_terminal._inflight == 0,
              worktree_terminal.outputReceived, window.frameSwapped)
        worktree_terminal.outputReceived.disconnect(worktree_received)
        # SSH output is asynchronous; wait remotely for the one shell command.
        remote("from pathlib import Path; import sys,time; p=Path(sys.argv[1])/'terminal.cwd'; end=time.monotonic()+3; exec('while not p.exists() and time.monotonic()<end: time.sleep(.02)'); assert p.read_text().strip()==sys.argv[1]", worktree_root)
        worktree_pane = find_item(window, "terminalPane")
        assert worktree_root in terminal_evaluate(worktree_pane, "window.avaTerminal.text()")
        save_screenshot(window, "remote-worktree")
        click(window, "closeTerminalTab_1")
        controller.openChat(remote_chat)
        until(lambda: controller.connected and controller.chatId == remote_chat, controller.changed)
        assert any(c["id"] == worktree_chat for p in controller.projects for c in p["chats"])
        window.setProperty("dark", True)
        window.setWidth(800)
        save_screenshot(window, "machines-remote-pinned")
        click(window, "machinesButton")
        save_screenshot(window, "machines-remote-connected")
        QTest.keyClick(window, Qt.Key.Key_Escape)
        window.close()
        until(lambda: controller._closed_emitted, controller.closed)
        restored = Controller(controller._cwd, [], QSettings(controller.settings.fileName(), QSettings.Format.IniFormat))
        restored_engine = create_engine(restored)
        restored_window = restored_engine.rootObjects()[0]
        assert isinstance(restored_window, QQuickWindow)
        try:
            restored.start()
            until(lambda: restored.connected and restored.chatId == remote_chat, restored.changed, timeout=40_000)
            assert restored.remoteMachine
            assert restored.property("sessionRows")[1]["id"] == remote_chat
            assert restored.runtime.info["instance_id"] == instance
            save_screenshot(restored_window, "machines-restored")
            click(restored_window, "machinesButton")
            click(restored_window, "removeMachine_" + identity)
            until(lambda: len(restored.property("machines")) == 1, restored.machinesChanged)
            assert not restored.remoteMachine and len(restored.property("projects")) == 1
        finally:
            restored_window.close()
            until(lambda: restored._closed_emitted, restored.closed)
            restored_engine.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        still_running = remote("from ava.app.backend import connect; from ava.base import ava_home; import json; print(json.dumps({'instance':connect(ava_home())['instance_id']}))")
        assert json.loads(still_running)["instance"] == instance
    finally:
        model_tunnel.terminate()
        model_tunnel.wait(timeout=5)
        assert model_tunnel.stderr is not None
        model_tunnel.stderr.close()


def test_desktop_close_keeps_agent_running_and_reopens_session(desktop, model_server, project):
    controller, window = desktop
    controller.start()
    until(lambda: bool(controller.projects), controller.changed)
    click(window, "newChatButton")
    until(lambda: controller.connected, controller.changed)
    identity = controller.chatId
    type_message(window, "Finish this task after I close the desktop.")
    QTest.keyClick(window, Qt.Key.Key_Return)
    until(lambda: any(row["body"] == "Hello " for row in controller._transcript.rows), controller.changed)
    connection = controller._connection
    window.close()
    until(lambda: controller._closed_emitted, controller.closed)
    with httpx.Client(
        base_url=connection._base,
        headers={"Authorization": "Bearer " + connection._token},
        timeout=3,
    ) as observer:
        response = observer.get("/api/chats/" + identity)
        assert response.status_code == 200 and response.json()["status"] == "running"
        assert observer.post("/api/system/shutdown", json={}).status_code == 409
        model_server[0].release.set()

    reopened = Controller(
        project, ["--model", "fixture-reasoning", "--effort", "high"],
        QSettings(controller.settings.fileName(), QSettings.Format.IniFormat)
    )
    engine = create_engine(reopened)
    restored_window = engine.rootObjects()[0]
    assert isinstance(restored_window, QQuickWindow)
    try:
        reopened.start()
        until(
            lambda: reopened.connected and reopened.chatId == identity and reopened.status == "idle"
            and any(row["body"] == "Hello 世界" for row in reopened._transcript.rows),
            reopened.changed,
        )
        assert [row["body"] for row in reopened._transcript.rows] == [
            "Finish this task after I close the desktop.", "Hello 世界"
        ]
        assert len(model_server) == 1
        save_screenshot(restored_window, "persistent-session")
        # Client launch flags apply to new chats without reconfiguring a shared daemon.
        click(restored_window, "newChatButton")
        until(
            lambda: reopened.connected and reopened.chatId != identity
            and reopened.property("selection").get("model") == "fixture-reasoning",
            reopened.changed,
        )
        assert reopened.property("selection")["model"] == "fixture-reasoning"
        assert reopened.property("selection")["effort"] == "high"
        with httpx.Client(
            base_url=connection._base,
            headers={"Authorization": "Bearer " + connection._token},
        ) as observer:
            normal = observer.post("/api/chats", json={"project_id": reopened.projectId}).json()
            assert observer.get("/api/chats/" + normal["id"] + "/models").json()["model"] == "fixture"
    finally:
        reopened.shutdown()
        until(lambda: reopened._closed_emitted, reopened.closed)
        restored_window.hide()
        engine.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_desktop_send_stream_reconnect_switch_cancel_and_restore(
    desktop, model_server, home, project
):
    controller, window = desktop
    controller.start()
    until(lambda: bool(controller.projects), controller.changed)
    assert not controller.error
    click(window, "newChatButton")
    until(lambda: bool(controller.connected) or bool(controller.error), controller.changed)
    assert not controller.error
    identity = controller.chatId
    type_message(window, "你好 Ava")
    QTest.keyClick(window, Qt.Key.Key_Return)
    until(
        lambda: any(r["body"] == "Hello " for r in controller._transcript.rows), controller.changed
    )
    assert [r["kind"] for r in controller._transcript.rows] == ["user", "assistant"]
    assert controller._transcript.rows[0]["body"] == "你好 Ava"
    until(lambda: not controller.busy, controller.changed)
    assert find_item(window, "composer").property("text") == ""

    # Reconnect in the middle of a stream using Last-Event-ID, preserving the partial reply.
    connection = controller._connection
    connection.close_stream()
    controller._reconnect()
    model_server[0].release.set()
    until(
        lambda: (
            controller.status == "idle" and controller._transcript.rows[-1]["body"] == "Hello 世界"
        ),
        controller.changed,
    )
    assert len(controller._transcript.rows) == 2

    until(lambda: controller.chats[0]["title"] == "你好 Ava", controller.changed)

    # Actual renderer output, retained only when explicitly requested for local inspection.
    window.update()
    until(lambda: not window.grabWindow().isNull(), window.frameSwapped)
    output = os.environ.get("AVA_DESKTOP_SCREENSHOT")
    if output:
        assert window.grabWindow().save(output)

    # Drafts belong to the conversation; another conversation never receives its events.
    type_message(window, "unsent draft")
    click(window, "newChatButton")
    until(lambda: controller.chatId != identity and controller.connected, controller.changed)
    second = controller.chatId
    assert not controller._transcript.rows
    assert find_item(window, "composer").property("text") == ""
    controller.openChat(identity)
    until(
        lambda: controller.chatId == identity and len(controller._transcript.rows) == 2,
        controller.changed,
    )
    assert find_item(window, "composer").property("text") == "unsent draft"
    controller._set_draft("")
    type_message(window, "cancel this run")
    click(window, "sendButton")
    until(
        lambda: (
            len(controller._transcript.rows) == 4
            and controller.status == "running"
            and not controller.busy
        ),
        controller.changed,
    )
    click(window, "stopButton")
    until(
        lambda: (
            controller.status == "idle"
            and any(r["heading"] == "Stopped" for r in controller._transcript.rows)
        ),
        controller.changed,
    )
    model_server[-1].release.set()
    expected = list(controller._transcript.rows)

    # The per-launch token protects even loopback clients, and the origin fence still applies.
    with httpx.Client(base_url=connection._base) as client:
        assert client.get("/api/projects").status_code == 401
        assert (
            client.get("/api/projects", headers={b"Authorization": b"Bearer \xff"}).status_code
            == 401
        )
        assert client.get(f"/api/chats/{identity}/events").status_code == 401
        headers = {"Authorization": f"Bearer {connection._token}"}
        assert client.get("/api/projects", headers=headers).status_code == 200
        assert (
            client.get(
                "/api/projects", headers={**headers, "Origin": "https://foreign.test"}
            ).status_code
            == 403
        )

    controller._detach()
    controller.runtime.stop()
    stop_backend(home, force=True)
    until(
        lambda: controller.runtime.process.state() == QProcess.ProcessState.NotRunning,
        controller.runtime.stopped,
    )
    old_port = connection._base
    with pytest.raises(httpx.ConnectError):
        httpx.get(old_port + "/api/projects")
    controller.start()
    until(
        lambda: controller.connected and controller._transcript.rows == expected, controller.changed
    )
    assert controller.chatId == identity
    assert not any(c["id"] == second for c in controller.chats)
    assert controller._connection._token != connection._token
    window.close()
    until(
        lambda: controller.runtime.process.state() == QProcess.ProcessState.NotRunning,
        controller.runtime.stopped,
    )


def test_project_switch_and_ime_preedit(desktop, model_server, project, tmp_path):
    controller, window = desktop
    controller.start()
    until(lambda: bool(controller.projects), controller.changed)
    click(window, "newChatButton")
    until(lambda: bool(controller.connected), controller.changed)
    first_project = controller.projectId
    composer = find_item(window, "composer")
    composer.forceActiveFocus()
    preedit = QInputMethodEvent("nihao", [])
    QCoreApplication.sendEvent(composer, preedit)
    assert composer.property("inputMethodComposing")
    QTest.keyClick(window, Qt.Key.Key_Return)
    assert not controller.busy
    assert not model_server
    commit = QInputMethodEvent()
    commit.setCommitString("你好")
    QCoreApplication.sendEvent(composer, commit)
    assert composer.property("text") == "你好"
    QTest.keyClick(window, Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier)
    assert composer.property("text") == "你好\n"
    assert not controller.busy
    other = tmp_path / "另一个项目"
    other.mkdir()
    controller.addProject(other.as_uri())
    until(lambda: controller.projectId != first_project, controller.changed)
    assert controller.projectPath == str(other)
    assert not controller.chatId
    controller.selectProject(first_project)
    until(lambda: bool(controller.connected), controller.changed)
    assert composer.property("text") == "你好\n"
    other_id = next(p["id"] for p in controller.projects if p["path"] == str(other))
    original_count = len(controller.chats)
    controller.newChat()
    controller.selectProject(other_id)
    until(
        lambda: (
            len(next(p for p in controller.projects if p["id"] == first_project)["chats"])
            == original_count + 1
        ),
        controller.navigationChanged,
    )
    assert controller.projectId == other_id and not controller.chatId


def test_desktop_context_chart_matches_report(desktop, model_server, project):
    controller, window = desktop
    controller.start()
    until(lambda: bool(controller.projects), controller.changed)
    click(window, "newChatButton")
    until(lambda: bool(controller.connected), controller.changed)
    type_message(window, "/context")
    QTest.keyClick(window, Qt.Key.Key_Return)
    dialog = window.findChild(QObject, "contextDialog")
    until(lambda: dialog.property("visible"), controller.changed)
    connection = controller._connection
    report = httpx.get(
        connection._base + f"/api/chats/{controller.chatId}/context",
        headers={"Authorization": f"Bearer {connection._token}"},
    ).json()
    shown = dialog.property("report")
    assert (shown.toVariant() if isinstance(shown, QJSValue) else shown) == report
    assert report["estimated_tokens"] == sum(s["tokens"] for s in report["sections"])
    assert not model_server, "Inspecting context must not call the model"

    def verify_graph(payload):
        until(
            lambda: find_item(window, "contextWindowTrack").width() > 0,
            window.frameSwapped,
        )
        presented = QSignalSpy(window.frameSwapped)
        window.update()
        assert presented.count() or presented.wait(3000), "Context layout was not presented"
        total = payload["estimated_tokens"]
        capacity = payload["context_window"]
        track = find_item(window, "contextWindowTrack")
        fill = find_item(window, "contextWindowFill")
        expected = min(1, total / capacity) if capacity else 0
        assert fill.width() == pytest.approx(track.width() * expected, abs=0.01)
        for section in payload["sections"]:
            kind = section["kind"]
            track = find_item(window, "contextTrack_" + kind)
            fill = find_item(window, "contextFill_" + kind)
            share = section["tokens"] / total if total else 0
            assert fill.width() == pytest.approx(track.width() * share, abs=0.01)
            label = find_item(window, "contextShare_" + kind).property("text")
            assert label.endswith("%")
            if label.startswith("<"):
                assert 0 < share * 100 < 0.1
            elif label.startswith(">"):
                assert 99.9 < share * 100 < 100
            else:
                rounded, valid = QLocale().toDouble(label[:-1])
                assert valid and rounded == pytest.approx(share * 100, abs=0.051)
        ordered = sorted(payload["sections"], key=lambda s: -s["tokens"])
        positions = [
            find_item(window, "contextLabel_" + s["kind"]).mapToScene(QPointF()).y()
            for s in ordered
        ]
        assert positions == sorted(positions), "Largest context categories must come first"
        assert "estimated tokens" in find_item(window, "contextShareNote").property("text")
        # Numeric byte sizes must not leak back into this user-facing report.
        pending = [find_item(window, "contextScroll")]
        while pending:
            item = pending.pop()
            text = item.property("text")
            if text and item.isVisible():
                assert "bytes" not in text.lower()
                assert "NaN" not in text and "Infinity" not in text
            pending.extend(item.childItems())

    verify_graph(report)
    save_screenshot(window, "context")
    click(window, "contextDoneButton")
    assert not dialog.property("visible")
    assert find_item(window, "composer").hasActiveFocus()

    # Use real request content to verify that attachments and a subsequent response
    # update the next-request estimate, without mixing it with provider usage.
    attachment = project / "context.md"
    attachment.write_text("# Context\n\n解释上下文统计。" * 50)
    controller.addAttachments([str(attachment)])
    type_message(window, "解释上下文统计。" * 30)
    click(window, "sendButton")
    until(lambda: bool(model_server), controller.changed)
    model_server[0].release.set()
    until(lambda: controller.status == "idle", controller.changed)
    type_message(window, "/context")
    QTest.keyClick(window, Qt.Key.Key_Return)
    until(lambda: dialog.property("visible"), controller.changed)
    updated = dialog.property("report")
    if isinstance(updated, QJSValue):
        updated = updated.toVariant()
    assert updated["estimated_tokens"] > report["estimated_tokens"]
    assert {"user_text", "assistant_text", "attachment_files"} <= {
        s["kind"] for s in updated["sections"]
    }
    verify_graph(updated)

    # Controlled report edge cases supplement the real backend flow: all category
    # rows, tiny/zero shares, Chinese wrapping, unknown capacity and overflow.
    from ava.session.context_report import SECTION_LABELS

    amounts = [0, 1, 99, 100, 700, 1500, 2500, 4500, 5500, 10000, 10000, 10000, 25000, 30000]
    sections = [
        {"kind": kind, "label": label, "tokens": value, "bytes": value * 4, "count": 1}
        for (kind, label), value in zip(SECTION_LABELS.items(), amounts, strict=True)
    ]
    sections[-1]["label"] = "消息格式开销 · 包含消息角色、边界与模型所需的格式信息，以及工具调用的分隔标识"
    mixed = {
        "sections": sections,
        "estimated_tokens": sum(amounts),
        "context_window": 128000,
        "measured_input_tokens": 75000,
        "compacted": True,
    }
    controller.contextRequested.emit(mixed)
    window.setWidth(800)
    window.setHeight(600)
    window.setProperty("dark", True)
    verify_graph(mixed)
    assert find_item(window, "contextShare_environment").property("text") == "<0.1%"
    assert find_item(window, "contextShare_system").property("text") == "0%"
    assert find_item(window, "contextLabel_framing").height() > 20, "Chinese label must wrap"
    assert dialog.property("height") <= window.height() - 64
    save_screenshot(window, "context-dark-narrow")
    scroll = find_item(window, "contextScroll")
    position = visible_rect(window, scroll).center().toPoint()
    QTest.wheelEvent(window, position, QPoint(0, -2400))
    until(
        lambda: not visible_rect(window, find_item(window, "contextMeasured")).isEmpty(),
        window.frameSwapped,
    )
    viewport = scroll.property("contentItem")
    until(lambda: not viewport.property("moving"), window.frameSwapped)
    assert viewport.property("contentY") <= max(
        0, viewport.property("contentHeight") - viewport.height()
    ) + 1
    save_screenshot(window, "context-scroll")
    click(window, "closeContextButton")
    assert not dialog.property("visible")
    controller.contextRequested.emit(mixed)
    QTest.qWait(30)
    assert not visible_rect(window, find_item(window, "contextTotal")).isEmpty()

    for capacity, total in [(0, 100000), (10000, 12000), (10000, 0), (0, 0)]:
        payload = {
            "sections": [{"kind": "system", "label": "System prompt", "tokens": total}]
            if total else [],
            "estimated_tokens": total,
            "context_window": capacity,
            "measured_input_tokens": 0 if total else None,
        }
        controller.contextRequested.emit(payload)
        verify_graph(payload)
        assert find_item(window, "contextEmpty").isVisible() == (total == 0)
        assert find_item(window, "contextOverflow").isVisible() == (0 < capacity < total)
        if not capacity:
            assert find_item(window, "contextWindowShare").property("text") == "Unknown"
        if total:
            assert "0 input tokens" in find_item(window, "contextMeasured").property("text")
        else:
            assert viewport.property("contentHeight") <= viewport.height() + 1, (
                viewport.property("contentHeight"), viewport.height()
            )
        save_screenshot(window, f"context-edge-{capacity}-{total}")
    QTest.keyClick(window, Qt.Key.Key_Escape)
    assert not dialog.property("visible")


def test_transcript_golden_replay(qt_app, capfd):
    model = Transcript()
    tester = QAbstractItemModelTester(model, QAbstractItemModelTester.FailureReportingMode.Warning)
    blocks = [{"kind": "text", "text": "请检查代码"}]
    events: list[dict] = [
        {
            "kind": "inbox/spliced",
            "target": "next_turn",
            "index": 0,
            "removed": 0,
            "inserted": [{"id": "m1", "blocks": blocks}],
        },
        {"kind": "step/claimed", "messages": [{"id": "m1", "blocks": blocks}]},
        {"kind": "assistant/chunk", "attempt_id": "a1", "delta": "Checking"},
        {
            "kind": "assistant/message",
            "attempt_id": "a1",
            "blocks": [
                {"kind": "text", "text": "Checking."},
                {
                    "kind": "tool_call",
                    "call_id": "t1",
                    "tool_name": "read",
                    "arguments_json": '{"path":"main.py"}',
                },
                {
                    "kind": "tool_call",
                    "call_id": "t2",
                    "tool_name": "read",
                    "arguments_json": '{"path":"missing.py"}',
                },
            ],
        },
        {
            "kind": "tool/result",
            "blocks": [{"kind": "tool_result", "call_id": "t1", "text": "print(1)"}],
        },
        {
            "kind": "assistant/message",
            "attempt_id": "a2",
            "blocks": [{"kind": "text", "text": "代码正确。"}],
        },
        {
            "kind": "tool/result",
            "blocks": [
                {"kind": "tool_result", "call_id": "t2", "text": "File not found", "is_error": True}
            ],
        },
        {"kind": "turn/end", "reason": "completed"},
    ]
    for seq, event in enumerate(events):
        event["seq"] = seq
        model.apply(event)
    expected = [
        ("user", "You", "请检查代码"),
        ("assistant", "Ava", "Checking."),
        ("tool", "read", "print(1)"),
        ("error", "read", "File not found"),
        ("assistant", "Ava", "代码正确。"),
    ]
    assert [(r["kind"], r["heading"], r["body"]) for r in model.rows] == expected
    assert model.pending_text() == ""
    for event in events:
        model.apply(event)
    assert len(model.rows) == 5 and model.rowCount() == 4
    roles = {bytes(name.data()).decode(): role for role, name in model.roles.items()}
    group = model.index(2)
    assert model.data(group, roles["groupRunning"]) == 0
    assert model.data(group, roles["groupFailed"]) == 1
    answer = QPersistentModelIndex(model.index(3))
    model.toggleGroup(2)
    assert model.rowCount() == 5 and answer.row() == 4
    assert model.data(answer, roles["body"]) == "代码正确。"
    model.toggleGroup(2)
    assert answer.row() == 3
    model.clear()
    for event in events:
        model.apply(event)
    assert [(r["kind"], r["heading"], r["body"]) for r in model.rows] == expected
    assert model.rowCount() == 4
    assert model.activity == ""

    activity = Transcript()
    activity_events: list[dict[str, Any]] = [
        {"kind": "step/start"},
        {"kind": "assistant/chunk", "attempt_id": "activity", "delta": "Checking"},
        {
            "kind": "assistant/message",
            "attempt_id": "activity",
            "blocks": [
                {
                    "kind": "tool_call",
                    "call_id": "command",
                    "tool_name": "bash",
                    "arguments_json": '{}',
                },
                {
                    "kind": "tool_call",
                    "call_id": "file",
                    "tool_name": "read",
                    "arguments_json": '{}',
                },
            ],
        },
        {
            "kind": "tool/result",
            "blocks": [{"kind": "tool_result", "call_id": "command", "text": "ok"}],
        },
        {
            "kind": "tool/result",
            "blocks": [{"kind": "tool_result", "call_id": "file", "text": "ok"}],
        },
        {"kind": "turn/end", "reason": "completed"},
    ]
    phases = []
    for seq, activity_event in enumerate(activity_events):
        activity.apply({**activity_event, "seq": seq})
        phases.append(activity.activity)
    assert phases == [
        "Thinking",
        "Responding",
        "Running commands, reading files",
        "Reading files",
        "Thinking",
        "",
    ]
    assert tester.model() == model
    assert "FAIL!" not in capfd.readouterr().err


def test_running_status_is_at_transcript_tail_and_tracks_elapsed_time(desktop, model_server):
    controller, window = desktop
    controller.start()
    until(lambda: bool(controller.projects), controller.changed)
    click(window, "newChatButton")
    until(lambda: controller.connected, controller.changed)

    type_message(window, "Show the running state")
    click(window, "sendButton")
    until(
        lambda: (
            controller.status == "running"
            and bool(model_server)
            and controller._transcript.activity == "Responding"
        ),
        controller.changed,
    )

    status = find_item(window, "runStatus")
    transcript = find_item(window, "transcriptView")
    composer = find_item(window, "composerCard")
    status_top = status.mapToScene(QPointF()).y()
    status_bottom = status_top + status.height()
    transcript_top = transcript.mapToScene(QPointF()).y()
    transcript_bottom = transcript_top + transcript.height()
    assert status_top >= transcript_top
    assert status_bottom <= transcript_bottom + 1
    assert status_bottom < composer.mapToScene(QPointF()).y()
    phase = find_item(window, "runPhase")
    elapsed = find_item(window, "runElapsed")
    assert phase.property("text") == "Responding"
    assert abs(phase.mapToScene(QPointF()).y() - elapsed.mapToScene(QPointF()).y()) <= 2
    first_elapsed = elapsed.property("text")
    assert re.fullmatch(r"\(\d+s · Esc to pause\)", first_elapsed)
    until(lambda: elapsed.property("text") != first_elapsed, window.frameSwapped, timeout=3000)
    assert re.fullmatch(r"\([1-9]\d*s · Esc to pause\)", elapsed.property("text"))
    save_screenshot(window, "running-status")
    for seconds, expected in (
        (59, "(59s · Esc to pause)"),
        (60, "(1m 00s · Esc to pause)"),
        (3600, "(1h 00m 00s · Esc to pause)"),
    ):
        status.setProperty("elapsedSeconds", seconds)
        assert elapsed.property("text") == expected

    model_server[0].release.set()
    until(lambda: controller.status == "idle", controller.changed)
    assert not status.isVisible()


def test_pause_queue_resume_and_backend_failure(desktop, model_server):
    controller, window = desktop
    controller.start()
    until(lambda: bool(controller.projects), controller.changed)
    click(window, "newChatButton")
    until(lambda: bool(controller.connected), controller.changed)
    identity = controller.chatId
    type_message(window, "first turn")
    click(window, "sendButton")
    until(lambda: len(controller._transcript.rows) == 2 and not controller.busy, controller.changed)
    type_message(window, "queued turn")
    click(window, "sendButton")
    until(
        lambda: controller.pendingText == "queued turn" and not controller.busy, controller.changed
    )
    controller.control("pause")
    until(lambda: controller.status == "pausing" and not controller.busy, controller.changed)
    model_server[0].release.set()
    until(lambda: controller.status == "paused", controller.changed)
    assert controller.pendingText == "queued turn"
    controller.control("resume")
    until(
        lambda: (
            sum(r["body"] == "Hello " for r in controller._transcript.rows) == 1
            and len(model_server) == 2
        ),
        controller.changed,
    )
    assert not controller.pendingText
    model_server[1].release.set()
    until(lambda: controller.status == "idle", controller.changed)
    users = [r["body"] for r in controller._transcript.rows if r["kind"] == "user"]
    assert users == ["first turn", "queued turn"]

    type_message(window, "after crash")
    os.kill(controller.runtime.info["pid"], SIGKILL)
    until(lambda: not controller.online, controller.changed)
    assert "connection lost" in controller.error
    until(
        lambda: controller.runtime.process.state() == QProcess.ProcessState.NotRunning,
        controller.runtime.stopped,
    )
    controller.start()
    until(
        lambda: (
            controller.chatId == identity
            and controller.connected
            and len([r for r in controller._transcript.rows if r["kind"] == "user"]) == 2
        ),
        controller.changed,
    )
    assert find_item(window, "composer").property("text") == "after crash"

    # Failed requests retain the draft and show the backend's actual rejection.
    connection = controller._connection
    response = httpx.post(
        connection._base + f"/api/chats/{identity}/archive",
        headers={"Authorization": f"Bearer {connection._token}"},
        json={"archived": True},
    )
    assert response.status_code == 200
    click(window, "sendButton")
    until(lambda: bool(controller.error) and not controller.busy, controller.changed)
    assert "archived" in controller.error
    assert find_item(window, "composer").property("text") == "after crash"


def save_screenshot(window, suffix):
    if QGuiApplication.focusWindow() != window:
        window.raise_()
        window.requestActivate()
    # Deliver pending native resize events before waiting for the next rendered
    # frame. A frame already queued at the old size is not a layout checkpoint.
    QCoreApplication.processEvents()
    window.contentItem().ensurePolished()
    presented = QSignalSpy(window.frameSwapped)
    window.update()
    assert presented.wait(2000), (
        f"screenshot frame was not presented: exposed={window.isExposed()}, "
        f"active={window.isActive()}, app={QGuiApplication.applicationState()}"
    )
    output = os.environ.get("AVA_DESKTOP_SCREENSHOT")
    if output:
        target = Path(output)
        # Use the same asynchronous capture path for every scene; avoid
        # grabWindow's synchronous GPU readback on the GUI thread.
        capture = window.contentItem().grabToImage()
        assert capture is not None
        repaint = QTimer()
        repaint.setInterval(16)
        repaint.timeout.connect(window.update)
        repaint.start()
        try:
            until(lambda: not capture.image().isNull(), capture.ready)
        finally:
            repaint.stop()
        # The item capture is asynchronous; composite the actual window clear
        # color because it is not a node in the captured content item.
        from PySide6.QtGui import QPainter

        pixels = capture.image()
        result = QImage(pixels.size(), QImage.Format.Format_ARGB32_Premultiplied)
        result.setDevicePixelRatio(pixels.devicePixelRatio())
        result.fill(window.color())
        painter = QPainter(result)
        painter.drawImage(QPointF(), pixels)
        painter.end()
        assert result.save(str(target.with_stem(target.stem + "-" + suffix)))


def terminal_evaluate(pane, script):
    done = QSignalSpy(pane.evaluated)
    QMetaObject.invokeMethod(pane, "evaluate", Q_ARG(str, script))
    assert done.count() or done.wait(3000)
    value = pane.property("evaluation")
    return value.toVariant() if isinstance(value, QJSValue) else value


def pdf_page_images(view):
    pending, images = [view], []
    while pending:
        item = pending.pop()
        if item.objectName() == "pdfPageImage":
            images.append(item)
        pending.extend(item.childItems())
    return images


def test_desktop_models_attachments_skills_markdown_and_files(
    desktop, model_server, project, qt_app
):
    """Golden workbench flow: user gestures must reach the real provider and renderer."""
    (project / "notes.md").write_text("# Project notes\n\nA **small** example.\n")
    skill = project / ".agents/skills/review/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: review\ndescription: Review the project carefully\n---\nRead the project notes.\n"
    )
    controller, window = desktop
    controller.start()
    until(lambda: bool(controller.projects), controller.changed)
    click(window, "newChatButton")
    until(lambda: controller.connected and bool(controller._skills), controller.changed)
    first_chat = controller.chatId

    # A mouse-opened picker changes both model and advertised reasoning effort.
    click(window, "modelButton")
    until(lambda: bool(controller.modelChoices.get("providers")), controller.changed)
    click(window, "modelPicker")
    QTest.keyClick(window, Qt.Key.Key_Down)
    QTest.keyClick(window, Qt.Key.Key_Return)
    assert controller.selection.get("model") == "fixture"
    click(window, "effortPicker")
    QTest.keyClick(window, Qt.Key.Key_Down)
    QTest.keyClick(window, Qt.Key.Key_Down)
    QTest.keyClick(window, Qt.Key.Key_Return)
    save_screenshot(window, "models")
    click(window, "applyConversationModel")
    until(lambda: controller.selection.get("effort") == "high", controller.changed)
    assert controller.selection["model"] == "fixture-reasoning"
    until(lambda: not window.findChild(QObject, "modelDialog").property("visible"), window.frameSwapped)

    click(window, "toggleRightSidebar")
    assert controller.fileState.get("kind") == "directory", (
        controller.fileState,
        controller.error,
        window.property("rightOpen"),
    )
    until(lambda: bool(find_item(window, "file_notes.md")), window.frameSwapped)
    click(window, "file_notes.md")
    assert controller.fileState["kind"] == "markdown"
    preview = find_item(window, "filePreview")
    assert "Project notes" in preview.property("textDocument").textDocument().toPlainText()
    click(window, "attachPreviewButton")
    assert [a["name"] for a in controller.attachments] == ["notes.md"]

    # Native chooser selection boundary, then actual clipboard-image paste into the composer.
    click(window, "attachButton")
    dialog = window.findChild(QObject, "attachmentDialog")
    assert dialog.setProperty("selectedFile", QUrl.fromLocalFile(str(project / "notes.md")))
    QMetaObject.invokeMethod(dialog, "accepted")
    QMetaObject.invokeMethod(dialog, "close")
    until(lambda: len(controller.attachments) == 2, controller.draftChanged)
    click(window, "remove_notes.md")
    assert len(controller.attachments) == 1
    picture = QImage(80, 60, QImage.Format.Format_RGB32)
    picture.fill(Qt.GlobalColor.darkGreen)
    qt_app.clipboard().setImage(picture)
    click(window, "composer")
    find_item(window, "composer").forceActiveFocus()
    QTest.keySequence(window, QKeySequence(QKeySequence.StandardKey.Paste))
    assert [a["kind"] for a in controller.attachments] == ["file", "image"]
    save_screenshot(window, "attachments")

    click(window, "newChatButton")
    until(lambda: controller.connected and controller.chatId != first_chat, controller.changed)
    assert not controller.attachments
    controller.openChat(first_chat)
    until(lambda: controller.connected and controller.chatId == first_chat, controller.changed)
    assert len(controller.attachments) == 2

    type_message(window, "/rev")
    until(lambda: bool(find_item(window, "command_review")), window.frameSwapped)
    save_screenshot(window, "commands")
    QTest.keyClick(window, Qt.Key.Key_Tab)
    assert controller.draft == "$review "
    type_message(window, "Summarize these notes.", append=True)
    click(window, "sendButton")
    until(
        lambda: any(r["kind"] == "assistant" for r in controller._transcript.rows),
        controller.changed,
    )
    assert model_server[0].request["model"] == "fixture-reasoning"
    assert model_server[0].request["reasoning_effort"] == "high"
    sent = json.dumps(model_server[0].request["messages"])
    assert "Project notes" in sent and "data:image/png;base64," in sent and "$review" in sent
    until(lambda: not controller.busy, controller.changed)
    assert not controller.attachments
    model_server[0].release.set()
    until(lambda: controller.status == "idle", controller.changed)
    until(lambda: bool(find_item(window, "assistantMarkdown")), window.frameSwapped)
    markdown = find_item(window, "assistantMarkdown")
    quick_document = markdown.property("textDocument")
    document = quick_document.textDocument()
    assert "A clearer workspace" in document.toPlainText()
    assert "**Native controls**" not in document.toPlainText()
    assert document.begin().blockFormat().headingLevel() == 2
    assert find_item(window, "transcriptAttachment_notes.md")
    assert find_item(window, "transcriptAttachment_Pasted image.png")
    save_screenshot(window, "workbench")

    click(window, "hideSidebarButton")
    assert not find_item(window, "leftSidebar").isVisible()
    click(window, "closeInspectorButton")
    assert not find_item(window, "rightSidebar").isVisible()
    assert controller.preference("leftSidebar", True) is False
    assert controller.preference("rightSidebar", True) is False
    window.setWidth(800)
    window.setHeight(600)
    save_screenshot(window, "compact")
    click(window, "toggleLeftSidebar")
    click(window, "toggleRightSidebar")
    save_screenshot(window, "narrow")
    card = find_item(window, "composerCard")
    send = find_item(window, "sendButton")
    assert (
        send.mapToScene(QPointF(send.width(), 0)).x()
        <= card.mapToScene(QPointF(card.width() - 8, 0)).x()
    )
    window.setProperty("dark", True)
    current_output = find_item(window, "assistantMarkdown")
    current_document = current_output.property("textDocument").textDocument()
    until(
        lambda: (
            current_document.find("Open preview").charFormat().foreground().color()
            == current_output.property("linkColor")
        ),
        window.frameSwapped,
    )
    save_screenshot(window, "dark")
    assert not controller.error


def test_desktop_embedded_browser_navigation(desktop, model_server, home, monkeypatch):
    controller, window = desktop
    # Exercise automatic default-browser detection in the real importer subprocess.
    # HOME is isolated by the shared fixture, including the OS preference boundary.
    user = Path.home()
    if sys.platform == "darwin":
        preferences = user / "Library/Preferences/com.apple.LaunchServices"
        preferences.mkdir(parents=True)
        (preferences / "com.apple.launchservices.secure.plist").write_bytes(
            plistlib.dumps(
                {
                    "LSHandlers": [
                        {"LSHandlerURLScheme": "https", "LSHandlerRoleAll": "org.mozilla.firefox"}
                    ]
                }
            )
        )
        root = user / "Library/Application Support/Firefox"
    else:
        binaries = user / "bin"
        binaries.mkdir()
        command = binaries / "xdg-settings"
        command.write_text("#!/bin/sh\nprintf 'firefox.desktop\\n'\n")
        command.chmod(0o700)
        monkeypatch.setenv("PATH", str(binaries) + os.pathsep + os.environ["PATH"])
        root = user / ".mozilla/firefox"
    profile = root / "fixture.default"
    profile.mkdir(parents=True)
    (root / "profiles.ini").write_text("[InstallFixture]\nDefault=fixture.default\n")
    with sqlite3.connect(profile / "cookies.sqlite") as db:
        db.execute(
            "CREATE TABLE moz_cookies (host TEXT, path TEXT, isSecure INTEGER, expiry INTEGER, name TEXT, value TEXT, isHttpOnly INTEGER, sameSite INTEGER, originAttributes TEXT)"
        )
        db.execute(
            "INSERT INTO moz_cookies VALUES ('127.0.0.1','/account',0,0,'fixture_session','imported-session',1,1,'')"
        )
        db.execute(
            "INSERT INTO moz_cookies VALUES ('127.0.0.1','/account',0,0,'isolated','container-session',1,1,'^userContextId=1')"
        )
    with sqlite3.connect(profile / "places.sqlite") as db:
        db.execute(
            "CREATE TABLE moz_places (id INTEGER, title TEXT, url TEXT, last_visit_date INTEGER)"
        )
        db.execute("CREATE TABLE moz_bookmarks (fk INTEGER, type INTEGER, title TEXT)")
        db.execute(
            "INSERT INTO moz_places VALUES (1, '中文资料', 'https://example.test/reference', 123)"
        )
        db.execute("INSERT INTO moz_bookmarks VALUES (1, 1, '中文资料')")
    controller.start()
    until(lambda: bool(controller.projects), controller.changed)
    click(window, "newChatButton")
    until(lambda: controller.connected, controller.changed)
    click(window, "toggleRightSidebar")
    click(window, "browserTab")
    until(lambda: bool(find_item(window, "webBrowser")), window.frameSwapped)
    browser = find_item(window, "webBrowser")
    session = controller.browserSession
    until(
        lambda: not session.importing and bool(session.importStatus),
        session.changed,
        timeout=20_000,
    )
    assert "1 cookies, 1 bookmarks, 1 history" in session.importStatus
    assert "Skipped 1" in session.importStatus
    assert not session.profile.isOffTheRecord()
    report = (home / "browser/library.json").read_text()
    assert "imported-session" not in report and "container-session" not in report
    assert json.loads(report)["attempted"] is True
    click(window, "bookmarksButton")
    until(lambda: bool(find_item(window, "browserEntry_中文资料")), window.frameSwapped)
    assert find_item(window, "browserEntry_中文资料").isVisible()
    click(window, "closeBrowserLibrary")
    url = json.loads((home / "settings.json").read_text())["providers"]["desktop-test"][
        "base_url"
    ].removesuffix("/v1")
    address = find_item(window, "browserAddress")
    address.setProperty("text", url + "/account")
    address.forceActiveFocus()
    QTest.keyClick(window, Qt.Key.Key_Return)
    until(
        lambda: browser.property("title") == "Account ready", browser.titleChanged, timeout=20_000
    )
    address.setProperty("text", url + "/preview")
    address.forceActiveFocus()
    QTest.keyClick(window, Qt.Key.Key_Return)
    until(
        lambda: browser.property("title") == "Browser ready", browser.titleChanged, timeout=20_000
    )
    until(lambda: not browser.property("loading"), browser.loadingChanged, timeout=20_000)
    assert browser.height() > 200 and browser.width() > 200
    QTest.qWait(400)
    assert browser.property("url").toString() == url + "/preview"
    save_screenshot(window, "browser")
    address.setProperty("text", url + "/next")
    address.forceActiveFocus()
    QTest.keyClick(window, Qt.Key.Key_Return)
    until(lambda: browser.property("title") == "Next page", browser.titleChanged)
    click(window, "browserBack")
    until(lambda: browser.property("title") == "Browser ready", browser.titleChanged)
    click(window, "browserForward")
    until(lambda: browser.property("title") == "Next page", browser.titleChanged)
    click(window, "closeInspectorButton")
    click(window, "toggleRightSidebar")
    assert browser.property("title") == "Next page"
    assert not controller.error


@pytest.mark.parametrize(
    "width,left_open,right_open",
    [(1280, True, False), (1600, False, False), (1280, True, True), (900, False, True)],
)
def test_desktop_message_composer_alignment(desktop, width, left_open, right_open):
    controller, window = desktop
    window.setWidth(width)
    window.setProperty("leftOpen", left_open)
    window.setProperty("rightOpen", right_open)
    controller._transcript.append("user", "You", "hello")
    controller._transcript.append("assistant", "Ava", "Hello")
    QTest.qWait(100)
    message = find_item(window, "assistantMarkdown")
    user_message = find_item(window, "messageBody")
    # Locate the semantic surface rather than depending on a cosmetic corner radius.
    composer = find_item(window, "composerCard")
    assert composer is not None
    message_left = message.mapToScene(QPointF(0, 0)).x()
    composer_left = composer.mapToScene(QPointF(0, 0)).x()
    assert abs(message_left - composer_left) <= 1
    assert abs(message.width() - composer.width()) <= 1
    assert user_message.width() < composer.width() * 0.9
    assert (
        abs(
            user_message.mapToScene(QPointF(user_message.width(), 0)).x()
            - (composer_left + composer.width())
        )
        <= 1
    )


def test_desktop_code_preview(desktop, model_server, project):
    source = '# 中文 and emoji 🐍\ndef hello():\n    return "world"\n' + "# long " + "x" * 240
    (project / "sample.py").write_text(source)
    controller, window = desktop
    controller.start()
    until(lambda: bool(controller.projects), controller.changed)
    click(window, "toggleRightSidebar")
    controller.browseFiles(str(project / "sample.py"))
    QTest.qWait(100)
    code = find_item(window, "codePreview")
    document = code.property("document")
    assert document.selectedText(0, 0, 3, 1000) == source
    assert document.count == 4
    assert document.selectedText(0, 15, 0, 17) == "🐍"
    until(lambda: "<span" in document.data(document.index(1), document.HTML), document.dataChanged)
    horizontal = find_item(window, "codeHorizontalScroll")
    assert horizontal.property("contentWidth") > horizontal.width()
    click(window, "codePreview")
    QTest.keySequence(window, QKeySequence(QKeySequence.StandardKey.SelectAll))
    QTest.keySequence(window, QKeySequence(QKeySequence.StandardKey.Copy))
    assert QGuiApplication.clipboard().text() == source
    save_screenshot(window, "code-preview")
    (project / "plain.unknown").write_text("<b>not markup</b>")
    controller.browseFiles(str(project / "plain.unknown"))
    until(lambda: document.count == 1, document.changed)
    assert document.selectedText(0, 0, 0, 1000) == "<b>not markup</b>"


def test_desktop_resizable_sidebars(desktop):
    _, window = desktop
    window.setProperty("rightOpen", True)
    QCoreApplication.processEvents()
    left = find_item(window, "leftSidebar")
    right = find_item(window, "rightSidebar")

    def drag(x, distance):
        origin = QPointF(x, window.height() / 2).toPoint()
        end = QPointF(x + distance, window.height() / 2).toPoint()
        QTest.mousePress(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, origin)
        QTest.mouseMove(window, end, 30)
        QTest.mouseRelease(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, end)
        QCoreApplication.processEvents()

    old_left = left.width()
    drag(left.mapToScene(QPointF(left.width() + 2, 0)).x(), 90)
    assert left.width() > old_left + 60
    old_right = right.width()
    drag(right.mapToScene(QPointF(-2, 0)).x(), -80)
    assert right.width() > old_right + 50
    click(window, "closeInspectorButton")
    click(window, "toggleRightSidebar")
    assert right.width() > old_right + 50


def test_desktop_sessions_group_by_project_and_switch_without_picker(
    desktop, model_server, project, tmp_path
):
    controller, window = desktop
    controller.start()
    until(lambda: bool(controller.projects), controller.changed)
    click(window, "newChatButton")
    until(lambda: controller.connected, controller.changed)
    first_project, first_chat = controller.projectId, controller.chatId
    type_message(window, "Draft for the first project")
    (project / "context.txt").write_text("First project context")
    controller.addAttachments([str(project / "context.txt")])
    other = tmp_path / "另一个项目"
    other.mkdir()
    controller.addProject(other.as_uri())
    until(lambda: controller.projectId != first_project, controller.changed)
    second_project = controller.projectId
    click(window, "newChatButton")
    until(lambda: controller.connected, controller.changed)
    second_chat = controller.chatId
    type_message(window, "另一个项目的草稿")

    assert find_item(window, "projectPicker") is None
    for identity in (first_chat, second_chat):
        assert find_item(window, "session_" + identity).isVisible()
    click(window, "session_" + first_chat)
    until(lambda: controller.connected and controller.chatId == first_chat, controller.changed)
    assert controller.projectId == first_project and controller.projectPath == str(project)
    assert controller.draft == "Draft for the first project"
    assert [entry["name"] for entry in controller.attachments] == ["context.txt"]
    assert controller.fileState["path"] == str(project)

    click(window, "projectGroup_" + second_project)
    QCoreApplication.processEvents()
    assert not find_item(window, "session_" + second_chat)
    assert controller.chatId == first_chat
    controller.refresh()
    until(
        lambda: not any(row.get("id") == second_chat for row in controller.sessionRows),
        controller.navigationChanged,
    )
    assert not controller.preference("groups/" + second_project, True)
    save_screenshot(window, "project-groups-collapsed")
    click(window, "projectGroup_" + second_project)
    click(window, "session_" + second_chat)
    until(lambda: controller.connected and controller.chatId == second_chat, controller.changed)
    assert controller.projectId == second_project
    assert controller.draft == "另一个项目的草稿" and not controller.attachments
    save_screenshot(window, "project-groups")

    click(window, "newChat_" + first_project)
    until(
        lambda: (
            controller.connected
            and controller.projectId == first_project
            and controller.chatId != first_chat
        ),
        controller.changed,
    )
    assert len(next(p for p in controller.projects if p["id"] == first_project)["chats"]) == 2
    assert not controller.draft and not controller.attachments
    assert not controller.error

    blank_chat = controller.chatId
    click(window, "session_" + first_chat)
    until(lambda: controller.connected and controller.chatId == first_chat, controller.changed)
    until(
        lambda: not any(
            chat["id"] == blank_chat
            for item in controller.projects
            for chat in item["chats"]
        ),
        controller.navigationChanged,
    )
    assert any(
        chat["id"] == second_chat
        for item in controller.projects
        for chat in item["chats"]
    )
    assert controller.draft == "Draft for the first project"


def test_desktop_new_chat_shows_working_directory_and_can_switch(desktop, model_server, project, tmp_path):
    controller, window = desktop
    controller.start()
    until(lambda: bool(controller.projects), controller.changed)
    assert controller.projectPath == str(project)
    assert find_item(window, "chatDirectory").property("text") == str(project)
    assert str(project) in find_item(window, "locationSubtitle").property("text")

    other = tmp_path / "chosen folder"
    other.mkdir()
    controller.newChatInFolder(other.as_uri())
    until(
        lambda: controller.connected
        and controller.chatId
        and controller.projectPath == str(other),
        controller.changed,
    )
    assert controller.workspacePath == str(other)
    assert find_item(window, "chatDirectory").property("text") == str(other)
    assert find_item(window, "chooseChatFolderButton").property("visible")

    # An empty new chat is still a new session, so the folder can be changed again,
    # including re-selecting the same folder without leaving an unused chat behind.
    first_chat = controller.chatId
    controller.newChatInFolder(other.as_uri())
    until(
        lambda: controller.connected
        and controller.chatId
        and controller.chatId != first_chat
        and controller.projectPath == str(other),
        controller.changed,
    )
    until(
        lambda: not any(chat["id"] == first_chat for project in controller.projects for chat in project["chats"]),
        controller.navigationChanged,
    )
    first_chat = controller.chatId
    third = tmp_path / "third folder"
    third.mkdir()
    controller.newChatInFolder(third.as_uri())
    until(
        lambda: controller.connected
        and controller.chatId
        and controller.chatId != first_chat
        and controller.projectPath == str(third),
        controller.changed,
    )
    until(
        lambda: not any(chat["id"] == first_chat for project in controller.projects for chat in project["chats"]),
        controller.navigationChanged,
    )
    assert controller.workspacePath == str(third)
    assert find_item(window, "chatDirectory").property("text") == str(third)

    type_message(window, "Draft for the third folder")
    assert not find_item(window, "chooseChatFolderButton").property("visible")


def test_desktop_session_title_is_single_line(desktop, model_server):
    controller, window = desktop
    controller.start()
    until(lambda: bool(controller.projects), controller.changed)
    click(window, "newChatButton")
    until(lambda: controller.connected, controller.changed)
    identity = controller.chatId
    type_message(window, "更新后的文字\n只读网页 Read only content")
    click(window, "sendButton")
    until(
        lambda: "\n" in next(
            chat["title"]
            for project in controller.projects
            for chat in project["chats"]
            if chat["id"] == identity
        ),
        controller.navigationChanged,
    )
    title = find_item(window, "sessionTitle_" + identity)
    assert title.property("lineCount") == 1


def test_desktop_remote_files_preview_cache_and_project_tabs(desktop, model_server, project):
    import shutil

    from ava.app.desktop.connection import Connection
    from ava.app.desktop.controller import Machine
    from ava.app.desktop.runtime import BackendProcess

    controller, window = desktop
    source = project / "中文 #?.md"
    source.write_text("# 远程文件\n\n**Markdown** 与中文")
    fixtures = Path(__file__).parent / "fixtures"
    shutil.copyfile(fixtures / "preview.pdf", project / "报告.pdf")
    large = project / "large"
    large.mkdir()
    file_count = 20_000 if os.environ.get("AVA_DESKTOP_PERF") else 2000
    for i in range(file_count):
        (large / f"module_{i:04}.py").touch()
    controller.start()
    until(lambda: bool(controller.projects), controller.changed)
    # A real authenticated backend, scoped as a remote authority. The separate
    # Fedora E2E exercises this same path through the actual SSH tunnel.
    local = controller._connection
    connection = Connection(int(local._base.rsplit(":", 1)[1]), local._token, controller, prefix="remote~")
    runtime = BackendProcess(project, controller, host="fixture")
    machine = Machine("remote", "Fedora 文件", runtime, "remote", connection,
                      connection._identifiers("/api/projects", {"projects": controller.projects})["projects"], status="Connected")
    controller._machines[machine.id] = machine
    controller._rebuild_projects()
    controller._heartbeat.stop()  # This transport fixture intentionally shares one backend store.
    controller.selectMachine(machine.id)
    click(window, "toggleRightSidebar")
    until(lambda: bool(find_item(window, "file_中文 #?.md")), window.frameSwapped)
    pane = find_item(window, "filePane")
    model = pane.property("treeModel")
    assert model.remote
    pulses = [time.perf_counter()]
    heartbeat = QTimer()
    heartbeat.setInterval(10)
    heartbeat.timeout.connect(lambda: pulses.append(time.perf_counter()))
    heartbeat.start()
    start = time.perf_counter()
    click(window, "file_large")
    tree = find_item(window, "fileTree")
    until(lambda: tree.property("rows") >= file_count + 3, tree.rowsChanged)
    directory_ms = (time.perf_counter() - start) * 1000
    click(window, "file_large")
    click(window, "file_中文 #?.md")
    until(lambda: pane.property("fileState").get("kind") == "markdown", model.previewReady)
    state = pane.property("fileState")
    assert state["text"] == source.read_text()
    cache = Path(state["attachmentPath"])
    assert cache != source and cache.read_bytes() == source.read_bytes()
    before = cache.stat().st_mtime_ns
    click(window, "refreshFilesButton")
    until(lambda: pane.property("fileState").get("kind") == "markdown", model.previewReady)
    assert cache.stat().st_mtime_ns == before
    source.write_text("# Changed remotely")
    click(window, "refreshFilesButton")
    until(lambda: pane.property("fileState").get("text") == "# Changed remotely", model.previewReady)
    click(window, "file_报告.pdf")
    until(lambda: bool(find_item(window, "pdfView")), window.frameSwapped)
    until(lambda: find_item(window, "pdfPreview").property("pageCount") == 3, window.frameSwapped)
    until(lambda: find_item(window, "pdfView").property("currentPageRenderingStatus") == 1, window.frameSwapped)
    heartbeat.stop()
    max_pause = max((b-a)*1000 for a,b in zip(pulses, pulses[1:], strict=False))
    print("REMOTE_FILES_BENCHMARK", json.dumps({"files": file_count, "directory_ms": round(directory_ms), "max_ui_pause_ms": round(max_pause)}))
    assert max_pause < 150
    save_screenshot(window, "remote-files-pdf")
    remote_pane = pane
    controller.selectMachine("local")
    until(lambda: find_item(window, "filePane") != remote_pane, window.frameSwapped)
    assert not find_item(window, "filePane").property("remote")
    controller.selectMachine("remote")
    until(lambda: find_item(window, "filePane") == remote_pane, window.frameSwapped)
    # A disconnected tree reports a retry action; cached PDF remains readable.
    machine.connection = None
    controller.machinesChanged.emit()
    click(window, "refreshFileTree")
    assert "offline" in model.error
    assert find_item(window, "pdfPreview").property("pageCount") == 3
    machine.connection = connection
    controller.machinesChanged.emit()
    click(window, "refreshFileTree")
    until(lambda: bool(find_item(window, "file_中文 #?.md")), window.frameSwapped)
    assert not model.error

    changing = project / "cancel.txt"
    changing.write_text("remote line\n" * 70_000)
    interrupted: list[bool] = []

    def switch_during_download(state):
        if state.get("path") == str(changing) and state.get("progress", 0) > 0 and not interrupted:
            interrupted.append(True)
            controller.browseFiles(str(source))

    model.previewReady.connect(switch_during_download)
    controller.browseFiles(str(changing))
    until(lambda: interrupted and pane.property("fileState").get("text") == "# Changed remotely", model.previewReady)
    model.previewReady.disconnect(switch_during_download)
    assert not any(path.suffix == ".txt" for path in Path(model._temporary.path()).iterdir())
    temporary_path = Path(model._temporary.path())
    controller.selectMachine("local")
    click(window, "closeInspectorTab_0")
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert not temporary_path.exists()


@pytest.mark.skipif(
    not os.environ.get("AVA_DESKTOP_PERF"), reason="Opt-in native rendering benchmark"
)
def test_desktop_file_explorer_performance(desktop, model_server, project, tmp_path):
    from time import perf_counter

    def next_frame():
        presented = QSignalSpy(window.frameSwapped)
        window.update()
        assert presented.wait(2000), "preview frame was not presented"

    large = project / "large"
    large.mkdir()
    for index in range(2000):
        (large / f"module_{index:04}.py").touch()
    code_file = project / "large.py"
    code_file.write_text("# 中文模块\ndef calculate(value):\n    return value * 2 + 42\n\n" * 4000)
    controller, window = desktop
    controller.start()
    until(lambda: bool(controller.projects), controller.changed)
    click(window, "toggleRightSidebar")
    until(lambda: bool(find_item(window, "file_large")), window.frameSwapped)
    QTest.qWait(150)  # Start after the inspector has presented its initial frame.
    samples = []
    previous = perf_counter()
    heartbeat = QTimer()
    heartbeat.setInterval(16)

    def beat():
        nonlocal previous
        now = perf_counter()
        samples.append(now - previous)
        previous = now

    heartbeat.timeout.connect(beat)
    heartbeat.start()
    start = perf_counter()
    click(window, "file_large")
    tree = find_item(window, "fileTree")
    until(lambda: tree.property("rows") >= 2002, tree.rowsChanged)
    next_frame()
    directory_ms = (perf_counter() - start) * 1000
    click(window, "file_large")
    QTest.qWait(30)
    start = perf_counter()
    click(window, "file_large.py")
    next_frame()
    preview_ms = (perf_counter() - start) * 1000
    code = find_item(window, "codePreview")
    assert code.property("text").startswith("# 中文模块")
    lines = find_item(window, "codeLines")
    for fraction in (0.25, 0.5, 0.9, 0):
        lines.setProperty("contentY", (lines.property("contentHeight") - lines.height()) * fraction)
        QTest.qWait(60)
    # Count the live native delegates, including the reuse pool, not model rows.
    delegates = [
        child for child in lines.childItems()[0].childItems() if child.objectName() == "codeLine"
    ]
    assert len(delegates) < 100
    minified = project / "bundle.js"
    minified.write_text("const answer = 42; " * 16000)
    start = perf_counter()
    controller.browseFiles(str(minified))
    next_frame()
    minified_ms = (perf_counter() - start) * 1000
    document = find_item(window, "codePreview").property("document")
    assert document.count > 1
    assert document.selectedText(0, 0, document.count - 1, 2147483647) == minified.read_text()
    markdown_file = project / "report.md"
    markdown_file.write_text(
        (
            "## 项目进展\n\n一份 **清晰易读** 的项目报告。\n\n> 重要的上下文\n\n- 任务一\n- 任务二\n\n```python\ndef answer():\n    return 42\n```\n\n"
        )
        * 300
    )
    start = perf_counter()
    controller.browseFiles(str(markdown_file))
    next_frame()
    markdown_ms = (perf_counter() - start) * 1000
    QTest.qWait(32)
    heartbeat.stop()
    metrics = {
        "directory_ms": round(directory_ms),
        "preview_ms": round(preview_ms),
        "max_ui_pause_ms": round(max(samples) * 1000),
        "live_code_rows": len(delegates),
        "minified_preview_ms": round(minified_ms),
        "markdown_preview_ms": round(markdown_ms),
        "markdown_bytes": markdown_file.stat().st_size,
    }
    print("FILE_EXPLORER_BENCHMARK", json.dumps(metrics))
    (tmp_path / "file-explorer-benchmark.json").write_text(json.dumps(metrics))
    save_screenshot(window, "large-markdown")
    controller.browseFiles(str(minified))
    next_frame()
    save_screenshot(window, "minified-code")
    controller.browseFiles(str(code_file))
    QTest.qWait(100)
    save_screenshot(window, "large-code")
    assert (
        directory_ms < 250
        and preview_ms < 500
        and minified_ms < 500
        and markdown_ms < 500
        and max(samples) < 0.15
    ), metrics


def test_desktop_pdf_preview_pages_zoom_and_tabs(desktop, model_server, project):
    import shutil

    fixtures = Path(__file__).parent / "fixtures"
    source = project / "中文报告.pdf"
    shutil.copyfile(fixtures / "preview.pdf", source)
    shutil.copyfile(fixtures / "preview-locked.pdf", project / "locked.pdf")
    (project / "broken.pdf").write_bytes(b"%PDF-1.7\nThis is not a valid PDF.")
    (project / "notes.txt").write_text("Back to a normal text preview")
    controller, window = desktop
    controller.start()
    until(lambda: bool(controller.projects), controller.changed)
    click(window, "toggleRightSidebar")
    until(lambda: bool(find_item(window, "file_中文报告.pdf")), window.frameSwapped)
    click(window, "file_中文报告.pdf")
    assert controller.fileState["kind"] == "pdf", controller.fileState
    until(lambda: find_item(window, "pdfView") is not None, window.frameSwapped)
    view = find_item(window, "pdfView")
    until(lambda: view.property("currentPageRenderingStatus") == 1, window.frameSwapped)
    until(
        lambda: all(
            image.mapToItem(view, QPointF()).x() >= 0
            and image.mapToItem(view, QPointF(image.width(), 0)).x() <= view.width()
            for image in pdf_page_images(view)
        ),
        window.frameSwapped,
    )
    assert find_item(window, "pdfPageCount").property("text") == "/ 3"
    assert not find_item(window, "attachPreviewButton").isVisible()
    save_screenshot(window, "pdf")
    image = next(image for image in pdf_page_images(view) if image.property("currentFrame") == 0)
    point = image.mapToScene(QPointF(image.width() / 2, image.height() * 0.75)).toPoint()
    QTest.mouseClick(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point)
    QTest.keySequence(window, QKeySequence(QKeySequence.StandardKey.SelectAll))
    QTest.keySequence(window, QKeySequence(QKeySequence.StandardKey.Copy))
    assert "中文文件预览" in QGuiApplication.clipboard().text()
    save_screenshot(window, "pdf-selected")
    QTest.mouseClick(window, Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier, point)
    click_text_menu("Copy")
    assert "A clearer workspace" in QGuiApplication.clipboard().text()
    window.setProperty("dark", True)
    QTest.keyClick(window, Qt.Key.Key_F10, Qt.KeyboardModifier.ShiftModifier)
    until(lambda: text_menu_item("Copy") is not None, window.frameSwapped)
    assert text_menu_item("Paste") is None
    save_screenshot(text_menu_item("Copy")[0], "pdf-context-dark")
    click_text_menu("Copy")
    assert "中文文件预览" in QGuiApplication.clipboard().text()
    window.setProperty("dark", False)

    click(window, "pdfNextPage")
    until(lambda: view.property("currentPage") == 1, window.frameSwapped)
    page = find_item(window, "pdfPageNumber")
    click(window, "pdfPageNumber")
    QTest.keySequence(window, QKeySequence(QKeySequence.StandardKey.SelectAll))
    QTest.keyClick(window, Qt.Key.Key_3)
    QTest.keyClick(window, Qt.Key.Key_Return)
    until(lambda: view.property("currentPage") == 2, window.frameSwapped)
    assert page.property("text") == "3"
    assert not find_item(window, "pdfNextPage").property("enabled")
    scale = view.property("renderScale")
    click(window, "pdfZoomIn")
    until(lambda: view.property("renderScale") > scale, window.frameSwapped)
    assert view.property("currentPage") == 2
    click(window, "pdfFitButton")
    click(window, "pdfActualSize")
    until(lambda: view.property("renderScale") == 1, window.frameSwapped)
    page.forceActiveFocus()
    page.setProperty("text", "1")
    QTest.keyClick(window, Qt.Key.Key_Return)
    until(lambda: view.property("currentPage") == 0, window.frameSwapped)
    table = find_item(window, "pdfPages")
    frame = QSignalSpy(window.frameSwapped)
    window.update()
    assert frame.count() or frame.wait(2000)
    before_scroll = table.property("contentY")
    position = visible_rect(window, view).center()
    QTest.wheelEvent(window, position, QPoint(0, -40))
    until(lambda: table.property("contentY") > before_scroll, window.frameSwapped)
    until(lambda: not table.property("moving"), window.frameSwapped)
    image = next(image for image in pdf_page_images(view) if image.property("currentFrame") == 0)
    before_zoom = -image.mapToItem(view, QPointF()).y() / view.property("renderScale")
    assert before_zoom > 0
    click(window, "pdfZoomIn")
    until(lambda: view.property("renderScale") == 1.25, window.frameSwapped)
    until(lambda: view.property("currentPageRenderingStatus") == 1, window.frameSwapped)
    frame = QSignalSpy(window.frameSwapped)
    window.update()
    assert frame.count() or frame.wait(2000)
    image = next(image for image in pdf_page_images(view) if image.property("currentFrame") == 0)
    after_zoom = -image.mapToItem(view, QPointF()).y() / view.property("renderScale")
    assert abs(after_zoom - before_zoom) < 3, (before_zoom, after_zoom)
    page.forceActiveFocus()
    page.setProperty("text", "3")
    QTest.keyClick(window, Qt.Key.Key_Return)
    until(lambda: view.property("currentPage") == 2, window.frameSwapped)
    click(window, "pdfFitButton")
    click(window, "pdfFitPage")
    until(lambda: view.property("renderScale") < 1, window.frameSwapped)
    click(window, "pdfPreviousPage")
    until(lambda: view.property("currentPage") == 1, window.frameSwapped)
    click(window, "pdfPreviousPage")
    until(lambda: view.property("currentPage") == 0, window.frameSwapped)
    click(window, "pdfLink_0")
    until(lambda: view.property("currentPage") == 2, window.frameSwapped)
    until(
        lambda: view.property("currentPageRenderingStatus") == 1
        and any(
            image.property("currentFrame") == 2 and not visible_rect(window, image).isEmpty()
            for image in pdf_page_images(view)
        ),
        window.frameSwapped,
    )
    saved_scale = view.property("renderScale")
    saved_position = find_item(window, "pdfPages").property("contentY")

    click(window, "addInspectorTab")
    click(window, "newFilesTab")
    until(lambda: bool(find_item(window, "file_locked.pdf")), window.frameSwapped)
    click(window, "file_locked.pdf")
    try:
        until(
            lambda: bool(find_item(window, "pdfPassword"))
            and find_item(window, "pdfPassword").isVisible(),
            window.frameSwapped,
        )
    except AssertionError:
        save_screenshot(window, "pdf-password-failure")
        pane_state = find_item(window, "filePane").property("fileState")
        if isinstance(pane_state, QJSValue):
            pane_state = pane_state.toVariant()
        preview = find_item(window, "pdfPreview")
        pytest.fail(str({"file": pane_state, "error": controller.error, "preview": {
            key: preview.property(key) for key in ("source", "ready", "passwordNeeded", "opened")
        } if preview else None}))
    locked = find_item(window, "pdfPreview")
    password = find_item(window, "pdfPassword")
    click(window, "pdfPassword")
    entry = QInputMethodEvent()
    entry.setCommitString("wrong-password")
    QCoreApplication.sendEvent(password, entry)
    click(window, "pdfUnlock")
    until(lambda: "Incorrect password" in find_item(window, "pdfNotice").property("text"), window.frameSwapped)
    assert password.property("text") == ""
    window.setProperty("dark", True)
    save_screenshot(window, "pdf-password")
    click(window, "pdfCancelUnlock")
    assert find_item(window, "pdfNotice").property("text") == "Preview canceled"
    click(window, "pdfRetry")
    until(lambda: password.isVisible(), window.frameSwapped)
    click(window, "pdfPassword")
    entry = QInputMethodEvent()
    entry.setCommitString("ava-test")
    QCoreApplication.sendEvent(password, entry)
    QTest.keyClick(window, Qt.Key.Key_Return)
    until(lambda: locked.property("ready"), window.frameSwapped)
    assert password.property("text") == ""
    until(lambda: find_item(window, "pdfView") != view, window.frameSwapped)
    second = find_item(window, "pdfView")
    assert second.property("currentPage") == 0
    until(lambda: second.property("currentPageRenderingStatus") == 1, window.frameSwapped)
    save_screenshot(window, "pdf-unlocked")
    click(window, "filesTab")
    assert find_item(window, "pdfView") == view
    assert view.property("currentPage") == 2
    assert view.property("renderScale") == saved_scale
    assert find_item(window, "pdfPages").property("contentY") == saved_position
    click(window, "closeInspectorTab_2")
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert not isValid(second) and not isValid(locked)

    window.setWidth(800)
    frame = QSignalSpy(window.frameSwapped)
    window.update()
    assert frame.count() or frame.wait(2000)
    until(lambda: view.width() < 300, window.frameSwapped)
    until(lambda: view.property("currentPageRenderingStatus") == 1, window.frameSwapped)
    save_screenshot(window, "pdf-narrow")
    for name in ("pdfPreviousPage", "pdfPageNumber", "pdfNextPage", "pdfZoomIn", "pdfFitButton", "pdfZoomValue"):
        control = find_item(window, name)
        panel = find_item(window, "fileContentPanel")
        bounds = control.mapRectToItem(panel, QRectF(0, 0, control.width(), control.height()))
        assert QRectF(0, 0, panel.width(), panel.height()).contains(bounds), name
    window.setWidth(1280)

    click(window, "file_broken.pdf")
    until(lambda: "Cannot open this PDF" in find_item(window, "pdfNotice").property("text"), window.frameSwapped)
    save_screenshot(window, "pdf-damaged")
    click(window, "file_notes.txt")
    until(lambda: find_item(window, "pdfPreview") is None, window.frameSwapped)
    assert not isValid(view)
    click(window, "file_中文报告.pdf")
    until(lambda: find_item(window, "pdfView") is not None, window.frameSwapped)
    source.write_bytes(b"damaged on disk")
    click(window, "refreshFilesButton")
    until(lambda: "Cannot open this PDF" in find_item(window, "pdfNotice").property("text"), window.frameSwapped)
    shutil.copyfile(fixtures / "preview.pdf", source)
    click(window, "pdfRetry")
    until(lambda: find_item(window, "pdfPreview").property("ready"), window.frameSwapped)
    assert not controller.error and not model_server

    # Switch away while resized page images are still being requested. This
    # exercised a Qt image-worker use-after-free in the original renderer.
    for i in range(20):
        click(window, "file_中文报告.pdf")
        until(lambda: find_item(window, "pdfView") is not None, window.frameSwapped)
        window.setWidth(800 if i % 2 else 1280)
        click(window, "file_broken.pdf")
        until(lambda: "Cannot open this PDF" in find_item(window, "pdfNotice").property("text"), window.frameSwapped)
    click(window, "file_notes.txt")
    until(lambda: find_item(window, "pdfPreview") is None, window.frameSwapped)
    assert not controller.pdf_images._sources


@pytest.mark.skipif(
    not os.environ.get("AVA_DESKTOP_PERF"), reason="Opt-in native rendering benchmark"
)
def test_desktop_pdf_lazy_rendering_performance(desktop, model_server, project, tmp_path, monkeypatch):
    from random import Random
    from time import perf_counter

    import psutil
    from PySide6.QtGui import QFont, QPageSize, QPainter, QPdfWriter

    source = project / "large.pdf"
    writer = QPdfWriter(str(source))
    writer.setResolution(72)
    writer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
    writer.setTitle("Thousand-page PDF acceptance fixture")
    painter = QPainter(writer)
    painter.setFont(QFont("Helvetica", 16))
    pixels = Random(42).randbytes(1024 * 1024 * 3)
    picture = QImage(pixels, 1024, 1024, QImage.Format.Format_RGB888)
    for index in range(1000):
        if index:
            assert writer.newPage()
        painter.drawText(32, 48, f"PDF preview - page {index + 1} of 1000")
        painter.drawText(32, 82, "中文分页预览 / Native PDF rendering")
        if index % 100 == 0:
            painter.drawImage(QRectF(32, 120, 480, 480), picture)
        painter.drawText(32, 770, "Pages are rendered only as they enter the viewport.")
    painter.end()
    del writer
    assert source.stat().st_size > 1024 * 1024

    controller, window = desktop
    controller.start()
    until(lambda: bool(controller.projects), controller.changed)
    click(window, "toggleRightSidebar")
    until(lambda: bool(find_item(window, "file_large.pdf")), window.frameSwapped)
    QTest.qWait(80)
    samples = []
    previous = perf_counter()
    heartbeat = QTimer()
    heartbeat.setInterval(16)

    def beat():
        nonlocal previous
        now = perf_counter()
        samples.append(now - previous)
        previous = now

    heartbeat.timeout.connect(beat)
    heartbeat.start()
    process = psutil.Process()
    before = process.memory_info().rss
    render_requests = []
    render = type(controller.pdf_images).requestImage

    def count_render(provider, image_id, size, requested_size):
        if provider is controller.pdf_images:
            render_requests.append((image_id.split("/")[1], requested_size.width(), requested_size.height()))
        return render(provider, image_id, size, requested_size)

    monkeypatch.setattr(type(controller.pdf_images), "requestImage", count_render)
    start = perf_counter()
    click(window, "file_large.pdf")
    until(lambda: find_item(window, "pdfView") is not None, window.frameSwapped)
    view = find_item(window, "pdfView")

    def page_ready(number):
        return view.property("currentPage") == number and any(
            image.property("currentFrame") == number
            and view.property("currentPageRenderingStatus") == 1
            and not visible_rect(window, image).isEmpty()
            for image in pdf_page_images(view)
        )

    until(lambda: page_ready(0), window.frameSwapped)
    frame = QSignalSpy(window.frameSwapped)
    window.update()
    assert frame.count() or frame.wait(2000)
    first_page_ms = (perf_counter() - start) * 1000
    initial_page_renders = [request for request in render_requests if request[0] == "0"]
    first_image = next(image for image in pdf_page_images(view) if image.property("currentFrame") == 0)
    displayed_pixels = (first_image.width() * window.devicePixelRatio(), first_image.height() * window.devicePixelRatio())
    assert find_item(window, "pdfPages").property("rows") == 1000
    assert "1000" in find_item(window, "pdfPageCount").property("text")
    live_images = [len(pdf_page_images(view))]
    jumps = []
    for number in (499, 999, 5, 800, 0):
        page = find_item(window, "pdfPageNumber")
        page.forceActiveFocus()
        page.setProperty("text", str(number + 1))
        start = perf_counter()
        QTest.keyClick(window, Qt.Key.Key_Return)
        until(lambda number=number: page_ready(number), window.frameSwapped)
        jumps.append((perf_counter() - start) * 1000)
        live_images.append(len(pdf_page_images(view)))
    QTest.qWait(32)
    heartbeat.stop()
    metrics = {
        "pages": 1000,
        "file_bytes": source.stat().st_size,
        "first_page_ms": round(first_page_ms),
        "initial_page_renders": initial_page_renders,
        "displayed_page_pixels": [round(value) for value in displayed_pixels],
        "jump_ms": [round(value) for value in jumps],
        "live_page_images": live_images,
        "max_ui_pause_ms": round(max(samples) * 1000),
        "rss_growth_mib": round((process.memory_info().rss - before) / 1024**2, 1),
        "renderer_rss_mib": round(psutil.Process(controller.pdf_images._process.pid).memory_info().rss / 1024**2, 1),
    }
    print("PDF_BENCHMARK", json.dumps(metrics))
    (tmp_path / "pdf-benchmark.json").write_text(json.dumps(metrics))
    save_screenshot(window, "pdf-thousand-pages")
    click(window, "closeInspectorTab_0")
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert not isValid(view)
    assert first_page_ms < 500 and max(jumps) < 500 and max(samples) < 0.15, metrics
    assert len(initial_page_renders) == 1, metrics
    assert all(abs(actual - displayed) <= window.devicePixelRatio() + 1
               for actual, displayed in zip(initial_page_renders[0][1:], displayed_pixels, strict=True)), metrics
    assert max(live_images) <= 8, metrics


def test_desktop_inspector_tabs_preserve_files_pages_and_release_closed_tabs(
    desktop, model_server, project, home
):
    (project / "first.py").write_text("# First file\nprint('one')\n" * 80)
    (project / "second.py").write_text("# Second file\nprint('two')\n")
    controller, window = desktop
    controller.start()
    until(lambda: bool(controller.projects), controller.changed)
    click(window, "toggleRightSidebar")
    until(lambda: bool(find_item(window, "file_first.py")), window.frameSwapped)
    click(window, "file_first.py")
    first = find_item(window, "filePane")
    first_lines = find_item(window, "codeLines")
    first_lines.setProperty("contentY", 120)
    tree_panel = find_item(window, "fileTreePanel")
    old_width = tree_panel.width()
    origin = tree_panel.mapToScene(QPointF(old_width + 2, 100)).toPoint()
    end = origin + QPointF(40, 0).toPoint()
    QTest.mousePress(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, origin)
    QTest.mouseMove(window, end, 30)
    QTest.mouseRelease(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, end)
    QCoreApplication.processEvents()
    assert tree_panel.width() > old_width + 25
    click(window, "addInspectorTab")
    click(window, "newFilesTab")
    until(lambda: find_item(window, "filePane") != first, window.frameSwapped)
    until(lambda: bool(find_item(window, "file_second.py")), window.frameSwapped)
    click(window, "file_second.py")
    second = find_item(window, "filePane")
    assert second != first
    assert first.property("fileState")["name"] == "first.py"
    assert second.property("fileState")["name"] == "second.py"
    assert (
        find_item(window, "fileTreePanel").isVisible()
        and find_item(window, "fileContentPanel").isVisible()
    )
    click(window, "filesTab")
    assert find_item(window, "filePane") == first
    assert first_lines.property("contentY") == 120
    assert find_item(window, "codePreview").property("text").startswith("# First file")

    click(window, "browserTab")
    first_browser = find_item(window, "webBrowser")
    url = json.loads((home / "settings.json").read_text())["providers"]["desktop-test"][
        "base_url"
    ].removesuffix("/v1")
    address = find_item(window, "browserAddress")
    address.setProperty("text", url + "/preview")
    address.forceActiveFocus()
    QTest.keyClick(window, Qt.Key.Key_Return)
    until(lambda: first_browser.property("title") == "Browser ready", first_browser.titleChanged)
    click(window, "addInspectorTab")
    click(window, "newBrowserTab")
    second_browser = find_item(window, "webBrowser")
    assert second_browser != first_browser
    address = find_item(window, "browserAddress")
    address.setProperty("text", url + "/next")
    address.forceActiveFocus()
    QTest.keyClick(window, Qt.Key.Key_Return)
    until(lambda: second_browser.property("title") == "Next page", second_browser.titleChanged)
    assert first_browser.property("title") == "Browser ready"
    until(lambda: not second_browser.property("loading"), second_browser.loadingChanged)
    QTest.qWait(80)
    save_screenshot(window, "multiple-tabs")
    click(window, "closeInspectorTab_3")
    click(window, "browserTab")
    assert find_item(window, "webBrowser") == first_browser
    before = len(controller._preview_objects)
    click(window, "closeInspectorTab_2")
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert len(controller._preview_objects) == before - 2
    assert not controller.error


def test_desktop_browser_media_playback_and_unsupported_format_notice(desktop, model_server, home):
    controller, window = desktop
    controller.start()
    until(lambda: bool(controller.projects), controller.changed)
    click(window, "toggleRightSidebar")
    click(window, "browserTab")
    browser = find_item(window, "webBrowser")
    url = json.loads((home / "settings.json").read_text())["providers"]["desktop-test"][
        "base_url"
    ].removesuffix("/v1")

    def navigate(path):
        address = find_item(window, "browserAddress")
        address.setProperty("text", url + path)
        address.forceActiveFocus()
        QTest.keyClick(window, Qt.Key.Key_Return)

    navigate("/video-webm")
    until(lambda: browser.property("title") == "Video ready", browser.titleChanged, timeout=20_000)
    assert not find_item(window, "browserMediaNotice").isVisible()
    navigate("/video-mp4")
    until(
        lambda: browser.property("title") in ("Video ready", "Media failed"),
        browser.titleChanged,
        timeout=20_000,
    )
    if browser.property("title") == "Media failed":
        until(lambda: find_item(window, "browserMediaNotice").isVisible(), window.frameSwapped)
        assert "H.264" in find_item(window, "browserPane").property("mediaProblem")
        assert find_item(window, "openMediaExternally").isVisible()
        save_screenshot(window, "unsupported-media")
    navigate("/preview")
    until(lambda: browser.property("title") == "Browser ready", browser.titleChanged)
    assert not find_item(window, "browserMediaNotice").isVisible()


def test_desktop_transcript_renders_markdown_tools_and_expands_complete_output(
    desktop, model_server
):
    controller, window = desktop
    controller.start()
    until(lambda: bool(controller.projects), controller.changed)
    click(window, "newChatButton")
    until(lambda: controller.connected, controller.changed)
    body = (
        "# 项目状态\n\n**已完成** 中文优化，*正在测试*。\n\n- 第一项\n- 第二项\n\n> 保留清晰的引用。\n\n```python\nprint('你好 👋')\nreturn 42\n```\n\n| 功能 | 状态 |\n| --- | --- |\n| Markdown | 已验证 |\n\n"
        + "详细说明，保留所有内容。\n\n" * 30
    )
    controller._transcript.append("tool", "read", body, '{"path":"STATUS.md"}')
    QTest.qWait(100)
    assert not find_item(window, "messageBody")
    save_screenshot(window, "transcript-collapsed")
    click(window, "expandMessage")
    until(lambda: bool(find_item(window, "markdownCodeBackground")), window.frameSwapped)
    output = find_item(window, "messageBody")
    quick_document = output.property("textDocument")
    document = quick_document.textDocument()
    assert "**已完成**" not in document.toPlainText()
    assert "# 项目状态" not in document.toPlainText()
    assert document.begin().blockFormat().headingLevel() == 1
    bold = document.find("已完成")
    assert bold.charFormat().fontWeight() >= 600
    assert not document.find("正在测试").charFormat().fontWeight() >= 600
    table = next(
        frame for frame in document.rootFrame().childFrames() if isinstance(frame, QTextTable)
    )
    assert (table.rows(), table.columns()) == (2, 2)
    assert (
        document.find("return").charFormat().foreground()
        != document.find("42").charFormat().foreground()
    )
    code_box = find_item(window, "markdownCodeBackground")
    quote_border = find_item(window, "markdownQuoteBorder")
    assert code_box.width() >= output.width() - 1 and code_box.height() > 30
    assert quote_border.width() == 3 and quote_border.height() > 10
    click(window, "copyActivityOutput")
    assert QGuiApplication.clipboard().text() == body
    assert "详细说明" in document.toPlainText()
    assert document.toPlainText().count("详细说明") == 30
    save_screenshot(window, "transcript-markdown")
    # A structural document check alone misses an opaque item covering native glyphs.
    if QGuiApplication.platformName() != "offscreen":
        frame = window.grabWindow()
        scale = frame.width() / window.width()
        top = code_box.mapToScene(QPointF(14, 5))
        region = frame.copy(
            int(top.x() * scale),
            int(top.y() * scale),
            int(180 * scale),
            int((code_box.height() - 10) * scale),
        )
        background = code_box.property("color").lightnessF()
        assert (
            sum(
                abs(region.pixelColor(x, y).lightnessF() - background) > 0.3
                for y in range(region.height())
                for x in range(region.width())
            )
            > 100
        )


def test_desktop_markdown_streaming_code_blocks_and_source_files(desktop, model_server):
    controller, window = desktop
    controller.start()
    until(lambda: bool(controller.projects), controller.changed)
    click(window, "newChatButton")
    until(lambda: controller.connected, controller.changed)
    body = (
        "## 渲染检查\n\n**中文粗体**与 `inline_code`。\n\n"
        "```python\nprint('你好 👋')\n```\n\n"
        '```json\n{"ready": true}\n```\n\n'
        "    indented_code()\n\n"
        "| 名称 | 内容 |\n| --- | --- |\n| 文件 | **已完成** |\n"
    )
    row = controller._transcript.append("assistant", "Ava", body[:35])
    until(lambda: bool(find_item(window, "assistantMarkdown")), window.frameSwapped)
    controller._transcript.update(row, body=body)
    output = find_item(window, "assistantMarkdown")
    until(lambda: '"ready"' in output.property("text"), window.frameSwapped)

    def decorations():
        value = output.property("decorations")
        return value.toVariant() if isinstance(value, QJSValue) else value

    until(lambda: bool(decorations()), window.frameSwapped)
    save_screenshot(window, "markdown-streaming")
    assert [d["text"] for d in decorations() if d["kind"] == "code"] == [
        "print('你好 👋')",
        '{"ready": true}',
        "indented_code()",
    ]
    quick_document = output.property("textDocument")
    document = quick_document.textDocument()
    assert document.find("中文粗体").charFormat().fontWeight() >= 600
    controller._transcript.clear()
    source = "# Python comment\nvalue = '**keep these characters**'\n"
    controller._transcript.append("tool", "read", source, '{"path":"example.py"}')
    click(window, "expandMessage")
    until(lambda: bool(find_item(window, "messageBody")), window.frameSwapped)
    quick_source = find_item(window, "messageBody").property("textDocument")
    assert quick_source.textDocument().toPlainText() == source


def test_desktop_file_tree_watches_changes_and_bounds_previews(
    desktop, model_server, project, tmp_path
):
    controller, window = desktop
    folder = project / "nested"
    folder.mkdir()
    (folder / "inside.txt").write_text("inside")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.txt").write_text("outside")
    (project / "external").symlink_to(outside, target_is_directory=True)
    controller.start()
    until(lambda: bool(controller.projects), controller.changed)
    assert not controller._preview_objects  # The hidden inspector has no file models.
    click(window, "toggleRightSidebar")
    until(lambda: bool(find_item(window, "file_nested")), window.frameSwapped)
    click(window, "file_nested")
    until(lambda: bool(find_item(window, "file_inside.txt")), window.frameSwapped)
    click(window, "file_inside.txt")
    assert controller.fileState["text"] == "inside"
    added = folder / "added.txt"
    added.write_text("new file")
    until(lambda: bool(find_item(window, "file_added.txt")), window.frameSwapped)
    click(window, "file_added.txt")
    assert controller.fileState["text"] == "new file"
    tree = find_item(window, "fileTree")
    rows = tree.property("rows")
    click(window, "file_external")
    QTest.qWait(100)
    assert tree.property("rows") == rows
    assert not find_item(window, "file_private.txt")
    controller.browseFiles(str(outside / "private.txt"))
    assert "inside this project" in controller.error
    controller.dismissError()
    binary = project / "binary.dat"
    binary.write_bytes(b"\x00binary")
    controller.browseFiles(str(binary))
    assert controller.fileState["kind"] == "unsupported"
    huge = project / "huge.txt"
    huge.write_bytes(b"x" * (1024 * 1024 + 1))
    controller.browseFiles(str(huge))
    assert "1 MiB" in controller.fileState["notice"]
    fifo = project / "pipe"
    os.mkfifo(fifo)
    controller.browseFiles(str(fifo))
    assert "regular file" in controller.error
    controller.dismissError()


def test_desktop_remove_project_hides_history_without_stopping_tasks(
    desktop, model_server, project, home
):
    controller, window = desktop
    preserved = project / "保留文件.txt"
    preserved.write_text("Project removal must leave this file intact.\n")
    controller.start()
    until(lambda: bool(controller.projects), controller.changed)
    click(window, "newChatButton")
    until(lambda: controller.connected, controller.changed)
    first, project_id = controller.chatId, controller.projectId
    type_message(window, "后台任务在隐藏项目后继续运行")
    QTest.keyClick(window, Qt.Key.Key_Return)
    until(lambda: any(row["body"] == "Hello " for row in controller._transcript.rows), controller.changed)
    click(window, "sessionMenu_" + first)
    click(window, "pinChatAction")
    frame = QSignalSpy(window.frameSwapped)
    window.update()
    assert frame.count() or frame.wait(2000)
    group = find_item(window, "projectGroup_" + project_id)
    QTest.mouseClick(window, Qt.MouseButton.RightButton, pos=visible_rect(window, group).center().toPoint())
    action = find_item(window, "removeProjectAction")
    assert action is not None and action.isVisible(), "Projects need a discoverable Remove from Ava action"
    click(window, "removeProjectAction")
    until(lambda: not controller.projects, controller.navigationChanged)
    assert not controller.projectId and not controller.chatId and not controller._transcript.rows
    assert not controller.searchChats("", False) and not controller.searchChats("", True)
    until(lambda: not find_item(window, "session_" + first), window.frameSwapped)
    assert find_item(window, "emptyStateAction").property("text") == "Add project"
    assert find_item(window, "emptyStateAction").property("enabled")
    assert preserved.read_text() == "Project removal must leave this file intact.\n"
    with httpx.Client(
        base_url=controller._connection._base,
        headers={"Authorization": "Bearer " + controller._connection._token},
        trust_env=False,
    ) as client:
        assert client.get(f"/api/chats/{first}").json()["status"] == "running"
        assert client.get("/api/projects").json()["projects"] == []
        model_server[0].release.set()
        until(
            lambda: client.get(f"/api/chats/{first}").json()["status"] == "idle",
            controller._heartbeat.timeout,
        )
    save_screenshot(window, "project-removed")
    controller._detach()
    stop_backend(home, force=True)
    controller.start()
    until(lambda: controller.online, controller.changed)
    # Startup from the removed directory must not implicitly add it back.
    until(lambda: controller._navigation_revision >= 0, controller.changed)
    assert not controller.projects
    controller.addProject(str(project))
    until(lambda: controller.connected and controller.chatId == first, controller.changed)
    until(lambda: any(row["body"] == "Hello 世界" for row in controller._transcript.rows), controller.changed)
    assert controller.projectId == project_id
    assert controller.preference("pinned/" + first, False)
    assert preserved.read_text() == "Project removal must leave this file intact.\n"
    assert len(model_server) == 1
    save_screenshot(window, "project-restored")


def test_desktop_remove_project_undo_and_other_client_keep_selection_and_drafts(
    desktop, model_server, project, home, tmp_path
):
    controller, window = desktop
    controller.start()
    until(lambda: bool(controller.projects), controller.changed)
    click(window, "newChatButton")
    until(lambda: controller.connected, controller.changed)
    first, first_project = controller.chatId, controller.projectId
    controller.renameChat(first, "Keep this conversation")
    until(lambda: controller.chatTitle == "Keep this conversation", controller.changed)
    other = tmp_path / "另一个项目：保持文件与全部会话历史"
    other.mkdir()
    controller.addProject(str(other))
    until(lambda: controller.projectPath == str(other), controller.changed)
    click(window, "newChatButton")
    until(lambda: controller.connected and controller.chatId != first, controller.changed)
    second, second_project = controller.chatId, controller.projectId
    click(window, "sessionMenu_" + second)
    click(window, "pinChatAction")
    type_message(window, "Keep this unsent draft")
    click(window, "projectMenu_" + first_project)
    click(window, "removeProjectAction")
    until(lambda: len(controller.projects) == 1, controller.navigationChanged)
    assert controller.chatId == second
    assert controller.draft == "Keep this unsent draft"
    until(lambda: bool(find_item(window, "undoRemoveProject")), window.frameSwapped)
    click(window, "undoRemoveProject")
    until(lambda: controller.connected and controller.chatId == first, controller.changed)
    click(window, "session_" + second)
    until(lambda: controller.connected and controller.chatId == second, controller.changed)
    assert controller.draft == "Keep this unsent draft"
    window.setProperty("dark", True)
    window.setWidth(800)
    click(window, "projectMenu_" + second_project)
    menu_action = find_item(window, "removeProjectAction")
    menu_button = find_item(window, "projectMenu_" + second_project)
    until(
        lambda: visible_rect(window, menu_action).left() < visible_rect(window, menu_button).right() + 12,
        window.frameSwapped,
    )
    save_screenshot(window, "project-menu-dark-narrow")
    QTest.keyClick(window, Qt.Key.Key_Escape)
    # Another desktop can hide the selected project. Its next heartbeat clears it.
    with httpx.Client(
        base_url=controller._connection._base,
        headers={"Authorization": "Bearer " + controller._connection._token},
        trust_env=False,
    ) as client:
        archived = client.post("/api/chats", json={"project_id": second_project}).json()["id"]
        client.post(f"/api/chats/{archived}/archive", json={"archived": True})
        before = {p: p.read_bytes() for p in home.rglob("*.jsonl*")}
        assert before
        client.post(f"/api/projects/{second_project}/hide")
        until(
            lambda: controller.connected and controller.chatId == first and len(controller.projects) == 1,
            controller.changed,
        )
        assert not controller.searchChats(other.name, False)
        assert not controller.searchChats(other.name, True)
        assert all(row["id"] != second for row in controller.sessionRows)
        assert {p: p.read_bytes() for p in before} == before
        restored = client.post("/api/projects", json={"path": str(other)}).json()
        assert {chat["id"] for chat in restored["chats"]} == {second, archived}
        until(lambda: len(controller.projects) == 2, controller.navigationChanged)
    click(window, "session_" + second)
    until(lambda: controller.connected and controller.chatId == second, controller.changed)
    assert controller.draft == "Keep this unsent draft"
    assert {p: p.read_bytes() for p in before} == before
    assert not model_server and not controller.error
    save_screenshot(window, "project-undo-dark-narrow")

    # A long sidebar still opens its last project's menu inside a small window.
    with httpx.Client(
        base_url=controller._connection._base,
        headers={"Authorization": "Bearer " + controller._connection._token},
        trust_env=False,
    ) as client:
        for index in range(12):
            folder = tmp_path / f"Additional project {index}"
            folder.mkdir()
            last = client.post("/api/projects", json={"path": str(folder)}).json()["id"]
    controller.refresh()
    until(lambda: len(controller.projects) == 14, controller.navigationChanged)
    window.setHeight(600)
    frame = QSignalSpy(window.frameSwapped)
    window.update()
    assert frame.count() or frame.wait(2000)
    sessions = find_item(window, "sessionList")
    # A queued frame can predate the resize's layout. Prepare the bottom-edge
    # menu scenario only once the viewport fits the resized window.
    until(lambda: visible_rect(window, sessions).height() >= sessions.height() - 1, window.frameSwapped)
    QMetaObject.invokeMethod(sessions, "forceLayout")
    QMetaObject.invokeMethod(sessions, "positionViewAtEnd")
    until(
        lambda: (item := find_item(window, "projectMenu_" + last)) is not None
        and not visible_rect(window, item).isEmpty(),
        window.frameSwapped,
    )
    click(window, "projectMenu_" + last)
    action = find_item(window, "removeProjectAction")
    frame = QSignalSpy(window.frameSwapped)
    window.update()
    assert frame.count() or frame.wait(2000)
    rectangle = action.mapRectToScene(action.boundingRect())
    assert QRectF(0, 0, window.width(), window.height()).contains(rectangle)
    save_screenshot(window, "project-menu-bottom-edge")
    QTest.keyClick(window, Qt.Key.Key_Escape)


def test_desktop_pin_moves_chats_above_project_groups(desktop, model_server, project, tmp_path):
    controller, window = desktop
    controller.start()
    until(lambda: bool(controller.projects), controller.changed)
    click(window, "newChatButton")
    until(lambda: controller.connected, controller.changed)
    first, first_project = controller.chatId, controller.projectId
    controller.renameChat(first, "中文置顶会话")
    until(lambda: controller.chatTitle == "中文置顶会话", controller.changed)
    other = tmp_path / "Second project"
    other.mkdir()
    controller.addProject(str(other))
    until(lambda: controller.projectPath == str(other), controller.changed)
    click(window, "newChatButton")
    until(lambda: controller.connected and controller.chatId != first, controller.changed)
    second, second_project = controller.chatId, controller.projectId

    click(window, "sessionMenu_" + first)
    click(window, "pinChatAction")
    save_screenshot(window, "pin-first")
    rows = controller.sessionRows
    assert next(i for i, row in enumerate(rows) if row["id"] == first) < next(
        i for i, row in enumerate(rows) if row["kind"] == "project"
    ), "Pinned conversations must move above every project group"
    until(lambda: bool(find_item(window, "pinnedSection")), window.frameSwapped)
    pinned = find_item(window, "session_" + first)
    assert pinned.mapToScene(QPointF()).y() < find_item(
        window, "projectGroup_" + first_project
    ).mapToScene(QPointF()).y()
    assert find_item(window, "pinnedProject_" + first).property("text") == project.name
    click(window, "sessionMenu_" + second)
    click(window, "pinChatAction")
    assert controller.preference("pinned/" + second, False)
    click(window, "projectGroup_" + first_project)
    click(window, "projectGroup_" + second_project)
    for identity in (first, second):
        assert find_item(window, "session_" + identity).isVisible()
        assert sum(row["id"] == identity for row in controller.sessionRows) == 1
    click(window, "session_" + first)
    until(lambda: controller.connected and controller.chatId == first, controller.changed)
    assert controller.projectId == first_project
    save_screenshot(window, "pin-projects-collapsed")
    window.setProperty("dark", True)
    window.setWidth(800)
    frame = QSignalSpy(window.frameSwapped)
    window.update()
    assert frame.count() or frame.wait(2000)
    save_screenshot(window, "pin-dark-narrow")

    # A newly constructed controller reads persisted pins and collapsed groups.
    controller.settings.sync()
    restored = Controller(
        project, [], QSettings(controller.settings.fileName(), QSettings.Format.IniFormat)
    )
    restored_engine = create_engine(restored)
    restored_window = restored_engine.rootObjects()[0]
    assert isinstance(restored_window, QQuickWindow)
    try:
        restored.start()
        until(lambda: restored.online and bool(restored.property("projects")), restored.changed)
        until(lambda: bool(find_item(restored_window, "session_" + second)), restored_window.frameSwapped)
        assert restored.property("sessionRows") == controller.sessionRows
        assert find_item(restored_window, "pinnedSection").isVisible()
    finally:
        restored_window.close()
        until(lambda: restored._closed_emitted, restored.closed)
        restored_engine.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    click(window, "sessionMenu_" + first)
    assert find_item(window, "pinChatAction").property("text") == "Unpin chat"
    click(window, "pinChatAction")
    assert controller.preference("groups/" + first_project, False)
    rows = controller.sessionRows
    assert next(i for i, row in enumerate(rows) if row["id"] == first) > next(
        i for i, row in enumerate(rows) if row["id"] == first_project
    )
    assert find_item(window, "session_" + first).isVisible()
    click(window, "sessionMenu_" + second)
    click(window, "pinChatAction")
    until(lambda: not find_item(window, "pinnedSection"), window.frameSwapped)
    assert controller.preference("groups/" + second_project, False)
    save_screenshot(window, "pin-cleared")
    assert not controller.error and not model_server


def test_desktop_search_manage_and_restore_conversations(desktop, model_server, project, tmp_path):
    controller, window = desktop
    controller.start()
    until(lambda: bool(controller.projects), controller.changed)
    click(window, "newChatButton")
    until(lambda: controller.connected, controller.changed)
    first = controller.chatId
    click(window, "sessionMenu_" + first)
    click(window, "renameChatAction")
    name = find_item(window, "chatNameField")
    name.setProperty("text", "中文界面审查")
    click(window, "saveChatName")
    until(lambda: controller.chatTitle == "中文界面审查", controller.changed)
    click(window, "sessionMenu_" + first)
    click(window, "pinChatAction")
    assert next(row for row in controller.sessionRows if row["id"] == first)["pinned"]
    second_project = tmp_path / "second-project"
    second_project.mkdir()
    controller.addProject(str(second_project))
    until(lambda: controller.projectPath == str(second_project), controller.changed)
    click(window, "newChatButton")
    until(lambda: controller.connected and controller.chatId != first, controller.changed)
    click(window, "searchChatsButton")
    search = find_item(window, "chatSearchField")
    search.setProperty("text", "中文界面")
    until(lambda: bool(find_item(window, "searchChat_" + first)), window.frameSwapped)
    click(window, "searchChat_" + first)
    until(lambda: controller.connected and controller.chatId == first, controller.changed)
    assert controller.projectPath == str(project)
    click(window, "sessionMenu_" + first)
    click(window, "archiveChatAction")
    until(
        lambda: all(row["id"] != first for row in controller.sessionRows),
        controller.navigationChanged,
    )
    click(window, "searchChatsButton")
    click(window, "searchArchivedToggle")
    find_item(window, "chatSearchField").setProperty("text", "中文界面")
    until(lambda: bool(find_item(window, "restoreChat_" + first)), window.frameSwapped)
    click(window, "restoreChat_" + first)
    until(
        lambda: any(row["id"] == first for row in controller.sessionRows),
        controller.navigationChanged,
    )
    assert not controller.error
    save_screenshot(window, "search-and-restore")


def test_desktop_activity_is_lazy_and_preserves_reading_position(desktop, model_server):
    controller, window = desktop
    controller.start()
    until(lambda: bool(controller.projects), controller.changed)
    click(window, "newChatButton")
    until(lambda: controller.connected, controller.changed)
    model = controller._transcript
    for index in range(40):
        model.append(
            "tool",
            "read",
            "# Output\n\n" + "A complete result.\n" * 10000,
            f'{{"path":"module-{index}.md"}}',
        )
    until(lambda: bool(find_item(window, "activityGroupSummary")), window.frameSwapped)
    assert not find_item(window, "messageBody")
    assert not controller._preview_objects
    view = find_item(window, "transcriptView")
    QTest.qWait(60)
    print("ACTIVITY_GROUPS", view.property("count"), view.property("contentHeight"))
    save_screenshot(window, "activity-groups")
    assert view.property("count") == 1
    assert view.property("contentHeight") < 40
    pending = model.append("tool", "bash", "Running…", '{"command":"test"}')
    until(lambda: find_item(window, "activityGroupRunning").isVisible(), window.frameSwapped)
    assert view.property("count") == 1
    model.update(pending, kind="error", body="Command failed")
    until(lambda: find_item(window, "activityGroupFailed").isVisible(), window.frameSwapped)
    assert not find_item(window, "activityGroupRunning").isVisible()
    click(window, "activityGroupToggle")
    until(lambda: view.property("count") == 41, window.frameSwapped)
    view.setProperty("follow", False)
    QMetaObject.invokeMethod(view, "positionViewAtBeginning")
    QTest.qWait(60)
    click(window, "expandMessage")
    until(lambda: bool(find_item(window, "activityCodePreview")), window.frameSwapped)
    assert len(controller._preview_objects) == 1
    click(window, "copyActivityOutput")
    assert QGuiApplication.clipboard().text() == model.rows[0]["body"]
    QMetaObject.invokeMethod(view, "positionViewAtEnd")
    until(lambda: not controller._preview_objects, window.frameSwapped)
    QMetaObject.invokeMethod(view, "positionViewAtBeginning")
    until(lambda: bool(find_item(window, "activityCodePreview")), window.frameSwapped)
    click(window, "activityGroupToggle")
    until(lambda: view.property("count") == 1, window.frameSwapped)
    assert not controller._preview_objects
    click(window, "activityGroupToggle")
    until(lambda: view.property("count") == 41, window.frameSwapped)
    QMetaObject.invokeMethod(view, "positionViewAtBeginning")
    QTest.qWait(60)
    until(lambda: bool(find_item(window, "activityCodePreview")), window.frameSwapped)
    click(window, "expandMessage")
    assert not controller._preview_objects
    QTest.qWait(30)
    position = view.property("contentY")
    model.append("assistant", "Ava", "More progress while you read.")
    QTest.qWait(60)
    assert abs(view.property("contentY") - position) < 1
    assert find_item(window, "jumpToLatest").isVisible()
    click(window, "jumpToLatest")
    until(lambda: view.property("atYEnd"), window.frameSwapped)
    assert view.property("follow")
    model.append("error", "bash", "Invalid command argument", '{"command":123}')
    model.append("error", "read", "Invalid read argument", "null")
    save_screenshot(window, "activity")


def test_desktop_variable_message_heights_settle_at_latest(desktop, model_server):
    controller, window = desktop
    controller.start()
    until(lambda: bool(controller.projects), controller.changed)
    click(window, "newChatButton")
    until(lambda: controller.connected, controller.changed)
    model = controller._transcript
    view = find_item(window, "transcriptView")
    model.append("user", "You", "项目状态如何")
    for step in range(5):
        model.append("assistant", "Ava", "正在检查项目状态。" * (step + 1))
        for _ in range(4):
            model.append("tool", "read", "A completed tool result", '{"path":"notes.md"}')
    model.append(
        "assistant", "Ava",
        "# 项目进展\n\n" + ("## 已完成的工作\n\n**验证结果**清晰可读。\n\n- 第一项\n- 第二项\n\n" * 25),
    )
    for _ in range(3):
        model.append("reasoning", "Thinking", "Completed reasoning")
    QTest.qWait(700)
    positions = []
    for _ in range(20):
        QTest.qWait(20)
        positions.append(view.property("contentY"))
    assert max(positions) - min(positions) < 1, positions
    assert view.property("atYEnd")
    streaming = model.append("assistant", "Ava", "## 后续更新\n\n")
    for paragraphs in range(1, 5):
        model.update(streaming, body="## 后续更新\n\n" + "一段新的状态说明。\n\n" * paragraphs)
        QTest.qWait(25)
    until(lambda: view.property("atYEnd"), window.frameSwapped)
    # Real wheel input ends follow mode; later output must not pull the reader back.
    position = view.mapToScene(QPointF(view.width() / 2, view.height() / 2))
    QTest.wheelEvent(window, position, QPoint(0, 1200))
    until(lambda: not view.property("moving"), view.movingChanged)
    assert not view.property("follow")
    assert view.property("currentIndex") == -1
    reading_y = view.property("contentY")
    model.update(streaming, body="## 后续更新\n\n" + "一段新的状态说明。\n\n" * 15)
    QTest.qWait(100)
    assert abs(view.property("contentY") - reading_y) < 1
    click(window, "jumpToLatest")
    until(lambda: view.property("atYEnd"), window.frameSwapped)
    assert view.property("follow")
    save_screenshot(window, "stable-variable-history")


def test_desktop_provider_settings_save_validate_and_reopen(desktop, model_server, home):
    controller, window = desktop
    controller.start()
    until(lambda: bool(controller.projects), controller.changed)
    click(window, "newChatButton")
    until(lambda: controller.connected, controller.changed)
    click(window, "settingsButton")
    click(window, "settingsProvidersTab")
    until(
        lambda: window.findChild(QObject, "settingsDialog").property("ready"),
        controller.providerSettingsChanged,
    )
    assert find_item(window, "settingsModel") is None
    assert find_item(window, "settingsEffort") is None
    assert find_item(window, "applySettingsToChat") is None
    url = find_item(window, "settingsBaseUrl")
    key = find_item(window, "settingsApiKey")
    original = (home / "settings.json").read_text()
    original_url = url.property("text")
    assert "verified" in find_item(window, "providerConnectionStatus").property("text")
    assert find_item(window, "settingsProviderName").property("text") == "desktop-test"
    save_screenshot(window, "provider-settings")
    key.setProperty("text", "desktop-test-new-key")
    key.forceActiveFocus()
    QTest.keySequence(window, QKeySequence(QKeySequence.StandardKey.SelectAll))
    QGuiApplication.clipboard().setText("clipboard remains private")
    open_text_context(window, key, keyboard=True)
    surface, copy = text_menu_item("Copy")
    assert not copy.property("enabled")
    assert not text_menu_item("Cut")[1].property("enabled")
    QTest.keyClick(surface, Qt.Key.Key_Escape)
    assert QGuiApplication.clipboard().text() == "clipboard remains private"
    url.setProperty("text", "http://remote.invalid/v1")
    click(window, "saveProviderSettings")
    until(
        lambda: bool(controller.providerSettingsState["error"]), controller.providerSettingsChanged
    )
    assert "HTTPS" in controller.providerSettingsState["error"]
    assert key.property("text") == "desktop-test-new-key"
    assert (home / "settings.json").read_text() == original
    save_screenshot(window, "settings-validation")
    url.setProperty("text", original_url)
    click(window, "saveProviderSettings")
    until(
        lambda: bool(controller.providerSettingsState["notice"]), controller.providerSettingsChanged
    )
    assert controller.selection["model"] == "fixture"
    assert controller.selection["effort"] is None
    assert not key.property("text")
    stored = json.loads((home / "settings.json").read_text())
    assert stored["model"] == "fixture"
    assert (
        stored["providers"]["desktop-test"]["models"]
        == json.loads(original)["providers"]["desktop-test"]["models"]
    )
    assert "desktop-test-new-key" not in (home / "settings.json").read_text()
    assert (
        json.loads((home / "auth.json").read_text())["desktop-test"]["key"]
        == "desktop-test-new-key"
    )
    click(window, "closeSettingsButton")
    click(window, "settingsButton")
    until(
        lambda: window.findChild(QObject, "settingsDialog").property("ready"),
        controller.providerSettingsChanged,
    )
    assert not key.property("text")

    def choose(name, index):
        click(window, name)
        QTest.keyClick(window, Qt.Key.Key_Home)
        for _ in range(index):
            QTest.keyClick(window, Qt.Key.Key_Down)
        QTest.keyClick(window, Qt.Key.Key_Return)

    connections = window.findChild(QObject, "settingsDialog").property("connections").toVariant()
    choose("settingsProviderPicker", next(i for i, item in enumerate(connections) if item["id"] == "codex"))
    assert find_item(window, "settingsProviderPicker").property("currentValue") == "codex"
    assert not key.isVisible()
    save_screenshot(window, "settings-codex-login")
    click(window, "customProviderType")
    find_item(window, "settingsProviderName").setProperty("text", "desktop-second")
    url.setProperty("text", original_url)
    key.setProperty("text", "second-test-key")

    def reveal(name):
        item = find_item(window, name)
        scroll = find_item(window, "settingsProviderScroll")
        offset = item.mapToItem(scroll, QPointF(0, 0)).y()
        scroll.setProperty(
            "contentY",
            min(
                max(0, scroll.property("contentY") + offset - 8),
                max(0, scroll.property("contentHeight") - scroll.height()),
            ),
        )
        QTest.qWait(30)

    click(window, "saveProviderSettings")
    until(
        lambda: bool(controller.providerSettingsState["notice"]), controller.providerSettingsChanged
    )
    assert controller.selection["provider"] == "desktop-test"
    assert json.loads((home / "settings.json").read_text())["provider"] == "desktop-test"
    click(window, "closeSettingsButton")
    click(window, "modelButton")
    until(lambda: len(controller.modelChoices.get("providers", [])) == 2, controller.changed)
    assert [item["id"] for item in controller.modelChoices["providers"]] == ["desktop-test", "desktop-second"]
    choose("conversationProviderPicker", 1)
    assert not find_item(window, "effortPicker").property("enabled")
    assert controller.selection["provider"] == "desktop-test"
    click(window, "closeModelButton")
    assert controller.selection["provider"] == "desktop-test"
    click(window, "modelButton")
    until(lambda: len(controller.modelChoices.get("providers", [])) == 2, controller.changed)
    choose("conversationProviderPicker", 1)
    click(window, "applyConversationModel")
    until(lambda: controller.selection.get("provider") == "desktop-second", controller.changed)
    until(lambda: not window.findChild(QObject, "modelDialog").property("visible"), window.frameSwapped)
    save_screenshot(window, "conversation-provider")
    previous = controller.chatId
    click(window, "newChatButton")
    until(lambda: controller.connected and controller.chatId != previous, controller.changed)
    assert controller.selection["provider"] == "desktop-test"
    controller.openChat(previous)
    until(lambda: controller.chatId == previous and controller.connected, controller.changed)
    assert controller.selection["provider"] == "desktop-second"
    type_message(window, "Verify the saved provider")
    QTest.keyClick(window, Qt.Key.Key_Return)
    until(lambda: controller.status == "running" and len(model_server) == 1, controller.changed)
    assert model_server[0].request["model"] == "fixture"
    click(window, "settingsButton")
    until(
        lambda: window.findChild(QObject, "settingsDialog").property("ready"),
        controller.providerSettingsChanged,
    )
    assert find_item(window, "settingsModel") is None
    click(window, "saveProviderSettings")
    until(
        lambda: bool(controller.providerSettingsState["notice"]), controller.providerSettingsChanged
    )
    assert controller.selection["model"] == "fixture" and controller.status == "running"
    assert json.loads((home / "settings.json").read_text())["model"] == "fixture"
    click(window, "closeSettingsButton")
    model_server[0].release.set()
    until(lambda: controller.status == "idle", controller.changed)

    click(window, "settingsButton")
    until(
        lambda: window.findChild(QObject, "settingsDialog").property("ready"),
        controller.providerSettingsChanged,
    )
    reveal("removeProviderKey")
    click(window, "removeProviderKey")
    until(
        lambda: not controller.providerSettingsState["saving"] and not controller.providerSettingsState["loading"],
        controller.providerSettingsChanged,
    )
    assert "desktop-second" not in json.loads((home / "auth.json").read_text())
    assert not key.property("text")
    window.setWidth(800)
    window.setHeight(600)
    until(
        lambda: window.findChild(QObject, "settingsDialog").property("width") <= 752
        and not visible_rect(window, find_item(window, "saveProviderSettings")).isEmpty(),
        window.frameSwapped,
    )
    save_screenshot(window, "settings-narrow")
    assert not visible_rect(window, find_item(window, "saveProviderSettings")).isEmpty()
    click(window, "settingsGeneralTab")
    click(window, "darkThemeButton")
    click(window, "settingsProvidersTab")
    save_screenshot(window, "settings-provider-dark")


def test_desktop_settings_theme_and_keyboard_search(desktop, model_server):
    controller, window = desktop
    controller.start()
    until(lambda: bool(controller.projects), controller.changed)
    click(window, "newChatButton")
    until(lambda: controller.connected, controller.changed)
    first = controller.chatId
    type_message(window, "Keep this draft")
    click(window, "newChatButton")
    until(lambda: controller.connected and controller.chatId != first, controller.changed)
    click(window, "settingsButton")
    click(window, "readingSizePicker")
    QTest.keyClick(window, Qt.Key.Key_Down)
    QTest.keyClick(window, Qt.Key.Key_Return)
    assert controller.readingSize == 17
    click(window, "darkThemeButton")
    assert window.property("dark")
    controller.settings.sync()
    stored = QSettings(controller.settings.fileName(), QSettings.Format.IniFormat)
    assert stored.value("ui/dark", False, type=bool)
    save_screenshot(window, "settings-dark")
    click(window, "lightThemeButton")
    assert not window.property("dark")
    save_screenshot(window, "settings-light")
    click(window, "closeSettingsButton")
    controller._transcript.append("assistant", "Ava", "**更清晰的中文**与可调整字号。")
    until(lambda: bool(find_item(window, "assistantMarkdown")), window.frameSwapped)
    assert find_item(window, "assistantMarkdown").property("font").pixelSize() == 17
    assert stored.value("ui/readingSize", 15, type=int) == 17
    click(window, "searchChatsButton")
    field = find_item(window, "chatSearchField")
    field.forceActiveFocus()
    QTest.keyClick(window, Qt.Key.Key_Down)
    QTest.keyClick(window, Qt.Key.Key_Return)
    until(lambda: controller.chatId == first and controller.connected, controller.changed)
    assert controller.draft == "Keep this draft"


@pytest.mark.parametrize("dark,width,height", [(False, 1280, 820), (True, 1280, 820), (False, 800, 600), (True, 800, 600)])
def test_desktop_design_system_appearance_navigation_and_focus(desktop, dark, width, height):
    from PySide6.QtQml import QQmlExpression, qmlContext

    controller, window = desktop
    window.setWidth(width)
    window.setHeight(height)
    window.setProperty("dark", dark)
    controller.start()
    until(lambda: bool(controller.projects), controller.changed)

    def theme(name):
        expression = QQmlExpression(qmlContext(window), window, "Theme." + name)
        value, undefined = expression.evaluate()
        assert not expression.hasError(), expression.error().toString()
        assert not undefined
        return value

    def luminance(color):
        channels = color.getRgbF()[:3]
        linear = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in channels]
        return sum(v * weight for v, weight in zip(linear, (0.2126, 0.7152, 0.0722), strict=True))

    # Gate actual semantic color pairs, not screenshots that merely look plausible.
    for foreground in ("text", "secondaryText", "danger", "warning", "success"):
        for background in ("workspace", "sidebar", "surface", "inset", "selection"):
            values = sorted((luminance(theme(foreground)), luminance(theme(background))))
            assert (values[1] + 0.05) / (values[0] + 0.05) >= 4.5, (dark, foreground, background)
    values = sorted((luminance(theme("primaryText")), luminance(theme("primary"))))
    assert (values[1] + 0.05) / (values[0] + 0.05) >= 4.5

    routes = [("board", "sessionBoardButton", "closeSessionBoardButton"),
              ("automations", "automationsButton", "newAutomationButton"),
              ("skills", "skillsButton", "newSkillButton"),
              ("mcp", "mcpButton", "addMcpButton"),
              ("analytics", "analyticsButton", "closeAnalytics")]
    for page, navigation, action in routes:
        click(window, navigation)
        assert window.property("workspacePage") == page
        until(lambda: find_item(window, "pageHeader") is not None, window.frameSwapped)
        header = find_item(window, "pageHeader")
        button = find_item(window, action)
        assert button is not None and button.isVisible()
        assert visible_rect(window, button).width() >= button.width() - 1
        assert visible_rect(window, button).height() >= button.height() - 1
        assert header.height() > 0
        assert find_item(window, navigation).property("selected")
        assert sum(bool(find_item(window, name).property("selected")) for _, name, _ in routes) == 1
        save_screenshot(window, f"design-{page}-{'dark' if dark else 'light'}-{width}")
        assert window.property("dark") == dark, "Screenshot capture must preserve appearance"

    click(window, "settingsButton")
    click(window, "reduceMotionSwitch")
    assert window.property("reducedMotion")
    assert controller.preference("reducedMotion", False)
    assert theme("motionDuration") == 0
    click(window, "closeSettingsButton")
    click(window, "searchChatsButton")
    field = find_item(window, "chatSearchField")
    assert field.hasActiveFocus()
    expression = QQmlExpression(qmlContext(field), field, "background.focused")
    assert expression.evaluate()[0] is True
    QTest.keyClick(window, Qt.Key.Key_Escape)
    assert not field.isVisible()


def test_desktop_review_stage_unstage_and_commit(desktop, model_server, project):
    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=project, text=True, capture_output=True, check=True
        ).stdout

    git("init", "-q", "-b", "main")
    git("config", "user.name", "Ava fixture")
    git("config", "user.email", "ava@example.invalid")
    git("config", "commit.gpgsign", "false")
    git("config", "core.hooksPath", "/dev/null")
    source = project / "计算.py"
    source.write_text("def answer():\n    return 1\n")
    git("add", ".")
    git("commit", "-qm", "Initial fixture")
    source.write_text("def answer():\n    return 42\n")
    (project / "[draft].md").write_text("# A new file\n")
    controller, window = desktop
    controller.start()
    until(lambda: bool(controller.projects), controller.changed)
    click(window, "newChatButton")
    until(lambda: controller.connected, controller.changed)
    click(window, "reviewChangesButton")
    until(lambda: bool(find_item(window, "reviewPane")), window.frameSwapped)
    review = find_item(window, "reviewPane").property("review")
    until(lambda: len(review.files) == 2, review.filesChanged)
    click(window, "change_worktree:计算.py")
    until(lambda: "+    return 42" in review.state["diff"], review.changed)
    assert "-    return 1" in review.state["diff"]
    save_screenshot(window, "review-working-tree")
    click(window, "stageFileButton")
    until(lambda: any(r["id"] == "staged:计算.py" for r in review.files), review.filesChanged)
    assert "+    return 42" in git("diff", "--cached")
    click(window, "change_staged:计算.py")
    click(window, "unstageFileButton")
    until(lambda: any(r["id"] == "worktree:计算.py" for r in review.files), review.filesChanged)
    assert not git("diff", "--cached")
    click(window, "change_worktree:[draft].md")
    until(lambda: "+# A new file" in review.state["diff"], review.changed)
    click(window, "stageFileButton")
    until(lambda: any(r["id"] == "staged:[draft].md" for r in review.files), review.filesChanged)
    click(window, "change_worktree:计算.py")
    click(window, "stageFileButton")
    until(lambda: all(r["scope"] == "staged" for r in review.files), review.filesChanged)
    click(window, "commitChangesButton")
    find_item(window, "commitMessageField").setProperty("text", "Improve the answer")
    save_screenshot(window, "review-commit")
    hooks = project / ".git" / "fixture-hooks"
    hooks.mkdir()
    hook = hooks / "pre-commit"
    hook.write_text("#!/bin/sh\necho 'Fixture validation failed' >&2\nexit 1\n")
    hook.chmod(0o755)
    git("config", "core.hooksPath", str(hooks))
    click(window, "confirmCommitButton")
    until(lambda: bool(review.state["error"]) and not review.state["busy"], review.changed)
    review.refresh()
    status_job = review._jobs["status"]
    until(lambda: "status" not in review._jobs, status_job.finished)
    assert "Fixture validation failed" in review.state["error"]
    assert find_item(window, "commitMessageField").property("text") == "Improve the answer"
    assert len(review.files) == 2 and git("diff", "--cached")
    hook.unlink()
    click(window, "confirmCommitButton")
    until(lambda: not review.files and not review.state["busy"], review.changed)
    assert git("log", "-1", "--format=%s").strip() == "Improve the answer"
    assert not git("status", "--porcelain")
    assert source.read_text().endswith("return 42\n")
    assert not review.state["error"]


def test_desktop_terminal_interactive_tabs_resize_and_interrupt(
    desktop, model_server, project, monkeypatch
):
    monkeypatch.setenv("SHELL", "/bin/bash")
    monkeypatch.setenv("PS1", "ava-test> ")
    monkeypatch.setenv("BASH_SILENCE_DEPRECATION_WARNING", "1")
    # Readline treats UTF-8 bytes as meta keys in the C locale. Establish the
    # locale this Unicode-paste scenario requires instead of inheriting CI's.
    monkeypatch.setenv("LC_ALL", "en_US.UTF-8" if sys.platform == "darwin" else "C.UTF-8")

    def written(relative):
        path = project / relative
        return path.exists() and path.stat().st_size > 0

    controller, window = desktop
    controller.start()
    until(lambda: bool(controller.projects), controller.changed)
    click(window, "toggleTerminalButton")
    until(lambda: bool(find_item(window, "terminalWeb")), window.frameSwapped)
    pane = find_item(window, "terminalPane")
    session = pane.property("session")
    until(lambda: session.ready, session.changed)
    assert find_item(window, "terminalDock").property("activePane") is pane

    click(window, "terminalWeb")
    for character in "pwd > terminal.txt":
        QTest.keyClick(window, character)  # type: ignore[call-overload]  # Qt accepts char; PySide stubs omit it.
    QTest.keyClick(window, Qt.Key.Key_Return)
    until(lambda: written("terminal.txt"), session.outputReceived)
    assert (project / "terminal.txt").read_text().strip() == str(project)
    until(lambda: session._inflight == 0, window.frameSwapped)
    assert "pwd > terminal.txt" in terminal_evaluate(pane, "window.avaTerminal.text()")
    # Unicode paste passes through xterm's bracketed-paste handling into the real PTY.
    QGuiApplication.clipboard().setText(
        "printf '\\033[32m你好 👋\\033[0m\\n'; printf '你好 👋' > unicode.txt"
    )
    if sys.platform == "darwin" and QGuiApplication.platformName() == "offscreen":
        QTest.keyClick(window, Qt.Key.Key_V, Qt.KeyboardModifier.MetaModifier)
    elif sys.platform == "darwin":
        QTest.keySequence(window, QKeySequence(QKeySequence.StandardKey.Paste))
    else:
        QTest.keyClick(
            window, Qt.Key.Key_V,
            Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier,
        )
    QTest.keyClick(window, Qt.Key.Key_Return)
    until(lambda: written("unicode.txt"), session.outputReceived)
    assert (project / "unicode.txt").read_text() == "你好 👋"
    until(lambda: session._inflight == 0, window.frameSwapped)
    assert "你好 👋" in terminal_evaluate(pane, "window.avaTerminal.text()")
    QGuiApplication.clipboard().setText("unchanged clipboard")
    modifier = (
        Qt.KeyboardModifier.MetaModifier
        if sys.platform == "darwin" and QGuiApplication.platformName() == "offscreen"
        else Qt.KeyboardModifier.ControlModifier
        if sys.platform == "darwin"
        else Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier
    )
    QTest.keyClick(window, Qt.Key.Key_A, modifier)
    QTest.keyClick(window, Qt.Key.Key_C, modifier)
    until(lambda: "pwd > terminal.txt" in QGuiApplication.clipboard().text(), window.frameSwapped)
    assert "你好 👋" in QGuiApplication.clipboard().text()
    # Copy and Paste must accurately reflect an empty terminal selection/clipboard.
    assert "\x1b" not in QGuiApplication.clipboard().text()
    click(window, "terminalWeb")
    assert not terminal_evaluate(pane, "window.avaTerminal.hasSelection()")
    QGuiApplication.clipboard().clear()
    open_text_context(window, find_item(window, "terminalWeb"))
    surface, copy = text_menu_item("Copy")
    assert not copy.property("enabled") and not text_menu_item("Paste")[1].property("enabled")
    QTest.keyClick(surface, Qt.Key.Key_Escape)
    click(window, "terminalWeb")
    QTest.qWait(100)  # Chromium presents the terminal after its write callback.
    QTest.keyClick(window, Qt.Key.Key_A, modifier)
    open_text_context(window, find_item(window, "terminalWeb"))
    assert text_menu_item("Copy")[1].property("enabled")
    QGuiApplication.clipboard().setText("Copy through the menu")
    click_text_menu("Copy")
    until(lambda: "pwd > terminal.txt" in QGuiApplication.clipboard().text(), window.frameSwapped)
    assert "你好 👋" in QGuiApplication.clipboard().text()
    save_screenshot(window, "terminal")
    old_size = terminal_evaluate(pane, "window.avaTerminal.size()")
    divider = find_item(window, "terminalDivider")
    point = divider.mapToScene(QPointF(divider.width() / 2, divider.height() / 2)).toPoint()
    QTest.mousePress(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point)
    QTest.mouseMove(window, point - QPointF(0, 100).toPoint(), delay=50)
    QTest.mouseRelease(
        window,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
        point - QPointF(0, 100).toPoint(),
    )
    until(lambda: find_item(window, "terminalDock").height() > 300, window.frameSwapped)
    QTest.qWait(100)
    new_size = terminal_evaluate(pane, "window.avaTerminal.size()")
    assert new_size[1] > old_size[1]
    assert controller.terminalHeight > 300

    def command(text, marker):
        click(window, "terminalWeb")
        for character in text:
            QTest.keyClick(window, character)
        QTest.keyClick(window, Qt.Key.Key_Return)
        until(marker, session.outputReceived, window.frameSwapped)

    command("stty size > size.txt", lambda: written("size.txt"))
    assert [int(n) for n in (project / "size.txt").read_text().split()] == new_size[::-1]
    command(
        "export AVA_TERM_VALUE=first; mkdir nested; cd nested; pwd > first.cwd",
        lambda: written("nested/first.cwd"),
    )
    first_process = session._process
    first_session = session
    click(window, "newTerminalButton")
    until(
        lambda: find_item(window, "terminalPane").property("session") is not first_session,
        window.frameSwapped,
    )
    pane = find_item(window, "terminalPane")
    session = pane.property("session")
    until(lambda: session.ready, session.changed)
    command(
        "printf '%s' \"${AVA_TERM_VALUE-unset}\" > second.env",
        lambda: written("second.env"),
    )
    assert (project / "second.env").read_text() == "unset"
    second_process = session._process
    second_session = session
    click(window, "hideTerminalButton")
    assert not find_item(window, "terminalDock").isVisible()

    assert first_process.poll() is None and second_process.poll() is None
    if QGuiApplication.platformName() == "offscreen":
        QTest.keySequence(window, QKeySequence("Ctrl+J"))
    else:
        # Cocoa keeps automation launched from the CLI inactive; window shortcuts
        # are covered offscreen, while native tests exercise the visible button.
        click(window, "toggleTerminalButton")
    until(lambda: find_item(window, "terminalDock").isVisible(), window.frameSwapped)
    click(window, "terminalTab_0")
    pane = find_item(window, "terminalPane")
    session = pane.property("session")
    assert session is first_session
    assert "first.cwd" in terminal_evaluate(pane, "window.avaTerminal.text()")
    command("sleep 60 & echo $! > child.pid; wait", lambda: written("nested/child.pid"))
    child = int((project / "nested/child.pid").read_text())
    # Ctrl+C must interrupt the foreground wait without closing the interactive shell.
    QTest.keyClick(
        window,
        Qt.Key.Key_C,
        Qt.KeyboardModifier.MetaModifier
        if QGuiApplication.platformName() == "cocoa"
        else Qt.KeyboardModifier.ControlModifier,
    )
    command("printf alive > alive.txt", lambda: written("nested/alive.txt"))
    window.setProperty("dark", True)
    QTest.qWait(100)
    color = window.color()
    assert terminal_evaluate(pane, "document.body.style.background") == f"rgb({color.red()}, {color.green()}, {color.blue()})"
    save_screenshot(window, "terminal-dark-tabs")
    click(window, "closeTerminalTab_0")
    until(lambda: first_session not in controller._terminals, first_session.closed)
    assert first_process.poll() is not None and second_process.poll() is None
    assert find_item(window, "terminalDock").property("activePane") is find_item(
        window, "terminalPane"
    )
    with pytest.raises(ProcessLookupError):
        os.kill(child, 0)
    click(window, "closeTerminalTab_1")
    until(lambda: not controller._terminals, second_session.closed)
    assert second_process.poll() is not None
    assert not find_item(window, "terminalDock").isVisible()


@pytest.mark.skipif(
    not os.environ.get("AVA_DESKTOP_PERF"), reason="Opt-in native terminal benchmark"
)
def test_desktop_terminal_output_backpressure(
    desktop, model_server, project, monkeypatch, tmp_path
):
    import base64
    import shlex
    from time import perf_counter

    monkeypatch.setenv("SHELL", "/bin/sh")
    (project / "produce.py").write_text(
        "import sys\n"
        "sys.stdout.write(('performance row ' + 'x'*48 + '\\n') * 65536)\n"
        "sys.stdout.write('END_TERMINAL_OUTPUT\\n')\n"
        "sys.stdout.flush()\n"
    )
    controller, window = desktop
    controller.start()
    until(lambda: bool(controller.projects), controller.changed)
    start = perf_counter()
    click(window, "toggleTerminalButton")
    pane = find_item(window, "terminalPane")
    session = pane.property("session")
    until(lambda: session.ready, session.changed)
    startup_ms = (perf_counter() - start) * 1000
    QTest.qWait(100)
    tail = b""
    received = 0
    complete = False
    pauses = []
    in_flight = []
    previous = perf_counter()
    heartbeat = QTimer()
    heartbeat.setInterval(16)

    def beat():
        nonlocal previous
        now = perf_counter()
        pauses.append(now - previous)
        in_flight.append(session._inflight)
        previous = now

    def output(encoded, count):
        nonlocal tail, received, complete
        received += count
        tail = (tail + base64.b64decode(encoded))[-100:]
        complete = complete or b"\r\nEND_TERMINAL_OUTPUT\r\n" in tail

    heartbeat.timeout.connect(beat)
    session.outputReceived.connect(output)
    click(window, "terminalWeb")
    for character in shlex.quote(sys.executable) + " produce.py":
        QTest.keyClick(window, character)  # type: ignore[call-overload]  # Qt accepts char; PySide stubs omit it.
    heartbeat.start()
    start = previous = perf_counter()
    QTest.keyClick(window, Qt.Key.Key_Return)
    until(
        lambda: complete and session._inflight == 0,
        session.outputReceived,
        window.frameSwapped,
        timeout=15_000,
    )
    elapsed = (perf_counter() - start) * 1000
    heartbeat.stop()
    text = terminal_evaluate(pane, "window.avaTerminal.text()")
    assert "END_TERMINAL_OUTPUT" in text
    assert len(text.splitlines()) < 5100
    metrics = dict(
        startup_ms=round(startup_ms),
        output_ms=round(elapsed),
        bytes=received,
        max_ui_pause_ms=round(max(pauses) * 1000),
        max_in_flight=max(in_flight),
        retained_lines=len(text.splitlines()),
    )
    print("TERMINAL_BENCHMARK", json.dumps(metrics))
    (tmp_path / "terminal-benchmark.json").write_text(json.dumps(metrics))
    assert startup_ms < 2000 and elapsed < 10000 and max(pauses) < 0.15, metrics
    assert max(in_flight) <= session.HIGH_WATER, metrics
    save_screenshot(window, "terminal-throughput")


def test_desktop_worktree_chat_keeps_project_and_uses_its_workspace(desktop, model_server, project, home):
    def git(*args):
        return subprocess.check_output(['git', '-C', str(project), *args], text=True)

    (project / 'answer.py').write_text('answer = 1\n')
    git('init', '-q', '-b', 'main')
    git('config', 'user.name', 'Ava fixture')
    git('config', 'user.email', 'ava@example.invalid')
    git('config', 'commit.gpgsign', 'false')
    git('add', '.')
    git('commit', '-qm', 'Initial answer')
    (project / 'answer.py').write_text('answer = 99\n')
    settings = json.loads((home / 'settings.json').read_text())
    settings['model'] = 'fixture-remote-tools'
    settings['providers']['desktop-test']['models']['fixture-remote-tools'] = {'context_window': 10000}
    (home / 'settings.json').write_text(json.dumps(settings))
    controller, window = desktop
    controller.start()
    until(lambda: bool(controller.projects), controller.changed)
    project_id = controller.projectId
    assert find_item(window, 'newChatOptionsButton') is not None, 'New chat needs a worktree choice'
    click(window, 'newChatOptionsButton')
    click(window, 'newWorktreeAction')
    until(lambda: bool(find_item(window, 'worktreeBranchField')), window.frameSwapped)
    find_item(window, 'worktreeBranchField').setProperty('text', 'bad branch name')
    click(window, 'createWorktreeChatButton')
    until(lambda: bool(controller.worktreeState.get('error')), controller.worktreeChanged)
    assert not controller.chatId and not controller.projects[0]['chats']
    save_screenshot(window, 'worktree-invalid-branch')
    find_item(window, 'worktreeBranchField').setProperty('text', 'ava/中文-review')
    until(lambda: not controller.worktreeState['loading'], controller.worktreeChanged)
    save_screenshot(window, 'worktree-dialog')
    pulses = [time.perf_counter()]
    heartbeat = QTimer()
    heartbeat.setInterval(10)
    heartbeat.timeout.connect(lambda: pulses.append(time.perf_counter()))
    heartbeat.start()
    click(window, 'createWorktreeChatButton')
    until(lambda: controller.connected and bool(controller.chatId), controller.changed)
    heartbeat.stop()
    max_pause = max(b - a for a, b in zip(pulses, pulses[1:], strict=False))
    assert max_pause < 0.15
    print('WORKTREE_CREATION', json.dumps({'max_ui_pause_ms': round(max_pause * 1000), 'ready_ms': round((pulses[-1] - pulses[0]) * 1000)}))
    workspace = Path(controller.workspacePath)
    assert workspace != project and controller.projectId == project_id
    assert len(controller.projects) == 1
    assert (workspace / 'answer.py').read_text() == 'answer = 1\n'
    assert (project / 'answer.py').read_text() == 'answer = 99\n'
    assert git('branch', '--show-current').strip() == 'main'
    chat_id = controller.chatId
    type_message(window, 'Work independently in this worktree')
    QTest.keyClick(window, Qt.Key.Key_Return)
    until(lambda: (workspace / 'remote-test-proof.txt').exists(), controller.changed)
    assert not (project / 'remote-test-proof.txt').exists()
    # The tool creates the file before the following model request reaches the
    # server. Release the assistant response, not the already-finished tool call.
    until(lambda: len(model_server) == 2, controller.changed)
    model_server[-1].release.set()
    until(lambda: controller.status == 'idle', controller.changed)
    controller.browseFiles('answer.py')
    until(lambda: bool(find_item(window, 'filePane')), window.frameSwapped)
    assert find_item(window, 'filePane').property('rootPath') == str(workspace)
    click(window, 'closeInspectorButton')
    click(window, 'toggleTerminalButton')
    until(lambda: bool(find_item(window, 'terminalPane')), window.frameSwapped)
    terminal = find_item(window, 'terminalPane').property('session')
    assert terminal.root == str(workspace)
    click(window, 'hideTerminalButton')
    save_screenshot(window, 'worktree-chat')
    click(window, 'newChatButton')
    until(lambda: controller.connected and controller.chatId != chat_id, controller.changed)
    assert controller.workspacePath == str(project)
    assert any(chat['id'] == chat_id for chat in controller.projects[0]['chats'])
    controller.openChat(chat_id)
    until(lambda: controller.connected and controller.workspacePath == str(workspace), controller.changed)
    assert (workspace / 'remote-test-proof.txt').exists()

    click(window, "skillsButton")
    skills = controller.skillView
    assert skills.project == chat_id, "The current worktree must be the initial skills workspace"
    click(window, "newSkillButton")
    find_item(window, "skillNameField").setProperty("text", "worktree-check")
    find_item(window, "skillDescriptionField").setProperty("text", "Check this branch.")
    find_item(window, "skillBodyField").setProperty("text", "# Check the branch\n\nInspect its changes.")
    click(window, "saveSkillButton")
    until(lambda: skills.detail.get("name") == "worktree-check" or bool(skills.editorError), skills.changed)
    assert not skills.editorError
    assert Path(skills.detail["path"]).is_relative_to(workspace)
    assert not (project / ".agents/skills/worktree-check").exists()
    assert skills.canUse
    save_screenshot(window, "skills-worktree")


def text_menu_item(caption):
    for surface in QGuiApplication.topLevelWindows():
        if not isinstance(surface, QQuickWindow) or not surface.isVisible():
            continue
        retained = surface.__dict__.setdefault("_test_items", {})
        pending = [surface.contentItem()]
        while pending:
            item = pending.pop()
            retained[getCppPointer(item)[0]] = item
            if item.isVisible() and item.inherits("QQuickMenuItem"):
                text = str(item.property("text") or "").replace("&", "").strip().casefold()
                if text == caption.casefold():
                    return surface, item
            if item.objectName() not in ("webBrowser", "terminalWeb"):
                pending.extend(item.childItems())
    return None


def click_text_menu(caption):
    application = QGuiApplication.instance()
    assert isinstance(application, QGuiApplication)
    until(lambda: text_menu_item(caption) is not None, application.focusWindowChanged,
          *(surface.frameSwapped for surface in QGuiApplication.topLevelWindows()
            if isinstance(surface, QQuickWindow)), timeout=2000)
    found = text_menu_item(caption)
    assert found is not None, caption
    surface, item = found
    assert item.property("enabled"), caption
    assert QTest.qWaitForWindowExposed(surface, 2000)
    QCoreApplication.processEvents()
    ancestors = []
    parent = item
    while parent is not None:
        ancestors.append(parent)
        parent = parent.parentItem()
    for parent in reversed(ancestors):
        parent.ensurePolished()
    point = item.mapToScene(QPointF(item.width() / 2, item.height() / 2)).toPoint()
    QTest.mouseClick(surface, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point)


def open_text_context(window, item, *, keyboard=False, point=None):
    from PySide6.QtGui import QContextMenuEvent

    point = point or visible_rect(window, item).center().toPoint()
    if not keyboard:
        QTest.mouseClick(window, Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier, point)
    # QTest sends Qt mouse input; the platform normally follows that with a
    # QContextMenuEvent. Deliver that same event through the real window.
    reason = QContextMenuEvent.Reason.Keyboard if keyboard else QContextMenuEvent.Reason.Mouse
    event = QContextMenuEvent(reason, point, window.mapToGlobal(point))
    QCoreApplication.sendEvent(window, event)
    until(lambda: text_menu_item("Copy") is not None, window.frameSwapped)


def test_desktop_text_context_preserves_selection_and_pastes_images(desktop, model_server):
    controller, window = desktop
    controller.start()
    until(lambda: bool(controller.projects), controller.changed)
    click(window, "newChatButton")
    until(lambda: controller.connected, controller.changed)
    type_message(window, "Keep this 中文 selection")
    composer = find_item(window, "composer")
    QTest.keySequence(window, QKeySequence(QKeySequence.StandardKey.SelectAll))
    selected = composer.property("selectedText")
    assert selected == "Keep this 中文 selection"
    picture = QImage(24, 24, QImage.Format.Format_RGB32)
    picture.fill(Qt.GlobalColor.blue)
    QGuiApplication.clipboard().setImage(picture)
    open_text_context(window, composer)
    assert composer.property("selectedText") == selected
    found = text_menu_item("Paste")
    assert found is not None
    surface, paste = found
    save_screenshot(surface, "composer-image-menu")
    assert paste.property("enabled"), "Image paste must work from the context menu as it does from the keyboard"
    point = paste.mapToScene(QPointF(paste.width() / 2, paste.height() / 2)).toPoint()
    QTest.mouseClick(surface, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point)
    until(lambda: len(controller.attachments) == 1, controller.draftChanged)
    assert composer.property("text") == selected
    assert controller.attachments[0]["kind"] == "image"
    until(lambda: composer.hasActiveFocus(), window.frameSwapped)
    QGuiApplication.clipboard().setText("菜单粘贴")
    open_text_context(window, composer, keyboard=True)
    click_text_menu("Paste")
    assert composer.property("text") == "菜单粘贴"
    QTest.keySequence(window, QKeySequence(QKeySequence.StandardKey.SelectAll))
    open_text_context(window, composer)
    click_text_menu("Cut")
    assert composer.property("text") == ""
    assert QGuiApplication.clipboard().text() == "菜单粘贴"
    open_text_context(window, composer)
    click_text_menu("Undo")
    assert composer.property("text") == "菜单粘贴"
    QGuiApplication.clipboard().clear()
    window.setProperty("dark", True)
    window.setWidth(800)
    window.setHeight(600)
    screen = window.screen().availableGeometry()
    window.setPosition(screen.right() - window.width(), screen.bottom() - window.height())
    frame = QSignalSpy(window.frameSwapped)
    window.update()
    assert frame.count() or frame.wait(2000)
    until(lambda: composer.hasActiveFocus(), window.frameSwapped)
    open_text_context(window, composer)
    surface, paste = text_menu_item("Paste")
    assert not paste.property("enabled")
    # Item popups share the app window; check the visible menu, not its host window.
    menu = paste.parentItem()
    top_left = surface.mapToGlobal(menu.mapToScene(QPointF(0, 0)).toPoint())
    assert screen.contains(QRectF(top_left, menu.size()).toAlignedRect())
    save_screenshot(surface, "text-menu-dark-edge")
    QTest.keyClick(surface, Qt.Key.Key_Escape)
    until(lambda: composer.hasActiveFocus(), window.frameSwapped)
    type_message(window, "继续输入", append=True)


def test_desktop_readonly_text_context_copies_markdown_and_code(desktop, model_server, project):
    controller, window = desktop
    controller.start()
    until(lambda: bool(controller.projects), controller.changed)
    click(window, "newChatButton")
    until(lambda: controller.connected, controller.changed)
    controller.selectModel("fixture-reasoning")
    until(lambda: controller.selection.get("model") == "fixture-reasoning", controller.changed)
    type_message(window, "Show a readable answer")
    click(window, "sendButton")
    until(lambda: bool(model_server), controller.changed)
    model_server[0].release.set()
    until(lambda: controller.status == "idle", controller.changed)
    until(lambda: bool(find_item(window, "assistantMarkdown")), window.frameSwapped)
    output = find_item(window, "assistantMarkdown")
    output.forceActiveFocus()
    QTest.keySequence(window, QKeySequence(QKeySequence.StandardKey.SelectAll))
    selected = output.property("selectedText")
    assert "Native controls" in selected and "**Native controls**" not in selected
    open_text_context(window, output)
    assert output.property("selectedText") == selected
    assert text_menu_item("Paste") is None and text_menu_item("Cut") is None
    surface, item = text_menu_item("Copy")
    assert item.height() == 32
    save_screenshot(surface, "readonly-text-menu")
    click_text_menu("Copy")
    assert QGuiApplication.clipboard().text() == selected.replace("\u2029", "\n").replace("\u2028", "\n")
    until(lambda: output.hasActiveFocus(), window.frameSwapped)

    code = "# 中文源代码\nanswer = '**keep this**'\nprint(answer)\n"
    source = project / "context.py"
    source.write_text(code)
    controller.browseFiles(str(source))
    until(
        lambda: bool(preview := find_item(window, "codePreview"))
        and preview.isVisible() and preview.property("lineCount") == 4,
        window.frameSwapped,
    )
    lines = find_item(window, "codeLines")
    save_screenshot(window, "code-context-before")
    open_text_context(window, lines)
    save_screenshot(text_menu_item("Copy")[0], "code-context-select-all")
    click_text_menu("Select all")
    preview = find_item(window, "codePreview")
    assert preview.property("startLine") == 0 and preview.property("endLine") == 3
    open_text_context(window, lines, keyboard=True)
    assert text_menu_item("Cut") is None
    click_text_menu("Copy")
    assert QGuiApplication.clipboard().text() == code
    preview = find_item(window, "codePreview")
    line_height = preview.property("lineHeight")
    gutter = preview.property("gutterWidth")
    start = lines.mapToScene(QPointF(gutter + 1, line_height / 2)).toPoint()
    end = lines.mapToScene(QPointF(gutter + 1, line_height * 2.5)).toPoint()
    QTest.mousePress(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, start)
    QTest.mouseMove(window, end, delay=30)
    QTest.mouseRelease(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, end)
    open_text_context(window, lines)
    click_text_menu("Copy")
    assert QGuiApplication.clipboard().text() == code[:code.index("print(answer)")]


def test_desktop_browser_text_context_edits_and_copies(desktop, model_server, home):
    controller, window = desktop
    controller.start()
    until(lambda: bool(controller.projects), controller.changed)
    click(window, "toggleRightSidebar")
    click(window, "browserTab")
    until(lambda: bool(find_item(window, "webBrowser")), window.frameSwapped)
    browser = find_item(window, "webBrowser")
    config = json.loads((home / "settings.json").read_text())
    base = config["providers"]["desktop-test"]["base_url"].removesuffix("/v1")
    browser.setProperty("url", QUrl(base + "/text-menu"))
    until(lambda: browser.property("title") == "Text menus ready", browser.titleChanged)
    until(lambda: not browser.property("loading"), window.frameSwapped)
    click(window, "webBrowser")
    QTest.qWait(100)  # Chromium presents the first document frame after load completion.
    save_screenshot(window, "browser-context-page")
    point = browser.mapToScene(QPointF(40, 40)).toPoint()
    QTest.mouseClick(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point)
    QTest.keySequence(window, QKeySequence(QKeySequence.StandardKey.SelectAll))
    until(lambda: browser.property("title") == "Selected:网页文字 selection", browser.titleChanged)
    save_screenshot(window, "browser-context-selected")
    open_text_context(window, browser, point=point)
    assert text_menu_item("Copy")[1].height() == 32
    click_text_menu("Copy")
    until(lambda: QGuiApplication.clipboard().text() == "网页文字 selection", window.frameSwapped)
    QGuiApplication.clipboard().setText("更新后的文字")
    open_text_context(window, browser, point=point)
    click_text_menu("Paste")
    until(lambda: browser.property("title") == "更新后的文字", browser.titleChanged)
    point = browser.mapToScene(QPointF(40, 150)).toPoint()
    QTest.mouseClick(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point)
    QTest.keySequence(window, QKeySequence(QKeySequence.StandardKey.SelectAll))
    until(lambda: browser.property("title") == "Selected page", browser.titleChanged)
    open_text_context(window, browser, point=point)
    assert text_menu_item("Cut") is None and text_menu_item("Paste") is None
    save_screenshot(text_menu_item("Copy")[0], "browser-text-menu")
    click_text_menu("Copy")
    until(lambda: "只读网页" in QGuiApplication.clipboard().text(), window.frameSwapped)


@pytest.mark.skipif(not os.environ.get("AVA_DESKTOP_PERF"), reason="Opt-in native rendering benchmark")
def test_desktop_pdf_complex_page_does_not_block_chat(desktop, model_server, project, monkeypatch):
    import subprocess

    from PySide6.QtGui import QColor, QPageSize, QPainter, QPdfWriter

    # Dense vector drawings stress PDF rasterization independently of page count.
    source = project / "drawing.pdf"
    writer = QPdfWriter(str(source))
    writer.setResolution(72)
    writer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
    painter = QPainter(writer)
    painter.setPen(Qt.PenStyle.NoPen)
    for index in range(12_000):
        painter.setBrush(QColor(index % 251, index * 7 % 251, index * 13 % 251, 160))
        painter.drawEllipse(QRectF(index * 17 % 500, index * 31 % 700, 36, 36))
    assert writer.newPage()
    painter.setPen(Qt.GlobalColor.black)
    painter.drawText(32, 48, "A simple second page")
    painter.end()
    del writer

    controller, window = desktop
    controller.start()
    until(lambda: bool(controller.projects), controller.changed)
    click(window, "newChatButton")
    until(lambda: controller.connected, controller.changed)
    click(window, "toggleRightSidebar")
    until(lambda: bool(find_item(window, "file_drawing.pdf")), window.frameSwapped)
    samples, typed = [], []
    previous = time.perf_counter()
    heartbeat = QTimer()
    heartbeat.setInterval(16)

    def beat():
        nonlocal previous
        now = time.perf_counter()
        samples.append(now - previous)
        previous = now

    heartbeat.timeout.connect(beat)
    heartbeat.start()
    start = time.perf_counter()

    def write_during_render():
        type_message(window, "PDF 渲染时继续工作")
        typed.append(time.perf_counter() - start)

    QTimer.singleShot(40, write_during_render)
    click(window, "file_drawing.pdf")
    until(
        lambda: bool(view := find_item(window, "pdfView"))
        and view.property("currentPageRenderingStatus") == 1 and bool(typed),
        window.frameSwapped, timeout=20_000,
    )
    heartbeat.stop()
    assert controller.draft == "PDF 渲染时继续工作"
    metrics = {"ready_ms": round((time.perf_counter() - start) * 1000),
               "typing_ms": round(typed[0] * 1000), "max_ui_pause_ms": round(max(samples) * 1000)}
    print("COMPLEX_PDF_BENCHMARK", json.dumps(metrics))
    QTest.mouseMove(window, find_item(window, "composer").mapToScene(QPointF(20, 20)).toPoint())
    save_screenshot(window, "complex-pdf-responsive-chat")
    assert max(samples) < .15 and typed[0] < .15, metrics

    # A renderer failure stays within the page, and its retry action starts a
    # fresh process. Closing another in-flight render must not await PDFium.
    view = find_item(window, "pdfView")
    worker = controller.pdf_images._process
    assert worker is not None
    click(window, "pdfZoomIn")
    until(lambda: bool(controller.pdf_images._render_key), window.frameSwapped)
    until(lambda: find_item(window, "pdfBusy").property("running"), window.frameSwapped)
    QTest.qWait(300)  # Qt Basic's loading indicator fades in over 250ms.
    save_screenshot(window, "pdf-render-loading")
    launch = subprocess.Popen

    def unavailable(command, *args, **kwargs):
        if "ava.app.desktop._pdf_worker" in command:
            raise OSError("Renderer unavailable during failure injection")
        return launch(command, *args, **kwargs)

    # A resize may already have queued another image. Keep the renderer
    # unavailable until those requests have reached the visible error state.
    with monkeypatch.context() as patch:
        patch.setattr(subprocess, "Popen", unavailable)
        worker.kill()
        until(lambda: view.property("currentPageRenderingStatus") == 3, window.frameSwapped)
        save_screenshot(window, "pdf-render-error")
    until(lambda: worker.poll() is not None, window.frameSwapped)
    click(window, "pdfRenderRetry_0")
    until(lambda: view.property("currentPageRenderingStatus") == 1, window.frameSwapped)
    assert controller.draft == "PDF 渲染时继续工作"
    worker = controller.pdf_images._process
    assert worker is not None
    click(window, "pdfZoomIn")
    until(lambda: bool(controller.pdf_images._render_key), window.frameSwapped)
    close_start = time.perf_counter()
    click(window, "closeInspectorTab_0")
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert find_item(window, "pdfView") is None
    assert not controller.pdf_images._sources
    close_ms = (time.perf_counter() - close_start) * 1000
    until(lambda: worker.poll() is not None, window.frameSwapped)
    print("PDF_CLOSE_BENCHMARK", json.dumps({"close_ms": round(close_ms), "renderer_stopped": True}))
    assert close_ms < 150


def test_desktop_session_board_tracks_completion_and_explicit_review(desktop, model_server, home):
    controller, window = desktop
    controller.start()
    until(lambda: bool(controller.projects), controller.changed)
    click(window, "newChatButton")
    until(lambda: controller.connected, controller.changed)
    chat_id = controller.chatId
    type_message(window, "Review this session from the board")
    QTest.keyClick(window, Qt.Key.Key_Return)
    until(lambda: any(row["body"] == "Hello " for row in controller._transcript.rows), controller.changed)
    save_screenshot(window, "board-entry")
    assert find_item(window, "sessionBoardButton") is not None, "A session board needs a sidebar entry"
    click(window, "sessionBoardButton")
    board = controller.board
    until(lambda: board.activeSessions.rowCount() == 1, board.changed)
    model_server[0].release.set()
    until(lambda: board.needsReview.rowCount() == 1 and board.activeSessions.rowCount() == 0, board.changed)
    save_screenshot(window, "board-needs-review")
    until(lambda: find_item(window, "openBoardChat_review_" + chat_id) is not None,
          board.changed, window.frameSwapped)
    open_button = find_item(window, "openBoardChat_review_" + chat_id)
    clicked = QSignalSpy(open_button.clicked)
    click(window, "openBoardChat_review_" + chat_id)
    save_screenshot(window, "board-open-result")
    assert clicked.count(), "The visible Open button did not receive the click"
    assert not window.property("boardOpen"), (controller.connected, controller.chatId, controller.error)
    until(lambda: controller.connected and not window.property("boardOpen"), controller.changed)
    click(window, "sessionBoardButton")
    # Match the existing read-on-open behavior; an unseen result still needs explicit review.
    until(lambda: board.reviewedSessions.rowCount() == 1 and board.needsReview.rowCount() == 0, board.changed)
    click(window, "openBoardChat_reviewed_" + chat_id)
    type_message(window, "A new result must need review again")
    QTest.keyClick(window, Qt.Key.Key_Return)
    until(lambda: len(model_server) == 2, controller.changed)
    click(window, "sessionBoardButton")
    model_server[1].release.set()
    until(lambda: controller.status == "idle", controller.changed)
    until(lambda: board.needsReview.rowCount() == 1, board.changed)
    window.setProperty("dark", True)
    window.setWidth(800)
    window.setHeight(600)
    save_screenshot(window, "board-dark-narrow")
    click(window, "reviewBoardChat_" + chat_id)
    until(lambda: board.reviewedSessions.rowCount() == 1, board.changed)
    instance = controller.runtime.info["instance_id"]
    controller._detach()
    stop_backend(home)
    controller.start()
    until(lambda: controller.online and board.reviewedSessions.rowCount() == 1, controller.changed, board.changed)
    assert controller.runtime.info["instance_id"] != instance
    assert board.needsReview.rowCount() == 0


def test_desktop_incomplete_http_response_is_reported_as_failure(qt_app, model_server, home):
    from ava.app.desktop.connection import Connection

    config = json.loads((home / "settings.json").read_text())
    address = QUrl(config["providers"]["desktop-test"]["base_url"])
    connection = Connection(address.port(), "local-test-only")
    result = []
    connection.call("GET", "/api/projects", None, lambda payload, error: result.append((payload, error)))
    until(lambda: bool(result), connection._network.finished)
    assert result[0][1], "HTTP 200 headers do not mean a disconnected response completed successfully"
    connection.close()


def test_desktop_mcp_manage_and_call_tools(desktop, model_server, home, project):
    from tests.test_mcp import FIXTURE

    settings = json.loads((home / "settings.json").read_text())
    settings["model"] = "fixture-mcp-tools"
    settings["providers"]["desktop-test"]["models"]["fixture-mcp-tools"] = {"context_window": 10000}
    (home / "settings.json").write_text(json.dumps(settings))
    controller, window = desktop
    controller.start()
    until(lambda: bool(controller.projects), controller.changed)
    save_screenshot(window, "mcp-entry")
    assert find_item(window, "mcpButton") is not None, "MCP servers need a discoverable management entry"
    click(window, "mcpButton")
    click(window, "addMcpButton")
    find_item(window, "mcpNameField").setProperty("text", "Workspace checks")
    launcher = project / "MCP runtime"
    find_item(window, "mcpCommandField").setProperty("text", shlex.join([str(launcher), str(FIXTURE)]))
    click(window, "addMcpCredentialButton")
    for name, value in (("mcpCredentialName_0", "MCP_TOOL_COUNT"), ("mcpCredentialValue_0", "2")):
        field = find_item(window, name)
        field.forceActiveFocus()
        event = QInputMethodEvent()
        event.setCommitString(value)
        QCoreApplication.sendEvent(field, event)
    save_screenshot(window, "mcp-editor")
    click(window, "saveMcpButton")
    view = controller.mcpView
    until(lambda: view.detail.get("status") == "error", view.changed, timeout=20000)
    assert view.detail["error"] and not view.editorError
    save_screenshot(window, "mcp-connection-error")
    click(window, "mcpErrorDetailsButton")
    assert "program could not be found" in find_item(window, "mcpSchemaPreview").property("text")
    QTest.keyClick(window, Qt.Key.Key_Escape)
    launcher.write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable) + ' "$@"\n')
    launcher.chmod(0o700)
    click(window, "connectMcpButton")
    until(lambda: view.detail.get("status") == "connected" or bool(view.editorError), view.changed, timeout=20000)
    assert not view.editorError
    assert view.toolRows.rowCount() == 2
    click(window, "mcpTool_record_change")
    assert '"$defs"' in find_item(window, "mcpSchemaPreview").property("text")
    save_screenshot(window, "mcp-schema")
    QTest.keyClick(window, Qt.Key.Key_Escape)
    save_screenshot(window, "mcp-connected")
    click(window, "closeMcpButton")
    controller.newChat()
    until(lambda: controller.connected, controller.changed)
    type_message(window, "Record the approved change with the MCP server.")
    click(window, "sendButton")
    until(lambda: len(model_server) == 2, controller.changed, timeout=20000)
    proof = json.loads((project / "mcp-proof.json").read_text())
    assert proof["change"]["approved"] and proof["cwd"] == str(project)
    result = next(message for message in model_server[1].request["messages"] if message["role"] == "tool")
    assert "Native MCP check" in result["content"]
    model_server[1].release.set()
    until(lambda: controller.status == "idle", controller.changed)
    click(window, "mcpButton")
    until(lambda: view.toolRows.rowCount() == 3, view.changed)
    assert any(row["name"] == "new_workspace_check" for row in view.toolRows.rows)
    click(window, "toggleMcpButton")
    until(lambda: view.detail.get("status") == "disabled", view.changed)
    import psutil
    until(lambda: not psutil.pid_exists(proof["pid"]), view.changed, window.frameSwapped)
    click(window, "editMcpButton")
    assert find_item(window, "mcpCredentialName_0").property("text") == "MCP_TOOL_COUNT"
    assert find_item(window, "mcpCredentialValue_0").property("text") == ""
    find_item(window, "mcpNameField").setProperty("text", "Workspace checks · 中文")
    click(window, "saveMcpButton")
    until(lambda: view.detail.get("name") == "Workspace checks · 中文", view.changed)
    assert view.detail["status"] == "disabled", "Editing a disabled server must not launch its process"
    click(window, "toggleMcpButton")
    until(lambda: view.detail.get("enabled"), view.changed)
    click(window, "connectMcpButton")
    until(lambda: view.detail.get("status") == "connected", view.changed)
    window.setProperty("dark", True)
    window.setWidth(800)
    window.setHeight(600)
    save_screenshot(window, "mcp-dark-narrow")
    click(window, "removeMcpButton")
    click(window, "confirmRemoveMcpButton")
    until(lambda: not view.rows.rows and not view.detail, view.changed)
    assert (project / "mcp-proof.json").exists(), "Removing an MCP connection must preserve workspace files"


def test_desktop_mcp_real_catalog_virtualization(desktop, model_server, home, capfd):
    from ava.tool.mcp import MCPServers, ServerConfig
    from tests.test_mcp import FIXTURE

    identity = MCPServers(home).save(ServerConfig(name="Workspace catalog", command=sys.executable,
                                                args=[str(FIXTURE)], env={"MCP_TOOL_COUNT": "1000"}))
    controller, window = desktop
    controller.start()
    until(lambda: bool(controller.projects), controller.changed)
    click(window, "mcpButton")
    servers = controller.mcpView
    until(lambda: bool(servers.rows.rows), servers.changed)
    click(window, "mcpServer_" + identity)
    click(window, "connectMcpButton")
    until(lambda: servers.detail.get("status") == "connected", servers.changed, timeout=20000)
    click(window, "closeMcpButton")
    pulses = [time.perf_counter()]
    timer = QTimer()
    timer.setInterval(10)
    timer.timeout.connect(lambda: pulses.append(time.perf_counter()))
    timer.start()
    start = time.perf_counter()
    click(window, "mcpButton")
    until(lambda: servers.toolRows.rowCount() == 1000, servers.changed)
    view = find_item(window, "mcpToolList")
    frame = QSignalSpy(window.frameSwapped)
    window.update()
    assert frame.count() or frame.wait(2000)
    open_ms = round((time.perf_counter() - start) * 1000)
    resets = QSignalSpy(servers.toolRows.modelReset)
    for fraction in (0.5, 1, 0):
        view.setProperty("contentY", max(0, view.property("contentHeight") - view.height()) * fraction)
        frame = QSignalSpy(window.frameSwapped)
        window.update()
        assert frame.count() or frame.wait(2000)
    live = [item for item in view.childItems()[0].childItems() if item.objectName().startswith("mcpTool_")]
    click(window, "refreshMcpButton")
    until(lambda: not servers.loading, servers.changed)
    assert resets.count() == 0
    timer.stop()
    metrics = {"tools": 1000, "open_ms": open_ms, "live_delegates": len(live),
               "max_gui_ms": round(max(b-a for a, b in zip(pulses, pulses[1:], strict=False)) * 1000)}
    with capfd.disabled():
        print("MCP_BENCHMARK", json.dumps(metrics))
    assert len(live) < 30 and open_ms < 1000 and metrics["max_gui_ms"] < 150
    save_screenshot(window, "mcp-thousand")


def test_desktop_skills_manage_and_affect_agent(desktop, model_server, project):
    controller, window = desktop
    controller.start()
    until(lambda: bool(controller.projects), controller.changed)
    controller.newChat()
    until(lambda: controller.connected, controller.changed)
    save_screenshot(window, "skills-entry")
    assert find_item(window, "skillsButton") is not None, "Skills need a discoverable management entry"
    click(window, "skillsButton")
    click(window, "newSkillButton")
    find_item(window, "skillNameField").setProperty("text", "review-changes")
    find_item(window, "skillDescriptionField").setProperty("text", "Review correctness using the sapphire checklist.")
    find_item(window, "skillBodyField").setProperty("text", "# Review changes\n\nCheck **correctness** and tests.\n\n- Inspect the diff\n- Explain risks\n")
    save_screenshot(window, "skills-editor")
    click(window, "saveSkillButton")
    view = controller.skillView
    until(lambda: bool(view.rows.rows) or bool(view.editorError), view.changed)
    assert not view.editorError
    until(lambda: bool(view.detail.get("body")), view.changed)
    source = project / ".agents/skills/review-changes/SKILL.md"
    original = source.read_bytes()
    preview = find_item(window, "skillInstructionsPreview")
    assert preview is not None
    assert "Review changes" in preview.property("text")
    save_screenshot(window, "skills-detail")
    click(window, "useSkillButton")
    type_message(window, "Please check this project.", append=True)
    click(window, "sendButton")
    until(lambda: len(model_server) == 1, controller.changed)
    first_system = model_server[0].request["messages"][0]["content"]
    assert "sapphire checklist" in first_system
    click(window, "skillsButton")
    click(window, "toggleSkillButton")
    until(lambda: view.detail.get("state") == "disabled", view.changed)
    assert controller.status == "running", "Changing availability must leave the active request alone"
    assert "sapphire checklist" in first_system
    model_server[0].release.set()
    until(lambda: controller.status == "idle", controller.changed)
    click(window, "closeSkillsButton")
    type_message(window, "Now summarize the project.")
    click(window, "sendButton")
    until(lambda: len(model_server) == 2, controller.changed)
    assert "sapphire checklist" not in model_server[1].request["messages"][0]["content"]
    model_server[1].release.set()
    until(lambda: controller.status == "idle", controller.changed)
    click(window, "skillsButton")
    click(window, "toggleSkillButton")
    until(lambda: view.detail.get("effective"), view.changed)
    window.setProperty("dark", True)
    window.setWidth(800)
    window.setHeight(600)
    save_screenshot(window, "skills-dark-narrow")
    click(window, "removeSkillButton")
    click(window, "confirmRemoveSkillButton")
    until(lambda: view.detail.get("state") == "removed" and not view.rows.rows, view.changed)
    assert source.read_bytes() == original, "Removing a shared/project skill must preserve its resources"
    click(window, "toggleSkillButton")
    until(lambda: view.detail.get("effective") and bool(view.rows.rows), view.changed)
    assert not view.error


def test_desktop_skills_real_catalog_virtualization(desktop, model_server, project, capfd):
    root = project / ".agents/skills"
    for i in range(1000):
        folder = root / f"check-{i:04}"
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text(f"---\nname: {folder.name}\ndescription: Review component {i}.\n---\n# Component {i}\n\nCheck **correctness**.\n")
    controller, window = desktop
    controller.start()
    until(lambda: bool(controller.projects), controller.changed)
    pulses = [time.perf_counter()]
    timer = QTimer()
    timer.setInterval(10)
    timer.timeout.connect(lambda: pulses.append(time.perf_counter()))
    timer.start()
    start = time.perf_counter()
    click(window, "skillsButton")
    skills = controller.skillView
    until(lambda: skills.rows.rowCount() == 1000 or bool(skills.error), skills.changed)
    assert not skills.error
    view = find_item(window, "skillList")
    frame = QSignalSpy(window.frameSwapped)
    window.update()
    assert frame.count() or frame.wait(2000)
    open_ms = round((time.perf_counter() - start) * 1000)
    resets = QSignalSpy(skills.rows.modelReset)
    for fraction in (0.5, 1, 0):
        view.setProperty("contentY", max(0, view.property("contentHeight") - view.height()) * fraction)
        frame = QSignalSpy(window.frameSwapped)
        window.update()
        assert frame.count() or frame.wait(2000)
    live = [item for item in view.childItems()[0].childItems() if item.objectName().startswith("skillRow_")]
    click(window, "refreshSkillsButton")
    until(lambda: not skills.busy, skills.changed)
    assert resets.count() == 0
    click(window, "skillRow_check-0000")
    until(lambda: skills.detail.get("body"), skills.changed)
    timer.stop()
    metrics = {"skills": 1000, "open_ms": open_ms, "live_delegates": len(live),
               "max_gui_ms": round(max(b-a for a, b in zip(pulses, pulses[1:], strict=False)) * 1000)}
    with capfd.disabled():
        print("SKILLS_BENCHMARK", json.dumps(metrics))
    assert len(live) < 30 and open_ms < 1000 and metrics["max_gui_ms"] < 150
    save_screenshot(window, "skills-thousand")


def test_desktop_automation_creates_runs_and_opens_results(desktop, model_server):
    controller, window = desktop
    controller.start()
    until(lambda: bool(controller.projects), controller.changed)
    save_screenshot(window, "automation-entry")
    assert find_item(window, "automationsButton") is not None, "Scheduled work needs a discoverable Automations entry"
    click(window, "automationsButton")
    click(window, "newAutomationButton")
    find_item(window, "automationNameField").setProperty("text", "Daily project brief")
    find_item(window, "automationPromptField").setProperty("text", "Summarize project progress and next steps.")
    find_item(window, "automationCountField").setProperty("text", "3")
    save_screenshot(window, "automation-editor")
    click(window, "saveAutomationButton")
    automation = controller.automations
    until(lambda: len(automation.rows.rows) == 1 or bool(automation.editorState["error"]), automation.changed, automation.editorChanged)
    assert not automation.editorState["error"]
    assert automation.rows.rows[0]["remaining"] == 3
    click(window, "runAutomationNowButton")
    until(lambda: len(model_server) == 1, controller.changed, automation.changed, window.frameSwapped)
    save_screenshot(window, "automation-running")
    click(window, "pauseAutomationButton")
    until(lambda: not automation.detail.get("enabled", True) or bool(automation.error), automation.changed)
    assert not automation.error
    assert automation.detail["runs"][0]["status"] == "running", "Pausing a schedule must leave the current run alone"
    model_server[0].release.set()
    until(lambda: bool(automation.detail["runs"]) and automation.detail["runs"][0]["status"] == "completed", automation.changed)
    assert automation.rows.rows[0]["remaining"] == 3, "Manual tests must not consume scheduled repetitions"
    click(window, "openAutomationRun_" + automation.detail["runs"][0]["id"])
    until(lambda: controller.connected and any(r["body"] == "Hello 世界" for r in controller._transcript.rows), controller.changed)
    # Opening the completed run auto-reviews it; wait for the backend update.
    until(lambda: controller.board.needsReview.rowCount() == 0
          and controller.board.reviewedSessions.rowCount() == 1, controller.board.changed)
    first_chat = controller.chatId
    click(window, "automationsButton")
    click(window, "pauseAutomationButton")
    until(lambda: automation.detail.get("enabled"), automation.changed)
    click(window, "runAutomationNowButton")
    until(lambda: len(model_server) == 2 and len(automation.detail["runs"]) == 2, automation.changed, window.frameSwapped)
    click(window, "stopAutomationRun_" + automation.detail["runs"][0]["id"])
    until(lambda: automation.detail["runs"][0]["status"] == "stopped", automation.changed)
    assert automation.detail["remaining"] == 3
    model_server[1].release.set()
    click(window, "editAutomationButton")
    until(lambda: (item := find_item(window, "automationNameField")) is not None and item.isVisible(), automation.editRequested, window.frameSwapped)
    find_item(window, "automationNameField").setProperty("text", "Weekly project brief · 中文")
    click(window, "saveAutomationButton")
    until(lambda: automation.detail.get("name") == "Weekly project brief · 中文", automation.changed)
    assert automation.detail["remaining"] == 3
    window.setProperty("dark", True)
    window.setWidth(800)
    window.setHeight(600)
    frame = QSignalSpy(window.frameSwapped)
    window.update()
    assert frame.count() or frame.wait(2000)
    history = find_item(window, "automationRunHistory")
    until(lambda: history.property("atYBeginning"), history.contentYChanged, window.frameSwapped)
    assert history.property("atYBeginning"), (history.property("contentY"), history.property("originY"))
    title = find_item(window, "automationDetailTitle")
    assert title.height() >= title.implicitHeight() - 1, (title.height(), title.implicitHeight())
    assert visible_rect(window, title).height() >= title.height() - 1, "Resizing must not clip the task heading"
    save_screenshot(window, "automation-dark-narrow")
    click(window, "editAutomationButton")
    until(lambda: (item := find_item(window, "automationNameField")) is not None and item.isVisible(), automation.editRequested, window.frameSwapped)
    save_screenshot(window, "automation-editor-narrow")
    QTest.keyClick(window, Qt.Key.Key_Escape)
    click(window, "removeAutomationButton")
    click(window, "confirmRemoveAutomationButton")
    until(lambda: not automation.rows.rows, automation.changed)
    assert any(chat["id"] == first_chat for project in controller.projects for chat in project["chats"])


def test_desktop_automation_list_virtualizes_and_preserves_state(desktop, capfd):
    controller, window = desktop
    automation = controller.automations
    controller._projects = [{"id": "p", "name": "Project", "machine": "local", "chats": []}]
    automation._snapshots["local"] = [
        {"id": f"a{i}", "name": f"Task {i} · 中文", "project_id": "p", "enabled": i % 2 == 0,
         "remaining": 3, "created_at": i, "active_run": "", "next_due": None} for i in range(1000)
    ]
    automation._navigation_changed()
    resets = QSignalSpy(automation.rows.modelReset)
    pulses = [time.perf_counter()]
    heartbeat = QTimer()
    heartbeat.setInterval(10)
    heartbeat.timeout.connect(lambda: pulses.append(time.perf_counter()))
    heartbeat.start()
    opened = time.perf_counter()
    click(window, "automationsButton")
    view = find_item(window, "automationList")
    assert view.property("count") == 1000
    open_ms = (time.perf_counter() - opened) * 1000
    for fraction in (0.5, 1, 0):
        view.setProperty("contentY", max(0, view.property("contentHeight") - view.height()) * fraction)
        frame = QSignalSpy(window.frameSwapped)
        window.update()
        assert frame.count() or frame.wait(2000)
    live = [item for item in view.childItems()[0].childItems() if item.objectName().startswith("automationTask_")]
    assert len(live) < 30
    click(window, "automationStatusFilter")
    QTest.keyClick(window, Qt.Key.Key_Down)
    QTest.keyClick(window, Qt.Key.Key_Return)
    assert automation.rows.rowCount() == 500
    click(window, "closeAutomationsButton")
    click(window, "automationsButton")
    assert find_item(window, "automationStatusFilter").property("currentIndex") == 1
    automation._navigation_changed()
    assert resets.count() == 0
    heartbeat.stop()
    metrics = {"summaries": 1000, "open_ms": round(open_ms), "live_delegates": len(live),
               "max_gui_ms": round(max(b-a for a, b in zip(pulses, pulses[1:], strict=False)) * 1000)}
    with capfd.disabled():
        print("AUTOMATION_BENCHMARK", json.dumps(metrics))
    assert open_ms < 500 and metrics["max_gui_ms"] < 150
    save_screenshot(window, "automation-thousand-tasks")


def test_desktop_session_board_filters_and_virtualizes_large_summary_lists(desktop):
    """Benchmark the native view with summaries; HTTP tests cover their durable source."""
    from datetime import UTC, datetime, timedelta

    controller, window = desktop
    board = controller.board
    machines = [{"id": key, "name": name, "online": True} for key, name in
                (("local", "This Mac"), ("remote", "Development server"))]
    projects: list[dict] = [{"id": f"p{i}", "name": f"Project {i}", "machine": machines[i % 2]["id"],
                 "machine_name": machines[i % 2]["name"], "chats": []} for i in range(4)]
    start = datetime(2026, 1, 1, tzinfo=UTC)
    for i in range(1000):
        complete = i % 3 != 0
        projects[i % 4]["chats"].append({
            "id": f"c{i}", "title": f"Session {i} — 中文任务", "status": "idle" if complete else "running",
            "completion_seq": 12 if complete else -1, "reviewed_through": 12 if i % 3 == 2 else -1,
            "completed_at": (start + timedelta(minutes=i)).isoformat() if complete else "",
            "started_at": (start + timedelta(minutes=i)).isoformat(), "completion_reason": "completed",
        })
    # One archived result must never appear in the board.
    projects[1]["chats"].append({**projects[1]["chats"][0], "id": "archived", "archived": True})
    board.update(machines, projects)
    assert board.totals == [334, 333, 333]
    assert board.needsReview.rowCount() == board.reviewedSessions.rowCount() == 20
    resets = [QSignalSpy(model.modelReset) for model in (board.activeSessions, board.needsReview, board.reviewedSessions)]

    def next_frame():
        frames = QSignalSpy(window.frameSwapped)
        window.update()
        assert frames.count() or frames.wait(2000)

    pulses = [time.perf_counter()]
    heartbeat = QTimer()
    heartbeat.setInterval(10)
    heartbeat.timeout.connect(lambda: pulses.append(time.perf_counter()))
    heartbeat.start()
    opened = time.perf_counter()
    click(window, "sessionBoardButton")
    next_frame()
    open_ms = (time.perf_counter() - opened) * 1000
    assert board.needsReview.rows[0]["id"] == "c997"
    live: list = []
    for key in ("active", "review", "reviewed"):
        view = find_item(window, "boardList_" + key)
        for fraction in (0.5, 1, 0):
            view.setProperty("contentY", max(0, view.property("contentHeight") - view.height()) * fraction)
            next_frame()
        live.extend(item for item in view.childItems()[0].childItems() if item.objectName().startswith("boardCard_"))
    assert len(live) < 45
    view = find_item(window, "boardList_review")
    view.setProperty("contentY", max(0, view.property("contentHeight") - view.height()))
    next_frame()
    click(window, "boardMore_review")
    assert board.needsReview.rowCount() == 40
    assert len({row["id"] for row in board.needsReview.rows}) == 40
    assert [row["id"] for row in board.needsReview.rows] == [f"c{i}" for i in range(997, 997 - 120, -3)]
    # Use native combo interaction, then revisit the board after a summary refresh.
    click(window, "boardMachineFilter")
    QTest.keyClick(window, Qt.Key.Key_End)
    QTest.keyClick(window, Qt.Key.Key_Return)
    assert all(row["machine"] == "Development server" for row in board.needsReview.rows)
    click(window, "boardProjectFilter")
    QTest.keyClick(window, Qt.Key.Key_End)
    QTest.keyClick(window, Qt.Key.Key_Return)
    assert all(row["project_id"] == "p3" for row in board.needsReview.rows)
    board.update(machines, projects)
    next_frame()
    assert find_item(window, "boardMachineFilter").property("currentValue") == "remote"
    assert find_item(window, "boardProjectFilter").property("currentValue") == "p3"
    click(window, "closeSessionBoardButton")
    click(window, "sessionBoardButton")
    assert find_item(window, "boardMachineFilter").property("currentValue") == "remote"
    assert find_item(window, "boardProjectFilter").property("currentValue") == "p3"
    next_frame()
    heartbeat.stop()
    metrics = {"summaries": 1000, "open_ms": round(open_ms), "max_gui_ms": round(max(b-a for a,b in zip(pulses, pulses[1:], strict=False)) * 1000), "live_delegates": len(live)}
    print("BOARD_BENCHMARK", json.dumps(metrics))
    assert open_ms < 500 and metrics["max_gui_ms"] < 150
    assert all(spy.count() == 0 for spy in resets)
    # Qt's exhaustive model checker is intentionally outside native performance timing.
    board.filter("search", "Session 99")
    testers = [QAbstractItemModelTester(model, QAbstractItemModelTester.FailureReportingMode.Warning)
               for model in (board.activeSessions, board.needsReview, board.reviewedSessions)]
    changed_projects = [{**p, "chats": [{**chat, "started_at": "2027"} if chat["id"] == "c99" else chat for chat in p["chats"]]}
                        for p in projects]
    board.update(machines, changed_projects)
    board.update(machines, projects)
    board.filter("search", "Session 999")
    assert len(testers) == 3
    save_screenshot(window, "board-filtered")
    # A hidden project leaves no cards, and its now-invalid selected filter clears.
    board.update(machines, projects[:3])
    assert board.filters["project"] == ""
    assert not any(row["project_id"] == "p3" for model in (board.activeSessions, board.needsReview, board.reviewedSessions) for row in model.rows)
    board.filter("search", "")
    board.filter("outcome", "attention")
    stopped = {**projects[1], "chats": [
        {**projects[1]["chats"][0], "id": "failed", "completion_reason": "provider_error"},
        {**projects[1]["chats"][0], "id": "paused", "status": "paused", "completion_reason": "user_pause"},
    ]}
    board.update([{**m, "online": False} for m in machines], [stopped])
    assert board.totals == [0, 2, 0]
    outcomes = {row["id"]: row["label"] for row in board.needsReview.rows}
    assert outcomes == {"failed": "Failed", "paused": "Paused"}
    assert not board.needsReview.rows[0]["online"]
    next_frame()
    assert not find_item(window, "reviewBoardChat_failed").property("enabled")
    save_screenshot(window, "board-offline")
    # A pause is briefly active before its closing record is durable, rather than vanishing.
    waiting_for_record = {**stopped, "chats": [{
        **stopped["chats"][1], "completion_seq": -1, "completion_reason": "",
    }]}
    board.update(machines, [waiting_for_record])
    assert board.totals == [1, 0, 0]
    assert board.activeSessions.rows[0]["label"] == "Paused"
    board.filter("outcome", "")
    board.update(machines, [{**stopped, "chats": [{"id": "legacy", "title": "Legacy backend", "status": "running"}]}])
    assert board.notice and board.activeSessions.rowCount() == 1
    assert not board.needsReview.rowCount()


def test_desktop_browser_screenshot_tool_result(desktop, model_server, home, project):
    import base64

    from ava.tool.mcp import MCPServers, ServerConfig
    from tests.test_mcp import FIXTURE

    settings = json.loads((home / "settings.json").read_text())
    settings["model"] = "fixture-mcp-image"
    settings["providers"]["desktop-test"]["models"]["fixture-mcp-image"] = {"context_window": 10000}
    (home / "settings.json").write_text(json.dumps(settings))
    screenshot = project / "captured-viewport.png"
    servers = MCPServers(home)
    servers.save(ServerConfig(name="Page capture", command=sys.executable,
                              args=[str(FIXTURE)], env={"MCP_IMAGE_PATH": str(screenshot)}))
    controller, window = desktop
    controller.start()
    until(lambda: bool(controller.projects), controller.changed)
    controller.newChat()
    until(lambda: controller.connected and not controller._busy, controller.changed)
    click(window, "toggleRightSidebar")
    click(window, "browserTab")
    browser = find_item(window, "webBrowser")
    address = settings["providers"]["desktop-test"]["base_url"].removesuffix("/v1") + "/preview"
    browser.setProperty("url", QUrl(address))
    until(lambda: browser.property("title") == "Browser ready" and not browser.property("loading"), window.frameSwapped)
    # Chromium can finish navigation before its surface has reached the Qt scene.
    # The fixture's text must be visible in pixels before using it as golden input.
    deadline = time.monotonic() + 5
    while True:
        capture = browser.grabToImage()
        until(lambda captured=capture: not captured.image().isNull(), capture.ready)
        pixels = capture.image()
        assert pixels.width() > 100 and pixels.height() > 100
        colors = {pixels.pixelColor(x, y).rgba() for x in range(0, pixels.width(), 4)
                  for y in range(0, min(220, pixels.height()), 4)}
        if len(colors) > 3:
            break
        assert time.monotonic() < deadline, "The loaded page never appeared in its screenshot"
        QTest.qWait(30)
    assert capture.saveToFile(str(screenshot))
    click(window, "toggleRightSidebar")
    type_message(window, "Inspect this browser screenshot")
    QTest.keyClick(window, Qt.Key.Key_Return)
    until(lambda: len(model_server) == 2 or bool(controller.error), controller.changed, timeout=20000)
    assert len(model_server) == 2, controller.error
    messages = model_server[1].request["messages"]
    results = [message for message in messages if message["role"] == "tool"]
    assert results[-1]["content"] == "Captured browser viewport"
    images = [part for message in messages if message["role"] == "user"
              for part in message["content"] if part["type"] == "image_url"]
    assert len(images) == 1
    assert base64.b64decode(images[0]["image_url"]["url"].split(",", 1)[1]) == screenshot.read_bytes()
    model_server[1].release.set()
    until(lambda: controller.status == "idle", controller.changed)
    row = next(row for row in controller.transcript.rows if row["attachments"])
    metadata = row["attachments"][0]
    assert metadata["byte_size"] == screenshot.stat().st_size
    assert "base64" not in metadata and "bytes" not in metadata
    connection = controller._connection
    resource = f"{connection._base}/api/chats/{controller.chatId}/{metadata['path']}"
    assert httpx.get(resource).status_code in (401, 403)
    fetched = httpx.get(resource, headers={"Authorization": "Bearer " + connection._token})
    assert fetched.status_code == 200 and fetched.content == screenshot.read_bytes()
    assert fetched.headers["content-type"] == "image/png"
    assert httpx.get(resource + "999", headers={"Authorization": "Bearer " + connection._token}).status_code == 404
    name = "transcriptAttachment_" + metadata["display_path"]
    until(lambda: find_item(window, name) is not None, window.frameSwapped)
    started = time.perf_counter()
    click(window, name)
    preview = controller.attachmentPreview
    until(lambda: not preview.state["loading"], preview.changed)
    assert not preview.state["error"]
    dialog = window.findChild(QObject, "toolImageDialog")
    until(lambda: dialog.property("imageReady"), window.frameSwapped)
    first_preview_ms = (time.perf_counter() - started) * 1000
    assert first_preview_ms < 500, first_preview_ms
    print(f"Tool image preview: {first_preview_ms:.0f} ms, {metadata['byte_size']} bytes; authenticated on-demand fetch")
    save_screenshot(window, "browser-tool-image")
    cached = preview.state["url"]
    assert dialog.property("activeFocus"), "The image preview must receive keyboard input"
    QTest.keyClick(window, Qt.Key.Key_Escape)
    until(lambda: not dialog.property("visible"), window.frameSwapped)
    click(window, name)
    assert preview.state["url"] == cached and not preview.state["loading"]
    window.setProperty("dark", True)
    window.setWidth(800)
    window.setHeight(600)
    save_screenshot(window, "browser-tool-image-dark-narrow")
    QTest.keyClick(window, Qt.Key.Key_Escape)
    chat = controller.chatId
    controller._detach()
    stop_backend(home)
    controller.start()
    until(lambda: controller.connected and controller.chatId == chat, controller.changed, timeout=20000)
    until(lambda: any(row["attachments"] for row in controller.transcript.rows), controller.changed)
    restored = next(row for row in controller.transcript.rows if row["attachments"])
    assert restored["attachments"] == row["attachments"]
    click(window, name)
    until(lambda: not preview.state["loading"], preview.changed)
    assert not preview.state["error"] and preview.state["url"]
    QTest.keyClick(window, Qt.Key.Key_Escape)


def test_desktop_agent_browser_handoff_and_takeover(desktop, model_server, home):
    import base64

    settings_path = home / 'settings.json'
    settings = json.loads(settings_path.read_text())
    settings['model'] = 'fixture-browser'
    settings_path.write_text(json.dumps(settings))
    controller, window = desktop
    directory = home / 'browser'
    directory.mkdir(exist_ok=True)
    (directory / 'library.json').write_text('{"attempted": true}')
    controller.start()
    until(lambda: bool(controller.projects), controller.changed)
    controller.newChat()
    until(lambda: controller.connected and not controller._busy, controller.changed)
    click(window, 'toggleRightSidebar')
    click(window, 'browserTab')
    click(window, 'browserHandoff')
    control = controller.browserControl
    until(lambda: bool(control.tabs.get('1', {}).get('active')), control.changed)
    assert control.tabs['1']['chat'] == controller.chatId
    browser = find_item(window, 'webBrowser')
    ticks = [time.perf_counter()]
    timer = QTimer()
    timer.setInterval(10)
    timer.timeout.connect(lambda: ticks.append(time.perf_counter()))
    timer.start()
    type_message(window, 'Complete the form in the shared browser, then capture it.')
    QTest.keyClick(window, Qt.Key.Key_Return)
    until(lambda: len(model_server) >= 13 or bool(controller.error), controller.changed, timeout=20000)
    timer.stop()
    assert len(model_server) == 13, controller.error
    stall = max(right-left for left, right in zip(ticks, ticks[1:], strict=False)) * 1000
    assert stall < 150, f'Browser control stalled the GUI for {stall:.0f} ms'
    print(f'Browser control: 12 actual agent actions, maximum GUI interval {stall:.0f} ms')
    messages = model_server[-1].request['messages']
    results = [message for message in messages if message['role'] == 'tool']
    assert 'Ava 中文 / input true / click true' in json.loads(results[2]['content'])['text']
    assert any(element['name'] == 'Shadow note' and element['value'] == 'Nested input' for element in json.loads(results[5]['content'])['elements'])
    assert any(element['name'] == 'Priority' and element['value'] == 'High' for element in json.loads(results[6]['content'])['elements'])
    assert any(element['name'] == 'Frame note' and element['value'] == 'Nested input' for element in json.loads(results[7]['content'])['elements'])
    assert json.loads(results[10]['content'])['url'].endswith('/next')
    assert browser.property('url').toString().endswith('/agent-browser')
    images = [part for message in messages if message['role'] == 'user' for part in message['content'] if part['type'] == 'image_url']
    assert len(images) == 1
    pixels = QImage.fromData(base64.b64decode(images[0]['image_url']['url'].split(',', 1)[1]))
    assert not pixels.isNull() and pixels.width() > 100 and pixels.height() > 100
    model_server[-1].release.set()
    until(lambda: controller.status == 'idle', controller.changed)
    save_screenshot(window, 'agent-browser-result')
    width, height = window.width(), window.height()
    window.setProperty('dark', True)
    window.setWidth(900)
    window.setHeight(650)
    save_screenshot(window, 'agent-browser-dark-narrow')
    takeover = find_item(window, 'browserTakeOver')
    right = takeover.mapToScene(QPointF(takeover.width(), takeover.height()))
    assert right.x() <= window.width() and right.y() <= window.height()
    click(window, 'browserTakeOver')
    until(lambda: not control.tabs.get('1', {}).get('active'), control.changed)
    window.setProperty('dark', False)
    window.setWidth(width)
    window.setHeight(height)
    click(window, 'browserHandoff')
    until(lambda: bool(control.tabs.get('1', {}).get('active')), control.changed)
    point = browser.mapToScene(QPointF(20, 20)).toPoint()
    QTest.mouseClick(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point)
    assert not control.tabs.get('1', {}).get('active'), 'User input must revoke agent control before reaching the page'
    click(window, 'browserHandoff')
    until(lambda: bool(control.tabs.get('1', {}).get('active')), control.changed)
    count = len(model_server)
    type_message(window, 'Wait for the missing page text.')
    QTest.keyClick(window, Qt.Key.Key_Return)
    until(lambda: bool(control.tabs.get('1', {}).get('working')), control.changed)
    started = time.perf_counter()
    click(window, 'browserTakeOver')
    assert not control.tabs.get('1', {}).get('active'), control.tabs
    until(lambda: len(model_server) >= count + 2 or bool(controller.error), controller.changed)
    assert not controller.error, controller.error
    assert len(model_server) == count + 2
    assert (time.perf_counter() - started) < 1
    assert not control.tabs.get('1', {}).get('active')
    cancelled = [message for message in model_server[-1].request['messages'] if message['role'] == 'tool'][-1]
    assert 'took control' in cancelled['content']
    model_server[-1].release.set()
    until(lambda: controller.status == 'idle', controller.changed)
    transcript = find_item(window, 'transcriptView')
    transcript.setProperty('follow', False)
    QMetaObject.invokeMethod(transcript, 'positionViewAtBeginning')
    click(window, 'activityGroupToggle')
    until(lambda: (summary := find_item(window, 'activitySummary')) is not None and summary.property('text').startswith('Open page'), window.frameSwapped)
    click(window, 'closeInspectorTab_1')
    assert isValid(window)


def test_desktop_agent_browser_rejects_covered_and_stale_targets(desktop, model_server, home):
    settings_path = home / 'settings.json'
    settings = json.loads(settings_path.read_text())
    settings['model'] = 'fixture-browser-guards'
    settings_path.write_text(json.dumps(settings))
    directory = home / 'browser'
    directory.mkdir(exist_ok=True)
    (directory / 'library.json').write_text('{"attempted": true}')
    controller, window = desktop
    controller.start()
    until(lambda: bool(controller.projects), controller.changed)
    controller.newChat()
    until(lambda: controller.connected and not controller._busy, controller.changed)
    click(window, 'toggleRightSidebar')
    click(window, 'browserTab')
    click(window, 'browserHandoff')
    control = controller.browserControl
    until(lambda: bool(control.tabs.get('1', {}).get('active')), control.changed)
    type_message(window, 'Check the guarded form.')
    QTest.keyClick(window, Qt.Key.Key_Return)
    until(lambda: len(model_server) >= 9 or bool(controller.error), controller.changed, timeout=20000)
    assert len(model_server) == 9, controller.error
    results = [message for message in model_server[-1].request['messages'] if message['role'] == 'tool']
    assert 'covers this target' in results[2]['content']
    assert any(element['name'] == 'Guarded field' and element['value'] == '' for element in json.loads(results[3]['content'])['elements'])
    assert any(element['name'] == 'Guarded field' and element['value'] == 'Accepted' for element in json.loads(results[4]['content'])['elements'])
    assert 'reference is stale' in results[6]['content']
    assert 'could not be loaded' in results[7]['content'], results[7]['content']
    model_server[-1].release.set()
    until(lambda: controller.status == 'idle', controller.changed)
    assert control.tabs['1']['active']
    type_message(window, '/context')
    QTest.keyClick(window, Qt.Key.Key_Return)
    until(lambda: not control.tabs.get('1', {}).get('active'), control.changed)
    dialog = window.findChild(QObject, 'contextDialog')
    assert dialog is not None and dialog.property('visible')
    save_screenshot(window, 'agent-browser-context-takeover')


def test_desktop_analytics_history_filters_and_live_skill_usage(desktop, model_server, home, project, monkeypatch):
    from datetime import UTC, datetime, timedelta
    from zoneinfo import ZoneInfo

    from ava.session import (
        Log,
        StepEnd,
        StepEndReason,
        StepStart,
        TurnEnd,
        TurnEndReason,
        TurnStart,
        Usage,
        UserMessage,
    )
    from tests.conftest import message

    controller, window = desktop
    analytics = controller.analytics
    today = datetime.now(ZoneInfo(analytics.property('filters')['timezone'])).replace(hour=0, minute=1, second=0, microsecond=0)
    with monkeypatch.context() as patch:
        log = Log.create_default(project, 'desktop-test', 'fixture')
        try:
            log.append(UserMessage(message('Historical analytics fixture')))
            for index in range(30):
                at = (today - timedelta(days=29-index)).astimezone(UTC)
                patch.setattr('ava.session.log.now_ms', lambda at=at: at)
                log.append_batch([TurnStart(index+1), StepStart(index+1, 1), Usage(str(index), input=(index+1)*1000, cached_read=(index+1)*500, cache_write=(index+1)*100, output=(index+1)*250)])
                patch.setattr('ava.session.log.now_ms', lambda at=at: at+timedelta(minutes=2))
                log.append_batch([StepEnd(index+1, 1, StepEndReason.completed), TurnEnd(index+1, TurnEndReason.completed)])
            log.sync()
        finally:
            log.close()
    other = project.parent/'other-project'
    other.mkdir()
    log = Log.create_default(other, 'desktop-test', 'fixture')
    log.append(UserMessage(message('A project without measured activity')))
    log.close()
    controller.start()
    until(lambda: bool(controller.projects), controller.changed)
    ticks = [time.perf_counter()]
    timer = QTimer()
    timer.setInterval(10)
    timer.timeout.connect(lambda: ticks.append(time.perf_counter()))
    timer.start()
    first_started = time.perf_counter()
    click(window, 'analyticsButton')
    until(lambda: analytics.data['totals']['tokens'] == 349650, analytics.changed)
    timer.stop()
    first_ms = (time.perf_counter()-first_started)*1000
    gap = max((b-a)*1000 for a, b in zip(ticks, ticks[1:], strict=False)) if len(ticks)>1 else 0
    assert first_ms < 500 and gap < 150, (first_ms, gap)
    assert analytics.data['totals']['active_ms'] == 14*60000
    assert len(analytics.data['days']) == 7
    assert find_item(window, 'analyticsTokens').property('value') == '349.6k'
    save_screenshot(window, 'analytics-week')
    assert find_item(window, 'pageHeader').property('title') == 'Session information'
    assert find_item(window, 'analytics7d').property('checked')
    # The rendered chart and composition bar must encode the real aggregates,
    # not merely show plausible shapes. Gate reflow in both appearances too.
    original_size = (window.width(), window.height())
    for dark, width, height in [(False, 1280, 820), (True, 1280, 820), (False, 800, 600), (True, 800, 600)]:
        window.setProperty('dark', dark)
        window.setWidth(width)
        window.setHeight(height)
        save_screenshot(window, f'analytics-overview-{dark}-{width}')
        pane = find_item(window, 'analyticsPane')
        chart = find_item(window, 'analyticsChart')
        mix = find_item(window, 'analyticsTokenMix')
        assert chart.width() > 250
        for item in (chart, mix):
            assert item.mapToScene(QPointF(0, 0)).x() >= 0
            assert item.mapToScene(QPointF(item.width(), 0)).x() <= window.width()
        if pane.property('narrow'):
            assert mix.mapToScene(QPointF(0, 0)).y() >= chart.mapToScene(QPointF(0, chart.height())).y()
        else:
            assert mix.mapToScene(QPointF(0, 0)).x() > chart.mapToScene(QPointF(chart.width(), 0)).x()
        for index, day in enumerate(analytics.data['days']):
            bar = find_item(window, f'analyticsBar_{index}')
            expected = chart.property('plotHeight') * day['tokens'] / chart.property('maximum')
            assert bar.height() == pytest.approx(expected)
        for key in ('input', 'cached_read', 'cache_write', 'output'):
            segment = find_item(window, 'analyticsTokenSegment_' + key)
            share = analytics.data['totals'][key] / analytics.data['totals']['tokens']
            assert segment.width() / segment.parentItem().width() == pytest.approx(share)
    window.setWidth(original_size[0])
    window.setHeight(original_size[1])
    window.setProperty('dark', False)
    first_day = find_item(window, 'analyticsDay_0')
    first_day.forceActiveFocus()
    QTest.keyClick(window, Qt.Key.Key_Right)
    assert find_item(window, 'analyticsDay_1').hasActiveFocus()
    started = time.perf_counter()
    click(window, 'analytics30d')
    until(lambda: len(analytics.data['days']) == 30, analytics.changed)
    assert analytics.data['totals']['tokens'] == 860250
    assert find_item(window, 'analytics30d').property('checked')
    assert not find_item(window, 'analytics7d').property('checked')
    switch_ms = (time.perf_counter()-started)*1000
    assert switch_ms < 100, switch_ms
    click(window, 'analyticsMetric_active_ms')
    assert find_item(window, 'analyticsPane').property('metric') == 'active_ms'
    window.setProperty('dark', True)
    window.setWidth(900)
    window.setHeight(700)
    save_screenshot(window, 'analytics-dark-narrow')
    chart = find_item(window, 'analyticsChart')
    assert chart.mapToScene(QPointF(chart.width(), 0)).x() <= window.width()
    project_filter = find_item(window, 'analyticsProject')
    choices = analytics.property('projects')
    other_index = next(i for i, row in enumerate(choices) if row['name'] == 'other-project')
    click(window, 'analyticsProject')
    QTest.keyClick(window, Qt.Key.Key_Home)
    for _ in range(other_index):
        QTest.keyClick(window, Qt.Key.Key_Down)
    QTest.keyClick(window, Qt.Key.Key_Return)
    until(lambda: analytics.property('filters')['project'] == choices[other_index]['id'] and analytics.data['reports'] > 0, analytics.changed)
    assert analytics.data['totals']['tokens'] == 0
    assert project_filter.property('currentText') == 'other-project'
    save_screenshot(window, 'analytics-empty-project')
    click(window, 'analyticsProject')
    QTest.keyClick(window, Qt.Key.Key_Home)
    QTest.keyClick(window, Qt.Key.Key_Return)
    until(lambda: analytics.data['totals']['tokens'] == 860250, analytics.changed)
    click(window, 'closeAnalytics')
    skill = project/'.agents/skills/review/SKILL.md'
    skill.parent.mkdir(parents=True)
    skill.write_text('---\nname: review\ndescription: Review code carefully.\n---\nRead the change and report findings.\n')
    settings = json.loads((home/'settings.json').read_text())
    settings['model'] = 'fixture-analytics'
    (home/'settings.json').write_text(json.dumps(settings))
    controller.newChat()
    until(lambda: controller.connected and not controller._busy, controller.changed)
    controller.selectModel('fixture-analytics')
    until(lambda: controller.selection.get('model') == 'fixture-analytics', controller.changed)
    type_message(window, 'Load the review skill and summarize it.')
    QTest.keyClick(window, Qt.Key.Key_Return)
    until(lambda: len(model_server) == 2, controller.changed)
    click(window, 'analyticsButton')
    updated = time.perf_counter()
    model_server[-1].release.set()
    until(lambda: analytics.data['totals']['skills'] == 1 and analytics.data['totals']['tokens'] == 861530, analytics.changed)
    update_ms = (time.perf_counter()-updated)*1000
    assert update_ms < 1000, update_ms
    until(lambda: controller.status == 'idle', controller.changed)
    assert analytics.data['totals']['tools'] == 1
    assert analytics.data['skills'][0]['name'] == 'review'
    assert analytics.data['totals']['missing_usage'] == 1
    save_screenshot(window, 'analytics-live')
    for kind in ('tools', 'skills'):
        entry = find_item(window, f'analyticsRank_{kind}_0')
        assert entry.property('share') == 1
        track = find_item(window, f'analyticsRankTrack_{kind}_0')
        assert track.childItems()[0].width() == pytest.approx(track.width())
    print(f'Analytics native: first display {first_ms:.0f} ms, GUI gap {gap:.0f} ms, 7/30-day switch {switch_ms:.0f} ms, live update {update_ms:.0f} ms; project filters, durable history and actual skill read/provider usage verified')

    connection = controller._connection
    until(lambda: not analytics._pending, connection._network.finished)
    request = connection._request
    def delayed_analytics(path):
        result = request(path)
        if path.startswith('/api/analytics?'):
            result.setUrl(QUrl(settings['providers']['desktop-test']['base_url'].removesuffix('/v1') + '/pending-analytics'))
        return result
    monkeypatch.setattr(connection, '_request', delayed_analytics)
    analytics.refresh()
    assert analytics._pending
    stop_backend(home)
    until(lambda: controller._connection is not connection and controller.connected, controller.changed, timeout=15000)
    click(window, 'closeAnalytics')
    controller.newChat()
    until(lambda: controller.connected and not controller._busy, controller.changed)
    type_message(window, 'Read the review skill after reconnecting.')
    QTest.keyClick(window, Qt.Key.Key_Return)
    until(lambda: len(model_server) == 4, controller.changed)
    model_server[-1].release.set()
    until(lambda: controller.status == 'idle', controller.changed)
    resumed = time.perf_counter()
    click(window, 'analyticsButton')
    until(lambda: analytics.data['totals']['tokens'] == 862810, analytics.changed, timeout=3000)
    resumed_ms = (time.perf_counter()-resumed)*1000
    assert resumed_ms < 1000, resumed_ms
    assert analytics.data['totals']['skills'] == 2
    assert not analytics.data['notice'], analytics.data['notice']
    save_screenshot(window, 'analytics-reconnected')
    print(f'Analytics reconnect: new task totals visible in {resumed_ms:.0f} ms after an interrupted HTTP request')


@pytest.mark.skipif(os.environ.get('AVA_DESKTOP_SSH_TESTS') != '1' or not os.environ.get('AVA_SSH_TEST_CONTAINER'), reason='Requires the isolated Fedora SSH fixture')
def test_desktop_remote_analytics_and_browser_handoff(desktop, model_server, home, project):
    from ava.app.desktop.ssh import ssh_arguments

    ssh = ssh_arguments('ava-test')
    def remote(code, data=None):
        result = subprocess.run([*ssh, 'ava-test', shlex.join(['/opt/ava/bin/python', '-c', code])],
                                input=data, capture_output=True, text=True, timeout=40)
        assert result.returncode == 0, result.stderr
        return result.stdout

    config = json.loads((home/'settings.json').read_text())
    config['model'] = 'fixture-analytics'
    for name in ('fixture-analytics', 'fixture-browser'):
        config['providers']['desktop-test']['models'][name] = {'context_window': 100000}
    (home/'settings.json').write_text(json.dumps(config))
    skill = project/'.agents/skills/review/SKILL.md'
    skill.parent.mkdir(parents=True)
    skill.write_text('---\nname: review\ndescription: Local review instructions.\n---\nRead the local workspace.\n')
    local_port = config['providers']['desktop-test']['base_url'].split(':')[-1].split('/')[0]
    port = remote("import socket; s=socket.socket(); s.bind(('127.0.0.1',0)); print(s.getsockname()[1]); s.close()").strip()
    config['providers']['desktop-test']['base_url'] = f'http://127.0.0.1:{port}/v1'
    remote(r"""
import sys
from pathlib import Path
from ava.llm.credentials import save_api_key
p=Path.home()
(p/'.ava').mkdir(exist_ok=True)
(p/'.ava/settings.json').write_text(sys.stdin.read())
save_api_key('desktop-test','local-test-only')
skill=p/'analytics project/.agents/skills/review/SKILL.md'
skill.parent.mkdir(parents=True,exist_ok=True)
skill.write_text('---\nname: review\ndescription: Fedora review instructions.\n---\nRead the Fedora workspace.\n')
""", json.dumps(config))
    model_tunnel = subprocess.Popen([*ssh, '-N', '-o', 'ExitOnForwardFailure=yes', '-R', f'127.0.0.1:{port}:127.0.0.1:{local_port}', 'ava-test'],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    controller, window = desktop
    try:
        controller.start()
        until(lambda: bool(controller.projects), controller.changed)
        controller.newChat()
        until(lambda: controller.connected and not controller._busy, controller.changed)
        local_chat = controller.chatId
        type_message(window, 'Read the local review skill.')
        click(window, 'sendButton')
        until(lambda: len(model_server) == 2, controller.changed)
        # Keep this turn open while the remote turn runs, creating a real overlap.
        click(window, 'machinesButton')
        find_item(window, 'machineHostField').setProperty('text', 'ava-test')
        find_item(window, 'machineNameField').setProperty('text', 'Fedora acceptance')
        click(window, 'addMachineAction')
        until(lambda: len(controller.machines) == 2 and (controller.machines[1]['online'] or controller.machines[1]['error']), controller.machinesChanged, timeout=180000)
        state = controller.machines[1]
        assert state['online'], state['error']
        machine = controller._machines[state['id']]
        assert 'analytics' in machine.runtime.info['capabilities']
        instance = machine.runtime.info['instance_id']
        click(window, 'selectMachine_'+machine.id)
        click(window, 'addProject_'+machine.id)
        find_item(window, 'remoteProjectPath').setProperty('text', '~/analytics project')
        click(window, 'addRemoteProjectAction')
        until(lambda: controller.projectPath.endswith('analytics project'), controller.changed)
        controller.newChat()
        until(lambda: controller.connected and not controller._busy, controller.changed)
        remote_chat = controller.chatId
        assert remote_chat.endswith('~'+local_chat), (remote_chat, local_chat)
        type_message(window, 'Read the Fedora review skill.')
        click(window, 'sendButton')
        until(lambda: len(model_server) == 4, controller.changed, timeout=20000)
        assert any(message['role'] == 'tool' and 'Read the Fedora workspace' in message['content'] for message in model_server[3].request['messages'])
        model_server[3].release.set()
        until(lambda: controller.status == 'idle', controller.changed)
        controller.openChat(local_chat)
        until(lambda: controller.chatId == local_chat and controller.status == 'running', controller.changed)
        model_server[1].release.set()
        until(lambda: controller.status == 'idle', controller.changed)
        click(window, 'analyticsButton')
        analytics = controller.analytics
        until(lambda: analytics.data['totals']['tokens'] == 2560 and analytics.data['reports'] == 2, analytics.changed, timeout=20000)
        assert analytics.data['totals']['skills'] == analytics.data['totals']['tools'] == 2
        assert analytics.data['totals']['active_ms'] < analytics.data['totals']['run_ms'], 'Parallel machines must not double-count active time'
        assert not analytics.data['notice'], analytics.data['notice']
        save_screenshot(window, 'remote-analytics-combined')
        click(window, 'analyticsMachine')
        QTest.keyClick(window, Qt.Key.Key_End)
        QTest.keyClick(window, Qt.Key.Key_Return)
        until(lambda: analytics.property('filters')['machine'] == machine.id and analytics.data['totals']['tokens'] == 1280, analytics.changed)
        click(window, 'analyticsMachine')
        QTest.keyClick(window, Qt.Key.Key_Home)
        QTest.keyClick(window, Qt.Key.Key_Return)
        until(lambda: analytics.data['totals']['tokens'] == 2560, analytics.changed)
        click(window, 'closeAnalytics')
        controller.openChat(remote_chat)
        until(lambda: controller.chatId == remote_chat and controller.connected, controller.changed)
        controller.newChat()
        until(lambda: controller.chatId != remote_chat and controller.connected and not controller._busy, controller.changed)
        controller.selectModel('fixture-browser')
        until(lambda: controller.selection.get('model') == 'fixture-browser', controller.changed)
        (home/'browser').mkdir(exist_ok=True)
        (home/'browser/library.json').write_text('{"attempted":true}')
        click(window, 'toggleRightSidebar')
        click(window, 'browserTab')
        click(window, 'browserHandoff')
        control = controller.browserControl
        until(lambda: bool(control.tabs.get('1', {}).get('active')), control.changed)
        assert control.tabs['1']['chat'] == controller.chatId
        type_message(window, 'Complete the form using the shared desktop browser, then capture it.')
        click(window, 'sendButton')
        until(lambda: len(model_server) == 17 or bool(controller.error), controller.changed, timeout=30000)
        assert len(model_server) == 17, controller.error
        results = [message for message in model_server[-1].request['messages'] if message['role'] == 'tool']
        assert 'Ava 中文 / input true / click true' in json.loads(results[2]['content'])['text']
        assert any(part['type'] == 'image_url' for message in model_server[-1].request['messages'] if message['role'] == 'user' for part in message['content'])
        model_server[-1].release.set()
        until(lambda: controller.status == 'idle', controller.changed)
        save_screenshot(window, 'remote-browser-result')
        click(window, 'analyticsButton')
        until(lambda: analytics.data['totals']['tools'] == 14, analytics.changed)
        assert analytics.data['totals']['tokens'] == 2560
        click(window, 'closeAnalytics')
        click(window, 'browserHandoff')
        until(lambda: bool(control.tabs.get('1', {}).get('active')), control.changed)
        type_message(window, 'Wait for the missing page text.')
        click(window, 'sendButton')
        until(lambda: bool(control.tabs.get('1', {}).get('working')), control.changed)
        machine.reconnect = False
        machine.runtime.process.terminate()
        until(lambda: machine.connection is None, controller.machinesChanged)
        assert not control.tabs.get('1', {}).get('active')
        click(window, 'analyticsButton')
        until(lambda: 'offline' in analytics.data['notice'], analytics.changed)
        assert analytics.data['totals']['tokens'] == 2560
        save_screenshot(window, 'remote-analytics-offline')
        until(lambda: len(model_server) == 19, controller.changed, analytics.changed, timeout=35000)
        error = [message for message in model_server[-1].request['messages'] if message['role'] == 'tool'][-1]['content']
        assert 'desktop' in error.lower() or 'control' in error.lower(), error
        model_server[-1].release.set()
        click(window, 'machinesButton')
        click(window, 'selectMachine_'+machine.id)
        until(lambda: machine.connection is not None and controller.connected, controller.machinesChanged, controller.changed, timeout=70000)
        assert machine.runtime.info['instance_id'] == instance
        QTest.keyClick(window, Qt.Key.Key_Escape)
        until(lambda: not analytics.data['notice'] and analytics.data['totals']['tools'] == 15, analytics.changed)
        assert analytics.data['totals']['tokens'] == 2560
        assert not control.tabs.get('1', {}).get('active'), 'Reconnect must not restore browser control automatically'
        assert len(model_server) == 19, 'Reconnect must not replay completed or uncertain actions'
        # Restart the remote daemon, keeping its durable statistics and identity.
        remote("from ava.app.backend import stop; from ava.base import ava_home; stop(ava_home(),force=True)")
        until(lambda: machine.connection is None, controller.machinesChanged, timeout=15000)
        until(lambda: machine.connection is not None and machine.runtime.info['instance_id'] != instance, controller.machinesChanged, timeout=70000)
        until(lambda: analytics.data['reports'] == 2 and not analytics.data['notice'] and analytics.data['totals']['tools'] == 15, analytics.changed)
        assert analytics.data['totals']['tokens'] == 2560 and analytics.data['totals']['skills'] == 2
        assert len(model_server) == 19
        save_screenshot(window, 'remote-analytics-restarted')
        print('Remote Analytics/browser: local and Fedora totals, overlapping time, 12 native browser actions, screenshot delivery, disconnect/reconnect and daemon restart verified')
    finally:
        controller.shutdown()
        until(lambda: controller._closed_emitted, controller.closed, timeout=15000)
        model_tunnel.terminate()
        model_tunnel.wait(timeout=5)
        if model_tunnel.stderr:
            model_tunnel.stderr.close()
