"""The Web UI routes, the loopback fence, message routing, and the replay-first event stream."""

from __future__ import annotations

import asyncio
import base64
import json
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

from ava.app.web.events import blocks_json
from ava.app.web.server import create_app
from ava.llm import Item, Role, Selection, make_reasoning_block
from ava.session import Log, StepStart, TurnStart
from tests.conftest import ScriptedProvider, text_response, tool_call_response

PNG_2X3 = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAIAAAAD")


async def test_analytics_dashboard_reports_durable_usage(client, scripted):
    from ava.llm import Usage

    response = await client.get('/api/analytics', params={'days': 7, 'timezone': 'UTC'})
    assert response.status_code == 200, response.text
    assert len(response.json()['days']) == 7
    assert response.json()['totals']['tokens'] == 0
    await client.post('/api/chats', json={'project_id': 'workspace'})
    scripted[0].scripts = [text_response('Accounted', usage=Usage(input=300, cached_read=100, output=50))]
    assert (await client.post('/api/chats/c1/messages', json={'text': 'Measure this request'})).status_code == 202
    deadline = asyncio.get_running_loop().time() + 2
    while True:
        report = (await client.get('/api/analytics')).json()
        if report['totals']['tokens'] == 450:
            break
        assert asyncio.get_running_loop().time() < deadline, report
        await asyncio.sleep(.02)
    assert report['totals']['responses'] == 1 and not report['totals']['missing_usage']
    unchanged = (await client.get('/api/analytics', params={'revision': report['revision']})).json()
    assert unchanged['unchanged'] and 'days' not in unchanged
    assert (await client.get('/api/analytics', params={'timezone': 'not-a-zone'})).status_code == 400
    assert (await client.get('/api/analytics', params={'days': 90})).status_code == 400
    assert (await client.get('/api/analytics', headers={'origin':'https://untrusted.example'})).status_code == 403
    await client.post('/api/projects/workspace/hide')
    assert (await client.get('/api/analytics')).json()['totals']['tokens'] == 0
    assert (await client.get('/api/analytics', params={'project':'workspace'})).status_code == 404


async def test_damaged_analytics_cache_does_not_prevent_backend_startup(home, project, scripted):
    cache = home/'analytics.sqlite3'
    cache.write_bytes(b'An interrupted or damaged derived cache')
    app = create_app(project)
    async with app.router.lifespan_context(app):
        assert app.state.analytics.index is not None
        report = await app.state.analytics.run(app.state.analytics.index.query)
        assert report['totals']['tokens'] == 0


@pytest.fixture
def scripted(monkeypatch: pytest.MonkeyPatch):
    providers: list[ScriptedProvider] = []

    def factory(*args, **kwargs):
        provider = ScriptedProvider(
            [
                text_response("web answer"),
                tool_call_response("c1", "bash", json.dumps({"command": "echo hi"})),
                text_response("after tool"),
            ]
        )
        requested = args[0] if args else None
        if requested is not None and getattr(requested, "provider", None):
            provider.id = requested.provider
            provider.selection = Selection(
                requested.provider,
                requested.model or "scripted-model",
                requested.effort,
            )
        providers.append(provider)
        return provider

    monkeypatch.setattr("ava.app.web.server.provider_from_environment", factory)
    return providers


@asynccontextmanager
async def _running_client(app):
    """Run an app on loopback so SSE and lifespan behavior match the browser-facing server."""
    from ava.app.web.server import bind, create_server

    sock = bind(0)
    server = create_server(app, sock)
    task = asyncio.create_task(server.serve(sockets=[sock]))
    while not server.started:
        await asyncio.sleep(0.01)
    port = sock.getsockname()[1]
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=10) as client:
            client.headers["host"] = f"127.0.0.1:{port}"
            client.port = port  # type: ignore[attr-defined]
            client.app = app  # type: ignore[attr-defined]
            yield client
    finally:
        server.should_exit = True
        await task


@pytest.fixture
async def client(home: Path, project: Path, scripted):
    """A real loopback server so the event stream is exercised over HTTP, not a buffered ASGI shim."""
    async with _running_client(create_app(project)) as running:
        yield running
    assert all(provider.closed for provider in scripted)


async def _sse_events(response: httpx.Response):
    """Session events as their JSON payload; unkeyed ``status`` messages as ``{"kind": "status"}``."""
    name = ""
    async for line in response.aiter_lines():
        if line.startswith("event: "):
            name = line[7:]
        elif line.startswith("data: "):
            event = json.loads(line[6:])
            if name == "status":
                event = {"kind": "status", **event}
            name = ""
            yield event


async def _events_until(
    client: httpx.AsyncClient, chat_id: str, stop, last: str | None = None
) -> list[dict]:
    """Read the stream until ``stop`` (an event kind or a predicate) matches."""
    done = stop if callable(stop) else (lambda event: event["kind"] == stop)
    headers = {"last-event-id": last} if last is not None else {}
    events: list[dict] = []
    async with client.stream("GET", f"/api/chats/{chat_id}/events", headers=headers) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        async for event in _sse_events(response):
            events.append(event)
            if done(event):
                break
    return events


def _kinds(events: list[dict], *, with_status: bool = False) -> list[str]:
    return [event["kind"] for event in events if with_status or event["kind"] != "status"]


def test_reasoning_summary_crosses_the_web_boundary_without_opaque_state():
    block = make_reasoning_block(
        '{"type":"reasoning","encrypted_content":"secret"}', "Checked the parser"
    )
    assert blocks_json(Item(role=Role.assistant, blocks=[block])) == [
        {"kind": "reasoning", "summary": "Checked the parser"}
    ]


async def test_fence_rejects_foreign_host_and_origin(client: httpx.AsyncClient):
    forbidden = await client.get("/api/projects", headers={"host": "example.invalid"})
    assert forbidden.status_code == 403 and forbidden.json() == {"error": "forbidden request"}
    cross = await client.post(
        "/api/chats/c1/messages", headers={"origin": "https://example.invalid"}, content=b"{}"
    )
    assert cross.status_code == 403
    same = await client.get("/api/projects", headers={"origin": f"http://127.0.0.1:{client.base_url.port}"})
    assert same.status_code == 200
    index = await client.get("/")
    assert index.status_code == 200
    assert index.text.startswith("<!doctype html>")
    assert "<title>ava</title>" in index.text and "katex" in index.text
    assert '<link rel="icon" href="/favicon.svg" type="image/svg+xml" />' in index.text
    assert '<div id="root"></div>' in index.text
    assert "@AVA_REACT_CSS@" not in index.text and "@AVA_REACT_JS@" not in index.text
    for path in ("/favicon.svg", "/favicon.ico"):
        favicon = await client.get(path)
        assert favicon.status_code == 200
        assert favicon.headers["content-type"] == "image/svg+xml"
        assert b"AVA monogram" in favicon.content


async def test_settings_route_saves_custom_provider_configuration(
    client: httpx.AsyncClient, home: Path
):
    initial = (await client.get("/api/settings")).json()
    assert "model" not in initial and "effort" not in initial
    assert all(not entry["valid"] for entry in initial["providers"])
    assert {item["id"] for item in initial["built_in_providers"]} == {
        "anthropic",
        "openai",
        "deepseek",
        "codex",
        "llamacpp",
    }

    saved = await client.put(
        "/api/settings",
        json={
            "provider_type": "custom",
            "provider": "company-gateway",
            "family": "openai",
            "base_url": "https://gateway.example.com/v1/",
            "model": "company-model",
            "effort": None,
            "api_key": None,
        },
    )
    assert saved.status_code == 200
    entry = next(item for item in saved.json()["providers"] if item["id"] == "company-gateway")
    assert entry["provider_type"] == "custom"
    assert entry["base_url"] == "https://gateway.example.com/v1"
    assert entry["status"] == "Not configured" and not entry["valid"]
    assert saved.json()["saved_provider"] == "company-gateway"
    settings_text = (home / "settings.json").read_text()
    document = json.loads(settings_text)
    assert document["providers"]["company-gateway"] == {
        "family": "openai",
        "base_url": "https://gateway.example.com/v1",
    }
    assert "provider" not in document and "model" not in document
    assert "api_key" not in saved.json()
    assert (await client.get("/api/settings")).headers["cache-control"] == "no-store"

    invalid = await client.put(
        "/api/settings",
        json={
            "provider_type": "custom",
            "provider": "openai",
            "family": "openai",
            "base_url": "https://gateway.example.com/v1",
            "model": "model",
        },
    )
    assert invalid.status_code == 400
    assert "reserved" in invalid.json()["error"]


async def test_settings_do_not_change_the_current_idle_chat(client: httpx.AsyncClient, scripted):
    assert (await client.post("/api/chats", json={"project_id": "workspace"})).status_code == 201
    original = scripted[0]
    saved = await client.put(
        "/api/settings",
        json={
            "provider_type": "custom",
            "provider": "company-gateway",
            "family": "openai",
            "base_url": "https://gateway.example.com/v1",
            "model": "company-model",
            "effort": "high",
            "chat_id": "c1",
        },
    )
    assert saved.status_code == 200
    assert "selection" not in saved.json()
    assert original.closed is False
    assert client_agent(client, "c1").state.provider is original
    assert original.selection == Selection("scripted", "scripted-model")


async def test_projects_chats_and_archive(client: httpx.AsyncClient, project: Path):
    (project / ".hidden").mkdir()
    (project / "alpha").mkdir()
    listing = (await client.get("/api/fs", params={"path": str(project)})).json()
    assert [entry["name"] for entry in listing["entries"]] == [".hidden", "alpha"]
    projects = (await client.get("/api/projects")).json()["projects"]
    assert projects[0]["id"] == "workspace" and projects[0]["chats"] == []
    added = await client.post("/api/projects", json={"path": str(project)})
    assert (
        added.status_code == 200 and added.json()["id"] == "workspace"
    )  # same directory as the workspace
    other = project.parent / "other"
    other.mkdir()
    added = await client.post("/api/projects", json={"path": str(other)})
    assert added.status_code == 201 and added.json()["id"] == "p1"
    assert (
        await client.post("/api/projects", json={"path": "/definitely/missing"})
    ).status_code == 400
    assert (await client.post("/api/chats", json={"project_id": "nope"})).status_code == 404
    created = await client.post("/api/chats", json={"project_id": "p1"})
    assert created.status_code == 201 and created.json() == {
        "id": "c1",
        "title": "",
        "status": "idle",
        "archived": False,
        "started_at": "", "completed_at": "", "completion_seq": -1,
        "completion_reason": "", "reviewed_through": -1,
    }
    renamed = await client.patch("/api/chats/c1", json={"title": "中文界面 review"})
    assert renamed.status_code == 200 and renamed.json()["title"] == "中文界面 review"
    for invalid in ("", "  ", "a" * 201, 42):
        assert (await client.patch("/api/chats/c1", json={"title": invalid})).status_code == 400
    assert (await client.patch("/api/chats/missing", json={"title": "Title"})).status_code == 404
    archived = await client.post("/api/chats/c1/archive", json={"archived": True})
    assert archived.json()["archived"] is True
    refused = await client.post("/api/chats/c1/messages", json={"text": "must not run"})
    assert refused.status_code == 409 and refused.json() == {"error": "chat is archived"}
    assert (await client.post("/api/chats/c1/archive", json={"archived": "yes"})).status_code == 400
    assert (await client.post("/api/chats/c1/archive", json={"archived": False})).json()[
        "archived"
    ] is False
    opened = (await client.get("/api/chats/c1")).json()
    assert opened["project_id"] == "p1" and opened["cwd"] == str(other) and opened["events"] == []


async def test_only_unused_chats_can_be_removed(
    client: httpx.AsyncClient, home: Path, scripted
):
    empty = await client.post("/api/chats", json={"project_id": "workspace"})
    assert empty.status_code == 201
    empty_id = empty.json()["id"]
    empty_chat = client_agent(client, empty_id)
    session_id = empty_chat.session_id
    session_path = empty_chat.session_path
    assert session_id and session_path and session_path.is_file()

    used = await client.post("/api/chats", json={"project_id": "workspace"})
    used_id = used.json()["id"]
    accepted = await client.post(
        f"/api/chats/{used_id}/messages", json={"text": "keep this chat"}
    )
    assert accepted.status_code == 202
    refused = await client.delete(f"/api/chats/{used_id}")
    assert refused.status_code == 409
    assert refused.json() == {"error": "only an unused chat can be removed"}
    assert (await client.delete("/api/chats/missing")).status_code == 404

    removed = await client.delete(f"/api/chats/{empty_id}")
    assert removed.status_code == 204 and removed.content == b""
    assert scripted[0].closed
    assert not session_path.exists()
    assert (await client.get(f"/api/chats/{empty_id}")).status_code == 404
    projects = (await client.get("/api/projects")).json()["projects"]
    assert not any(chat["id"] == empty_id for project in projects for chat in project["chats"])
    assert session_id not in json.loads((home / "web.json").read_text())["sessions"]


async def test_projects_and_sessions_survive_a_server_restart(home: Path, project: Path, scripted):
    other = project.parent / "remembered-project"
    other.mkdir()

    async with _running_client(create_app(project)) as first:
        added = await first.post("/api/projects", json={"path": str(other)})
        assert added.status_code == 201
        created = await first.post("/api/chats", json={"project_id": added.json()["id"]})
        chat_id = created.json()["id"]
        accepted = await first.post(
            f"/api/chats/{chat_id}/messages", json={"text": "remember this history"}
        )
        assert accepted.status_code == 202
        events = await _events_until(first, chat_id, "turn/end")
        renamed = await first.patch(f"/api/chats/{chat_id}", json={"title": "Renamed durable chat"})
        assert renamed.status_code == 200
        archived = await first.post(f"/api/chats/{chat_id}/archive", json={"archived": True})
        assert archived.json()["archived"] is True
        completed = archived.json()
        assert completed["completion_seq"] == events[-1]["seq"]

    async with _running_client(create_app(project)) as second:
        projects = (await second.get("/api/projects")).json()["projects"]
        remembered = next(item for item in projects if item["path"] == str(other))
        assert remembered["chats"] == [
            {
                "id": chat_id,
                "title": "Renamed durable chat",
                "status": "idle",
                "archived": True,
                "started_at": completed["started_at"],
                "completed_at": completed["completed_at"], "completion_seq": events[-1]["seq"],
                "completion_reason": "completed", "reviewed_through": -1,
            }
        ]
        opened = (await second.get(f"/api/chats/{chat_id}")).json()
        assert opened["project_id"] == remembered["id"]
        assert opened["cwd"] == str(other)
        replay = await _events_until(second, chat_id, "turn/end")
        assert any(
            event["kind"] == "step/claimed"
            and event["messages"][0]["blocks"][-1]["text"] == "remember this history"
            for event in replay
        )
        assert any(
            event["kind"] == "assistant/message"
            and event["blocks"] == [{"kind": "text", "text": "web answer"}]
            for event in replay
        )

    assert (home / "web.json").is_file()
    assert (home / "web.json").stat().st_mode & 0o777 == 0o600
    assert all(provider.closed for provider in scripted)


async def test_hidden_projects_keep_history_and_restore_without_automatic_rediscovery(
    home: Path, project: Path, scripted, monkeypatch
):
    from ava.base import AvaError, ErrorKind

    original = project / "keep.txt"
    original.write_bytes(b"Project files must not be changed.\n")
    async with _running_client(create_app(project)) as first:
        project_id = (await first.get("/api/projects")).json()["projects"][0]["id"]
        created = await first.post("/api/chats", json={"project_id": project_id})
        chat_id = created.json()["id"]
        await first.post(f"/api/chats/{chat_id}/messages", json={"text": "Preserved history"})
        await _events_until(first, chat_id, "turn/end")
        archived = (await first.post("/api/chats", json={"project_id": project_id})).json()["id"]
        await first.post(f"/api/chats/{archived}/archive", json={"archived": True})
        registry = first.app.state.registry
        expected = registry.projects[0].summary()
        task = registry.find_chat(chat_id)[1].task
        if task is not None:
            await task
        before = {p: p.read_bytes() for p in home.rglob("*.jsonl*")}
        assert before
        revision = (await first.get("/api/system")).json()["navigation_revision"]
        for _ in range(2):
            assert (await first.post(f"/api/projects/{project_id}/hide")).status_code == 200
        assert (await first.post("/api/projects/missing/hide")).status_code == 404
        assert (await first.get("/api/projects")).json()["projects"] == []
        assert (await first.get("/api/system")).json()["navigation_revision"] == revision + 1
        assert (await first.post("/api/chats", json={"project_id": project_id})).status_code == 404
        # Launch/connection discovery must not undo an explicit user removal.
        await first.post("/api/projects", json={"path": str(project), "restore": False})
        assert (await first.get("/api/projects")).json()["projects"] == []
        assert {p: p.read_bytes() for p in before} == before
        assert original.read_bytes() == b"Project files must not be changed.\n"

    async with _running_client(create_app(project)) as second:
        assert (await second.get("/api/projects")).json()["projects"] == []
        registry = second.app.state.registry
        persist = registry.persist

        def failed_persist():
            raise AvaError(ErrorKind.io, "Disk unavailable")

        with monkeypatch.context() as patch:
            patch.setattr(registry, "persist", failed_persist)
            failed = await second.post("/api/projects", json={"path": str(project)})
            assert failed.status_code == 503
            assert registry.projects[0].hidden
        restored = await second.post("/api/projects", json={"path": str(project)})
        assert restored.json() == {**expected, "revision": registry.revision}
        assert len((await second.get("/api/projects")).json()["projects"]) == 1
        with monkeypatch.context() as patch:
            patch.setattr(registry, "persist", failed_persist)
            assert (await second.post(f"/api/projects/{project_id}/hide")).status_code == 503
            assert not registry.projects[0].hidden
        assert registry.persist == persist
        assert {p: p.read_bytes() for p in before} == before
        replay = await _events_until(second, chat_id, "turn/end")
        assert any(event["kind"] == "assistant/message" for event in replay)


async def test_session_review_survives_restart_without_acknowledging_a_new_result(
    home: Path, project: Path, scripted, monkeypatch
):
    from ava.base import AvaError, ErrorKind

    async def summary(client):
        return (await client.get("/api/projects")).json()["projects"][0]["chats"][0]

    async with _running_client(create_app(project)) as client:
        project_id = (await client.get("/api/projects")).json()["projects"][0]["id"]
        chat = (await client.post("/api/chats", json={"project_id": project_id})).json()
        endpoint = f"/api/chats/{chat['id']}"
        assert chat["completion_seq"] == chat["reviewed_through"] == -1
        assert (await client.post(endpoint + "/review", json={"through": 0})).status_code == 409
        await client.post(endpoint + "/messages", json={"text": "First result"})
        events = await _events_until(client, chat["id"], "turn/end")
        sequence = events[-1]["seq"]
        assert (await client.get(endpoint)).status_code == 200
        assert (await summary(client))["reviewed_through"] == -1
        for invalid in ({}, {"through": -1}, {"through": True}, {"through": "1"}):
            assert (await client.post(endpoint + "/review", json=invalid)).status_code == 400
        registry = client.app.state.registry

        def failed_persist():
            raise AvaError(ErrorKind.io, "Disk unavailable")

        with monkeypatch.context() as patch:
            patch.setattr(registry, "persist", failed_persist)
            failed = await client.post(endpoint + "/review", json={"through": sequence})
            assert failed.status_code == 503
            assert (await summary(client))["reviewed_through"] == -1
        reviewed = (await client.post(endpoint + "/review", json={"through": sequence})).json()
        assert reviewed["reviewed_through"] == reviewed["completion_seq"] == sequence
        assert reviewed["completed_at"] and reviewed["started_at"] <= reviewed["completed_at"]

    async with _running_client(create_app(project)) as client:
        restored = await summary(client)
        assert restored["reviewed_through"] == restored["completion_seq"] == sequence
        assert restored["completed_at"] == reviewed["completed_at"]
        await client.post(endpoint + "/messages", json={"text": "Second result"})
        events = await _events_until(client, chat["id"], lambda e: e["kind"] == "turn/end" and e["seq"] > sequence)
        new_sequence = events[-1]["seq"]
        # A delayed request from another desktop cannot review a result it never saw.
        stale = (await client.post(endpoint + "/review", json={"through": sequence})).json()
        assert stale["completion_seq"] == new_sequence > stale["reviewed_through"] == sequence
        assert (await client.post(endpoint + "/review", json={"through": new_sequence + 1})).status_code == 409
        summaries = (await client.get("/api/projects")).json()["projects"][0]["chats"]
        assert summaries[0]["completion_seq"] == new_sequence
        await client.post(f"/api/projects/{project_id}/hide")
        assert (await client.post(endpoint + "/review", json={"through": new_sequence})).status_code == 404
        assert (await client.get("/api/projects")).json()["projects"] == []
        await client.post("/api/projects", json={"path": str(project)})
        assert (await summary(client))["reviewed_through"] == sequence
        scripted[-1].scripts = [AvaError(ErrorKind.invalid_argument, "Fixture provider failed")]
        await client.post(endpoint + "/messages", json={"text": "A failed run also needs attention"})
        failure = await _events_until(client, chat["id"], "drive/error", last=str(new_sequence))
        task = client.app.state.registry.find_chat(chat["id"])[1].task
        if task is not None:
            await task
        failed = await summary(client)
        assert failed["status"] == "idle" and failed["completion_reason"] == "error"
        assert failed["completion_seq"] == failure[-1]["seq"] > new_sequence
        assert failed["reviewed_through"] == sequence


async def test_historical_logs_recreate_projects_without_a_web_index(
    home: Path, project: Path, scripted
):
    historical_project = project.parent / "historical-project"
    historical_project.mkdir()

    async with _running_client(create_app(project)) as first:
        added = await first.post("/api/projects", json={"path": str(historical_project)})
        created = await first.post("/api/chats", json={"project_id": added.json()["id"]})
        chat_id = created.json()["id"]
        await first.post(f"/api/chats/{chat_id}/messages", json={"text": "history from logs"})
        await _events_until(first, chat_id, "turn/end")

    (home / "web.json").unlink()

    async with _running_client(create_app(project)) as second:
        projects = (await second.get("/api/projects")).json()["projects"]
        restored = next(item for item in projects if item["path"] == str(historical_project))
        assert len(restored["chats"]) == 1
        assert restored["chats"][0]["title"] == "history from logs"
        replay = await _events_until(second, restored["chats"][0]["id"], "turn/end")
        assert any(event["kind"] == "assistant/message" for event in replay)

    assert all(provider.closed for provider in scripted)


async def test_malformed_historical_chat_does_not_block_web_startup(
    home: Path, project: Path, scripted
):
    healthy = Log.create_default(project, "scripted", "scripted-model")
    healthy.close()
    log = Log.create_default(project, "scripted", "scripted-model")
    log.append_batch(
        [
            TurnStart(turn=1),
            StepStart(turn=1, step=1),
            TurnStart(turn=2),
        ]
    )
    log.close()

    async with _running_client(create_app(project)) as running:
        response = await running.get("/api/projects")
        assert response.status_code == 200
        workspace = next(
            item for item in response.json()["projects"] if item["path"] == str(project)
        )
        assert len(workspace["chats"]) == 1

    assert all(provider.closed for provider in scripted)


async def test_message_validation(client: httpx.AsyncClient):
    await client.post("/api/chats", json={"project_id": "workspace"})
    bad = await client.post(
        "/api/chats/c1/messages", content=b"not json", headers={"content-type": "application/json"}
    )
    assert bad.status_code == 400 and bad.json() == {"error": "message must be valid JSON"}
    empty = await client.post("/api/chats/c1/messages", json={"text": ""})
    assert empty.json() == {"error": "message must contain non-empty text or an attachment"}
    delivery = await client.post("/api/chats/c1/messages", json={"text": "x", "delivery": "later"})
    assert delivery.json() == {"error": "delivery must be 'steer' or 'followup'"}
    utf8 = await client.post(
        "/api/chats/c1/messages",
        json={"attachments": [{"kind": "file", "name": "bad.txt", "data_base64": "/w=="}]},
    )
    assert utf8.status_code == 400 and utf8.json() == {
        "error": "file attachment 'bad.txt' is not valid UTF-8"
    }
    chat = (await client.get("/api/chats/c1")).json()
    assert chat["title"] == "" and chat["status"] == "idle"


async def test_message_runs_a_turn_and_events_replay(client: httpx.AsyncClient, scripted):
    await client.post("/api/chats", json={"project_id": "workspace"})
    accepted = await client.post(
        "/api/chats/c1/messages",
        json={
            "attachments": [
                {
                    "kind": "image",
                    "name": "photo.png",
                    "data_base64": base64.b64encode(PNG_2X3).decode(),
                },
                {
                    "kind": "file",
                    "name": "notes.txt",
                    "data_base64": base64.b64encode(b"alpha").decode(),
                },
            ]
        },
    )
    assert accepted.status_code == 202
    assert accepted.json() == {
        "accepted": True,
        "chat": {"id": "c1", "title": "photo.png", "status": "running", "archived": False,
                 "started_at": "", "completed_at": "", "completion_seq": -1,
                 "completion_reason": "", "reviewed_through": -1},
    }
    events = await _events_until(client, "c1", "turn/end")
    assert events[0]["kind"] == "status"
    assert events[0]["status"] in ("running", "idle")
    assert events[0]["model"] == "scripted-model" and events[0]["cwd"]
    events = [event for event in events if event["kind"] != "status"]
    kinds = [event["kind"] for event in events]
    assert kinds == [
        "session/start",
        "prompt/resolved",
        "tools/advertised",
        "inbox/spliced",
        "turn/start",
        "step/start",
        "step/claimed",
        "selection",
        "assistant/message",
        "attempt/timing",
        "step/end",
        "turn/end",
    ]  # assistant/chunk (seq 8) is write-through: durable, but never replayed to a later subscriber
    claimed = events[6]
    assert claimed["messages"][0]["blocks"] == [
        {"kind": "image", "display_path": "photo.png", "media_type": "image/png", "byte_size": 24},
        {"kind": "file_text", "display_path": "notes.txt", "byte_size": 5},
    ]
    assert events[8]["seq"] == 9 and events[8]["blocks"] == [{"kind": "text", "text": "web answer"}]
    assert events[-1]["reason"] == "completed" and "elapsed_ms" in events[-1]
    provider = scripted[0]
    first = provider.contexts[0].items[0]
    assert [block.kind.value for block in first.blocks] == ["image", "file_text"]
    # Last-Event-ID suppresses already received sequence numbers on reconnect.
    replay = await _events_until(client, "c1", "turn/end", last="10")
    assert [event["seq"] for event in replay if event["kind"] != "status"] == [11, 12]
    assert replay[0]["kind"] == "status"
    assert replay[0]["status"] == "idle" and replay[0]["turn_open"] is False
    chat = (await client.get("/api/chats/c1")).json()
    assert chat["status"] == "idle" and chat["title"] == "photo.png"


async def test_status_channel_reports_provider_neutral_context_usage(
    client: httpx.AsyncClient, scripted, project: Path
):
    from ava.llm import Usage

    await client.post("/api/chats", json={"project_id": "workspace"})
    initial = await _events_until(client, "c1", "status")
    assert initial == [
        {
            "kind": "status",
            "status": "idle",
            "turn_open": False,
            "cwd": str(project),
            "provider": "scripted",
            "model": "scripted-model",
            "effort": None,
            "context_used_tokens": 0,
            "context_window_tokens": 10_000,
            "context_remaining_percent": 100,
            "input_tokens": None,
            "output_tokens": None,
            "cache_hit_percent": None,
            "ttft_ms": None,
        }
    ]

    scripted[0].scripts = [
        text_response("answer", usage=Usage(input=3000, cached_read=1000, output=500))
    ]
    await client.post("/api/chats/c1/messages", json={"text": "hello"})
    await _events_until(client, "c1", "turn/end")
    settled = await _events_until(client, "c1", "status")
    assert settled[0]["context_used_tokens"] == 4500
    assert settled[0]["context_remaining_percent"] == 55
    assert settled[0]["input_tokens"] == 4000
    assert settled[0]["output_tokens"] == 500
    assert settled[0]["cache_hit_percent"] == 25
    assert isinstance(settled[0]["ttft_ms"], int)


async def test_running_enter_steers_and_alt_enter_queues(client: httpx.AsyncClient, scripted):
    await client.post("/api/chats", json={"project_id": "workspace"})
    provider = scripted[0]
    provider.gate = (
        asyncio.Event()
    )  # hold the first provider call open so submissions arrive mid-turn
    initial = await client.post("/api/chats/c1/messages", json={"text": "initial request"})
    assert initial.status_code == 202 and initial.json()["chat"]["status"] == "running"
    await asyncio.wait_for(provider.started.wait(), 5)
    steer = await client.post(
        "/api/chats/c1/messages", json={"text": "steer now", "delivery": "steer"}
    )
    followup = await client.post(
        "/api/chats/c1/messages", json={"text": "queue later", "delivery": "followup"}
    )
    assert (
        steer.json()["chat"]["status"] == "running"
        and followup.json()["chat"]["status"] == "running"
    )
    provider.gate.set()
    events = await _events_until(client, "c1", lambda e: e["kind"] == "turn/end" and e["turn"] == 2)
    claims = [
        (e["turn"], e["step"], e["target"], e["messages"][0]["blocks"][-1]["text"])
        for e in events
        if e["kind"] == "step/claimed"
    ]
    assert claims == [
        (1, 1, "next_turn", "initial request"),
        (1, 2, "next_step", "steer now"),
        (2, 1, "next_turn", "queue later"),
    ]
    texts = [
        [b.text for i in ctx.items for b in i.blocks if b.kind.value == "text"]
        for ctx in provider.contexts
    ]
    # Turn 1 takes three calls (answer, tool call, follow-on); the queued follow-up opens turn 2.
    assert (
        "steer now" in texts[1] and "queue later" not in texts[1] and "queue later" not in texts[2]
    )
    assert "queue later" in texts[3]
    # An idle steer still opens an ordinary turn.
    idle = await client.post(
        "/api/chats/c1/messages", json={"text": "idle steer", "delivery": "steer"}
    )
    assert idle.status_code == 202
    events = await _events_until(client, "c1", lambda e: e["kind"] == "turn/end" and e["turn"] == 3)
    last_claim = [e for e in events if e["kind"] == "step/claimed"][-1]
    assert last_claim["target"] == "next_turn" and last_claim["turn"] == 3
    assert (await client.get("/api/chats/c1")).json()["status"] == "idle"


async def test_pending_message_routes_revise_delete_and_send_now(
    client: httpx.AsyncClient, scripted
):
    await client.post("/api/chats", json={"project_id": "workspace"})
    provider = scripted[0]
    provider.gate = asyncio.Event()
    await client.post("/api/chats/c1/messages", json={"text": "initial request"})
    await asyncio.wait_for(provider.started.wait(), 5)

    queued = await client.post(
        "/api/chats/c1/messages", json={"text": "discard this", "delivery": "followup"}
    )
    assert queued.status_code == 202
    deleted = await client.delete("/api/chats/c1/inbox/m-2")
    assert deleted.status_code == 200 and deleted.json()["deleted"] is True

    await client.post(
        "/api/chats/c1/messages", json={"text": "rough follow-up", "delivery": "followup"}
    )
    malformed = await client.patch("/api/chats/c1/inbox/m-3", json={"text": 42})
    assert malformed.status_code == 400
    empty = await client.patch("/api/chats/c1/inbox/m-3", json={"text": "   "})
    assert empty.status_code == 400
    revised = await client.patch("/api/chats/c1/inbox/m-3", json={"text": "revised for this turn"})
    assert revised.status_code == 200 and revised.json()["revised"] is True

    sent = await client.post("/api/chats/c1/inbox/m-4/send")
    assert sent.status_code == 202 and sent.json()["sent"] is True
    stale = await client.delete("/api/chats/c1/inbox/m-4")
    assert stale.status_code == 409
    inbox = client_agent(client, "c1").state.session.inbox()
    assert inbox.next_turn == []
    assert [entry.id for entry in inbox.next_step] == ["m-5"]

    provider.gate.set()
    events = await _events_until(client, "c1", "turn/end")
    claims = [event for event in events if event["kind"] == "step/claimed"]
    assert [
        (claim["turn"], claim["step"], claim["target"], claim["messages"][0]["blocks"][-1]["text"])
        for claim in claims
    ] == [
        (1, 1, "next_turn", "initial request"),
        (1, 2, "next_step", "revised for this turn"),
    ]


async def test_event_stream_disconnect_releases_the_subscription(
    client: httpx.AsyncClient, scripted
):
    """A dropped browser connection must not leak a session subscriber."""
    await client.post("/api/chats", json={"project_id": "workspace"})
    agent = client_agent(client, "c1")
    subscribers = agent.state.session._subscribers
    baseline = len(subscribers)
    async with client.stream("GET", "/api/chats/c1/events") as response:
        async for line in response.aiter_lines():
            if line.startswith("id: "):
                break
        assert len(subscribers) == baseline + 1
    for _ in range(50):
        if len(subscribers) == baseline:
            break
        await asyncio.sleep(0.05)
    assert len(subscribers) == baseline


def client_agent(client: httpx.AsyncClient, chat_id: str):
    registry = client.app.state.registry  # type: ignore[attr-defined]
    found = registry.find_chat(chat_id)
    assert found is not None
    return found[1].agent


async def test_pause_resume_and_abort_controls(client: httpx.AsyncClient, scripted):
    await client.post("/api/chats", json={"project_id": "workspace"})
    provider = scripted[0]
    provider.scripts = [
        tool_call_response("c1", "bash", json.dumps({"command": "echo one"})),
        text_response("after resume"),
        text_response("second turn"),
    ]
    provider.gate = asyncio.Event()
    await client.post("/api/chats/c1/messages", json={"text": "go"})
    await asyncio.wait_for(provider.started.wait(), 5)
    assert (await client.post("/api/chats/c1/cancel", json={"cause": "later"})).status_code == 400
    paused = await client.post("/api/chats/c1/cancel", json={"cause": "pause"})
    assert paused.json()["status"] == "pausing"
    # Steering while pausing is retained for the resumed continuation.
    steer = await client.post(
        "/api/chats/c1/messages", json={"text": "and then", "delivery": "steer"}
    )
    assert steer.status_code == 202
    provider.gate.set()
    # Status flips to paused before the closing record is durable, so wait on the record.
    events = await _events_until(client, "c1", lambda e: e["kind"] == "turn/end")
    assert events[-1]["reason"] == "user_pause"
    paused_summary = (await client.get("/api/projects")).json()["projects"][0]["chats"][0]
    assert paused_summary["status"] == "paused"
    assert paused_summary["completion_reason"] == "user_pause"
    assert paused_summary["completion_seq"] == events[-1]["seq"]
    assert any(e["kind"] == "status" and e["status"] == "pausing" for e in events)
    assert (
        await client.post("/api/chats/c1/messages", json={"text": "queued", "delivery": "followup"})
    ).status_code == 202
    assert (await client.get("/api/chats/c1")).json()["status"] == "paused"
    # Hold one live connection across the resume: the status channel must settle to idle after
    # the last turn ends. A snapshot taken on reconnect would hide a missing final notification.
    events = []
    async with client.stream("GET", "/api/chats/c1/events") as response:
        stream = _sse_events(response)
        assert (await anext(stream))["status"] == "paused"
        resumed = await client.post("/api/chats/c1/resume")
        assert resumed.status_code == 202
        assert (await client.post("/api/chats/c1/resume")).status_code == 409
        seen_turn_three = False
        async for event in stream:
            events.append(event)
            if event["kind"] == "turn/end" and event["turn"] == 3:
                seen_turn_three = True
            elif seen_turn_three and event["kind"] == "status" and event["status"] == "idle":
                break
    claims = [
        (e["turn"], e["step"], e["target"], e["messages"][0]["blocks"][-1]["text"])
        for e in events
        if e["kind"] == "step/claimed"
    ]
    # The paused turn is closed, so the continuation is a new turn that claims only steering
    # (no synthetic user message); the queued follow-up opens the turn after it.
    assert claims == [
        (1, 1, "next_turn", "go"),
        (2, 1, "next_step", "and then"),
        (3, 1, "next_turn", "queued"),
    ]
    assert [e["reason"] for e in events if e["kind"] == "turn/end"] == [
        "user_pause",
        "completed",
        "completed",
    ]

    provider.scripts = [text_response("never finishes")]
    provider.started.clear()
    provider.gate = asyncio.Event()
    await client.post("/api/chats/c1/messages", json={"text": "abort me"})
    await asyncio.wait_for(provider.started.wait(), 5)
    aborted = await client.post("/api/chats/c1/cancel", json={"cause": "abort"})
    assert aborted.json()["status"] == "aborting"
    events = await _events_until(client, "c1", lambda e: e["kind"] == "turn/end" and e["turn"] == 4)
    assert events[-1]["reason"] == "user_abort"
    events = await _events_until(
        client, "c1", lambda e: e["kind"] == "status" and e["status"] == "idle"
    )
    assert (await client.get("/api/chats/c1")).json()["status"] == "idle"


async def test_model_and_effort_selection_apply_at_the_next_step(
    client: httpx.AsyncClient, scripted, monkeypatch
):
    from ava.llm import ModelCapabilities

    monkeypatch.setenv("OPENAI_API_KEY", "fixture-key")
    profiles = {
        "scripted-model": ModelCapabilities(effort_values=["low", "high"]),
        "other-model": ModelCapabilities(effort_values=["max"]),
    }

    async def list_models(self, *args):
        return list(profiles)

    def catalog_provider(*args, **kwargs):
        provider = ScriptedProvider([text_response("selected answer")])
        provider.id = "openai"
        provider.model_overrides = dict(profiles)
        return provider

    monkeypatch.setattr(ScriptedProvider, "list_models", list_models)
    monkeypatch.setattr("ava.app.web.providers.provider_from_environment", catalog_provider)
    monkeypatch.setattr("ava.app.web.providers.provider_names", lambda: ["openai"])
    await client.post("/api/chats", json={"project_id": "workspace", "provider": "openai", "model": "scripted-model"})
    listed = (await client.get("/api/chats/c1/models")).json()
    assert listed["effort_values"] == ["low", "high"] and listed["catalog_available"]
    assert [entry["id"] for entry in listed["providers"]] == ["openai"]
    bad = await client.post("/api/chats/c1/model", json={"model": "other-model", "effort": "high"})
    assert bad.status_code == 400
    assert client_agent(client, "c1").current_selection().model == "scripted-model"
    chosen = await client.post("/api/chats/c1/model", json={"model": "other-model", "effort": "max"})
    assert chosen.json() == {"provider": "openai", "model": "other-model", "effort": "max"}
    assert (await client.post("/api/chats/c1/model", json={"model": ""})).status_code == 400
    provider = client_agent(client, "c1").state.provider
    provider.gate = asyncio.Event()
    await client.post("/api/chats/c1/messages", json={"text": "go"})
    await asyncio.wait_for(provider.started.wait(), 5)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fixture-key")
    busy = await client.post("/api/chats/c1/model", json={"provider": "deepseek", "model": "other-model"})
    assert busy.status_code == 409
    pending = await client.post("/api/chats/c1/model", json={"model": "scripted-model", "effort": "low"})
    assert pending.status_code == 200 and pending.json()["effort"] == "low"
    assert client_agent(client, "c1").state.provider is provider and not provider.closed
    provider.gate.set()
    events = await _events_until(client, "c1", "turn/end")
    selection = next(e for e in events if e["kind"] == "selection")
    assert selection["model"] == "other-model" and selection["effort"] == "max"
    provider = client_agent(client, "c1").state.provider
    assert provider.selection.model == "other-model"
    assert client_agent(client, "c1").current_selection() == Selection("openai", "scripted-model", "low")
    assert not provider.remembers_selection


async def test_compact_now_appends_a_seed_and_refuses_while_busy(
    client: httpx.AsyncClient, scripted
):
    from ava.llm import Usage

    await client.post("/api/chats", json={"project_id": "workspace"})
    provider = scripted[0]
    provider.scripts = [
        text_response("h" * 4000, usage=Usage(input=9000, output=10)),
        text_response("## Goal\n- keep going\n\n## Next Steps\n- more\n"),
        text_response("after"),
    ]
    provider.gate = asyncio.Event()
    await client.post("/api/chats/c1/messages", json={"text": "first " + "x" * 3000})
    await asyncio.wait_for(provider.started.wait(), 5)
    busy = await client.post("/api/chats/c1/compact")
    assert busy.status_code == 409 and "busy" in busy.json()["error"]
    provider.gate.set()
    await _events_until(client, "c1", "turn/end")
    compacted = await client.post("/api/chats/c1/compact")
    assert compacted.json()["outcome"] == "compacted"
    events = await _events_until(client, "c1", "compaction/seed")
    seed = events[-1]
    assert "## Files" in seed["blocks"][0]["text"] and seed["covered_end"] > 0
    again = await client.post("/api/chats/c1/compact")
    assert again.json()["outcome"] == "nothing_to_compact"


async def test_skills_route_lists_the_project_catalog(client: httpx.AsyncClient, project: Path):
    skill = project / ".agents/skills/deploy"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\ndescription: Ship the thing\n---\n")
    await client.post("/api/chats", json={"project_id": "workspace"})
    listed = (await client.get("/api/chats/c1/skills")).json()["skills"]
    assert listed == [
        {
            "name": "deploy",
            "description": "Ship the thing",
            "scope": "project",
            "path": str(skill / "SKILL.md"),
        }
    ]
    assert (await client.get("/api/chats/nope/skills")).status_code == 404


async def test_credentials_are_stored_and_idle_chats_reloaded(
    client: httpx.AsyncClient, home: Path, monkeypatch: pytest.MonkeyPatch
):
    await client.post("/api/chats", json={"project_id": "workspace"})
    reloaded: list[str] = []
    def provider(cli, resumed, requirement):
        reloaded.append(requirement.value)
        return client_agent(client, "c1").state.provider

    monkeypatch.setattr("ava.agent.agent.provider_from_environment", provider)
    bad = await client.post("/api/credentials", json={"provider": "scripted"})
    assert bad.status_code == 400
    codex = await client.post("/api/credentials", json={"provider": "codex", "key": "x"})
    assert codex.status_code == 400 and "codex login" in codex.json()["error"]
    saved = await client.post("/api/credentials", json={"provider": "scripted", "key": "sk-test"})
    assert saved.json() == {"provider": "scripted", "reloaded": ["c1"], "failed": {}}
    assert json.loads((home / "auth.json").read_text()) == {
        "scripted": {"type": "api_key", "key": "sk-test"}
    }
    assert (home / "auth.json").stat().st_mode & 0o777 == 0o600
    removed = await client.delete("/api/credentials/scripted")
    assert removed.json()["reloaded"] == ["c1"] and reloaded == ["required", "allow_missing"]
    assert json.loads((home / "auth.json").read_text()) == {}


async def test_context_route_reports_the_model_window(client: httpx.AsyncClient, scripted):
    await client.post("/api/chats", json={"project_id": "workspace"})
    empty = (await client.get("/api/chats/c1/context")).json()
    assert [section["kind"] for section in empty["sections"]] == ["system", "environment", "tools"]
    await client.post("/api/chats/c1/messages", json={"text": "hello there"})
    await _events_until(client, "c1", "turn/end")
    report = (await client.get("/api/chats/c1/context")).json()
    kinds = [section["kind"] for section in report["sections"]]
    assert "user_text" in kinds and "assistant_text" in kinds and "framing" in kinds
    assert report["estimated_tokens"] == sum(section["tokens"] for section in report["sections"])
    assert report["context_window"] == 10_000 and report["threshold_percent"] == 85
    assert report["compacted"] is False
    assert (await client.get("/api/chats/nope/context")).status_code == 404


async def test_project_files_page_snapshot_and_bounded_versioned_download(client, project, tmp_path):
    import os

    folder = project / '目录 #?'
    folder.mkdir()
    for i in range(700):
        (folder / f'{i:04}.txt').write_text(f'文件 {i}')
    source = folder / '中文 #?.md'
    source.write_text('# Hello 世界\n\n**Remote preview**')
    (folder / 'empty').mkdir()
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'secret.txt').write_text('must not be returned')
    (folder / 'escape').symlink_to(outside, target_is_directory=True)
    (folder / 'within').symlink_to(folder / 'empty', target_is_directory=True)
    (folder / 'pipe').parent.mkdir(exist_ok=True)
    os.mkfifo(folder / 'pipe')
    path = '/api/projects/workspace/'
    first = (await client.get(path + 'files', params={'path': str(folder)})).json()
    assert len(first['entries']) == 256 and first['next'] == 256
    assert [entry['fileName'] for entry in first['entries'][:2]] == ['empty', 'within']
    # Later pages remain from the original snapshot, without duplicates as files change.
    (folder / '0000.txt').unlink()
    (folder / 'new.txt').write_text('created after initial page')
    rows = list(first['entries'])
    offset = first['next']
    while offset is not None:
        page = (await client.get(path + 'files', params={'path': str(folder), 'cursor': first['cursor'], 'offset': offset})).json()
        rows += page['entries']
        offset = page['next']
    assert len(rows) == first['total'] == len({row['filePath'] for row in rows})
    assert not any(row['fileName'] == 'new.txt' for row in rows)
    assert not next(row for row in rows if row['fileName'] == 'escape')['directory']
    empty = (await client.get(path + 'files', params={'path': str(folder / 'empty')})).json()
    assert empty['entries'] == [] and empty['next'] is None
    for invalid in [str(folder / 'escape'), '../outside', str(outside)]:
        assert (await client.get(path + 'files', params={'path': invalid})).status_code == 400
    assert (await client.get(path + 'file', params={'path': str(folder / 'pipe')})).status_code == 400
    assert (await client.get(path + 'file', params={'path': str(folder / 'escape/secret.txt')})).status_code == 400
    metadata = (await client.get(path + 'file', params={'path': str(source)})).json()
    assert metadata['kind'] == 'markdown'
    download = await client.get(path + 'file/content', params={'path': str(source), 'version': metadata['version']})
    assert download.content == source.read_bytes()
    source.write_text('Changed')
    stale = await client.get(path + 'file/content', params={'path': str(source), 'version': metadata['version']})
    assert stale.status_code == 409
    large = folder / 'large.txt'
    with large.open('wb') as stream:
        stream.truncate(1024 * 1024 + 1)
    metadata = (await client.get(path + 'file', params={'path': str(large)})).json()
    assert metadata['kind'] == 'unsupported' and '1 MiB' in metadata['notice']
    assert (await client.get(path + 'file/content', params={'path': str(large), 'version': metadata['version']})).status_code == 400
    await client.post('/api/projects/workspace/hide')
    assert (await client.get(path + 'files', params={'path': str(folder), 'cursor': first['cursor'], 'offset': 256})).status_code == 400
    assert source.read_text() == 'Changed'


async def test_worktree_sessions_restore_project_identity_and_fence_file_access(home, project, scripted):
    import subprocess
    from uuid import uuid4

    def git(*args):
        return subprocess.check_output(["git", "-C", str(project), *args], text=True).strip()

    git("init", "-q", "-b", "main")
    git("config", "user.name", "Ava fixture")
    git("config", "user.email", "ava@example.invalid")
    git("config", "commit.gpgsign", "false")
    (project / "code").mkdir()
    (project / "code" / "answer.txt").write_text("committed")
    git("add", ".")
    git("commit", "-qm", "initial")
    (project / "code" / "answer.txt").write_text("uncommitted")
    async with _running_client(create_app(project / "code")) as first:
        project_id = (await first.get("/api/projects")).json()["projects"][0]["id"]
        options = await first.get(f"/api/projects/{project_id}/worktrees")
        assert options.status_code == 200
        assert any(ref["ref"] == "refs/heads/main" for ref in options.json()["refs"])
        body = {"project_id": project_id, "workspace": "worktree", "branch": "ava/中文-review",
                "base_ref": "refs/heads/main", "request_id": uuid4().hex}
        results = await asyncio.gather(*(first.post("/api/chats", json=body) for _ in range(2)))
        assert all(response.status_code in (200, 201) for response in results), [r.text for r in results]
        assert results[0].json() == results[1].json()
        chat = results[0].json()
        workspace = Path(chat["cwd"])
        assert workspace.name == "code" and workspace.parent.parent == home / "worktrees"
        assert (workspace / "answer.txt").read_text() == "committed"
        assert (project / "code" / "answer.txt").read_text() == "uncommitted"
        assert git("branch", "--show-current") == "main"
        opened = (await first.get(f"/api/chats/{chat['id']}")).json()
        assert opened["project_id"] == project_id and opened["cwd"] == str(workspace)
        info = await first.get(f"/api/projects/{project_id}/file", params={"workspace": str(workspace), "path": "answer.txt"})
        assert info.status_code == 200
        content = await first.get(f"/api/projects/{project_id}/file/content", params={"workspace": str(workspace), "path": "answer.txt", "version": info.json()["version"]})
        assert content.text == "committed"
        assert (await first.get(f"/api/projects/{project_id}/files", params={"workspace": str(home)})).status_code == 400
        assert (await first.delete(f"/api/chats/{chat['id']}")).status_code == 409
        assert len((await first.get("/api/projects")).json()["projects"]) == 1
    # Durable labels preserve grouping even when rebuilding the optional UI index.
    (home / "web.json").unlink()
    async with _running_client(create_app(project / "code")) as second:
        projects = (await second.get("/api/projects")).json()["projects"]
        assert len(projects) == 1 and projects[0]["path"] == str(project / "code")
        assert len(projects[0]["chats"]) == 1
        resumed = projects[0]["chats"][0]
        body["project_id"] = projects[0]["id"]
        retried = await second.post("/api/chats", json=body)
        assert retried.status_code == 200 and retried.json()["id"] == resumed["id"]
        assert retried.json()["cwd"] == str(workspace)
        await second.post(f"/api/projects/{projects[0]['id']}/hide")
        assert (await second.get("/api/projects")).json()["projects"] == []
        await second.post("/api/projects", json={"path": str(project / "code")})
        assert (workspace / "answer.txt").read_text() == "committed"
    assert all(provider.closed for provider in scripted)


async def test_worktree_creation_failures_are_retryable_without_orphan_sessions(home, project, scripted, monkeypatch):
    import subprocess
    from uuid import uuid4

    from ava.base import AvaError, ErrorKind

    def git(*args):
        return subprocess.check_output(["git", "-C", str(project), *args], text=True).strip()

    git("init", "-q", "-b", "main")
    git("config", "user.name", "Ava fixture")
    git("config", "user.email", "ava@example.invalid")
    git("config", "commit.gpgsign", "false")
    git("commit", "--allow-empty", "-qm", "initial")
    async with _running_client(create_app(project)) as client:
        registry = client.app.state.registry
        body = {"project_id": registry.projects[0].id, "workspace": "worktree", "branch": "ava/retry", "request_id": uuid4().hex}
        for invalid in ({"branch": "bad name"}, {"base_ref": "does-not-exist"}, {"request_id": "../escape"}):
            response = await client.post("/api/chats", json={**body, **invalid})
            assert response.status_code == 400, response.text
        assert not registry.projects[0].chats
        assert not list(home.rglob("*.jsonl*"))
        # Constructor failure must remove its new empty log before discovery
        # can resurrect a failed creation as an orphan conversation.
        with monkeypatch.context() as patch:
            def broken_state(*args, **kwargs):
                raise AvaError(ErrorKind.io, "Cannot load workspace instructions")
            patch.setattr("ava.agent.agent.AgentState.create", broken_state)
            failed_agent = await client.post("/api/chats", json={"project_id": registry.projects[0].id})
            assert failed_agent.status_code == 503
            assert not list(home.rglob("*.jsonl*"))
        # A slow real checkout hook must not block the event loop. Concurrent
        # retries join the task; changing its parameters cannot hijack it.
        marker = project / "hook-ready"
        hook = project / ".git" / "hooks" / "post-checkout"
        hook.write_text(f'#!/bin/sh\ntouch "{marker}"\nsleep 0.3\nprintf kept > hook-output\n')
        hook.chmod(0o700)
        with monkeypatch.context() as patch:
            def unavailable():
                raise AvaError(ErrorKind.io, "Fixture disk unavailable")
            patch.setattr(registry, "persist", unavailable)
            pending = asyncio.create_task(client.post("/api/chats", json=body))
            async with asyncio.timeout(3):
                while not marker.exists():
                    await asyncio.sleep(0.01)
            assert (await client.get("/api/projects")).status_code == 200
            assert not pending.done()
            mismatch = await client.post("/api/chats", json={**body, "branch": "ava/other"})
            assert mismatch.status_code == 409
            failed = await pending
            assert failed.status_code == 503 and "retained" in failed.text
        assert not registry.projects[0].chats
        assert not list(home.rglob("*.jsonl*"))
        retained = home / "worktrees" / body["request_id"]
        assert (retained / "hook-output").read_text() == "kept"
        retried = await client.post("/api/chats", json=body)
        assert retried.status_code == 201, retried.text
        assert retried.json()["cwd"] == str(retained)
        assert len(registry.projects[0].chats) == 1
        assert (retained / "hook-output").read_text() == "kept"
    assert all(provider.closed for provider in scripted)


async def test_browser_handoff_routes_deliver_once_and_cancel_inflight_actions(client, scripted):
    project_id = (await client.get('/api/projects')).json()['projects'][0]['id']
    chat = (await client.post('/api/chats', json={'project_id': project_id})).json()['id']
    provider = scripted[-1]
    provider.scripts = [tool_call_response('browser-one', 'browser', '{"action":"snapshot"}'), text_response('Inspected')]
    prefix = f'/api/chats/{chat}/browser'
    lease = (await client.post(prefix, json={})).json()['id']
    path = prefix + '/' + lease
    assert (await client.post(f'/api/chats/{chat}/messages', json={'text': 'Inspect the shared page'})).status_code == 202
    command = (await client.get(path)).json()['command']
    assert command['action'] == 'snapshot'
    duplicate_poll = asyncio.create_task(client.get(path))
    await asyncio.sleep(0.02)
    assert not duplicate_poll.done(), 'A delivered action must never be replayed by a later poll'
    assert (await client.post(path, json={'id': 'stale', 'text': 'Wrong result'})).status_code == 409
    assert (await client.post(path, json={'id': command['id'], 'text': 'Shared page text'})).status_code == 200
    assert (await duplicate_poll).json()['command'] is None
    events = await _events_until(client, chat, 'turn/end')
    assert any(event['kind'] == 'tool/result' for event in events)
    assert any(block.text == 'Shared page text' for item in provider.contexts[-1].items for block in item.blocks)
    assert (await client.post(path, json={'id': command['id'], 'text': 'Repeated'})).status_code == 409

    provider.scripts = [tool_call_response('browser-two', 'browser', '{"action":"click","ref":"old"}'), text_response('Taken over')]
    provider.calls = 0
    assert (await client.post(f'/api/chats/{chat}/messages', json={'text': 'Continue'})).status_code == 202
    pending = (await client.get(path)).json()['command']
    replacement = (await client.post(prefix, json={})).json()['id']
    assert replacement != lease
    assert (await client.post(path, json={'id': pending['id'], 'text': 'Late result'})).status_code == 410
    await _events_until(client, chat, lambda event: event['kind'] == 'turn/end' and event.get('turn') == 2)
    assert any(block.is_error and 'different browser tab' in block.text for item in provider.contexts[-1].items for block in item.blocks)
    assert (await client.delete(prefix + '/' + replacement)).status_code == 200
    assert (await client.get(prefix + '/' + replacement)).status_code == 410
