"""Native OAuth setup, browser handoff and loopback callback against real HTTP peers."""

from __future__ import annotations

import socket
import threading
from urllib.parse import urlencode

import httpx
import pytest

from tests.fixtures.oauth_server import oauth_server as oauth_server
from tests.test_desktop import (
    Qt,
    QTest,
    click,
    find_item,
    save_screenshot,
    until,
)
from tests.test_desktop import (
    desktop as desktop,
)
from tests.test_desktop import (
    model_server as model_server,
)
from tests.test_desktop import (
    qt_app as create_qt_app,
)


@pytest.fixture(scope="module")
def qt_app():
    from PySide6.QtGui import QGuiApplication

    # Reuse the main acceptance module's application/style when it already exists.
    app = QGuiApplication.instance()
    if app is not None:
        yield app
    else:
        yield from create_qt_app.__wrapped__()


def test_desktop_mcp_oauth_sign_in_and_cancel(desktop, model_server, oauth_server, monkeypatch):
    controller, window = desktop
    controller.start()
    until(lambda: bool(controller.projects), controller.changed)
    click(window, "mcpButton")
    click(window, "addMcpButton")
    find_item(window, "mcpNameField").setProperty("text", "Gmail OAuth fixture")
    find_item(window, "mcpTransportChoice").setProperty("currentIndex", 1)
    find_item(window, "mcpUrlField").setProperty("text", oauth_server.url)
    choice = find_item(window, "mcpAuthChoice")
    assert choice is not None, "HTTP MCP configuration must offer OAuth sign-in, not just static headers"
    choice.setProperty("currentIndex", 1)
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        redirect_uri = f"http://127.0.0.1:{reservation.getsockname()[1]}/oauth/callback"
    for name, value in (("mcpOAuthClientId", oauth_server.client_id),
                        ("mcpOAuthClientSecret", oauth_server.client_secret),
                        ("mcpOAuthIssuer", oauth_server.issuer),
                        ("mcpOAuthScope", oauth_server.scope),
                        ("mcpOAuthRedirect", redirect_uri)):
        find_item(window, name).setProperty("text", value)
    save_screenshot(window, "mcp-oauth-editor")
    click(window, "saveMcpButton")
    view = controller.mcpView
    until(lambda: bool(view.detail.get("id")), view.changed, timeout=20000)
    assert view.detail["auth_status"] == "required", view.detail
    assert oauth_server.authorizations == 0
    opened = []
    monkeypatch.setattr("ava.app.desktop.mcp.QDesktopServices.openUrl", lambda url: opened.append(url.toString()) or True)
    click(window, "signInMcpButton")
    until(lambda: bool(opened) and view.detail.get("auth_status") == "signing_in", view.changed)
    save_screenshot(window, "mcp-oauth-signing-in")
    # A browser on this desktop follows the callback even when the backend is elsewhere.
    results = []
    def browse():
        results.append(httpx.get(opened[-1], follow_redirects=True, timeout=10))
    browser = threading.Thread(target=browse)
    browser.start()
    until(lambda: view.detail.get("auth_status") == "authorized", view.changed, timeout=20000)
    browser.join(2)
    assert results and results[0].status_code == 200
    assert oauth_server.last_authorization["scope"] == oauth_server.scope
    save_screenshot(window, "mcp-oauth-authorized")
    click(window, "editMcpButton")
    assert find_item(window, "mcpOAuthClientSecret").property("text") == ""
    QTest.keyClick(window, Qt.Key.Key_Escape)
    click(window, "signOutMcpButton")
    until(lambda: view.detail.get("auth_status") == "required", view.changed)
    click(window, "signInMcpButton")
    until(lambda: len(opened) == 2, view.changed)
    click(window, "cancelMcpSignInButton")
    until(lambda: view.detail.get("auth_status") == "required", view.changed)
    assert not view.authenticating
    window.setProperty("dark", True)
    window.setWidth(800)
    window.setHeight(600)
    click(window, "editMcpButton")
    save_screenshot(window, "mcp-oauth-dark-narrow")
    QTest.keyClick(window, Qt.Key.Key_Escape)


def test_oauth_loopback_rejects_forged_callbacks_and_occupied_ports(qt_app):
    from PySide6.QtCore import QObject, QUrl
    from PySide6.QtNetwork import QNetworkAccessManager, QNetworkRequest

    from ava.app.desktop.oauth import OAuthCallbackListener

    parent = QObject()
    listener = OAuthCallbackListener(parent)
    network = QNetworkAccessManager(parent)
    received = []
    listener.received.connect(received.append)
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        uri = f"http://127.0.0.1:{reservation.getsockname()[1]}/oauth/callback"
    try:
        assert listener.start(uri) == ""
        listener.state = "one-time-state"
        occupied = OAuthCallbackListener(parent)
        assert "in use" in occupied.start(uri)
        for query in ({"code": "fake", "state": "wrong"}, {"code": "fake", "state": "λ"},
                      {"code": "fake", "state": ["one-time-state", "duplicate"]}):
            reply = network.get(QNetworkRequest(QUrl(uri + "?" + urlencode(query, doseq=True))))
            until(reply.isFinished, reply.finished)
            assert reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute) == 400
            assert not received
            reply.deleteLater()
        reply = network.get(QNetworkRequest(QUrl(uri + "?" + urlencode({"code": "real-code", "state": "one-time-state", "iss": "https://issuer.example"}))))
        until(reply.isFinished, reply.finished)
        assert reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute) == 200
        assert received == [{"code": "real-code", "state": "one-time-state", "iss": "https://issuer.example"}]
        assert not listener.server.isListening() and not listener.timer.isActive()
        assert reply.rawHeader("Cache-Control").data() == b"no-store"
        reply.deleteLater()
    finally:
        listener.close()
