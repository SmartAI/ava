"""Provider adapters against local fake endpoints, plus the settings resolver."""

from __future__ import annotations

import http.server
import json
import threading
from pathlib import Path

import pytest

from ava.base import AvaError
from ava.llm import (
    Context,
    Item,
    Role,
    Selection,
    SelectionOverride,
    StreamEventKind,
    ToolDef,
    ToolParam,
    ToolParamType,
    make_image_block,
    make_text_block,
    make_tool_call_block,
    make_tool_result_block,
    provider_from_environment,
    sort_model_ids,
)
from ava.llm.anthropic import AnthropicProvider, AnthropicSettings, request_body
from ava.llm.configuration import load_provider_settings
from ava.llm.openai import OpenAIProvider, openai_request_body
from ava.llm.provider import resolve_model_alias

ANTHROPIC_STREAM = (
    "event: message_start\n"
    'data: {"type":"message_start","message":{"usage":{"input_tokens":1200,"cache_read_input_tokens":300}}}\n\n'
    'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'
    'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"web "}}\n\n'
    'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"answer"}}\n\n'
    'event: content_block_start\ndata: {"type":"content_block_start","index":1,"content_block":{"type":"tool_use","id":"call_1","name":"read"}}\n\n'
    'event: content_block_delta\ndata: {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"{\\"path\\":"}}\n\n'
    'event: content_block_delta\ndata: {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"\\"a\\"}"}}\n\n'
    'event: content_block_stop\ndata: {"type":"content_block_stop","index":1}\n\n'
    'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"tool_use"},"usage":{"output_tokens":40}}\n\n'
    'event: message_stop\ndata: {"type":"message_stop"}\n\n'
)

OPENAI_STREAM = (
    'data: {"choices":[{"index":0,"delta":{"content":"hi "}}]}\n\n'
    'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_7","type":"function","function":{"name":"bash","arguments":""}}]}}]}\n\n'
    'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\"command\\":\\"ls\\"}"}}]}}]}\n\n'
    'data: {"choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}\n\n'
    'data: {"choices":[],"usage":{"prompt_tokens":100,"completion_tokens":30,"prompt_tokens_details":{"cached_tokens":40},"completion_tokens_details":{"reasoning_tokens":10}}}\n\n'
    "data: [DONE]\n\n"
)


class _Fake(http.server.BaseHTTPRequestHandler):
    requests: list[dict] = []
    stream = ""
    status = 200

    def do_GET(self):
        body = json.dumps(
            {"id": "m", "max_tokens": 128000, "data": [{"id": "claude-x"}], "has_more": False}
        ).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("content-length", "0"))
        _Fake.requests.append(
            {"headers": dict(self.headers), "body": json.loads(self.rfile.read(length))}
        )
        body = _Fake.stream.encode()
        self.send_response(_Fake.status)
        self.send_header("content-type", "text/event-stream")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def fake_server():
    _Fake.requests = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Fake)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


def _collect(events):
    def sink(event):
        events.append(event)

    return sink


async def test_anthropic_adapter_streams_text_tools_and_usage(fake_server: str):
    _Fake.stream = ANTHROPIC_STREAM
    provider = AnthropicProvider(
        Selection("anthropic", "claude-x"), AnthropicSettings(base_url=fake_server), "key"
    )
    context = Context(
        system_prompt="sys", items=[Item(role=Role.user, blocks=[make_text_block("q")])]
    )
    events = []
    stop = await provider.stream(context, provider.selection, _collect(events))
    assert stop.value == "tool_use"
    kinds = [event.kind for event in events]
    assert kinds == [
        StreamEventKind.usage,
        StreamEventKind.text_delta,
        StreamEventKind.text_delta,
        StreamEventKind.tool_call_start,
        StreamEventKind.tool_call_delta,
        StreamEventKind.tool_call_delta,
        StreamEventKind.tool_call_end,
        StreamEventKind.usage,
        StreamEventKind.done,
    ]
    assert (
        events[0].usage.input == 1200
        and events[0].usage.cached_read == 300
        and events[-2].usage.output == 40
    )
    sent = _Fake.requests[-1]
    assert sent["headers"]["x-api-key"] == "key" and sent["body"]["system"] == "sys"
    assert sent["body"]["max_tokens"] == 32000 and sent["body"]["stream"] is True
    await provider.aclose()


async def test_anthropic_non_2xx_never_reaches_the_parser(fake_server: str):
    _Fake.stream = ANTHROPIC_STREAM
    _Fake.status = 429
    try:
        provider = AnthropicProvider(
            Selection("anthropic", "claude-x"), AnthropicSettings(base_url=fake_server), "key"
        )
        events = []
        with pytest.raises(AvaError) as info:
            await provider.stream(Context(), provider.selection, _collect(events))
        assert info.value.kind.value == "rate_limited" and events == []
        await provider.aclose()
    finally:
        _Fake.status = 200


async def test_openai_adapter_streams_and_normalizes_usage(fake_server: str):
    _Fake.stream = OPENAI_STREAM
    provider = OpenAIProvider(Selection("openai", "gpt-5.4"), fake_server, "key")
    events = []
    stop = await provider.stream(Context(system_prompt="s"), provider.selection, _collect(events))
    assert stop.value == "tool_use"
    assert [e.kind for e in events] == [
        StreamEventKind.text_delta,
        StreamEventKind.tool_call_start,
        StreamEventKind.tool_call_delta,
        StreamEventKind.tool_call_end,
        StreamEventKind.usage,
        StreamEventKind.done,
    ]
    usage = events[-2].usage
    assert (usage.input, usage.cached_read, usage.output, usage.reasoning) == (60, 40, 20, 10)
    sent = _Fake.requests[-1]["body"]
    assert sent["messages"][0] == {"role": "system", "content": "s"}
    assert sent["stream_options"] == {"include_usage": True}
    assert _Fake.requests[-1]["headers"]["authorization"] == "Bearer key"
    await provider.aclose()


def _tool() -> ToolDef:
    return ToolDef(
        "read",
        "Read",
        [
            ToolParam("path", "p", ToolParamType.string, True),
            ToolParam("offset", "o", ToolParamType.integer, minimum=1),
        ],
    )


def test_request_bodies_serialize_blocks_in_order():
    context = Context(
        system_prompt="sys",
        items=[
            Item(
                role=Role.user,
                blocks=[
                    make_image_block("a.png", b"\x89PNG", "image/png"),
                    make_text_block('q "x"'),
                ],
            ),
            Item(role=Role.assistant, blocks=[make_tool_call_block("c1", "read", "not json")]),
            Item(role=Role.tool, blocks=[make_tool_result_block("c1", "out", True)]),
        ],
        tools=[_tool()],
    )
    anthropic = json.loads(request_body(context, "claude", 100))
    assert anthropic["messages"][0]["content"][0]["source"]["data"] == "iVBORw=="
    assert anthropic["messages"][1]["content"][0] == {
        "type": "tool_use",
        "id": "c1",
        "name": "read",
        "input": {},
    }
    assert anthropic["messages"][2]["content"][0]["is_error"] is True
    assert anthropic["tools"][0]["input_schema"]["required"] == ["path"]
    assert anthropic["tools"][0]["input_schema"]["properties"]["offset"]["minimum"] == 1
    openai = json.loads(openai_request_body(context, "gpt", "high"))
    assert openai["reasoning_effort"] == "high" and openai["parallel_tool_calls"] is False
    assert openai["messages"][1]["content"][0]["type"] == "image_url"
    assert openai["messages"][2]["tool_calls"][0]["function"]["arguments"] == "not json"
    assert openai["messages"][3] == {"role": "tool", "tool_call_id": "c1", "content": "out"}


def test_model_ordering_and_alias_resolution():
    models = [
        "claude-sonnet-4-20250514",
        "claude-opus-4",
        "claude-sonnet-4",
        "gpt-5.4-mini",
        "gpt-5.4",
    ]
    sort_model_ids(models)
    assert models.index("claude-sonnet-4") < models.index("claude-sonnet-4-20250514")
    assert resolve_model_alias("sonnet", models, {}) == "claude-sonnet-4"
    assert resolve_model_alias("sonnet", models, {"sonnet": "claude-opus-4"}) == "claude-opus-4"
    assert resolve_model_alias("default", ["only-one"], {}) == "only-one"
    assert resolve_model_alias("unknown-id", models, {}) == "unknown-id"


def test_settings_resolution_order(home: Path, monkeypatch: pytest.MonkeyPatch):
    settings_file = home / "settings.json"
    settings_file.write_text(
        json.dumps(
            {
                "provider": "my-gateway",
                "model": "company-model",
                "providers": {
                    "anthropic": {},
                    "openai": {},
                    "my-gateway": {
                        "family": "openai",
                        "base_url": "https://gateway.internal/v1/",
                        "api_key_env": "GATEWAY_KEY",
                        "models": {
                            "company-model": {
                                "context_window": 128000,
                                "effort_values": ["low", "high"],
                            }
                        },
                    },
                },
            }
        )
    )
    settings = load_provider_settings(SelectionOverride(), None)
    assert settings.family == "openai" and settings.base_url == "https://gateway.internal/v1"
    assert settings.model_overrides["company-model"].context_window_tokens == 128000
    monkeypatch.setenv("AVA_PROVIDER", "anthropic")
    settings = load_provider_settings(SelectionOverride(), None)
    assert (
        settings.selection == Selection("anthropic", "claude-sonnet-5")
        and settings.api_key_env == "ANTHROPIC_API_KEY"
    )
    resumed = Selection("openai", "gpt-5.4", "high")
    settings = load_provider_settings(SelectionOverride(), resumed)
    assert settings.selection == resumed
    settings = load_provider_settings(SelectionOverride(model="gpt-5.4-mini"), resumed)
    assert settings.selection == Selection("openai", "gpt-5.4-mini", "high")
    with pytest.raises(AvaError) as info:
        provider_from_environment(SelectionOverride(provider="openai"))
    assert info.value.kind.value == "auth"
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    provider = provider_from_environment(SelectionOverride(provider="openai"))
    assert provider.id == "openai" and provider.context_window == 1_050_000
