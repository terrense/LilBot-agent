"""USD cost accounting and the session budget guard."""

from __future__ import annotations

from lilbot.config import LilBotConfig
from lilbot.core.agent import Agent
from lilbot.core.events import ProviderTurn
from lilbot.core.model_cost import ModelPrice, estimate_cost, format_usd, price_for
from lilbot.tools import ToolContext, ToolRegistry


class _Mem:
    def context(self, *a, **k):
        return "(none)"


class _Sk:
    def list(self, *a, **k):
        return []


class _UsageProvider:
    def __init__(self, prompt=1_000_000, completion=100_000, cached=0):
        self.prompt = prompt
        self.completion = completion
        self.cached = cached
        self.calls = 0

    def complete(self, messages, tools):
        self.calls += 1
        return ProviderTurn(
            content="ok",
            usage={
                "prompt_tokens": self.prompt,
                "completion_tokens": self.completion,
                "cache_read_tokens": self.cached,
            },
        )

    def complete_stream(self, messages, tools):
        from lilbot.core.events import StreamEvent
        yield StreamEvent(final=self.complete(messages, tools))


def _agent(tmp_path, provider, **cfg) -> Agent:
    ctx = ToolContext(
        sandbox=None, permissions=None, memory=_Mem(), skills=_Sk(),
        subagents=None, mcp=None, config=None,
    )
    config = LilBotConfig(
        workspace=tmp_path,
        model="deepseek-chat",
        base_url="https://api.deepseek.com",
        **cfg,
    )
    return Agent(config, provider, ToolRegistry(), ctx)


def test_price_table_exact_family_and_unknown():
    exact = price_for("deepseek-chat", "https://api.deepseek.com")
    assert exact is not None and exact.input_per_million == 0.27
    family = price_for("deepseek-v9-preview", "https://api.deepseek.com")
    assert family is not None and family.input_per_million == 0.27
    assert price_for("mystery-model", "https://unknown.example/v1") is None
    local = price_for("qwen2.5:3b", "http://127.0.0.1:11434/v1")
    assert local is not None
    assert local.input_per_million == 0.0
    offline = price_for("mock", "")
    assert offline is not None and offline.source == "offline"


def test_estimate_cost_splits_uncached_cache_and_output():
    price = ModelPrice(0.27, 1.10, 0.07)
    estimate = estimate_cost(
        {"prompt_tokens": 1_000_000, "cache_read_tokens": 400_000, "completion_tokens": 100_000},
        price,
    )
    assert estimate.input_tokens == 600_000
    assert estimate.cached_tokens == 400_000
    assert abs(estimate.input_cost_usd - 0.162) < 1e-9
    assert abs(estimate.cache_cost_usd - 0.028) < 1e-9
    assert abs(estimate.output_cost_usd - 0.11) < 1e-9
    assert abs(estimate.total_cost_usd - 0.30) < 1e-9


def test_cache_count_is_clamped_to_prompt():
    price = ModelPrice(1.0, 1.0, 0.5)
    estimate = estimate_cost(
        {"prompt_tokens": 100, "cache_read_tokens": 500, "completion_tokens": 0},
        price,
    )
    assert estimate.cached_tokens == 100
    assert estimate.input_tokens == 0


def test_agent_accumulates_cost(tmp_path):
    provider = _UsageProvider()
    agent = _agent(tmp_path, provider)
    agent._maybe_extract = lambda: None
    agent._maybe_update_session_memory = lambda: None
    list(agent.run_turn("hi"))
    info = agent.cost_info()
    assert abs(float(info["total_usd"]) - 0.38) < 1e-6
    assert abs(float(info["last_turn_usd"]) - 0.38) < 1e-6
    assert abs(float(agent.usage["cost_usd"]) - 0.38) < 1e-6


def test_budget_warning_at_eighty_percent(tmp_path):
    provider = _UsageProvider()
    agent = _agent(tmp_path, provider, budget_usd=0.4)
    # Disable side queries so the fake provider only counts main-loop calls.
    agent._maybe_extract = lambda: None
    agent._maybe_update_session_memory = lambda: None
    list(agent.run_turn("one"))
    assert agent._budget_warned is False  # 0.38 is 95% — checked on the next call
    list(agent.run_turn("two"))
    assert agent._budget_warned is True
    assert "budget_warning" in agent._recovery_transitions


def test_budget_stops_before_the_next_request(tmp_path):
    provider = _UsageProvider()
    agent = _agent(tmp_path, provider, budget_usd=0.1)
    agent._maybe_extract = lambda: None
    agent._maybe_update_session_memory = lambda: None
    list(agent.run_turn("one"))
    assert provider.calls == 1
    list(agent.run_turn("two"))
    assert provider.calls == 1  # the second request was never sent
    assert any("USD budget reached" in str(m.get("content", "")) for m in agent.messages)


def test_reset_conversation_clears_cost(tmp_path):
    provider = _UsageProvider()
    agent = _agent(tmp_path, provider)
    agent._maybe_extract = lambda: None
    agent._maybe_update_session_memory = lambda: None
    list(agent.run_turn("one"))
    assert agent._cost_total > 0
    agent.reset_conversation()
    assert agent._cost_total == 0
    assert agent._cost_last_turn == 0
    assert agent.usage.get("cost_usd", 0) in (0, None)


def test_format_usd_keeps_sub_cent_precision():
    assert format_usd(0) == "$0.0000"
    assert format_usd(0.001234) == "$0.001234"
    assert format_usd(1.23456) == "$1.2346"
