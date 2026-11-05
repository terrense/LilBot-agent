from __future__ import annotations

import json

import pytest

from lilbot.config import LilBotConfig
from lilbot.core.agent import Agent
from lilbot.core.events import ProviderTurn, ToolCall
from lilbot.core.session import SessionStore
from lilbot.core.session_db import SessionDB
from lilbot.tools import ToolContext, ToolDef, ToolRegistry, ToolResult


class _Mem:
    def context(self, *args, **kwargs):
        return "(none)"


class _Skills:
    def list(self, *args, **kwargs):
        return []


def _agent(tmp_path, provider, registry=None):
    cfg = LilBotConfig(workspace=tmp_path)
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    ctx = ToolContext(None, None, _Mem(), _Skills(), None, None, cfg)
    return Agent(cfg, provider, registry or ToolRegistry(), ctx)


def test_session_db_commits_messages_in_order_and_uses_wal(tmp_path):
    db = SessionDB(tmp_path / "sessions.sqlite3")
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]
    for message in messages:
        db.append_message("s1", message, {"total_tokens": 7}, {"turns": 1})

    saved = db.load("s1")
    assert saved is not None
    assert saved["messages"] == messages
    assert saved["usage"] == {"total_tokens": 7}
    assert saved["meta"] == {"turns": 1}
    assert db.journal_mode() == "wal"
    assert db.synchronous() == 1  # SQLite NORMAL
    db.close()


def test_legacy_json_load_migrates_once_and_sqlite_wins(tmp_path):
    state_dir = tmp_path / ".lilbot"
    store = SessionStore(state_dir)
    store.dir.mkdir(parents=True)
    legacy = {
        "session_id": "legacy-session",
        "updated_at": 123.0,
        "messages": [{"role": "user", "content": "from json"}],
        "usage": {"total_tokens": 9},
        "meta": {"turns": 1},
    }
    store._path("legacy-session").write_text(json.dumps(legacy), encoding="utf-8")

    loaded = store.load("legacy-session")
    assert loaded is not None
    assert loaded["messages"] == legacy["messages"]
    assert store.db.load("legacy-session")["messages"] == legacy["messages"]

    stale_json = {**legacy, "messages": [{"role": "user", "content": "stale"}]}
    store._path("legacy-session").write_text(json.dumps(stale_json), encoding="utf-8")
    assert store.load("legacy-session")["messages"] == legacy["messages"]
    assert store.latest_id() == "legacy-session"
    store.close()


def test_agent_resume_reads_legacy_json_session(tmp_path):
    state_dir = tmp_path / ".lilbot"
    json_dir = state_dir / "sessions"
    json_dir.mkdir(parents=True)
    legacy = {
        "session_id": "legacy-resume",
        "updated_at": 123.0,
        "messages": [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "old session"},
            {"role": "assistant", "content": "old answer"},
        ],
        "usage": {"total_tokens": 5},
        "meta": {"turns": 1},
    }
    (json_dir / "legacy-resume.json").write_text(json.dumps(legacy), encoding="utf-8")

    class _Provider:
        def complete(self, messages, tools):
            return ProviderTurn(content="unused")

    agent = _agent(tmp_path, _Provider())
    assert "Resumed session" in agent.resume("legacy-resume")
    assert agent.session_id == "legacy-resume"
    assert agent.messages == legacy["messages"]
    assert agent.usage == legacy["usage"]
    assert agent.sessions.db.load("legacy-resume")["messages"] == legacy["messages"]
    agent.close()


def test_save_keeps_json_export_and_database_snapshot(tmp_path):
    store = SessionStore(tmp_path / ".lilbot")
    messages = [{"role": "user", "content": "export me"}]
    store.save("saved", messages, {"total_tokens": 3}, {"turns": 1})

    assert json.loads(store._path("saved").read_text(encoding="utf-8"))["messages"] == messages
    assert store.db.load("saved")["messages"] == messages
    store.close()


def test_resume_closes_open_tool_calls_as_unknown(tmp_path):
    store = SessionStore(tmp_path / ".lilbot")
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "do work"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "call_done", "type": "function", "function": {"name": "write", "arguments": "{}"}},
                {"id": "call_unknown", "type": "function", "function": {"name": "delete", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "call_done", "name": "write", "content": "written"},
    ]
    store.save("interrupted", messages, {}, {"turns": 1})
    store.close()

    class _Provider:
        def complete(self, messages, tools):
            return ProviderTurn(content="resumed")

    agent = _agent(tmp_path, _Provider())
    response = agent.resume("interrupted")
    assert "unknown" in response
    assert [m["tool_call_id"] for m in agent.messages if m.get("role") == "tool"] == [
        "call_done", "call_unknown",
    ]
    unknown = next(m for m in agent.messages if m.get("tool_call_id") == "call_unknown")
    assert "execution result" in unknown["content"]
    assert "Do not blindly replay" in agent.messages[-1]["content"]
    persisted = agent.sessions.load("interrupted")
    assert persisted["messages"] == agent.messages
    agent.close()


def test_crash_after_third_tool_keeps_each_result_without_turn_export(tmp_path):
    calls = [ToolCall("do_work", {"n": n}, call_id=f"call_{n}") for n in range(1, 5)]

    class _Provider:
        def complete(self, messages, tools):
            return ProviderTurn(tool_calls=calls)

    executed: list[int] = []

    def do_work(args, ctx):
        n = int(args["n"])
        if n == 4:
            raise KeyboardInterrupt("simulated process interruption")
        executed.append(n)
        return ToolResult(True, f"completed {n}")

    registry = ToolRegistry()
    registry.register(ToolDef("do_work", "A tool with a side effect", {"type": "object"}, do_work))
    agent = _agent(tmp_path, _Provider(), registry)
    # Simulate abrupt process loss before the ordinary exception/turn-end JSON
    # export. The SQLite append log must already contain completed observations.
    agent._persist_after_failure = lambda exc: None  # type: ignore[method-assign]

    with pytest.raises(KeyboardInterrupt):
        list(agent.run_turn("run four operations"))

    assert executed == [1, 2, 3]
    sid = agent.session_id
    store = SessionStore(tmp_path / ".lilbot")
    snapshot = store.load(sid)
    assert snapshot is not None
    assert snapshot["messages"][0]["role"] == "system"
    actual_results = [m for m in snapshot["messages"] if m.get("role") == "tool"]
    assert [m["content"] for m in actual_results] == ["completed 1", "completed 2", "completed 3"]
    assert not store._path(sid).exists()  # recovered from SQLite, not JSON export

    class _ResumeProvider:
        def complete(self, messages, tools):
            return ProviderTurn(content="ready")

    resumed = _agent(tmp_path, _ResumeProvider())
    message = resumed.resume(sid)
    assert "marked 1 interrupted tool result" in message
    assert len([m for m in resumed.messages if m.get("role") == "tool"]) == 4
    assert resumed.messages[-1]["role"] == "user"
    assert "unknown execution status" in resumed.messages[-1]["content"]
    # Resume repairs the protocol history; it does not execute any tool.
    assert executed == [1, 2, 3]
    resumed.close()
    store.close()
    agent.close()
