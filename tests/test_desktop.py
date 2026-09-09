"""Qt UI and owned backend acceptance scenarios, using a gated local model server."""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import (  # noqa: E402
    QCoreApplication,
    QEvent,
    QEventLoop,
    QPointF,
    QProcess,
    QSettings,
    Qt,
    QTimer,
)
from PySide6.QtGui import QGuiApplication, QInputMethodEvent  # noqa: E402
from PySide6.QtQuick import QQuickItem  # noqa: E402
from PySide6.QtQuickControls2 import QQuickStyle  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

from ava.app.desktop.application import create_engine  # noqa: E402
from ava.app.desktop.controller import Controller  # noqa: E402
from ava.app.desktop.transcript import Transcript  # noqa: E402


def until(predicate, *signals, timeout=10_000):
    """Advance the GUI loop on real signals; the timer only bounds a failing test."""
    if predicate():
        return
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
    assert predicate(), "condition did not become true before the deadline"


@pytest.fixture(scope="module")
def qt_app():
    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        os.environ.setdefault("QT_QUICK_BACKEND", "software")
    app = QGuiApplication.instance() or QGuiApplication([])
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

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

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
                chunk({"content": "Hello "})
                if exchange.release.wait(15):
                    chunk({"content": "世界"})
                    chunk({}, "stop")
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
                "models": {"fixture": {"context_window": 10000}},
            }
        },
    }
    (home / "settings.json").write_text(json.dumps(settings))
    try:
        yield exchanges
    finally:
        for exchange in exchanges:
            exchange.release.set()
        server.shutdown()
        server.server_close()
        thread.join(2)


@pytest.fixture
def desktop(qt_app, home, project):
    controller = Controller(
        project, [], QSettings(str(home / "desktop.ini"), QSettings.Format.IniFormat)
    )
    engine = create_engine(controller)
    assert engine.rootObjects()
    window = engine.rootObjects()[0]
    yield controller, window
    controller.shutdown()
    until(
        lambda: controller.runtime.process.state() == QProcess.ProcessState.NotRunning,
        controller.runtime.stopped,
        timeout=15_000,
    )
    window.hide()
    engine.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def click(window, name):
    item = window.findChild(QQuickItem, name)
    assert item is not None, name
    assert item.property("enabled"), name
    QCoreApplication.processEvents()
    position = item.mapToScene(QPointF(item.width() / 2, item.height() / 2)).toPoint()
    QTest.mouseClick(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, position)


def type_message(window, text):
    composer = window.findChild(QQuickItem, "composer")
    composer.forceActiveFocus()
    event = QInputMethodEvent()
    event.setCommitString(text)
    QCoreApplication.sendEvent(composer, event)
    assert composer.property("text") == text


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
    assert window.findChild(QQuickItem, "composer").property("text") == ""

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
    assert window.findChild(QQuickItem, "composer").property("text") == ""
    controller.openChat(identity)
    until(
        lambda: controller.chatId == identity and len(controller._transcript.rows) == 2,
        controller.changed,
    )
    assert window.findChild(QQuickItem, "composer").property("text") == "unsent draft"
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
    assert any(c["id"] == second for c in controller.chats)
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
    composer = window.findChild(QQuickItem, "composer")
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


def test_transcript_golden_replay(qt_app):
    model = Transcript()
    blocks = [{"kind": "text", "text": "请检查代码"}]
    events = [
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
        {"kind": "turn/end", "reason": "completed"},
    ]
    for seq, event in enumerate(events):
        event["seq"] = seq
        model.apply(event)
    expected = [
        ("user", "You", "请检查代码"),
        ("assistant", "Ava", "Checking."),
        ("tool", "read", "print(1)"),
        ("assistant", "Ava", "代码正确。"),
    ]
    assert [(r["kind"], r["heading"], r["body"]) for r in model.rows] == expected
    assert model.pending_text() == ""
    for event in events:
        model.apply(event)
    assert len(model.rows) == 4
    model.clear()
    for event in events:
        model.apply(event)
    assert [(r["kind"], r["heading"], r["body"]) for r in model.rows] == expected


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
    controller.runtime.process.kill()
    until(lambda: not controller.online, controller.changed)
    assert "unexpectedly" in controller.error
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
    assert window.findChild(QQuickItem, "composer").property("text") == "after crash"

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
    assert window.findChild(QQuickItem, "composer").property("text") == "after crash"
