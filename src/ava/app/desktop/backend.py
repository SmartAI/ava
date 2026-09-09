"""An owned loopback backend: readiness on stdout, shutdown on stdin or parent exit."""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import socket
import sys
import threading
from pathlib import Path

import uvicorn

from ava.app.web.server import bind, create_app
from ava.llm import SelectionOverride


class DesktopServer(uvicorn.Server):
    def __init__(self, config: uvicorn.Config, port: int, token: str) -> None:
        super().__init__(config)
        self._port = port
        self._token = token

    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        await super().startup(sockets)
        if self.started:
            print(json.dumps({"port": self._port, "token": self._token}), flush=True)


async def serve(cwd: Path, selection: SelectionOverride) -> None:
    token = secrets.token_urlsafe(32)
    sock = bind(0)
    try:
        app = create_app(cwd, selection=selection, access_token=token)
        port = sock.getsockname()[1]
        app.state.bound_port = port
        server = DesktopServer(
            uvicorn.Config(app, log_level="warning", access_log=False, timeout_graceful_shutdown=3),
            port,
            token,
        )
        loop = asyncio.get_running_loop()

        def stop() -> None:
            server.should_exit = True

        def watch_parent() -> None:
            # Any command, including EOF when the parent dies, requests orderly shutdown.
            sys.stdin.buffer.read(1)
            try:
                loop.call_soon_threadsafe(stop)
            except RuntimeError:
                pass  # The server already exited and closed the loop.

        threading.Thread(target=watch_parent, daemon=True).start()
        await server.serve(sockets=[sock])
    finally:
        sock.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider")
    parser.add_argument("--model")
    parser.add_argument("--effort")
    args = parser.parse_args()
    asyncio.run(serve(Path.cwd(), SelectionOverride(args.provider, args.model, args.effort)))


if __name__ == "__main__":
    main()
