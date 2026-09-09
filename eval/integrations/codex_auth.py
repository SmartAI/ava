"""Read local Codex OAuth and inject disposable, access-only agent credentials."""

from __future__ import annotations

import base64
import json
import os
import shlex
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

AUTH_ERROR = "Local Codex OAuth is missing, invalid, or expired; refresh the Codex login before running"


def auth_documents() -> tuple[dict[str, Any], dict[str, Any]]:
    """Never return refresh tokens or API keys, or modify the source login."""
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    try:
        with (home / "auth.json").open("rb") as stream:
            raw = stream.read(1_048_577)
        if len(raw) > 1_048_576:
            raise ValueError
        document = json.loads(raw)
        if document["auth_mode"] != "chatgpt":
            raise ValueError
        tokens = document["tokens"]
        access, identity = tokens["access_token"], tokens["id_token"]
        if not isinstance(access, str) or not isinstance(identity, str) or not identity:
            raise ValueError
        payload = access.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        expiry = claims["exp"]
        if type(expiry) is not int or expiry <= time.time() + 60:
            raise ValueError
        account = claims["https://api.openai.com/auth"]["chatgpt_account_id"]
        if not isinstance(account, str) or not account or any(ord(c) < 33 or ord(c) > 126 for c in account):
            raise ValueError
        if tokens.get("account_id") and tokens["account_id"] != account:
            raise ValueError
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        raise ValueError(AUTH_ERROR) from None
    return (
        {"auth_mode": "chatgpt", "tokens": {
            "access_token": access, "id_token": identity, "account_id": account,
        }},
        {"openai-codex": {
            "type": "oauth", "access": access, "refresh": "", "expires": expiry * 1000,
            "accountId": account,
        }},
    )


@asynccontextmanager
async def codex_environment(agent: Any, environment: Any, *, kind: str) -> AsyncIterator[dict[str, str]]:
    """Keep credentials outside Harbor's mounted logs and frozen experiment inputs."""
    if kind not in {"ava", "pi"}:
        raise ValueError("Codex credential target must be ava or pi")
    ava, pi = auth_documents()
    remote = f"/tmp/ava-benchmark-oauth-{uuid.uuid4().hex}"
    await agent.exec_as_agent(environment, command=shlex.join(["mkdir", "-m", "700", remote]))
    try:
        await agent._upload_config_text(
            environment, content=json.dumps(ava if kind == "ava" else pi),
            remote_path=f"{remote}/auth.json", filename="auth.json",
        )
        await agent.exec_as_root(environment, command=shlex.join(["chmod", "600", f"{remote}/auth.json"]))
        yield {"CODEX_HOME" if kind == "ava" else "PI_CODING_AGENT_DIR": remote}
    finally:
        await agent.exec_as_root(environment, command=shlex.join(["rm", "-rf", "--", remote]))
