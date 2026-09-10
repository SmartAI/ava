"""Qt UI and persistent backend acceptance scenarios, using a gated local model server."""

from __future__ import annotations

import gc
import json
import os
import re
import shlex
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
    until(lambda: bool(controller.modelChoices), controller.changed)
    click(window, "modelPicker")
    QTest.keyClick(window, Qt.Key.Key_Down)
    QTest.keyClick(window, Qt.Key.Key_Return)
    until(lambda: controller.selection.get("model") == "fixture-reasoning", controller.changed)
    until(
        lambda: controller.modelChoices.get("effort_values") == ["low", "high"], controller.changed
    )
    click(window, "effortPicker")
    QTest.keyClick(window, Qt.Key.Key_Down)
    QTest.keyClick(window, Qt.Key.Key_Down)
    QTest.keyClick(window, Qt.Key.Key_Return)
    until(lambda: controller.selection.get("effort") == "high", controller.changed)
    save_screenshot(window, "models")
    click(window, "closeModelButton")

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
    composer = find_item(window, "composer").parentItem()
    # The input lives inside a ScrollView; locate its outer rounded card.
    while composer.parentItem() and composer.property("radius") != 20:
        composer = composer.parentItem()
    assert composer.property("radius") == 20
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
    model = find_item(window, "settingsModel")
    url = find_item(window, "settingsBaseUrl")
    key = find_item(window, "settingsApiKey")
    original = (home / "settings.json").read_text()
    original_url = url.property("text")
    assert model.property("text") == "fixture"
    assert find_item(window, "settingsProviderName").property("text") == "desktop-test"
    save_screenshot(window, "provider-settings")
    model.setProperty("text", "fixture-reasoning")
    find_item(window, "settingsEffort").setProperty("text", "high")
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
    assert model.property("text") == "fixture-reasoning"
    assert (home / "settings.json").read_text() == original
    save_screenshot(window, "settings-validation")
    url.setProperty("text", original_url)
    click(window, "saveProviderSettings")
    until(
        lambda: bool(controller.providerSettingsState["notice"]), controller.providerSettingsChanged
    )
    until(lambda: controller.selection.get("model") == "fixture-reasoning", controller.changed)
    assert controller.selection["effort"] == "high"
    assert not key.property("text")
    stored = json.loads((home / "settings.json").read_text())
    assert stored["model"] == "fixture-reasoning"
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
    assert model.property("text") == "fixture-reasoning" and not key.property("text")

    # Built-in auth modes are visible without making an external provider request.
    click(window, "builtinProviderType")
    picker = find_item(window, "settingsProviderPicker")
    builtins = window.findChild(QObject, "settingsDialog").property("builtIns")
    if isinstance(builtins, QJSValue):
        builtins = builtins.toVariant()
    click(window, "settingsProviderPicker")
    QTest.keyClick(window, Qt.Key.Key_Home)
    for _ in range(next(i for i, item in enumerate(builtins) if item["id"] == "codex")):
        QTest.keyClick(window, Qt.Key.Key_Down)
    QTest.keyClick(window, Qt.Key.Key_Return)
    assert picker.property("currentValue") == "codex"
    assert not key.isVisible()
    save_screenshot(window, "settings-codex-login")
    click(window, "customProviderType")
    find_item(window, "settingsProviderName").setProperty("text", "desktop-second")
    model.setProperty("text", "fixture")
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

    reveal("applySettingsToChat")
    click(window, "applySettingsToChat")
    assert not find_item(window, "applySettingsToChat").property("checked")
    click(window, "saveProviderSettings")
    until(
        lambda: bool(controller.providerSettingsState["notice"]), controller.providerSettingsChanged
    )
    assert controller.selection["provider"] == "desktop-test"
    assert json.loads((home / "settings.json").read_text())["provider"] == "desktop-second"
    click(window, "closeSettingsButton")
    previous = controller.chatId
    click(window, "newChatButton")
    until(lambda: controller.connected and controller.chatId != previous, controller.changed)
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
    assert not find_item(window, "applySettingsToChat").property("enabled")
    assert not find_item(window, "applySettingsToChat").property("checked")
    model.setProperty("text", "fixture-reasoning")
    click(window, "saveProviderSettings")
    until(
        lambda: bool(controller.providerSettingsState["notice"]), controller.providerSettingsChanged
    )
    assert controller.selection["model"] == "fixture" and controller.status == "running"
    assert json.loads((home / "settings.json").read_text())["model"] == "fixture-reasoning"
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
        lambda: "removed" in controller.providerSettingsState["notice"],
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
    assert board.needsReview.rowCount() == 1, "Opening a session must not mark its result reviewed"
    click(window, "reviewBoardChat_" + chat_id)
    until(lambda: board.reviewedSessions.rowCount() == 1 and board.needsReview.rowCount() == 0, board.changed)
    click(window, "openBoardChat_reviewed_" + chat_id)
    type_message(window, "A new result must need review again")
    QTest.keyClick(window, Qt.Key.Key_Return)
    until(lambda: len(model_server) == 2, controller.changed)
    model_server[1].release.set()
    until(lambda: controller.status == "idle", controller.changed)
    click(window, "sessionBoardButton")
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
