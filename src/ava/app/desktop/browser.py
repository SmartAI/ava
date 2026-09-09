"""Persistent embedded-browser profile and a cancellable, one-time local import."""

from __future__ import annotations

import json
import os
import sys
from typing import Any

from PySide6.QtCore import Property, QDateTime, QObject, QProcess, QTimer, QUrl, Signal, Slot
from PySide6.QtNetwork import QNetworkCookie
from PySide6.QtWebEngineCore import QWebEngineScript
from PySide6.QtWebEngineQuick import QQuickWebEngineProfile

from ava.base import ava_home


class BrowserSession(QObject):
    changed = Signal()

    def __init__(self, parent: QObject) -> None:
        super().__init__(parent)
        self._profile: QQuickWebEngineProfile | None = None
        self._directory = ava_home() / "browser"
        self._library: dict[str, Any] = {"bookmarks": [], "history": []}
        self._status = ""
        self._importing = False
        self._pending: set[tuple[bytes, str, str]] = set()
        self._accepted = 0
        self._stdout = bytearray()
        self._closing = False
        self._process = QProcess(self)
        self._process.setProgram(sys.executable)
        self._process.setArguments(["-m", "ava.app.desktop.browser_import"])
        self._process.readyReadStandardOutput.connect(self._read)
        self._process.readyReadStandardError.connect(lambda: self._process.readAllStandardError())
        self._process.finished.connect(self._imported)
        self._process.errorOccurred.connect(self._process_error)
        self._deadline = QTimer(self)
        self._deadline.setSingleShot(True)
        self._deadline.timeout.connect(self._timeout)
        self._cookie_deadline = QTimer(self)
        self._cookie_deadline.setSingleShot(True)
        self._cookie_deadline.timeout.connect(self._finish)

    @Property(QObject, constant=True)
    def profile(self) -> QObject:
        if self._profile is None:
            self._directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._directory.chmod(0o700)
            try:
                self._library = json.loads((self._directory / "library.json").read_text())
                self._status = self._library.get("status", "")
            except (OSError, ValueError):
                pass
            self._profile = QQuickWebEngineProfile("Ava", self)
            self._profile.setPersistentStoragePath(str(self._directory / "profile"))
            self._profile.setCachePath(str(self._directory / "cache"))
            self._profile.setPersistentCookiesPolicy(
                QQuickWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies
            )
            self._profile.cookieStore().cookieAdded.connect(self._cookie_added)
            if not self._library.get("attempted"):
                QTimer.singleShot(0, self.importDefaultBrowser)
        return self._profile

    @Property(QWebEngineScript, constant=True)
    def mediaScript(self) -> QWebEngineScript:
        script = QWebEngineScript()
        script.setName("Ava media compatibility")
        script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
        script.setWorldId(QWebEngineScript.ScriptWorldId.ApplicationWorld)
        script.setRunsOnSubFrames(True)
        script.setSourceCode(
            "document.addEventListener('error', function(e) { if (e.target instanceof HTMLMediaElement && e.target.error) console.info('__AVA_MEDIA__:' + e.target.error.code); }, true);"
        )
        return script

    @Property(str, notify=changed)
    def importStatus(self) -> str:
        return self._status

    @Property(bool, notify=changed)
    def importing(self) -> bool:
        return self._importing

    @Slot(str, str, result=list)
    def search(self, mode: str, query: str) -> list:
        if mode not in ("bookmarks", "history"):
            return []
        query = query.casefold()
        return [
            entry
            for entry in self._library.get(mode, [])
            if query in (entry["title"] + " " + entry["url"]).casefold()
        ][:100]

    @Slot()
    def importDefaultBrowser(self) -> None:
        if self._importing or self._closing or self._profile is None:
            return
        self._stdout.clear()
        self._pending.clear()
        self._accepted = 0
        self._importing = True
        self._status = "Importing your default browser… macOS may request Keychain access."
        self.changed.emit()
        self._process.start()
        self._deadline.start(120_000)

    def _read(self) -> None:
        self._stdout.extend(self._process.readAllStandardOutput().data())
        if len(self._stdout) > 64 * 1024 * 1024:
            self._process.kill()
            self._stdout.clear()

    def _process_error(self, error: QProcess.ProcessError) -> None:
        if error == QProcess.ProcessError.FailedToStart:
            self._deadline.stop()
            self._apply(
                {"warnings": ["The browser importer could not start. Try importing again."]}
            )

    def _timeout(self) -> None:
        self._process.kill()

    def _imported(self, code: int, _: QProcess.ExitStatus) -> None:
        self._deadline.stop()
        self._read()
        if self._closing:
            self._stdout.clear()
            return
        try:
            if code:
                raise ValueError("import process failed")
            result = json.loads(self._stdout)
        except (ValueError, UnicodeError):
            self._apply(
                {
                    "warnings": [
                        "Browser import did not finish. Check system permissions, then retry."
                    ]
                }
            )
            return
        finally:
            self._stdout.clear()
        self._apply(result)

    def _apply(self, result: dict) -> None:
        self._library["attempted"] = True
        self._library["source"] = result.get("source", "")
        self._library["warnings"] = result.get("warnings", [])
        for kind in ("bookmarks", "history"):
            # Explicit re-import refreshes source entries without losing the existing library.
            entries = {row["url"]: row for row in self._library.get(kind, [])}
            entries.update({row["url"]: row for row in result.get(kind, [])})
            self._library[kind] = list(entries.values())
        if self._profile is None:
            self._finish()
            return
        cookies = result.pop("cookies", [])
        self._pending = {
            (entry["name"].encode(), entry["domain"].lstrip("."), entry["path"] or "/")
            for entry in cookies
        }
        for entry in cookies:
            cookie = QNetworkCookie(entry["name"].encode(), entry["value"].encode())
            if not entry.get("host_only"):
                cookie.setDomain(entry["domain"])
            cookie.setPath(entry["path"] or "/")
            cookie.setSecure(entry["secure"])
            cookie.setHttpOnly(entry["http_only"])
            if entry.get("expires"):
                cookie.setExpirationDate(QDateTime.fromSecsSinceEpoch(int(entry["expires"])))
            cookie.setSameSitePolicy(
                {
                    -1: QNetworkCookie.SameSite.Default,
                    0: QNetworkCookie.SameSite.None_,
                    1: QNetworkCookie.SameSite.Lax,
                    2: QNetworkCookie.SameSite.Strict,
                }.get(entry.get("same_site", -1), QNetworkCookie.SameSite.Default)
            )
            origin = QUrl(
                ("https://" if entry["secure"] else "http://")
                + entry["domain"].lstrip(".")
                + (entry["path"] or "/")
            )
            self._profile.cookieStore().setCookie(cookie, origin)
        cookies.clear()
        if self._pending:
            self._status = f"Importing cookies from {self._library['source']}…"
            self._cookie_deadline.start(10_000)
            self.changed.emit()
        else:
            self._finish()

    def _cookie_added(self, cookie: QNetworkCookie) -> None:
        identity = (bytes(cookie.name().data()), cookie.domain().lstrip("."), cookie.path())
        if identity in self._pending:
            self._pending.remove(identity)
            self._accepted += 1
            if not self._pending:
                self._finish()

    def _finish(self) -> None:
        self._cookie_deadline.stop()
        warnings = self._library.get("warnings", [])
        if self._pending:
            warnings = [
                *warnings,
                f"{len(self._pending)} cookies were not accepted by the browser.",
            ]
        self._pending.clear()
        source = self._library.get("source")
        self._status = (
            f"Imported from {source}: {self._accepted} cookies, {len(self._library.get('bookmarks', []))} bookmarks, {len(self._library.get('history', []))} history entries."
            if source
            else ""
        )
        if warnings:
            self._status += " " + " ".join(warnings)
        self._library["status"] = self._status.strip()
        try:
            self._directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            target = self._directory / "library.json"
            temporary = target.with_suffix(".tmp")
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "w") as stream:
                json.dump(self._library, stream, ensure_ascii=False)
            temporary.replace(target)
        except OSError:
            self._status += " The import result could not be saved."
        self._importing = False
        self.changed.emit()

    def shutdown(self) -> None:
        self._closing = True
        self._deadline.stop()
        self._cookie_deadline.stop()
        if self._process.state() != QProcess.ProcessState.NotRunning:
            self._process.kill()
            self._process.waitForFinished(1000)
        self._stdout.clear()
