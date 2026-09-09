"""Migration checks against synthetic profiles; never inspect the user's browser."""

import hashlib
import json
import plistlib
import sqlite3
from pathlib import Path

import pytest

from ava.app.desktop import browser_import


def test_chrome_selected_profile_decryption_and_cookie_boundaries(home, monkeypatch):
    cookie3 = pytest.importorskip("browser_cookie3")
    from Cryptodome.Cipher import AES
    from Cryptodome.Protocol.KDF import PBKDF2
    from Cryptodome.Util.Padding import pad

    monkeypatch.setattr(browser_import.sys, "platform", "darwin")
    monkeypatch.setattr(cookie3, "_get_osx_keychain_password", lambda *_: b"fixture-key")
    root = Path.home() / "Library/Application Support/Google/Chrome"
    profile = root / "Profile 2"
    profile.mkdir(parents=True)
    (root / "Local State").write_text(json.dumps({"profile": {"last_used": "Profile 2"}}))
    preferences = Path.home() / "Library/Preferences/com.apple.LaunchServices"
    preferences.mkdir(parents=True)
    (preferences / "com.apple.launchservices.secure.plist").write_bytes(
        plistlib.dumps(
            {
                "LSHandlers": [
                    {"LSHandlerURLScheme": "https", "LSHandlerRoleAll": "com.google.chrome"}
                ]
            }
        )
    )
    source = browser_import.detect_source()
    assert source.profile == profile and source.kind == "chrome"
    (profile / "Bookmarks").write_text(
        json.dumps(
            {
                "roots": {
                    "bookmark_bar": {
                        "children": [
                            {
                                "type": "url",
                                "name": "中文书签",
                                "url": "https://example.test/account",
                            },
                            {"type": "url", "name": "Local", "url": "file:///private/test"},
                        ]
                    }
                }
            }
        )
    )
    with sqlite3.connect(profile / "History") as db:
        db.execute("CREATE TABLE urls (url TEXT, title TEXT, last_visit_time INTEGER)")
        db.execute("INSERT INTO urls VALUES ('https://example.test/account', 'Account', 123)")
    key = PBKDF2(b"fixture-key", b"saltysalt", 16, count=1003)
    encrypted = b"v10" + AES.new(key, AES.MODE_CBC, iv=b" " * 16).encrypt(
        pad(hashlib.sha256(b"example.test").digest() + b"fixture-session", 16)
    )
    path = profile / "Cookies"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE meta (key TEXT, value TEXT)")
        db.execute("INSERT INTO meta VALUES ('version', '24')")
        db.execute(
            "CREATE TABLE cookies (host_key TEXT, name TEXT, path TEXT, is_secure INTEGER, expires_utc INTEGER, value TEXT, encrypted_value BLOB, is_httponly INTEGER, samesite INTEGER, top_frame_site_key TEXT)"
        )
        for name, partition, cipher, expiry in [
            ("session", "", encrypted, 0),
            ("isolated", "https://other.test", encrypted, 0),
            ("unknown", "", b"v20unsupported", 0),
            ("expired", "", encrypted, 1),
        ]:
            db.execute(
                "INSERT INTO cookies VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    "example.test",
                    name,
                    "/account",
                    1,
                    expiry,
                    "",
                    cipher,
                    1,
                    2,
                    partition,
                ),
            )
    before = path.read_bytes()
    result = browser_import.collect(source)
    assert len(result["cookies"]) == 1
    assert result["cookies"][0] == {
        "name": "session",
        "value": "fixture-session",
        "domain": "example.test",
        "host_only": True,
        "path": "/account",
        "secure": True,
        "http_only": True,
        "expires": None,
        "same_site": 2,
    }
    assert result["bookmarks"] == [{"title": "中文书签", "url": "https://example.test/account"}]
    assert len(result["history"]) == 1 and len(result["warnings"]) == 2
    assert path.read_bytes() == before

    def denied(**_):
        raise ValueError("fixture-secret-must-not-reach-the-report")

    monkeypatch.setattr(cookie3, "chrome", denied)
    result = browser_import.collect(source)
    assert not result["cookies"] and len(result["bookmarks"]) == 1
    assert "fixture-secret" not in json.dumps(result)


def test_embedded_cookie_survives_application_restart(home):
    pytest.importorskip("PySide6.QtWebEngineQuick")
    import os
    import subprocess
    import sys
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            authenticated = "fixture_session=persisted-session" in self.headers.get("Cookie", "")
            self.wfile.write(
                b"<title>Account ready</title>" if authenticated else b"<title>Sign in</title>"
            )

    directory = home / "browser"
    directory.mkdir()
    (directory / "library.json").write_text('{"attempted":true,"bookmarks":[],"history":[]}')
    script = """
import sys
from PySide6.QtCore import SIGNAL, QObject, QTimer, QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtNetwork import QNetworkCookie
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuick import QQuickItem
from PySide6.QtWebEngineQuick import QtWebEngineQuick
from shiboken6 import delete
from ava.app.desktop.browser import BrowserSession

QtWebEngineQuick.initialize()
app = QGuiApplication([])
session = BrowserSession(app)
profile = session.profile
engine = QQmlApplicationEngine()
engine.setInitialProperties({"session": session})
engine.loadData(b"import QtQuick; import QtQuick.Controls; import QtWebEngine; ApplicationWindow { id: window; required property var session; visible: true; width: 500; height: 300; WebEngineView { objectName: 'browser'; anchors.fill: parent; profile: window.session.profile; url: 'about:blank' } }")
assert engine.rootObjects()
window = engine.rootObjects()[0]
browser = window.findChild(QQuickItem, "browser")
ready = []
def check():
    if browser.property("title") == "Account ready":
        ready.append(True)
        app.quit()
QObject.connect(browser, SIGNAL("titleChanged()"), check)
def navigate(*_):
    browser.setProperty("url", QUrl(sys.argv[2]))
if sys.argv[1] == "write":
    profile.cookieStore().cookieAdded.connect(navigate)
    cookie = QNetworkCookie(b"fixture_session", b"persisted-session")
    cookie.setPath("/")
    cookie.setHttpOnly(True)
    profile.cookieStore().setCookie(cookie, QUrl(sys.argv[2]))
else:
    QTimer.singleShot(0, navigate)
QTimer.singleShot(10000, app.quit)
app.exec()
session.shutdown()
delete(engine)
delete(session)
assert ready, "Browser session did not survive restart"
"""
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", QT_QUICK_BACKEND="software")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for mode in ("write", "read"):
            result = subprocess.run(
                [sys.executable, "-c", script, mode, f"http://127.0.0.1:{server.server_port}/"],
                env=env,
                capture_output=True,
                text=True,
                timeout=20,
            )
            assert result.returncode == 0, mode + ": " + result.stderr
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
