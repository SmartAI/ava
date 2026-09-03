"""Anthropic Messages adapter: request serialization, stream normalization, model discovery."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from ava.base import AvaError, CancelToken, ErrorKind
from ava.base.cancel import NEVER
from ava.llm.provider import (
    Provider,
    Selection,
    StopReason,
    StreamEvent,
    StreamEventKind,
    StreamSink,
    Usage,
    builtin_model_capabilities,
    check_request_limits,
    encode_base64,
    parse_model_ids,
    request_file_text,
    request_schema_type,
    response_error,
)
from ava.llm.types import ContentBlockKind, Context, Role, ToolDef
from ava.transport import Client, Request, SseEvent

DEFAULT_MAX_OUTPUT_TOKENS = 32_000
ANTHROPIC_VERSION = "2023-06-01"
_PATH_SAFE_MODEL = re.compile(r"^[A-Za-z0-9._-]+$")


def _dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _object_or_empty(arguments_json: str) -> dict:
    # A malformed streamed call must still be paired with its recoverable tool-error result.
    try:
        parsed = json.loads(arguments_json)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _tool_schema(tool: ToolDef) -> dict:
    properties: dict[str, dict] = {}
    required: list[str] = []
    for param in tool.params:
        schema: dict = {"type": request_schema_type(param.type), "description": param.description}
        if param.minimum is not None:
            schema["minimum"] = param.minimum
        properties[param.name] = schema
        if param.required:
            required.append(param.name)
    return {
        "name": tool.name,
        "description": tool.description,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    }


def request_body(context: Context, model: str, max_tokens: int) -> str:
    """Serialize one request. Reasoning blocks are never sent back (see model-layer rule 4)."""
    messages: list[dict] = []
    image_count = 0
    for item in context.items:
        role = "assistant" if item.role == Role.assistant else "user"
        content: list[dict] = []
        for block in item.blocks:
            match block.kind:
                case ContentBlockKind.reasoning:
                    continue
                case ContentBlockKind.text:
                    content.append({"type": "text", "text": block.text})
                case ContentBlockKind.file_text:
                    content.append({"type": "text", "text": request_file_text(block)})
                case ContentBlockKind.image:
                    image_count += 1
                    content.append(
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": block.media_type,
                                "data": encode_base64(block.bytes),
                            },
                        }
                    )
                case ContentBlockKind.tool_call:
                    content.append(
                        {
                            "type": "tool_use",
                            "id": block.call_id,
                            "name": block.tool_name,
                            "input": _object_or_empty(block.arguments_json),
                        }
                    )
                case ContentBlockKind.tool_result:
                    result: dict = {
                        "type": "tool_result",
                        "tool_use_id": block.call_id,
                        "content": block.text,
                    }
                    if block.is_error:
                        result["is_error"] = True
                    content.append(result)
        messages.append({"role": role, "content": content})
    body: dict = {"model": model, "max_tokens": max_tokens, "stream": True, "messages": messages}
    if context.system_prompt:
        body["system"] = context.system_prompt
    if context.tools:
        body["tools"] = [_tool_schema(tool) for tool in context.tools]
    encoded = _dumps(body)
    check_request_limits(image_count, len(encoded.encode("utf-8")))
    return encoded


@dataclass(slots=True)
class AnthropicStreamState:
    tool_call_ids: dict[int, str] = field(default_factory=dict)
    stop_reason: StopReason | None = None


def _usage_from(source: dict | None) -> Usage | None:
    if not isinstance(source, dict):
        return None
    usage = Usage(
        input=source.get("input_tokens"),
        cached_read=source.get("cache_read_input_tokens"),
        cache_write=source.get("cache_creation_input_tokens"),
        output=source.get("output_tokens"),
    )
    creation = source.get("cache_creation")
    if isinstance(creation, dict):
        usage.cache_write_1h = creation.get("ephemeral_1h_input_tokens")
    for name in ("input", "cached_read", "cache_write", "cache_write_1h", "output"):
        if getattr(usage, name) is not None and not isinstance(getattr(usage, name), int):
            setattr(usage, name, None)
    return usage if usage.any() else None


def _emit_usage(source: dict | None, sink: StreamSink) -> None:
    usage = _usage_from(source)
    if usage is not None:
        sink(StreamEvent(kind=StreamEventKind.usage, usage=usage))


def consume_anthropic_event(
    event: SseEvent, sink: StreamSink, state: AnthropicStreamState
) -> StopReason | None:
    try:
        value = json.loads(event.data)
    except json.JSONDecodeError:
        raise AvaError(
            ErrorKind.parse, "Anthropic stream contained invalid JSON; check endpoint compatibility"
        ) from None
    if not isinstance(value, dict):
        raise AvaError(
            ErrorKind.parse, "Anthropic stream contained invalid JSON; check endpoint compatibility"
        )
    kind = value.get("type") if isinstance(value.get("type"), str) else ""
    if event.event == "error" or kind == "error":
        raise AvaError(
            ErrorKind.provider,
            "Anthropic reported an error while streaming; retry or check provider status",
            _dumps(value.get("error")) if value.get("error") is not None else "",
        )
    index = value.get("index")
    if kind == "message_start":
        message = value.get("message")
        if isinstance(message, dict):
            _emit_usage(message.get("usage"), sink)
    elif kind == "content_block_start":
        block = value.get("content_block")
        if isinstance(block, dict) and block.get("type") == "tool_use":
            call_id = block.get("id")
            name = block.get("name")
            if not isinstance(index, int) or not call_id or not name:
                raise AvaError(
                    ErrorKind.parse,
                    "Anthropic tool call start is missing its index, id, or name; check endpoint compatibility",
                )
            if index in state.tool_call_ids:
                raise AvaError(
                    ErrorKind.parse,
                    "Anthropic reused a content block index; check endpoint compatibility",
                )
            state.tool_call_ids[index] = call_id
            sink(StreamEvent(kind=StreamEventKind.tool_call_start, id=call_id, name=name))
    elif kind == "content_block_delta":
        delta = value.get("delta")
        if not isinstance(delta, dict):
            raise AvaError(
                ErrorKind.parse,
                "Anthropic content delta is missing 'delta'; check endpoint compatibility",
            )
        if delta.get("type") == "text_delta":
            text = delta.get("text")
            if not isinstance(text, str):
                raise AvaError(
                    ErrorKind.parse,
                    "Anthropic text delta is missing 'text'; check endpoint compatibility",
                )
            sink(StreamEvent(kind=StreamEventKind.text_delta, text=text))
        elif delta.get("type") == "input_json_delta":
            partial = delta.get("partial_json")
            if not isinstance(index, int) or not isinstance(partial, str):
                raise AvaError(
                    ErrorKind.parse,
                    "Anthropic tool input delta is missing its index or partial_json; check endpoint compatibility",
                )
            call_id = state.tool_call_ids.get(index)
            if call_id is None:
                raise AvaError(
                    ErrorKind.parse,
                    "Anthropic tool input delta has no matching start; check endpoint compatibility",
                )
            sink(StreamEvent(kind=StreamEventKind.tool_call_delta, text=partial, id=call_id))
    elif kind == "content_block_stop" and isinstance(index, int):
        call_id = state.tool_call_ids.pop(index, None)
        if call_id is not None:
            sink(StreamEvent(kind=StreamEventKind.tool_call_end, id=call_id))
    elif kind == "message_delta":
        _emit_usage(value.get("usage"), sink)
        delta = value.get("delta")
        if isinstance(delta, dict) and delta.get("stop_reason") is not None:
            reason = delta["stop_reason"]
            if reason == "end_turn":
                state.stop_reason = StopReason.end_turn
            elif reason == "max_tokens":
                state.stop_reason = StopReason.max_tokens
            elif reason == "tool_use":
                state.stop_reason = StopReason.tool_use
            else:
                raise AvaError(
                    ErrorKind.provider, f"Anthropic stopped with unsupported reason '{reason}'"
                )
    elif kind == "message_stop":
        if state.tool_call_ids:
            raise AvaError(
                ErrorKind.parse,
                "Anthropic stopped before finishing a tool call; retry or check endpoint compatibility",
            )
        sink(StreamEvent(kind=StreamEventKind.done))
        return state.stop_reason or StopReason.end_turn
    return None


@dataclass(slots=True)
class AnthropicSettings:
    base_url: str
    api_key_env: str = "ANTHROPIC_API_KEY"


class AnthropicProvider(Provider):
    def __init__(self, selection: Selection, settings: AnthropicSettings, api_key: str) -> None:
        super().__init__(selection)
        self.id = selection.provider
        self.display_name = "Anthropic" if selection.provider == "anthropic" else self.id
        self.context_window = (
            builtin_model_capabilities(selection.provider, selection.model).context_window_tokens
            or 0
        )
        self._settings = settings
        self._api_key = api_key
        self._max_tokens: int | None = None
        self._max_tokens_model = ""
        self._transport = Client()

    def _headers(self, accept: str) -> list[tuple[str, str]]:
        return [
            ("content-type", "application/json"),
            ("accept", accept),
            ("anthropic-version", ANTHROPIC_VERSION),
            ("x-api-key", self._api_key),
        ]

    async def stream(
        self, context: Context, selected: Selection, sink: StreamSink, cancel: CancelToken = NEVER
    ) -> StopReason:
        if self._max_tokens is None or self._max_tokens_model != selected.model:
            resolved = await self._resolve_max_tokens(selected.model, cancel)
            cancel.raise_if_cancelled()
            self._max_tokens = resolved
            self._max_tokens_model = selected.model
        body = request_body(context, selected.model, self._max_tokens)
        request = Request(
            url=self._settings.base_url + "/v1/messages",
            headers=self._headers("text/event-stream"),
            body=body,
        )
        state = AnthropicStreamState()
        stream_error: AvaError | None = None
        stop_reason: StopReason | None = None

        def on_event(event: SseEvent) -> None:
            nonlocal stream_error, stop_reason
            if stream_error is not None or stop_reason is not None:
                return
            try:
                consumed = consume_anthropic_event(event, sink, state)
            except AvaError as error:
                stream_error = error
                return
            if consumed is not None:
                stop_reason = consumed

        response = await self._transport.post_sse(request, on_event, cancel)
        if not 200 <= response.status < 300:
            raise response_error("Anthropic", response.status, response.body)
        if stream_error is not None:
            raise stream_error
        if stop_reason is not None:
            return stop_reason
        raise AvaError(
            ErrorKind.parse,
            "Anthropic stream ended without a message_stop event; retry or check endpoint compatibility",
        )

    async def list_models(self, cancel: CancelToken = NEVER) -> list[str]:
        request = Request(
            url=self._settings.base_url + "/v1/models?limit=1000",
            headers=self._headers("application/json"),
        )
        response = await self._transport.get(request, cancel)
        if not 200 <= response.status < 300:
            raise response_error("Anthropic", response.status, response.body)
        return parse_model_ids(response.body, "Anthropic")

    async def _resolve_max_tokens(self, model: str, cancel: CancelToken) -> int:
        if not _PATH_SAFE_MODEL.match(model):
            return DEFAULT_MAX_OUTPUT_TOKENS
        request = Request(
            url=f"{self._settings.base_url}/v1/models/{model}",
            headers=self._headers("application/json"),
        )
        try:
            response = await self._transport.get(request, cancel)
        except AvaError as error:
            if error.kind == ErrorKind.cancelled:
                raise
            return DEFAULT_MAX_OUTPUT_TOKENS
        if not 200 <= response.status < 300:
            return DEFAULT_MAX_OUTPUT_TOKENS
        try:
            info = json.loads(response.body)
        except json.JSONDecodeError:
            return DEFAULT_MAX_OUTPUT_TOKENS
        max_tokens = info.get("max_tokens") if isinstance(info, dict) else None
        if not isinstance(max_tokens, int) or max_tokens <= 0:
            return DEFAULT_MAX_OUTPUT_TOKENS
        return min(max_tokens, DEFAULT_MAX_OUTPUT_TOKENS)

    async def aclose(self) -> None:
        await self._transport.aclose()
