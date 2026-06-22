"""Tests for session persistence + resume (ported from mewcode)."""
from __future__ import annotations

from pathlib import Path

from lilbot.config import LilBotConfig
from lilbot.core.agent import Agent
from lilbot.core.events import ProviderTurn
from lilbot.core.session import SessionStore
from lilbot.tools import ToolContext, ToolRegistry


class _Mem:
    def context(self, *a, **k):
        return "(none)"


class _Skills:
    def list(self, *a, **k):
        return []


def _agent(tmp_path, replies):
    turns = iter(replies)

    class P:
        def complete(self, messages, tools):
            return next(turns)

    cfg = LilBotConfig(workspace=tmp_path)
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    ctx = ToolContext(None, None, _Mem(), _Skills(), None, None, cfg)
    return Agent(cfg, P(), ToolRegistry(), ctx)


def test_session_saved_after_turn(tmp_path):
    agent = _agent(tmp_path, [ProviderTurn(content="hi there")])
    list(agent.run_turn("hello"))
    store = SessionStore(tmp_path / ".lilbot")
    infos = store.list()
    assert len(infos) == 1
    assert infos[0].session_id == agent.session_id
    assert infos[0].preview == "hello"


def test_resume_restores_messages(tmp_path):
    a1 = _agent(tmp_path, [ProviderTurn(content="answer one")])
    list(a1.run_turn("remember X=42"))
    sid = a1.session_id
    saved_len = len(a1.messages)

    # Fresh agent (new session) resumes the old one.
    a2 = _agent(tmp_path, [ProviderTurn(content="ok")])
    msg = a2.resume(sid)
    assert "Resumed session" in msg
    assert a2.session_id == sid
    assert len(a2.messages) == saved_len
    assert any("remember X=42" in str(m.get("content")) for m in a2.messages)


def test_resume_latest_when_no_id(tmp_path):
    a1 = _agent(tmp_path, [ProviderTurn(content="one")])
    list(a1.run_turn("first session msg"))

    a2 = _agent(tmp_path, [ProviderTurn(content="x")])
    msg = a2.resume(None)  # latest
    assert "Resumed session" in msg
    assert any("first session msg" in str(m.get("content")) for m in a2.messages)


def test_resume_missing_is_graceful(tmp_path):
    agent = _agent(tmp_path, [ProviderTurn(content="x")])
    assert "not found" in agent.resume("nope-does-not-exist")


def test_store_list_sorted_newest_first(tmp_path):
    store = SessionStore(tmp_path / ".lilbot")
    store.save("20240101-000000", [{"role": "user", "content": "old"}], {})
    store.save("20250101-000000", [{"role": "user", "content": "new"}], {})
    infos = store.list()
    assert infos[0].session_id == "20250101-000000"
