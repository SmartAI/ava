"""Codex Responses adapter over the Codex CLI's ChatGPT OAuth credential.

Ava reuses a read-only snapshot of ``$CODEX_HOME/auth.json`` (default ``~/.codex/auth.json``): it
never reads the refresh token, never writes the file, and never takes over login. Every malformed,
missing, or expired case returns the same secret-free instruction to run ``codex login``.
Requests are stateless (``store: false``) and carry only ``Authorization`` and
``ChatGPT-Account-Id``; provider rejection bodies never enter returned errors.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path

from ava.base import AvaError, CancelToken, ErrorKind
from ava.base.cancel import NEVER
from ava.llm.provider import (
    ModelCapabilities,
    Provider,
    Selection,
    StopReason,
    StreamEvent,
    StreamEventKind,
    StreamSink,
    Usage,
    check_request_limits,
    encode_base64,
    request_file_text,
    request_schema_type,
)
from ava.llm.types import ContentBlockKind, Context, Item, Role, ToolDef
from ava.transport import Client, Request, SseEvent

CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"
# Catalog filtering follows this audited Codex protocol contract, not Ava's release number.
CODEX_CLIENT_VERSION = "0.150.1"
MAX_CODEX_AUTH_BYTES = 1 << 20
_OPENAI_AUTH_CLAIM = "https://api.openai.com/auth"


# ---- Credentials ------------------------------------------------------------------------------


@dataclass(slots=True)
class CodexCredential:
    access_token: str
    account_id: str


def _auth_error() -> AvaError:
    return AvaError(
        ErrorKind.auth, "Codex credentials are unavailable; run 'codex login' and restart Ava"
    )


def _decode_base64url(encoded: str) -> bytes | None:
    if not encoded:
        return None
    padded = encoded + "=" * (-len(encoded) % 4)
    try:
        return base64.urlsafe_b64decode(padded)
    except (binascii.Error, ValueError):
        return None


def _jwt_claims(token: str) -> dict | None:
    parts = token.split(".")
    if (
        len(parts) != 3
        or _decode_base64url(parts[0]) is None
        or _decode_base64url(parts[2]) is None
    ):
        return None
    payload = _decode_base64url(parts[1])
    if payload is None:
        return None
    try:
        claims = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return claims if isinstance(claims, dict) else None


def _header_value_safe(value: str) -> bool:
    return bool(value) and all(0x21 <= ord(character) <= 0x7E for character in value)


def parse_codex_credential(text: str, now: int | None = None) -> CodexCredential:
    """Fail-closed parsing of the Codex CLI auth file against ``now`` (epoch seconds)."""
    now = int(time.time()) if now is None else now
    try:
        document = json.loads(text)
    except json.JSONDecodeError:
        raise _auth_error() from None
    if not isinstance(document, dict) or document.get("auth_mode") != "chatgpt":
        raise _auth_error()
    raw_tokens = document.get("tokens")
    tokens: dict = raw_tokens if isinstance(raw_tokens, dict) else {}
    access_token = tokens.get("access_token") or ""
    id_token = tokens.get("id_token") or ""
    if (
        not isinstance(access_token, str)
        or not isinstance(id_token, str)
        or not access_token
        or not id_token
    ):
        raise _auth_error()
    access_claims = _jwt_claims(access_token)
    expiry = access_claims.get("exp") if access_claims else None
    if not isinstance(expiry, int) or isinstance(expiry, bool) or expiry <= now:
        raise _auth_error()
    id_claims = _jwt_claims(id_token)
    if id_claims is None:
        raise _auth_error()
    account_id = tokens.get("account_id") or ""
    if not account_id:
        auth_claims = id_claims.get(_OPENAI_AUTH_CLAIM)
        account_id = (
            auth_claims.get("chatgpt_account_id", "") if isinstance(auth_claims, dict) else ""
        )
    if not isinstance(account_id, str) or not _header_value_safe(account_id):
        raise _auth_error()
    return CodexCredential(access_token=access_token, account_id=account_id)


def load_codex_credential_file(path: Path, now: int | None = None) -> CodexCredential:
    try:
        with open(path, "rb") as source:
            data = source.read(MAX_CODEX_AUTH_BYTES + 1)
    except OSError:
        raise _auth_error() from None
    if len(data) > MAX_CODEX_AUTH_BYTES:
        raise _auth_error()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise _auth_error() from None
    return parse_codex_credential(text, now)


def load_codex_credential() -> CodexCredential:
    configured = os.environ.get("CODEX_HOME")
    if configured:
        home = Path(configured)
    elif os.environ.get("HOME"):
        home = Path(os.environ["HOME"]) / ".codex"
    else:
        raise _auth_error()
    return load_codex_credential_file(home / "auth.json")


# ---- Request ----------------------------------------------------------------------------------


def _dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _text_content(kind: str, text: str) -> dict:
    return {"type": kind, "text": text}


def _user_item(item: Item, counter: list[int]) -> dict:
    content: list[dict] = []
    for block in item.blocks:
        match block.kind:
            case ContentBlockKind.text:
                content.append(_text_content("input_text", block.text))
            case ContentBlockKind.file_text:
                content.append(_text_content("input_text", request_file_text(block)))
            case ContentBlockKind.image:
                counter[0] += 1
                content.append(
                    {
                        "type": "input_image",
                        "image_url": f"data:{block.media_type};base64,{encode_base64(block.bytes)}",
                    }
                )
            case _:
                raise AvaError(
                    ErrorKind.internal, "user message contains a block that Codex cannot serialize"
                )
    return {"type": "message", "role": "user", "content": content}


def _assistant_items(item: Item, selected: Selection) -> list[str]:
    """Encoded JSON items. Reasoning is replayed byte-identically, and only to the model that made it."""
    encoded: list[str] = []
    for block in item.blocks:
        match block.kind:
            case ContentBlockKind.reasoning:
                if (
                    item.provenance.provider != selected.provider
                    or item.provenance.model != selected.model
                ):
                    continue
                if not block.opaque_json:
                    raise AvaError(ErrorKind.internal, "Codex reasoning item is empty")
                encoded.append(block.opaque_json)
            case ContentBlockKind.text | ContentBlockKind.file_text:
                text = (
                    block.text if block.kind == ContentBlockKind.text else request_file_text(block)
                )
                encoded.append(
                    _dumps(
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [_text_content("output_text", text)],
                        }
                    )
                )
            case ContentBlockKind.tool_call:
                encoded.append(
                    _dumps(
                        {
                            "type": "function_call",
                            "name": block.tool_name,
                            "arguments": block.arguments_json,
                            "call_id": block.call_id,
                        }
                    )
                )
            case _:
                raise AvaError(
                    ErrorKind.internal,
                    "assistant message contains a block that Codex cannot serialize",
                )
    return encoded


def _tool_items(item: Item) -> list[dict]:
    items: list[dict] = []
    for block in item.blocks:
        if block.kind != ContentBlockKind.tool_result:
            raise AvaError(
                ErrorKind.internal,
                "tool message contains a non-result block that Codex cannot serialize",
            )
        items.append(
            {"type": "function_call_output", "call_id": block.call_id, "output": block.text}
        )
    return items


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
        "name": tool.name,
        "description": tool.description,
        "strict": False,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    }


def codex_input_json(context: Context, selected: Selection) -> str:
    counter = [0]
    encoded: list[str] = []
    for item in context.items:
        if item.role == Role.user:
            encoded.append(_dumps(_user_item(item, counter)))
        elif item.role == Role.assistant:
            encoded.extend(_assistant_items(item, selected))
        else:
            encoded.extend(_dumps(entry) for entry in _tool_items(item))
    output = "[" + ",".join(encoded) + "]"
    check_request_limits(counter[0], len(output.encode("utf-8")))
    return output


def codex_request_body(
    context: Context, selected: Selection, *, prompt_cache_key: str | None = None
) -> str:
    """The stateless Responses request, assembled around the pre-encoded input array."""
    reasoning: dict = {"effort": selected.effort} if selected.effort is not None else {}
    head: dict = {"model": selected.model}
    if context.system_prompt:
        head["instructions"] = context.system_prompt
    tail = {
        "tools": [_tool_schema(tool) for tool in context.tools],
        "tool_choice": "auto",
        "parallel_tool_calls": False,
        "reasoning": reasoning,
        "store": False,
        "stream": True,
        "include": ["reasoning.encrypted_content"],
    }
    if prompt_cache_key is not None:
        tail["prompt_cache_key"] = prompt_cache_key
    body = (
        _dumps(head)[:-1]
        + ',"input":'
        + codex_input_json(context, selected)
        + ","
        + _dumps(tail)[1:]
    )
    check_request_limits(0, len(body.encode("utf-8")))
    return body


# ---- Stream -----------------------------------------------------------------------------------


@dataclass(slots=True)
class CodexStreamState:
    item_id: str = ""
    call_id: str = ""
    name: str = ""
    arguments: str = ""
    tool_started: bool = False
    arguments_done: bool = False
    saw_tool_call: bool = False


def _stream_error(message: str, kind: ErrorKind = ErrorKind.parse) -> AvaError:
    return AvaError(kind, message)


def _context_overflow_error() -> AvaError:
    return AvaError(
        ErrorKind.provider,
        "Codex request exceeded the model context window",
        "context_length_exceeded",
    )


def _is_overflow(error: object) -> bool:
    return isinstance(error, dict) and error.get("code") == "context_length_exceeded"


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _emit_usage(source: dict, sink: StreamSink) -> None:
    raw_input_details = source.get("input_tokens_details")
    input_details: dict = raw_input_details if isinstance(raw_input_details, dict) else {}
    raw_output_details = source.get("output_tokens_details")
    output_details: dict = raw_output_details if isinstance(raw_output_details, dict) else {}
    cached = _int_or_none(input_details.get("cached_tokens"))
    reasoning = _int_or_none(output_details.get("reasoning_tokens"))
    usage = Usage(
        cached_read=cached,
        cache_write=_int_or_none(input_details.get("cache_write_tokens")),
        reasoning=reasoning,
    )
    input_tokens = _int_or_none(source.get("input_tokens"))
    if input_tokens is not None:
        usage.input = input_tokens - min(input_tokens, cached or 0)
    output_tokens = _int_or_none(source.get("output_tokens"))
    if output_tokens is not None:
        usage.output = output_tokens - min(output_tokens, reasoning or 0)
    if usage.any():
        sink(StreamEvent(kind=StreamEventKind.usage, usage=usage))


def _start_tool(
    item: dict, sink: StreamSink, state: CodexStreamState, *, require_item_id: bool
) -> None:
    if state.tool_started or state.saw_tool_call:
        raise _stream_error(
            "Codex streamed multiple tool calls after Ava disabled them; retry or check endpoint compatibility",
            ErrorKind.provider,
        )
    call_id = item.get("call_id")
    name = item.get("name")
    item_id = item.get("id")
    if not call_id or not name or (require_item_id and not item_id):
        raise _stream_error(
            "Codex tool call is missing its identity; retry or check endpoint compatibility"
        )
    state.item_id = item_id or call_id
    state.call_id = call_id
    state.name = name
    state.tool_started = True
    sink(StreamEvent(kind=StreamEventKind.tool_call_start, id=call_id, name=name))
    arguments = item.get("arguments")
    if isinstance(arguments, str) and arguments:
        state.arguments = arguments
        sink(StreamEvent(kind=StreamEventKind.tool_call_delta, text=arguments, id=call_id))


def _append_arguments(
    state: CodexStreamState, item_id: str, sink: StreamSink, arguments: str
) -> None:
    if not state.tool_started or state.item_id != item_id:
        raise _stream_error(
            "Codex streamed tool arguments before the call identity; retry or check endpoint compatibility"
        )
    if not arguments.startswith(state.arguments):
        raise _stream_error(
            "Codex changed finalized tool arguments; retry or check endpoint compatibility"
        )
    suffix = arguments[len(state.arguments) :]
    state.arguments = arguments
    if suffix:
        sink(StreamEvent(kind=StreamEventKind.tool_call_delta, text=suffix, id=state.call_id))


def _finish_tool(item: dict, sink: StreamSink, state: CodexStreamState) -> None:
    arguments = item.get("arguments")
    if not isinstance(arguments, str):
        raise _stream_error(
            "Codex completed a tool call without arguments; retry or check endpoint compatibility"
        )
    if not state.tool_started:
        _start_tool(item, sink, state, require_item_id=False)
    else:
        item_id = item.get("id")
        if (
            (item_id is not None and item_id != state.item_id)
            or item.get("call_id") != state.call_id
            or item.get("name") != state.name
        ):
            raise _stream_error(
                "Codex changed a completed tool call; retry or check endpoint compatibility"
            )
        _append_arguments(state, state.item_id, sink, arguments)
    sink(StreamEvent(kind=StreamEventKind.tool_call_end, id=state.call_id))
    state.item_id = state.call_id = state.name = state.arguments = ""
    state.tool_started = False
    state.arguments_done = False
    state.saw_tool_call = True


def consume_codex_event(
    event: SseEvent, sink: StreamSink, state: CodexStreamState
) -> StopReason | None:
    if event.event == "error":
        raise _stream_error(
            "Codex reported an error while streaming; retry or check provider status",
            ErrorKind.provider,
        )
    if event.data == "[DONE]":
        raise _stream_error(
            "Codex stream ended without a terminal response; retry or check endpoint compatibility"
        )
    try:
        value = json.loads(event.data)
    except json.JSONDecodeError:
        value = None
    if not isinstance(value, dict) or not isinstance(value.get("type"), str):
        raise _stream_error(
            "Codex stream contained invalid JSON; retry or check endpoint compatibility"
        )
    kind = value["type"]
    if event.event and event.event != kind:
        raise _stream_error(
            "Codex SSE event name disagrees with its JSON type; retry or check endpoint compatibility"
        )

    if kind == "response.output_text.delta":
        sink(StreamEvent(kind=StreamEventKind.text_delta, text=str(value.get("delta", ""))))
    elif kind in ("response.output_item.added", "response.output_item.done"):
        item = value.get("item")
        if not isinstance(item, dict):
            raise _stream_error(
                "Codex stream contained invalid JSON; retry or check endpoint compatibility"
            )
        done = kind == "response.output_item.done"
        item_type = item.get("type")
        if item_type == "function_call":
            if done:
                _finish_tool(item, sink, state)
            else:
                _start_tool(item, sink, state, require_item_id=True)
        elif done and item_type == "reasoning":
            # Round-trip the provider's opaque state byte-identically: re-serialize the item exactly.
            sink(StreamEvent(kind=StreamEventKind.reasoning_item, text=_dumps(item)))
        elif item_type not in ("message", "reasoning"):
            raise _stream_error(
                "Codex returned an unsupported output item; retry or check model compatibility",
                ErrorKind.provider,
            )
    elif kind == "response.function_call_arguments.delta":
        if state.arguments_done:
            raise _stream_error(
                "Codex streamed tool arguments after finalizing them; retry or check endpoint compatibility"
            )
        item_id = str(value.get("item_id", ""))
        if not state.tool_started or state.item_id != item_id:
            raise _stream_error(
                "Codex streamed tool arguments before the call identity; retry or check endpoint compatibility"
            )
        delta = str(value.get("delta", ""))
        state.arguments += delta
        sink(StreamEvent(kind=StreamEventKind.tool_call_delta, text=delta, id=state.call_id))
    elif kind == "response.function_call_arguments.done":
        if state.arguments_done:
            raise _stream_error(
                "Codex finalized tool arguments more than once; retry or check endpoint compatibility"
            )
        _append_arguments(
            state, str(value.get("item_id", "")), sink, str(value.get("arguments", ""))
        )
        state.arguments_done = True
    elif kind in ("response.completed", "response.incomplete", "response.failed"):
        raw_response = value.get("response")
        response: dict = raw_response if isinstance(raw_response, dict) else {}
        usage = response.get("usage")
        if isinstance(usage, dict):
            _emit_usage(usage, sink)
        if state.tool_started:
            raise _stream_error(
                "Codex stopped before finishing a tool call; retry or check endpoint compatibility"
            )
        if kind == "response.failed":
            if _is_overflow(response.get("error")):
                raise _context_overflow_error()
            raise _stream_error(
                "Codex reported a failed response; retry or check provider status",
                ErrorKind.provider,
            )
        if kind == "response.incomplete":
            raw_details = response.get("incomplete_details")
            details: dict = raw_details if isinstance(raw_details, dict) else {}
            if details.get("reason") != "max_output_tokens":
                raise _stream_error(
                    "Codex returned an incomplete response; retry or check provider status",
                    ErrorKind.provider,
                )
            sink(StreamEvent(kind=StreamEventKind.done))
            return StopReason.max_tokens
        sink(StreamEvent(kind=StreamEventKind.done))
        return StopReason.tool_use if state.saw_tool_call else StopReason.end_turn
    return None


def codex_response_error(status: int, body: str = "") -> AvaError:
    """An allowlisted error code preserves recovery signals without retaining response bodies."""
    if status == 400:
        try:
            envelope = json.loads(body)
        except json.JSONDecodeError:
            envelope = None
        if isinstance(envelope, dict) and _is_overflow(envelope.get("error")):
            return _context_overflow_error()
    kind = ErrorKind.provider
    action = "check the endpoint and provider status"
    if status in (401, 403):
        kind = ErrorKind.auth
        action = "run 'codex login' and restart Ava"
    elif status == 429:
        kind = ErrorKind.rate_limited
        action = "wait and retry"
    return AvaError(kind, f"Codex request returned HTTP {status}; {action}")


# ---- Catalog and provider ---------------------------------------------------------------------


@dataclass(slots=True)
class CodexCatalogModel:
    id: str
    priority: int
    capabilities: ModelCapabilities = field(default_factory=ModelCapabilities)


def parse_codex_model_catalog(text: str) -> list[CodexCatalogModel]:
    """Only entries whose visibility is ``list``; capabilities are the endpoint's own facts."""
    try:
        response = json.loads(text)
    except json.JSONDecodeError:
        response = None
    models_json = response.get("models") if isinstance(response, dict) else None
    if not isinstance(models_json, list):
        raise AvaError(
            ErrorKind.parse,
            "Codex model list is invalid; run 'codex login' or check endpoint compatibility",
        )
    seen: set[str] = set()
    models: list[CodexCatalogModel] = []
    for source in models_json:
        if not isinstance(source, dict):
            continue
        slug = source.get("slug") or ""
        if not slug or source.get("visibility") != "list":
            continue
        if slug in seen:
            raise AvaError(
                ErrorKind.parse,
                "Codex model list contains duplicate ids; check endpoint compatibility",
            )
        seen.add(slug)
        capabilities = ModelCapabilities()
        context = _int_or_none(source.get("context_window"))
        if context is None:
            context = _int_or_none(source.get("max_context_window"))
        if context is not None and context > 0:
            capabilities.context_window_tokens = context
        if source.get("shell_type"):
            capabilities.supports_tools = True
        levels = source.get("supported_reasoning_levels")
        if isinstance(levels, list):
            efforts: list[str] = []
            for level in levels:
                effort = level.get("effort") if isinstance(level, dict) else None
                if isinstance(effort, str) and effort and effort not in efforts:
                    efforts.append(effort)
            capabilities.effort_values = efforts
        priority = _int_or_none(source.get("priority"))
        models.append(
            CodexCatalogModel(
                id=slug,
                priority=priority if priority is not None else 2**63 - 1,
                capabilities=capabilities,
            )
        )
    return models


def codex_default_model(catalog: list[CodexCatalogModel]) -> str:
    return min(catalog, key=lambda model: model.priority).id if catalog else ""


class CodexProvider(Provider):
    def __init__(self, selection: Selection, base_url: str, credential: CodexCredential) -> None:
        super().__init__(selection)
        self.id = selection.provider
        self.display_name = "Codex" if selection.provider == "codex" else self.id
        self._base_url = base_url.rstrip("/")
        self._credential = credential
        self._capabilities: dict[str, ModelCapabilities] = {}
        self._prompt_cache_key = secrets.token_hex(16)
        self._transport = Client()

    def _headers(self, accept: str) -> list[tuple[str, str]]:
        return [
            ("accept", accept),
            ("authorization", f"Bearer {self._credential.access_token}"),
            ("ChatGPT-Account-Id", self._credential.account_id),
        ]

    async def stream(
        self, context: Context, selected: Selection, sink: StreamSink, cancel: CancelToken = NEVER
    ) -> StopReason:
        body = codex_request_body(context, selected, prompt_cache_key=self._prompt_cache_key)
        request = Request(
            url=self._base_url + "/responses",
            headers=[("content-type", "application/json")] + self._headers("text/event-stream"),
            body=body,
        )
        state = CodexStreamState()
        stream_error: AvaError | None = None
        stop_reason: StopReason | None = None

        def on_event(event: SseEvent) -> None:
            nonlocal stream_error, stop_reason
            if stream_error is not None or stop_reason is not None:
                return
            try:
                consumed = consume_codex_event(event, sink, state)
            except AvaError as error:
                stream_error = error
                return
            if consumed is not None:
                stop_reason = consumed

        response = await self._transport.post_sse(request, on_event, cancel)
        if not 200 <= response.status < 300:
            raise codex_response_error(response.status, response.body)
        if stream_error is not None:
            raise stream_error
        if stop_reason is not None:
            return stop_reason
        raise _stream_error(
            "Codex stream ended without a terminal response; retry or check endpoint compatibility"
        )

    async def list_models(self, cancel: CancelToken = NEVER) -> list[str]:
        request = Request(
            url=f"{self._base_url}/models?client_version={CODEX_CLIENT_VERSION}",
            headers=self._headers("application/json"),
        )
        response = await self._transport.get(request, cancel)
        if not 200 <= response.status < 300:
            raise codex_response_error(response.status, response.body)
        catalog = parse_codex_model_catalog(response.body)
        self._capabilities = {model.id: model.capabilities for model in catalog}
        default_model = codex_default_model(catalog)
        if default_model:
            self.model_aliases.setdefault("default", default_model)
        return [model.id for model in catalog]

    def discovered_capabilities(self, model: str) -> ModelCapabilities:
        return self._capabilities.get(model, ModelCapabilities())

    async def aclose(self) -> None:
        await self._transport.aclose()
