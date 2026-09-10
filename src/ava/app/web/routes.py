"""HTTP routes and boundary validation for the loopback Web UI."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse

from ava.agent import CancelCause, CompactNowOutcome, Status
from ava.agent.prompt import discover_skills
from ava.app.attach import TEXT_LIMIT, decode_base64, sniff_image, valid_utf8_prefix
from ava.base import AvaError, ErrorKind
from ava.base.images import IMAGE_BYTE_LIMIT
from ava.llm import (
    AuthRequirement,
    ContentBlock,
    Item,
    Role,
    make_file_text_block,
    make_image_block,
    make_text_block,
)
from ava.llm.configuration import save_provider_connection
from ava.llm.credentials import delete_api_key, save_api_key
from ava.llm.provider import Selection

from . import worktrees
from .models import (
    AddProjectBody,
    ArchiveBody,
    CancelBody,
    CreateChatBody,
    CredentialsBody,
    MessageBody,
    RenameChatBody,
    ReviewBody,
    ReviseMessageBody,
    SelectionBody,
    SettingsBody,
    parse_body,
)
from .providers import conversation_catalog, open_catalog, provider_names, settings_payload
from .registry import Project, WebState, title_from_text
from .streaming import begin_drive, event_stream

FAVICON = Path(__file__).parent / "assets" / "ava-logo.svg"
_LOG = logging.getLogger(__name__)

ATTACHMENT_COUNT_LIMIT = 10
ATTACHMENT_BYTE_LIMIT = 8 * 1024 * 1024
ATTACHMENT_IMAGE_LIMIT = 10
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

    @app.get("/favicon.svg")
    @app.get("/favicon.ico")
    async def favicon() -> Response:
        return FileResponse(FAVICON, media_type="image/svg+xml")

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
        return JSONResponse({
            "projects": [project.summary() for project in registry.projects if not project.hidden],
            "revision": registry.revision,
        })

    @app.get("/api/settings")
    async def settings() -> Response:
        try:
            return JSONResponse(await settings_payload(), headers={"cache-control": "no-store"})
        except AvaError as error:
            return error_response(503, error.message)

    @app.put("/api/settings")
    async def update_settings(request: Request) -> Response:
        body = await parse_body(request, SettingsBody)
        if body is None:
            return error_response(400, "settings must be a valid JSON object")
        provider_name = body.provider.strip()
        base_url = body.base_url.strip() if body.base_url is not None else None
        if body.api_key and provider_name == "codex":
            return error_response(
                400, "the codex provider reuses the Codex CLI login; run 'codex login' instead"
            )

        try:
            save_provider_connection(
                provider_name,
                custom=body.provider_type == "custom",
                family=body.family,
                base_url=base_url,
            )
            if body.api_key:
                save_api_key(provider_name, body.api_key)
        except AvaError as error:
            status = 400 if error.kind in (ErrorKind.invalid_argument, ErrorKind.parse) else 503
            return error_response(status, error.message)

        await reload_provider(provider_name, AuthRequirement.allow_missing)
        payload = await settings_payload()
        payload["saved_provider"] = provider_name
        return JSONResponse(payload, headers={"cache-control": "no-store"})

    @app.post("/api/projects")
    async def add_project(request: Request) -> Response:
        body = await parse_body(request, AddProjectBody)
        if body is None:
            return error_response(400, "path must be a non-empty JSON string")
        path = Path(body.path).expanduser().resolve()
        if not path.is_dir():
            return error_response(400, "path must name an existing directory")
        existing = next((project for project in registry.projects if project.path == path), None)
        if existing is not None:
            if body.restore:
                try:
                    registry.set_hidden(existing, False)
                except AvaError as error:
                    return error_response(503, error.message)
            return JSONResponse({**existing.summary(), "revision": registry.revision})
        project = Project(id=registry.next_project_id(), name=path.name or str(path), path=path)
        registry.projects.append(project)
        try:
            registry.persist()
        except AvaError as error:
            registry.projects.remove(project)
            return error_response(503, error.message)
        return JSONResponse({**project.summary(), "revision": registry.revision}, status_code=201)

    @app.post("/api/projects/{project_id}/hide")
    async def hide_project(project_id: str) -> Response:
        project = registry.find_project(project_id)
        if project is None:
            return error_response(404, "no such project")
        try:
            registry.set_hidden(project, True)
        except AvaError as error:
            return error_response(503, error.message)
        return JSONResponse({"id": project.id, "hidden": True, "revision": registry.revision})

    @app.get("/api/projects/{project_id}/worktrees")
    async def worktree_options(project_id: str) -> Response:
        project = registry.find_project(project_id)
        if project is None or project.hidden:
            return error_response(404, "no such project")
        try:
            return JSONResponse(await worktrees.options(project.path))
        except (OSError, ValueError, TimeoutError) as error:
            return error_response(400, str(error) or "Git took too long. Retry loading branches.")

    async def create_workspace_chat(body: CreateChatBody, key: str) -> Response:
        project = registry.find_project(body.project_id)
        if project is None or project.hidden:
            return error_response(404, "no such project")
        for owner in registry.projects:
            for existing in owner.chats:
                if existing.creation_key == key:
                    if owner.id != project.id or existing.worktree != (body.branch if body.workspace == "worktree" else "") or existing.creation_base != body.base_ref:
                        return error_response(409, "This request already created a different workspace.")
                    return JSONResponse(existing.summary())
        try:
            chat = await state.create_chat(project, body, key)
            return JSONResponse(chat.summary(), status_code=201)
        except (AvaError, OSError, ValueError, TimeoutError) as error:
            message = error.message if isinstance(error, AvaError) else str(error)
            return error_response(503 if isinstance(error, (AvaError, OSError)) else 400, message)

    @app.post("/api/chats")
    async def create_chat(request: Request) -> Response:
        body = await parse_body(request, CreateChatBody)
        if body is None:
            return error_response(400, "Invalid chat or workspace settings.")
        if body.workspace == "worktree" and not body.request_id:
            return error_response(400, "Worktree creation requires a request_id for safe retry.")
        key = body.request_id or uuid4().hex
        # Setup survives HTTP disconnects; explicit retries join the same task.
        task = registry.creations.get(key)
        if task is not None and task.get_name() != body.model_dump_json():
            return error_response(409, "This request is already creating a different session.")
        if task is None:
            task = asyncio.create_task(create_workspace_chat(body, key), name=body.model_dump_json())
            registry.creations[key] = task
            task.add_done_callback(lambda completed: registry.creations.pop(key, None))
        return await asyncio.shield(task)

    @app.post("/api/chats/{chat_id}/review")
    async def review_chat(chat_id: str, request: Request) -> Response:
        found = registry.find_chat(chat_id)
        if found is None or found[0].hidden:
            return error_response(404, "no such chat")
        chat = found[1]
        body = await parse_body(request, ReviewBody)
        if body is None:
            return error_response(400, "review must identify the result being acknowledged")
        if body.through > chat.completion_seq:
            return error_response(409, "This result is no longer available. Refresh the board.")
        previous = chat.reviewed_through
        chat.reviewed_through = max(previous, body.through)
        try:
            if chat.reviewed_through != previous:
                registry.persist()
        except AvaError as error:
            chat.reviewed_through = previous
            return error_response(503, error.message)
        return JSONResponse(chat.summary())

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
                "cwd": str(chat.agent.cwd),
                "worktree": chat.worktree,
                "status": chat.status,
                "turn_open": chat.agent.turn_open,
                "archived": chat.archived,
                "events": [],
            }
        )

    @app.patch("/api/chats/{chat_id}")
    async def rename_chat(chat_id: str, request: Request) -> Response:
        found = registry.find_chat(chat_id)
        if found is None:
            return error_response(404, "no such chat")
        body = await parse_body(request, RenameChatBody)
        if body is None or not body.title.strip():
            return error_response(400, "title must contain between 1 and 200 characters")
        chat = found[1]
        previous = chat.title
        chat.title = body.title.strip()
        try:
            registry.persist()
        except AvaError as error:
            chat.title = previous
            return error_response(503, error.message)
        chat.notify_status()
        return JSONResponse(chat.summary())

    @app.get("/api/chats/{chat_id}/images/{seq}/{block_index}/{image_index}")
    async def tool_image(chat_id: str, seq: int, block_index: int, image_index: int) -> Response:
        from ava.session import ToolResult

        found = registry.find_chat(chat_id)
        if found is None or found[0].hidden or min(seq, block_index, image_index) < 0:
            return error_response(404, "no such image")
        # Sequences are sorted and immutable; binary search avoids scanning history
        # or decompressing its log whenever a user opens a screenshot.
        session = found[1].agent.state.session
        low, high = 0, len(session)
        while low < high:
            middle = (low + high) // 2
            if session.at(middle).seq < seq:
                low = middle + 1
            else:
                high = middle
        if low == len(session) or session.at(low).seq != seq:
            return error_response(404, "no such image")
        payload = session.at(low).payload
        if not isinstance(payload, ToolResult):
            return error_response(404, "no such image")
        try:
            image = payload.item.blocks[block_index].attachments[image_index]
        except IndexError:
            return error_response(404, "no such image")
        if len(image.bytes) > IMAGE_BYTE_LIMIT or image.media_type not in {"image/png", "image/jpeg", "image/gif", "image/webp"}:
            return error_response(422, "image format or size is unsupported")
        try:
            data = await asyncio.to_thread(bytes, image.bytes)
            sniff_image(data, {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp"}[image.media_type])
        except AvaError as error:
            return error_response(422, error.message)
        return Response(data, media_type=image.media_type,
                        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})

    @app.post("/api/chats/{chat_id}/archive")
    async def archive_chat(chat_id: str, request: Request) -> Response:
        found = registry.find_chat(chat_id)
        if found is None:
            return error_response(404, "no such chat")
        body = await parse_body(request, ArchiveBody)
        if body is None:
            return error_response(400, "archived must be a JSON boolean")
        chat = found[1]
        previous = chat.archived
        chat.archived = body.archived
        try:
            registry.persist()
        except AvaError as error:
            chat.archived = previous
            return error_response(503, error.message)
        return JSONResponse(chat.summary())

    @app.delete("/api/chats/{chat_id}", status_code=204)
    async def delete_empty_chat(chat_id: str) -> Response:
        found = registry.find_chat(chat_id)
        if found is None:
            return error_response(404, "no such chat")
        project, chat = found
        if chat.worktree or chat.title or chat.archived or chat.model_configured or chat.status != "idle" or chat.agent.turn_open:
            return error_response(409, "only an unused chat can be removed")
        session_path = chat.agent.session_path
        if session_path is None:
            return error_response(409, "chat has no removable session")
        try:
            index = registry.remove_chat(project, chat)
        except AvaError as error:
            return error_response(503, error.message)
        try:
            session_path.unlink()
        except OSError as error:
            try:
                registry.restore_chat(project, chat, index)
            except AvaError as restore_error:
                return error_response(
                    503,
                    f"cannot remove chat session: {error.strerror or error}; "
                    f"cannot restore its index: {restore_error.message}",
                )
            return error_response(503, f"cannot remove chat session: {error.strerror or error}")
        try:
            await chat.agent.aclose()
        except Exception:
            _LOG.exception("failed to close removed chat %s", chat_id)
        return Response(status_code=204)

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
            try:
                registry.persist()
            except AvaError:
                # The accepted input is already durable and can re-derive its title on restart.
                pass
        if chat.drive.acknowledge(followed_running_drive, agent.status):
            begin_drive(chat)
        return JSONResponse({"accepted": True, "chat": chat.summary()}, status_code=202)

    @app.patch("/api/chats/{chat_id}/inbox/{message_id}")
    async def revise_pending_message(chat_id: str, message_id: str, request: Request) -> Response:
        found = registry.find_chat(chat_id)
        if found is None:
            return error_response(404, "no such chat")
        chat = found[1]
        if chat.archived:
            return error_response(409, "chat is archived")
        body = await parse_body(request, ReviseMessageBody)
        if body is None:
            return error_response(400, "text must be a JSON string")
        try:
            await chat.agent.revise_pending(message_id, body.text.strip())
        except AvaError as error:
            status = (
                409
                if error.kind == ErrorKind.not_found
                else 400
                if error.kind == ErrorKind.invalid_argument
                else 503
            )
            return error_response(status, error.message)
        return JSONResponse({"revised": True, "chat": chat.summary()})

    @app.delete("/api/chats/{chat_id}/inbox/{message_id}")
    async def delete_pending_message(chat_id: str, message_id: str) -> Response:
        found = registry.find_chat(chat_id)
        if found is None:
            return error_response(404, "no such chat")
        chat = found[1]
        if chat.archived:
            return error_response(409, "chat is archived")
        try:
            await chat.agent.delete_pending(message_id)
        except AvaError as error:
            status = 409 if error.kind == ErrorKind.not_found else 503
            return error_response(status, error.message)
        return JSONResponse({"deleted": True, "chat": chat.summary()})

    @app.post("/api/chats/{chat_id}/inbox/{message_id}/send")
    async def send_pending_message(chat_id: str, message_id: str) -> Response:
        found = registry.find_chat(chat_id)
        if found is None:
            return error_response(404, "no such chat")
        chat = found[1]
        if chat.archived:
            return error_response(409, "chat is archived")
        if chat.agent.status == Status.aborting:
            return error_response(409, "chat is aborting")
        agent = chat.agent
        followed_running_drive = chat.drive.running
        try:
            await agent.send_pending(message_id)
        except AvaError as error:
            status = 409 if error.kind == ErrorKind.not_found else 503
            return error_response(status, error.message)
        if agent.status == Status.paused:
            agent.resume()
        if chat.drive.acknowledge(followed_running_drive, agent.status):
            begin_drive(chat)
        return JSONResponse({"sent": True, "chat": chat.summary()}, status_code=202)

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
        try:
            payload = await conversation_catalog(found[1].agent.current_selection())
        except AvaError as error:
            return error_response(503, error.message)
        return JSONResponse(payload, headers={"cache-control": "no-store"})

    @app.post("/api/chats/{chat_id}/model")
    async def select_model(chat_id: str, request: Request) -> Response:
        found = registry.find_chat(chat_id)
        if found is None:
            return error_response(404, "no such chat")
        body = await parse_body(request, SelectionBody)
        if body is None:
            return error_response(400, "body must be a JSON object")
        chat = found[1]
        agent = chat.agent
        previous = agent.current_selection()
        provider_name = body.provider or previous.provider
        replacement = None
        try:
            if provider_name not in provider_names():
                return error_response(400, "Configure this provider in Settings first.")
            entry, replacement = await open_catalog(provider_name)
            if replacement is None:
                return error_response(400, entry["message"])
            model = body.model if "model" in body.model_fields_set else previous.model
            if not model or model not in {item["id"] for item in entry["models"]}:
                return error_response(400, "Choose a model from this provider's catalog.")
            changed_model = provider_name != previous.provider or model != previous.model
            effort = body.effort if "effort" in body.model_fields_set else None if changed_model else previous.effort
            selection = Selection(provider_name, model, effort)
            replacement.validate_selection(selection)
            if provider_name == previous.provider and (chat.status != "idle" or chat.drive.running):
                # Validate the complete draft before touching either pending field.
                agent.state.provider.model_overrides[model] = replacement.capabilities(model)
                agent.select_model(model)
                agent.select_effort(effort)
            else:
                if chat.drive.running:
                    return error_response(409, "Wait for this conversation to finish before changing providers.")
                replacement.selection = selection
                replacement.context_window = replacement.capabilities(model).context_window_tokens or 0
                replacement.selection_model_may_be_alias = False
                await agent.replace_provider(replacement)
                replacement = None
            chat.model_configured = True
            registry.persist()
            chat.notify_status()
        except AvaError as error:
            return error_response(409 if "busy" in error.message else 400, error.message)
        finally:
            if replacement is not None:
                await replacement.aclose()
        return JSONResponse(asdict(selection))

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
        catalog = await asyncio.to_thread(discover_skills, found[1].agent.cwd)
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
