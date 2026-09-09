"""Read the default browser's selected profile. Secrets only cross a private process pipe."""

from __future__ import annotations

import configparser
import contextlib
import json
import os
import plistlib
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


@dataclass(frozen=True)
class BrowserSource:
    name: str
    kind: str
    profile: Path


def default_browser(home: Path) -> str:
    if sys.platform == "darwin":
        path = (
            home
            / "Library/Preferences/com.apple.LaunchServices/com.apple.launchservices.secure.plist"
        )
        if not path.exists():
            return ""
        handlers = plistlib.loads(path.read_bytes()).get("LSHandlers", [])
        return next(
            (
                h.get("LSHandlerRoleAll", "")
                for h in reversed(handlers)
                if h.get("LSHandlerURLScheme") == "https"
                or h.get("LSHandlerContentType") == "com.apple.default-app.web-browser"
            ),
            "",
        )
    if sys.platform.startswith("linux"):
        return subprocess.run(
            ["xdg-settings", "get", "default-web-browser"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        ).stdout.strip()
    return ""


def detect_source(home: Path | None = None) -> BrowserSource:
    home = home or Path.home()
    identity = default_browser(home).lower()
    mac = sys.platform == "darwin"
    if "firefox" in identity:
        root = home / ("Library/Application Support/Firefox" if mac else ".mozilla/firefox")
        config = configparser.ConfigParser()
        config.read(root / "profiles.ini")
        chosen = next(
            (s for s in config.sections() if s.startswith("Install") and config[s].get("Default")),
            None,
        )
        if chosen:
            profile = root / config[chosen]["Default"]
        else:
            chosen = next((s for s in config.sections() if config[s].get("Default") == "1"), None)
            if chosen is None:
                raise ValueError("Firefox has no default profile to import.")
            profile = Path(config[chosen]["Path"])
            if config[chosen].get("IsRelative", "1") == "1":
                profile = root / profile
        return BrowserSource("Firefox", "firefox", profile)
    choices = [
        ("brave", "Brave", "brave", "BraveSoftware/Brave-Browser", "BraveSoftware/Brave-Browser"),
        ("edgemac", "Microsoft Edge", "edge", "Microsoft Edge", "microsoft-edge"),
        ("microsoft-edge", "Microsoft Edge", "edge", "Microsoft Edge", "microsoft-edge"),
        ("chromium", "Chromium", "chromium", "Chromium", "chromium"),
        ("chrome", "Chrome", "chrome", "Google/Chrome", "google-chrome"),
    ]
    for match, name, kind, mac_path, linux_path in choices:
        if match not in identity:
            continue
        root = (
            home
            / ("Library/Application Support" if mac else ".config")
            / (mac_path if mac else linux_path)
        )
        if not root.is_dir():
            raise ValueError(f"No {name} profile was found for this user.")
        state_file = root / "Local State"
        state = json.loads(state_file.read_text()) if state_file.is_file() else {}
        last_used = state.get("profile", {}).get("last_used", "Default")
        profile = (root / last_used).resolve()
        if not profile.is_relative_to(root.resolve()) or not profile.is_dir():
            raise ValueError(f"The selected {name} profile is unavailable.")
        return BrowserSource(name, kind, profile)
    raise ValueError(
        "Automatic import currently supports Chrome, Edge, Brave, Chromium and Firefox on macOS and Linux. This default browser is not supported yet."
    )


def query(path: Path, sql: str) -> list[dict[str, Any]]:
    # mode=ro also sees committed WAL data; never open the source with write access.
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=3) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(sql)]


def web_url(value: str) -> bool:
    parsed = urlsplit(value)
    return parsed.scheme in ("http", "https") and bool(parsed.hostname)


def read_library(source: BrowserSource) -> tuple[list[dict], list[dict]]:
    bookmarks: list[dict] = []
    history: list[dict] = []
    if source.kind == "firefox":
        places = source.profile / "places.sqlite"
        if places.exists():
            bookmarks = query(
                places,
                "SELECT COALESCE(b.title,p.title,p.url) AS title,p.url FROM moz_bookmarks b JOIN moz_places p ON p.id=b.fk WHERE b.type=1",
            )
            history = query(
                places,
                "SELECT COALESCE(title,url) AS title,url,last_visit_date AS visited FROM moz_places WHERE last_visit_date IS NOT NULL ORDER BY last_visit_date DESC",
            )
    else:
        bookmark_file = source.profile / "Bookmarks"
        if bookmark_file.exists():
            pending = list(json.loads(bookmark_file.read_text()).get("roots", {}).values())
            while pending:
                node = pending.pop()
                if node.get("type") == "url":
                    bookmarks.append({"title": node.get("name") or node["url"], "url": node["url"]})
                pending.extend(reversed(node.get("children", [])))
        history_file = source.profile / "History"
        if history_file.exists():
            history = query(
                history_file,
                "SELECT COALESCE(NULLIF(title,''),url) AS title,url,last_visit_time AS visited FROM urls ORDER BY last_visit_time DESC",
            )
    return (
        [row for row in bookmarks if web_url(row["url"])],
        [row for row in history if web_url(row["url"])],
    )


def read_cookies(source: BrowserSource) -> tuple[list[dict], list[str]]:
    candidates = ("cookies.sqlite",) if source.kind == "firefox" else ("Network/Cookies", "Cookies")
    path = next(
        (source.profile / name for name in candidates if (source.profile / name).is_file()), None
    )
    if path is None:
        return [], []
    cookies = []
    skipped = 0
    unsupported = 0
    if source.kind == "firefox":
        # Read rows directly: cookie jars collapse container identities, and Firefox's
        # session restore format has different isolation metadata from this database.
        rows = query(path, "SELECT * FROM moz_cookies")
        for row in rows:
            if row.get("originAttributes"):
                skipped += 1
                continue
            if row["expiry"] and row["expiry"] < time.time():
                continue
            cookies.append(
                {
                    "name": row["name"],
                    "value": row["value"],
                    "domain": row["host"],
                    "host_only": not row["host"].startswith("."),
                    "path": row["path"],
                    "secure": bool(row["isSecure"]),
                    "http_only": bool(row["isHttpOnly"]),
                    "expires": row["expiry"] or None,
                    "same_site": row.get("sameSite", -1),
                }
            )
    else:
        import browser_cookie3  # type: ignore[import-untyped]

        # Qt's public cookie API has no partition key. Never flatten isolated cookies.
        columns = {row["name"] for row in query(path, "PRAGMA table_info(cookies)")}
        partition = "top_frame_site_key" if "top_frame_site_key" in columns else "''"
        same_site = "samesite" if "samesite" in columns else "-1"
        metadata = query(
            path,
            f"SELECT host_key AS domain,name,path,{same_site} AS same_site,{partition} AS partition_key,"
            "CASE WHEN length(value)=0 AND length(encrypted_value)>0 AND substr(encrypted_value,1,3) NOT IN (x'763130',x'763131') THEN 1 ELSE 0 END AS unsupported FROM cookies",
        )
        isolated = {(m["domain"], m["name"], m["path"]) for m in metadata if m["partition_key"]}
        unavailable = {(m["domain"], m["name"], m["path"]) for m in metadata if m["unsupported"]}
        policies = {(m["domain"], m["name"], m["path"]): m["same_site"] for m in metadata}
        # Suppress third-party diagnostics; only our structured result crosses the pipe.
        with (
            open(os.devnull, "w") as sink,
            contextlib.redirect_stdout(sink),
            contextlib.redirect_stderr(sink),
        ):
            jar = getattr(browser_cookie3, source.kind)(cookie_file=str(path))
        for cookie in jar:
            identity = (cookie.domain, cookie.name, cookie.path)
            if identity in isolated or identity in unavailable or cookie.is_expired(time.time()):
                continue
            cookies.append(
                {
                    "name": cookie.name,
                    "value": cookie.value,
                    "domain": cookie.domain,
                    "host_only": not cookie.domain_initial_dot,
                    "path": cookie.path,
                    "secure": cookie.secure,
                    "http_only": cookie.has_nonstandard_attr("HTTPOnly")
                    or cookie.has_nonstandard_attr("HttpOnly"),
                    "expires": cookie.expires,
                    "same_site": policies.get(identity, -1),
                }
            )
        skipped, unsupported = len(isolated), len(unavailable)
    warnings = []
    if skipped:
        warnings.append(
            f"Skipped {skipped} partitioned/container cookies that cannot be represented safely in this browser."
        )
    if unsupported:
        warnings.append(
            f"Skipped {unsupported} cookies with unsupported encryption. Those sites require signing in again."
        )
    return cookies, warnings


def collect(source: BrowserSource) -> dict[str, Any]:
    result: dict[str, Any] = {
        "source": f"{source.name} · {source.profile.name}",
        "cookies": [],
        "bookmarks": [],
        "history": [],
        "warnings": [],
    }
    try:
        result["bookmarks"], result["history"] = read_library(source)
    except (OSError, ValueError, sqlite3.Error):
        result["warnings"].append(
            "Bookmarks or history could not be read. Close the source browser and retry."
        )
    try:
        result["cookies"], warnings = read_cookies(source)
        result["warnings"].extend(warnings)
    except Exception:
        # Third-party decryption errors can include cookie values. Never send their text
        # to stderr, the GUI, a transcript, or a log.
        result["warnings"].append(
            "Cookies could not be unlocked. Allow the browser's Safe Storage key in the system keychain, then retry. Some sessions may require signing in again."
        )
    return result


def main() -> None:
    try:
        result = collect(detect_source())
    except (OSError, ValueError, subprocess.SubprocessError, configparser.Error):
        result = {
            "source": "",
            "cookies": [],
            "bookmarks": [],
            "history": [],
            "warnings": [
                "No supported default browser profile was found. Automatic import supports Chrome, Edge, Brave, Chromium and Firefox on macOS and Linux."
            ],
        }
    # Only the owning QProcess reads this pipe. Never invoke this module as a diagnostic.
    sys.stdout.write(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
