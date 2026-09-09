"""OAuth snapshots must not modify the login or leak refresh credentials to trials."""

from __future__ import annotations

import asyncio
import base64
import json
import shlex
import time
from contextlib import nullcontext
from pathlib import Path

import pytest

from eval.integrations.codex_auth import AUTH_ERROR, auth_documents, codex_environment


def login(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, expired: bool = False) -> Path:
    claims = {"exp": int(time.time()) + (-60 if expired else 3600),
              "https://api.openai.com/auth": {"chatgpt_account_id": "test-account"}}
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    document = {"auth_mode": "chatgpt", "OPENAI_API_KEY": "must-not-copy",
                "tokens": {"access_token": f"e30.{payload}.e30", "id_token": "identity",
                           "refresh_token": "must-not-refresh", "account_id": "test-account"}}
    path = tmp_path / "auth.json"
    path.write_text(json.dumps(document))
    path.chmod(0o600)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    return path


def test_oauth_snapshots_preserve_login_and_exclude_refresh_and_api_keys(tmp_path, monkeypatch):
    path = login(tmp_path, monkeypatch)
    before, stat = path.read_bytes(), path.stat()
    ava, pi = auth_documents()
    encoded = json.dumps([ava, pi])
    assert "must-not-copy" not in encoded and "must-not-refresh" not in encoded
    assert "refresh_token" not in ava["tokens"]
    assert pi["openai-codex"]["refresh"] == ""
    assert ava["tokens"]["access_token"] == pi["openai-codex"]["access"]
    assert path.read_bytes() == before
    assert (path.stat().st_mtime_ns, path.stat().st_mode) == (stat.st_mtime_ns, stat.st_mode)


@pytest.mark.parametrize("case", ["expired", "missing", "malformed", "oversized"])
def test_bad_login_fails_without_secret_in_error(tmp_path, monkeypatch, case):
    path = login(tmp_path, monkeypatch, expired=case == "expired")
    if case == "missing":
        path.unlink()
    elif case == "malformed":
        path.write_text("must-not-copy")
    elif case == "oversized":
        path.write_text("x" * 1_048_577)
    with pytest.raises(ValueError) as error:
        auth_documents()
    assert str(error.value) == AUTH_ERROR


def test_benchmark_does_not_fall_back_to_api_key_or_create_output(tmp_path, monkeypatch):
    from eval.benchmark import AgentSpec, Experiment, run

    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-use")
    config = Experiment(name="oauth", suite="unused.json", agents=[
        AgentSpec(id="ava", kind="ava", model="codex/gpt-6-astra", effort="medium"),
    ])
    output = tmp_path / "results"
    with pytest.raises(ValueError, match="Local Codex OAuth"):
        run(config, output, tmp_path / "missing-harbor")
    assert not output.exists()


@pytest.mark.parametrize("kind", ["ava", "pi"])
@pytest.mark.parametrize("failure", [None, RuntimeError, asyncio.CancelledError])
async def test_runtime_credentials_are_outside_logs_and_removed_on_failure(tmp_path, monkeypatch, kind, failure):
    login(tmp_path, monkeypatch)
    commands, uploads = [], []

    class Agent:
        async def exec_as_agent(self, environment, *, command):
            commands.append(shlex.split(command))

        exec_as_root = exec_as_agent

        async def _upload_config_text(self, environment, **kwargs):
            uploads.append(kwargs)

    with pytest.raises(failure) if failure else nullcontext():
        async with codex_environment(Agent(), object(), kind=kind) as env:
            remote = next(iter(env.values()))
            assert remote.startswith("/tmp/ava-benchmark-oauth-")
            assert uploads[0]["remote_path"] == f"{remote}/auth.json"
            assert ["chmod", "600", f"{remote}/auth.json"] in commands
            if failure:
                raise failure("agent failed")
    assert commands[-1] == ["rm", "-rf", "--", remote]
