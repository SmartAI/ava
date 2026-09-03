"""The loopback Web UI server.

The server binds ``127.0.0.1``, accepts only requests whose ``Host`` names that address at the bound
port, and rejects cross-origin ``Origin`` headers so another website cannot submit prompts. It owns
an in-memory registry of projects and chats; every chat owns an independent ``Agent`` with its own
durable session, and the event route subscribes directly to that agent's session stream.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

from ava.agent import Agent, CompactionOptions, Status
from ava.app.attach import TEXT_LIMIT, decode_base64, sniff_image, valid_utf8_prefix
from ava.app.web.events import event_json
from ava.base import AvaError, ErrorKind
from ava.llm import (
    ContentBlock,
    Item,
    Role,
    SelectionOverride,
    make_file_text_block,
    make_image_block,
    make_text_block,
    provider_from_environment,
)
from ava.session import Event

DEFAULT_PORT = 8777
ATTACHMENT_COUNT_LIMIT = 10
ATTACHMENT_BYTE_LIMIT = 8 * 1024 * 1024
ATTACHMENT_IMAGE_LIMIT = 10
TITLE_LIMIT = 40

_ASSETS = Path(__file__).parent / "assets"


@cache
def web_asset() -> str:
    page = (_ASSETS / "index.html").read_text(encoding="utf-8")
    page = page.replace(
        "@AVA_KATEX_CSS@", (_ASSETS / "vendor" / "katex.css").read_text(encoding="utf-8")
    )
    return page.replace(
        "@AVA_KATEX_JS@", (_ASSETS / "vendor" / "katex.min.js").read_text(encoding="utf-8")
    )


# ---- Registry ---------------------------------------------------------------------------------


@dataclass(slots=True)
class DriveHandoff:
    """Distinguishes a successful lost wake from input pending after a failure."""

    running: bool = False
    started: bool = False
    last_succeeded: bool = False
    restart_after_current: bool = False

    def acknowledge(self, followed_running_drive: bool, status: Status) -> bool:
        """Start work only when not waiting on an owning drive's result."""
        if status != Status.idle:
            return False
        if self.running:
            self.restart_after_current |= self.started
            return False
        return not followed_running_drive or self.last_succeeded

    def finish(self, succeeded: bool) -> bool:
        """Only a successful drive may compensate for an acknowledgement it narrowly missed."""
        self.running = False
        self.started = False
        self.last_succeeded = succeeded
        restart, self.restart_after_current = self.restart_after_current, False
        return restart and succeeded


@dataclass(slots=True)
class Chat:
    id: str
    agent: Agent
    title: str = ""
    archived: bool = False
    attachment_bytes: int = 0
    image_attachments: int = 0
    drive: DriveHandoff = field(default_factory=DriveHandoff)
    task: asyncio.Task | None = None

    @property
    def status(self) -> str:
        return "running" if self.drive.running or self.agent.status == Status.running else "idle"

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "archived": self.archived,
        }


@dataclass(slots=True)
class Project:
    id: str
    name: str
    path: Path
    chats: list[Chat] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "path": str(self.path),
            "chats": [chat.summary() for chat in self.chats],
        }


class Registry:
    def __init__(self, cwd: Path) -> None:
        self.projects: list[Project] = [
            Project(id="workspace", name=cwd.name or str(cwd), path=cwd)
        ]
        self._next_project = 1
        self._next_chat = 1

    def next_project_id(self) -> str:
        value, self._next_project = self._next_project, self._next_project + 1
        return f"p{value}"

    def next_chat_id(self) -> str:
        value, self._next_chat = self._next_chat, self._next_chat + 1
        return f"c{value}"

    def find_project(self, project_id: str) -> Project | None:
        return next((project for project in self.projects if project.id == project_id), None)

    def find_chat(self, chat_id: str) -> tuple[Project, Chat] | None:
        for project in self.projects:
            for chat in project.chats:
                if chat.id == chat_id:
                    return project, chat
        return None


# ---- Support ---------------------------------------------------------------------------------


def title_from_text(text: str) -> str:
    if not text:
        return "New chat"
    encoded = text.encode("utf-8")
    boundary = valid_utf8_prefix(encoded, TITLE_LIMIT)
    if boundary is None:
        return "New chat"
    if boundary == len(encoded):
        return text
    return encoded[:boundary].decode("utf-8") + "…"


def list_directories(requested: Path) -> dict[str, Any]:
    try:
        path = requested.resolve()
        entries = sorted(
            (entry for entry in path.iterdir() if entry.is_dir()),
            key=lambda entry: entry.name,
        )
    except OSError as error:
        raise AvaError(
            ErrorKind.io, f"cannot read directory '{requested}': {error.strerror or error}"
        ) from error
    parent = str(path.parent) if path.parent != path else ""
    return {
        "path": str(path),
        "parent": parent,
        "entries": [{"name": entry.name, "path": str(entry)} for entry in entries],
    }


@dataclass(slots=True)
class DecodedAttachments:
    blocks: list[ContentBlock]
    decoded_bytes: int = 0
    images: int = 0


def decode_attachments(
    attachments: list[dict[str, Any]], current_bytes: int, current_images: int
) -> DecodedAttachments:
    def reject(message: str) -> AvaError:
        return AvaError(ErrorKind.invalid_argument, message)

    if len(attachments) > ATTACHMENT_COUNT_LIMIT:
        raise reject("messages support at most 10 attachments")
    if current_bytes > ATTACHMENT_BYTE_LIMIT:
        raise reject("chat attachments exceed the 8 MiB lifetime limit")
    if current_images > ATTACHMENT_IMAGE_LIMIT:
        raise reject("chat images exceed the 10-image lifetime limit")
    result = DecodedAttachments(blocks=[])
    for attachment in attachments:
        if not isinstance(attachment, dict):
            raise reject("attachments must be objects")
        name = attachment.get("name")
        kind = attachment.get("kind")
        data_base64 = attachment.get("data_base64")
        if not isinstance(name, str) or not name:
            raise reject("attachment name must be a non-empty JSON string")
        if kind not in ("image", "file"):
            raise reject("attachment kind must be 'image' or 'file'")
        if not isinstance(data_base64, str):
            raise reject("attachment data_base64 must be a JSON string")
        remaining = ATTACHMENT_BYTE_LIMIT - current_bytes - result.decoded_bytes
        if kind == "file" and len(data_base64) > (TEXT_LIMIT + 2) // 3 * 4:
            raise reject("file attachments are limited to 50 KiB of text")
        if len(data_base64) > (remaining + 2) // 3 * 4:
            raise reject("chat attachments exceed the 8 MiB lifetime limit")
        try:
            decoded = decode_base64(data_base64)
        except AvaError as error:
            raise reject(f"attachment '{name}': {error.message}") from None
        if len(decoded) > remaining:
            raise reject("chat attachments exceed the 8 MiB lifetime limit")
        if kind == "image":
            try:
                info = sniff_image(decoded, Path(name).suffix)
            except AvaError as error:
                raise reject(f"image attachment '{name}': {error.message}") from None
            if current_images + result.images == ATTACHMENT_IMAGE_LIMIT:
                raise reject("chat images exceed the 10-image lifetime limit")
            result.blocks.append(make_image_block(name, decoded, info.media_type))
            result.images += 1
            result.decoded_bytes += len(decoded)
        else:
            if len(decoded) > TEXT_LIMIT:
                raise reject("file attachments are limited to 50 KiB of text")
            if valid_utf8_prefix(decoded, len(decoded)) != len(decoded):
                raise reject(f"file attachment '{name}' is not valid UTF-8")
            result.blocks.append(make_file_text_block(name, decoded.decode("utf-8")))
            result.decoded_bytes += len(decoded)
    return result


# ---- Application -----------------------------------------------------------------------------


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse(
        {"error": message}, status_code=status, headers={"cache-control": "no-store"}
    )


def create_app(
    cwd: Path, options: CompactionOptions | None = None, selection: SelectionOverride | None = None
) -> FastAPI:
    cwd = cwd.resolve()
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    registry = Registry(cwd)
    app.state.registry = registry
    app.state.bound_port = 0
    compaction = options or CompactionOptions()
    override = selection or SelectionOverride()

    def allowed(request: Request) -> bool:
        host = request.headers.get("host")
        if host is None:
            return False
        port = app.state.bound_port
        default_port = port == 80 and host in ("127.0.0.1", "localhost")
        if not default_port and host not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            return False
        origin = request.headers.get("origin")
        return origin is None or origin == f"http://{host}"

    @app.middleware("http")
    async def fence(request: Request, call_next):  # type: ignore[no-untyped-def]
        if not allowed(request):
            return _error(403, "forbidden request")
        return await call_next(request)

    @app.get("/")
    async def index() -> Response:
        return HTMLResponse(web_asset())

    @app.get("/favicon.ico")
    async def favicon() -> Response:
        return Response(status_code=204)

    @app.get("/api/fs")
    async def browse(path: str = "") -> Response:
        if not path:
            home = os.environ.get("HOME")
            if not home:
                return _error(500, "cannot determine the user home directory")
            path = home
        try:
            return JSONResponse(list_directories(Path(path)))
        except AvaError as error:
            return _error(400, error.message)

    @app.get("/api/projects")
    async def projects() -> Response:
        return JSONResponse({"projects": [project.summary() for project in registry.projects]})

    @app.post("/api/projects")
    async def add_project(request: Request) -> Response:
        body = await _json_body(request)
        path_value = body.get("path") if isinstance(body, dict) else None
        if not isinstance(path_value, str) or not path_value:
            return _error(400, "path must be a non-empty JSON string")
        path = Path(path_value).resolve()
        if not path.is_dir():
            return _error(400, "path must name an existing directory")
        existing = next((project for project in registry.projects if project.path == path), None)
        if existing is not None:
            return JSONResponse(existing.summary())
        project = Project(id=registry.next_project_id(), name=path.name or str(path), path=path)
        registry.projects.append(project)
        return JSONResponse(project.summary(), status_code=201)

    @app.post("/api/chats")
    async def create_chat(request: Request) -> Response:
        body = await _json_body(request)
        project_id = body.get("project_id") if isinstance(body, dict) else None
        if not isinstance(project_id, str) or not project_id:
            return _error(400, "project_id must be a non-empty JSON string")
        project = registry.find_project(project_id)
        if project is None:
            return _error(404, "no such project")
        try:
            provider = provider_from_environment(override)
            agent = Agent.create(provider, project.path, compaction)
        except AvaError as error:
            return _error(503, error.message)
        chat = Chat(id=registry.next_chat_id(), agent=agent)
        project.chats.append(chat)
        return JSONResponse(chat.summary(), status_code=201)

    @app.get("/api/chats/{chat_id}")
    async def open_chat(chat_id: str) -> Response:
        found = registry.find_chat(chat_id)
        if found is None:
            return _error(404, "no such chat")
        project, chat = found
        return JSONResponse(
            {
                "id": chat.id,
                "project_id": project.id,
                "title": chat.title,
                "cwd": str(project.path),
                "status": chat.status,
                "archived": chat.archived,
                "events": [],
            }
        )

    @app.post("/api/chats/{chat_id}/archive")
    async def archive_chat(chat_id: str, request: Request) -> Response:
        found = registry.find_chat(chat_id)
        if found is None:
            return _error(404, "no such chat")
        body = await _json_body(request)
        archived = body.get("archived") if isinstance(body, dict) else None
        if not isinstance(archived, bool):
            return _error(400, "archived must be a JSON boolean")
        found[1].archived = archived
        return JSONResponse(found[1].summary())

    @app.post("/api/chats/{chat_id}/messages")
    async def post_message(chat_id: str, request: Request) -> Response:
        found = registry.find_chat(chat_id)
        if found is None:
            return _error(404, "no such chat")
        _, chat = found
        if chat.archived:
            return _error(409, "chat is archived")
        body = await _json_body(request)
        if not isinstance(body, dict):
            return _error(400, "message must be valid JSON")
        text = body.get("text", "")
        attachments = body.get("attachments", [])
        delivery = body.get("delivery", "followup")
        if not isinstance(text, str) or not isinstance(attachments, list):
            return _error(400, "message must be valid JSON")
        if not text and not attachments:
            return _error(400, "message must contain non-empty text or an attachment")
        if delivery not in ("followup", "steer"):
            return _error(400, "delivery must be 'steer' or 'followup'")
        try:
            decoded = decode_attachments(attachments, chat.attachment_bytes, chat.image_attachments)
        except AvaError as error:
            return _error(400, error.message)
        title = title_from_text(text or attachments[0].get("name", "")) if not chat.title else ""
        item = Item(role=Role.user, blocks=list(decoded.blocks))
        if text:
            item.blocks.append(make_text_block(text))
        chat.attachment_bytes += decoded.decoded_bytes
        chat.image_attachments += decoded.images
        agent = chat.agent
        steering = delivery == "steer" and agent.turn_open
        followed_running_drive = chat.drive.running
        try:
            if steering:
                await agent.steer(item)
            else:
                await agent.followup(item)
        except AvaError as error:
            chat.attachment_bytes -= decoded.decoded_bytes
            chat.image_attachments -= decoded.images
            return _error(400 if error.kind == ErrorKind.invalid_argument else 503, error.message)
        if not chat.title:
            chat.title = title
        if chat.drive.acknowledge(followed_running_drive, agent.status):
            _begin_drive(chat)
        return JSONResponse({"accepted": True, "chat": chat.summary()}, status_code=202)

    @app.get("/api/chats/{chat_id}/events")
    async def events(chat_id: str, request: Request) -> Response:
        found = registry.find_chat(chat_id)
        if found is None:
            return _error(404, "not found")
        last = _last_event_id(request.headers.get("last-event-id"))
        return StreamingResponse(
            _event_stream(found[1].agent, last),
            media_type="text/event-stream",
            headers={"cache-control": "no-store"},
        )

    return app


async def _json_body(request: Request) -> Any:
    try:
        return json.loads(await request.body() or b"null")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def _last_event_id(value: str | None) -> int | None:
    if value is None or not value.isdigit():
        return None
    return int(value)


def _begin_drive(chat: Chat) -> None:
    """One driver per chat; a follow-up acknowledged mid-drive is picked up at the next boundary."""
    chat.drive.running = True
    chat.drive.started = False
    agent = chat.agent

    async def run_drives() -> None:
        while True:
            chat.drive.started = True
            try:
                await agent.drive()
                succeeded = True
            except AvaError:
                succeeded = False
            if not chat.drive.finish(succeeded):
                return
            chat.drive.running = True

    chat.task = asyncio.create_task(run_drives())


async def _event_stream(agent: Agent, last: int | None) -> AsyncIterator[bytes]:
    queue: asyncio.Queue[Event] = asyncio.Queue()

    def emit(event: Event) -> None:
        if last is not None and event.seq <= last:
            return
        queue.put_nowait(event)

    subscription = agent.subscribe(emit)
    try:
        while True:
            event = await queue.get()
            try:
                encoded = event_json(event)
            except AvaError:
                return
            yield f"id: {event.seq}\ndata: {encoded}\n\n".encode()
    finally:
        subscription.close()


# ---- Serving ---------------------------------------------------------------------------------


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
    """A uvicorn server bound to an already-listening loopback socket."""
    import uvicorn

    app.state.bound_port = sock.getsockname()[1]
    config = uvicorn.Config(app, log_level="warning", lifespan="off", access_log=False)
    return uvicorn.Server(config)


async def serve(app: FastAPI, sock: socket.socket) -> None:
    await create_server(app, sock).serve(sockets=[sock])
