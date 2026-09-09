"""Tool screenshots must survive the real protocol, agent turn and durable replay."""

from __future__ import annotations

import base64
import sys

import pytest

from ava.agent import Agent
from ava.base import CancelToken
from ava.llm.types import Role
from ava.tool.mcp import MCPServers, ServerConfig
from tests.conftest import ScriptedProvider, message, text_response, tool_call_response
from tests.test_mcp import FIXTURE

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=")


async def test_mcp_image_reaches_model_and_survives_reopen(home, project):
    screenshot = project / "browser.png"
    screenshot.write_bytes(PNG)
    servers = MCPServers(home)
    servers.save(ServerConfig(name="Browser fixture", command=sys.executable,
                              args=[str(FIXTURE)], env={"MCP_IMAGE_PATH": str(screenshot)}))
    agent = None
    try:
        tools = await servers.tools(project, CancelToken())
        capture = next(tool for tool in tools if "inspect_page" in tool.name)
        output = await capture.run("{}", CancelToken())
        assert not output.is_error, output.text
        provider = ScriptedProvider([tool_call_response("capture-1", capture.name, "{}"), text_response("I can see the page")])
        agent = Agent.create(provider, project, tools=tools)
        await agent.followup(message("Inspect the captured page"))
        await agent.drive()
        result = next(item for item in provider.contexts[1].items if item.role == Role.tool).blocks[0]
        assert result.attachments[0].bytes == PNG
        path = agent.session_path
        await agent.aclose()
        agent = Agent.reopen(ScriptedProvider([text_response("Restored")]), project, path)
        restored = next(item for item in agent.state.session.model_context().items if item.role == Role.tool).blocks[0]
        assert restored == result
        assert restored.text == "Captured browser viewport"
    finally:
        if agent is not None:
            await agent.aclose()
        await servers.aclose()


def test_image_provider_shapes_and_request_limits():
    import json

    import pytest

    from ava.base import AvaError
    from ava.llm import (
        Context,
        Item,
        Selection,
        make_image_block,
        make_text_block,
        make_tool_call_block,
        make_tool_result_block,
    )
    from ava.llm.anthropic import request_body
    from ava.llm.codex import codex_input_json
    from ava.llm.openai import openai_request_body

    image = make_image_block("viewport.png", PNG, "image/png")
    first = make_tool_result_block("first", "Page one", False, [image])
    second = make_tool_result_block("second", "Could not click here", True, [image])
    context = Context(items=[
        Item(role=Role.user, blocks=[make_text_block("Inspect both pages")]),
        Item(role=Role.assistant, blocks=[make_tool_call_block("first", "browser"), make_tool_call_block("second", "browser")]),
        Item(role=Role.tool, blocks=[first]),
        Item(role=Role.tool, blocks=[second]),
        Item(role=Role.assistant, blocks=[make_text_block("Compared the pages")]),
    ])
    chat = json.loads(openai_request_body(context, "fixture", None))["messages"]
    assert [message["role"] for message in chat] == ["user", "assistant", "tool", "tool", "user", "assistant"]
    assert [message["tool_call_id"] for message in chat[2:4]] == ["first", "second"]
    assert [part["type"] for part in chat[4]["content"]] == ["text", "image_url", "text", "image_url"]
    assert "first" in chat[4]["content"][0]["text"] and "second" in chat[4]["content"][2]["text"]
    assert base64.b64decode(chat[4]["content"][1]["image_url"]["url"].split(",")[1]) == PNG
    codex = json.loads(codex_input_json(context, Selection("codex", "fixture")))
    results = [item for item in codex if item["type"] == "function_call_output"]
    assert [item["call_id"] for item in results] == ["first", "second"]
    assert results[0]["output"][0] == {"type": "input_text", "text": "Page one"}
    assert results[0]["output"][1] == {"type": "input_image", "image_url": chat[4]["content"][1]["image_url"]["url"]}
    claude = json.loads(request_body(context, "fixture", 1024))["messages"]
    assert claude[2]["content"][0]["tool_use_id"] == "first"
    assert claude[3]["content"][0]["is_error"] is True
    assert base64.b64decode(claude[3]["content"][0]["content"][1]["source"]["data"]) == PNG
    first.attachments = [image] * 21
    for serialize in (lambda: openai_request_body(context, "fixture", None),
                      lambda: codex_input_json(context, Selection("codex", "fixture")),
                      lambda: request_body(context, "fixture", 1024)):
        with pytest.raises(AvaError, match="image count"):
            serialize()


def test_recent_images_are_bounded_without_modifying_history():
    from ava.llm import Item, make_image_block, make_tool_result_block
    from ava.session import Session, ToolResult, UserMessage
    from ava.session.codec import block_from_wire, block_to_wire
    from ava.session.compaction import IMAGE_BLOCK_TOKENS, estimate_block_tokens

    session = Session()
    user = make_image_block("user.png", PNG, "image/png")
    session.append(UserMessage(item=Item(role=Role.user, blocks=[user] * 15)))
    for number in range(30):
        image = make_image_block(f"page-{number}.png", PNG, "image/png")
        session.append(ToolResult(item=Item(role=Role.tool, blocks=[make_tool_result_block(str(number), "Page", False, [image])])))
    context = session.model_context()
    results = [block for item in context.items if item.role == Role.tool for block in item.blocks]
    assert [image.display_path for block in results for image in block.attachments] == [f"page-{n}.png" for n in range(25, 30)]
    assert "omitted" in results[0].text and "omitted" not in results[-1].text
    assert all(event.payload.item.blocks[0].attachments for event in session.events[1:])
    assert all(event.payload.item.blocks[0].text == "Page" for event in session.events[1:])
    assert sum(len(item.blocks) for item in context.items if item.role == Role.user) == 15
    assert estimate_block_tokens(results[-1]) >= IMAGE_BLOCK_TOKENS
    assert block_from_wire(block_to_wire(results[-1])) == results[-1]
    # The byte budget applies independently of image count. It never mutates history.
    large = make_image_block("large.png", b"x" * 5_000_000, "image/png")
    session.append(ToolResult(item=Item(role=Role.tool, blocks=[make_tool_result_block("large", "Large capture", False, [large, large])])))
    assert sum(len(image.bytes) for item in session.model_context().items for block in item.blocks for image in block.attachments) < 8 * 1024 * 1024
    assert len(session.events[-1].payload.item.blocks[0].attachments) == 2


async def test_mcp_rejects_invalid_or_oversized_image_before_model_io(home, project):
    from ava.base.images import IMAGE_BYTE_LIMIT

    image_path = project / "invalid.png"
    servers = MCPServers(home)
    servers.save(ServerConfig(name="Bad images", command=sys.executable, args=[str(FIXTURE)],
                              env={"MCP_IMAGE_PATH": str(image_path)}))
    try:
        tools = await servers.tools(project, CancelToken())
        capture = next(tool for tool in tools if "inspect_page" in tool.name)
        for data, expected in ((b"<svg></svg>", "unsupported image header"),
                               (b"x" * (IMAGE_BYTE_LIMIT + 1), "7.5 MB")):
            image_path.write_bytes(data)
            result = await capture.run("{}", CancelToken())
            assert result.is_error and expected in result.text
            assert not result.attachments
    finally:
        await servers.aclose()


async def test_abort_preserves_completed_screenshot(home, project):
    from ava.agent import CancelCause
    from ava.llm import ToolDef, make_image_block
    from ava.session import ToolResult
    from ava.tool import Output, Tool

    async def capture(arguments, cancel):
        agent.cancel(CancelCause.user_abort)
        return Output("Captured before stop", attachments=[make_image_block("stopped.png", PNG, "image/png")])

    provider = ScriptedProvider([tool_call_response("stopped", "capture", "{}")])
    agent = Agent.create(provider, project, tools=[Tool(ToolDef("capture", "Capture the page"), capture)])
    try:
        await agent.followup(message("Capture"))
        await agent.drive()
        results = [event.payload for event in agent.state.session.events if isinstance(event.payload, ToolResult)]
        assert len(results) == 1
        assert results[0].item.blocks[0].attachments[0].bytes == PNG
    finally:
        await agent.aclose()


@pytest.mark.parametrize("encoding", ["plain", "zstd"])
def test_image_history_memory_integrity_and_single_file_portability(project, tmp_path, monkeypatch, encoding):
    import gc
    import hashlib
    import os
    import shutil
    import struct
    import tracemalloc
    import zlib

    from ava.app.web.events import event_dict
    from ava.base import AvaError
    from ava.llm import Item, make_image_block, make_tool_result_block
    from ava.session import Log, OpenMode, Session, SessionStart, ToolResult
    from ava.session import log as log_module
    from ava.session.log import PhysicalEncoding
    from ava.session.writer import SessionWriter

    def png():
        def chunk(kind, data):
            return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        rows = b"".join(b"\0" + os.urandom(512 * 3) for _ in range(256))
        return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 512, 256, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(rows, 1)) + chunk(b"IEND", b""))

    suffix = ".jsonl.zst" if encoding == "zstd" else ".jsonl"
    path = tmp_path / ("images" + suffix)
    log = Log.create(path, SessionStart(id="images", cwd=str(project), provider="fixture", model="fixture", format=1), PhysicalEncoding(encoding))
    session = Session(log.take_loaded_events())
    writer = SessionWriter(session, log)
    fingerprints = []
    tracemalloc.start()
    try:
        for index in range(24):
            data = png()
            fingerprints.append(hashlib.sha256(data).hexdigest())
            writer.acknowledge(ToolResult(item=Item(role=Role.tool, blocks=[
                make_tool_result_block(str(index), "Captured", False, [make_image_block(f"{index}.png", data, "image/png")]),
            ])))
        del data
        writer.sync()
        gc.collect()
        assert tracemalloc.get_traced_memory()[0] < 4 * 1024 * 1024
        writer.close()
        del session, writer, log
        gc.collect()
        tracemalloc.reset_peak()
        before = tracemalloc.get_traced_memory()[0]
        restored = Log.open(path, OpenMode.read_only)
        assert tracemalloc.get_traced_memory()[1] - before < 12 * 1024 * 1024
        restored.close()
    finally:
        tracemalloc.stop()

    reads = []
    pread = os.pread
    def tracked(fd, size, offset):
        value = pread(fd, size, offset)
        reads.append(len(value))
        return value
    monkeypatch.setattr(log_module.os, "pread", tracked)
    assert Log.read_header(path).id == "images"
    assert sum(reads) < 128 * 1024, "Project discovery must only read the session header"
    reads.clear()
    for event in restored.loaded_events:
        event_dict(event)
    Session(restored.loaded_events).model_context()
    assert not reads, "Event replay and context budgeting must not load image bytes"
    image = restored.loaded_events[1].payload.item.blocks[0].attachments[0]
    assert hashlib.sha256(bytes(image.bytes)).hexdigest() == fingerprints[0]
    count = len(reads)
    assert count and sum(reads) < 1024 * 1024
    assert hashlib.sha256(bytes(image.bytes)).hexdigest() == fingerprints[0]
    assert len(reads) == count, "Repeated image reads must hit the bounded cache"

    original = path.read_bytes()
    unread = restored.loaded_events[2].payload.item.blocks[0].attachments[0]
    path.write_bytes(b"corrupt")
    with pytest.raises(AvaError, match="image source"):
        bytes(unread.bytes)
    path.write_bytes(original)
    assert hashlib.sha256(bytes(unread.bytes)).hexdigest() == fingerprints[1]
    copied = tmp_path / ("copied" + suffix)
    shutil.copyfile(path, copied)
    path.unlink()
    portable = Log.open(copied, OpenMode.read_only)
    portable.close()
    assert len(portable.loaded_events) == 25
    for event, fingerprint in zip(portable.loaded_events[1:], fingerprints, strict=True):
        assert hashlib.sha256(bytes(event.payload.item.blocks[0].attachments[0].bytes)).hexdigest() == fingerprint
    assert log_module._IMAGE_CACHE_BYTES <= log_module._IMAGE_CACHE_LIMIT
    assert len(log_module._IMAGE_CACHE) <= 16
    if encoding == "zstd":
        from ava.llm import make_tool_call_block
        from ava.session import AssistantMessage, StepStart, TurnStart

        # Reproduce a crash after the image record but before its frame checksum.
        torn_path = tmp_path / "torn.jsonl.zst"
        active = Log.create_at(torn_path, project, "fixture", "fixture")
        active.append_batch([TurnStart(turn=1), StepStart(turn=1, step=1),
                             AssistantMessage(attempt_id="capture", item=Item(role=Role.assistant,
                                 blocks=[make_tool_call_block("capture", "browser")]))])
        data = png()
        active.append(ToolResult(item=Item(role=Role.tool, blocks=[
            make_tool_result_block("capture", "Captured", False, [make_image_block("tail.png", data, "image/png")]),
        ])))
        active.close()
        torn_path.write_bytes(torn_path.read_bytes()[:-3])
        partial = Log.open(torn_path, OpenMode.read_only)
        partial.close()
        tail = partial.loaded_events[-1].payload.item.blocks[0].attachments[0]
        assert bytes(tail.bytes) == data
        repaired = Log.open(torn_path, OpenMode.repair)
        repaired.close()
        result = next(event.payload for event in repaired.loaded_events if isinstance(event.payload, ToolResult))
        assert bytes(result.item.blocks[0].attachments[0].bytes) == data
        assert bytes(tail.bytes) == data
