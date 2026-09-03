"""Composition, loopback fencing, and serving for the Web UI."""

from __future__ import annotations

import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import cache
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request

from ava.agent import CompactionOptions
from ava.base import AvaError, ErrorKind
from ava.llm import SelectionOverride, provider_from_environment

from .registry import Registry, WebState
from .routes import error_response, register_routes

DEFAULT_PORT = 8777
_ASSETS = Path(__file__).parent / "assets"


@cache
def web_asset() -> str:
    page = (_ASSETS / "index.html").read_text(encoding="utf-8")
    replacements = {
        "@AVA_KATEX_CSS@": _ASSETS / "vendor" / "katex.css",
        "@AVA_REACT_CSS@": _ASSETS / "app.min.css",
        "@AVA_KATEX_JS@": _ASSETS / "vendor" / "katex.min.js",
        "@AVA_REACT_JS@": _ASSETS / "app.min.js",
    }
    for marker, path in replacements.items():
        page = page.replace(marker, path.read_text(encoding="utf-8"))
    return page


def create_app(
    cwd: Path, options: CompactionOptions | None = None, selection: SelectionOverride | None = None
) -> FastAPI:
    registry = Registry(cwd.resolve())

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await registry.aclose()

    app = FastAPI(
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.registry = registry
    app.state.bound_port = 0
    state = WebState(
        registry=registry,
        compaction=options or CompactionOptions(),
        selection=selection or SelectionOverride(),
        provider_factory=provider_from_environment,
    )

    @app.middleware("http")
    async def fence(request: Request, call_next: Any):
        host = request.headers.get("host")
        port = app.state.bound_port
        default_port = port == 80 and host in ("127.0.0.1", "localhost")
        expected = (f"127.0.0.1:{port}", f"localhost:{port}")
        allowed_host = host is not None and (default_port or host in expected)
        origin = request.headers.get("origin")
        if not allowed_host or (origin is not None and origin != f"http://{host}"):
            return error_response(403, "forbidden request")
        return await call_next(request)

    register_routes(app, state, web_asset)
    return app


def bind(port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("127.0.0.1", port))
    except OSError as error:
        sock.close()
        raise AvaError(
            ErrorKind.network, f"cannot bind Web UI to 127.0.0.1:{port}: {error.strerror}"
        ) from error
    sock.listen(128)
    return sock


def create_server(app: FastAPI, sock: socket.socket):
    import uvicorn

    app.state.bound_port = sock.getsockname()[1]
    config = uvicorn.Config(app, log_level="warning", lifespan="on", access_log=False)
    return uvicorn.Server(config)


async def serve(app: FastAPI, sock: socket.socket) -> None:
    await create_server(app, sock).serve(sockets=[sock])
