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

    def call_tool(self, server_name: str, tool_name: str, arguments: dict[str, Any]) -> str:
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

