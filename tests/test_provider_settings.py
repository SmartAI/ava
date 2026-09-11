"""Provider credentials and session selection through the real HTTP boundary."""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from ava.app.web.server import create_app
from ava.llm.credentials import save_api_key
from tests.test_web import _running_client


@pytest.fixture
def catalog_server():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            requests.append((self.path, self.headers.get("Authorization")))
            authenticated = self.headers.get("Authorization") == "Bearer valid-test-key"
            self.send_response(200 if authenticated else 401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"data":[{"id":"fixture"},{"id":"fixture-reasoning"}]}' if authenticated else b'{"error":"Invalid key"}')

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def connection(url, **extra):
    return {"family": "openai", "base_url": url, **extra}


async def test_provider_readiness_and_credentials_are_separate_from_defaults(home, project, catalog_server, monkeypatch):
    url, requests = catalog_server
    configuration = {
        "provider": "gateway", "model": "fixture-reasoning", "effort": "high",
        "providers": {
            "gateway": connection(url, models={"fixture-reasoning": {"effort_values": ["low", "high"]}}),
            "invalid": connection(url),
            "missing": connection(url),
            "shell": connection(url, api_key_env="AVA_TEST_SHELL_KEY"),
        },
    }
    (home / "settings.json").write_text(json.dumps(configuration))
    save_api_key("gateway", "valid-test-key")
    save_api_key("invalid", "bad-test-key")
    monkeypatch.setenv("AVA_TEST_SHELL_KEY", "valid-test-key")
    async with _running_client(create_app(project)) as client:
        settings = (await client.get("/api/settings")).json()
        entries = {entry["id"]: entry for entry in settings["providers"]}
        assert settings["default_selection"] == {
            "provider": "gateway", "model": "fixture-reasoning", "effort": "high"
        }
        assert entries["gateway"]["is_default"] and not entries["shell"]["is_default"]
        assert entries["gateway"]["valid"] and entries["gateway"]["has_stored_key"]
        assert entries["shell"]["credential_source"] == "Environment"
        assert entries["invalid"]["configured"] and not entries["invalid"]["valid"]
        assert entries["invalid"]["status"] == "Needs attention"
        assert not entries["missing"]["configured"]
        assert "valid-test-key" not in json.dumps(settings) and "bad-test-key" not in json.dumps(settings)
        assert (await client.post("/api/chats", json={"project_id": "workspace"})).status_code == 201
        listed = (await client.get("/api/chats/c1/models")).json()
        assert [entry["id"] for entry in listed["providers"]] == ["gateway", "shell"]
        assert listed["effort_values"] == ["low", "high"]
        saved = await client.put("/api/settings", json={
            "provider_type": "custom", "provider": "invalid", **connection(url), "api_key": "valid-test-key",
            # Old clients cannot accidentally update session or default selections.
            "chat_id": "c1", "model": "fixture", "effort": None,
        })
        assert saved.status_code == 200, saved.text
        assert saved.json()["default_selection"] == {
            "provider": "gateway", "model": "fixture-reasoning", "effort": "high"
        }
        assert json.loads((home / "settings.json").read_text()) == configuration
        assert next(entry for entry in saved.json()["providers"] if entry["id"] == "invalid")["valid"]
        chat = (await client.get("/api/chats/c1/models")).json()
        assert (chat["provider"], chat["model"], chat["effort"]) == ("gateway", "fixture-reasoning", "high")
        assert (await client.delete("/api/credentials/invalid")).status_code == 200
        assert "invalid" not in [entry["id"] for entry in (await client.get("/api/chats/c1/models")).json()["providers"]]
        assert all(path == "/v1/models" for path, _ in requests)


async def test_environment_sync_accepts_only_configured_provider_credentials(home, project, monkeypatch):
    configuration = {
        "providers": {
            "shell": connection("http://127.0.0.1:1/v1", api_key_env="AVA_TEST_SHELL_KEY"),
        }
    }
    (home / "settings.json").write_text(json.dumps(configuration))
    monkeypatch.setenv("AVA_TEST_SHELL_KEY", "old-key")
    async with _running_client(create_app(project)) as client:
        updated = await client.post(
            "/api/system/environment", json={"variables": {"AVA_TEST_SHELL_KEY": "new-key"}}
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["updated"] == ["AVA_TEST_SHELL_KEY"]
        assert os.environ["AVA_TEST_SHELL_KEY"] == "new-key"

        invalid = await client.post(
            "/api/system/environment", json={"variables": {"NOT_A_PROVIDER_KEY": "x"}}
        )
        assert invalid.status_code == 400
        assert "not a configured provider credential" in invalid.json()["error"]


async def test_selection_is_atomic_session_scoped_and_survives_restart(home, project, catalog_server):
    url, _ = catalog_server
    configuration = {
        "provider": "first", "model": "fixture",
        "providers": {
            "first": connection(url),
            "second": connection(url, models={"fixture-reasoning": {"effort_values": ["low", "high"], "context_window": 10000}}),
        },
    }
    (home / "settings.json").write_text(json.dumps(configuration))
    for name in ("first", "second"):
        save_api_key(name, "valid-test-key")
    chosen = {"provider": "second", "model": "fixture-reasoning", "effort": "high"}
    async with _running_client(create_app(project)) as client:
        for _ in range(2):
            assert (await client.post("/api/chats", json={"project_id": "workspace"})).status_code == 201
        for bad in (chosen | {"effort": "max"}, chosen | {"model": "unknown"}, chosen | {"provider": "unconfigured"}):
            response = await client.post("/api/chats/c1/model", json=bad)
            assert response.status_code == 400, response.text
            assert (await client.get("/api/chats/c1/models")).json()["provider"] == "first"
        response = await client.post("/api/chats/c1/model", json=chosen)
        assert response.status_code == 200 and response.json() == chosen
        agent = client.app.state.registry.find_chat("c1")[1].agent
        assert agent.state.provider.context_window == 10000
        other = (await client.get("/api/chats/c2/models")).json()
        assert other["provider"] == "first" and other["model"] == "fixture"
        assert json.loads((home / "settings.json").read_text()) == configuration
    # No message has been sent. The unapplied session choice must still restore.
    async with _running_client(create_app(project)) as client:
        restored = (await client.get("/api/chats/c1/models")).json()
        assert {key: restored[key] for key in chosen} == chosen
        assert (await client.post("/api/chats", json={"project_id": "workspace"})).status_code == 201
        assert (await client.get("/api/chats/c3/models")).json()["provider"] == "first"
        cleared = await client.post("/api/chats/c1/model", json={"model": "fixture"})
        assert cleared.json()["effort"] is None


async def test_empty_setup_can_open_a_conversation_but_not_select_unconfigured_models(home, project):
    async with _running_client(create_app(project)) as client:
        response = await client.post("/api/chats", json={"project_id": "workspace"})
        assert response.status_code == 201, response.text
        catalog = (await client.get("/api/chats/c1/models")).json()
        assert catalog["providers"] == [] and catalog["models"] == []
        response = await client.post("/api/chats/c1/model", json={"provider": "openai", "model": "gpt-5.4"})
        assert response.status_code == 400
        assert "API key" in response.json()["error"]
