"""Composition, loopback fencing, and serving for the Web UI."""

from __future__ import annotations

import secrets
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import cache
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request

from ava.agent import CompactionOptions
from ava.agent.skills import revision as skill_revision
from ava.app.backend_state import BackendState
from ava.base import AvaError, ErrorKind, ava_home
from ava.llm import SelectionOverride, provider_from_environment

from .automations import Automations, register_automation_routes
from .mcp import register_mcp_routes
from .registry import Registry, WebState
from .routes import error_response, register_routes
from .skills import register_skill_routes
from .workspace import register_workspace_routes

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
    cwd: Path | None,
    options: CompactionOptions | None = None,
    selection: SelectionOverride | None = None,
    *,
    access_token: str | None = None,
) -> FastAPI:
    compaction = options or CompactionOptions()
    selected = selection or SelectionOverride()
    backend = BackendState(ava_home())
    try:
        registry = Registry(cwd.resolve() if cwd is not None else None)
    except BaseException:
        backend.close()
        raise

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            registry.restore(compaction, selected, provider_from_environment)
            await automations.start()
            yield
        finally:
            try:
                try:
                    await automations.stop_dispatch()
                finally:
                    try:
                        await registry.aclose()
                    finally:
                        if hasattr(automations, "store"):
                            await automations.close()
            finally:
                backend.close()

    app = FastAPI(
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.registry = registry
    app.state.backend = backend
    app.state.bound_port = 0
    state = WebState(
        registry=registry,
        compaction=compaction,
        selection=selected,
        provider_factory=provider_from_environment,
    )
    automations = Automations(state, ava_home() / "automations.sqlite3")
    app.state.automations = automations

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
        if access_token is not None and not secrets.compare_digest(
            request.headers.get("authorization", "").encode(), f"Bearer {access_token}".encode()
        ):
            return error_response(401, "authentication required")
        return await call_next(request)

    register_routes(app, state, web_asset)
    register_workspace_routes(app, registry)
    register_automation_routes(app, automations)
    register_skill_routes(app, registry)
    register_mcp_routes(app, registry)

    @app.get("/api/system")
    async def system_info() -> dict:
        return {**backend.info, "navigation_revision": registry.revision, "automation_revision": automations.store.revision, "automation_error": automations.error, "skill_revision": skill_revision(), "mcp_revision": str(registry.mcp.generation) + ":" + skill_revision()}

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
