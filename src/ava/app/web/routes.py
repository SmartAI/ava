"""HTTP routes and boundary validation for the loopback Web UI."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

from ava.agent import Agent, CancelCause, CompactNowOutcome, Status
from ava.agent.prompt import discover_skills
from ava.app.attach import TEXT_LIMIT, decode_base64, sniff_image, valid_utf8_prefix
from ava.base import AvaError, ErrorKind
from ava.llm import (
    AuthRequirement,
    ContentBlock,
    Item,
    Role,
    make_file_text_block,
    make_image_block,
    make_text_block,
)
from ava.llm.credentials import delete_api_key, save_api_key

from .models import (
    AddProjectBody,
    ArchiveBody,
    CancelBody,
    CreateChatBody,
    CredentialsBody,
    MessageBody,
    SelectionBody,
    parse_body,
)
from .registry import Chat, Project, WebState
from .streaming import begin_drive, event_stream

ATTACHMENT_COUNT_LIMIT = 10
ATTACHMENT_BYTE_LIMIT = 8 * 1024 * 1024
ATTACHMENT_IMAGE_LIMIT = 10
TITLE_LIMIT = 40
COMPACTION_MESSAGES = {
    CompactNowOutcome.compacted: "compacted the conversation; the model now sees a summary plus the recent tail",
    CompactNowOutcome.nothing_to_compact: "nothing to compact yet",
    CompactNowOutcome.failed: "compaction failed; the conversation is unchanged",
    CompactNowOutcome.disabled: "compaction is disabled for this run",
}


def error_response(status: int, message: str) -> JSONResponse:
    return JSONResponse(
        {"error": message}, status_code=status, headers={"cache-control": "no-store"}
    )


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
            (entry for entry in path.iterdir() if entry.is_dir()), key=lambda entry: entry.name
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
        else:
            if len(decoded) > TEXT_LIMIT:
                raise reject("file attachments are limited to 50 KiB of text")
            if valid_utf8_prefix(decoded, len(decoded)) != len(decoded):
                raise reject(f"file attachment '{name}' is not valid UTF-8")
            result.blocks.append(make_file_text_block(name, decoded.decode("utf-8")))
        result.decoded_bytes += len(decoded)
    return result


def last_event_id(value: str | None) -> int | None:
    return int(value) if value is not None and value.isdigit() else None


def register_routes(app: FastAPI, state: WebState, index_html: Callable[[], str]) -> None:
    """Attach routes to ``app`` while keeping construction and lifecycle in ``server``."""
    registry = state.registry

    @app.get("/")
    async def index() -> Response:
        return HTMLResponse(index_html())

    @app.get("/favicon.ico")
    async def favicon() -> Response:
        return Response(status_code=204)

    @app.get("/api/fs")
    async def browse(path: str = "") -> Response:
        path = path or os.environ.get("HOME", "")
        if not path:
            return error_response(500, "cannot determine the user home directory")
        try:
            return JSONResponse(list_directories(Path(path)))
        except AvaError as error:
            return error_response(400, error.message)

    @app.get("/api/projects")
    async def projects() -> Response:
        return JSONResponse({"projects": [project.summary() for project in registry.projects]})

    @app.post("/api/projects")
    async def add_project(request: Request) -> Response:
        body = await parse_body(request, AddProjectBody)
        if body is None:
            return error_response(400, "path must be a non-empty JSON string")
        path = Path(body.path).resolve()
        if not path.is_dir():
            return error_response(400, "path must name an existing directory")
        existing = next((project for project in registry.projects if project.path == path), None)
        if existing is not None:
            return JSONResponse(existing.summary())
        project = Project(id=registry.next_project_id(), name=path.name or str(path), path=path)
        registry.projects.append(project)
        return JSONResponse(project.summary(), status_code=201)

    @app.post("/api/chats")
    async def create_chat(request: Request) -> Response:
        body = await parse_body(request, CreateChatBody)
        if body is None:
            return error_response(400, "project_id must be a non-empty JSON string")
        project = registry.find_project(body.project_id)
        if project is None:
            return error_response(404, "no such project")
        try:
            agent = Agent.create(state.provider_factory(state.selection), project.path, state.compaction)
        except AvaError as error:
            return error_response(503, error.message)
        chat = Chat(id=registry.next_chat_id(), agent=agent)
        project.chats.append(chat)
        return JSONResponse(chat.summary(), status_code=201)

    @app.get("/api/chats/{chat_id}")
    async def open_chat(chat_id: str) -> Response:
        found = registry.find_chat(chat_id)
        if found is None:
            return error_response(404, "no such chat")
        project, chat = found
        return JSONResponse(
            {
                "id": chat.id,
                "project_id": project.id,
                "title": chat.title,
                "cwd": str(project.path),
                "status": chat.status,
                "turn_open": chat.agent.turn_open,
                "archived": chat.archived,
                "events": [],
            }
        )

    @app.post("/api/chats/{chat_id}/archive")
    async def archive_chat(chat_id: str, request: Request) -> Response:
        found = registry.find_chat(chat_id)
        if found is None:
            return error_response(404, "no such chat")
        body = await parse_body(request, ArchiveBody)
        if body is None:
            return error_response(400, "archived must be a JSON boolean")
        found[1].archived = body.archived
        return JSONResponse(found[1].summary())

    @app.post("/api/chats/{chat_id}/messages")
    async def post_message(chat_id: str, request: Request) -> Response:
        found = registry.find_chat(chat_id)
        if found is None:
            return error_response(404, "no such chat")
        chat = found[1]
        if chat.archived:
            return error_response(409, "chat is archived")
        body = await parse_body(request, MessageBody)
        if body is None:
            return error_response(400, "message must be valid JSON")
        text = body.text
        attachments = body.attachments
        delivery = body.delivery
        if not text and not attachments:
            return error_response(400, "message must contain non-empty text or an attachment")
        if delivery not in ("followup", "steer"):
            return error_response(400, "delivery must be 'steer' or 'followup'")
        try:
            decoded = decode_attachments(attachments, chat.attachment_bytes, chat.image_attachments)
        except AvaError as error:
            return error_response(400, error.message)
        title = title_from_text(text or attachments[0].get("name", "")) if not chat.title else ""
        item = Item(role=Role.user, blocks=list(decoded.blocks))
        if text:
            item.blocks.append(make_text_block(text))
        chat.attachment_bytes += decoded.decoded_bytes
        chat.image_attachments += decoded.images
        agent = chat.agent
        steering = delivery == "steer" and agent.status in (
            Status.running,
            Status.pausing,
            Status.paused,
        )
        followed_running_drive = chat.drive.running
        try:
            await (agent.steer(item) if steering else agent.followup(item))
        except AvaError as error:
            chat.attachment_bytes -= decoded.decoded_bytes
            chat.image_attachments -= decoded.images
            status = 400 if error.kind == ErrorKind.invalid_argument else 503
            return error_response(status, error.message)
        if not chat.title:
            chat.title = title
        if chat.drive.acknowledge(followed_running_drive, agent.status):
            begin_drive(chat)
        return JSONResponse({"accepted": True, "chat": chat.summary()}, status_code=202)

    @app.post("/api/chats/{chat_id}/cancel")
    async def cancel(chat_id: str, request: Request) -> Response:
        found = registry.find_chat(chat_id)
        if found is None:
            return error_response(404, "no such chat")
        body = await parse_body(request, CancelBody)
        if body is None:
            return error_response(400, "cause must be 'pause' or 'abort'")
        chat = found[1]
        cause = CancelCause.user_pause if body.cause == "pause" else CancelCause.user_abort
        chat.agent.cancel(cause)
        return JSONResponse(chat.summary())

    @app.post("/api/chats/{chat_id}/resume")
    async def resume(chat_id: str) -> Response:
        found = registry.find_chat(chat_id)
        if found is None:
            return error_response(404, "no such chat")
        chat = found[1]
        if chat.agent.status != Status.paused:
            return error_response(409, "chat is not paused")
        chat.agent.resume()
        if chat.drive.acknowledge(False, chat.agent.status):
            begin_drive(chat)
        return JSONResponse(chat.summary(), status_code=202)

    @app.get("/api/chats/{chat_id}/models")
    async def models(chat_id: str) -> Response:
        found = registry.find_chat(chat_id)
        if found is None:
            return error_response(404, "no such chat")
        agent = found[1].agent
        choices = await agent.model_choices()
        selection = agent.current_selection()
        capabilities = agent.current_capabilities()
        return JSONResponse(
            {
                "provider": selection.provider,
                "model": selection.model,
                "effort": selection.effort,
                "effort_values": capabilities.effort_values,
                "models": choices.models,
                "catalog_available": choices.provider_catalog_available,
            }
        )

    @app.post("/api/chats/{chat_id}/model")
    async def select_model(chat_id: str, request: Request) -> Response:
        found = registry.find_chat(chat_id)
        if found is None:
            return error_response(404, "no such chat")
        body = await parse_body(request, SelectionBody)
        if body is None:
            return error_response(400, "body must be a JSON object")
        agent = found[1].agent
        try:
            if "model" in body.model_fields_set:
                if body.model is None:
                    return error_response(400, "model must be a JSON string")
                agent.select_model(body.model)
            if "effort" in body.model_fields_set:
                agent.select_effort(body.effort)
        except AvaError as error:
            return error_response(400, error.message)
        selection = agent.current_selection()
        return JSONResponse(
            {"provider": selection.provider, "model": selection.model, "effort": selection.effort}
        )

    @app.post("/api/chats/{chat_id}/compact")
    async def compact(chat_id: str) -> Response:
        found = registry.find_chat(chat_id)
        if found is None:
            return error_response(404, "no such chat")
        try:
            outcome = await found[1].agent.compact_now()
        except AvaError as error:
            return error_response(409 if error.recoverable else 503, error.message)
        return JSONResponse({"outcome": outcome.value, "message": COMPACTION_MESSAGES[outcome]})

    @app.get("/api/chats/{chat_id}/skills")
    async def skills(chat_id: str) -> Response:
        found = registry.find_chat(chat_id)
        if found is None:
            return error_response(404, "no such chat")
        catalog = discover_skills(found[0].path)
        return JSONResponse(
            {
                "skills": [
                    {
                        "name": skill.name,
                        "description": skill.description,
                        "scope": skill.scope,
                        "path": str(skill.path),
                    }
                    for skill in catalog
                ]
            }
        )

    async def reload_provider(provider: str, requirement: AuthRequirement) -> dict[str, Any]:
        reloaded: list[str] = []
        failed: dict[str, str] = {}
        for project in registry.projects:
            for chat in project.chats:
                if chat.agent.provider_id != provider:
                    continue
                try:
                    await chat.agent.reload_credentials(requirement)
                    reloaded.append(chat.id)
                except AvaError as error:
                    failed[chat.id] = error.message
        return {"provider": provider, "reloaded": reloaded, "failed": failed}

    @app.post("/api/credentials")
    async def login(request: Request) -> Response:
        body = await parse_body(request, CredentialsBody)
        if body is None:
            return error_response(400, "provider and key must be non-empty JSON strings")
        if body.provider == "codex":
            return error_response(
                400, "the codex provider reuses the Codex CLI login; run 'codex login' instead"
            )
        try:
            save_api_key(body.provider, body.key)
        except AvaError as error:
            return error_response(503, error.message)
        return JSONResponse(await reload_provider(body.provider, AuthRequirement.required))

    @app.delete("/api/credentials/{provider}")
    async def logout(provider: str) -> Response:
        if provider == "codex":
            return error_response(
                400, "the codex provider reuses the Codex CLI login; run 'codex logout' instead"
            )
        try:
            delete_api_key(provider)
        except AvaError as error:
            return error_response(503, error.message)
        return JSONResponse(await reload_provider(provider, AuthRequirement.allow_missing))

    @app.get("/api/chats/{chat_id}/context")
    async def context(chat_id: str) -> Response:
        found = registry.find_chat(chat_id)
        if found is None:
            return error_response(404, "no such chat")
        return JSONResponse(asdict(found[1].agent.context_report(prepare=True)))

    @app.get("/api/chats/{chat_id}/events")
    async def events(chat_id: str, request: Request) -> Response:
        found = registry.find_chat(chat_id)
        if found is None:
            return error_response(404, "not found")
        return StreamingResponse(
            event_stream(found[1], last_event_id(request.headers.get("last-event-id"))),
            media_type="text/event-stream",
            headers={"cache-control": "no-store"},
        )
