"""Real processes and HTTP verify daemon ownership, identity, and recovery."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from ava.app.backend import connect, stop
from ava.app.backend_state import write_private_json


@pytest.mark.skipif(
    os.environ.get("AVA_SERVICE_TESTS") != "1",
    reason="Installs an isolated user service; opt in with AVA_SERVICE_TESTS=1",
)
def test_user_service_restarts_without_clients_and_uninstalls(home, project, monkeypatch):
    home = home / 'background 数据 $100% "quote"'
    home.mkdir()
    monkeypatch.setenv("AVA_HOME", str(home))
    installed = command(project, "service-install")
    try:
        assert installed.returncode == 0, installed.stderr
        state = json.loads(installed.stdout)
        assert state["installed"] and state["active"] and state["autostart"]
        first = connect(home)
        assert first is not None
        # There are no desktop clients. Only the OS supervisor can restart it.
        start = time.monotonic()
        os.kill(first["pid"], signal.SIGKILL)
        restarted = None
        while time.monotonic() - start < 20:
            candidate = connect(home)
            if candidate and candidate["instance_id"] != first["instance_id"]:
                restarted = candidate
                break
            time.sleep(0.05)
        assert restarted is not None, "The supervisor did not restart the backend"
        assert restarted["machine_id"] == first["machine_id"]
        assert restarted["token"] != first["token"]
        print("SERVICE_RESTART_MS", round((time.monotonic() - start) * 1000))
        assert command(project, "connect").returncode == 0
        connected = connect(home)
        assert connected is not None
        assert connected["instance_id"] == restarted["instance_id"]
        assert command(project, "service-install").returncode == 0
        connected = connect(home)
        assert connected is not None
        assert connected["instance_id"] == restarted["instance_id"]
        assert command(project, "stop").returncode == 0
        # A deliberate successful shutdown must not be treated as a crash.
        time.sleep(4)
        assert connect(home) is None
        status = json.loads(command(project, "service-status").stdout)
        assert status["installed"] and status["autostart"] and not status["active"]
        assert command(project, "connect").returncode == 0
        connected = connect(home)
        assert connected is not None
        assert connected["machine_id"] == first["machine_id"]
    finally:
        removed = command(project, "service-uninstall", "--force")
        if installed.returncode == 0:
            assert removed.returncode == 0, removed.stderr
            assert not json.loads(removed.stdout)["installed"]
            assert connect(home) is None


def command(project, *args):
    return subprocess.run(
        [sys.executable, "-m", "ava.app.backend", *args, "--project", str(project)],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=35,
    )


def test_connect_refreshes_credential_environment_without_restart(home, project, monkeypatch):
    (home / "settings.json").write_text(json.dumps({"provider": "deepseek", "model": "deepseek-v4-pro"}))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    first = command(project, "connect")
    assert first.returncode == 0, first.stderr
    endpoint = json.loads(first.stdout)

    monkeypatch.setenv("DEEPSEEK_API_KEY", "fresh-key")
    refreshed = command(project, "connect")
    assert refreshed.returncode == 0, refreshed.stderr
    second = json.loads(refreshed.stdout)
    assert second["instance_id"] == endpoint["instance_id"]

    with httpx.Client(
        base_url=f"http://127.0.0.1:{second['port']}",
        headers={"Authorization": "Bearer " + second["token"]},
        trust_env=False,
    ) as client:
        check = client.post(
            "/api/system/environment",
            json={"variables": {"DEEPSEEK_API_KEY": "fresh-key"}},
        )
        assert check.status_code == 200, check.text
        assert check.json()["updated"] == []


def test_environment_sync_restarts_an_idle_legacy_backend(home, monkeypatch):
    from ava.app.backend import _sync_environment
    from ava.base import ava_home

    monkeypatch.setenv("DEEPSEEK_API_KEY", "fresh-key")
    response = httpx.Response(404, request=httpx.Request("POST", "http://127.0.0.1:1/api/system/environment"))
    monkeypatch.setattr("ava.app.backend.httpx.post", lambda *_args, **_kwargs: response)
    stopped: list[object] = []
    monkeypatch.setattr("ava.app.backend.stop", lambda *_args, **_kwargs: stopped.append(ava_home()))
    assert _sync_environment({"port": 1, "token": "token"}) is False
    assert stopped == [ava_home()]


def test_concurrent_clients_reuse_one_authenticated_backend(home, project):
    with ThreadPoolExecutor(max_workers=3) as workers:
        results = list(workers.map(lambda _: command(project, "connect"), range(3)))
    assert all(result.returncode == 0 for result in results), [r.stderr for r in results]
    endpoints = [json.loads(result.stdout) for result in results]
    assert len({entry["instance_id"] for entry in endpoints}) == 1
    assert len({entry["pid"] for entry in endpoints}) == 1
    endpoint = endpoints[0]
    assert endpoint == connect(home)
    assert (home / "backend.json").stat().st_mode & 0o777 == 0o600
    assert (home / "machine.json").stat().st_mode & 0o777 == 0o600
    for action in ("status", "start"):
        result = command(project, action)
        assert result.returncode == 0
        assert "token" not in json.loads(result.stdout)
        assert endpoint["token"] not in result.stdout + result.stderr
    base = f"http://127.0.0.1:{endpoint['port']}"
    with httpx.Client(base_url=base, trust_env=False) as client:
        assert client.get("/api/system").status_code == 401
        client.headers["Authorization"] = "Bearer " + endpoint["token"]
        response = client.get("/api/system")
        assert response.status_code == 200
        assert response.json()["machine_id"] == endpoint["machine_id"]
        assert "token" not in response.text
        assert (
            client.get("/api/system", headers={"Origin": "https://foreign.invalid"}).status_code
            == 403
        )
        assert client.get("/api/system", headers={"Host": "foreign.invalid"}).status_code == 403
        assert client.post("/api/system/shutdown", json={"force": "yes"}).status_code == 400
    stop(home)
    assert connect(home) is None


def test_backend_crash_recovers_identity_without_losing_projects(home, project, tmp_path):
    started = command(project, "connect")
    assert started.returncode == 0, started.stderr
    first = json.loads(started.stdout)
    other = tmp_path / "another project"
    other.mkdir()
    # A new desktop's requested project is added to the existing daemon.
    assert command(other, "connect").returncode == 0
    os.kill(first["pid"], signal.SIGKILL)
    # Ownership is proven by its open lock, not the stale PID/connection file.
    restarted = command(project, "connect")
    assert restarted.returncode == 0, restarted.stderr
    second = json.loads(restarted.stdout)
    assert second["machine_id"] == first["machine_id"]
    assert second["instance_id"] != first["instance_id"]
    assert second["token"] != first["token"]
    with httpx.Client(
        base_url=f"http://127.0.0.1:{second['port']}",
        headers={"Authorization": "Bearer " + second["token"]},
        trust_env=False,
    ) as client:
        projects = client.get("/api/projects").json()["projects"]
        assert {row["path"] for row in projects} == {str(project), str(other)}
        assert (
            client.get(
                "/api/system", headers={"Authorization": "Bearer " + first["token"]}
            ).status_code
            == 401
        )
    # Reject a descriptor pointing at the wrong incarnation or protocol.
    for changed, message in (
        ({"instance_id": "0" * 32}, "identity"),
        ({"protocol": 999}, "incompatible"),
    ):
        try:
            write_private_json(home / "backend.json", {**second, **changed})
            rejected = command(project, "connect")
            assert rejected.returncode != 0 and message in rejected.stderr
        finally:
            write_private_json(home / "backend.json", second)
    assert connect(home) == second


def test_standalone_web_server_cannot_race_daemon_state(home, project):
    started = command(project, "connect")
    assert started.returncode == 0, started.stderr
    before = (home / "web.json").read_bytes()
    contender = subprocess.run(
        [sys.executable, "-c", "from ava.app.cli import main; main()", "--serve", "0"],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert contender.returncode != 0 and "already using this Ava home" in contender.stderr
    assert (home / "web.json").read_bytes() == before
    connected = connect(home)
    assert connected is not None
    assert connected["instance_id"] == json.loads(started.stdout)["instance_id"]
