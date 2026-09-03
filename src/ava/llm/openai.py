"""OpenAI-compatible Chat Completions adapter (OpenAI, DeepSeek, llama.cpp, vLLM, gateways)."""

from __future__ import annotations

import json
from dataclasses import dataclass

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
from ava.llm.types import ContentBlockKind, Context, Item, Role, ToolDef
from ava.transport import Client, Request, SseEvent


def _dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _user_message(item: Item, counter: list[int]) -> dict:
    parts: list[dict] = []
    for block in item.blocks:
        match block.kind:
            case ContentBlockKind.text:
                parts.append({"type": "text", "text": block.text})
            case ContentBlockKind.file_text:
                parts.append({"type": "text", "text": request_file_text(block)})
            case ContentBlockKind.image:
                counter[0] += 1
                parts.append(
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{block.media_type};base64,{encode_base64(block.bytes)}"
                        },
                    }
                )
            case _:
                raise AvaError(
                    ErrorKind.internal, "user message contains a block that OpenAI cannot serialize"
                )
    return {"role": "user", "content": parts}


def _assistant_message(item: Item) -> dict:
    content: list[dict] = []
    calls: list[dict] = []
    for block in item.blocks:
        match block.kind:
            case ContentBlockKind.text:
                content.append({"type": "text", "text": block.text})
            case ContentBlockKind.file_text:
                content.append({"type": "text", "text": request_file_text(block)})
            case ContentBlockKind.tool_call:
                calls.append(
                    {
                        "id": block.call_id,
                        "type": "function",
                        "function": {
                            "name": block.tool_name,
                            "arguments": block.arguments_json or "{}",
                        },
                    }
                )
            case ContentBlockKind.reasoning:
                continue
            case _:
                raise AvaError(
                    ErrorKind.internal,
                    "assistant message contains a block that OpenAI cannot serialize",
                )
    message: dict = {"role": "assistant", "content": content or None}
    if calls:
        message["tool_calls"] = calls
    return message


def _tool_messages(item: Item) -> list[dict]:
    messages: list[dict] = []
    for block in item.blocks:
        if block.kind != ContentBlockKind.tool_result:
            raise AvaError(
                ErrorKind.internal,
                "tool message contains a non-result block that OpenAI cannot serialize",
            )
        messages.append({"role": "tool", "tool_call_id": block.call_id, "content": block.text})
    return messages


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
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


def openai_request_body(context: Context, model: str, effort: str | None) -> str:
    body: dict = {"model": model}
    if effort is not None:
        body["reasoning_effort"] = effort
    body["stream"] = True
    body["stream_options"] = {"include_usage": True}
    messages: list[dict] = []
    if context.system_prompt:
        messages.append({"role": "system", "content": context.system_prompt})
    counter = [0]
    for item in context.items:
        if item.role == Role.user:
            messages.append(_user_message(item, counter))
        elif item.role == Role.assistant:
            messages.append(_assistant_message(item))
        else:
            messages.extend(_tool_messages(item))
    body["messages"] = messages
    if context.tools:
        # The loop dispatches one streamed call at a time.
        body["parallel_tool_calls"] = False
        body["tools"] = [_tool_schema(tool) for tool in context.tools]
    encoded = _dumps(body)
    check_request_limits(counter[0], len(encoded.encode("utf-8")))
    return encoded


@dataclass(slots=True)
class OpenAIStreamState:
    tool_index: int | None = None
    tool_id: str = ""
    tool_name: str = ""
    tool_started: bool = False
    stop_reason: StopReason | None = None


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _emit_openai_usage(source: dict, sink: StreamSink) -> None:
    raw_details = source.get("prompt_tokens_details")
    details: dict = raw_details if isinstance(raw_details, dict) else {}
    raw_completion_details = source.get("completion_tokens_details")
    completion_details: dict = (
        raw_completion_details if isinstance(raw_completion_details, dict) else {}
    )
    prompt_tokens = _int_or_none(source.get("prompt_tokens"))
    cached = _int_or_none(details.get("cached_tokens"))
    if cached is None:
        cached = _int_or_none(source.get("prompt_cache_hit_tokens"))
    reasoning = _int_or_none(completion_details.get("reasoning_tokens"))
    usage = Usage()
    miss = _int_or_none(source.get("prompt_cache_miss_tokens"))
    if miss is not None:
        usage.input = miss
    elif prompt_tokens is not None:
        usage.input = prompt_tokens - min(prompt_tokens, cached or 0)
    usage.cached_read = cached
    usage.cache_write = _int_or_none(details.get("cache_write_tokens"))
    completion_tokens = _int_or_none(source.get("completion_tokens"))
    if completion_tokens is not None:
        usage.output = completion_tokens - min(completion_tokens, reasoning or 0)
    usage.reasoning = reasoning
    if usage.any():
        sink(StreamEvent(kind=StreamEventKind.usage, usage=usage))


def _consume_tool_delta(delta: dict, sink: StreamSink, state: OpenAIStreamState) -> None:
    index = delta.get("index")
    if not isinstance(index, int):
        raise AvaError(
            ErrorKind.parse,
            "OpenAI tool call delta is missing its index; check endpoint compatibility",
        )
    if state.tool_index is not None and state.tool_index != index:
        raise AvaError(
            ErrorKind.provider,
            "OpenAI streamed parallel tool calls after Ava disabled them; check endpoint compatibility",
        )
    state.tool_index = index
    streamed_id = delta.get("id") or ""
    if streamed_id:
        if state.tool_id and state.tool_id != streamed_id:
            raise AvaError(
                ErrorKind.parse,
                "OpenAI changed a streamed tool call id; check endpoint compatibility",
            )
        state.tool_id = streamed_id
    raw_function = delta.get("function")
    function: dict = raw_function if isinstance(raw_function, dict) else {}
    name = function.get("name")
    if isinstance(name, str) and name:
        if state.tool_name and state.tool_name != name:
            raise AvaError(
                ErrorKind.parse, "OpenAI changed a streamed tool name; check endpoint compatibility"
            )
        state.tool_name = name
    if not state.tool_started and state.tool_id and state.tool_name:
        sink(
            StreamEvent(
                kind=StreamEventKind.tool_call_start, id=state.tool_id, name=state.tool_name
            )
        )
        state.tool_started = True
    arguments = function.get("arguments")
    if isinstance(arguments, str) and arguments:
        if not state.tool_started:
            raise AvaError(
                ErrorKind.parse,
                "OpenAI streamed tool arguments before the call identity; check endpoint compatibility",
            )
        sink(StreamEvent(kind=StreamEventKind.tool_call_delta, text=arguments, id=state.tool_id))


def _apply_finish_reason(reason: str, sink: StreamSink, state: OpenAIStreamState) -> None:
    if reason == "stop":
        state.stop_reason = StopReason.end_turn
    elif reason == "length":
        state.stop_reason = StopReason.max_tokens
    elif reason == "tool_calls":
        if not state.tool_started:
            raise AvaError(
                ErrorKind.parse,
                "OpenAI stopped for tool calls without a complete call identity; check endpoint compatibility",
            )
        sink(StreamEvent(kind=StreamEventKind.tool_call_end, id=state.tool_id))
        state.tool_started = False
        state.stop_reason = StopReason.tool_use
    else:
        raise AvaError(ErrorKind.provider, f"OpenAI stopped with unsupported reason '{reason}'")


def consume_openai_event(
    event: SseEvent, sink: StreamSink, state: OpenAIStreamState
) -> StopReason | None:
    if event.data == "[DONE]":
        if state.tool_started:
            raise AvaError(
                ErrorKind.parse,
                "OpenAI stopped before finishing a tool call; retry or check endpoint compatibility",
            )
        sink(StreamEvent(kind=StreamEventKind.done))
        return state.stop_reason or StopReason.end_turn
    try:
        value = json.loads(event.data)
    except json.JSONDecodeError:
        raise AvaError(
            ErrorKind.parse, "OpenAI stream contained invalid JSON; check endpoint compatibility"
        ) from None
    if not isinstance(value, dict):
        raise AvaError(
            ErrorKind.parse, "OpenAI stream contained invalid JSON; check endpoint compatibility"
        )
    if event.event == "error" or value.get("error") is not None:
        raise AvaError(
            ErrorKind.provider,
            "OpenAI reported an error while streaming; retry or check provider status",
            _dumps(value.get("error")) if value.get("error") is not None else "",
        )
    if isinstance(value.get("usage"), dict):
        _emit_openai_usage(value["usage"], sink)
    choices = value.get("choices")
    if not isinstance(choices, list):
        return None
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        if isinstance(choice.get("index"), int) and choice["index"] != 0:
            raise AvaError(
                ErrorKind.provider,
                "OpenAI returned more than one completion choice; check endpoint compatibility",
            )
        delta = choice.get("delta")
        if isinstance(delta, dict):
            content = delta.get("content")
            if isinstance(content, str):
                sink(StreamEvent(kind=StreamEventKind.text_delta, text=content))
            calls = delta.get("tool_calls")
            if isinstance(calls, list):
                for call in calls:
                    if isinstance(call, dict):
                        _consume_tool_delta(call, sink, state)
        finish_reason = choice.get("finish_reason")
        if isinstance(finish_reason, str) and finish_reason:
            _apply_finish_reason(finish_reason, sink, state)
    return None


def openai_idle_timeout(provider: str) -> float:
    # CPU prompt prefill can remain silent for minutes on a healthy local server.
    return 600.0 if provider == "llamacpp" else 120.0


class OpenAIProvider(Provider):
    def __init__(self, selection: Selection, base_url: str, api_key: str) -> None:
        super().__init__(selection)
        self.id = selection.provider
        self.display_name = {
            "openai": "OpenAI",
            "llamacpp": "llama.cpp",
            "deepseek": "DeepSeek",
        }.get(selection.provider, self.id)
        self.context_window = (
            builtin_model_capabilities(selection.provider, selection.model).context_window_tokens
            or 0
        )
        self._base_url = base_url
        self._api_key = api_key
        self._transport = Client()

    def _auth_headers(self) -> list[tuple[str, str]]:
        return [("authorization", f"Bearer {self._api_key}")] if self._api_key else []

    async def stream(
        self, context: Context, selected: Selection, sink: StreamSink, cancel: CancelToken = NEVER
    ) -> StopReason:
        body = openai_request_body(context, selected.model, selected.effort)
        request = Request(
            url=self._base_url + "/chat/completions",
            headers=[("content-type", "application/json"), ("accept", "text/event-stream")]
            + self._auth_headers(),
            body=body,
            idle_timeout_seconds=openai_idle_timeout(self.id),
        )
        state = OpenAIStreamState()
        stream_error: AvaError | None = None
        stop_reason: StopReason | None = None

        def on_event(event: SseEvent) -> None:
            nonlocal stream_error, stop_reason
            if stream_error is not None or stop_reason is not None:
                return
            try:
                consumed = consume_openai_event(event, sink, state)
            except AvaError as error:
                stream_error = error
                return
            if consumed is not None:
                stop_reason = consumed

        response = await self._transport.post_sse(request, on_event, cancel)
        if not 200 <= response.status < 300:
            raise response_error("OpenAI", response.status, response.body)
        if stream_error is not None:
            raise stream_error
        if stop_reason is not None:
            return stop_reason
        raise AvaError(
            ErrorKind.parse,
            "OpenAI stream ended without a [DONE] event; retry or check endpoint compatibility",
        )

    async def list_models(self, cancel: CancelToken = NEVER) -> list[str]:
        request = Request(
            url=self._base_url + "/models",
            headers=[("accept", "application/json")] + self._auth_headers(),
        )
        response = await self._transport.get(request, cancel)
        if not 200 <= response.status < 300:
            raise response_error("OpenAI", response.status, response.body)
        return parse_model_ids(response.body, "OpenAI")

    async def aclose(self) -> None:
        await self._transport.aclose()
