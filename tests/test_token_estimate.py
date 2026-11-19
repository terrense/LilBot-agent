"""CJK-aware token estimate calibrated against real
usage, side-query usage kept out of the compaction trigger, and an auxiliary
model for side queries."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from lilbot.cli import _calibration_text, _token_rows
from lilbot.config import LilBotConfig, apply_env_overrides
from lilbot.core import agent as agent_module
from lilbot.core.agent import Agent
from lilbot.core.compaction import (
    RECOVERY_TOTAL_TOKENS,
    RecoveryState,
    estimate_tokens,
    estimate_tokens_text,
)
from lilbot.core.events import ProviderTurn, StreamEvent
from lilbot.tools import ToolContext, ToolRegistry
from lilbot.tui.dashboard import DashboardUI

ZH = "这是一个用于测试中文分词的句子，包含常见的汉字和标点符号。" * 50  # 1,450 chars
EN = "This is a sentence used to test English tokenization with common words. " * 50


# ── estimator ────────────────────────────────────────────────────────────

def test_chinese_is_no_longer_undercounted():
    assert len(ZH) == 1450
    assert 1000 <= estimate_tokens_text(ZH) <= 1600  # was 362 with len/4


def test_ascii_estimate_is_unchanged():
    for text in (EN, "def f(x):\n    return x * 2\n", '{"a": [1, 2, 3]}'):
        assert estimate_tokens_text(text) == int(len(text) / 4)


def test_mixed_and_other_scripts():
    assert estimate_tokens_text("") == 0
    mixed = "修复 bug：retry 3 次"  # 6 CJK chars + punctuation, ASCII rest
    assert estimate_tokens_text(mixed) > len(mixed) / 4
    assert estimate_tokens_text("привет мир") == int(1 / 4 + 9 / 2)  # one space is ASCII


def test_recovery_attachment_budget_holds_for_chinese():
    state = RecoveryState()
    for i in range(5):
        state.record_file_read(f"doc{i}.md", "中文内容" * 5000)
    for i in range(8):
        state.record_note(f"笔记{i}", "很长的中文笔记" * 3000)
    attachment = state.build_attachment(["read_file"])
    assert estimate_tokens_text(attachment) <= RECOVERY_TOTAL_TOKENS + 50


# ── agent calibration ────────────────────────────────────────────────────

class _Mem:
    def context(self): return "(none)"
    def list(self): return []


class _Sk:
    def list(self): return []


class _Usage:
    """Answers with usage = ``ratio`` x the request's raw estimate (or a fixed count)."""

    def __init__(self, ratio: float = 2.0, fixed: int | None = None):
        self.ratio = ratio
        self.fixed = fixed
        self.calls = 0

    def complete(self, messages, tools):
        self.calls += 1
        prompt = self.fixed if self.fixed is not None else int(estimate_tokens(messages) * self.ratio)
        return ProviderTurn(content="side answer", usage={"prompt_tokens": prompt})

    def complete_stream(self, messages, tools):
        yield StreamEvent(final=self.complete(messages, tools))


def _agent(tmp_path: Path, provider, **cfg) -> Agent:
    ctx = ToolContext(sandbox=None, permissions=None, memory=_Mem(), skills=_Sk(),
                      subagents=None, mcp=None, config=None)
    config = LilBotConfig(workspace=tmp_path, provider="openai", model="main-model", api_key="k",
                          base_url="https://main.example/v1", **cfg)
    return Agent(config, provider, ToolRegistry(), ctx)


def test_ratio_learns_from_real_usage(tmp_path):
    agent = _agent(tmp_path, _Usage(ratio=2.0))
    assert agent.context_tokens() == 0  # no signal yet -> caller uses raw estimate
    list(agent.run_turn("你好，帮我看看这个问题"))
    info = agent.token_calibration()
    assert info["samples"] == 1
    assert abs(info["ratio"] - 2.0) < 0.05
    assert info["last_prompt_tokens"] > 0


def test_ratio_is_an_ema(tmp_path):
    provider = _Usage(ratio=2.0)
    agent = _agent(tmp_path, provider)
    list(agent.run_turn("one"))
    provider.ratio = 1.0
    list(agent.run_turn("two"))
    assert abs(agent.token_calibration()["ratio"] - (0.7 * 2.0 + 0.3 * 1.0)) < 0.05


def test_absurd_samples_are_ignored(tmp_path):
    agent = _agent(tmp_path, _Usage(fixed=1))  # a mock/proxy reporting 1 token
    list(agent.run_turn("hello there"))
    assert agent.token_calibration()["samples"] == 0
    assert agent.token_calibration()["ratio"] == 1.0


def test_context_tokens_is_last_real_plus_calibrated_tail(tmp_path):
    agent = _agent(tmp_path, _Usage(ratio=2.0))
    list(agent.run_turn("hi"))
    last = agent.token_calibration()["last_prompt_tokens"]
    ratio = agent.token_calibration()["ratio"]
    before = agent.context_tokens()
    # A big Chinese tool result lands after the last request.
    agent.messages.append({"role": "tool", "tool_call_id": "t", "name": "read_file", "content": ZH})
    grown = agent.context_tokens()
    expected_tail = estimate_tokens(agent.messages[agent._last_request_len:]) * ratio
    assert grown > before
    assert abs(grown - (last + expected_tail)) <= 2


def test_side_queries_do_not_overwrite_the_trigger(tmp_path):
    provider = _Usage(ratio=2.0)
    agent = _agent(tmp_path, provider)
    list(agent.run_turn("hi"))
    last = agent._last_input_tokens
    provider.fixed = 7  # a tiny recall/extraction prompt
    agent._meta_query("pick memories", "query")
    assert agent._last_input_tokens == last
    assert agent.usage["prompt_tokens"] >= last + 7  # still counted for billing


def test_compaction_trigger_uses_context_tokens(tmp_path, monkeypatch):
    agent = _agent(tmp_path, _Usage(ratio=2.0))
    list(agent.run_turn("hi"))
    seen = {}

    def fake_auto_compact(messages, *args, **kwargs):
        seen["actual"] = kwargs.get("actual_tokens")
        return None

    monkeypatch.setattr(agent_module, "auto_compact", fake_auto_compact)
    agent.messages.append({"role": "tool", "tool_call_id": "t", "name": "x", "content": ZH})
    agent.compact(manual=True)
    assert seen["actual"] == agent.context_tokens()
    assert seen["actual"] > agent._last_input_tokens


# ── auxiliary model ──────────────────────────────────────────────────────

def test_side_queries_go_to_aux_model_main_loop_stays(tmp_path):
    main, aux = _Usage(), _Usage()
    agent = _agent(tmp_path, main, aux_model="cheap-model", aux_base_url="https://aux.example/v1/")
    built = []
    agent._build_aux_provider = lambda cfg: built.append(cfg) or aux
    assert agent._meta_query("sys", "user") == "side answer"
    agent._meta_query("sys", "again")
    assert aux.calls == 2 and main.calls == 0
    assert len(built) == 1  # cached
    assert built[0].model == "cheap-model" and built[0].base_url == "https://aux.example/v1"
    list(agent.run_turn("hi"))
    assert main.calls == 1 and aux.calls == 2
    assert agent.config.model == "main-model"


def test_no_aux_model_means_main_provider(tmp_path):
    main = _Usage()
    agent = _agent(tmp_path, main)
    agent._meta_query("sys", "user")
    assert main.calls == 1
    agent.config.aux_model = "main-model"  # same as main -> no second client
    agent._build_aux_provider = lambda cfg: (_ for _ in ()).throw(AssertionError("should not build"))
    agent._meta_query("sys", "user")
    assert main.calls == 2


def test_aux_env_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("LILBOT_AUX_MODEL", "MiniMax-M2")
    monkeypatch.setenv("LILBOT_AUX_BASE_URL", "https://api.minimaxi.com/v1/")
    cfg = apply_env_overrides(LilBotConfig(workspace=tmp_path))
    assert cfg.aux_model == "MiniMax-M2"
    assert cfg.aux_base_url == "https://api.minimaxi.com/v1"


# ── what the user sees ───────────────────────────────────────────────────

def test_tokens_command_shows_calibration(tmp_path):
    agent = _agent(tmp_path, _Usage(ratio=2.0))
    ctx = SimpleNamespace(config=agent.config)
    assert "not calibrated" in _calibration_text(agent)
    list(agent.run_turn("hi"))
    rows = dict(_token_rows(agent, ctx))
    assert "real/estimate x2" in rows["estimate_calibration"]
    assert rows["approx_context_tokens"] == str(agent.context_tokens())


def test_dashboard_context_percent_ignores_cumulative_usage(tmp_path):
    agent = _agent(tmp_path, _Usage(ratio=1.0), context_window=100_000)
    for _ in range(5):
        list(agent.run_turn("hi"))
    agent.usage["prompt_tokens"] = 10_000_000  # lifetime total, not context size
    view = SimpleNamespace(agent=agent, ctx=SimpleNamespace(config=agent.config))
    view._model_context_limit = lambda: DashboardUI._model_context_limit(view)
    assert DashboardUI._model_context_limit(view) == 100_000  # unknown model -> config window
    assert DashboardUI._context_percent(view) < 5
