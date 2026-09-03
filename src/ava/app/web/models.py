"""Typed request bodies for the Web API."""

from __future__ import annotations

import json
from typing import Any, Literal

from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class RequestBody(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")


class AddProjectBody(RequestBody):
    path: str = Field(min_length=1)


class CreateChatBody(RequestBody):
    project_id: str = Field(min_length=1)


class ArchiveBody(RequestBody):
    archived: bool


class MessageBody(RequestBody):
    text: str = ""
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    delivery: str = "followup"


class CancelBody(RequestBody):
    cause: Literal["pause", "abort"]


class SelectionBody(RequestBody):
    model: str | None = None
    effort: str | None = None


class CredentialsBody(RequestBody):
    provider: str = Field(min_length=1)
    key: str = Field(min_length=1)


async def parse_body[Body: RequestBody](
    request: Request, body_type: type[Body]
) -> Body | None:
    try:
        raw = json.loads(await request.body() or b"null")
        return body_type.model_validate(raw)
    except (json.JSONDecodeError, UnicodeDecodeError, ValidationError):
        return None
