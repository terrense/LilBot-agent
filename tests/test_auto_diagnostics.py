"""Tests for M2 — auto diagnostics injection after edits (CodeWhale loop)."""
from __future__ import annotations

from lilbot.config import LilBotConfig
from lilbot.core.agent import Agent
from lilbot.sandbox import Sandbox
from lilbot.tools import ToolContext, ToolRegistry, register_builtins


class _Mem:
    def context(self, *a, **k):
        return "(none)"


class _Skills:
    def list(self, *a, **k):
        return []


class _P:
    def complete(self, messages, tools):
        from lilbot.core.events import ProviderTurn
        return ProviderTurn(content="ok")


def _agent(tmp_path):
    cfg = LilBotConfig(workspace=tmp_path)
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    registry = ToolRegistry()
    register_builtins(registry)
    ctx = ToolContext(Sandbox(tmp_path), None, _Mem(), _Skills(), None, None, cfg)
    return Agent(cfg, _P(), registry, ctx)


def test_syntax_error_is_diagnosed_and_stashed(tmp_path):
    (tmp_path / "bad.py").write_text("def f(:\n    pass\n", encoding="utf-8")
    agent = _agent(tmp_path)
    agent._edited_this_turn = ["bad.py"]
    agent._run_post_edit_diagnostics()
    assert "bad.py" in agent._pending_diagnostics
    assert "error" in agent._pending_diagnostics.lower()


def test_clean_file_produces_no_diagnostics(tmp_path):
    (tmp_path / "good.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    agent = _agent(tmp_path)
    agent._edited_this_turn = ["good.py"]
    agent._run_post_edit_diagnostics()
    assert agent._pending_diagnostics == ""


def test_non_diagnosable_extension_skipped(tmp_path):
    (tmp_path / "notes.txt").write_text("def f(:\n", encoding="utf-8")
    agent = _agent(tmp_path)
    agent._edited_this_turn = ["notes.txt"]
    agent._run_post_edit_diagnostics()
    assert agent._pending_diagnostics == ""


def test_disabled_via_config(tmp_path):
    (tmp_path / "bad.py").write_text("def f(:\n", encoding="utf-8")
    agent = _agent(tmp_path)
    agent.config.auto_diagnostics = False
    agent._edited_this_turn = ["bad.py"]
    agent._run_post_edit_diagnostics()
    assert agent._pending_diagnostics == ""


def test_diagnostics_injected_into_next_provider_call_then_cleared(tmp_path):
    agent = _agent(tmp_path)
    agent._pending_diagnostics = "DIAG: bad.py L1 [error] oops"
    msgs = agent._provider_messages()
    assert any("DIAG: bad.py" in str(m.get("content")) for m in msgs)
    # One-shot: cleared after consumption.
    assert agent._pending_diagnostics == ""
    msgs2 = agent._provider_messages()
    assert not any("DIAG: bad.py" in str(m.get("content")) for m in msgs2)


def test_edit_records_path_for_diagnosis(tmp_path):
    from lilbot.core.events import ToolCall
    from lilbot.tools.registry import ToolDef, ToolResult
    agent = _agent(tmp_path)
    # Stub the write_file tool so _run_one_call records the edited path.
    agent.registry.register(ToolDef("write_file", "w", {"type": "object", "properties": {}},
                                    lambda a, c: ToolResult(True, "written")))
    agent._run_one_call(ToolCall("write_file", {"path": "x.py", "content": "y"}))
    assert "x.py" in agent._edited_this_turn
