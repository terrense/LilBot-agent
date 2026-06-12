from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lilbot.memory import MemoryStore
from lilbot.sandbox import Sandbox, SandboxError
from lilbot.skills import SkillRegistry
from lilbot.tools import ToolRegistry, register_builtins


class CoreTests(unittest.TestCase):
    def test_sandbox_blocks_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = Sandbox(Path(tmp))
            with self.assertRaises(SandboxError):
                sandbox.resolve("../outside.txt")

    def test_memory_search(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp))
            store.add("style", "Prefer concise Chinese summaries.", kind="preference")
            hits = store.search("concise Chinese")
            self.assertEqual(hits[0].name, "style")

    def test_bundled_skills_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            skills = SkillRegistry(Path(tmp))
            self.assertIsNotNone(skills.get("review"))
            self.assertIn("hello", skills.render("summarize", "hello"))

    def test_tool_registry_has_core_tools(self):
        registry = ToolRegistry()
        register_builtins(registry)
        names = {tool.name for tool in registry.list()}
        self.assertIn("read_file", names)
        self.assertIn("agent_spawn", names)
        self.assertIn("mcp_servers", names)
        self.assertIn("web_search", names)
        self.assertIn("fetch_url", names)


if __name__ == "__main__":
    unittest.main()
