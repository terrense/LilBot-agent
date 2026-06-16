from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import subprocess
from pathlib import Path
from types import SimpleNamespace

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

    def test_apply_patch_python_fallback_handles_non_git_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "sample.txt").write_text("alpha\nbeta\n", encoding="utf-8")
            registry, ctx = _ctx(root)
            ctx.sandbox.run = lambda command, timeout=30: SimpleNamespace(ok=False, output="git unavailable", returncode=127)
            patch = "\n".join(
                [
                    "--- a/sample.txt",
                    "+++ b/sample.txt",
                    "@@ -1,2 +1,2 @@",
                    " alpha",
                    "-beta",
                    "+gamma",
                ]
            )

            result, _ = registry.execute("apply_patch", {"patch": patch}, ctx)

            self.assertTrue(result.ok)
            self.assertEqual((root / "sample.txt").read_text(encoding="utf-8"), "alpha\ngamma\n")
            self.assertEqual(result.metadata["engine"], "python")
            self.assertEqual(result.metadata["git_returncode"], 127)

    def test_run_tests_writes_log_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry, ctx = _ctx(root)
            if os.name == "nt":
                command = f"& '{sys.executable}' -c \"print('artifact-ok')\""
            else:
                command = f"'{sys.executable}' -c \"print('artifact-ok')\""

            result, _ = registry.execute("run_tests", {"command": command, "timeout": 20}, ctx)

            self.assertTrue(result.ok)
            self.assertIn("artifact", result.metadata)
            artifact = root / result.metadata["artifact"]
            self.assertTrue(artifact.exists())
            self.assertIn("artifact-ok", artifact.read_text(encoding="utf-8"))

    def test_project_map_detects_frameworks_and_entrypoints(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            (root / "src" / "main.tsx").write_text("export function App() { return null }\n", encoding="utf-8")
            (root / "package.json").write_text(
                json.dumps(
                    {
                        "dependencies": {"react": "^19.0.0", "vite": "^6.0.0"},
                        "scripts": {"dev": "vite", "test": "vitest"},
                    }
                ),
                encoding="utf-8",
            )
            (root / "pyproject.toml").write_text("[tool.pytest.ini_options]\npythonpath=['.']\n", encoding="utf-8")
            registry, ctx = _ctx(root)

            result, _ = registry.execute("project_map", {"max_files": 50}, ctx)

            self.assertTrue(result.ok)
            names = {item["name"] for item in result.metadata["frameworks"]}
            self.assertIn("React", names)
            self.assertIn("Vite", names)
            self.assertIn("Pytest", names)
            self.assertIn("src/main.tsx", result.metadata["entrypoints"])

    def test_lsp_symbols_and_definition_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pkg").mkdir()
            (root / "pkg" / "code.py").write_text(
                "class Alpha:\n"
                "    def target(self):\n"
                "        return helper()\n"
                "\n"
                "def helper():\n"
                "    return 1\n",
                encoding="utf-8",
            )
            registry, ctx = _ctx(root)

            symbols, _ = registry.execute("lsp_symbols", {"path": ".", "query": "target"}, ctx)
            definition, _ = registry.execute("lsp_definition", {"symbol": "helper"}, ctx)

            self.assertTrue(symbols.ok)
            self.assertEqual(symbols.metadata["provider"], "fallback")
            self.assertEqual(symbols.metadata["symbols"][0]["name"], "target")
            self.assertTrue(definition.ok)
            self.assertEqual(definition.metadata["definitions"][0]["path"], "pkg/code.py")
            self.assertEqual(definition.metadata["definitions"][0]["line"], 5)

    def test_lsp_phase2_fallbacks_for_workspace_references_diagnostics_and_rename(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pkg").mkdir()
            (root / "pkg" / "code.py").write_text(
                "def helper():\n"
                "    return 1\n"
                "\n"
                "def caller():\n"
                "    return helper()\n",
                encoding="utf-8",
            )
            (root / "pkg" / "broken.py").write_text("def broken(:\n    pass\n", encoding="utf-8")
            registry, ctx = _ctx(root)

            workspace_symbols, _ = registry.execute("lsp_workspace_symbols", {"path": ".", "query": "helper"}, ctx)
            references, _ = registry.execute("lsp_references", {"symbol": "helper", "path": "."}, ctx)
            diagnostics, _ = registry.execute("lsp_diagnostics", {"path": "."}, ctx)
            rename, _ = registry.execute(
                "lsp_rename_preview",
                {"path": "pkg/code.py", "line": 1, "character": 4, "symbol": "helper", "new_name": "helper_v2"},
                ctx,
            )

            self.assertTrue(workspace_symbols.ok)
            self.assertEqual(workspace_symbols.metadata["provider"], "fallback")
            self.assertTrue(any(item["name"] == "helper" for item in workspace_symbols.metadata["symbols"]))
            self.assertTrue(references.ok)
            self.assertGreaterEqual(references.metadata["count"], 2)
            self.assertTrue(diagnostics.ok)
            self.assertEqual(diagnostics.metadata["diagnostics"][0]["path"], "pkg/broken.py")
            self.assertTrue(rename.ok)
            self.assertFalse(rename.metadata["applies"])
            self.assertTrue(any(item["new_text"] == "helper_v2" for item in rename.metadata["edits"]))

    def test_worktree_merge_back_dry_run_reports_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=root, check=True)
            (root / "tracked.txt").write_text("one\n", encoding="utf-8")
            subprocess.run(["git", "add", "tracked.txt"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-m", "initial"], cwd=root, check=True, capture_output=True)
            target_branch = subprocess.run(["git", "branch", "--show-current"], cwd=root, check=True, capture_output=True, text=True).stdout.strip()
            subprocess.run(["git", "worktree", "add", "-b", "feature/test", "wt", "HEAD"], cwd=root, check=True, capture_output=True)
            (root / "wt" / "tracked.txt").write_text("one\ntwo\n", encoding="utf-8")
            subprocess.run(["git", "add", "tracked.txt"], cwd=root / "wt", check=True)
            subprocess.run(["git", "commit", "-m", "feature"], cwd=root / "wt", check=True, capture_output=True)
            registry, ctx = _ctx(root)

            result, _ = registry.execute(
                "WorktreeMergeBack",
                {"path": "wt", "source_branch": "feature/test", "target_branch": target_branch, "dry_run": True},
                ctx,
            )

            self.assertTrue(result.ok)
            self.assertEqual(result.metadata["status"], "preflight")
            self.assertEqual(result.metadata["source_branch"], "feature/test")
            self.assertIn("tracked.txt", result.metadata["diff_stat"])


if __name__ == "__main__":
    unittest.main()
