"""Offline Day 7 demo. Run from the repository with python -B benchmarks/demo_day7.py."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lilbot.mcp.client import StdioMCPClient  # noqa: E402
from lilbot.sandbox import PermissionManager, Sandbox  # noqa: E402
from lilbot.tools import ToolContext, ToolDef, ToolRegistry, ToolResult  # noqa: E402
from lilbot.tools.registry import ToolCapability  # noqa: E402


def demo_permission_boundary() -> None:
    with tempfile.TemporaryDirectory(prefix="lilbot-day7-demo-") as directory:
        root = Path(directory)
        marker = root / "demo.txt"
        prompts: list[str] = []

        def deny_prompt(label: str) -> str:
            prompts.append(label)
            return "n"

        # This intentionally reproduces a handler that forgot its permission check.
        def unchecked_writer(arguments: dict, context: ToolContext) -> ToolResult:
            marker.write_text("demo", encoding="utf-8")
            return ToolResult(True, "wrote the temporary demo file")

        permissions = PermissionManager(root / ".lilbot", "ask", prompt=deny_prompt)
        permissions.quiet = True
        context = ToolContext(
            Sandbox(root), permissions, None, None, None, None,
            SimpleNamespace(state_dir=root / ".lilbot", api_key=""),
        )
        tool = ToolDef(
            "demo_writer", "Write a temporary demo file", {"type": "object"},
            unchecked_writer, criteria=ToolCapability.WRITE,
        )
        registry = ToolRegistry()
        registry.register(tool)

        tool.handler({}, context)
        print("BEFORE missing handler check -> file_exists", marker.exists(), "approval_prompts", len(prompts))
        marker.unlink()

        result, _ = registry.execute(tool.name, {}, context)
        print("AFTER registry check, answer n -> file_exists", marker.exists(), "approval_prompts", len(prompts), "ok", result.ok)

        permissions.set_mode("deny-all")
        result, _ = registry.execute(tool.name, {}, context)
        print("AFTER deny-all -> file_exists", marker.exists(), "ok", result.ok)


_MCP_PROBE = """
import json
import os
import sys
for line in sys.stdin:
    request = json.loads(line)
    if 'id' not in request:
        continue
    info = {
        'name': 'day7-demo',
        'model_key_visible': 'LILBOT_API_KEY' in os.environ,
        'configured_token_visible': 'DEMO_SERVER_TOKEN' in os.environ,
    }
    print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': {'serverInfo': info}}), flush=True)
"""


def demo_mcp_environment() -> None:
    # Use a synthetic value and only report presence, never credential contents.
    with patch.dict(os.environ, {"LILBOT_API_KEY": "day7-demo-fake-value", "LILBOT_CHILD_ENV_ALLOW": ""}):
        old = subprocess.run(
            [sys.executable, "-B", "-c", "import os; print('LILBOT_API_KEY' in os.environ)"],
            capture_output=True, text=True, check=True,
        )
        print("BEFORE inherited environment -> model_key_visible", old.stdout.strip())
        client = StdioMCPClient(
            "day7-demo", sys.executable, ["-B", "-u", "-c", _MCP_PROBE],
            env={"DEMO_SERVER_TOKEN": "${LILBOT_API_KEY}"}, timeout=5,
        )
        try:
            client.start()
            print("AFTER MCP environment -> model_key_visible", client.server_info["model_key_visible"])
            print("AFTER explicit env mapping -> configured_token_visible", client.server_info["configured_token_visible"])
        finally:
            client.close()


if __name__ == "__main__":
    demo_permission_boundary()
    demo_mcp_environment()
