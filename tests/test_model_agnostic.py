"""Model-agnostic provider wiring: any OpenAI-compatible model/endpoint plugs in.

LilBot ships no model of its own (hermes-agent style), so switching to an
arbitrary provider/model and picking the right transport must work.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from lilbot.cli import _parse_model_request, switch_runtime_model
from lilbot.config import LilBotConfig
from lilbot.llm.providers import (
    OpenAICompatibleProvider,
    RuleBasedProvider,
    choose_provider,
    is_local_endpoint,
    resolve_endpoint,
)


# ── endpoint resolution + provider selection ─────────────────────────────

def test_resolve_endpoint_known_and_unknown():
    assert resolve_endpoint("openai") == "https://api.openai.com/v1"
    assert resolve_endpoint("moonshot") == "https://api.moonshot.cn/v1"
    assert resolve_endpoint("ollama").startswith("http://localhost")
    assert resolve_endpoint("totally-custom") is None


def test_choose_provider_offline_vs_real():
    # Explicit offline/mock -> rule provider.
    cfg = LilBotConfig(workspace=Path("."), provider="mock", api_key="")
    assert isinstance(choose_provider(cfg), RuleBasedProvider)
    # A cloud provider WITH a key -> real transport, whatever its name.
    for name in ("openai", "moonshot", "groq", "openrouter", "some-new-vendor"):
        cfg = LilBotConfig(workspace=Path("."), provider=name, api_key="sk-x",
                           base_url="https://api.example.com/v1")
        assert isinstance(choose_provider(cfg), OpenAICompatibleProvider), name


def test_local_endpoint_needs_no_key():
    assert is_local_endpoint("http://localhost:11434/v1") is True
    assert is_local_endpoint("https://api.openai.com/v1") is False
    # Local server (Ollama/vLLM) with no key still gets the real transport.
    cfg = LilBotConfig(workspace=Path("."), provider="ollama", api_key="",
                       base_url="http://localhost:11434/v1")
    assert isinstance(choose_provider(cfg), OpenAICompatibleProvider)


# ── /model argument parsing ──────────────────────────────────────────────

def test_parse_model_request_alias():
    provider, model, base = _parse_model_request("flash")
    assert provider == "deepseek" and model == "deepseek-chat"
    assert base == "https://api.deepseek.com"


def test_parse_model_request_provider_qualified():
    provider, model, base = _parse_model_request("moonshot:kimi-k2")
    assert provider == "moonshot" and model == "kimi-k2"
    assert base == "https://api.moonshot.cn/v1"
    # slash separator + a model name that itself contains a slash
    provider, model, base = _parse_model_request("openrouter/anthropic/claude-3.5")
    assert provider == "openrouter" and model == "anthropic/claude-3.5"


def test_parse_model_request_bare_model_keeps_provider():
    # Unknown head -> treat whole thing as a model name, keep current provider.
    provider, model, base = _parse_model_request("gpt-4o")
    assert provider is None and model == "gpt-4o" and base is None


# ── switch at runtime ────────────────────────────────────────────────────

def _agent_ctx(tmp_path):
    cfg = LilBotConfig(workspace=tmp_path, provider="deepseek",
                       model="deepseek-v4-flash", base_url="https://api.deepseek.com",
                       api_key="sk-x")
    agent = SimpleNamespace(config=cfg, provider=None)
    ctx = SimpleNamespace(config=cfg, subagents=SimpleNamespace(provider=None))
    return agent, ctx, cfg


def test_switch_to_arbitrary_model(tmp_path):
    agent, ctx, cfg = _agent_ctx(tmp_path)
    model = switch_runtime_model(agent, ctx, "gpt-4o")
    assert model == "gpt-4o"
    assert cfg.model == "gpt-4o"
    assert cfg.provider == "deepseek"          # bare model keeps provider
    assert callable(ctx.subagents.provider)


def test_switch_to_new_provider_and_model(tmp_path):
    agent, ctx, cfg = _agent_ctx(tmp_path)
    model = switch_runtime_model(agent, ctx, "groq:llama-3.3-70b")
    assert model == "llama-3.3-70b"
    assert cfg.provider == "groq"
    assert cfg.base_url == "https://api.groq.com/openai/v1"


def test_switch_empty_raises(tmp_path):
    agent, ctx, _ = _agent_ctx(tmp_path)
    try:
        switch_runtime_model(agent, ctx, "   ")
        assert False, "expected ValueError"
    except ValueError:
        pass
