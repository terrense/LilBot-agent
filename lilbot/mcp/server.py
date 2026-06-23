"""MCP server mode (port of CodeWhale's mcp_server — the inverse of M7).

LilBot can expose its own tools to *other* MCP clients (editors, other agents)
over JSON-RPC 2.0 on stdio. For safety, only read-only tools are exposed by
default; an explicit allowlist (`.lilbot/mcp_server.json` → `expose_tools`) can
widen or narrow that.

Run with: ``python -m lilbot --mcp-server``
"""
from __future__ import annotations

import json
import sys
from typing import Any, TextIO

PROTOCOL_VERSION = "2024-11-05"


def load_expose_config(state_dir: Any) -> list[str] | None:
    """Read an optional expose allowlist from .lilbot/mcp_server.json."""
    try:
        from pathlib import Path
        path = Path(state_dir) / "mcp_server.json"
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        names = data.get("expose_tools")
        if isinstance(names, list):
            return [str(n) for n in names if isinstance(n, str)]
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    return None


class LilBotMCPServer:
    def __init__(self, registry: Any, ctx: Any, expose_tools: list[str] | None = None) -> None:
        self.registry = registry
        self.ctx = ctx
        self._expose_tools = expose_tools  # None => default read-only set

    def _exposed_names(self) -> set[str]:
        registered = {s.get("name") for s in self.registry.all_schemas()}
        if self._expose_tools:
            return {n for n in self._expose_tools if n in registered}
        # Safe default: only read-only tools.
        from ..tools.builtin import READ_ONLY_TOOLS
        return {n for n in READ_ONLY_TOOLS if n in registered}

    def _tool_descriptors(self) -> list[dict[str, Any]]:
        exposed = self._exposed_names()
        out: list[dict[str, Any]] = []
        for schema in self.registry.all_schemas():
            name = schema.get("name")
            if name in exposed:
                out.append({
                    "name": name,
                    "description": schema.get("description", ""),
                    "inputSchema": schema.get("input_schema") or {"type": "object", "properties": {}},
                })
        return out

    # -- message handling (pure, testable) --------------------------------

    def handle(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        """Return a JSON-RPC response dict, or None for notifications."""
        method = msg.get("method")
        mid = msg.get("id")

        if mid is None:
            return None  # notification (e.g. notifications/initialized)

        if method == "initialize":
            return self._ok(mid, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "lilbot", "version": "0.1"},
            })
        if method == "ping":
            return self._ok(mid, {})
        if method == "tools/list":
            return self._ok(mid, {"tools": self._tool_descriptors()})
        if method == "tools/call":
            return self._handle_call(mid, msg.get("params") or {})
        return self._err(mid, -32601, f"method not found: {method}")

    def _handle_call(self, mid: Any, params: dict[str, Any]) -> dict[str, Any]:
        name = str(params.get("name") or "")
        arguments = params.get("arguments") or {}
        if name not in self._exposed_names():
            return self._err(mid, -32601, f"tool not exposed: {name}")
        try:
            result, _elapsed = self.registry.execute(name, arguments, self.ctx)
        except Exception as exc:  # noqa: BLE001
            return self._err(mid, -32603, f"tool execution error: {exc}")
        return self._ok(mid, {
            "content": [{"type": "text", "text": result.output}],
            "isError": not result.ok,
        })

    @staticmethod
    def _ok(mid: Any, result: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    @staticmethod
    def _err(mid: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}

    # -- IO loop ----------------------------------------------------------

    def serve(self, instream: TextIO | None = None, outstream: TextIO | None = None) -> None:
        instream = instream or sys.stdin
        outstream = outstream or sys.stdout
        for line in instream:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            response = self.handle(msg)
            if response is not None:
                outstream.write(json.dumps(response, ensure_ascii=False) + "\n")
                outstream.flush()
