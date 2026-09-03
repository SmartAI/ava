"""The Web UI routes, the loopback fence, message routing, and the replay-first event stream."""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path

import httpx
import pytest

from ava.app.web.server import create_app
from tests.conftest import ScriptedProvider, text_response, tool_call_response

PNG_2X3 = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAIAAAAD")


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
        providers.append(provider)
        return provider

    monkeypatch.setattr("ava.app.web.server.provider_from_environment", factory)
    return providers


@pytest.fixture
async def client(home: Path, project: Path, scripted):
    """A real loopback server so the event stream is exercised over HTTP, not a buffered ASGI shim."""
    from ava.app.web.server import bind, create_server

    sock = bind(0)
    app = create_app(project)
    server = create_server(app, sock)
    task = asyncio.create_task(server.serve(sockets=[sock]))
    while not server.started:
        await asyncio.sleep(0.01)
    port = sock.getsockname()[1]
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=10) as client:
        client.headers["host"] = f"127.0.0.1:{port}"
        client.port = port  # type: ignore[attr-defined]
        client.app = app  # type: ignore[attr-defined]
        yield client
    server.should_exit = True
    await task


async def _events_until(
    client: httpx.AsyncClient, chat_id: str, stop, last: str | None = None
) -> list[dict]:
    """Read the stream until ``stop`` (an event kind or a predicate) matches.

    Session events come back as their JSON payload; unkeyed ``status`` messages come back as
    ``{"kind": "status", ...}`` so a test can watch control state on the same channel.
    """
    done = stop if callable(stop) else (lambda event: event["kind"] == stop)
    headers = {"last-event-id": last} if last is not None else {}
    events: list[dict] = []
    name = ""
    async with client.stream("GET", f"/api/chats/{chat_id}/events", headers=headers) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        async for line in response.aiter_lines():
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                event = json.loads(line[6:])
                if name == "status":
                    event = {"kind": "status", **event}
                name = ""
                events.append(event)
                if done(event):
                    break
    return events


def _kinds(events: list[dict], *, with_status: bool = False) -> list[str]:
    return [event["kind"] for event in events if with_status or event["kind"] != "status"]


async def test_fence_rejects_foreign_host_and_origin(client: httpx.AsyncClient):
    forbidden = await client.get("/api/projects", headers={"host": "example.invalid"})
    assert forbidden.status_code == 403 and forbidden.json() == {"error": "forbidden request"}
    cross = await client.post(
        "/api/chats/c1/messages", headers={"origin": "https://example.invalid"}, content=b"{}"
    )
    assert cross.status_code == 403
    same = await client.get("/api/projects", headers={"origin": f"http://127.0.0.1:{client.port}"})
    assert same.status_code == 200
    index = await client.get("/")
    assert index.status_code == 200 and "<title>ava</title>" in index.text and "katex" in index.text
    assert (await client.get("/favicon.ico")).status_code == 204


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
    }
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
        "chat": {"id": "c1", "title": "photo.png", "status": "running", "archived": False},
    }
    events = await _events_until(client, "c1", "turn/end")
    assert (
        events[0] == {"kind": "status", "status": "running", "turn_open": True}
        or events[0]["kind"] == "status"
    )
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
    assert replay[0] == {"kind": "status", "status": "idle", "turn_open": False}
    chat = (await client.get("/api/chats/c1")).json()
    assert chat["status"] == "idle" and chat["title"] == "photo.png"


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
    assert any(e["kind"] == "status" and e["status"] == "pausing" for e in events)
    assert (
        await client.post("/api/chats/c1/messages", json={"text": "queued", "delivery": "followup"})
    ).status_code == 202
    assert (await client.get("/api/chats/c1")).json()["status"] == "paused"
    resumed = await client.post("/api/chats/c1/resume")
    assert resumed.status_code == 202
    assert (await client.post("/api/chats/c1/resume")).status_code == 409
    events = await _events_until(client, "c1", lambda e: e["kind"] == "turn/end" and e["turn"] == 3)
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
    client: httpx.AsyncClient, scripted
):
    from ava.llm import ModelCapabilities

    await client.post("/api/chats", json={"project_id": "workspace"})
    provider = scripted[0]
    provider.model_overrides["scripted-model"] = ModelCapabilities(effort_values=["low", "high"])
    provider.model_overrides["other-model"] = ModelCapabilities(effort_values=["max"])
    listed = (await client.get("/api/chats/c1/models")).json()
    assert listed["model"] == "scripted-model" and listed["effort"] is None
    assert listed["effort_values"] == ["low", "high"] and listed["catalog_available"] is False
    assert "scripted-model" in listed["models"]
    assert (await client.post("/api/chats/c1/model", json={"effort": "max"})).status_code == 400
    chosen = await client.post(
        "/api/chats/c1/model", json={"model": "other-model", "effort": "max"}
    )
    assert chosen.json() == {"provider": "scripted", "model": "other-model", "effort": "max"}
    assert (await client.post("/api/chats/c1/model", json={"model": ""})).status_code == 400
    await client.post("/api/chats/c1/messages", json={"text": "go"})
    events = await _events_until(client, "c1", "turn/end")
    selection = next(e for e in events if e["kind"] == "selection")
    assert selection["model"] == "other-model" and selection["effort"] == "max"
    assert provider.selection.model == "other-model"
