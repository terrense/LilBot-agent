"""Inline and forked skills must enforce their declared allowed_tools."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from lilbot.config import LilBotConfig
from lilbot.core.agent import Agent
from lilbot.core.events import ProviderTurn
from lilbot.skills.registry import Skill
from lilbot.tools.builtin import _skill_run
from lilbot.tools.registry import ToolContext, ToolDef, ToolRegistry, ToolResult


class _Skills:
    def __init__(self, skill: Skill):
        self.skill = skill

    def get(self, name: str):
        return self.skill if name == self.skill.name else None

    def list(self, include_hidden: bool = False):
        return [self.skill]


class _Mem:
    def context(self) -> str:
        return "(none)"


class _Subs:
    def __init__(self):
        self.kwargs = None

    def open(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(id="a1", status="running", transcript_handle="t1")

    def projection(self, task):
        return {"id": task.id, "status": task.status}


def _skill(mode: str = "inline", allowed: list[str] | None = None) -> Skill:
    return Skill(
        name="demo",
        description="demo skill",
        body="Do the demo.",
        source=Path("demo/SKILL.md"),
        mode=mode,
        allowed_tools=allowed or [],
    )


def _registry(executed: list[str]) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(ToolDef(
        "read_file", "read", {},
        lambda args, ctx: (executed.append("read"), ToolResult(True, "read ok"))[1],
    ))
    registry.register(ToolDef(
        "write_file", "write", {},
        lambda args, ctx: (executed.append("write"), ToolResult(True, "write ok"))[1],
    ))
    return registry


def test_inline_skill_sets_scope_and_blocks_other_tools():
    executed: list[str] = []
    registry = _registry(executed)
    ctx = SimpleNamespace(skills=_Skills(_skill(allowed=["read_file"])), subagents=None, active_skill=None)

    result = _skill_run({"skill": "demo"}, ctx)
    assert result.ok is True
    assert ctx.active_skill["allowed_tools"] == frozenset({"read_file"})

    denied, _ = registry.execute("write_file", {}, ctx)
    assert denied.ok is False
    assert denied.metadata["gate"] == "skill_scope"

    allowed, _ = registry.execute("read_file", {}, ctx)
    assert allowed.ok is True
    assert executed == ["read"]


def test_inline_scope_filters_advertised_schemas():
    registry = _registry([])
    ctx = SimpleNamespace(skills=_Skills(_skill(allowed=["read_file"])), subagents=None, active_skill=None)
    _skill_run({"skill": "demo"}, ctx)
    names = [schema["name"] for schema in registry.schemas(allowed_names=ctx.active_skill["allowed_tools"])]
    assert "read_file" in names
    assert "write_file" not in names


def test_inline_scope_never_broadens():
    ctx = SimpleNamespace(skills=_Skills(_skill(allowed=["read_file"])), subagents=None, active_skill=None)
    _skill_run({"skill": "demo"}, ctx)
    # A second, wider skill must intersect with the existing scope.
    ctx.skills = _Skills(_skill(allowed=["read_file", "write_file"]))
    _skill_run({"skill": "demo"}, ctx)
    assert ctx.active_skill["allowed_tools"] == frozenset({"read_file"})


def test_forked_skill_passes_allowed_tools_to_subagent():
    subs = _Subs()
    ctx = SimpleNamespace(skills=_Skills(_skill(mode="fork", allowed=["read_file"])), subagents=subs)
    result = _skill_run({"skill": "demo"}, ctx)
    assert result.ok is True
    assert subs.kwargs["allowed_tools"] == ["read_file"]
    assert subs.kwargs["fork_context"] is True


def test_agent_turn_resets_inline_skill_scope(tmp_path):
    class _Provider:
        def complete(self, messages, tools):
            return ProviderTurn(content="ok")

    ctx = ToolContext(
        sandbox=None, permissions=None, memory=_Mem(), skills=_Skills(_skill()),
        subagents=None, mcp=None, config=None,
    )
    ctx.active_skill = {"name": "demo", "allowed_tools": frozenset({"read_file"})}
    agent = Agent(
        LilBotConfig(workspace=tmp_path, context_window=40_000),
        _Provider(), ToolRegistry(), ctx,
    )
    list(agent.run_turn("hi"))
    assert ctx.active_skill is None
