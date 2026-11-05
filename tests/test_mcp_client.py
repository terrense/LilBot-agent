"""Tests for M7 — real MCP client (handshake, discovery, call, registration)."""
from __future__ import annotations

import json
import os
import sys
import textwrap
from types import SimpleNamespace

import pytest

from lilbot.mcp.client import StdioMCPClient
from lilbot.mcp.manager import MCPManager
from lilbot.sandbox.permissions import PermissionManager
from lilbot.tools import ToolRegistry
from lilbot.tools.registry import ApprovalRequirement, ToolCapability

# A minimal MCP server that speaks JSON-RPC 2.0 over stdio.
_FAKE_SERVER = textwrap.dedent(
    """
    import sys, json, os
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
                 "annotations": {"readOnlyHint": True},
                 "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}},
                                 "required": ["text"]}}]}})
        elif method == "tools/call":
            params = msg.get("params", {}); args = params.get("arguments", {})
            if params.get("name") == "echo":
                send({"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text", "text": "echo: " + str(args.get("text", ""))}]}})
            elif params.get("name") == "env":
                values = {name: os.environ.get(name) for name in args.get("names", [])}
                send({"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text", "text": json.dumps(values)}]}})
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


@pytest.mark.parametrize("configured_env", [
    {},
    {"SERVICE_TOKEN": "${LILBOT_API_KEY}"},
    {"LILBOT_API_KEY": "${LILBOT_API_KEY}"},
])
def test_mcp_child_only_receives_explicit_credentials(server_script, monkeypatch, configured_env):
    fake_key = "day7-fake-model-key-for-mcp"
    monkeypatch.setenv("LILBOT_API_KEY", fake_key)
    monkeypatch.setenv("DAY7_MCP_PUBLIC", "visible")
    monkeypatch.delenv("LILBOT_CHILD_ENV_ALLOW", raising=False)
    names = ["LILBOT_API_KEY", "SERVICE_TOKEN", "DAY7_MCP_PUBLIC", "PATH", "SystemRoot"]
    client = StdioMCPClient("env", sys.executable, [server_script], env=configured_env, timeout=10)
    client.start()
    process = client._proc
    try:
        output, is_error = client.call_tool("env", {"names": names})
        assert not is_error
        values = json.loads(output)
        assert values["LILBOT_API_KEY"] == (fake_key if "LILBOT_API_KEY" in configured_env else None)
        assert values["SERVICE_TOKEN"] == (fake_key if "SERVICE_TOKEN" in configured_env else None)
        assert values["DAY7_MCP_PUBLIC"] == "visible"
        assert values["PATH"] == os.environ.get("PATH")
        assert values["SystemRoot"] == os.environ.get("SystemRoot")
        assert os.environ["LILBOT_API_KEY"] == fake_key
    finally:
        client.close()
    assert process is not None and process.poll() is not None


@pytest.mark.parametrize("configured_env", [{}, {"SERVICE_TOKEN": "${LILBOT_API_KEY}"}])
def test_legacy_mcp_child_scrubs_and_applies_configured_env(tmp_path, monkeypatch, configured_env):
    fake_key = "day7-fake-model-key-for-legacy-mcp"
    monkeypatch.setenv("LILBOT_API_KEY", fake_key)
    monkeypatch.setenv("DAY7_MCP_PUBLIC", "visible")
    monkeypatch.delenv("LILBOT_CHILD_ENV_ALLOW", raising=False)
    script = tmp_path / "legacy_env.py"
    script.write_text(
        "import json, os, sys\n"
        "json.loads(sys.stdin.readline())\n"
        "names = ['LILBOT_API_KEY', 'SERVICE_TOKEN', 'DAY7_MCP_PUBLIC', 'PATH', 'SystemRoot']\n"
        "print(json.dumps({name: os.environ.get(name) for name in names}))\n",
        encoding="utf-8",
    )
    state = tmp_path / ".lilbot"
    state.mkdir()
    (state / "mcp.json").write_text(json.dumps({
        "servers": {"env": {"command": sys.executable, "args": [str(script)], "env": configured_env}}
    }), encoding="utf-8")
    manager = MCPManager(state, tmp_path)
    values = json.loads(manager.call_tool("env", "env", {}))
    assert values["LILBOT_API_KEY"] is None
    assert values["SERVICE_TOKEN"] == (fake_key if "SERVICE_TOKEN" in configured_env else None)
    assert values["DAY7_MCP_PUBLIC"] == "visible"
    assert values["PATH"] == os.environ.get("PATH")
    assert values["SystemRoot"] == os.environ.get("SystemRoot")


def test_failed_handshake_closes_child(tmp_path):
    script = tmp_path / "reject_initialize.py"
    script.write_text(
        "import json, sys\n"
        "for line in sys.stdin:\n"
        "    message = json.loads(line)\n"
        "    print(json.dumps({'jsonrpc': '2.0', 'id': message.get('id'), "
        "'error': {'code': -32601, 'message': 'handshake rejected'}}), flush=True)\n",
        encoding="utf-8",
    )
    client = StdioMCPClient("rejected", sys.executable, [str(script)], timeout=10)
    with pytest.raises(RuntimeError, match="handshake rejected"):
        client.start()
    assert client._proc is None
    assert not client.alive


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
        assert tool.approval_requirement == ApprovalRequirement.Required
        assert ToolCapability.RequiresApproval in tool.criteria
        # It is NOT in the per-turn payload (deferred) but is discoverable.
        assert "mcp__fake__echo" in registry.deferred_tool_names()
        # And it actually invokes the server.
        ctx = SimpleNamespace(permissions=PermissionManager(state, "accept-all"), config=None)
        result, _ms = registry.execute("mcp__fake__echo", {"text": "world"}, ctx)
        assert result.ok
        assert result.output == "echo: world"
    finally:
        mgr.shutdown()


@pytest.mark.parametrize("mode,answer,allowed", [
    ("ask", "n", False),
    ("ask", "y", True),
    ("deny-all", "y", False),
])
def test_discovered_mcp_calls_require_permission(tmp_path, server_script, monkeypatch, mode, answer, allowed):
    state = tmp_path / ".lilbot"
    state.mkdir()
    (state / "mcp.json").write_text(json.dumps({
        "servers": {"fake": {"command": sys.executable, "args": [server_script]}}
    }), encoding="utf-8")
    manager = MCPManager(state, tmp_path)
    registry = ToolRegistry()
    prompts = []
    calls = []
    permissions = PermissionManager(state, mode, prompt=lambda text: prompts.append(text) or answer)
    permissions.quiet = True
    ctx = SimpleNamespace(permissions=permissions, config=None)
    try:
        assert manager.connect_and_register(registry) == 1
        client = manager._clients["fake"]
        call_tool = client.call_tool

        def track_call(name, arguments):
            calls.append(name)
            return call_tool(name, arguments)

        monkeypatch.setattr(client, "call_tool", track_call)
        result, _ = registry.execute("mcp__fake__echo", {"text": "hello"}, ctx)
        assert result.ok is allowed
        assert calls == (["echo"] if allowed else [])
        assert len(prompts) == (1 if mode == "ask" else 0)
    finally:
        manager.shutdown()


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
