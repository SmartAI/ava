"""Bounded skill discovery and durable, machine-local availability preferences."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

from ava.base import ava_home, find_project_root

NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
HEADER_LIMIT = 16 * 1024
PREVIEW_LIMIT = 64 * 1024


@dataclass(frozen=True, slots=True)
class Skill:
    name: str
    description: str
    scope: str
    path: Path


def revision() -> str:
    try:
        stat = (ava_home() / "capabilities.sqlite3").stat()
        return f"{stat.st_ino}:{stat.st_mtime_ns}:{stat.st_size}"
    except FileNotFoundError:
        return "0"


@lru_cache(maxsize=16)
def _preferences(home: Path, stamp: str) -> dict[str, str]:
    if stamp == "0":
        return {}
    with closing(sqlite3.connect(f"{(home / 'capabilities.sqlite3').as_uri()}?mode=ro", uri=True)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='skills'").fetchone():
            return {}
        return dict(db.execute("SELECT path, state FROM skills"))


def preferences() -> dict[str, str]:
    return _preferences(ava_home(), revision())


@lru_cache(maxsize=2048)
def _metadata(path: Path, stamp: tuple[int, int, int]) -> tuple[str, str]:
    try:
        with path.open("rb") as source:
            prefix = source.read(HEADER_LIMIT + 1)
        lines = prefix.decode("utf-8", errors="replace").splitlines()
        if not lines or lines[0] != "---":
            raise ValueError("SKILL.md must start with YAML frontmatter.")
        end = next((i for i in range(1, len(lines)) if lines[i] == "---"), None)
        if end is None:
            raise ValueError("Missing frontmatter closing delimiter (16 KiB limit).")
        header = yaml.safe_load("\n".join(lines[1:end]))
        if not isinstance(header, dict):
            raise ValueError("Frontmatter must be a mapping.")
        # Older Ava skills omitted name; retain their directory-name convention.
        if header.get("name", path.parent.name) != path.parent.name:
            raise ValueError("The skill name must match its directory.")
        description = header.get("description")
        if not isinstance(description, str) or not 1 <= len(description.strip()) <= 1024:
            raise ValueError("Description must contain 1–1,024 characters.")
        if not NAME.fullmatch(path.parent.name) or len(path.parent.name) > 64:
            raise ValueError("Use a name of up to 64 lowercase letters, numbers and single hyphens.")
        return description.strip(), ""
    except (OSError, UnicodeError, ValueError, yaml.YAMLError) as error:
        return "", str(error)


def inventory(cwd: Path) -> list[dict]:
    roots = [(find_project_root(cwd) / ".agents/skills", "project", "Project"),
             (ava_home() / "skills", "global", "Ava personal")]
    if home := os.environ.get("HOME"):
        roots.append((Path(home) / ".codex/skills", "global", "Shared"))
    states = preferences()
    rows: list[dict] = []
    winners: dict[str, str] = {}
    for root, scope, source in roots:
        try:
            entries = sorted(root.iterdir())
        except FileNotFoundError:
            continue
        for entry in entries:
            path = entry / "SKILL.md"
            try:
                stat = path.stat()
                if not path.is_file():
                    continue
            except FileNotFoundError:
                continue
            description, error = _metadata(path, (stat.st_ino, stat.st_mtime_ns, stat.st_size))
            state = states.get(str(path), "enabled")
            shadowed = winners.get(entry.name, "")
            if not error and state != "removed":
                # Disabling a project skill must not silently enable a same-named shared skill.
                winners.setdefault(entry.name, str(path))
            rows.append({"id": hashlib.sha256(str(path).encode()).hexdigest()[:24],
                         "name": entry.name, "description": description, "scope": scope,
                         "source": source, "path": str(path), "state": state, "error": error,
                         "location": (".agents/skills" if scope == "project" else "$AVA_HOME/skills" if source == "Ava personal" else "~/.codex/skills") + f"/{entry.name}/SKILL.md",
                         "shadowed_by": shadowed,
                         "effective": state == "enabled" and not error and not shadowed})
    return sorted(rows, key=lambda row: (row["name"], row["source"]))


def discover_skills(cwd: Path) -> list[Skill]:
    return [Skill(row["name"], row["description"], row["scope"], Path(row["path"]))
            for row in inventory(cwd) if row["effective"]]


def detail(cwd: Path, identity: str) -> dict:
    row = next((row for row in inventory(cwd) if row["id"] == identity), None)
    if row is None:
        raise ValueError("This skill is no longer available. Refresh the list.")
    with Path(row["path"]).open("rb") as source:
        content = source.read(PREVIEW_LIMIT + 1)
    truncated = len(content) > PREVIEW_LIMIT
    text = content[:PREVIEW_LIMIT].decode("utf-8", errors="replace")
    lines = text.splitlines(keepends=True)
    if lines and lines[0].strip() == "---":
        end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
        if end is not None:
            text = "".join(lines[end + 1:])
    return {**row, "body": text, "truncated": truncated}


def set_state(cwd: Path, identity: str, state: str) -> None:
    if state not in {"enabled", "disabled", "removed"}:
        raise ValueError("Choose enabled, disabled or removed.")
    row = next((row for row in inventory(cwd) if row["id"] == identity), None)
    if row is None:
        raise ValueError("This skill is no longer available. Refresh the list.")
    home = ava_home()
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    with closing(sqlite3.connect(home / "capabilities.sqlite3", timeout=5)) as db, db:
        db.execute("CREATE TABLE IF NOT EXISTS skills (path TEXT PRIMARY KEY, state TEXT NOT NULL)")
        db.execute("INSERT INTO skills VALUES (?, ?) ON CONFLICT(path) DO UPDATE SET state=excluded.state",
                   (row["path"], state))


def create(cwd: Path, name: str, description: str, body: str, scope: str) -> dict:
    if not NAME.fullmatch(name) or len(name) > 64:
        raise ValueError("Use a name of up to 64 lowercase letters, numbers and single hyphens.")
    if not 1 <= len(description.strip()) <= 1024:
        raise ValueError("Description must contain 1–1,024 characters.")
    if not body.strip() or len(body.encode()) > PREVIEW_LIMIT:
        raise ValueError("Instructions are required and must fit within 64 KiB.")
    if scope not in {"project", "global"}:
        raise ValueError("Choose Project or Personal.")
    root = find_project_root(cwd) / ".agents/skills" if scope == "project" else ava_home() / "skills"
    path = root / name / "SKILL.md"
    text = f"---\nname: {name}\ndescription: {json.dumps(description.strip(), ensure_ascii=False)}\n---\n\n{body.rstrip()}\n"
    # Exclusive directory creation prevents replacing any existing skill or its resources.
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        path.parent.mkdir(mode=0o700)
    except FileExistsError:
        # Retrying an uncertain successful response is safe for this exact draft.
        if path.is_file() and path.stat().st_size == len(text.encode()) and path.read_text() == text:
            return next(row for row in inventory(cwd) if row["path"] == str(path))
        raise ValueError("A skill with this name already exists in this location.") from None
    temporary = path.with_name(".SKILL.md.tmp")
    with temporary.open("x", encoding="utf-8") as output:
        output.write(text)
        output.flush()
        os.fsync(output.fileno())
    # Readers only discover the complete file, even during concurrent agent requests.
    os.replace(temporary, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return next(row for row in inventory(cwd) if row["path"] == str(path))
