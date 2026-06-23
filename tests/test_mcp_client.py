"""Tests for M7 — real MCP client (handshake, discovery, call, registration)."""
from __future__ import annotations

import json
import sys
import textwrap

import pytest

from lilbot.mcp.client import StdioMCPClient
from lilbot.mcp.manager import MCPManager
from lilbot.tools import ToolRegistry

# A minimal MCP server that speaks JSON-RPC 2.0 over stdio.
_FAKE_SERVER = textwrap.dedent(
    """
    import sys, json
    def send(obj):
        sys.stdout.write(json.dumps(obj) + "\\n"); sys.stdout.flush()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        mid = msg.get("id"); method = msg.get("method")
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": "2024-11-05", "capabilities": {},
                "serverInfo": {"name": "fake", "version": "1.0"}}})
        elif method == "notifications/initialized":
            pass
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": mid, "result": {"tools": [
                {"name": "echo", "description": "Echo text back",
                 "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}},
                                 "required": ["text"]}}]}})
        elif method == "tools/call":
            params = msg.get("params", {}); args = params.get("arguments", {})
            if params.get("name") == "echo":
                send({"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text", "text": "echo: " + str(args.get("text", ""))}]}})
            else:
                send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "unknown tool"}})
        elif mid is not None:
            send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "method not found"}})
    """
)


@pytest.fixture
def server_script(tmp_path):
    p = tmp_path / "fake_mcp_server.py"
    p.write_text(_FAKE_SERVER, encoding="utf-8")
    return str(p)


def test_client_handshake_discovery_and_call(server_script):
    client = StdioMCPClient("fake", sys.executable, [server_script], timeout=10)
    client.start()
    try:
        assert client.alive
        assert client.server_info.get("name") == "fake"
        tools = client.list_tools()
        assert [t["name"] for t in tools] == ["echo"]
        text, is_error = client.call_tool("echo", {"text": "hi"})
        assert text == "echo: hi"
        assert is_error is False
    finally:
        client.close()


def test_call_unknown_tool_raises(server_script):
    client = StdioMCPClient("fake", sys.executable, [server_script], timeout=10)
    client.start()
    try:
        with pytest.raises(RuntimeError):
            client.call_tool("does_not_exist", {})
    finally:
        client.close()


def test_manager_connects_registers_and_invokes(tmp_path, server_script):
    state = tmp_path / ".lilbot"
    state.mkdir()
    (state / "mcp.json").write_text(json.dumps({
        "servers": {"fake": {"command": sys.executable, "args": [server_script]}}
    }), encoding="utf-8")

    mgr = MCPManager(state, tmp_path)
    registry = ToolRegistry()
    try:
        count = mgr.connect_and_register(registry)
        assert count == 1
        assert mgr.connect_errors == []
        # Registered as a first-class deferred tool.
        tool = registry.get("mcp__fake__echo")
        assert tool is not None
        assert tool.should_defer is True
        # It is NOT in the per-turn payload (deferred) but is discoverable.
        assert "mcp__fake__echo" in registry.deferred_tool_names()
        # And it actually invokes the server.
        result, _ms = registry.execute("mcp__fake__echo", {"text": "world"}, None)
        assert result.ok
        assert result.output == "echo: world"
    finally:
        mgr.shutdown()


def test_manager_bad_server_is_non_fatal(tmp_path):
    state = tmp_path / ".lilbot"
    state.mkdir()
    (state / "mcp.json").write_text(json.dumps({
        "servers": {"broken": {"command": "definitely-not-a-real-command-xyz", "args": []}}
    }), encoding="utf-8")
    mgr = MCPManager(state, tmp_path)
    registry = ToolRegistry()
    count = mgr.connect_and_register(registry)  # must not raise
    assert count == 0
    assert len(mgr.connect_errors) == 1
