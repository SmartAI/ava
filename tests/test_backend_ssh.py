"""SSH transport contract against the isolated Fedora fixture (no user SSH credentials)."""

from __future__ import annotations

import json
import os
import shlex
import socket
import subprocess
import time

import httpx
import pytest


@pytest.mark.skipif(not os.environ.get("AVA_SSH_TEST_CONFIG"), reason="Requires the isolated Fedora SSH fixture")
def test_ssh_service_survives_disconnect_and_tunnel_keeps_host_fence():
    ssh = ["ssh", "-F", os.environ["AVA_SSH_TEST_CONFIG"]]

    def remote(*arguments):
        response = subprocess.run(
            [*ssh, "ava-test", shlex.join(arguments)], capture_output=True, text=True, timeout=40
        )
        assert response.returncode == 0, response.stderr
        return response.stdout

    backend = ("/opt/ava/bin/python", "-m", "ava.app.backend")
    tunnel = None
    try:
        installed = json.loads(remote(*backend, "service-install"))
        assert installed["active"] and installed["autostart"] and installed["linger"]
        remote("mkdir", "-p", "/home/ava-test/remote project")
        info = json.loads(remote(*backend, "connect", "--project", "/home/ava-test/remote project"))
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        assert port != info["port"]
        tunnel = subprocess.Popen(
            [*ssh, "-N", "-o", "ExitOnForwardFailure=yes", "-L",
             f"127.0.0.1:{port}:127.0.0.1:{info['port']}", "ava-test"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=2) as client:
            deadline = time.monotonic() + 10
            while True:
                try:
                    response = client.get("/api/system")
                    break
                except httpx.TransportError:
                    assert tunnel.poll() is None and time.monotonic() < deadline
                    time.sleep(0.05)
            assert response.status_code == 403  # Local port is not the remote authority.
            client.headers["Host"] = f"127.0.0.1:{info['port']}"
            assert client.get("/api/system").status_code == 401
            client.headers["Authorization"] = "Bearer " + info["token"]
            identity = client.get("/api/system").json()
            assert identity["machine_id"] == info["machine_id"]
            assert client.get("/api/system", headers={"Origin": "https://foreign.invalid"}).status_code == 403
            projects = client.get("/api/projects").json()["projects"]
            assert any(project["path"] == "/home/ava-test/remote project" for project in projects)
        tunnel.terminate()
        tunnel.wait(timeout=5)
        # Every SSH command/tunnel has exited; the remote service remains the same instance.
        again = json.loads(remote(*backend, "connect", "--project", "/home/ava-test/remote project"))
        assert again["instance_id"] == info["instance_id"]
        assert json.loads(remote(*backend, "service-status"))["active"]
        if container := os.environ.get("AVA_SSH_TEST_CONTAINER"):
            restarted = subprocess.run(["docker", "restart", "-t", "10", container], capture_output=True, timeout=40)
            assert restarted.returncode == 0, restarted.stderr
            forwarded = subprocess.run(["docker", "port", container, "22/tcp"], capture_output=True, text=True, check=True, timeout=5)
            ssh += ["-p", str(int(forwarded.stdout.strip().rsplit(":", 1)[1]))]
            deadline = time.monotonic() + 20
            # Observe from Docker, before any SSH login can start the user manager.
            while True:
                active = subprocess.run([
                    "docker", "exec", "--user", "ava-test", "-e", "XDG_RUNTIME_DIR=/run/user/1000",
                    container, "systemctl", "--user", "is-active", installed["name"],
                ], capture_output=True, timeout=5)
                if active.returncode == 0:
                    break
                assert time.monotonic() < deadline, active.stderr
                time.sleep(0.1)
            booted = json.loads(remote(*backend, "connect", "--project", "/home/ava-test/remote project"))
            assert booted["machine_id"] == info["machine_id"]
            assert booted["instance_id"] != info["instance_id"]
    finally:
        if tunnel is not None:
            if tunnel.poll() is None:
                tunnel.terminate()
                tunnel.wait(timeout=5)
            if tunnel.stderr:
                tunnel.stderr.close()
        remote(*backend, "service-uninstall", "--force")


def test_backend_bundle_contains_package_code_without_local_artifacts(tmp_path, monkeypatch):
    import io
    import zipfile

    from ava.app.desktop.ssh import backend_wheel

    package = tmp_path / "ava"
    package.mkdir()
    (package / "__init__.py").write_text("# package")
    (package / ".env").write_text("private local configuration")
    (package / ".DS_Store").write_bytes(b"Finder metadata")
    (package / "__pycache__").mkdir()
    (package / "__pycache__/module.pyc").write_bytes(b"bytecode")
    outside = tmp_path / "outside.txt"
    outside.write_text("not package content")
    (package / "linked.txt").symlink_to(outside)
    monkeypatch.setattr("ava.__file__", str(package / "__init__.py"))
    filename, data = backend_wheel()
    assert filename.endswith("-py3-none-any.whl")
    assert backend_wheel()[1] == data
    with zipfile.ZipFile(io.BytesIO(data)) as wheel:
        assert [name for name in wheel.namelist() if name.startswith("ava/")] == ["ava/__init__.py"]
        assert wheel.read("ava/__init__.py") == b"# package"


def test_ssh_askpass_only_accepts_new_host_fingerprints():
    import sys

    from ava.app.desktop import _ssh_askpass

    fingerprint = "SHA256:" + "A" * 43
    prompt = ("The authenticity of host 'jump.example (10.0.0.1)' can't be established.\n"
              f"ED25519 key fingerprint is {fingerprint}.\n"
              "Are you sure you want to continue connecting (yes/no/[fingerprint])? ")
    for value in (prompt, prompt.replace("fingerprint is ", "fingerprint is: ")):
        key = _ssh_askpass.host_key(value)
        assert key and key["host"] == "jump.example (10.0.0.1)" and key["fingerprint"] == fingerprint
    # Passphrases and changed-IP yes/no confirmations cannot become trust prompts.
    for value in ("Enter passphrase for key '/tmp/id': ", "user@host's password: ",
                  "REMOTE HOST IDENTIFICATION HAS CHANGED!\n" + prompt,
                  "Warning: the host key differs from the key for the IP address.\nAre you sure? (yes/no)",
                  prompt.replace(fingerprint, "SHA256:truncated")):
        assert _ssh_askpass.host_key(value) is None
        result = subprocess.run([sys.executable, _ssh_askpass.__file__, value],
                                capture_output=True, text=True, timeout=3)
        assert result.returncode != 0 and not result.stdout


def test_remote_workspace_refuses_a_different_machine_before_running_git(home, project):
    import sys

    from ava.app.backend_state import BackendState

    state = BackendState(home)
    command = [sys.executable, "-m", "ava.app.remote", "--machine-id"]
    try:
        rejected = subprocess.run([*command, "0" * 32, "--project", str(project), "git", "init", "-q"], capture_output=True, text=True, timeout=5)
        assert rejected.returncode == 1 and "Machine identity changed" in rejected.stderr
        assert not (project / ".git").exists()
        accepted = subprocess.run([*command, str(state.info["machine_id"]), "--project", str(project), "git", "init", "-q"], capture_output=True, text=True, timeout=5)
        assert accepted.returncode == 0, accepted.stderr
        assert (project / ".git").is_dir()
    finally:
        state.close()
