from __future__ import annotations

import contextlib
import io
import json
import tempfile
import threading
import time
import unittest
import os
import sys
from pathlib import Path
from types import SimpleNamespace

from lilbot.memory import MemoryStore
from lilbot.sandbox import PermissionManager, Sandbox, SandboxError, analyze_powershell_command
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

    def test_powershell_safety_classifies_shell_risks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outside = analyze_powershell_command(r"Remove-Item -LiteralPath ..\outside -Recurse -Force", root)
            inside = analyze_powershell_command(r"Remove-Item -LiteralPath .\build -Recurse -Force", root)
            encoded = analyze_powershell_command("powershell -EncodedCommand AAAA", root)
            composed = analyze_powershell_command("Write-Output ok; Write-Output done > out.txt", root)

        self.assertTrue(outside["blocked"])
        self.assertEqual(outside["risk_level"], "critical")
        self.assertTrue(any(finding["rule"] == "path_outside_workspace" for finding in outside["findings"]))
        self.assertFalse(inside["blocked"])
        self.assertEqual(inside["risk_level"], "high")
        self.assertTrue(encoded["blocked"])
        self.assertTrue(any(finding["rule"] == "encoded_command" for finding in encoded["findings"]))
        self.assertFalse(composed["blocked"])
        self.assertTrue(any(finding["rule"] == "command_separator" for finding in composed["findings"]))
        self.assertTrue(any(finding["rule"] == "redirection" for finding in composed["findings"]))

    def test_powershell_safety_blocks_unsafe_shell_tool_before_execution(self):
        if os.name != "nt":
            self.skipTest("PowerShell safety integration is Windows-specific.")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / ".lilbot"
            registry = ToolRegistry()
            register_builtins(registry)
            ctx = ToolContext(
                Sandbox(root),
                PermissionManager(state, "accept-all", interactive=False),
                MemoryStore(state),
                SkillRegistry(state),
                SimpleNamespace(),
                SimpleNamespace(),
                SimpleNamespace(state_dir=state),
            )

            result, _ = registry.execute("bash", {"command": r"Remove-Item -LiteralPath ..\outside -Recurse -Force"}, ctx)

        self.assertFalse(result.ok)
        self.assertTrue(result.metadata["powershell_safety"]["blocked"])
        self.assertEqual(result.metadata["powershell_safety"]["risk_level"], "critical")

    def test_powershell_safety_summary_reaches_permission_prompt(self):
        if os.name != "nt":
            self.skipTest("PowerShell safety integration is Windows-specific.")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / ".lilbot"
            registry = ToolRegistry()
            register_builtins(registry)
            ctx = ToolContext(
                Sandbox(root),
                PermissionManager(state, "ask", prompt=lambda _: "n", interactive=True),
                MemoryStore(state),
                SkillRegistry(state),
                SimpleNamespace(),
                SimpleNamespace(),
                SimpleNamespace(state_dir=state),
            )
            stream = io.StringIO()

            with contextlib.redirect_stdout(stream):
                result, _ = registry.execute("bash", {"command": "Write-Output ok; Write-Output done"}, ctx)

        self.assertFalse(result.ok)
        self.assertIn("PowerShell safety", stream.getvalue())
        self.assertEqual(result.metadata["powershell_safety"]["risk_level"], "medium")

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
        self.assertIn("EnterPlanMode", names)
        self.assertIn("ExitPlanMode", names)
        self.assertIn("EnterWorktree", names)
        self.assertIn("ExitWorktree", names)
        self.assertIn("git_status", names)
        self.assertIn("lsp_symbols", names)
        self.assertIn("lsp_definition", names)
        self.assertIn("lsp_workspace_symbols", names)
        self.assertIn("lsp_references", names)
        self.assertIn("lsp_diagnostics", names)
        self.assertIn("lsp_rename_preview", names)
        self.assertIn("agent_transcript", names)
        self.assertIn("WorktreeMergeBack", names)

    def test_plan_mode_lifecycle_persists_approval_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / ".lilbot"
            registry = ToolRegistry()
            register_builtins(registry)
            ctx = ToolContext(
                Sandbox(root),
                PermissionManager(state, "accept-all", interactive=False),
                MemoryStore(state),
                SkillRegistry(state),
                SimpleNamespace(),
                SimpleNamespace(),
                SimpleNamespace(state_dir=state),
            )

            entered, _ = registry.execute("EnterPlanMode", {"reason": "needs design"}, ctx)
            exited, _ = registry.execute("ExitPlanMode", {"plan": "1. inspect\n2. implement"}, ctx)
            state_data = json.loads((state / "plan_mode.json").read_text(encoding="utf-8"))

        self.assertTrue(entered.ok)
        self.assertTrue(exited.ok)
        self.assertFalse(state_data["active"])
        self.assertEqual(state_data["approval_state"], "pending_approval")
        self.assertTrue(state_data["requires_approval"])

    def test_pending_plan_blocks_write_and_execution_tools_until_approved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / ".lilbot"
            registry = ToolRegistry()
            register_builtins(registry)
            ctx = ToolContext(
                Sandbox(root),
                PermissionManager(state, "accept-all", interactive=False),
                MemoryStore(state),
                SkillRegistry(state),
                SimpleNamespace(),
                SimpleNamespace(),
                SimpleNamespace(state_dir=state),
            )

            registry.execute("ExitPlanMode", {"plan": "1. design\n2. implement"}, ctx)
            blocked_write, _ = registry.execute("write_file", {"path": "blocked.txt", "content": "nope"}, ctx)
            blocked_shell, _ = registry.execute("bash", {"command": "echo nope"}, ctx)
            registry.execute("ExitPlanMode", {"plan": "approved", "approved": True}, ctx)
            allowed_write, _ = registry.execute("write_file", {"path": "allowed.txt", "content": "ok"}, ctx)
            blocked_file_exists = (root / "blocked.txt").exists()
            allowed_file_content = (root / "allowed.txt").read_text(encoding="utf-8")

        self.assertFalse(blocked_write.ok)
        self.assertEqual(blocked_write.metadata["gate"], "plan_approval")
        self.assertFalse(blocked_file_exists)
        self.assertFalse(blocked_shell.ok)
        self.assertEqual(blocked_shell.metadata["gate"], "plan_approval")
        self.assertTrue(allowed_write.ok)
        self.assertEqual(allowed_file_content, "ok")

    def test_worktree_reports_unsupported_outside_git_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / ".lilbot"
            registry = ToolRegistry()
            register_builtins(registry)
            ctx = ToolContext(
                Sandbox(root),
                PermissionManager(state, "accept-all", interactive=False),
                MemoryStore(state),
                SkillRegistry(state),
                SimpleNamespace(),
                SimpleNamespace(),
                SimpleNamespace(state_dir=state),
            )

            result, _ = registry.execute("EnterWorktree", {"path": "wt"}, ctx)

        self.assertFalse(result.ok)
        self.assertFalse(result.metadata["supported"])
        self.assertEqual(result.metadata["status"], "unsupported")

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

    def test_agent_tool_schema_renders_when_to_use_and_full_tools(self):
        from lilbot.core.events import ProviderTurn
        from lilbot.subagents import SubAgentManager, SubAgentTask

        registry = ToolRegistry()
        registry.register(ToolDef("agent_open", "Open subagent.", {"type": "object"}, lambda args, ctx: ToolResult(True, "ok")))
        registry.register(ToolDef("Agent", "Open subagent alias.", {"type": "object"}, lambda args, ctx: ToolResult(True, "ok")))
        registry.register(ToolDef("agent_eval", "Eval subagent.", {"type": "object"}, lambda args, ctx: ToolResult(True, "ok")))
        manager = SubAgentManager(lambda messages, tools: ProviderTurn(content="finished"))
        manager.tasks["sub_1"] = SubAgentTask(
            id="sub_1",
            agent_type="researcher",
            prompt="research three public facts",
            name="facts",
            status="running",
        )

        schemas = registry.schemas(manager.get_render_context())
        descriptions = {schema["name"]: schema["description"] for schema in schemas}

        self.assertIn("Available agent types and the tools they have access to:", descriptions["agent_open"])
        self.assertIn("- **researcher**:", descriptions["agent_open"])
        self.assertIn("Use for web research", descriptions["agent_open"])
        self.assertIn("web_search", descriptions["agent_open"])
        self.assertIn("Before launching a duplicate agent", descriptions["agent_open"])
        self.assertIn("fetch_url", descriptions["Agent"])
        self.assertIn("Active subagents", descriptions["agent_eval"])
        self.assertIn("facts [researcher] running", descriptions["agent_eval"])

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

    def test_explicit_empty_subagent_allowed_tools_means_no_tools(self):
        from lilbot.core.events import ProviderTurn
        from lilbot.subagents import SubAgentManager

        registry = ToolRegistry()
        registry.register(ToolDef("read_file", "Read a file.", {"type": "object"}, lambda args, ctx: ToolResult(True, "read")))
        seen_tools = []

        def provider(messages, tools):
            seen_tools.append([tool["name"] for tool in tools])
            return ProviderTurn(content="done")

        manager = SubAgentManager(provider)
        manager.configure_tools(registry, SimpleNamespace())
        task = manager.open("explore", "inspect", allowed_tools=[], background=False)

        self.assertEqual(task.allowed_tools, [])
        self.assertEqual(seen_tools[0], [])

    def test_claude_style_allowed_tool_names_expand_for_subagents(self):
        from lilbot.core.events import ProviderTurn
        from lilbot.subagents import SubAgentManager

        registry = ToolRegistry()
        registry.register(ToolDef("read_file", "Read a file.", {"type": "object"}, lambda args, ctx: ToolResult(True, "read")))
        registry.register(ToolDef("grep_files", "Search files.", {"type": "object"}, lambda args, ctx: ToolResult(True, "grep")))
        registry.register(ToolDef("write_file", "Write a file.", {"type": "object"}, lambda args, ctx: ToolResult(True, "wrote")))
        seen_tools = []

        def provider(messages, tools):
            seen_tools.append([tool["name"] for tool in tools])
            return ProviderTurn(content="done")

        manager = SubAgentManager(provider)
        manager.configure_tools(registry, SimpleNamespace())
        manager.open("custom", "inspect", allowed_tools=["Read", "Grep"], background=False)

        self.assertIn("read_file", seen_tools[0])
        self.assertIn("grep_files", seen_tools[0])
        self.assertNotIn("write_file", seen_tools[0])

    def test_custom_subagent_creation_gates_reject_missing_unknown_and_control_tools(self):
        from lilbot.core.events import ProviderTurn
        from lilbot.subagents import SubAgentGateError, SubAgentManager

        registry = ToolRegistry()
        registry.register(ToolDef("read_file", "Read a file.", {"type": "object"}, lambda args, ctx: ToolResult(True, "read")))
        registry.register(ToolDef("Agent", "Spawn an agent.", {"type": "object"}, lambda args, ctx: ToolResult(True, "spawned")))
        manager = SubAgentManager(lambda messages, tools: ProviderTurn(content="done"))
        manager.configure_tools(registry, SimpleNamespace())

        with self.assertRaises(SubAgentGateError) as missing:
            manager.open("custom", "inspect", background=False)
        with self.assertRaises(SubAgentGateError) as unknown:
            manager.open("custom", "inspect", allowed_tools=["read_file", "NoSuchTool"], background=False)
        with self.assertRaises(SubAgentGateError) as control:
            manager.open("custom", "inspect", allowed_tools=["read_file", "Agent(worker)"], background=False)

        self.assertEqual(manager.list_tasks(), [])
        self.assertEqual(missing.exception.failures[0]["gate_number"], 1)
        self.assertTrue(any(gate["gate_number"] == 2 for gate in unknown.exception.failures))
        self.assertTrue(any(gate["gate_number"] == 3 for gate in control.exception.failures))

    def test_custom_subagent_runtime_gate_rejects_tool_outside_allowlist(self):
        from lilbot.core.events import ProviderTurn, ToolCall
        from lilbot.subagents import SubAgentManager

        registry = ToolRegistry()
        registry.register(ToolDef("read_file", "Read a file.", {"type": "object"}, lambda args, ctx: ToolResult(True, "read")))
        registry.register(ToolDef("write_file", "Write a file.", {"type": "object"}, lambda args, ctx: ToolResult(True, "wrote")))

        def provider(messages, tools):
            if not any(message.get("role") == "tool" for message in messages):
                return ProviderTurn(tool_calls=[ToolCall("write_file", {"path": "x.txt", "content": "x"})])
            return ProviderTurn(
                content=(
                    "SUMMARY: denied.\n"
                    "CHANGES: None.\n"
                    f"EVIDENCE: {messages[-1]['content']}\n"
                    "RISKS: None observed.\n"
                    "BLOCKERS: None."
                )
            )

        manager = SubAgentManager(provider)
        manager.configure_tools(registry, SimpleNamespace())
        task = manager.open("custom", "try write", allowed_tools=["read_file"], background=False)

        self.assertEqual(task.status, "completed")
        self.assertIn("Gate 4", task.result)
        self.assertIn("runtime_allowed_tools", task.result)

    def test_custom_subagent_runtime_gate_rejects_control_tool_even_with_wildcard(self):
        from lilbot.core.events import ProviderTurn, ToolCall
        from lilbot.subagents import SubAgentManager

        registry = ToolRegistry()
        registry.register(ToolDef("read_file", "Read a file.", {"type": "object"}, lambda args, ctx: ToolResult(True, "read")))
        registry.register(ToolDef("Agent", "Spawn an agent.", {"type": "object"}, lambda args, ctx: ToolResult(True, "spawned")))

        def provider(messages, tools):
            if not any(message.get("role") == "tool" for message in messages):
                return ProviderTurn(tool_calls=[ToolCall("Agent", {"prompt": "nested"})])
            return ProviderTurn(
                content=(
                    "SUMMARY: denied.\n"
                    "CHANGES: None.\n"
                    f"EVIDENCE: {messages[-1]['content']}\n"
                    "RISKS: None observed.\n"
                    "BLOCKERS: None."
                )
            )

        manager = SubAgentManager(provider)
        manager.configure_tools(registry, SimpleNamespace())
        task = manager.open("custom", "try nested agent", allowed_tools=["*"], background=False)
        definition = manager.definitions[task.agent_type]
        tool_names = [tool["name"] for tool in manager._tool_schemas_for_task(definition, task)]

        self.assertNotIn("Agent", tool_names)
        self.assertEqual(task.status, "completed")
        self.assertIn("Gate 5", task.result)
        self.assertIn("runtime_role_or_policy", task.result)

    def test_subagent_transcript_is_persisted_and_exposed_as_handle(self):
        from lilbot.core.events import ProviderTurn
        from lilbot.subagents import SubAgentManager

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / ".lilbot"
            manager = SubAgentManager(lambda messages, tools: ProviderTurn(content="done"), state / "agents")
            task = manager.open("writer", "draft", background=False)
            projection = manager.projection(task)
            transcript_data = manager.transcript(task.id, after=0, limit=2)

            handle = projection["transcript_handle"]
            self.assertIsNotNone(handle)
            transcript = (root / str(handle)).read_text(encoding="utf-8")

        self.assertIn('"event": "queued"', transcript)
        self.assertIn('"event": "provider_turn"', transcript)
        self.assertIn('"event": "completed"', transcript)
        self.assertGreaterEqual(projection["progress"]["events"], 3)
        self.assertEqual(projection["progress"]["last_event"], "completed")
        self.assertIsNotNone(transcript_data)
        self.assertEqual(len(transcript_data["events"]), 2)
        self.assertTrue(transcript_data["cursor"] >= 2)

    def test_subagent_concurrency_limit_queues_extra_background_tasks(self):
        from lilbot.core.events import ProviderTurn
        from lilbot.subagents import SubAgentManager

        release = threading.Event()
        entered = 0
        entered_lock = threading.Lock()

        def provider(messages, tools):
            nonlocal entered
            with entered_lock:
                entered += 1
            release.wait(2)
            return ProviderTurn(content="done")

        manager = SubAgentManager(provider, max_concurrent=2)
        tasks = [manager.open("writer", f"task {idx}", background=True) for idx in range(4)]
        deadline = time.time() + 2
        while time.time() < deadline:
            status = manager.runtime_status()
            if status["running"] == 2 and status["queued"] == 2:
                break
            time.sleep(0.02)
        status = manager.runtime_status()

        self.assertEqual(status["max_concurrent"], 2)
        self.assertEqual(status["running"], 2)
        self.assertEqual(status["queued"], 2)
        self.assertEqual(entered, 2)

        release.set()
        deadline = time.time() + 3
        while time.time() < deadline and any(not task.terminal for task in tasks):
            time.sleep(0.02)
        self.assertTrue(all(task.status == "completed" for task in tasks))

    def test_subagent_persisted_running_tasks_resume_after_restart_when_configured(self):
        from lilbot.core.events import ProviderTurn
        from lilbot.subagents import SubAgentManager

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / ".lilbot"
            release = threading.Event()
            resumed_called = threading.Event()

            def provider(messages, tools):
                release.wait(2)
                return ProviderTurn(content="done")

            manager = SubAgentManager(provider, state / "agents", max_concurrent=1)
            task = manager.open("writer", "long task", background=True)
            deadline = time.time() + 2
            while time.time() < deadline and task.status != "running":
                time.sleep(0.02)

            def resumed_provider(messages, tools):
                resumed_called.set()
                return ProviderTurn(content="resumed")

            recovered_manager = SubAgentManager(resumed_provider, state / "agents")
            recovered = recovered_manager.get(task.id)
            self.assertIsNotNone(recovered)
            self.assertEqual(recovered.status, "queued")
            self.assertTrue(recovered.recovered)

            recovered_manager.configure_tools(SimpleNamespace(schemas=lambda: []), SimpleNamespace())
            deadline = time.time() + 3
            while time.time() < deadline and recovered.status != "completed":
                time.sleep(0.02)
            release.set()
            deadline = time.time() + 3
            while time.time() < deadline and not task.terminal:
                time.sleep(0.02)

        self.assertIsNotNone(recovered)
        self.assertEqual(recovered.status, "completed")
        self.assertTrue(recovered.recovered)
        self.assertTrue(resumed_called.is_set())
        self.assertIn("SUMMARY: resumed", recovered.result)

    def test_subagent_worktree_isolation_reports_unsupported_outside_git_repo(self):
        from lilbot.core.events import ProviderTurn
        from lilbot.subagents import SubAgentManager

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / ".lilbot"
            registry = ToolRegistry()
            register_builtins(registry)
            provider_called = False

            def provider(messages, tools):
                nonlocal provider_called
                provider_called = True
                return ProviderTurn(content="should not run")

            manager = SubAgentManager(provider, state / "agents")
            ctx = ToolContext(
                Sandbox(root),
                PermissionManager(state, "accept-all", interactive=False),
                MemoryStore(state),
                SkillRegistry(state),
                manager,
                SimpleNamespace(),
                SimpleNamespace(state_dir=state, workspace=root),
            )
            manager.configure_tools(registry, ctx)
            task = manager.open("writer", "isolated", background=False, isolation="worktree")
            projection = manager.projection(task)

        self.assertFalse(provider_called)
        self.assertEqual(task.status, "failed")
        self.assertEqual(projection["worktree"]["status"], "unsupported")
        self.assertIn("workspace is not a git repository", task.error)

    def test_forked_skill_executes_in_subagent_with_skill_allowed_tools(self):
        from lilbot.core.events import ProviderTurn
        from lilbot.subagents import SubAgentManager

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / ".lilbot"
            skill_dir = state / "skills" / "deep-scan"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                """---
name: deep-scan
description: Deep scan a subsystem
allowed-tools: Read, Grep
context: fork
agent: custom
---
Inspect {{args}}
""",
                encoding="utf-8",
            )
            seen_tools = []

            def provider(messages, tools):
                seen_tools.append([tool["name"] for tool in tools])
                return ProviderTurn(content="fork done")

            registry = ToolRegistry()
            register_builtins(registry)
            subagents = SubAgentManager(provider, state / "agents")
            ctx = ToolContext(
                Sandbox(root),
                SimpleNamespace(),
                MemoryStore(state),
                SkillRegistry(state),
                subagents,
                SimpleNamespace(),
                SimpleNamespace(state_dir=state),
            )
            subagents.configure_tools(registry, ctx)

            result, _ = registry.execute("Skill", {"skill": "deep-scan", "args": "src"}, ctx)

        self.assertTrue(result.ok)
        self.assertIn("SUMMARY: fork done", result.output)
        self.assertIn("read_file", seen_tools[0])
        self.assertIn("grep_files", seen_tools[0])
        self.assertNotIn("write_file", seen_tools[0])

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
