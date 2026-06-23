from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class MCPServer:
    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    protocol: str = "jsonrpc-lines"


class MCPManager:
    """Small MCP-style adapter.

    This is intentionally conservative: it reads `.lilbot/mcp.json` and can call
    simple JSON-RPC-over-lines servers. Full MCP transports can be added behind
    this interface later.
    """

    def __init__(self, state_dir: Path, workspace: Path):
        self.state_dir = state_dir
        self.workspace = workspace
        self.config_path = state_dir / "mcp.json"
        self.servers = self._load()
        # Persistent MCP clients (M7), keyed by server name.
        self._clients: dict[str, Any] = {}
        self.connect_errors: list[str] = []

    def _load(self) -> dict[str, MCPServer]:
        if not self.config_path.exists():
            return {}
        try:
            raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        servers: dict[str, MCPServer] = {}
        for name, data in raw.get("servers", {}).items():
            servers[name] = MCPServer(name=name, **data)
        return servers

    def list_servers(self) -> list[MCPServer]:
        return sorted(self.servers.values(), key=lambda server: server.name)

    def write_example_config(self) -> Path:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        if not self.config_path.exists():
            example = {
                "servers": {
                    "example": {
                        "command": "python",
                        "args": ["path/to/server.py"],
                        "protocol": "jsonrpc-lines",
                    }
                }
            }
            self.config_path.write_text(json.dumps(example, indent=2), encoding="utf-8")
        return self.config_path

    # -- M7: persistent client + discovery + first-class registration ------

    def connect_all(self) -> list[str]:
        """Start a persistent MCP client per configured server and discover its
        tools. Best-effort: a failing server is recorded and skipped, never
        raised. Returns the list of error strings.
        """
        from .client import StdioMCPClient

        self.connect_errors = []
        for name, server in self.servers.items():
            if name in self._clients:
                continue
            try:
                client = StdioMCPClient(
                    name=name,
                    command=server.command,
                    args=list(server.args),
                    env=dict(server.env),
                    cwd=server.cwd or str(self.workspace),
                )
                client.start()
                client.list_tools()
                self._clients[name] = client
            except Exception as exc:  # noqa: BLE001 - never crash startup
                self.connect_errors.append(f"MCP server '{name}': {exc}")
        return self.connect_errors

    def register_discovered_tools(self, registry: Any) -> int:
        """Register each discovered MCP tool as a first-class deferred tool
        named ``mcp__<server>__<tool>``. Returns the number registered.
        """
        from ..tools.registry import ToolDef, ToolResult

        count = 0
        for server_name, client in self._clients.items():
            for tool in getattr(client, "tools", []):
                mcp_name = str(tool.get("name") or "")
                if not mcp_name:
                    continue
                full = f"mcp__{server_name}__{mcp_name}"
                schema = tool.get("inputSchema") or {"type": "object", "properties": {}}
                desc = str(tool.get("description") or mcp_name)

                def _handler(args: dict[str, Any], ctx: Any, _c=client, _t=mcp_name) -> ToolResult:
                    try:
                        text, is_error = _c.call_tool(_t, args or {})
                    except Exception as exc:  # noqa: BLE001
                        return ToolResult(False, f"MCP tool call failed: {exc}")
                    return ToolResult(not is_error, text)

                registry.register(ToolDef(full, desc, schema, _handler, should_defer=True))
                count += 1
        return count

    def connect_and_register(self, registry: Any) -> int:
        self.connect_all()
        return self.register_discovered_tools(registry)

    def shutdown(self) -> None:
        for client in self._clients.values():
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
        self._clients.clear()

    def call_tool(self, server_name: str, tool_name: str, arguments: dict[str, Any]) -> str:
        # Prefer a live persistent client (M7); fall back to the legacy one-shot.
        client = self._clients.get(server_name)
        if client is not None and getattr(client, "alive", False):
            try:
                text, is_error = client.call_tool(tool_name, arguments or {})
                return text if not is_error else f"(mcp error) {text}"
            except Exception as exc:  # noqa: BLE001
                return f"MCP tool call failed: {exc}"
        server = self.servers.get(server_name)
        if not server:
            known = ", ".join(self.servers) or "none"
            return f"Unknown MCP server '{server_name}'. Known servers: {known}"
        if server.protocol != "jsonrpc-lines":
            return f"Protocol '{server.protocol}' is not implemented yet."

        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        }
        cwd = Path(server.cwd).resolve() if server.cwd else self.workspace
        proc = subprocess.run(
            [server.command, *server.args],
            input=json.dumps(request) + "\n",
            text=True,
            capture_output=True,
            cwd=cwd,
            timeout=30,
        )
        output = (proc.stdout or "") + (proc.stderr or "")
        return output.strip() or f"MCP server exited with code {proc.returncode}"

