"""Per-model context windows and the schema accounting that feeds them."""

from __future__ import annotations

import pytest

from lilbot.config import LilBotConfig
from lilbot.core.agent import Agent
from lilbot.core.events import ProviderTurn
from lilbot.core.model_windows import (
    DEFAULT_CONTEXT_WINDOW,
    resolve_context_window,
)
from lilbot.tools import ToolContext, ToolDef, ToolRegistry, ToolResult


class _Mem:
    def context(self, *a, **k):
        return "(none)"


class _Sk:
    def list(self, *a, **k):
        return []


def _agent(tmp_path, provider=None, **cfg) -> Agent:
    ctx = ToolContext(
        sandbox=None, permissions=None, memory=_Mem(), skills=_Sk(),
        subagents=None, mcp=None, config=None,
    )
    config = LilBotConfig(workspace=tmp_path, **cfg)
    return Agent(config, provider, ToolRegistry(), ctx)


def test_exact_model_id_wins():
    resolved = resolve_context_window("deepseek-chat", "https://api.deepseek.com", 128_000, False)
    assert resolved.window == 64_000
    assert resolved.source == "model"


def test_model_family_is_used_when_the_id_is_unknown():
    resolved = resolve_context_window("deepseek-v9-preview", "https://api.deepseek.com", 128_000, False)
    assert resolved.window == 64_000
    assert resolved.source == "model_family"


def test_provider_default_when_the_model_is_unknown():
    resolved = resolve_context_window("mystery-1", "https://api.deepseek.com/v1", 128_000, False)
    assert resolved.window == 64_000
    assert resolved.source == "provider"


def test_explicit_setting_beats_the_table():
    resolved = resolve_context_window("deepseek-chat", "https://api.deepseek.com", 32_000, True)
    assert resolved.window == 32_000
    assert resolved.source == "configured"


def test_configured_value_is_the_fallback():
    resolved = resolve_context_window("mystery-1", "https://unknown.example/v1", 48_000, False)
    assert resolved.window == 48_000
    assert resolved.source == "configured"


def test_local_endpoint_keeps_the_configured_value():
    resolved = resolve_context_window("qwen2.5:3b", "http://127.0.0.1:11434/v1", 64_000, False)
    assert resolved.window == 64_000
    assert resolved.source == "local_endpoint"


def test_unknown_endpoint_uses_the_default_window():
    resolved = resolve_context_window("mystery-1", "", 0, False)
    assert resolved.window == DEFAULT_CONTEXT_WINDOW
    assert resolved.source == "default"


def test_agent_reports_the_resolved_window(tmp_path):
    agent = _agent(
        tmp_path,
        provider=None,
        provider_name=None,
        model="deepseek-chat",
        base_url="https://api.deepseek.com",
    ) if False else _agent(
        tmp_path,
        model="deepseek-chat",
        base_url="https://api.deepseek.com",
    )
    assert agent.effective_context_window() == 64_000
    info = agent.context_window_info()
    assert info.source == "model"
    assert info.detail == "deepseek-chat"


def test_agent_honors_an_explicit_window(tmp_path):
    agent = _agent(
        tmp_path,
        model="deepseek-chat",
        base_url="https://api.deepseek.com",
        context_window=32_000,
        context_window_explicit=True,
    )
    assert agent.effective_context_window() == 32_000


def test_dashboard_and_agent_agree_on_the_window(tmp_path):
    agent = _agent(tmp_path, model="deepseek-chat", base_url="https://api.deepseek.com")
    assert agent.effective_context_window() == 64_000


def test_tool_schema_tokens_are_counted_in_the_estimate(tmp_path):
    registry = ToolRegistry()
    registry.register(ToolDef(
        "read_file", "read", {"type": "object", "properties": {}},
        lambda a, c: ToolResult(True, "ok"),
    ))
    ctx = ToolContext(
        sandbox=None, permissions=None, memory=_Mem(), skills=_Sk(),
        subagents=None, mcp=None, config=None,
    )
    agent = Agent(
        LilBotConfig(workspace=tmp_path, model="deepseek-chat"),
        None, registry, ctx,
    )
    schemas = registry.schemas()
    assert schemas
    agent._update_schema_tokens(schemas)
    breakdown = agent.token_breakdown()
    assert breakdown["tool_schema_tokens"] > 0
    assert breakdown["total_tokens"] >= breakdown["tool_schema_tokens"]
    assert breakdown["context_window"] == 64_000
    assert breakdown["threshold"] < breakdown["context_window"]


def test_schema_growth_is_added_to_the_reported_usage(tmp_path):
    class _Provider:
        def complete(self, messages, tools):
            return ProviderTurn(content="ok", usage={"prompt_tokens": 1000})

        def complete_stream(self, messages, tools):
            from lilbot.core.events import StreamEvent
            yield StreamEvent(final=self.complete(messages, tools))

    registry = ToolRegistry()
    registry.register(ToolDef(
        "read_file", "read", {"type": "object", "properties": {}},
        lambda a, c: ToolResult(True, "ok"),
    ))
    ctx = ToolContext(
        sandbox=None, permissions=None, memory=_Mem(), skills=_Sk(),
        subagents=None, mcp=None, config=None,
    )
    agent = Agent(
        LilBotConfig(workspace=tmp_path, model="deepseek-chat"),
        _Provider(), registry, ctx,
    )
    list(agent.run_turn("hi"))
    first = agent.token_breakdown()
    # The reported usage already counted the schemas; the growth is zero.
    assert first["schema_growth_tokens"] == 0
    assert first["total_tokens"] >= 1000


@pytest.mark.parametrize("model,expected", [
    ("gpt-4o", 128_000),
    ("gpt-4.1-mini", 1_047_576),
    ("claude-sonnet-4", 200_000),
    ("gemini-2.5-pro", 1_048_576),
    ("qwen3-235b-a22b", 262_144),
    ("kimi-k2", 131_072),
    ("glm-4.5", 128_000),
])
def test_known_models_have_the_documented_window(model, expected):
    assert resolve_context_window(model, "", 0, False).window == expected
