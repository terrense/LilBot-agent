"""A recovery matrix: classified failures are recovered, never persisted as success.

Each row injects a failure (or a truncated response) and either a subsequent
success or an exhausted chain. The assertions are about observable outcomes:
a recovered turn ends completed; an exhausted turn raises or ends with an
explicit non-success outcome; a partial answer is kept but marked incomplete.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from lilbot.config import LilBotConfig
from lilbot.core.agent import Agent
from lilbot.core.events import (
    ProviderTurn,
    StreamEvent,
    ToolCall,
    ToolFinished,
    ToolStarted,
    TurnFinished,
)
from lilbot.llm.providers import ProviderError
from lilbot.tools import ToolContext, ToolRegistry


class _Mem:
    def context(self, *a, **k):
        return "(none)"


class _Sk:
    def list(self, *a, **k):
        return []


@dataclass
class Item:
    kind: str
    text: str = ""
    error: ProviderError | None = None


def final(text: str = "OK") -> Item:
    return Item("final", text)


def length(text: str = "partial") -> Item:
    return Item("length", text)


def length_tool() -> Item:
    return Item("length_tool")


def fail(reason: str | None = None, status: int | None = None, overflow: bool = False) -> Item:
    return Item(
        "error",
        error=ProviderError(
            f"injected {reason or status or 'failure'}",
            reason=reason,
            status_code=status,
            is_overflow=overflow,
        ),
    )


def text_then_fail(text: str, reason: str | None = None, status: int | None = None) -> Item:
    return Item("text_error", text=text, error=ProviderError(
        f"injected {reason or status or 'failure'}", reason=reason, status_code=status
    ))


class ScriptedProvider:
    def __init__(self, script: list[Item]):
        self.script = script
        self.calls = 0

    def _next(self) -> Item:
        if self.calls >= len(self.script):
            raise AssertionError(f"script exhausted after {self.calls} provider calls")
        item = self.script[self.calls]
        self.calls += 1
        return item

    def complete(self, messages, tools):
        item = self._next()
        if item.kind == "error":
            raise item.error
        if item.kind == "text_error":
            raise item.error
        return ProviderTurn(content=item.text or "side", finish_reason="stop")

    def complete_stream(self, messages, tools):
        item = self._next()
        if item.kind == "final":
            yield StreamEvent(final=ProviderTurn(content=item.text, finish_reason="stop"))
            return
        if item.kind == "length":
            yield StreamEvent(final=ProviderTurn(content=item.text, finish_reason="length"))
            return
        if item.kind == "length_tool":
            yield StreamEvent(final=ProviderTurn(
                content="",
                tool_calls=[ToolCall("write_file", {"path": "x"})],
                finish_reason="length",
            ))
            return
        if item.kind == "error":
            raise item.error
        if item.kind == "text_error":
            if item.text:
                yield StreamEvent(text=item.text)
            raise item.error
        raise AssertionError(f"unknown script item {item.kind}")


def _agent(tmp_path, provider, *, compact_raises=False, allow_fallback=False):
    cfg = LilBotConfig(
        workspace=tmp_path,
        model="mystery-model",
        base_url="https://unknown.example/v1",
        max_retries=1,
    )
    ctx = ToolContext(
        sandbox=None, permissions=None, memory=_Mem(), skills=_Sk(),
        subagents=None, mcp=None, config=None,
    )
    agent = Agent(cfg, provider, ToolRegistry(), ctx)
    agent._start_recall_prefetch = lambda text: None
    agent._maybe_compact = lambda: None
    agent._maybe_extract = lambda: None
    agent._maybe_update_session_memory = lambda: None
    agent._sleep = lambda delay: None
    if compact_raises:
        def bad_compact(manual=False):
            raise RuntimeError("summary provider failed")
        agent.compact = bad_compact
    else:
        agent.compact = lambda manual=False: "compacted"
    used = {"fallback": False}

    def activate():
        if not allow_fallback or used["fallback"]:
            return False
        used["fallback"] = True
        return True

    agent._activate_fallback = activate
    return agent


def _run(tmp_path, case):
    provider = ScriptedProvider(case["script"])
    agent = _agent(
        tmp_path,
        provider,
        compact_raises=case.get("compact_raises", False),
        allow_fallback=case.get("allow_fallback", False),
    )
    events = []
    error = None
    try:
        events = list(agent.run_turn("go"))
    except ProviderError as exc:
        error = exc
    return agent, events, error


def _assistant_text(agent) -> list[str]:
    return [
        str(m.get("content") or "")
        for m in agent.messages
        if m.get("role") == "assistant"
    ]


SCENARIOS = [
    {"name": "server error then success", "script": [fail("server_error"), final()], "expect": "success"},
    {"name": "partial text then server error then success", "script": [text_then_fail("partial", "server_error"), final()], "expect": "success"},
    {"name": "rate limit then success", "script": [fail(status=429), final()], "expect": "success"},
    {"name": "timeout then success", "script": [fail("timeout"), final()], "expect": "success"},
    {"name": "network then success", "script": [fail("network"), final()], "expect": "success"},
    {"name": "retry then fallback then success", "script": [fail("server_error"), fail("server_error"), final()], "expect": "success", "allow_fallback": True},
    {"name": "overflow compacts then success", "script": [fail(overflow=True), final()], "expect": "success"},
    {"name": "overflow twice then success", "script": [fail(overflow=True), fail(overflow=True), final()], "expect": "success"},
    {"name": "overflow exhausted without fallback", "script": [fail(overflow=True), fail(overflow=True), fail(overflow=True)], "expect": "error"},
    {"name": "overflow exhausted then fallback success", "script": [fail(overflow=True), fail(overflow=True), fail(overflow=True), final()], "expect": "success", "allow_fallback": True},
    {"name": "billing then fallback success", "script": [fail(status=402), final()], "expect": "success", "allow_fallback": True},
    {"name": "model not found then fallback success", "script": [fail(status=404), final()], "expect": "success", "allow_fallback": True},
    {"name": "auth is not retried", "script": [fail(status=401)], "expect": "error"},
    {"name": "format error is not retried", "script": [fail(status=400)], "expect": "error"},
    {"name": "output truncation resumes then succeeds", "script": [length(), final()], "expect": "success"},
    {"name": "output truncation exhausted", "script": [length(), length(), length(), length()], "expect": "truncated"},
    {"name": "truncated tool call is not executed then succeeds", "script": [length_tool(), final()], "expect": "success"},
    {"name": "truncated tool call exhausted", "script": [length_tool(), length_tool(), length_tool(), length_tool()], "expect": "truncated"},
    {"name": "compaction failure then fallback success", "script": [fail(overflow=True), final()], "expect": "success", "compact_raises": True, "allow_fallback": True},
    {"name": "compaction failure without fallback", "script": [fail(overflow=True)], "expect": "error", "compact_raises": True},
    {"name": "partial auth is preserved but fails", "script": [text_then_fail("keep me", status=401)], "expect": "partial_error"},
    {"name": "partial survives a silent retry failure", "script": [text_then_fail("keep me", "server_error"), fail(status=401)], "expect": "partial_error"},
    {"name": "fallback also fails", "script": [fail("server_error"), fail("server_error"), fail("server_error"), fail("server_error")], "expect": "error", "allow_fallback": True},
]


def test_recovery_matrix_has_at_least_twenty_cases():
    assert len(SCENARIOS) >= 20


@pytest.mark.parametrize("case", SCENARIOS, ids=[case["name"] for case in SCENARIOS])
def test_recovery_matrix(tmp_path, case):
    agent, events, error = _run(tmp_path, case)
    expect = case["expect"]

    if expect == "success":
        assert error is None
        finished = [e for e in events if isinstance(e, TurnFinished)]
        assert finished and finished[-1].outcome == "completed"
        assert any("OK" in text for text in _assistant_text(agent))
        return

    if expect == "truncated":
        assert error is None
        finished = [e for e in events if isinstance(e, TurnFinished)]
        assert finished and finished[-1].outcome == "output_truncated"
        assert any(
            "incomplete" in str(m.get("content", ""))
            for m in agent.messages
            if m.get("role") == "system"
        )
        assert not any(isinstance(e, ToolStarted) for e in events)
        assert not any(isinstance(e, ToolFinished) for e in events)
        return

    assert error is not None
    assert not any("OK" == text.strip() for text in _assistant_text(agent))
    if expect == "partial_error":
        assert any("keep me" in text for text in _assistant_text(agent))
        assert any(
            "incomplete" in str(m.get("content", ""))
            for m in agent.messages
            if m.get("role") == "system"
        )
