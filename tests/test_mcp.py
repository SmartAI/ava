"""Actual MCP peers, provider wire fidelity, session persistence and process lifetime."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import psutil
import pytest

from ava.base import AvaError, CancelToken
from ava.llm.anthropic import _tool_schema as anthropic_schema
from ava.llm.codex import _tool_schema as codex_schema
from ava.llm.openai import _tool_schema as openai_schema
from ava.session import Event, ToolsAdvertised
from ava.session.codec import decode_record, encode_record
from ava.tool.mcp import MCPServers, ServerConfig
from tests.test_web import client as client
from tests.test_web import scripted as scripted

FIXTURE = Path(__file__).parent / "fixtures/mcp_server.py"


@pytest.mark.parametrize("legacy", [False, True], ids=["subscriptions", "legacy-notifications"])
async def test_stdio_tools_preserve_nested_schema_and_cancel_cleanly(home, project, legacy):
    servers = MCPServers(home)
    config = ServerConfig(name="Fixture", command=sys.executable, args=[str(FIXTURE)], env={"MCP_LEGACY": "1"} if legacy else {})
    identity = servers.save(config)
    cancel = CancelToken()
    try:
        tools = await servers.tools(project, cancel)
        assert len(tools) == 2, await servers.snapshot(project)
        connection = servers.connections[(identity, str(project))]
        assert (connection.client.protocol_version < "2026-07-28") == legacy
        record = next(tool for tool in tools if "record_change" in tool.name)
        schema = record.definition.input_schema
        assert schema["$defs"]["Change"]["properties"]["labels"]["items"] == {"type": "string"}
        assert anthropic_schema(record.definition)["input_schema"] == schema
        assert codex_schema(record.definition)["parameters"] == schema
        assert openai_schema(record.definition)["function"]["parameters"] == schema
        event = Event(seq=1, at=datetime(2026, 9, 9, tzinfo=UTC), payload=ToolsAdvertised(tools=[record.definition]))
        restored = decode_record(encode_record(event))
        assert restored.payload.tools[0] == record.definition
        arguments = json.dumps({"change": {"title": "A real change", "labels": ["ux", "tests", "refresh-tools"], "approved": True}})
        result = await record.run(arguments, cancel)
        assert not result.is_error, result.text
        proof = json.loads((project / "mcp-proof.json").read_text())
        assert proof["cwd"] == str(project) and proof["change"]["labels"] == ["ux", "tests", "refresh-tools"]
        pid = proof["pid"]
        async with asyncio.timeout(5):
            while len(connection.tools) != 3:
                await asyncio.sleep(0.01)
        assert len(await servers.tools(project, cancel)) == 3
        assert servers.connections[(identity, str(project))] is connection
        other = project / "another workspace"
        other.mkdir()
        other_tools = await servers.tools(other, cancel)
        other_record = next(tool for tool in other_tools if "record_change" in tool.name)
        assert not (await other_record.run(arguments, cancel)).is_error
        other_proof = json.loads((other / "mcp-proof.json").read_text())
        assert other_proof["cwd"] == str(other) and other_proof["pid"] != pid
        slow = next(tool for tool in tools if "slow_check" in tool.name)
        stopped = CancelToken()
        pending = asyncio.create_task(slow.run("{}", stopped))
        async with asyncio.timeout(5):
            while not (project / "mcp-waiting").exists():
                await asyncio.sleep(0.01)
        config.enabled = False
        servers.save(config, identity, servers.configs()[0]["version"])
        assert (await servers.snapshot(project))[0]["active_calls"] == 1
        assert not pending.done() and psutil.pid_exists(pid)
        assert (await record.run("{}", cancel)).is_error
        stopped.cancel()
        try:
            await pending
            raise AssertionError("Cancelled calls must not be reported as success")
        except AvaError:
            pass
        async with asyncio.timeout(5):
            while not (project / "mcp-cancelled").exists():
                await asyncio.sleep(0.01)
        assert not await servers.tools(project, cancel)
        assert (await record.run("{}", cancel)).is_error
    finally:
        await servers.aclose()
    assert not psutil.pid_exists(pid)
    assert not psutil.pid_exists(other_proof["pid"])


async def test_http_mcp_management_auth_and_restart(client, home, project):
    from uuid import uuid4

    import httpx

    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    secret = "Bearer synthetic-mcp-secret"
    process = subprocess.Popen([sys.executable, str(FIXTURE), str(port)], cwd=project,
                               env={**os.environ, "MCP_EXPECTED_AUTH": secret}, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        async with httpx.AsyncClient(timeout=1) as probe, asyncio.timeout(10):
            while True:
                try:
                    assert (await probe.get(f"http://127.0.0.1:{port}/mcp")).status_code == 401
                    break
                except httpx.TransportError:
                    assert process.poll() is None
                    await asyncio.sleep(0.03)
        base = "/api/projects/workspace/mcp"
        draft = {"name": "HTTP fixture", "transport": "http", "url": f"http://127.0.0.1:{port}/mcp",
                 "credentials": [{"name": "Authorization", "value": "Bearer invalid-fixture-token"}], "request_id": uuid4().hex}
        added = await client.post(base, json=draft)
        assert added.status_code == 201, added.text
        identity = added.json()["id"]
        assert (await client.post(base, json=draft)).json()["id"] == identity
        listing = await client.get(base)
        assert secret not in listing.text
        row = listing.json()["servers"][0]
        assert row["status"] == "error" and "401" in row["error"], row
        assert "invalid-fixture-token" not in listing.text
        repair = {**draft, "version": row["version"], "credentials": [{"name": "Authorization", "value": secret}]}
        assert (await client.post(base + "/" + identity, json=repair)).status_code == 200
        row = (await client.get(base)).json()["servers"][0]
        assert row["status"] == "connected" and len(row["tools"]) == 2, row
        changed = {**draft, "name": "Renamed HTTP server", "version": row["version"],
                   "credentials": [{"name": "Authorization", "value": None}]}
        assert (await client.post(base + "/" + identity, json=changed)).status_code == 200
        row = (await client.get(base)).json()["servers"][0]
        assert row["status"] == "connected", row
        assert (await client.post(base + "/" + identity, json=changed)).status_code == 400
        assert (await client.post(base + "/" + identity + "/refresh", json={"version": row["version"]})).json()["status"] == "connected"
        tools = await client.app.state.registry.mcp.tools(project, CancelToken())
        tool = next(tool for tool in tools if "record_change" in tool.name)
        result = await tool.run(json.dumps({"change": {"title": "HTTP tool call", "labels": ["mcp", "refresh-tools"], "approved": True}}), CancelToken())
        assert not result.is_error, result.text
        assert json.loads((project / "mcp-proof.json").read_text())["change"]["title"] == "HTTP tool call"
        async with asyncio.timeout(5):
            while len((await client.get(base)).json()["servers"][0]["tools"]) != 3:
                await asyncio.sleep(0.01)
        fresh = MCPServers(home)
        try:
            # A new owner connects using durable configuration, with no credential round trip through the UI.
            assert len(await fresh.tools(project, CancelToken())) == 3
        finally:
            await fresh.aclose()
        assert (await client.get(base, params={"cwd": str(project.parent)})).status_code == 404
        response = await client.request("DELETE", base + "/" + identity, json={"version": row["version"]})
        assert response.status_code == 200, response.text
        assert (await client.get(base)).json()["servers"] == []
    finally:
        process.terminate()
        try:
            await asyncio.to_thread(process.wait, 5)
        except subprocess.TimeoutExpired:
            process.kill()
            await asyncio.to_thread(process.wait, 5)
        process.stderr.close()


async def test_idle_mcp_process_is_released_and_reopens_for_next_call(home, project, monkeypatch):
    import ava.tool.mcp as mcp

    monkeypatch.setattr(mcp, "IDLE_SECONDS", 0.15, raising=False)
    servers = MCPServers(home)
    servers.save(ServerConfig(name="Idle process", command=sys.executable, args=[str(FIXTURE)]))
    args = json.dumps({"change": {"title": "Wake up", "labels": [], "approved": True}})
    try:
        tools = await servers.tools(project, CancelToken())
        record = next(tool for tool in tools if "record_change" in tool.name)
        assert not (await record.run(args, CancelToken())).is_error
        first_pid = json.loads((project / "mcp-proof.json").read_text())["pid"]
        async with asyncio.timeout(3):
            while psutil.pid_exists(first_pid):
                await asyncio.sleep(0.02)
        assert not (await record.run(args, CancelToken())).is_error
        second_pid = json.loads((project / "mcp-proof.json").read_text())["pid"]
        assert second_pid != first_pid
        token = CancelToken()
        slow = next(tool for tool in tools if "slow_check" in tool.name)
        pending = asyncio.create_task(slow.run("{}", token))
        async with asyncio.timeout(3):
            while not (project / "mcp-waiting").exists():
                await asyncio.sleep(0.01)
        await asyncio.sleep(0.35)
        assert not pending.done() and psutil.pid_exists(second_pid)
        token.cancel()
        with pytest.raises(AvaError):
            await pending
    finally:
        await servers.aclose()
    assert not psutil.pid_exists(second_pid)
