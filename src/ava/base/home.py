"""Ava home lookup, ASCII folding, and project-root discovery."""

from __future__ import annotations

import os
from pathlib import Path

from ava.base.errors import AvaError, ErrorKind


def ava_home() -> Path:
    override = os.environ.get("AVA_HOME")
    if override:
        path = Path(override)
        if not path.is_absolute():
            raise AvaError(ErrorKind.invalid_argument, "AVA_HOME must be an absolute path")
        return path
    home = os.environ.get("HOME")
    if home:
        return Path(home) / ".ava"
    raise AvaError(
        ErrorKind.invalid_argument, "cannot locate Ava home", "AVA_HOME and HOME are not set"
    )


def ascii_lower(text: str) -> str:
    return "".join(c.lower() if c < "\x80" else c for c in text)


def find_project_root(cwd: Path) -> Path:
    directory = cwd
    while True:
        if (directory / ".git").exists():
            return directory
        parent = directory.parent
        if parent == directory:
            return cwd
        directory = parent
