"""The provider-neutral conversation vocabulary shared by the model, session, and tool packages.

Values only: no behavior, no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Role(StrEnum):
    user = "user"
    assistant = "assistant"
    tool = "tool"


class ContentBlockKind(StrEnum):
    text = "text"
    file_text = "file_text"
    image = "image"
    reasoning = "reasoning"
    tool_call = "tool_call"
    tool_result = "tool_result"


class Origin(StrEnum):
    none = "none"
    interrupted = "interrupted"
    skipped = "skipped"


@dataclass(slots=True)
class ContentBlock:
    kind: ContentBlockKind
    origin: Origin = Origin.none
    text: str = ""
    display_path: str = ""
    bytes: bytes = b""
    media_type: str = ""
    opaque_json: str = ""
    call_id: str = ""
    tool_name: str = ""
    arguments_json: str = ""
    is_error: bool = False


def make_text_block(text: str) -> ContentBlock:
    return ContentBlock(kind=ContentBlockKind.text, text=text)


def make_file_text_block(display_path: str, text: str) -> ContentBlock:
    return ContentBlock(kind=ContentBlockKind.file_text, display_path=display_path, text=text)


def make_image_block(display_path: str, data: bytes, media_type: str) -> ContentBlock:
    return ContentBlock(
        kind=ContentBlockKind.image, display_path=display_path, bytes=data, media_type=media_type
    )


def make_reasoning_block(opaque_json: str, summary: str = "") -> ContentBlock:
    return ContentBlock(
        kind=ContentBlockKind.reasoning, text=summary, opaque_json=opaque_json
    )


def make_tool_call_block(call_id: str, tool_name: str, arguments_json: str = "") -> ContentBlock:
    return ContentBlock(
        kind=ContentBlockKind.tool_call,
        call_id=call_id,
        tool_name=tool_name,
        arguments_json=arguments_json,
    )


def make_tool_result_block(call_id: str, text: str, is_error: bool) -> ContentBlock:
    return ContentBlock(
        kind=ContentBlockKind.tool_result, call_id=call_id, text=text, is_error=is_error
    )


class ToolParamType(StrEnum):
    string = "string"
    integer = "integer"
    boolean = "boolean"


@dataclass(slots=True)
class ToolParam:
    name: str
    description: str
    type: ToolParamType = ToolParamType.string
    required: bool = False
    minimum: int | None = None


@dataclass(slots=True)
class ToolDef:
    name: str
    description: str
    params: list[ToolParam] = field(default_factory=list)


@dataclass(slots=True)
class Provenance:
    provider: str = ""
    model: str = ""


@dataclass(slots=True)
class Item:
    role: Role
    blocks: list[ContentBlock] = field(default_factory=list)
    provenance: Provenance = field(default_factory=Provenance)


@dataclass(slots=True)
class Context:
    """What the model sees. Items are shared references into the session's event storage."""

    system_prompt: str = ""
    items: list[Item] = field(default_factory=list)
    tools: list[ToolDef] = field(default_factory=list)
