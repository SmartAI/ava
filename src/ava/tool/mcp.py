"""Durable MCP configuration and backend-owned connections scoped to a working directory."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import sqlite3
import time
from contextlib import AsyncExitStack, closing
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ava.base import CancelToken
from ava.base.images import IMAGE_BYTE_LIMIT, sniff_image
from ava.llm.types import ContentBlock, ToolDef, make_image_block
from ava.tool.api import Output, Tool, parse_arguments

IDLE_SECONDS = 300.0


class ServerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(min_length=1, max_length=80)
    transport: Literal["stdio", "http"] = "stdio"
    command: str = ""
    args: list[str] = Field(default_factory=list, max_length=128)
    url: str = ""
    env: dict[str, str] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True

    @model_validator(mode="after")
    def valid_transport(self):
        self.name = self.name.strip()
        if not self.name:
            raise ValueError("A server name is required.")
        if self.transport == "stdio":
            if not self.command.strip() or "\0" in self.command or self.url or self.headers:
                raise ValueError("Provide a program and arguments for a stdio server.")
        else:
            parsed = urlsplit(self.url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
                raise ValueError("Provide an HTTP(S) MCP endpoint without embedded credentials.")
            if self.command or self.args or self.env:
                raise ValueError("HTTP servers use a URL and optional headers, not a program.")
        if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) for key in self.env):
            raise ValueError("Environment variable names must be valid identifiers.")
        if any(not key or any(c in key + value for c in "\r\n\0") for key, value in self.headers.items()):
            raise ValueError("Headers must have valid names and single-line values.")
        if len(json.dumps(self.model_dump()).encode()) > 64 * 1024:
            raise ValueError("Server configuration exceeds 64 KiB.")
        return self


def tool_name(server: str, name: str) -> str:
    readable = re.sub(r"[^a-zA-Z0-9_-]", "_", name)[:35]
    digest = hashlib.sha256(name.encode()).hexdigest()[:6]
    return f"mcp_{server[:8]}_{readable}_{digest}"


def _error(error: BaseException, config: dict) -> str:
    while isinstance(error, BaseExceptionGroup) and error.exceptions:
        error = error.exceptions[0]
    if isinstance(error, FileNotFoundError):
        return "The program could not be found. Check the command and install it on the selected machine before retrying."
    if isinstance(error, PermissionError):
        return "The program could not be started. Check its execution permission on the selected machine."
    message = str(error) or type(error).__name__
    for secret in [*config.get("env", {}).values(), *config.get("headers", {}).values()]:
        if secret:
            message = message.replace(secret, "[redacted]")
    return message[:1000]


class MCPConnection:
    def __init__(self, owner: MCPServers, config: dict, cwd: Path) -> None:
        self.owner, self.config, self.cwd = owner, config, cwd
        self.ready = asyncio.Event()
        self.stop = asyncio.Event()
        self.wake = asyncio.Event()
        self.catalog_lock = asyncio.Lock()
        self.task: asyncio.Task | None = None
        self.client: Any = None
        self.tools: list[dict] = []
        self.info: dict = {}
        self.phase = "idle"
        self.error = ""
        self.active = 0
        self.retired = False
        self.touched = time.monotonic()

    def changed(self, phase: str, error: str = "") -> None:
        self.phase, self.error = phase, error
        self.owner.generation += 1

    async def open(self) -> None:
        self.touched = time.monotonic()
        if self.task is None:
            self.changed("connecting")
            self.task = asyncio.create_task(self._serve(), name=f"mcp-{self.config['id']}")
        await self.ready.wait()

    async def _serve(self) -> None:
        # The owner task enters and exits every SDK context. Request handlers never own it.
        try:
            from mcp import Client, StdioServerParameters, stdio_client
            from mcp.types import Implementation, ListRootsResult, Root, ToolListChangedNotification

            async with AsyncExitStack() as stack:
                if self.config["transport"] == "stdio":
                    params = StdioServerParameters(command=self.config["command"], args=self.config["args"],
                                                   env=self.config["env"], cwd=self.cwd)
                    stderr = stack.enter_context(open(os.devnull, "w"))
                    transport = stdio_client(params, errlog=stderr)
                else:
                    import httpx2
                    from mcp.client.streamable_http import streamable_http_client

                    async def check_auth(response):
                        if response.status_code in {401, 403}:
                            raise ValueError(f"HTTP {response.status_code}: Access was denied. Check this server's authentication headers and permissions.")

                    http = await stack.enter_async_context(httpx2.AsyncClient(headers=self.config["headers"],
                                                                          timeout=httpx2.Timeout(15, read=120),
                                                                          event_hooks={"response": [check_auth]}))
                    transport = streamable_http_client(self.config["url"], http_client=http)

                async def roots(_context):
                    return ListRootsResult(roots=[Root.model_validate({"uri": self.cwd.as_uri(), "name": self.cwd.name})])

                async def notification(message):
                    # Never perform protocol I/O in the dispatcher's notification callback.
                    if isinstance(message, ToolListChangedNotification):
                        self.wake.set()
                    elif isinstance(message, Exception):
                        self.changed("error", _error(message, self.config))
                        self.stop.set()
                        self.wake.set()

                client = Client(transport, list_roots_callback=roots,
                                message_handler=notification,
                                client_info=Implementation(name="ava", version="0.1.0"), read_timeout_seconds=15)
                async with asyncio.timeout(15):
                    self.client = await stack.enter_async_context(client)
                    if client.protocol_version >= "2026-07-28":
                        subscribed = asyncio.Event()
                        watcher = asyncio.create_task(self._watch_tools(subscribed))

                        async def stop_watcher():
                            watcher.cancel()
                            await asyncio.gather(watcher, return_exceptions=True)

                        stack.push_async_callback(stop_watcher)
                        await subscribed.wait()
                    await self.refresh_tools()
                    self.info = client.server_info.model_dump(mode="json") if client.server_info else {}
                self.changed("connected")
                self.touched = time.monotonic()
                self.ready.set()
                while not self.stop.is_set():
                    remaining = IDLE_SECONDS - (time.monotonic() - self.touched)
                    if not self.active and remaining <= 0:
                        self.changed("idle")
                        break
                    try:
                        async with asyncio.timeout(30 if self.active else max(0.01, min(30, remaining))):
                            await self.wake.wait()
                    except TimeoutError:
                        if client.protocol_version < "2026-07-28" and (self.active or time.monotonic() - self.touched < IDLE_SECONDS):
                            await client.session.send_ping()
                        continue
                    self.wake.clear()
                    if not self.stop.is_set():
                        await self.refresh_tools()
        except asyncio.CancelledError:
            self.changed("disconnected")
        except Exception as error:
            self.changed("error", _error(error, self.config))
        finally:
            self.client = None
            self.ready.set()
            if self.phase == "connected":
                self.changed("disconnected")

    async def _watch_tools(self, subscribed: asyncio.Event) -> None:
        from mcp.shared.exceptions import MCPError
        from mcp.types import METHOD_NOT_FOUND

        while not self.stop.is_set():
            try:
                async with self.client.listen(tools_list_changed=True) as stream:
                    if subscribed.is_set():
                        self.wake.set()  # Reconnecting subscriptions do not replay missed events.
                    subscribed.set()
                    async for _event in stream:
                        self.wake.set()
            except Exception as error:
                self.error = "Live tool updates unavailable: " + _error(error, self.config)
                self.owner.generation += 1
                if isinstance(error, MCPError) and error.code == METHOD_NOT_FOUND:
                    return
            finally:
                subscribed.set()
            await asyncio.sleep(1)

    async def refresh_tools(self) -> None:
        async with self.catalog_lock, asyncio.timeout(15):
            if self.client is None or self.retired:
                return
            tools: list[dict] = []
            cursor = None
            cursors: set[str] = set()
            names: set[str] = set()
            size = 0
            while True:
                result = await self.client.list_tools(cursor=cursor, cache_mode="refresh")
                for tool in result.tools:
                    if tool.name in names:
                        raise ValueError("The server advertised duplicate tool names.")
                    names.add(tool.name)
                    value = tool.model_dump(mode="json", by_alias=False)
                    size += len(json.dumps(value).encode())
                    tools.append(value)
                    if len(tools) > 2048 or size > 4 * 1024 * 1024:
                        raise ValueError("The server's catalog exceeds 2,048 tools or 4 MiB of tool definitions.")
                cursor = result.next_cursor
                if cursor is None:
                    break
                if cursor in cursors:
                    raise ValueError("The server repeated its tools pagination cursor.")
                cursors.add(cursor)
            # Publish the complete catalog atomically; incomplete pages never reach a model.
            if tools != self.tools or self.error:
                self.tools, self.error = tools, ""
                self.owner.generation += 1

    def retire(self) -> None:
        self.retired = True
        if not self.active:
            self.stop.set()
            self.wake.set()

    async def call(self, name: str, arguments: str, cancel: CancelToken) -> Output:
        args = parse_arguments(arguments)
        if isinstance(args, str):
            return Output(args, True)
        if self.retired or self.client is None or self.phase != "connected":
            return Output("The MCP server is no longer connected. Refresh its tools before retrying.", True)
        self.active += 1
        self.touched = time.monotonic()
        self.owner.generation += 1
        try:
            result = await cancel.guard(self.client.call_tool(name, args, read_timeout_seconds=120))
            parts: list[str] = []
            unsupported: list[str] = []
            images: list[ContentBlock] = []
            image_bytes = 0
            for content in result.content:
                if content.type == "text":
                    parts.append(content.text)
                elif content.type == "resource_link":
                    parts.append(f"{content.name}: {content.uri}")
                elif content.type == "resource" and hasattr(content.resource, "text"):
                    parts.append(f"{content.resource.uri}\n{content.resource.text}")
                elif content.type == "image":
                    extension = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp"}.get(content.mime_type)
                    if extension is None:
                        raise ValueError("Tool images must be PNG, JPEG, GIF or WebP.")
                    if len(images) >= 4 or len(content.data) > ((IMAGE_BYTE_LIMIT - image_bytes + 2) // 3) * 4:
                        raise ValueError("Tool output supports at most 4 images and 7.5 MB of image data per call.")
                    data = base64.b64decode(content.data, validate=True)
                    image_bytes += len(data)
                    if image_bytes > IMAGE_BYTE_LIMIT:
                        raise ValueError("Tool images exceed the 7.5 MB limit for one call.")
                    info = sniff_image(data, extension)
                    images.append(make_image_block(f"{name}-{len(images) + 1}{extension}", data, info.media_type))
                else:
                    unsupported.append(content.type)
            if not parts and result.structured_content is not None:
                parts.append(json.dumps(result.structured_content, ensure_ascii=False))
            if unsupported:
                parts.append("Media content is not yet supported by this tool-result path: " + ", ".join(unsupported))
            text = "\n\n".join(parts)
            if len(text) > 65536:
                text = text[:65536] + "\n[MCP output truncated at 65,536 characters.]"
            return Output(text or ("Tool returned images." if images else "Tool completed without content."),
                          bool(result.is_error or unsupported), attachments=images)
        except Exception as error:
            cancel.raise_if_cancelled()
            message = _error(error, self.config)
            self.error = message
            return Output("MCP tool failed: " + message, True)
        finally:
            self.active -= 1
            self.touched = time.monotonic()
            self.owner.generation += 1
            if self.retired and not self.active:
                self.stop.set()
                self.wake.set()


class MCPServers:
    def __init__(self, home: Path) -> None:
        self.home = home
        self.connections: dict[tuple[str, str], MCPConnection] = {}
        self.retiring: set[MCPConnection] = set()
        self.generation = 0

    def configs(self) -> list[dict]:
        path = self.home / "capabilities.sqlite3"
        if not path.exists():
            return []
        with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='mcp_servers'").fetchone():
                return []
            return [{**json.loads(row[2]), "id": row[0], "version": row[1]}
                    for row in db.execute("SELECT id, version, config FROM mcp_servers ORDER BY rowid")]

    def save(self, config: ServerConfig, identity: str = "", version: int = 0) -> str:
        self.home.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self.home / "capabilities.sqlite3"
        with closing(sqlite3.connect(path, timeout=5)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS mcp_servers (id TEXT PRIMARY KEY, version INTEGER NOT NULL, config TEXT NOT NULL)")
            if identity and version:
                changed = db.execute("UPDATE mcp_servers SET config=?,version=version+1 WHERE id=? AND version=?",
                                     (config.model_dump_json(), identity, version)).rowcount
                if changed != 1:
                    raise ValueError("This server changed in another client. Refresh before saving.")
            else:
                identity = identity or uuid4().hex
                existing = db.execute("SELECT config FROM mcp_servers WHERE id=?", (identity,)).fetchone()
                if existing is not None:
                    if existing[0] != config.model_dump_json():
                        raise ValueError("This request already created a different MCP server.")
                else:
                    db.execute("INSERT INTO mcp_servers VALUES (?,1,?)", (identity, config.model_dump_json()))
        path.chmod(0o600)
        return identity

    def remove(self, identity: str, version: int) -> None:
        with closing(sqlite3.connect(self.home / "capabilities.sqlite3", timeout=5)) as db, db:
            if db.execute("DELETE FROM mcp_servers WHERE id=? AND version=?", (identity, version)).rowcount != 1:
                raise ValueError("This server changed in another client. Refresh before removing it.")

    async def reconcile(self) -> list[dict]:
        configs = await asyncio.to_thread(self.configs)
        active = {config["id"]: config for config in configs if config["enabled"]}
        for key, connection in list(self.connections.items()):
            if key[0] not in active or active[key[0]]["version"] != connection.config["version"]:
                connection.retire()
                self.retiring.add(connection)
                del self.connections[key]
        self.retiring = {c for c in self.retiring if c.task and not c.task.done()}
        return configs

    async def connect(self, identity: str, cwd: Path, *, retry: bool = False) -> MCPConnection:
        configs = await self.reconcile()
        config = next((c for c in configs if c["id"] == identity and c["enabled"]), None)
        if config is None:
            raise ValueError("Enable this server before connecting.")
        key = (identity, str(cwd))
        connection = self.connections.get(key)
        if connection is not None and (connection.phase == "idle" or retry and connection.phase in {"error", "disconnected"}):
            connection.retire()
            self.retiring.add(connection)
            connection = None
        if connection is None:
            connection = MCPConnection(self, config, cwd)
            self.connections[key] = connection
        await connection.open()
        return connection

    async def tools(self, cwd: Path, cancel: CancelToken) -> list[Tool]:
        configs = await self.reconcile()
        result: list[Tool] = []
        semaphore = asyncio.Semaphore(4)

        async def open_config(config):
            async with semaphore:
                return await self.connect(config["id"], cwd)

        connections = await cancel.guard(asyncio.gather(*(open_config(c) for c in configs if c["enabled"])))
        for connection in connections:
            if connection.phase != "connected":
                continue
            for schema in connection.tools:
                async def run(arguments, token, connection=connection, name=schema["name"]):
                    # UI edits may retire a connection while a model request is running.
                    configs = await self.reconcile()
                    if not any(c["id"] == connection.config["id"] and c["version"] == connection.config["version"] and c["enabled"] for c in configs):
                        return Output("This MCP server was disabled or changed. Refresh its tools before retrying.", True)
                    current = await self.connect(connection.config["id"], cwd)
                    return await current.call(name, arguments, token)

                result.append(Tool(ToolDef(name=tool_name(connection.config["id"], schema["name"]),
                                          description=f"{connection.config['name']}: {schema.get('description') or schema['name']}",
                                          display_name=f"{connection.config['name']} · {schema.get('title') or schema['name'].replace('_', ' ')}",
                                          input_schema=schema["input_schema"]), run))
        return result

    async def snapshot(self, cwd: Path) -> list[dict]:
        rows = []
        for config in await self.reconcile():
            connection = self.connections.get((config["id"], str(cwd)))
            finishing = sum(c.active for c in self.retiring if c.config["id"] == config["id"] and c.cwd == cwd)
            rows.append({**{key: value for key, value in config.items() if key not in {"env", "headers"}},
                         "env_names": list(config["env"]), "header_names": list(config["headers"]),
                         "status": "disabled" if not config["enabled"] else connection.phase if connection else "idle",
                         "error": connection.error if connection else "", "active_calls": finishing + (connection.active if connection else 0),
                         "tools": connection.tools if connection else [], "info": connection.info if connection else {}})
        return rows

    async def aclose(self) -> None:
        connections = {*self.connections.values(), *self.retiring}
        for connection in connections:
            connection.retire()
            connection.stop.set()
            connection.wake.set()
        tasks = [connection.task for connection in connections if connection.task is not None]
        if tasks:
            try:
                async with asyncio.timeout(10):
                    await asyncio.gather(*tasks, return_exceptions=True)
            except TimeoutError:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        self.connections.clear()
        self.retiring.clear()
