from __future__ import annotations

import tempfile
import unittest
import os
import sys
from pathlib import Path
from types import SimpleNamespace

from lilbot.memory import MemoryStore
from lilbot.sandbox import Sandbox, SandboxError
from lilbot.sandbox.workspace import _decode_process_output
from lilbot.skills import SkillRegistry
from lilbot.tools import ToolContext, ToolDef, ToolRegistry, ToolResult, register_builtins


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
            self.assertIsNotNone(skills.get("delegate"))
            self.assertIsNotNone(skills.get("v4-best-practices"))

    def test_skill_registry_parses_claude_style_frontmatter(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            skill_dir = state / "skills" / "deep-scan"
            (skill_dir / "refs").mkdir(parents=True)
            (skill_dir / "refs" / "notes.md").write_text("reference", encoding="utf-8")
            (skill_dir / "SKILL.md").write_text(
                """---
name: deep-scan
description: Deep scan a subsystem
aliases: [scan-deep, ds]
allowed-tools: Read, Grep, Glob
argument-hint: <area>
arguments: area
when_to_use: Use for focused project exploration.
context: fork
agent: explore
user-invocable: true
---
Base ${CLAUDE_SKILL_DIR}; area={{area}}; args=$ARGUMENTS; legacy={{args}}
""",
                encoding="utf-8",
            )
            skills = SkillRegistry(state)
            skill = skills.get("scan-deep")
            self.assertIsNotNone(skill)
            self.assertEqual(skill.mode, "fork")
            self.assertEqual(skill.allowed_tools, ["Read", "Grep", "Glob"])
            self.assertEqual(skill.agent, "explore")
            self.assertEqual(len(skill.companion_files or []), 1)
            rendered = skills.render("ds", "payments")
            self.assertIn("area=payments", rendered)
            self.assertIn(str(skill_dir), rendered)

    def test_skill_registry_hides_non_invocable_skills_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            hidden_dir = state / "skills" / "hidden-helper"
            hidden_dir.mkdir(parents=True)
            (hidden_dir / "SKILL.md").write_text(
                """---
name: hidden-helper
description: Hidden helper
user-invocable: false
---
secret
""",
                encoding="utf-8",
            )
            skills = SkillRegistry(state)
            self.assertNotIn("hidden-helper", {s.name for s in skills.list()})
            self.assertIn("hidden-helper", {s.name for s in skills.list(include_hidden=True)})

    def test_tool_registry_has_core_tools(self):
        registry = ToolRegistry()
        register_builtins(registry)
        names = {tool.name for tool in registry.list()}
        self.assertIn("read_file", names)
        self.assertIn("agent_spawn", names)
        self.assertIn("mcp_servers", names)
        self.assertIn("web_search", names)
        self.assertIn("fetch_url", names)
        self.assertIn("agent_open", names)
        self.assertIn("agent_eval", names)
        self.assertIn("agent_close", names)
        self.assertIn("Agent", names)
        self.assertIn("Task", names)
        self.assertIn("load_skill", names)
        self.assertIn("Skill", names)
        self.assertIn("update_plan", names)
        self.assertIn("git_status", names)

    def test_codewhale_style_subagent_lifecycle(self):
        from lilbot.core.events import ProviderTurn
        from lilbot.subagents import SubAgentManager

        seen_tools = []

        def provider(messages, tools):
            seen_tools.append([tool["name"] for tool in tools])
            return ProviderTurn(content="finished")

        manager = SubAgentManager(provider)
        task = manager.open("explorer", "map files", name="scan", background=False)
        self.assertEqual(task.agent_type, "explore")
        self.assertEqual(task.status, "completed")
        self.assertIn("read_file", task.allowed_tools)
        self.assertIn("grep_files", task.allowed_tools)
        self.assertNotIn("write_file", task.allowed_tools)
        projection = manager.projection(task)
        self.assertEqual(projection["name"], "scan")
        self.assertTrue(projection["terminal"])
        self.assertIn("read_file", projection["allowed_tools"])
        self.assertIn("SUMMARY:", projection["result"])

    def test_general_research_and_writing_subagent_types(self):
        from lilbot.core.events import ProviderTurn
        from lilbot.subagents import SubAgentManager

        manager = SubAgentManager(lambda messages, tools: ProviderTurn(content="finished"))

        researcher = manager.open("researcher", "research travel options", background=False)
        self.assertEqual(researcher.agent_type, "researcher")
        self.assertIn("web_search", researcher.allowed_tools)
        self.assertNotIn("write_file", researcher.allowed_tools)

        writer = manager.open("writer", "draft an essay", background=False)
        self.assertEqual(writer.agent_type, "writer")
        self.assertEqual(writer.allowed_tools, [])

        critic = manager.open("critic", "review a plan", background=False)
        self.assertEqual(critic.agent_type, "critic")
        self.assertEqual(critic.allowed_tools, [])

    def test_subagent_executes_allowed_tool_calls(self):
        from lilbot.core.events import ProviderTurn, ToolCall
        from lilbot.subagents import SubAgentManager

        registry = ToolRegistry()
        registry.register(ToolDef("read_file", "Read a file.", {"type": "object"}, lambda args, ctx: ToolResult(True, "README contents")))
        registry.register(ToolDef("write_file", "Write a file.", {"type": "object"}, lambda args, ctx: ToolResult(True, "wrote")))
        ctx = ToolContext(
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(),
        )
        seen_tools = []

        def provider(messages, tools):
            seen_tools.append([tool["name"] for tool in tools])
            if not any(message.get("role") == "tool" for message in messages):
                return ProviderTurn(tool_calls=[ToolCall("read_file", {"path": "README.md"})])
            return ProviderTurn(
                content=(
                    "SUMMARY: inspected README.\n"
                    "CHANGES: None.\n"
                    f"EVIDENCE: {messages[-1]['content']}\n"
                    "RISKS: None observed.\n"
                    "BLOCKERS: None."
                )
            )

        manager = SubAgentManager(provider)
        manager.configure_tools(registry, ctx)
        task = manager.open("explore", "read README", background=False)

        self.assertEqual(task.status, "completed")
        self.assertIn("README contents", task.result)
        self.assertIn("read_file", seen_tools[0])
        self.assertNotIn("write_file", seen_tools[0])

    def test_read_only_subagent_disallows_writes_even_with_wildcard(self):
        from lilbot.core.events import ProviderTurn
        from lilbot.subagents import SubAgentManager

        registry = ToolRegistry()
        registry.register(ToolDef("read_file", "Read a file.", {"type": "object"}, lambda args, ctx: ToolResult(True, "read")))
        registry.register(ToolDef("write_file", "Write a file.", {"type": "object"}, lambda args, ctx: ToolResult(True, "wrote")))
        manager = SubAgentManager(lambda messages, tools: ProviderTurn(content="done"))
        manager.configure_tools(registry, SimpleNamespace())
        task = manager.open("explore", "inspect", allowed_tools=["*"], background=False)
        definition = manager.definitions[task.agent_type]
        tool_names = [tool["name"] for tool in manager._tool_schemas_for_task(definition, task)]

        self.assertIn("read_file", tool_names)
        self.assertNotIn("write_file", tool_names)

    def test_custom_agent_definitions_load_from_markdown(self):
        from lilbot.core.events import ProviderTurn
        from lilbot.subagents import SubAgentManager

        with tempfile.TemporaryDirectory() as tmp:
            agents_dir = Path(tmp) / "agents"
            agents_dir.mkdir()
            (agents_dir / "security.md").write_text(
                """---
name: security-auditor
description: Review code for security issues.
tools: read_file, grep_files
writes: false
shell: read-only
model: pro
---
You are a focused security auditor. Do not edit files.
""",
                encoding="utf-8",
            )
            manager = SubAgentManager(lambda messages, tools: ProviderTurn(content="checked"), agents_dir)
            definition = manager.definitions["security-auditor"]
            self.assertEqual(definition.allowed_tools, ["read_file", "grep_files"])
            self.assertFalse(definition.writes)
            task = manager.open("security-auditor", "inspect auth", background=False)
            self.assertEqual(task.agent_type, "security-auditor")
            self.assertIn("SUMMARY:", task.result)


if __name__ == "__main__":
    unittest.main()
