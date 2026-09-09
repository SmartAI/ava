"""Actual MCP peers, provider wire fidelity, session persistence and process lifetime."""

from __future__ import annotations

import asyncio
import json
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
