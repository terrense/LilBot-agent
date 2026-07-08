"""CC-parity batch 3: tool contract (validateInput / contextModifier /
isDestructive / maxResultSizeChars), cache accounting, and the structured
event log.
"""
from __future__ import annotations

from pathlib import Path

from lilbot.config import LilBotConfig
from lilbot.core.agent import CACHE_BREAK_MIN_PROMPT, Agent
from lilbot.core.eventlog import EventLog
from lilbot.core.events import ProviderTurn, ToolCall
from lilbot.tools import ToolContext, ToolDef, ToolRegistry, ToolResult
from lilbot.tools.builtin import _edit_file_validate, _shell_is_destructive
from lilbot.tools.offload import maybe_offload
from lilbot.tools.registry import ValidationResult


# ── #4 validateInput ─────────────────────────────────────────────────────

def test_validation_blocks_handler(tmp_path):
    ran = {"handler": False}

    def handler(args, ctx):
        ran["handler"] = True
        return ToolResult(True, "ok")

    def validate(args, ctx):
        return (False, "bad input")

    registry = ToolRegistry()
    registry.register(ToolDef("t", "x", {"type": "object"}, handler, validate=validate))
    result, _ = registry.execute("t", {}, _ctx())
    assert result.ok is False
    assert result.output == "bad input"
    assert result.metadata.get("validation_error") is True
    assert ran["handler"] is False  # never ran


def test_validation_result_forms_normalized():
    tool = ToolDef("t", "x", {"type": "object"}, lambda a, c: ToolResult(True, ""))
    tool.validate = lambda a, c: None
    assert tool.run_validation({}, None).ok is True
    tool.validate = lambda a, c: ValidationResult(False, "m", 42)
    r = tool.run_validation({}, None)
    assert r.ok is False and r.message == "m" and r.error_code == 42
    tool.validate = lambda a, c: (True, "")
    assert tool.run_validation({}, None).ok is True


def test_edit_file_validate_rejects_noop():
    ok, _ = _edit_file_validate({"old": "a", "new": "b"}, None)
    assert ok is True
    ok, msg = _edit_file_validate({"old": "same", "new": "same"}, None)
    assert ok is False and "identical" in msg
    ok, msg = _edit_file_validate({"old": "", "new": "x"}, None)
    assert ok is False and "non-empty" in msg


# ── #4 contextModifier ───────────────────────────────────────────────────

def test_context_modifier_applied_for_non_concurrency_safe(tmp_path):
    def handler(args, ctx):
        return ToolResult(True, "done", context_modifier=lambda c: _tagged(c))

    registry = ToolRegistry()
    # No ReadOnly capability -> not concurrency-safe -> modifier honored.
    registry.register(ToolDef("mut", "x", {"type": "object"}, handler))
    agent = _agent(tmp_path, registry)
    agent._run_one_call(ToolCall("mut", {}))
    assert getattr(agent.ctx, "_tag", None) == "modified"


# ── #4 isDestructive ─────────────────────────────────────────────────────

def test_destructive_predicate_and_metadata(tmp_path):
    assert _shell_is_destructive({"command": "rm -rf build"}) is True
    assert _shell_is_destructive({"command": "git push --force origin main"}) is True
    assert _shell_is_destructive({"command": "ls -la"}) is False

    def handler(args, ctx):
        return ToolResult(True, "ok")

    registry = ToolRegistry()
    registry.register(ToolDef("dangit", "x", {"type": "object"}, handler,
                              destructive_check=lambda a: True))
    result, _ = registry.execute("dangit", {}, _ctx())
    assert result.metadata.get("destructive") is True


# ── #4 maxResultSizeChars ────────────────────────────────────────────────

def test_max_result_chars_opt_out():
    big = "X" * 100_000
    # limit=-1 -> never offload (CC's Infinity)
    out, extra = maybe_offload(big, None, -1)
    assert out == big and extra == {}
    # default -> truncates/offloads
    out2, extra2 = maybe_offload(big, None, 0)
    assert len(out2) < len(big) and extra2


# ── #17 cache accounting ─────────────────────────────────────────────────

def test_cache_stats_hit_rate_and_break(tmp_path):
    agent = _agent(tmp_path, ToolRegistry())
    # A warm turn: big prompt, mostly cache read.
    agent._record_cache_usage({"prompt_tokens": 10_000, "cache_read_tokens": 9_000})
    stats = agent.cache_stats()
    assert stats["hit_rate"] == 0.9
    assert stats["breaks"] == 0
    # A subsequent big prompt with zero cache read -> counted as a break.
    agent._record_cache_usage({"prompt_tokens": CACHE_BREAK_MIN_PROMPT + 1, "cache_read_tokens": 0})
    assert agent.cache_stats()["breaks"] == 1


# ── #18 structured event log ─────────────────────────────────────────────

def test_event_log_writes_and_reads(tmp_path):
    log = EventLog(tmp_path)
    log.log("tool_call", tool="bash", ok=True, elapsed_ms=12)
    log.log("compaction", method="prune", before_tokens=100, after_tokens=40)
    records = log.read_all()
    assert [r["event"] for r in records] == ["lilbot_tool_call", "lilbot_compaction"]
    assert records[0]["tool"] == "bash"


def test_event_log_only_serializes_scalars(tmp_path):
    log = EventLog(tmp_path)
    log.log("x", good="str", num=1, obj={"leak": "secret"}, lst=[1, 2])
    rec = log.read_all()[0]
    assert rec["good"] == "str" and rec["num"] == 1
    assert rec["obj"] == "<dict>" and rec["lst"] == "<list>"  # non-scalars redacted to type


def test_event_log_disabled_without_state_dir():
    log = EventLog(None)
    assert log.enabled() is False
    log.log("x", a=1)  # no-op, no crash
    assert log.read_all() == []


def test_agent_turn_emits_events(tmp_path):
    provider = _FinalProvider()
    agent = _agent(tmp_path, ToolRegistry(), state=True)
    list(agent.run_turn("hi"))
    events = [r["event"] for r in agent.events.read_all()]
    assert "lilbot_turn_start" in events
    assert "lilbot_turn_finished" in events


# ── helpers ──────────────────────────────────────────────────────────────

def _tagged(ctx):
    try:
        ctx._tag = "modified"
    except Exception:
        pass
    return ctx


class _Mem:
    def context(self): return "(none)"
    def list(self): return []


class _Sk:
    def list(self): return []


class _FinalProvider:
    def complete(self, messages, tools):
        return ProviderTurn(content="done", usage={"prompt_tokens": 10})

    def complete_stream(self, messages, tools):
        from lilbot.core.events import StreamEvent
        yield StreamEvent(final=self.complete(messages, tools))


def _ctx() -> ToolContext:
    return ToolContext(sandbox=None, permissions=None, memory=_Mem(), skills=_Sk(),
                       subagents=None, mcp=None, config=None)


def _agent(tmp_path: Path, registry: ToolRegistry, state: bool = False) -> Agent:
    cfg = LilBotConfig(workspace=tmp_path, context_window=40_000)
    ctx = ToolContext(sandbox=None, permissions=None, memory=_Mem(), skills=_Sk(),
                      subagents=None, mcp=None, config=cfg if state else None)
    return Agent(cfg, _FinalProvider(), registry, ctx)
