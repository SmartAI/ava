"""Qt UI and persistent backend acceptance scenarios, using a gated local model server."""

from __future__ import annotations

import gc
import json
import os
import re
import shlex
import sys
import threading
import time
import weakref
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import (  # noqa: E402
    Q_ARG,
    QCoreApplication,
    QEvent,
    QEventLoop,
    QMetaObject,
    QPersistentModelIndex,
    QPointF,
    QProcess,
    QRectF,
    QSettings,
    Qt,
    QTimer,
)
from PySide6.QtGui import (  # noqa: E402
    QGuiApplication,
    QImage,
    QInputMethodEvent,
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








def test_desktop_controller_is_released_after_shutdown(qt_app, home, project):
    controller = Controller(project, [], QSettings(str(home / "lifetime.ini"), QSettings.Format.IniFormat))
    reference = weakref.ref(controller)
    controller.shutdown()
    del controller
    gc.collect()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert reference() is None, "Connection callbacks must not retain a closed controller"












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




def save_screenshot(window, suffix):
    output = os.environ.get("AVA_DESKTOP_SCREENSHOT")
    if output:
        from pathlib import Path

        target = Path(output)
        if QGuiApplication.focusWindow() != window:
            window.raise_()
            window.requestActivate()
        presented = QSignalSpy(window.frameSwapped)
        window.update()
        assert presented.wait(2000), (
            f"screenshot frame was not presented: exposed={window.isExposed()}, "
            f"active={window.isActive()}, app={QGuiApplication.applicationState()}"
        )
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
