"""Shared backend ownership and private, durable connection metadata."""

from __future__ import annotations

import fcntl
import json
import os
import secrets
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any

from ava.base import AvaError, ErrorKind

PROTOCOL_VERSION = 1


def write_private_json(path: Path, value: dict[str, Any]) -> None:
    write_private_file(path, (json.dumps(value, ensure_ascii=False) + "\n").encode())


def write_private_file(path: Path, content: bytes) -> None:
    temporary = path.with_name(path.name + "." + secrets.token_hex(8) + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def startup_lock(home: Path) -> Iterator[None]:
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(home / "backend-start.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    deadline = time.monotonic() + 30
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise AvaError(ErrorKind.network, "Timed out waiting for Ava backend startup.") from None
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)


class BackendState:
    """One writer per Ava home. Never unlink the lock inode when releasing it."""

    def __init__(self, home: Path) -> None:
        self.home = home
        self._fd = -1
        self._published = False
        home.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(home / "backend.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            os.close(fd)
            raise AvaError(
                ErrorKind.io,
                "Another Ava backend is already using this Ava home.",
                "Connect to the running backend, or stop it before starting another server.",
            ) from error
        self._fd = fd
        try:
            path = home / "machine.json"
            if path.exists():
                value = json.loads(path.read_text(encoding="utf-8"))
                machine_id = value["machine_id"]
                if not isinstance(machine_id, str) or len(machine_id) != 32:
                    raise ValueError("invalid machine identity")
                int(machine_id, 16)
            else:
                machine_id = secrets.token_hex(16)
                write_private_json(path, {"machine_id": machine_id})
            self.info = {
                "machine_id": machine_id,
                "instance_id": secrets.token_hex(16),
                "protocol": PROTOCOL_VERSION,
                "version": version("ava"),
                "pid": os.getpid(),
                "started_at": datetime.now(UTC).isoformat(),
                "capabilities": ["projects", "sessions", "event-replay", "session-review", "automations", "skills", "mcp", "analytics"],
            }
        except (KeyError, ValueError, TypeError, OSError) as error:
            self.close()
            raise AvaError(ErrorKind.io, "Cannot load Ava backend identity.", str(error)) from error

    def publish(self, port: int, token: str) -> None:
        write_private_json(self.home / "backend.json", {**self.info, "port": port, "token": token})
        self._published = True

    def close(self) -> None:
        if self._fd < 0:
            return
        try:
            if self._published:
                (self.home / "backend.json").unlink(missing_ok=True)
        finally:
            os.close(self._fd)
            self._fd = -1


def read_endpoint(home: Path) -> dict[str, Any] | None:
    path = home / "backend.json"
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "r", encoding="utf-8") as source:
        stat = os.fstat(source.fileno())
        if stat.st_uid != os.getuid() or stat.st_mode & 0o077 or stat.st_size > 4096:
            raise AvaError(ErrorKind.io, "Ava backend connection metadata is not private or valid.")
        try:
            value = json.load(source)
            if not isinstance(value, dict):
                raise ValueError("expected an object")
            port, token = value["port"], value["token"]
            if type(port) is not int or not 0 < port < 65536:
                raise ValueError("invalid port")
            if not isinstance(token, str) or len(token) < 32:
                raise ValueError("invalid token")
            for key in ("machine_id", "instance_id"):
                if not isinstance(value[key], str) or len(value[key]) != 32:
                    raise ValueError("invalid identity")
            if type(value["protocol"]) is not int:
                raise ValueError("invalid protocol")
        except (ValueError, TypeError, KeyError) as error:
            raise AvaError(
                ErrorKind.parse, "Cannot read Ava backend connection metadata."
            ) from error
        return value
