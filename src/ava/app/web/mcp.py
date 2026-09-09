"""Machine-owned MCP configuration with workspace-bound status and connections."""

from __future__ import annotations

import asyncio
import shlex
import sqlite3
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import Field, ValidationError

from ava.tool.mcp import ServerConfig

from .models import RequestBody, parse_body
from .registry import Registry
from .routes import error_response


class Credential(RequestBody):
    name: str = Field(min_length=1, max_length=100)
    value: str | None = None


class ServerDraft(RequestBody):
    name: str = Field(min_length=1, max_length=80)
    transport: Literal["stdio", "http"]
    command_line: str = ""
    url: str = ""
    credentials: list[Credential] = Field(default_factory=list, max_length=64)
    version: int = Field(default=0, ge=0)
    request_id: str = Field(default="", pattern=r"^(|[a-f0-9]{32})$")


class ServerAction(RequestBody):
    version: int = Field(ge=1)
    enabled: bool | None = None


def register_mcp_routes(app: FastAPI, registry: Registry) -> None:
    @app.api_route("/api/projects/{project_id}/mcp", methods=["GET", "POST"])
    @app.api_route("/api/projects/{project_id}/mcp/{identity}", methods=["POST", "DELETE"])
    @app.post("/api/projects/{project_id}/mcp/{identity}/{action}")
    async def endpoint(project_id: str, request: Request, identity: str = "", action: str = "", cwd: str = "") -> Response:
        project = registry.find_project(project_id)
        if project is None or project.hidden:
            return error_response(404, "No such project.")
        directory = Path(cwd) if cwd else project.path
        if directory != project.path and not any(chat.agent.cwd == directory for chat in project.chats):
            return error_response(404, "No such project workspace.")
        servers = registry.mcp
        try:
            if request.method == "GET":
                rows = await servers.snapshot(directory)
                for row in rows:
                    row["command_line"] = shlex.join([row["command"], *row["args"]]) if row["command"] else ""
                return JSONResponse({"servers": rows})
            configs = await asyncio.to_thread(servers.configs)
            current = next((config for config in configs if config["id"] == identity), None)
            if identity and current is None:
                return error_response(404, "This MCP server no longer exists.")
            if action in {"connect", "refresh"}:
                body = await parse_body(request, ServerAction)
                if body is None or current is None or body.version != current["version"]:
                    return error_response(400, "This server changed. Refresh before connecting.")
                connection = await servers.connect(identity, directory, retry=True)
                if action == "refresh" and connection.phase == "connected":
                    try:
                        await connection.refresh_tools()
                    except Exception as error:
                        from ava.tool.mcp import _error

                        connection.changed("error", _error(error, connection.config))
                return JSONResponse({"status": connection.phase, "error": connection.error})
            if action == "toggle" or request.method == "DELETE":
                body = await parse_body(request, ServerAction)
                if body is None or current is None:
                    return error_response(400, "Provide the current server version.")
                if request.method == "DELETE":
                    await asyncio.to_thread(servers.remove, identity, body.version)
                else:
                    config = ServerConfig.model_validate({key: value for key, value in current.items() if key not in {"id", "version"}})
                    if body.enabled is None:
                        return error_response(400, "Choose whether to enable this server.")
                    config.enabled = body.enabled
                    await asyncio.to_thread(servers.save, config, identity, body.version)
                await servers.reconcile()
                return JSONResponse({"ok": True})
            if action:
                return error_response(404, "Unknown MCP action.")
            draft = await parse_body(request, ServerDraft)
            if draft is None:
                return error_response(400, "Provide valid server settings.")
            credentials: dict[str, str] = {}
            key = "env" if draft.transport == "stdio" else "headers"
            for credential in draft.credentials:
                if credential.name in credentials:
                    return error_response(400, "Each credential name must be unique.")
                if credential.value is None:
                    if not current or credential.name not in current[key]:
                        return error_response(400, "Enter a value for each new credential.")
                    credentials[credential.name] = current[key][credential.name]
                else:
                    credentials[credential.name] = credential.value
            argv = shlex.split(draft.command_line) if draft.transport == "stdio" else []
            config = ServerConfig(name=draft.name, transport=draft.transport,
                                  command=argv[0] if argv else "", args=argv[1:],
                                  url=draft.url if draft.transport == "http" else "",
                                  env=credentials if key == "env" else {}, headers=credentials if key == "headers" else {},
                                  enabled=current["enabled"] if current else True)
            if not identity and not draft.request_id:
                return error_response(400, "New servers require a request_id for safe retry.")
            saved = await asyncio.to_thread(servers.save, config, identity or draft.request_id, draft.version)
            await servers.reconcile()
            if config.enabled:
                await servers.connect(saved, directory, retry=True)
            return JSONResponse({"id": saved}, status_code=200 if identity else 201)
        except ValidationError as error:
            return error_response(400, "; ".join(item["msg"] for item in error.errors(include_input=False)))
        except (OSError, ValueError, sqlite3.Error) as error:
            return error_response(400, str(error))
