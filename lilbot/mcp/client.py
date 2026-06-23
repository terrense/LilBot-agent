"""Synchronous MCP stdio client (port of mewcode's async MCPClient).

mewcode uses the official async `mcp` SDK; LilBot is synchronous, so this is a
minimal, dependency-free JSON-RPC-2.0-over-stdio client: a persistent subprocess
plus a background reader thread. It performs the MCP `initialize` handshake,
then supports `tools/list` discovery and `tools/call`.
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
from typing import Any

PROTOCOL_VERSION = "2024-11-05"


def _resolve_env(env: dict[str, str]) -> dict[str, str]:
    """Expand ${VAR} / $VAR references against the current environment."""
    out: dict[str, str] = {}
    for k, v in (env or {}).items():
        out[k] = os.path.expandvars(str(v))
    return out


class StdioMCPClient:
    def __init__(
        self,
        name: str,
        command: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.name = name
        self.command = command
        self.args = args or []
        self.env = env or {}
        self.cwd = cwd
        self.timeout = timeout
        self._proc: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._id = 0
        self._id_lock = threading.Lock()
        self._pending: dict[int, queue.Queue] = {}
        self._pending_lock = threading.Lock()
        self.alive = False
        self.tools: list[dict[str, Any]] = []
        self.server_info: dict[str, Any] = {}

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        child_env = {**os.environ, **_resolve_env(self.env)}
        self._proc = subprocess.Popen(
            [self.command, *self.args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            env=child_env,
            cwd=self.cwd,
        )
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        result = self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "clientInfo": {"name": "LilBot", "version": "0.1"},
        }, timeout=min(self.timeout, 15.0))
        self.server_info = result.get("serverInfo", {}) if isinstance(result, dict) else {}
        self._notify("notifications/initialized")
        self.alive = True

    def close(self) -> None:
        self.alive = False
        if self._proc is not None:
            try:
                self._proc.terminate()
            except OSError:
                pass

    # -- transport --------------------------------------------------------

    def _read_loop(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        for line in self._proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            mid = msg.get("id")
            if mid is None:
                continue  # a notification / log message — ignore
            with self._pending_lock:
                q = self._pending.get(mid)
            if q is not None:
                q.put(msg)

    def _next_id(self) -> int:
        with self._id_lock:
            self._id += 1
            return self._id

    def _write(self, payload: dict[str, Any]) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise RuntimeError(f"MCP server '{self.name}' is not running")
        self._proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self._proc.stdin.flush()

    def _request(self, method: str, params: dict[str, Any] | None = None, timeout: float | None = None) -> Any:
        mid = self._next_id()
        q: queue.Queue = queue.Queue()
        with self._pending_lock:
            self._pending[mid] = q
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": mid, "method": method}
        if params is not None:
            payload["params"] = params
        try:
            self._write(payload)
            msg = q.get(timeout=timeout if timeout is not None else self.timeout)
        except queue.Empty:
            raise TimeoutError(f"MCP request '{method}' to '{self.name}' timed out")
        finally:
            with self._pending_lock:
                self._pending.pop(mid, None)
        if isinstance(msg, dict) and "error" in msg:
            err = msg["error"]
            raise RuntimeError(str(err.get("message") if isinstance(err, dict) else err))
        return msg.get("result", {}) if isinstance(msg, dict) else {}

    def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        self._write(payload)

    # -- MCP methods ------------------------------------------------------

    def list_tools(self) -> list[dict[str, Any]]:
        result = self._request("tools/list")
        tools = result.get("tools", []) if isinstance(result, dict) else []
        self.tools = [t for t in tools if isinstance(t, dict) and t.get("name")]
        return self.tools

    def call_tool(self, name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        """Call an MCP tool. Returns (text, is_error)."""
        result = self._request("tools/call", {"name": name, "arguments": arguments or {}})
        if not isinstance(result, dict):
            return str(result), False
        content = result.get("content", [])
        parts = [
            str(c.get("text", ""))
            for c in content
            if isinstance(c, dict) and c.get("type") == "text"
        ]
        text = "\n".join(p for p in parts if p)
        return text or json.dumps(result, ensure_ascii=False), bool(result.get("isError"))
