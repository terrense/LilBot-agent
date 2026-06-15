from __future__ import annotations

import tempfile
import unittest
import subprocess
from pathlib import Path

from lilbot.config import LilBotConfig
from lilbot.core.events import ProviderTurn
from lilbot.mcp import MCPManager
from lilbot.memory import MemoryStore
from lilbot.sandbox import PermissionManager, Sandbox
from lilbot.skills import SkillRegistry
from lilbot.subagents import SubAgentManager
from lilbot.tools import ToolContext, ToolRegistry, register_builtins


def _ctx(root: Path) -> tuple[ToolRegistry, ToolContext]:
    state = root / ".lilbot"
    cfg = LilBotConfig(workspace=root, permission_mode="accept-all")
    registry = ToolRegistry()
    register_builtins(registry)
    ctx = ToolContext(
        Sandbox(root),
        PermissionManager(state, "accept-all", interactive=False),
        MemoryStore(state),
        SkillRegistry(state),
        SubAgentManager(lambda messages, tools: ProviderTurn(content="ok")),
        MCPManager(state, root),
        cfg,
    )
    return registry, ctx


class Batch1WorkspaceToolTests(unittest.TestCase):
    def test_read_file_line_projections_and_query(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "sample.txt").write_text("alpha\nbeta\ngamma\nbeta tail\n", encoding="utf-8")
            registry, ctx = _ctx(root)

            result, _ = registry.execute("read_file", {"path": "sample.txt", "lines": "2-3"}, ctx)
            self.assertTrue(result.ok)
            self.assertIn("2 | beta", result.output)
            self.assertIn("3 | gamma", result.output)

            result, _ = registry.execute("read_file", {"path": "sample.txt", "tail": 1}, ctx)
            self.assertTrue(result.ok)
            self.assertEqual(result.output.strip(), "beta tail")

            result, _ = registry.execute("read_file", {"path": "sample.txt", "query": "beta", "context": 0}, ctx)
            self.assertTrue(result.ok)
            self.assertIn('"match_count": 2', result.output)

    def test_list_dir_filters_noisy_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "node_modules").mkdir()
            (root / "node_modules" / "noise.js").write_text("x", encoding="utf-8")
            (root / "src").mkdir()
            (root / "src" / "app.py").write_text("print('hi')", encoding="utf-8")
            registry, ctx = _ctx(root)

            result, _ = registry.execute("list_dir", {"path": ".", "max_depth": 2}, ctx)
            self.assertTrue(result.ok)
            self.assertIn("src/", result.output)
            self.assertIn("src/app.py", result.output)
            self.assertNotIn("node_modules", result.output)

    def test_grep_files_regex_context_and_handle_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pkg").mkdir()
            (root / "pkg" / "code.py").write_text("one\ndef target():\n    return 1\n", encoding="utf-8")
            registry, ctx = _ctx(root)

            result, _ = registry.execute(
                "grep_files",
                {"pattern": "def\\s+target", "path": ".", "glob": "*.py", "context": 1},
                ctx,
            )
            self.assertTrue(result.ok)
            self.assertIn("pkg/code.py:2", result.output)
            self.assertIn("return 1", result.metadata["matches"][0]["context"])

            result, _ = registry.execute("handle_read", {"handle": "pkg/code.py", "lines": "2-3"}, ctx)
            self.assertTrue(result.ok)
            self.assertIn("2 | def target():", result.output)

    def test_git_tools_return_structured_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=root, check=True)
            (root / "tracked.txt").write_text("one\n", encoding="utf-8")
            subprocess.run(["git", "add", "tracked.txt"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-m", "initial"], cwd=root, check=True, capture_output=True)
            (root / "tracked.txt").write_text("one\ntwo\n", encoding="utf-8")
            registry, ctx = _ctx(root)

            status, _ = registry.execute("git_status", {}, ctx)
            self.assertTrue(status.ok)
            self.assertFalse(status.metadata["clean"])
            self.assertEqual(status.metadata["changes"][0]["path"], "tracked.txt")

            diff, _ = registry.execute("git_diff", {}, ctx)
            self.assertTrue(diff.ok)
            self.assertIn("tracked.txt", diff.metadata["files"])

            log, _ = registry.execute("git_log", {"limit": 1}, ctx)
            self.assertTrue(log.ok)
            self.assertEqual(log.metadata["count"], 1)
            self.assertEqual(log.metadata["commits"][0]["subject"], "initial")


if __name__ == "__main__":
    unittest.main()
