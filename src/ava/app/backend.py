"""Persistent, authenticated loopback backend; also usable under launchd/systemd."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import Request

from ava.app.backend_state import PROTOCOL_VERSION, BackendState, read_endpoint, startup_lock
from ava.app.web.routes import error_response
from ava.app.web.server import bind, create_app
from ava.base import AvaError, ErrorKind, ava_home
from ava.llm import SelectionOverride


def connect(home: Path) -> dict[str, Any] | None:
    """Verify a private endpoint, including instance identity, before reusing it."""
    info = read_endpoint(home)
    if info is None:
        return None
    if info["protocol"] != PROTOCOL_VERSION:
        raise AvaError(ErrorKind.invalid_argument, "The Ava backend uses an incompatible protocol.")
    try:
        response = httpx.get(
            f"http://127.0.0.1:{info['port']}/api/system",
            headers={"Authorization": "Bearer " + info["token"]},
            timeout=2,
            trust_env=False,
        )
    except httpx.TransportError:
        return None
    if response.status_code != 200:
        raise AvaError(ErrorKind.network, "Cannot authenticate the running Ava backend.")
    try:
        identity = response.json()
        if any(identity[key] != info[key] for key in ("machine_id", "instance_id", "protocol")):
            raise ValueError("identity mismatch")
    except (ValueError, KeyError, TypeError) as error:
        raise AvaError(ErrorKind.network, "The Ava backend identity did not match.") from error
    return info


def _credential_environment_values() -> dict[str, str]:
    """Return only API-key variables present in this process's environment."""
    from ava.app.web.providers import credential_environment

    try:
        configured = credential_environment()
    except AvaError:
        return {}
    return {
        name: value
        for name in set(configured.values())
        if name and (value := os.environ.get(name))
    }


def _sync_environment(info: dict[str, Any]) -> bool:
    """Copy fresh shell credentials into a running backend without persisting them.

    Returns False when an old backend was stopped so the caller can start one that
    inherits this process's environment.
    """
    variables = _credential_environment_values()
    if not variables:
        return True
    try:
        response = httpx.post(
            f"http://127.0.0.1:{info['port']}/api/system/environment",
            headers={"Authorization": "Bearer " + info["token"]},
            json={"variables": variables},
            timeout=5,
            trust_env=False,
        )
    except httpx.TransportError:
        return True
    if response.status_code == 200:
        return True
    # The running backend predates live credential sync. Restart it when it is idle;
    # active tasks are intentionally left alone.
    try:
        stop(ava_home())
    except AvaError:
        return True
    return False


def ensure_running(cwd: Path | None) -> dict[str, Any]:
    home = ava_home()
    from ava.app.backend_service import start_installed

    with startup_lock(home):
        deadline = time.monotonic() + 30
        info = connect(home)
        if info is not None and _sync_environment(info):
            return info
        if start_installed(home):
            while time.monotonic() < deadline:
                info = connect(home)
                if info is not None and _sync_environment(info):
                    return info
                time.sleep(0.05)
            raise AvaError(ErrorKind.network, "Ava background service is not ready. Check its service log.")
        log_path = home / "backend.log"
        log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(log_fd, "ab") as log:
            start_offset = os.fstat(log.fileno()).st_size
            process = subprocess.Popen(
                [sys.executable, "-m", "ava.app.backend", "serve", *(["--project", str(cwd)] if cwd else ["--no-project"])],
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=log,
                start_new_session=True,
            )
        while time.monotonic() < deadline:
            if process.poll() is not None:
                with log_path.open("rb") as log:
                    log.seek(start_offset)
                    detail = log.read(4096).decode("utf-8", "replace").strip()
                raise AvaError(ErrorKind.network, "Ava backend failed to start.", detail)
            info = connect(home)
            if info is not None and _sync_environment(info):
                return info
            time.sleep(0.05)
        # Do not terminate a backend another desktop could already be using.
        raise AvaError(ErrorKind.network, "Ava is still starting. Reconnect to check its status.")


def stop(home: Path, *, force: bool = False, defer_if_busy: bool = False) -> bool:
    """Return False only when an optional update is deferred by active tasks."""
    info = connect(home)
    if info is None:
        return True
    response = httpx.post(
        f"http://127.0.0.1:{info['port']}/api/system/shutdown",
        headers={"Authorization": "Bearer " + info["token"]},
        json={"force": force},
        timeout=5,
        trust_env=False,
    )
    if response.status_code == 409 and defer_if_busy and not force:
        return False
    if response.status_code != 200:
        raise AvaError(ErrorKind.invalid_argument, response.json().get("error", "Cannot stop Ava."))
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        current = read_endpoint(home)
        if current is None or current["instance_id"] != info["instance_id"]:
            return True
        time.sleep(0.05)
    raise AvaError(ErrorKind.network, "Ava backend has not finished shutting down.")


class BackendServer(uvicorn.Server):
    def __init__(self, config: uvicorn.Config, state: BackendState, port: int, token: str) -> None:
        super().__init__(config)
        self._state, self._port, self._token = state, port, token

    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        await super().startup(sockets)
        if self.started:
            self._state.publish(self._port, self._token)


async def serve(cwd: Path | None, selection: SelectionOverride, port: int = 0) -> None:
    sock = bind(port)
    app = None
    try:
        token = secrets.token_urlsafe(32)
        app = create_app(cwd, selection=selection, access_token=token)
        app.state.bound_port = sock.getsockname()[1]
        server = BackendServer(
            uvicorn.Config(app, log_level="warning", access_log=False, timeout_graceful_shutdown=3),
            app.state.backend,
            app.state.bound_port,
            token,
        )

        @app.post("/api/system/shutdown")
        async def shutdown(request: Request):
            try:
                body = await request.json()
            except ValueError:
                return error_response(400, "expected a JSON object")
            if not isinstance(body, dict) or type(body.get("force", False)) is not bool:
                return error_response(400, "force must be a boolean")
            running = any(
                chat.status not in ("idle", "error")
                for project in app.state.registry.projects
                for chat in project.chats
            )
            if running and not body.get("force", False):
                return error_response(409, "Tasks are active. Stop them first or use --force.")
            server.should_exit = True
            return {"status": "stopping"}

        await server.serve(sockets=[sock])
    finally:
        sock.close()
        if app is not None:
            app.state.backend.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Persistent Ava backend")
    parser.add_argument(
        "action",
        choices=("start", "connect", "serve", "status", "stop", "service-install", "service-status", "service-uninstall", "service-restart"),
        nargs="?",
        default="start",
    )
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--no-project", action="store_true", help="Use existing projects without adding the working directory")
    parser.add_argument("--machine-id", help="Refuse operations on a different Ava data store")
    parser.add_argument("--provider")
    parser.add_argument("--model")
    parser.add_argument("--effort")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--force", action="store_true", help="Stop even while tasks are active")
    parser.add_argument("--defer-if-busy", action="store_true", help="Keep the current backend when active tasks prevent a service update")
    args = parser.parse_args()
    if args.defer_if_busy and (args.action != "service-install" or args.force):
        parser.error("--defer-if-busy requires service-install and cannot be combined with --force")
    if not 0 <= args.port < 65536:
        parser.error("--port must be between 0 and 65535")
    if args.action != "serve" and any((args.port, args.provider, args.model, args.effort)):
        parser.error("--port, --provider, --model and --effort configure foreground 'serve' only")
    cwd = args.project.expanduser().resolve()
    if args.action in ("start", "connect", "serve") and not args.no_project and not cwd.is_dir():
        parser.error("--project must name an existing directory")
    try:
        if args.machine_id:
            from ava.app.backend_state import read_endpoint
            endpoint = read_endpoint(ava_home())
            if endpoint is None or endpoint.get("machine_id") != args.machine_id:
                raise AvaError(ErrorKind.invalid_argument, "Machine identity changed or cannot be verified.")
        if args.action.startswith("service-"):
            from ava.app.backend_service import install_service, service_status, uninstall_service

            if args.action == "service-restart":
                with startup_lock(ava_home()):
                    stop(ava_home(), force=args.force)
                ensure_running(None)
                result = service_status(ava_home())
            elif args.action == "service-install":
                result = install_service(ava_home(), force=args.force, defer_if_busy=args.defer_if_busy)
            elif args.action == "service-uninstall":
                result = uninstall_service(ava_home(), force=args.force)
            else:
                result = service_status(ava_home())
            print(json.dumps(result))
            return 0
        if args.action == "serve":
            asyncio.run(
                serve(None if args.no_project else cwd, SelectionOverride(args.provider, args.model, args.effort), args.port)
            )
            return 0
        if args.action == "stop":
            stop(ava_home(), force=args.force)
            print(json.dumps({"status": "stopped"}))
            return 0
        if args.action == "status":
            info = connect(ava_home())
        else:
            info = ensure_running(None if args.no_project else cwd)
            if not args.no_project:
                response = httpx.post(
                    f"http://127.0.0.1:{info['port']}/api/projects",
                    headers={"Authorization": "Bearer " + info["token"]},
                    json={"path": str(cwd), "restore": False},
                    timeout=10,
                    trust_env=False,
                )
                if response.status_code not in (200, 201):
                    raise AvaError(ErrorKind.network, "Could not open the project on the Ava backend.")
        if info is None:
            print(json.dumps({"status": "stopped"}))
        else:
            # Only the desktop/SSH bootstrap command returns the private bearer token.
            print(
                json.dumps(
                    info
                    if args.action == "connect"
                    else {key: value for key, value in info.items() if key != "token"}
                )
            )
        return 0
    except (AvaError, OSError, httpx.HTTPError) as error:
        print(f"ava-backend: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
