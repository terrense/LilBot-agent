"""Tests for the lifecycle hook engine (ported from mewcode)."""
from __future__ import annotations

import json
from pathlib import Path

from lilbot.hooks import HookContext, HookEngine, load_hooks
from lilbot.hooks.models import Hook, HookAction, HookMatch


def test_load_hooks_parses_and_skips_invalid(tmp_path):
    (tmp_path / "hooks.json").write_text(json.dumps({"hooks": [
        {"id": "ok", "event": "pre_tool_use", "action": {"type": "block", "message": "no"}},
        {"id": "bad-event", "event": "nope", "action": {"type": "block"}},
        {"id": "bad-action", "event": "turn_start", "action": {"type": "explode"}},
    ]}), encoding="utf-8")
    hooks = load_hooks(tmp_path)
    assert [h.id for h in hooks] == ["ok"]


def test_match_by_tool_and_path():
    m = HookMatch(tool="write_file", path_regex=r"\.env$")
    assert m.matches(HookContext(event="pre_tool_use", tool_name="write_file", file_path="a/.env"))
    assert not m.matches(HookContext(event="pre_tool_use", tool_name="write_file", file_path="a/main.py"))
    assert not m.matches(HookContext(event="pre_tool_use", tool_name="read_file", file_path="a/.env"))


def test_pre_tool_block_returns_reason():
    hook = Hook(
        id="guard", event="pre_tool_use",
        action=HookAction(type="block", message="Refusing to touch .env"),
        match=HookMatch(tool="write_file", path_regex=r"\.env$"),
    )
    engine = HookEngine([hook])
    reason = engine.run_pre_tool(HookContext(
        event="pre_tool_use", tool_name="write_file", file_path=".env"))
    assert reason == "Refusing to touch .env"
    # Non-matching tool is not blocked.
    assert engine.run_pre_tool(HookContext(
        event="pre_tool_use", tool_name="read_file", file_path=".env")) is None


def test_prompt_action_collected_as_message():
    hook = Hook(id="remind", event="turn_start",
                action=HookAction(type="prompt", message="Run the tests."))
    engine = HookEngine([hook])
    engine.run("turn_start", HookContext(event="turn_start"))
    assert engine.drain_prompt_messages() == ["Run the tests."]
    # Draining clears.
    assert engine.drain_prompt_messages() == []


def test_command_hook_reports_output():
    hook = Hook(id="echo", event="post_tool_use",
                action=HookAction(type="command", command="echo hello-hook"))
    engine = HookEngine([hook])
    engine.run("post_tool_use", HookContext(event="post_tool_use", tool_name="edit_file"))
    notes = engine.drain_notifications()
    assert len(notes) == 1
    assert "hello-hook" in notes[0].output
    assert notes[0].success


def test_run_once_hook_fires_only_once():
    hook = Hook(id="one", event="turn_start", run_once=True,
                action=HookAction(type="prompt", message="hi"))
    engine = HookEngine([hook])
    engine.run("turn_start", HookContext(event="turn_start"))
    engine.run("turn_start", HookContext(event="turn_start"))
    assert engine.drain_prompt_messages() == ["hi"]


def test_agent_blocks_tool_via_hook(tmp_path):
    import sys
    from types import SimpleNamespace
    from lilbot.config import LilBotConfig
    from lilbot.core.agent import Agent
    from lilbot.core.events import ProviderTurn, ToolCall
    from lilbot.tools import ToolContext, ToolDef, ToolRegistry, ToolResult

    state = tmp_path / ".lilbot"
    state.mkdir()
    (state / "hooks.json").write_text(json.dumps({"hooks": [
        {"id": "guard", "event": "pre_tool_use",
         "match": {"tool": "write_file", "path_regex": r"\.env$"},
         "action": {"type": "block", "message": "Refusing to write .env"}},
    ]}), encoding="utf-8")

    executed = {"called": False}

    def handler(args, ctx):
        executed["called"] = True
        return ToolResult(True, "wrote")

    registry = ToolRegistry()
    registry.register(ToolDef("write_file", "w", {"type": "object", "properties": {}}, handler))

    calls = [iter([ProviderTurn(tool_calls=[ToolCall("write_file", {"path": ".env"})]),
                   ProviderTurn(content="done")])]

    class P:
        def complete(self, messages, tools):
            return next(calls[0])

    cfg = LilBotConfig(workspace=tmp_path)

    class Mem:
        def context(self, *a, **k):
            return "(none)"

    class Sk:
        def list(self, *a, **k):
            return []

    ctx = ToolContext(None, None, Mem(), Sk(), None, None, cfg)
    agent = Agent(cfg, P(), registry, ctx)
    events = list(agent.run_turn("write the env file"))

    assert executed["called"] is False  # tool never ran
    blocked = [m for m in agent.messages if m.get("role") == "tool" and "Blocked by hook" in str(m.get("content"))]
    assert blocked and "Refusing to write .env" in blocked[0]["content"]


def test_hooks_hot_reload_picks_up_new_file(tmp_path):
    from lilbot.config import LilBotConfig
    from lilbot.core.agent import Agent
    from lilbot.core.events import ProviderTurn, ToolCall
    from lilbot.tools import ToolContext, ToolDef, ToolRegistry, ToolResult

    (tmp_path / ".lilbot").mkdir()
    ran = {"n": 0}

    def w(args, ctx):
        ran["n"] += 1
        return ToolResult(True, "wrote")

    registry = ToolRegistry()
    registry.register(ToolDef("write_file", "w", {"type": "object", "properties": {}}, w))

    turns = iter([
        ProviderTurn(tool_calls=[ToolCall("write_file", {"path": ".env"})]),
        ProviderTurn(content="done"),
        ProviderTurn(tool_calls=[ToolCall("write_file", {"path": ".env"})]),
        ProviderTurn(content="done"),
    ])

    class P:
        def complete(self, m, t):
            return next(turns)

    class Mem:
        def context(self, *a, **k):
            return "(none)"

    class Sk:
        def list(self, *a, **k):
            return []

    cfg = LilBotConfig(workspace=tmp_path)
    agent = Agent(cfg, P(), registry, ToolContext(None, None, Mem(), Sk(), None, None, cfg))

    # Session started with no hooks.json -> first write goes through.
    list(agent.run_turn("write env"))
    assert ran["n"] == 1
    assert len(agent.hooks.hooks) == 0

    # Create the hooks file mid-session (no restart).
    (tmp_path / ".lilbot" / "hooks.json").write_text(json.dumps({"hooks": [
        {"id": "g", "event": "pre_tool_use",
         "match": {"tool": "write_file", "path_regex": r"\.env$"},
         "action": {"type": "block", "message": "no env"}},
    ]}), encoding="utf-8")

    # Next turn hot-reloads and blocks the write.
    list(agent.run_turn("write env again"))
    assert len(agent.hooks.hooks) == 1
    assert ran["n"] == 1  # still 1 — second write was blocked
