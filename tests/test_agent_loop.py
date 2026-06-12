from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lilbot.config import LilBotConfig, load_config
from lilbot.core.agent import Agent
from lilbot.core.events import ProviderTurn, TextDelta, ToolCall, TurnFinished
from lilbot.tools import ToolContext, ToolDef, ToolRegistry, ToolResult


class EmptyMemory:
    def context(self) -> str:
        return "(none)"


class EmptySkills:
    def list(self) -> list:
        return []


class LoopingProvider:
    def __init__(self, calls_per_turn: int = 1):
        self.calls_per_turn = calls_per_turn
        self.calls: list[tuple[list[dict], list[dict]]] = []

    def complete(self, messages: list[dict], tools: list[dict]) -> ProviderTurn:
        self.calls.append((messages, tools))
        if tools:
            calls = [ToolCall(f"noop_{idx}", {}) for idx in range(self.calls_per_turn)]
            return ProviderTurn(tool_calls=calls)
        return ProviderTurn(content="final answer from gathered results")


def make_agent(tmp: str, provider: LoopingProvider, max_steps: int) -> tuple[Agent, list[str]]:
    executed: list[str] = []
    registry = ToolRegistry()

    def handler(args, ctx):
        executed.append(ctx.current_tool)
        return ToolResult(True, f"result from {ctx.current_tool}")

    for name in ["noop_0", "noop_1"]:
        registry.register(ToolDef(name, "noop", {"type": "object"}, handler))
    ctx = ToolContext(
        sandbox=None,
        permissions=None,
        memory=EmptyMemory(),
        skills=EmptySkills(),
        subagents=None,
        mcp=None,
        config=None,
    )
    original_execute = registry.execute

    def execute(name, arguments, context):
        context.current_tool = name
        return original_execute(name, arguments, context)

    registry.execute = execute  # type: ignore[method-assign]
    cfg = LilBotConfig(workspace=Path(tmp), max_steps=max_steps)
    return Agent(cfg, provider, registry, ctx), executed


class AgentLoopTests(unittest.TestCase):
    def test_default_and_legacy_max_steps_are_ten(self):
        self.assertEqual(LilBotConfig(Path(".")).max_steps, 10)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / ".lilbot"
            state.mkdir()
            (state / "config.json").write_text(
                json.dumps({"provider": "auto", "model": "lilbot-rule-model", "max_steps": 8}),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                cfg = load_config(root)
        self.assertEqual(cfg.max_steps, 10)

    def test_step_limit_synthesizes_final_answer_without_stopped_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = LoopingProvider()
            agent, executed = make_agent(tmp, provider, max_steps=2)
            events = list(agent.run_turn("keep using tools"))

        text = "\n".join(event.text for event in events if isinstance(event, TextDelta))
        finished = [event for event in events if isinstance(event, TurnFinished)][-1]
        self.assertEqual(executed, ["noop_0", "noop_0"])
        self.assertEqual(finished.steps, 2)
        self.assertIn("final answer from gathered results", text)
        self.assertNotIn("Stopped after max_steps", text)
        self.assertEqual(provider.calls[-1][1], [])

    def test_unexecuted_tool_calls_are_not_recorded_at_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = LoopingProvider(calls_per_turn=2)
            agent, executed = make_agent(tmp, provider, max_steps=1)
            list(agent.run_turn("call two tools"))

        self.assertEqual(executed, ["noop_0"])
        assistant_calls = [
            message
            for message in agent.messages
            if message.get("role") == "assistant" and message.get("tool_calls")
        ]
        self.assertEqual(len(assistant_calls[-1]["tool_calls"]), 1)


if __name__ == "__main__":
    unittest.main()
