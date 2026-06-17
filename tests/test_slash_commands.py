from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from lilbot.cli import (
    SLASH_COMMANDS,
    handle_slash,
    resolve_slash_command,
    slash_command_runs_agent,
    slash_commands_matching,
)
from lilbot.tools import ToolResult


class FakeAgent:
    def __init__(self):
        self.messages = [
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": "hello"},
        ]
        self.usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        self.reset_calls = 0

    def run_turn(self, _prompt):
        raise AssertionError("local slash command should not enter Agent Loop")

    def reset_conversation(self):
        self.reset_calls += 1
        self.messages = [{"role": "system", "content": "system prompt"}]
        self.usage.clear()
        return "Conversation reset. Messages now: 1"

    def compact(self):
        return "Compacted context. Messages now: 2"


class FakeRegistry:
    def __init__(self):
        self.calls = []

    def execute(self, name, args, _ctx):
        self.calls.append((name, args))
        return ToolResult(True, '{"ok": true}', {}), 0

    def list(self):
        return []


class FakeUI:
    def __init__(self):
        self.prints = []
        self.errors = []
        self.tables = []
        self.events = []
        self.cleared = False

    def print(self, value="", style=None):
        self.prints.append((str(value), style))

    def error(self, message):
        self.errors.append(str(message))

    def table(self, title, columns, rows):
        self.tables.append((title, columns, list(rows)))

    def event(self, event):
        self.events.append(event)

    def help(self, compact=False):
        self.prints.append(("help", compact))

    def theme_demo(self):
        self.prints.append(("theme", None))

    def clear_trace(self):
        self.cleared = True


def fake_ctx():
    return SimpleNamespace(
        config=SimpleNamespace(
            compact_after_messages=28,
            max_steps=20,
            model="deepseek-v4-flash",
            provider="deepseek",
            workspace="F:/project",
            font_size=22,
        ),
        permissions=SimpleNamespace(mode="ask"),
        skills=SimpleNamespace(reload=lambda: None, list=lambda: []),
        subagents=SimpleNamespace(list_types=lambda: [], list_tasks=lambda: []),
        mcp=SimpleNamespace(list_servers=lambda: [], write_example_config=lambda: "mcp.json"),
    )


class SlashCommandTests(unittest.TestCase):
    def test_registry_exposes_fast_path_command_types_and_aliases(self):
        command_types = {command.name: command.type for command in SLASH_COMMANDS}

        self.assertEqual(command_types["clear"], "local-ui")
        self.assertEqual(command_types["tokens"], "local")
        self.assertEqual(command_types["plan"], "local-ui")
        self.assertEqual(command_types["review"], "prompt")
        self.assertEqual(resolve_slash_command("p").name, "plan")

        matches = [command.name for command in slash_commands_matching("/cl")]
        self.assertIn("clear", matches)

    def test_local_tokens_and_clear_do_not_enter_agent_loop(self):
        agent = FakeAgent()
        registry = FakeRegistry()
        ctx = fake_ctx()
        ui = FakeUI()

        self.assertTrue(handle_slash("/tokens", agent, registry, ctx, ui))
        self.assertEqual(ui.tables[-1][0], "Token Usage")
        self.assertEqual(registry.calls, [])

        self.assertTrue(handle_slash("/clear", agent, registry, ctx, ui))
        self.assertTrue(ui.cleared)
        self.assertEqual(agent.reset_calls, 1)
        self.assertEqual(agent.messages, [{"role": "system", "content": "system prompt"}])
        self.assertEqual(agent.usage, {})

    def test_plan_without_task_is_local_but_plan_with_task_runs_agent(self):
        agent = FakeAgent()
        registry = FakeRegistry()
        ctx = fake_ctx()
        ui = FakeUI()

        with patch("lilbot.cli.run_prompt") as run_prompt:
            self.assertTrue(handle_slash("/plan", agent, registry, ctx, ui))
            run_prompt.assert_not_called()
            self.assertEqual(registry.calls[-1][0], "EnterPlanMode")

            self.assertTrue(handle_slash("/plan design auth module", agent, registry, ctx, ui))
            run_prompt.assert_called_once()
            self.assertIn("design auth module", run_prompt.call_args.args[2])
            self.assertEqual(registry.calls[-1][0], "EnterPlanMode")

        self.assertFalse(slash_command_runs_agent("/plan"))
        self.assertTrue(slash_command_runs_agent("/plan design auth module"))

    def test_do_exits_plan_mode_locally(self):
        agent = FakeAgent()
        registry = FakeRegistry()

        self.assertTrue(handle_slash("/do", agent, registry, fake_ctx(), FakeUI()))

        name, args = registry.calls[-1]
        self.assertEqual(name, "ExitPlanMode")
        self.assertEqual(args["approval_state"], "approved")

    def test_review_is_prompt_command(self):
        agent = FakeAgent()
        registry = FakeRegistry()
        ctx = fake_ctx()
        ui = FakeUI()

        with patch("lilbot.cli.run_prompt") as run_prompt:
            self.assertTrue(handle_slash("/review focus on concurrency", agent, registry, ctx, ui))

        run_prompt.assert_called_once()
        self.assertIn("Extra review focus: focus on concurrency", run_prompt.call_args.args[2])
        self.assertTrue(slash_command_runs_agent("/review"))


if __name__ == "__main__":
    unittest.main()
