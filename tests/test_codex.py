"""The Codex adapter: credential validation, catalog policy, request shape, and stream mapping."""

from __future__ import annotations

import base64
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
    make_text_block,
)
from ava.llm.codex import (
    CodexCredential,
    CodexProvider,
    codex_request_body,
    load_codex_credential,
    parse_codex_credential,
    parse_codex_model_catalog,
)
from ava.llm.registry import provider_from_environment
from ava.llm.types import (
    Provenance,
    make_reasoning_block,
    make_tool_call_block,
    make_tool_result_block,
)


def _segment(value: dict) -> str:
    return (
        base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode())
        .rstrip(b"=")
        .decode()
    )


def jwt(payload: dict) -> str:
    return f"{_segment({'alg': 'none'})}.{_segment(payload)}.c2ln"


def auth_json(exp: int = 4_102_444_800, account_id: str | None = None) -> str:
    tokens = {
        "access_token": jwt({"exp": exp}),
        "id_token": jwt({"https://api.openai.com/auth": {"chatgpt_account_id": "acct-test"}}),
        "refresh_token": "ignored",
    }
    if account_id is not None:
        tokens["account_id"] = account_id
    return json.dumps({"auth_mode": "chatgpt", "tokens": tokens})


def test_credential_parsing_fails_closed():
    credential = parse_codex_credential(auth_json(), now=1_000)
    assert credential == CodexCredential(
        access_token=jwt({"exp": 4_102_444_800}), account_id="acct-test"
    )
    assert (
        parse_codex_credential(auth_json(account_id="explicit"), now=1_000).account_id == "explicit"
    )
    for bad in (
        "not json",
        json.dumps({"auth_mode": "apikey"}),
        auth_json(exp=500),
        auth_json(account_id="has space"),
        json.dumps(
            {"auth_mode": "chatgpt", "tokens": {"access_token": "x.y", "id_token": jwt({})}}
        ),
    ):
        with pytest.raises(AvaError) as info:
            parse_codex_credential(bad, now=1_000)
        assert info.value.kind.value == "auth" and "codex login" in info.value.message
        assert "acct" not in info.value.message and "ey" not in info.value.detail


def test_credential_loading_follows_codex_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text(auth_json())
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    assert load_codex_credential().account_id == "acct-test"
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "missing"))
    with pytest.raises(AvaError):
        load_codex_credential()


def test_catalog_keeps_listed_models_and_endpoint_facts():
    catalog = parse_codex_model_catalog(
        json.dumps(
            {
                "models": [
                    {
                        "slug": "gpt-secondary",
                        "visibility": "list",
                        "priority": 20,
                        "context_window": 128000,
                    },
                    {
                        "slug": "gpt-default",
                        "visibility": "list",
                        "priority": 10,
                        "context_window": 272000,
                        "shell_type": "unified_exec",
                        "supported_reasoning_levels": [
                            {"effort": "low"},
                            {"effort": "high"},
                            {"effort": "low"},
                        ],
                    },
                    {"slug": "hidden", "visibility": "hide", "priority": 0},
                ]
            }
        )
    )
    assert [model.id for model in catalog] == ["gpt-secondary", "gpt-default"]
    assert catalog[1].capabilities.context_window_tokens == 272000
    assert catalog[1].capabilities.supports_tools is True
    assert catalog[1].capabilities.effort_values == ["low", "high"]
    assert catalog[0].capabilities.supports_tools is None
    from ava.llm.codex import codex_default_model

    assert codex_default_model(catalog) == "gpt-default"


def test_request_body_replays_reasoning_only_for_the_same_model():
    selected = Selection("codex", "gpt-default", "high")
    same = Item(role=Role.assistant, provenance=Provenance("codex", "gpt-default"))
    same.blocks = [
        make_reasoning_block('{"type":"reasoning","id":"rs-1","encrypted_content":"opaque"}'),
        make_tool_call_block("call-1", "read", '{"path":"a"}'),
    ]
    other = Item(role=Role.assistant, provenance=Provenance("codex", "gpt-old"))
    other.blocks = [
        make_reasoning_block('{"type":"reasoning","id":"rs-0"}'),
        make_text_block("earlier"),
    ]
    context = Context(
        system_prompt="sys",
        items=[
            Item(role=Role.user, blocks=[make_text_block("q")]),
            other,
            same,
            Item(role=Role.tool, blocks=[make_tool_result_block("call-1", "out", False)]),
        ],
    )
    body = json.loads(codex_request_body(context, selected))
    assert body["model"] == "gpt-default" and body["instructions"] == "sys"
    assert (
        body["store"] is False and body["stream"] is True and body["parallel_tool_calls"] is False
    )
    assert body["reasoning"] == {"effort": "high"} and body["include"] == [
        "reasoning.encrypted_content"
    ]
    assert [item["type"] for item in body["input"]] == [
        "message",
        "message",
        "reasoning",
        "function_call",
        "function_call_output",
    ]
    assert body["input"][2] == {"type": "reasoning", "id": "rs-1", "encrypted_content": "opaque"}
    assert body["input"][3]["call_id"] == "call-1"


def _sse(payload: dict) -> str:
    return f"event: {payload['type']}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"


class _Codex(http.server.BaseHTTPRequestHandler):
    posts: list[dict] = []
    gets: list[dict] = []

    def _send(self, body: str, content_type: str, status: int = 200) -> None:
        encoded = body.encode()
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self):
        _Codex.gets.append({"path": self.path, "headers": dict(self.headers)})
        self._send(
            json.dumps(
                {
                    "models": [
                        {
                            "slug": "gpt-secondary",
                            "visibility": "list",
                            "priority": 20,
                            "context_window": 128000,
                        },
                        {
                            "slug": "gpt-default",
                            "visibility": "list",
                            "priority": 10,
                            "context_window": 272000,
                            "shell_type": "unified_exec",
                            "supported_reasoning_levels": [{"effort": "low"}, {"effort": "high"}],
                        },
                    ]
                }
            ),
            "application/json",
        )

    def do_POST(self):
        length = int(self.headers.get("content-length", "0"))
        request = json.loads(self.rfile.read(length))
        _Codex.posts.append({"headers": dict(self.headers), "body": request})
        if len(_Codex.posts) == 1:
            arguments = json.dumps(
                {"path": "sample.txt", "offset": 1, "limit": 2}, separators=(",", ":")
            )
            split = len(arguments) // 2
            body = "".join(
                [
                    _sse(
                        {
                            "type": "response.output_item.done",
                            "item": {
                                "id": "rs-1",
                                "type": "reasoning",
                                "encrypted_content": "opaque-test",
                            },
                        }
                    ),
                    _sse(
                        {
                            "type": "response.output_item.added",
                            "item": {
                                "id": "fc-1",
                                "type": "function_call",
                                "call_id": "call-1",
                                "name": "read",
                                "arguments": "",
                            },
                        }
                    ),
                    _sse(
                        {
                            "type": "response.function_call_arguments.delta",
                            "item_id": "fc-1",
                            "delta": arguments[:split],
                        }
                    ),
                    _sse(
                        {
                            "type": "response.function_call_arguments.delta",
                            "item_id": "fc-1",
                            "delta": arguments[split:],
                        }
                    ),
                    _sse(
                        {
                            "type": "response.function_call_arguments.done",
                            "item_id": "fc-1",
                            "arguments": arguments,
                        }
                    ),
                    _sse(
                        {
                            "type": "response.output_item.done",
                            "item": {
                                "id": "fc-1",
                                "type": "function_call",
                                "call_id": "call-1",
                                "name": "read",
                                "arguments": arguments,
                            },
                        }
                    ),
                    _sse(
                        {
                            "type": "response.completed",
                            "response": {
                                "usage": {
                                    "input_tokens": 120,
                                    "input_tokens_details": {"cached_tokens": 20},
                                    "output_tokens": 30,
                                    "output_tokens_details": {"reasoning_tokens": 10},
                                }
                            },
                        }
                    ),
                ]
            )
        else:
            body = "".join(
                [
                    _sse(
                        {
                            "type": "response.output_item.added",
                            "item": {"id": "msg-1", "type": "message"},
                        }
                    ),
                    _sse(
                        {
                            "type": "response.output_text.delta",
                            "item_id": "msg-1",
                            "delta": "codex ",
                        }
                    ),
                    _sse(
                        {
                            "type": "response.output_text.delta",
                            "item_id": "msg-1",
                            "delta": "answer",
                        }
                    ),
                    _sse(
                        {
                            "type": "response.completed",
                            "response": {"usage": {"input_tokens": 10, "output_tokens": 2}},
                        }
                    ),
                ]
            )
        self._send(body, "text/event-stream")

    def log_message(self, *args):
        pass


@pytest.fixture
def codex_server():
    _Codex.posts = []
    _Codex.gets = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Codex)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


async def test_codex_provider_streams_tools_reasoning_and_usage(codex_server: str):
    credential = CodexCredential(access_token="access-test", account_id="acct-test")
    provider = CodexProvider(Selection("codex", "default"), codex_server + "/", credential)
    models = await provider.list_models()
    assert models == ["gpt-secondary", "gpt-default"]
    assert provider.model_aliases["default"] == "gpt-default"
    assert _Codex.gets[0]["path"] == "/models?client_version=0.150.1"
    assert _Codex.gets[0]["headers"]["ChatGPT-Account-Id"] == "acct-test"
    assert provider.capabilities("gpt-default").effort_values == ["low", "high"]

    events = []
    selected = Selection("codex", "gpt-default", "high")
    stop = await provider.stream(
        Context(system_prompt="sys", items=[Item(role=Role.user, blocks=[make_text_block("q")])]),
        selected,
        events.append,
    )
    assert stop.value == "tool_use"
    kinds = [event.kind for event in events]
    assert kinds == [
        StreamEventKind.reasoning_item,
        StreamEventKind.tool_call_start,
        StreamEventKind.tool_call_delta,
        StreamEventKind.tool_call_delta,
        StreamEventKind.tool_call_end,
        StreamEventKind.usage,
        StreamEventKind.done,
    ]
    assert json.loads(events[0].text) == {
        "id": "rs-1",
        "type": "reasoning",
        "encrypted_content": "opaque-test",
    }
    assert "".join(
        e.text for e in events if e.kind == StreamEventKind.tool_call_delta
    ) == json.dumps({"path": "sample.txt", "offset": 1, "limit": 2}, separators=(",", ":"))
    usage = events[-2].usage
    assert (usage.input, usage.cached_read, usage.output, usage.reasoning) == (100, 20, 20, 10)
    sent = _Codex.posts[0]
    assert (
        sent["headers"]["authorization"] == "Bearer access-test"
        and sent["headers"]["ChatGPT-Account-Id"] == "acct-test"
    )
    assert (
        sent["body"]["reasoning"] == {"effort": "high"}
        and sent["body"]["input"][0]["type"] == "message"
    )

    events = []
    stop = await provider.stream(Context(), selected, events.append)
    assert stop.value == "end_turn"
    assert "".join(e.text for e in events if e.kind == StreamEventKind.text_delta) == "codex answer"
    await provider.aclose()


def test_registry_builds_codex_from_cli_credential(home: Path, monkeypatch: pytest.MonkeyPatch):
    codex_home = home / "codex"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text(auth_json())
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    provider = provider_from_environment(SelectionOverride(provider="codex"))
    assert isinstance(provider, CodexProvider)
    assert provider.selection.model == "default" and provider.selection_model_may_be_alias
    (home / "settings.json").write_text(
        json.dumps({"providers": {"codex": {"base_url": "https://evil.invalid"}}})
    )
    with pytest.raises(AvaError):
        provider_from_environment(SelectionOverride(provider="codex"))
