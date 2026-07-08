"""CC-parity batch 2: input-level concurrency, searchHint, structured hooks
(updatedInput / Stop hook), and the bounded overflow-recovery chain.
"""
from __future__ import annotations

import types
from pathlib import Path

import lilbot.hooks.engine as hook_engine
from lilbot.config import LilBotConfig
from lilbot.core.agent import (
    MAX_OUTPUT_TRUNCATIONS,
    MAX_OVERFLOW_RECOVERIES,
    MAX_STOP_CONTINUATIONS,
    Agent,
)
from lilbot.core.events import ProviderTurn, ToolCall
from lilbot.hooks import HookAction, HookContext, HookEngine, HookMatch, Hook
from lilbot.hooks.engine import _parse_structured
from lilbot.llm.providers import ProviderError
from lilbot.tools import ToolContext, ToolDef, ToolRegistry, ToolResult
from lilbot.tools.builtin import _read_only_command_safe


# ── #3 input-level concurrency safety ────────────────────────────────────

def test_read_only_command_predicate():
    assert _read_only_command_safe({"command": "ls -la"}) is True
    assert _read_only_command_safe({"command": "git status -s"}) is True
    assert _read_only_command_safe({"command": "grep -r foo ."}) is True
    assert _read_only_command_safe({"command": "rm -rf build"}) is False
    assert _read_only_command_safe({"command": "git push"}) is False
    # Compound commands never auto-classify as safe.
    assert _read_only_command_safe({"command": "ls && rm x"}) is False
    # Backgrounded read-only command stays serial.
    assert _read_only_command_safe({"command": "ls", "background": True}) is False


def test_tooldef_is_concurrency_safe_per_input():
    tool = ToolDef(
        "bash", "shell", {"type": "object"}, lambda a, c: ToolResult(True, ""),
        concurrency_check=_read_only_command_safe,
    )
    assert tool.is_concurrency_safe({"command": "ls"}) is True
    assert tool.is_concurrency_safe({"command": "rm x"}) is False


def test_partition_groups_read_only_bash_but_not_mutating():
    registry = ToolRegistry()
    registry.register(ToolDef(
        "bash", "shell", {"type": "object"}, lambda a, c: ToolResult(True, ""),
        concurrency_check=_read_only_command_safe,
    ))
    ctx = ToolContext(sandbox=None, permissions=None, memory=_Mem(), skills=_Sk(),
                      subagents=None, mcp=None, config=None)
    agent = Agent(LilBotConfig(workspace=Path(".")), _NoopProvider(), registry, ctx)
    calls = [
        ToolCall("bash", {"command": "ls"}),
        ToolCall("bash", {"command": "git status"}),
        ToolCall("bash", {"command": "rm -rf x"}),
        ToolCall("bash", {"command": "cat f"}),
    ]
    batches = agent._partition_calls(calls)
    # First two read-only calls batch together; the rm is its own batch; cat is
    # its own batch (a mutating call breaks the run).
    assert [len(b) for b in batches] == [2, 1, 1]


# ── #5 searchHint ────────────────────────────────────────────────────────

def test_search_hint_surfaces_deferred_tool():
    registry = ToolRegistry()
    registry.register(ToolDef(
        "NotebookEdit", "Edit a notebook cell", {"type": "object"},
        lambda a, c: ToolResult(True, ""), should_defer=True, search_hint="jupyter ipynb",
    ))
    registry.register(ToolDef(
        "other", "unrelated tool", {"type": "object"},
        lambda a, c: ToolResult(True, ""), should_defer=True,
    ))
    hits = registry.search_deferred("jupyter", max_results=5)
    assert hits and hits[0]["name"] == "NotebookEdit"


# ── #16 structured hooks ─────────────────────────────────────────────────

def _fake_run(stdout: str, returncode: int = 0):
    def _run(argv, **kwargs):
        return types.SimpleNamespace(stdout=stdout, stderr="", returncode=returncode)
    return _run


def test_parse_structured_only_activates_on_json_object():
    assert _parse_structured("plain text") == {}
    assert _parse_structured('{"decision": "block"}') == {"decision": "block"}
    assert _parse_structured("[1,2]") == {}


def test_pre_tool_hook_rewrites_input(monkeypatch):
    monkeypatch.setattr(hook_engine.subprocess, "run",
                        _fake_run('{"updatedInput": {"path": "safe.txt"}}'))
    hook = Hook(id="rw", event="pre_tool_use",
                action=HookAction(type="command", command="whatever"),
                match=HookMatch(tool="write_file"))
    engine = HookEngine([hook])
    outcome = engine.run_pre_tool(HookContext(event="pre_tool_use", tool_name="write_file"))
    assert outcome.block is None
    assert outcome.updated_input == {"path": "safe.txt"}


def test_pre_tool_hook_json_block(monkeypatch):
    monkeypatch.setattr(hook_engine.subprocess, "run",
                        _fake_run('{"decision": "block", "reason": "no writes to prod"}'))
    hook = Hook(id="guard", event="pre_tool_use",
                action=HookAction(type="command", command="x"),
                match=HookMatch(tool="write_file"))
    engine = HookEngine([hook])
    outcome = engine.run_pre_tool(HookContext(event="pre_tool_use", tool_name="write_file"))
    assert outcome.block == "no writes to prod"


def test_stop_hook_forces_continuation(monkeypatch):
    monkeypatch.setattr(hook_engine.subprocess, "run",
                        _fake_run('{"continue": false, "reason": "tests not run yet"}'))
    hook = Hook(id="gate", event="stop",
                action=HookAction(type="command", command="x"))
    engine = HookEngine([hook])
    cont = engine.run_stop(HookContext(event="stop"))
    assert cont == "tests not run yet"


def test_agent_stop_hook_continues_then_caps(tmp_path):
    # A stop hook that always blocks would loop forever; the agent caps it.
    provider = _FinalTextProvider()
    hook = Hook(id="always", event="stop",
                action=HookAction(type="block", message="keep going"))
    agent = _agent(tmp_path, provider)
    agent.hooks = HookEngine([hook])
    events = list(agent.run_turn("do something"))
    # The turn eventually ended (did not hang); continuations were capped.
    assert agent._stop_continuations == MAX_STOP_CONTINUATIONS
    # The forced continuation instruction was injected each time.
    injected = [m for m in agent.messages if m.get("role") == "user" and "stop hook" in str(m.get("content", ""))]
    assert len(injected) == MAX_STOP_CONTINUATIONS


# ── #8/#23 structured overflow + bounded recovery chain ──────────────────

def test_provider_error_carries_structured_overflow():
    exc = ProviderError("boom", status_code=413, is_overflow=True)
    assert exc.status_code == 413
    assert exc.is_overflow is True


def test_overflow_recovery_recovers_then_records_transition(tmp_path):
    provider = _OverflowThenOkProvider(fail_times=1)
    agent = _agent(tmp_path, provider)
    list(agent.run_turn("hello"))
    assert "reactive_compact_retry" in agent._recovery_transitions
    assert provider.ok_calls >= 1  # recovered and completed


def test_overflow_recovery_is_bounded(tmp_path):
    # Always overflows -> after MAX_OVERFLOW_RECOVERIES the error surfaces.
    provider = _OverflowThenOkProvider(fail_times=99)
    agent = _agent(tmp_path, provider)
    raised = False
    try:
        list(agent.run_turn("hello"))
    except ProviderError as exc:
        raised = exc.is_overflow
    assert raised
    assert agent._recovery_transitions.count("reactive_compact_retry") == MAX_OVERFLOW_RECOVERIES


# ── #9 output-truncation (max_output_tokens) resume ──────────────────────

class _TruncatedThenDoneProvider:
    def __init__(self, truncate_times: int):
        self.truncate_times = truncate_times
        self.calls = 0

    def complete(self, messages, tools):
        self.calls += 1
        if self.truncate_times > 0:
            self.truncate_times -= 1
            return ProviderTurn(content="partial...", usage={"prompt_tokens": 10}, finish_reason="length")
        return ProviderTurn(content="complete answer", usage={"prompt_tokens": 10}, finish_reason="stop")

    def complete_stream(self, messages, tools):
        from lilbot.core.events import StreamEvent
        yield StreamEvent(final=self.complete(messages, tools))


def test_output_truncation_resumes_and_records_transition(tmp_path):
    provider = _TruncatedThenDoneProvider(truncate_times=1)
    agent = _agent(tmp_path, provider)
    list(agent.run_turn("write something long"))
    assert "max_output_tokens_recovery" in agent._recovery_transitions
    # A resume instruction was injected exactly once.
    resumes = [m for m in agent.messages if m.get("role") == "user" and "Output token limit hit" in str(m.get("content", ""))]
    assert len(resumes) == 1
    assert provider.calls == 2  # truncated once, then completed


def test_output_truncation_is_bounded(tmp_path):
    provider = _TruncatedThenDoneProvider(truncate_times=99)
    agent = _agent(tmp_path, provider)
    list(agent.run_turn("write forever"))
    assert agent._output_truncations == MAX_OUTPUT_TRUNCATIONS  # capped, no infinite loop


# ── helpers ──────────────────────────────────────────────────────────────

class _Mem:
    def context(self): return "(none)"
    def list(self): return []


class _Sk:
    def list(self): return []


class _NoopProvider:
    def complete(self, messages, tools):
        return ProviderTurn(content="done")


class _FinalTextProvider:
    """Always returns a final text answer (no tool calls) so the turn ends."""
    def complete(self, messages, tools):
        return ProviderTurn(content="final answer", usage={"prompt_tokens": 10})

    def complete_stream(self, messages, tools):
        from lilbot.core.events import StreamEvent
        yield StreamEvent(final=self.complete(messages, tools))


class _OverflowThenOkProvider:
    def __init__(self, fail_times: int):
        self.fail_times = fail_times
        self.ok_calls = 0

    def complete(self, messages, tools):
        if self.fail_times > 0:
            self.fail_times -= 1
            raise ProviderError("context_length_exceeded", status_code=400, is_overflow=True)
        self.ok_calls += 1
        return ProviderTurn(content="ok", usage={"prompt_tokens": 10})

    def complete_stream(self, messages, tools):
        from lilbot.core.events import StreamEvent
        yield StreamEvent(final=self.complete(messages, tools))


def _agent(tmp_path: Path, provider) -> Agent:
    ctx = ToolContext(sandbox=None, permissions=None, memory=_Mem(), skills=_Sk(),
                      subagents=None, mcp=None, config=None)
    cfg = LilBotConfig(workspace=tmp_path, context_window=40_000)
    return Agent(cfg, provider, ToolRegistry(), ctx)
