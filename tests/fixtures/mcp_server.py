"""A real protocol peer with nested schemas, observable calls and cancellation."""

from __future__ import annotations

import asyncio
import base64
import json
import os
from pathlib import Path

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.types import METHOD_NOT_FOUND, ImageContent, TextContent
from pydantic import BaseModel

server = MCPServer("Ava acceptance server", version="1.0")


class Change(BaseModel):
    title: str
    labels: list[str]
    approved: bool


@server.tool()
async def record_change(change: Change, ctx: Context) -> dict:
    """Record an approved change in this workspace, preserving its nested fields."""
    value = {"cwd": str(Path.cwd()), "change": change.model_dump(), "pid": os.getpid()}
    Path("mcp-proof.json").write_text(json.dumps(value))
    if "refresh-tools" in change.labels:
        server.add_tool(catalog_entry, name="new_workspace_check")
        if ctx.protocol_version and ctx.protocol_version >= "2026-07-28":
            await ctx.notify_tools_changed()
        else:
            await ctx.session.send_tool_list_changed()
    return value


@server.tool()
async def slow_check() -> str:
    """Wait until cancelled, recording cleanup in the workspace."""
    Path("mcp-waiting").write_text(str(os.getpid()))
    try:
        await asyncio.sleep(120)
        return "Finished waiting"
    finally:
        Path("mcp-cancelled").write_text("cancelled")


def catalog_entry(query: str = "") -> str:
    """Search this workspace's indexed source files and explain the matching entries."""
    return query or "Workspace index is ready."


if os.environ.get("MCP_IMAGE_PATH"):
    @server.tool()
    def inspect_page() -> list[TextContent | ImageContent]:
        """Return the captured page so the model can inspect its actual pixels."""
        return [TextContent(type="text", text="Captured browser viewport"),
                ImageContent(type="image", mime_type="image/png",
                             data=base64.b64encode(Path(os.environ["MCP_IMAGE_PATH"]).read_bytes()).decode())]


for index in range(max(0, int(os.environ.get("MCP_TOOL_COUNT", "2")) - 2)):
    server.add_tool(catalog_entry, name=f"workspace_check_{index:04}")


if __name__ == "__main__":
    import sys

    if len(sys.argv) == 2:
        import uvicorn
        from starlette.responses import JSONResponse

        app = server.streamable_http_app(json_response=True)

        async def authenticated(scope, receive, send):
            expected = os.environ.get("MCP_EXPECTED_AUTH", "")
            if scope["type"] == "http" and expected and dict(scope["headers"]).get(b"authorization") != expected.encode():
                await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
                return
            await app(scope, receive, send)

        uvicorn.run(authenticated, host="127.0.0.1", port=int(sys.argv[1]), log_level="error")
    else:
        if os.environ.get("MCP_LEGACY"):
            # Reject discovery before the SDK adopts a modern connection. It then serves
            # the real 2025 initialize/call/cancel/notification protocol on the same pipe.
            probe = json.loads(sys.stdin.readline())
            assert probe["method"] == "server/discover"
            print(json.dumps({"jsonrpc": "2.0", "id": probe["id"],
                              "error": {"code": METHOD_NOT_FOUND, "message": "Unknown method"}}), flush=True)
        server.run()
