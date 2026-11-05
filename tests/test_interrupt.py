from __future__ import annotations

import signal
import threading
from types import SimpleNamespace

import pytest

from lilbot.cli import run_prompt
from lilbot.config import LilBotConfig
from lilbot.core.agent import Agent
from lilbot.core.events import ProviderRetry, ProviderTurn, StreamEvent, TextDelta, ToolCall, ToolStarted, TurnFinished
from lilbot.llm.providers import ProviderError
from lilbot.tools import ToolContext, ToolDef, ToolRegistry, ToolResult
from lilbot.tui.dashboard import DashboardUI
from prompt_toolkit.keys import Keys


class _Memory:
    def context(self):
        return "(none)"

    def list(self):
        return []


class _Skills:
    def list(self):
        return []


def _agent(tmp_path, provider, registry=None, **config_kwargs):
    config = LilBotConfig(workspace=tmp_path, **config_kwargs)
    ctx = ToolContext(None, None, _Memory(), _Skills(), None, None, config)
    return Agent(config, provider, registry or ToolRegistry(), ctx)


def test_interrupt_during_last_allowed_tool_persists_result_and_skips_synthesis(tmp_path):
    started = threading.Event()
    release = threading.Event()

    class Provider:
        calls = 0

        def complete(self, messages, tools):
            self.calls += 1
            return ProviderTurn(tool_calls=[ToolCall("work", {})])

    def work(arguments, ctx):
        started.set()
        assert release.wait(5)
        return ToolResult(True, "completed before interruption")

    provider = Provider()
    registry = ToolRegistry()
    registry.register(ToolDef("work", "demo", {"type": "object"}, work))
    agent = _agent(tmp_path, provider, registry, max_steps=1)
    events, errors = [], []

    def consume():
        try:
            events.extend(agent.run_turn("do work"))
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=consume)
    worker.start()
    try:
        assert started.wait(3)
        agent.request_interrupt()
    finally:
        release.set()
        worker.join(5)
    try:
        assert not worker.is_alive()
        assert errors == []
        assert isinstance(events[-1], TurnFinished)
        assert events[-1].outcome == "interrupted"
        assert provider.calls == 1
        saved = agent.sessions.load(agent.session_id)
        assert saved["messages"] == agent.messages
        assert saved["messages"][-1]["content"] == "completed before interruption"
    finally:
        agent.close()


def test_interrupt_before_announced_tool_starts_repairs_history(tmp_path):
    executed = []

    class Provider:
        def complete(self, messages, tools):
            return ProviderTurn(tool_calls=[ToolCall("work", {}, call_id="not_started")])

    registry = ToolRegistry()
    registry.register(ToolDef("work", "demo", {}, lambda args, ctx: executed.append(True) or ToolResult(True, "ran")))
    agent = _agent(tmp_path, Provider(), registry)
    try:
        events = []
        for event in agent.run_turn("do work"):
            events.append(event)
            if isinstance(event, ToolStarted):
                agent.request_interrupt()
        assert events[-1].outcome == "interrupted"
        assert executed == []
        results = [message for message in agent.messages if message.get("role") == "tool"]
        assert results[0]["tool_call_id"] == "not_started"
        assert "result for this call is unknown" in results[0]["content"]
        assert agent.sessions.load(agent.session_id)["messages"] == agent.messages
    finally:
        agent.close()


def test_interrupt_wakes_real_retry_backoff_without_retrying(tmp_path):
    retry_seen = threading.Event()

    class Provider:
        calls = 0

        def complete(self, messages, tools):
            self.calls += 1
            raise ProviderError("busy", status_code=503, retry_after=60)

    provider = Provider()
    agent = _agent(tmp_path, provider)
    events, errors = [], []

    def consume():
        try:
            for event in agent.run_turn("hello"):
                events.append(event)
                if isinstance(event, ProviderRetry):
                    retry_seen.set()
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=consume)
    worker.start()
    try:
        assert retry_seen.wait(3)
    finally:
        agent.request_interrupt()
        worker.join(3)
    try:
        assert not worker.is_alive()
        assert errors == []
        assert events[-1].outcome == "interrupted"
        assert provider.calls == 1
    finally:
        agent.close()


def test_interrupt_stream_preserves_partial_text_and_closes_stream(tmp_path):
    closed = []

    class Provider:
        def complete_stream(self, messages, tools):
            try:
                yield StreamEvent(text="partial answer")
                yield StreamEvent(text="should not appear")
            finally:
                closed.append(True)

    agent = _agent(tmp_path, Provider())
    try:
        events = []
        for event in agent.run_turn("hello"):
            events.append(event)
            if isinstance(event, TextDelta):
                agent.request_interrupt()
        assert [event.text for event in events if isinstance(event, TextDelta)] == ["partial answer"]
        assert events[-1].outcome == "interrupted"
        assert closed == [True]
        assert agent.sessions.load(agent.session_id)["messages"][-1]["content"] == "partial answer"
    finally:
        agent.close()


@pytest.mark.parametrize("fails", [False, True])
def test_cli_installs_interrupt_handler_and_restores_it(monkeypatch, fails):
    handlers = []
    previous = object()
    interrupts = []
    monkeypatch.setattr(signal, "getsignal", lambda signum: previous)
    monkeypatch.setattr(signal, "signal", lambda signum, handler: handlers.append(handler))

    def turn(prompt):
        handlers[0](signal.SIGINT, None)
        if fails:
            raise RuntimeError("provider error")
        yield TurnFinished(0, outcome="interrupted")

    agent = SimpleNamespace(run_turn=turn, request_interrupt=lambda: interrupts.append(True))
    events = []
    ui = SimpleNamespace(event=events.append)
    if fails:
        with pytest.raises(RuntimeError):
            run_prompt(agent, ui, "hello")
    else:
        run_prompt(agent, ui, "hello")
        assert events[-1].outcome == "interrupted"
    assert interrupts == [True]
    assert handlers[-1] is previous


def test_dashboard_busy_ctrl_c_requests_interrupt():
    ui = object.__new__(DashboardUI)
    interrupts, messages = [], []
    ui.busy = True
    ui.agent = SimpleNamespace(request_interrupt=lambda: interrupts.append(True))
    ui._append = messages.append
    bindings = ui._keys().get_bindings_for_keys((Keys.ControlC,))
    bindings[-1].handler(SimpleNamespace())
    assert interrupts == [True]
    assert "Interrupt requested" in messages[-1]


def test_dashboard_reports_saved_interruption():
    ui = object.__new__(DashboardUI)
    ui.lines = []
    ui.work_items = []
    ui._refresh = lambda: None
    ui.event(TurnFinished(2, outcome="interrupted"))
    assert ui.lines[-1] == "interrupted after 2 steps; session saved"
    assert ui.work_items == ["No active work."]
