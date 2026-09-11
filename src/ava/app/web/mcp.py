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
from ava.tool.mcp_oauth import AuthenticationRequired, OAuthConfig

from .models import RequestBody, parse_body
from .registry import Registry
from .routes import error_response


class Credential(RequestBody):
    name: str = Field(min_length=1, max_length=100)
    value: str | None = None


class OAuthDraft(RequestBody):
    client_id: str = ""
    client_secret: str | None = ""
    issuer: str = ""
    scope: str = ""
    redirect_uri: str = "http://127.0.0.1:8766/oauth/callback"


class ServerDraft(RequestBody):
    name: str = Field(min_length=1, max_length=80)
    transport: Literal["stdio", "http"]
    command_line: str = ""
    url: str = ""
    credentials: list[Credential] = Field(default_factory=list, max_length=64)
    oauth: OAuthDraft | None = None
    version: int = Field(default=0, ge=0)
    request_id: str = Field(default="", pattern=r"^(|[a-f0-9]{32})$")


class ServerAction(RequestBody):
    version: int = Field(ge=1)
    enabled: bool | None = None
    flow_id: str = Field(default="", max_length=256)


class OAuthCallback(ServerAction):
    flow_id: str = Field(min_length=1, max_length=256)
    code: str = Field(default="", max_length=8192)
    state: str = Field(default="", max_length=256)
    iss: str | None = Field(default=None, max_length=2048)
    error: str = Field(default="", max_length=256)


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
            if action in {"authenticate", "oauth_callback", "cancel_auth", "sign_out"}:
                body = await parse_body(request, OAuthCallback if action == "oauth_callback" else ServerAction)
                if body is None or current is None or body.version != current["version"]:
                    return error_response(400, "This server changed. Refresh before signing in.")
                await servers.reconcile()
                auth = await servers.oauth_for(current)
                if auth is None:
                    return error_response(400, "Enable OAuth in this server's settings first.")
                if action == "authenticate":
                    if not current["enabled"]:
                        return error_response(400, "Enable this server before signing in.")
                    return JSONResponse(await auth.start())
                if action == "oauth_callback":
                    assert isinstance(body, OAuthCallback)
                    await auth.complete(body.flow_id, code=body.code, state=body.state, iss=body.iss, error=body.error)
                    if auth.status == "authorized":
                        servers.disconnect(identity)
                        await servers.connect(identity, directory, retry=True)
                elif action == "cancel_auth":
                    if body.flow_id != auth.flow_id:
                        return error_response(400, "This sign-in attempt is no longer active.")
                    await auth.cancel()
                else:
                    await auth.sign_out()
                    servers.oauth.pop(identity, None)
                    servers.disconnect(identity)
                return JSONResponse({"auth_status": auth.status})
            if action in {"connect", "refresh"}:
                body = await parse_body(request, ServerAction)
                if body is None or current is None or body.version != current["version"]:
                    return error_response(400, "This server changed. Refresh before connecting.")
                connection = await servers.connect(identity, directory, retry=True)
                if action == "refresh" and connection.phase == "connected":
                    try:
                        await connection.refresh_tools()
                    except Exception as error:
                        connection.failed(error)
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
            oauth = None
            if draft.oauth:
                settings = draft.oauth.model_dump()
                if settings["client_secret"] is None:
                    previous = (current or {}).get("oauth") or {}
                    if any(settings[key] != previous.get(key) for key in ("client_id", "issuer")):
                        return error_response(400, "Enter the client secret again when changing OAuth clients.")
                    settings["client_secret"] = previous.get("client_secret", "")
                oauth = OAuthConfig.model_validate(settings)
            config = ServerConfig(name=draft.name, transport=draft.transport,
                                  command=argv[0] if argv else "", args=argv[1:],
                                  url=draft.url if draft.transport == "http" else "",
                                  env=credentials if key == "env" else {}, headers=credentials if key == "headers" else {},
                                  oauth=oauth, enabled=current["enabled"] if current else True)
            if not identity and not draft.request_id:
                return error_response(400, "New servers require a request_id for safe retry.")
            saved = await asyncio.to_thread(servers.save, config, identity or draft.request_id, draft.version)
            await servers.reconcile()
            if config.enabled:
                await servers.connect(saved, directory, retry=True)
            return JSONResponse({"id": saved}, status_code=200 if identity else 201)
        except ValidationError as error:
            return error_response(400, "; ".join(item["msg"] for item in error.errors(include_input=False)))
        except (OSError, ValueError, sqlite3.Error, AuthenticationRequired) as error:
            return error_response(400, str(error))
