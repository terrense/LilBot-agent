"""Tests for Dynamic Agent Tool Prompt Parity (SPEC_DYNAMIC_AGENT_TOOL_PROMPT_PARITY.md).

T-4.1: Dynamic description renders correctly
T-4.2: Auto-delegation no longer fires
T-4.3: Gate enforcement unchanged
"""

from __future__ import annotations

import pytest
from pathlib import Path
from unittest.mock import MagicMock

from lilbot.subagents.manager import AgentDefinition, SubAgentManager, SubAgentTask
from lilbot.subagents.render import render_agent_types, render_active_agents
from lilbot.tools.registry import ToolContext, ToolDef, ToolRegistry


# ── Test data ──────────────────────────────────────────────────────────────

def _agent_def(name: str, description: str, writes: bool = False, shell: str = "minimal") -> AgentDefinition:
    return AgentDefinition(
        name=name,
        description=description,
        system_hint="Test agent.",
        writes=writes,
        shell=shell,
    )


def _task(id: str, name: str, agent_type: str, status: str, prompt: str) -> SubAgentTask:
    return SubAgentTask(
        id=id,
        name=name,
        agent_type=agent_type,
        prompt=prompt,
        status=status,
    )


# ── T-4.1: Dynamic description renders correctly ───────────────────────────

class TestRenderAgentTypes:
    def test_renders_all_builtin_types(self):
        definitions = [
            _agent_def("general", "Flexible worker for multi-step tasks.", writes=True, shell="yes"),
            _agent_def("explore", "Read-only explorer.", writes=False, shell="read-only"),
            _agent_def("researcher", "Researcher for public facts.", writes=False, shell="web"),
        ]
        result = render_agent_types(definitions)

        assert "Available agent types" in result
        assert "**general**" in result
        assert "Flexible worker" in result
        assert "**explore**" in result
        assert "Read-only explorer" in result
        assert "**researcher**" in result
        assert "Researcher for public facts" in result
        assert "writes=yes" in result
        assert "writes=no" in result
        assert "shell=read-only" in result
        assert "shell=web" in result

    def test_empty_definitions(self):
        result = render_agent_types([])
        assert "No agent types available" in result

    def test_renders_tool_count(self):
        definitions = [
            AgentDefinition(
                name="worker",
                description="Worker with tools.",
                system_hint="...",
                allowed_tools=["read_file", "grep_files", "write_file"],
                disallowed_tools=["agent_open"],
            ),
        ]
        result = render_agent_types(definitions)
        assert "3 tools" in result
        assert "1 blocked" in result


class TestRenderActiveAgents:
    def test_renders_active_tasks(self):
        tasks = [
            _task("s1", "explore_map", "explore", "running", "map the project structure"),
            _task("s2", "research_facts", "researcher", "completed", "research latest trends in AI"),
        ]
        result = render_active_agents(tasks)

        assert "Active subagents" in result
        assert "explore_map [explore] running" in result
        assert "map the project structure" in result
        # Completed tasks should NOT appear
        assert "research_facts" not in result

    def test_no_active_agents(self):
        result = render_active_agents([])
        assert "No active subagents" in result

    def test_all_terminal_shows_no_active(self):
        tasks = [
            _task("s1", "done_task", "general", "completed", "did something"),
            _task("s2", "failed_task", "general", "failed", "tried something"),
            _task("s3", "cancelled_task", "general", "cancelled", "was going to do something"),
        ]
        result = render_active_agents(tasks)
        assert "No active subagents" in result

    def test_truncates_long_prompts(self):
        long_prompt = "This is a very long prompt that exceeds eighty characters and should be truncated accordingly by the render function."
        tasks = [_task("s1", "agent1", "general", "running", long_prompt)]
        result = render_active_agents(tasks)
        # Should not contain the full prompt
        assert long_prompt not in result


# ── T-4.1 (continued): ToolRegistry integration ────────────────────────────

class TestDynamicSchemas:
    def test_schemas_without_context_returns_static_descriptions(self):
        registry = ToolRegistry()
        registry.register(ToolDef("agent_open", "Static description.", {}, lambda a, c: MagicMock()))

        schemas = registry.schemas()
        agent_open = [s for s in schemas if s["name"] == "agent_open"][0]
        assert agent_open["description"] == "Static description."

    def test_schemas_with_context_expands_agent_open(self):
        registry = ToolRegistry()
        registry.register(ToolDef("agent_open", "Base description.", {}, lambda a, c: MagicMock()))
        registry.register(ToolDef("agent_eval", "Base eval description.", {}, lambda a, c: MagicMock()))

        definitions = [_agent_def("explore", "Read-only explorer.", writes=False, shell="read-only")]
        render_ctx = {
            "agent_types": definitions,
            "active_tasks": [],
            "max_concurrent": 8,
            "running_count": 0,
        }

        schemas = registry.schemas(render_ctx)
        agent_open = [s for s in schemas if s["name"] == "agent_open"][0]

        assert "Base description." in agent_open["description"]
        assert "Available agent types" in agent_open["description"]
        assert "**explore**" in agent_open["description"]

    def test_schemas_with_context_expands_agent_eval(self):
        registry = ToolRegistry()
        registry.register(ToolDef("agent_open", "Base.", {}, lambda a, c: MagicMock()))
        registry.register(ToolDef("agent_eval", "Base eval.", {}, lambda a, c: MagicMock()))

        tasks = [_task("s1", "worker1", "general", "running", "doing work")]
        render_ctx = {
            "agent_types": [],
            "active_tasks": tasks,
            "max_concurrent": 8,
            "running_count": 1,
        }

        schemas = registry.schemas(render_ctx)
        agent_eval = [s for s in schemas if s["name"] == "agent_eval"][0]

        assert "Base eval." in agent_eval["description"]
        assert "Active subagents" in agent_eval["description"]
        assert "worker1" in agent_eval["description"]

    def test_agent_and_task_aliases_also_expanded(self):
        """Agent and Task are compatibility aliases for agent_open."""
        registry = ToolRegistry()
        registry.register(ToolDef("Agent", "Claude-style agent.", {}, lambda a, c: MagicMock()))
        registry.register(ToolDef("Task", "Legacy task alias.", {}, lambda a, c: MagicMock()))

        definitions = [_agent_def("general", "General worker.")]
        render_ctx = {"agent_types": definitions, "active_tasks": [], "max_concurrent": 8, "running_count": 0}

        schemas = registry.schemas(render_ctx)
        for name in ("Agent", "Task"):
            schema = [s for s in schemas if s["name"] == name][0]
            assert "Available agent types" in schema["description"]


# ── T-4.3: Gate enforcement unchanged ─────────────────────────────────────

class TestGateEnforcement:
    def test_custom_agent_without_allowed_tools_fails(self):
        """Gate 1: custom agents require explicit allowed_tools."""
        mgr = SubAgentManager(provider=lambda m, t: MagicMock())
        try:
            mgr.open(agent_type="custom", prompt="do work")
            pytest.fail("Expected SubAgentGateError")
        except Exception as exc:
            error_str = str(exc).lower()
            assert "gate" in error_str or "allowed" in error_str
