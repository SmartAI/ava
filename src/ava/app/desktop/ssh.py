"""Private SSH bootstrap helper. Owns a tunnel, never the remote agent process."""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import re
import shlex
import signal
import socket
import subprocess
import sys
import time
import zipfile
from importlib.metadata import distribution
from pathlib import Path

import httpx

import ava
from ava.app.backend_state import PROTOCOL_VERSION


def ssh_arguments(host: str, *, terminal: bool = False, confirm_host: bool = False) -> list[str]:
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.@:\[\]-]{0,254}", host):
        raise ValueError("Use an SSH host alias or user@hostname, without spaces or options.")
    args = ["ssh"]
    if config := os.environ.get("AVA_SSH_CONFIG"):
        args += ["-F", config]
    trust = ["-o", "BatchMode=no", "-o", "StrictHostKeyChecking=ask", "-o", "FingerprintHash=sha256",
             "-o", "PasswordAuthentication=no", "-o", "KbdInteractiveAuthentication=no", "-o", "NumberOfPasswordPrompts=0"] if confirm_host else ["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes"]
    return [*args, "-tt" if terminal else "-T", *trust,
            "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=2",
            "-o", "ForwardAgent=no", "-o", "RequestTTY=force" if terminal else "RequestTTY=no", "-o", "EscapeChar=none", "-o", "ControlMaster=no",
            "-o", "ControlPath=none", "-o", "ForkAfterAuthentication=no"]


def workspace_command(remote: dict, root: str, action: str, arguments: list[str] | None = None) -> list[str]:
    command = [remote["python"], "-m", "ava.app.remote", "--machine-id", remote["machine_id"],
               "--project", root, action, *(arguments or [])]
    return [*ssh_arguments(remote["host"], terminal=action == "terminal"), remote["host"], shlex.join(command)]


def backend_wheel() -> tuple[str, bytes]:
    """Package the installed pure-Python distribution, including editable development builds.

    Stable zip timestamps make the content hash an immutable deployment identity. No build
    system, local environment files, credentials, or project files are sent to the server.
    """
    package = Path(ava.__file__).parent
    dist = distribution("ava")
    info = f"ava-{dist.version}.dist-info"
    entries = {str(path.relative_to(package.parent)): path.read_bytes()
               for path in sorted(package.rglob("*"))
               if path.is_file() and not path.is_symlink() and "__pycache__" not in path.parts
               and path.suffix not in {".pyc", ".pyo"}
               and not any(part.startswith(".") for part in path.relative_to(package).parts)}
    entries[info + "/METADATA"] = (dist.read_text("METADATA") or "").encode()
    entries[info + "/entry_points.txt"] = (dist.read_text("entry_points.txt") or "").encode()
    entries[info + "/WHEEL"] = b"Wheel-Version: 1.0\nGenerator: ava-desktop\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    records = [f"{name},sha256={base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip('=')},{len(data)}"
               for name, data in entries.items()]
    entries[info + "/RECORD"] = ("\n".join([*records, info + "/RECORD,,"]) + "\n").encode()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as wheel:
        for name, data in entries.items():
            member = zipfile.ZipInfo(name)
            member.compress_type = zipfile.ZIP_DEFLATED
            member.external_attr = 0o644 << 16
            wheel.writestr(member, data)
    return f"ava-{dist.version}-py3-none-any.whl", buffer.getvalue()


def connect_remote(host: str) -> None:
    args = ssh_arguments(host)
    child: subprocess.Popen | None = None

    def execute(arguments: list[str], data: bytes | None = None, timeout: int = 30, *, environment: dict | None = None) -> str:
        nonlocal child
        command = ssh_arguments(host, confirm_host=True) if environment is not None else args
        child = subprocess.Popen([*command, host, shlex.join(arguments)], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment)
        output, errors = child.communicate(data, timeout=timeout)
        if child.returncode:
            raise ValueError(errors.decode("utf-8", "replace")[-3000:].strip() or "SSH command failed.")
        child = None
        return output.decode("utf-8")

    try:
        print("AVA_PROGRESS Connecting over SSH…", file=sys.stderr, flush=True)
        from ._ssh_askpass import prompt_environment

        with prompt_environment() as environment:
            probe = json.loads(execute(["python3", "-c", "import json,sys,pathlib; print(json.dumps({'home':str(pathlib.Path.home()),'version':list(sys.version_info[:2]),'platform':sys.platform}))"], timeout=300, environment=environment))
        if probe["version"] < [3, 12] or probe["platform"] not in ("linux", "darwin"):
            raise ValueError("The remote machine needs Python 3.12 or newer and Linux/systemd or macOS/launchd.")
        filename, wheel = backend_wheel()
        digest = hashlib.sha256(wheel).hexdigest()
        folder = probe["home"] + "/.local/share/ava/backends/" + digest[:24]
        python = folder + "/venv/bin/python"
        prepared = execute(["python3", "-c", "from pathlib import Path; import sys; print(Path(sys.argv[1], '.ready').is_file())", folder]).strip() == "True"
        if not prepared:
            print("AVA_PROGRESS Installing the matching Ava backend…", file=sys.stderr, flush=True)
            execute(["python3", "-c", "from pathlib import Path; import sys,hashlib; data=sys.stdin.buffer.read(); assert hashlib.sha256(data).hexdigest()==sys.argv[3]; p=Path(sys.argv[1]); p.mkdir(parents=True,exist_ok=True); (p/sys.argv[2]).write_bytes(data)", folder, filename, digest], wheel)
            execute(["python3", "-m", "venv", folder + "/venv"], timeout=60)
            execute([python, "-m", "pip", "install", "--disable-pip-version-check", folder + "/" + filename], timeout=180)
            execute([python, "-c", "from ava.app.backend_state import PROTOCOL_VERSION; import sys; assert PROTOCOL_VERSION==int(sys.argv[1])", str(PROTOCOL_VERSION)])
            execute(["python3", "-c", "from pathlib import Path; import sys; Path(sys.argv[1], '.ready').touch()", folder])
        print("AVA_PROGRESS Starting the remote background service…", file=sys.stderr, flush=True)
        # The service definition is authoritative: another desktop may have
        # activated a different package since this one was last connected.
        service = json.loads(execute([python, "-m", "ava.app.backend", "service-status"]))
        # A removed service on a previously activated installation means the
        # user turned startup off. Reconnecting must preserve that choice.
        if service["installed"] or execute(["python3", "-c", "from pathlib import Path; import sys; print(Path(sys.argv[1], '.activated').is_file())", folder]).strip() != "True":
            service = json.loads(execute([python, "-m", "ava.app.backend", "service-install", "--defer-if-busy"], timeout=70))
            if not service.get("update_pending"):
                execute(["python3", "-c", "from pathlib import Path; import sys; Path(sys.argv[1], '.activated').touch()", folder])
        if service.get("update_pending"):
            print("AVA_PROGRESS Connecting to the existing backend; update deferred…", file=sys.stderr, flush=True)
        info = json.loads(execute([python, "-m", "ava.app.backend", "connect", "--no-project"], timeout=40))
        if info.get("protocol") != PROTOCOL_VERSION:
            raise ValueError("The remote backend uses an incompatible protocol.")
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        authority = f"127.0.0.1:{info['port']}"
        child = subprocess.Popen([*args, "-N", "-o", "ExitOnForwardFailure=yes", "-L",
                                  f"127.0.0.1:{port}:{authority}", host], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=2,
                          headers={"Host": authority, "Authorization": "Bearer " + info["token"]}) as client:
            deadline = time.monotonic() + 15
            while True:
                try:
                    response = client.get("/api/system")
                    identity = response.json()
                    if response.status_code != 200 or any(identity.get(key) != info.get(key) for key in ("machine_id", "instance_id", "protocol")):
                        raise ValueError("Remote machine identity did not match the SSH bootstrap.")
                    if not {"projects", "sessions", "event-replay"}.issubset(identity.get("capabilities", [])):
                        raise ValueError("The remote backend is missing required capabilities. Update it after its tasks finish.")
                    break
                except httpx.TransportError:
                    if child.poll() is not None or time.monotonic() >= deadline:
                        raise ValueError("The SSH tunnel could not connect to the remote backend.") from None
                    time.sleep(0.05)
        # This stdout is private IPC with the desktop, never a user-facing log.
        print(json.dumps({**info, "port": port, "authority": authority, "python": python, "service": service}), flush=True)
        if child.wait():
            raise ValueError("SSH disconnected. Reconnect to see the remote tasks' current state.")
    finally:
        if child is not None:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=3)
            for stream in (child.stdin, child.stdout, child.stderr):
                if stream:
                    stream.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Private Ava SSH connection helper")
    parser.add_argument("--host", required=True)
    args = parser.parse_args()

    def stop(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    try:
        connect_remote(args.host)
        return 0
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
        print(f"ava-ssh: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
