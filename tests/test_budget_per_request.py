"""The context budget is checked before every provider call, not once a turn."""

from __future__ import annotations

from lilbot.config import LilBotConfig
from lilbot.core.agent import Agent
from lilbot.core.events import ProviderTurn, ToolCall
from lilbot.tools import ToolContext, ToolDef, ToolRegistry, ToolResult


class _Mem:
    def context(self, *a, **k):
        return "(none)"


class _Sk:
    def list(self, *a, **k):
        return []


class _RecordingProvider:
    """Returns a tool call whose result is large enough to blow the window.

    Every request is recorded so a test can see what was actually sent.
    """

    def __init__(self, payload: str = "x" * 40_000):
        self.payload = payload
        self.calls = 0
        self.seen: list[list[dict]] = []

    def complete(self, messages, tools):
        self.calls += 1
        self.seen.append([dict(m) for m in messages])
        if self.calls == 1:
            return ProviderTurn(tool_calls=[ToolCall("read_file", {"path": "x"})])
        return ProviderTurn(content="done")

    def complete_stream(self, messages, tools):
        from lilbot.core.events import StreamEvent
        yield StreamEvent(final=self.complete(messages, tools))


def _agent(tmp_path, provider, registry, **cfg) -> Agent:
    ctx = ToolContext(
        sandbox=None, permissions=None, memory=_Mem(), skills=_Sk(),
        subagents=None, mcp=None, config=None,
    )
    config = LilBotConfig(workspace=tmp_path, **cfg)
    return Agent(config, provider, registry, ctx)


def _tool_payload(messages: list[dict]) -> str:
    for message in messages:
        if message.get("role") == "tool":
            return str(message.get("content") or "")
    return ""


def test_oversized_result_is_shed_before_the_next_request(tmp_path):
    """The second request must not carry the full oversized tool result."""
    registry = ToolRegistry()
    registry.register(ToolDef(
        "read_file", "read", {"type": "object", "properties": {}},
        lambda a, c: ToolResult(True, "x" * 40_000),
    ))
    provider = _RecordingProvider()
    agent = _agent(
        tmp_path, provider, registry,
        model="mystery-small", base_url="https://unknown.example/v1",
        context_window=16_000, context_window_explicit=True,
        max_steps=6,
    )
    list(agent.run_turn("read a lot"))

    assert provider.calls >= 2
    second = _tool_payload(provider.seen[1])
    assert second, "the tool result should still be present as an observation"
    assert len(second) < 40_000, "the oversized result was sent to the model intact"
    assert "x" * 40_000 not in second


def test_budget_check_is_a_noop_below_the_threshold(tmp_path):
    registry = ToolRegistry()
    registry.register(ToolDef(
        "read_file", "read", {"type": "object", "properties": {}},
        lambda a, c: ToolResult(True, "small result"),
    ))
    provider = _RecordingProvider(payload="small result")
    agent = _agent(
        tmp_path, provider, registry,
        model="mystery-small", base_url="https://unknown.example/v1",
        context_window=1_000_000, context_window_explicit=True,
    )
    list(agent.run_turn("hello"))
    assert _tool_payload(provider.seen[1]) == "small result"


def test_schema_tokens_are_counted_in_the_estimate(tmp_path):
    """A large tool catalog alone pushes a request over the threshold."""
    registry = ToolRegistry()
    for index in range(40):
        registry.register(ToolDef(
            f"tool_{index}", "x" * 400, {"type": "object", "properties": {}},
            lambda a, c: ToolResult(True, "ok"),
        ))
    provider = _RecordingProvider()
    agent = _agent(
        tmp_path, provider, registry,
        model="mystery-small", base_url="https://unknown.example/v1",
        context_window=16_000, context_window_explicit=True,
    )
    schemas = registry.schemas()
    agent._update_schema_tokens(schemas)
    breakdown = agent.token_breakdown()
    assert breakdown["tool_schema_tokens"] > 0
    assert breakdown["total_tokens"] >= breakdown["tool_schema_tokens"]


def test_effective_window_is_used_by_the_trigger(tmp_path):
    """A 64k model must not be given the 128k default budget."""
    registry = ToolRegistry()
    provider = _RecordingProvider()
    agent = _agent(
        tmp_path, provider, registry,
        model="deepseek-chat", base_url="https://api.deepseek.com",
    )
    assert agent.effective_context_window() == 64_000
    # The configured fallback is larger, and must not win.
    assert agent.config.context_window == 128_000
