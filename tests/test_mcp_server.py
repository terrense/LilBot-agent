"""Tests for M8 — MCP server mode (expose LilBot tools to other MCP clients)."""
from __future__ import annotations

import io
import json

from lilbot.mcp.server import LilBotMCPServer
from lilbot.tools import ToolContext, ToolRegistry, register_builtins
from lilbot.tools.registry import ToolCapability, ToolDef, ToolResult


def _server(expose=None):
    registry = ToolRegistry()
    register_builtins(registry)
    ctx = ToolContext(None, None, None, None, None, None, None)
    return LilBotMCPServer(registry, ctx, expose), registry


def test_initialize_response():
    server, _ = _server()
    resp = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert resp["result"]["serverInfo"]["name"] == "lilbot"
    assert "tools" in resp["result"]["capabilities"]


def test_notification_returns_none():
    server, _ = _server()
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_tools_list_defaults_to_readonly():
    server, _ = _server()
    resp = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = {t["name"] for t in resp["result"]["tools"]}
    # Read-only tools exposed; write/exec tools NOT exposed by default.
    assert "read_file" in names
    assert "git_status" in names
    assert "write_file" not in names
    assert "bash" not in names


def test_tools_call_executes_exposed_tool(tmp_path):
    registry = ToolRegistry()
    registry.register(ToolDef("read_thing", "r", {"type": "object", "properties": {}},
                              lambda a, c: ToolResult(True, "the answer"), criteria=ToolCapability.READ))
    ctx = ToolContext(None, None, None, None, None, None, None)
    server = LilBotMCPServer(registry, ctx, expose_tools=["read_thing"])
    resp = server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                          "params": {"name": "read_thing", "arguments": {}}})
    assert resp["result"]["content"][0]["text"] == "the answer"
    assert resp["result"]["isError"] is False


def test_tools_call_rejects_unexposed_tool():
    server, _ = _server()
    resp = server.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                          "params": {"name": "bash", "arguments": {"command": "echo hi"}}})
    assert "error" in resp
    assert "not exposed" in resp["error"]["message"]


def test_unknown_method_errors():
    server, _ = _server()
    resp = server.handle({"jsonrpc": "2.0", "id": 5, "method": "frobnicate"})
    assert resp["error"]["code"] == -32601


def test_serve_loop_over_streams():
    server, _ = _server()
    requests = "\n".join([
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
    ]) + "\n"
    out = io.StringIO()
    server.serve(io.StringIO(requests), out)
    lines = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
    # initialize + tools/list -> 2 responses (the notification produced none).
    assert len(lines) == 2
    assert lines[0]["id"] == 1
    assert lines[1]["id"] == 2
    assert any(t["name"] == "read_file" for t in lines[1]["result"]["tools"])


def test_roundtrip_m7_client_against_m8_server(tmp_path):
    # LilBot's own M7 client driving LilBot's M8 server end-to-end via stdio.
    import os
    import sys
    import textwrap
    from lilbot.mcp.client import StdioMCPClient

    # A tiny launcher that builds a registry and serves it.
    launcher = tmp_path / "serve.py"
    launcher.write_text(textwrap.dedent(
        """
        from lilbot.mcp.server import LilBotMCPServer
        from lilbot.tools import ToolContext, ToolRegistry
        from lilbot.tools.registry import ToolCapability, ToolDef, ToolResult
        reg = ToolRegistry()
        reg.register(ToolDef("ping_tool", "p", {"type": "object", "properties": {}},
                             lambda a, c: ToolResult(True, "pong"), criteria=ToolCapability.READ))
        ctx = ToolContext(None, None, None, None, None, None, None)
        LilBotMCPServer(reg, ctx, expose_tools=["ping_tool"]).serve()
        """
    ), encoding="utf-8")

    # Ensure the launcher subprocess can import lilbot (its script dir, not the
    # project root, is on sys.path otherwise).
    client = StdioMCPClient("self", sys.executable, [str(launcher)],
                            env={"PYTHONPATH": os.getcwd()}, timeout=10)
    client.start()
    try:
        tools = client.list_tools()
        assert any(t["name"] == "ping_tool" for t in tools)
        text, is_error = client.call_tool("ping_tool", {})
        assert text == "pong" and is_error is False
    finally:
        client.close()
