"""Day 2 (Hermes gap P2): classify provider failures, back off and retry
transient ones, fall back to a second model, and persist a failed turn."""
from __future__ import annotations

import time
from email.utils import formatdate
from pathlib import Path

import httpx
import pytest

from lilbot.config import LilBotConfig, apply_env_overrides
from lilbot.core.agent import Agent
from lilbot.core.events import ProviderRetry, ProviderTurn, StreamEvent, TurnFinished
from lilbot.llm.errors import FailoverReason, classify
from lilbot.llm.providers import OpenAICompatibleProvider, ProviderError
from lilbot.llm.retry import jittered_backoff, parse_retry_after, retry_delay
from lilbot.tools import ToolContext, ToolRegistry


# ── classify ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "status, text, reason, retryable, fallback",
    [
        (429, "Too Many Requests", FailoverReason.rate_limit, True, True),
        (429, "insufficient_quota: you exceeded your plan", FailoverReason.billing, False, True),
        (402, "Payment Required", FailoverReason.billing, False, True),
        (401, "invalid api key", FailoverReason.auth, False, False),
        (403, "forbidden", FailoverReason.auth, False, False),
        (503, "Service Unavailable", FailoverReason.overloaded, True, True),
        (529, "overloaded", FailoverReason.overloaded, True, True),
        (500, "Internal Server Error", FailoverReason.server_error, True, True),
        (502, "Bad Gateway", FailoverReason.server_error, True, True),
        (504, "Gateway Timeout", FailoverReason.timeout, True, True),
        (404, "model_not_found", FailoverReason.model_not_found, False, True),
        (400, "unknown model: foo", FailoverReason.model_not_found, False, True),
        (400, "messages: field required", FailoverReason.format_error, False, False),
    ],
)
def test_classify_by_status(status, text, reason, retryable, fallback):
    info = classify(ProviderError(text, status_code=status))
    assert info.reason is reason
    assert info.retryable is retryable
    assert info.should_fallback is fallback
    assert info.should_compress is False


def test_classify_overflow_compresses_instead_of_retrying():
    info = classify(ProviderError("context_length_exceeded", status_code=400, is_overflow=True))
    assert info.reason is FailoverReason.context_overflow
    assert info.should_compress and not info.retryable and not info.should_fallback


def test_classify_transport_errors():
    assert classify(ProviderError("x", reason="timeout")).reason is FailoverReason.timeout
    assert classify(ProviderError("x", reason="network")).retryable
    assert classify(httpx.ReadTimeout("slow")).reason is FailoverReason.timeout
    assert classify(httpx.ConnectError("refused")).reason is FailoverReason.network
    assert classify(ConnectionResetError()).reason is FailoverReason.network


def test_classify_programming_errors_are_not_retried():
    info = classify(KeyError("choices"))
    assert info.reason is FailoverReason.unknown
    assert not info.retryable and not info.should_fallback


def test_retry_after_is_carried_through():
    info = classify(ProviderError("slow down", status_code=429, retry_after=7.0))
    assert info.retry_after == 7.0


# ── backoff timing ───────────────────────────────────────────────────────

def test_jittered_backoff_grows_and_caps():
    for _ in range(50):
        assert 2.0 <= jittered_backoff(1) <= 3.0
        assert 8.0 <= jittered_backoff(3) <= 12.0
        assert 60.0 <= jittered_backoff(20) <= 90.0


def test_jitter_actually_varies():
    assert len({round(jittered_backoff(2), 6) for _ in range(20)}) > 1


def test_parse_retry_after_forms():
    assert parse_retry_after("5") == 5.0
    assert parse_retry_after("0.5") == 0.5
    assert parse_retry_after(None) is None
    assert parse_retry_after("soon") is None
    future = parse_retry_after(formatdate(time.time() + 30, usegmt=True))
    assert future is not None and 25 <= future <= 31


def test_retry_delay_prefers_server_hint_but_caps_it():
    assert retry_delay(1, 7.0) == 7.0
    assert retry_delay(1, 3600.0) == 60.0
    assert 2.0 <= retry_delay(1, None) <= 3.0


# ── provider wraps failures with structure ───────────────────────────────

def _provider_with(handler) -> OpenAICompatibleProvider:
    cfg = LilBotConfig(workspace=Path("."), provider="openai", model="m", api_key="k",
                       base_url="https://api.example/v1")
    provider = OpenAICompatibleProvider(cfg)
    provider._client = httpx.Client(transport=httpx.MockTransport(handler))
    provider._client_key = cfg.base_url
    return provider


_MSGS = [{"role": "user", "content": "hi"}]


@pytest.mark.parametrize("streaming", [False, True])
def test_provider_parses_retry_after_header(streaming):
    provider = _provider_with(lambda req: httpx.Response(429, headers={"Retry-After": "7"}, json={"error": "slow"}))
    with pytest.raises(ProviderError) as caught:
        if streaming:
            list(provider.complete_stream(_MSGS, []))
        else:
            provider.complete(_MSGS, [])
    assert caught.value.status_code == 429
    assert caught.value.retry_after == 7.0


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("exc, reason", [
    (httpx.ConnectError("refused"), "network"),
    (httpx.ReadTimeout("slow"), "timeout"),
])
def test_provider_wraps_transport_errors(streaming, exc, reason):
    def handler(request):
        raise exc

    provider = _provider_with(handler)
    with pytest.raises(ProviderError) as caught:
        if streaming:
            list(provider.complete_stream(_MSGS, []))
        else:
            provider.complete(_MSGS, [])
    assert caught.value.reason == reason
    assert classify(caught.value).retryable


def test_missing_key_is_auth():
    cfg = LilBotConfig(workspace=Path("."), provider="openai", model="m", api_key="")
    with pytest.raises(ProviderError) as caught:
        OpenAICompatibleProvider(cfg).complete(_MSGS, [])
    assert classify(caught.value).reason is FailoverReason.auth


# ── agent recovery loop ──────────────────────────────────────────────────

class _Mem:
    def context(self): return "(none)"
    def list(self): return []


class _Sk:
    def list(self): return []


class _Scripted:
    """Raises the scripted errors in order, then answers ``text``."""

    def __init__(self, *errors: Exception, text: str = "ok", partial: str = ""):
        self.errors = list(errors)
        self.text = text
        self.partial = partial
        self.calls = 0

    def complete(self, messages, tools):
        raise AssertionError("streaming path expected")

    def complete_stream(self, messages, tools):
        self.calls += 1
        if self.partial:
            yield StreamEvent(text=self.partial)
        if self.errors:
            raise self.errors.pop(0)
        yield StreamEvent(final=ProviderTurn(content=self.text, usage={"prompt_tokens": 5}))


def _agent(tmp_path: Path, provider, **cfg_kwargs) -> tuple[Agent, list[float]]:
    ctx = ToolContext(sandbox=None, permissions=None, memory=_Mem(), skills=_Sk(),
                      subagents=None, mcp=None, config=None)
    cfg = LilBotConfig(workspace=tmp_path, provider="openai", model="main-model", api_key="k",
                       base_url="https://main.example/v1", **cfg_kwargs)
    agent = Agent(cfg, provider, ToolRegistry(), ctx)
    sleeps: list[float] = []
    agent._sleep = sleeps.append
    return agent, sleeps


def _err(status: int, text: str = "boom", **kw) -> ProviderError:
    return ProviderError(text, status_code=status, **kw)


def test_429_then_503_then_success(tmp_path):
    provider = _Scripted(_err(429), _err(503))
    agent, sleeps = _agent(tmp_path, provider)
    events = list(agent.run_turn("hello"))
    retries = [e for e in events if isinstance(e, ProviderRetry)]
    assert [(r.reason, r.action, r.attempt) for r in retries] == [
        ("rate_limit", "retry", 1), ("overloaded", "retry", 2),
    ]
    assert len(sleeps) == 2
    assert isinstance(events[-1], TurnFinished)
    assert agent.messages[-1]["content"] == "ok"
    assert "retry_rate_limit" in agent._recovery_transitions
    assert provider.calls == 3


def test_retry_after_header_sets_the_wait(tmp_path):
    agent, sleeps = _agent(tmp_path, _Scripted(_err(429, retry_after=4.0)))
    list(agent.run_turn("hello"))
    assert sleeps == [4.0]


def test_auth_error_is_not_retried(tmp_path):
    provider = _Scripted(_err(401, "invalid api key"))
    agent, sleeps = _agent(tmp_path, provider, fallback_model="backup")
    with pytest.raises(ProviderError):
        list(agent.run_turn("hello"))
    assert provider.calls == 1 and sleeps == []


def test_retries_are_bounded_then_error_surfaces(tmp_path):
    provider = _Scripted(*[_err(503) for _ in range(10)])
    agent, sleeps = _agent(tmp_path, provider, max_retries=2)
    with pytest.raises(ProviderError):
        list(agent.run_turn("hello"))
    assert provider.calls == 3 and len(sleeps) == 2


def test_failed_turn_is_persisted(tmp_path):
    agent, _ = _agent(tmp_path, _Scripted(*[_err(503) for _ in range(10)]), max_retries=0)
    with pytest.raises(ProviderError):
        list(agent.run_turn("remember this question"))
    saved = agent.sessions.load(agent.session_id)
    assert saved is not None
    assert any(m.get("content") == "remember this question" for m in saved["messages"])


def test_no_retry_after_text_was_streamed(tmp_path):
    # The user already saw "Hal"; replaying would print it twice.
    provider = _Scripted(_err(503), partial="Hal")
    agent, sleeps = _agent(tmp_path, provider)
    with pytest.raises(ProviderError):
        list(agent.run_turn("hello"))
    assert provider.calls == 1 and sleeps == []


def test_hidden_stream_can_still_be_retried(tmp_path):
    provider = _Scripted(_err(503), partial="Hal")
    agent, sleeps = _agent(tmp_path, provider, stream_output=False)
    events = list(agent.run_turn("hello"))
    assert isinstance(events[-1], TurnFinished) and len(sleeps) == 1


def test_fallback_after_retries_exhausted_then_main_model_next_turn(tmp_path):
    main = _Scripted(*[_err(503) for _ in range(3)])
    backup = _Scripted(text="from backup")
    agent, sleeps = _agent(tmp_path, main, max_retries=2, fallback_model="backup-model")
    built: list[LilBotConfig] = []

    def build(cfg):
        built.append(cfg)
        return backup

    agent._build_fallback_provider = build
    events = list(agent.run_turn("hello"))
    actions = [(e.reason, e.action) for e in events if isinstance(e, ProviderRetry)]
    assert actions == [("overloaded", "retry"), ("overloaded", "retry"), ("overloaded", "fallback")]
    assert agent.messages[-1]["content"] == "from backup"
    assert built[0].model == "backup-model" and built[0].base_url == "https://main.example/v1"
    assert agent.config.model == "main-model"  # the main config is untouched

    # Next turn goes back to the main model (which has recovered by now).
    list(agent.run_turn("again"))
    assert main.calls == 4 and backup.calls == 1


def test_model_not_found_falls_back_without_waiting(tmp_path):
    backup = _Scripted(text="from backup")
    agent, sleeps = _agent(tmp_path, _Scripted(_err(404, "model_not_found")),
                           fallback_model="backup-model", fallback_base_url="https://other.example/v1/")
    built: list[LilBotConfig] = []
    agent._build_fallback_provider = lambda cfg: built.append(cfg) or backup
    list(agent.run_turn("hello"))
    assert sleeps == []
    assert built[0].base_url == "https://other.example/v1"
    assert agent.messages[-1]["content"] == "from backup"


def test_fallback_that_also_fails_surfaces_the_error(tmp_path):
    agent, _ = _agent(tmp_path, _Scripted(_err(404)), fallback_model="backup-model", max_retries=0)
    backup = _Scripted(_err(404))
    agent._build_fallback_provider = lambda cfg: backup
    with pytest.raises(ProviderError):
        list(agent.run_turn("hello"))
    assert backup.calls == 1  # fallback is tried once per turn, not in a loop


def test_fallback_equal_to_main_model_is_ignored(tmp_path):
    agent, _ = _agent(tmp_path, _Scripted(_err(404)), fallback_model="main-model")
    agent._build_fallback_provider = lambda cfg: pytest.fail("should not build")
    with pytest.raises(ProviderError):
        list(agent.run_turn("hello"))


def test_env_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("LILBOT_MAX_RETRIES", "5")
    monkeypatch.setenv("LILBOT_FALLBACK_MODEL", "MiniMax-M2")
    monkeypatch.setenv("LILBOT_FALLBACK_BASE_URL", "https://api.minimaxi.com/v1/")
    cfg = apply_env_overrides(LilBotConfig(workspace=tmp_path))
    assert cfg.max_retries == 5
    assert cfg.fallback_model == "MiniMax-M2"
    assert cfg.fallback_base_url == "https://api.minimaxi.com/v1"


def test_retry_notice_text():
    from lilbot.tui.classic import _retry_notice

    assert "retry 2 in 4.0s" in _retry_notice(ProviderRetry("rate_limit", "retry", 2, 4.0, "429"))
    assert "fallback model MiniMax-M2" in _retry_notice(ProviderRetry("overloaded", "fallback", detail="MiniMax-M2"))
