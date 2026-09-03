"""Provider-scoped API keys in ``$AVA_HOME/auth.json`` with atomic replacement."""

from __future__ import annotations

import json
import os
from pathlib import Path

from ava.base import AvaError, ErrorKind, ava_home


def _auth_path() -> Path:
    return ava_home() / "auth.json"


def stored_api_key(provider: str) -> str | None:
    path = _auth_path()
    if not path.exists():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise AvaError(ErrorKind.io, f"cannot read credential file '{path}'", str(error)) from error
    except json.JSONDecodeError:
        raise AvaError(ErrorKind.parse, f"cannot parse credential file '{path}'") from None
    if not isinstance(document, dict):
        raise AvaError(ErrorKind.parse, f"cannot parse credential file '{path}'")
    entry = document.get(provider)
    if entry is None:
        return None
    if not isinstance(entry, dict) or entry.get("type") != "api_key" or not entry.get("key"):
        raise AvaError(
            ErrorKind.auth, f"stored credential for provider '{provider}' is not a valid API key"
        )
    return str(entry["key"])


def _update_api_key(provider: str, key: str | None) -> None:
    if not provider or (key is not None and key == ""):
        raise AvaError(ErrorKind.invalid_argument, "provider and API key must not be empty")
    home = ava_home()
    path = home / "auth.json"
    document: dict = {}
    exists = path.exists()
    if exists:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise AvaError(
                ErrorKind.parse, f"cannot update invalid credential file '{path}'"
            ) from None
        if not isinstance(document, dict):
            raise AvaError(ErrorKind.parse, f"cannot update invalid credential file '{path}'")
    if key is not None:
        document[provider] = {"type": "api_key", "key": key}
    else:
        if not exists or document.pop(provider, None) is None:
            return
    encoded = json.dumps(document, ensure_ascii=False, separators=(",", ":"))
    created = not home.exists()
    home.mkdir(parents=True, exist_ok=True)
    if created:
        os.chmod(home, 0o700)
    temporary = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    try:
        with open(temporary, "w", encoding="utf-8") as output:
            output.write(encoded)
        mode = path.stat().st_mode & 0o777 if exists else 0o600
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise AvaError(
            ErrorKind.io, f"cannot replace credential file '{path}'", str(error)
        ) from error


def save_api_key(provider: str, key: str) -> None:
    _update_api_key(provider, key)


def delete_api_key(provider: str) -> None:
    _update_api_key(provider, None)
