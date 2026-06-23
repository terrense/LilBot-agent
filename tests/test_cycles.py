"""Tests for M4 — cycle memory archive + recall_archive (port from CodeWhale)."""
from __future__ import annotations

from types import SimpleNamespace

from lilbot.config import LilBotConfig
from lilbot.core.agent import Agent
from lilbot.core.cycles import CycleArchive
from lilbot.core.events import ProviderTurn
from lilbot.tools import ToolContext, ToolRegistry
from lilbot.tools.builtin import _recall_archive


def test_archive_writes_and_lists(tmp_path):
    arc = CycleArchive(tmp_path / ".lilbot")
    p = arc.archive("User wanted a parser refactor. Files: parser.py.", summarized_messages=12, before_tokens=90000)
    assert p is not None
    items = arc.list()
    assert len(items) == 1
    assert "parser refactor" in items[0].briefing
    assert "before_tokens: 90000" in items[0].briefing


def test_archive_search(tmp_path):
    arc = CycleArchive(tmp_path / ".lilbot")
    arc.archive("Discussed database indexing strategy.")
    arc.archive("Refactored the auth module.")
    hits = arc.search("indexing")
    assert len(hits) == 1 and "indexing" in hits[0].briefing


def test_empty_briefing_not_archived(tmp_path):
    arc = CycleArchive(tmp_path / ".lilbot")
    assert arc.archive("   ") is None
    assert arc.list() == []


class _Mem:
    def context(self, *a, **k):
        return "(none)"


class _Skills:
    def list(self, *a, **k):
        return []


def _agent(tmp_path):
    cfg = LilBotConfig(workspace=tmp_path)
    cfg.state_dir.mkdir(parents=True, exist_ok=True)

    class P:
        def complete(self, messages, tools):
            return ProviderTurn(content="STRUCTURED SUMMARY of the earlier work on the indexer.")

    ctx = ToolContext(None, None, _Mem(), _Skills(), None, None, cfg)
    return Agent(cfg, P(), ToolRegistry(), ctx)


def test_compaction_archives_a_cycle_recoverable_via_tool(tmp_path):
    agent = _agent(tmp_path)
    # Build a long conversation so manual compaction actually summarizes.
    filler = "x " * 2000
    agent.messages = [{"role": "system", "content": "sys"}]
    for i in range(8):
        role = "user" if i % 2 == 0 else "assistant"
        agent.messages.append({"role": role, "content": f"msg {i} about the indexer " + filler})
    agent.messages.append({"role": "user", "content": "current"})

    msg = agent.compact(manual=True)
    assert "Compacted context" in msg

    # A cycle file now exists and recall_archive can find it.
    cfg = SimpleNamespace(state_dir=tmp_path / ".lilbot")
    ctx = ToolContext(None, None, None, None, None, None, cfg)
    res = _recall_archive({"query": "indexer"}, ctx)
    assert res.ok
    assert "SUMMARY" in res.output or "indexer" in res.output.lower()


def test_recall_archive_no_archives(tmp_path):
    cfg = SimpleNamespace(state_dir=tmp_path / ".lilbot")
    ctx = ToolContext(None, None, None, None, None, None, cfg)
    res = _recall_archive({"query": "anything"}, ctx)
    assert res.ok
    assert "no archives" in res.output
