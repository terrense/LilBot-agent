from __future__ import annotations

import tempfile
import unittest
import os
import sys
from pathlib import Path

from lilbot.memory import MemoryStore
from lilbot.sandbox import Sandbox, SandboxError
from lilbot.sandbox.workspace import _decode_process_output
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

    def test_subprocess_output_decode_never_raises_on_bad_bytes(self):
        self.assertEqual(_decode_process_output("hello"), "hello")
        self.assertIn("\ufffd", _decode_process_output(b"\xb4"))

    def test_sandbox_run_handles_invalid_child_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = Sandbox(Path(tmp))
            snippet = "import sys; sys.stdout.buffer.write(bytes([0xb4])); sys.stderr.buffer.write('错误'.encode('utf-8'))"
            command = f'& "{sys.executable}" -c "{snippet}"' if os.name == "nt" else f'"{sys.executable}" -c "{snippet}"'
            result = sandbox.run(command)
        self.assertTrue(result.ok)
        self.assertNotIn("UnicodeDecodeError", result.output)

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
