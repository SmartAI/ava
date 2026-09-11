"""OAuth acceptance through the real management API and SDK HTTP transport."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
import pytest

from ava.base import CancelToken
from ava.tool.mcp import MCPServers
from tests.fixtures.oauth_server import oauth_server as oauth_server
from tests.test_web import client as client
from tests.test_web import scripted as scripted

BASE = "/api/projects/workspace/mcp"


def draft(server, **overrides):
    return {"name": "Mail", "transport": "http", "url": server.url, "request_id": uuid4().hex,
            "oauth": {"client_id": server.client_id, "client_secret": server.client_secret,
                      "issuer": server.issuer, "scope": server.scope}, **overrides}


async def row(client):
    result = await client.get(BASE)
    assert result.status_code == 200, result.text
    return result.json()["servers"][0]


async def begin(client, identity, version=1):
    result = await client.post(f"{BASE}/{identity}/authenticate", json={"version": version})
    assert result.status_code == 200, result.text
    return result.json()


async def consent(start):
    async with httpx.AsyncClient() as browser:
        response = await browser.get(start["authorization_url"])
    assert response.status_code == 302, response.text
    return {key: values[0] for key, values in parse_qs(urlsplit(response.headers["location"]).query).items()}


async def sign_in(client, identity, version=1):
    start = await begin(client, identity, version)
    callback = await consent(start)
    result = await client.post(f"{BASE}/{identity}/oauth_callback", json={"version": version, "flow_id": start["flow_id"], **callback})
    assert result.status_code == 200, result.text
    assert result.json()["auth_status"] == "authorized"
    replay = await client.post(f"{BASE}/{identity}/oauth_callback", json={"version": version, "flow_id": start["flow_id"], **callback})
    assert replay.status_code == 400
    return start, callback


@pytest.mark.parametrize("public_catalog", [True, False], ids=["gmail-late-challenge", "protected-initialize"])
async def test_oauth_login_refresh_restart_and_sign_out(client, home, project, oauth_server, public_catalog):
    oauth_server.public_catalog = public_catalog
    added = await client.post(BASE, json=draft(oauth_server))
    assert added.status_code == 201, added.text
    identity = added.json()["id"]
    before = await row(client)
    assert before["status"] == "auth_required" and before["auth_status"] == "required"
    assert "client_secret" not in before["oauth"] and before["oauth"]["has_client_secret"]
    assert not await client.app.state.registry.mcp.tools(project, CancelToken())
    assert oauth_server.authorizations == 0
    await sign_in(client, identity)
    assert oauth_server.calls == 0, "Sign-in must not invoke a mailbox tool"
    assert oauth_server.last_authorization["scope"] == oauth_server.scope
    servers = client.app.state.registry.mcp
    tools = await servers.tools(project, CancelToken())
    assert len(tools) == 1
    assert (await tools[0].run("{}", CancelToken())).text == "INBOX, Important"
    oauth_server.tokens.clear()  # Server-side invalidation before the advertised expiry.
    assert not (await tools[0].run("{}", CancelToken())).is_error
    assert oauth_server.refreshes == 1 and oauth_server.authorizations == 1

    # Expiry is persisted as a wall-clock deadline, not restarted from expires_in.
    auth = servers.oauth[identity]
    auth.context.token_expiry_time = time.time() - 1
    auth.data["expires_at"] = time.time() - 1
    await auth._persist()
    await servers.aclose()
    fresh = MCPServers(home)
    client.app.state.registry.mcp = fresh
    tools = await fresh.tools(project, CancelToken())
    assert len(tools) == 1
    assert not (await tools[0].run("{}", CancelToken())).is_error
    assert oauth_server.refreshes == 2 and oauth_server.authorizations == 1
    # All workspaces use the same auth instance/refresh lock.
    other = project / "other"
    other.mkdir()
    fresh.oauth[identity].context.token_expiry_time = time.time() - 1
    await asyncio.gather(fresh.tools(project, CancelToken()), fresh.tools(other, CancelToken()))
    assert oauth_server.refreshes == 3
    listing = (await client.get(BASE)).text
    for secret in [oauth_server.client_secret, *oauth_server.tokens, *oauth_server.refresh_tokens]:
        assert secret not in listing
    assert (home / "capabilities.sqlite3").stat().st_mode & 0o777 == 0o600
    result = await client.post(f"{BASE}/{identity}/sign_out", json={"version": 1})
    assert result.status_code == 200
    assert (await row(client))["auth_status"] == "required"
    assert not await fresh.tools(project, CancelToken())
    with sqlite3.connect(home / "capabilities.sqlite3") as db:
        saved = json.loads(db.execute("SELECT data FROM mcp_oauth WHERE id=?", (identity,)).fetchone()[0])
    assert saved["tokens"] is None


async def test_oauth_callback_binding_denial_and_config_changes(client, home, oauth_server):
    settings = draft(oauth_server)
    added = await client.post(BASE, json=settings)
    identity = added.json()["id"]
    start = await begin(client, identity)
    callback = await consent(start)
    body = {"version": 1, "flow_id": start["flow_id"], **callback}
    path = f"{BASE}/{identity}/oauth_callback"
    for replacement in ({"state": "wrong"}, {"state": "λ"}, {"flow_id": "wrong"}, {"version": 2}):
        assert (await client.post(path, json={**body, **replacement})).status_code == 400
    assert not oauth_server.tokens
    # Issuer validation remains enabled after the loopback/state checks.
    result = await client.post(path, json={**body, "iss": "https://not-the-authority.example"})
    assert result.status_code == 400 and "identity validation failed" in result.text
    assert not oauth_server.tokens
    denied = await begin(client, identity)
    state = parse_qs(urlsplit(denied["authorization_url"]).query)["state"][0]
    result = await client.post(path, json={"version": 1, "flow_id": denied["flow_id"], "state": state, "error": "access_denied"})
    assert result.status_code == 200
    assert "denied" in (await row(client))["auth_error"]
    pending = await begin(client, identity)
    callback = await consent(pending)
    changed = {**settings, "version": 1, "oauth": {**settings["oauth"], "scope": "mail.full_access", "client_secret": None}}
    result = await client.post(f"{BASE}/{identity}", json=changed)
    assert result.status_code == 200, result.text
    assert (await client.post(path, json={"version": 1, "flow_id": pending["flow_id"], **callback})).status_code == 400
    assert (await row(client))["auth_status"] == "required"
    await sign_in(client, identity, 2)
    renamed = {**changed, "version": 2, "name": "Renamed mailbox"}
    assert (await client.post(f"{BASE}/{identity}", json=renamed)).status_code == 200
    assert (await row(client))["auth_status"] == "authorized", "A display-name edit should not discard authorization"
    assert (await client.request("DELETE", f"{BASE}/{identity}", json={"version": 3})).status_code == 200
    with sqlite3.connect(home / "capabilities.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM mcp_oauth").fetchone()[0] == 0


@pytest.mark.parametrize("failure", ["refresh-revoked", "insufficient-scope"])
async def test_oauth_registration_revocation_and_cancel(client, project, oauth_server, failure):
    added = await client.post(BASE, json=draft(oauth_server, oauth={"scope": oauth_server.scope}))
    assert added.status_code == 201, added.text
    identity = added.json()["id"]
    start = await begin(client, identity)
    assert (await client.post(f"{BASE}/{identity}/cancel_auth", json={"version": 1, "flow_id": start["flow_id"]})).status_code == 200
    assert (await row(client))["auth_status"] == "required"
    await sign_in(client, identity)
    servers = client.app.state.registry.mcp
    tools = await servers.tools(project, CancelToken())
    if failure == "refresh-revoked":
        oauth_server.reject_refresh = True
        servers.oauth[identity].context.token_expiry_time = time.time() - 1
    else:
        oauth_server.insufficient_scope = True
    failed = await tools[0].run("{}", CancelToken())
    assert failed.is_error and "sign in" in failed.text.lower()
    after = await row(client)
    assert after["auth_status"] == "required"
    assert oauth_server.client_secret not in json.dumps(after)
    assert oauth_server.authorizations == 1, "An agent must never start interactive reauthorization"


async def test_sign_out_cannot_be_undone_by_inflight_refresh(client, home, project, oauth_server):
    added = await client.post(BASE, json=draft(oauth_server))
    identity = added.json()["id"]
    await sign_in(client, identity)
    servers = client.app.state.registry.mcp
    tools = await servers.tools(project, CancelToken())
    servers.oauth[identity].context.token_expiry_time = time.time() - 1
    oauth_server.refresh_gate = threading.Event()
    pending = asyncio.create_task(tools[0].run("{}", CancelToken()))
    try:
        async with asyncio.timeout(5):
            while not oauth_server.refresh_started.is_set():
                await asyncio.sleep(0.01)
        assert (await client.post(f"{BASE}/{identity}/sign_out", json={"version": 1})).status_code == 200
        oauth_server.refresh_gate.set()
        await pending
        assert (await row(client))["auth_status"] == "required"
        with sqlite3.connect(home / "capabilities.sqlite3") as db:
            assert json.loads(db.execute("SELECT data FROM mcp_oauth WHERE id=?", (identity,)).fetchone()[0])["tokens"] is None
    finally:
        oauth_server.refresh_gate.set()
        await pending


async def test_oauth_config_rejects_unsafe_or_ambiguous_credentials(client, oauth_server):
    settings = draft(oauth_server)
    for override in (
        {"url": "http://public.example/mcp"},
        {"credentials": [{"name": "authorization", "value": "Bearer secret"}]},
        {"oauth": {**settings["oauth"], "issuer": "http://public.example"}},
        {"oauth": {**settings["oauth"], "issuer": ""}},
        {"oauth": {**settings["oauth"], "redirect_uri": "https://attacker.example/callback"}},
        {"oauth": {**settings["oauth"], "redirect_uri": "http://127.0.0.1:0/oauth/callback"}},
    ):
        result = await client.post(BASE, json={**settings, **override})
        assert result.status_code == 400, result.text
    assert (await client.get(BASE)).json()["servers"] == []
