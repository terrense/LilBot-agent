"""Tests for the session-memory living document (#12)."""
from __future__ import annotations

import json
from pathlib import Path

from lilbot.config import LilBotConfig
from lilbot.core.agent import Agent
from lilbot.core.compaction import RecoveryState
from lilbot.core.events import ProviderTurn, StreamEvent
from lilbot.core.session_memory import SECTIONS, SessionMemory
from lilbot.tools import ToolContext, ToolRegistry


def test_empty_until_content_written(tmp_path):
    sm = SessionMemory(tmp_path)
    assert sm.is_empty() is True
    assert sm.text() == ""


def test_merge_updates_only_touches_given_sections(tmp_path):
    sm = SessionMemory(tmp_path)
    merged = sm.merge_updates({"Current State": "Building the parser.", "Worklog": "1. scaffolded"})
    assert merged["Current State"] == "Building the parser."
    assert merged["Worklog"] == "1. scaffolded"
    # Untouched sections keep their placeholders.
    assert merged["Learnings"].startswith("_")
    assert sm.is_empty() is False
    # A second update preserves the earlier one and adds a new section.
    merged2 = sm.merge_updates({"Learnings": "Regex is enough here."})
    assert merged2["Current State"] == "Building the parser."   # preserved
    assert merged2["Learnings"] == "Regex is enough here."


def test_unknown_sections_ignored(tmp_path):
    sm = SessionMemory(tmp_path)
    merged = sm.merge_updates({"Bogus Section": "junk", "Worklog": "did a thing"})
    assert "Bogus Section" not in merged
    assert merged["Worklog"] == "did a thing"


def test_roundtrip_render_parse(tmp_path):
    sm = SessionMemory(tmp_path)
    sm.merge_updates({"Session Title": "Parser work", "Current State": "mid-refactor"})
    reloaded = SessionMemory(tmp_path).load()
    assert reloaded["Session Title"] == "Parser work"
    assert reloaded["Current State"] == "mid-refactor"
    # Every canonical section is present after a roundtrip.
    assert set(reloaded) == {name for name, _ in SECTIONS}


def test_recovery_note_survives_compaction_attachment():
    rec = RecoveryState()
    rec.record_note("Session memory (running notes)", "## Current State\nbuilding X")
    attachment = rec.build_attachment(tool_names=None)
    assert "Session memory (running notes)" in attachment
    assert "building X" in attachment


class _Provider:
    """Returns section-update JSON for the session-memory side-query, else final text."""
    def __init__(self):
        self.updated = False

    def complete(self, messages, tools):
        system = str(messages[0].get("content", ""))
        if "session-notes document" in system:
            self.updated = True
            return ProviderTurn(content=json.dumps({"Current State": "wired the loop"}))
        return ProviderTurn(content="done", usage={"prompt_tokens": 10})

    def complete_stream(self, messages, tools):
        yield StreamEvent(final=self.complete(messages, tools))


def test_agent_updates_session_memory_in_background(tmp_path):
    provider = _Provider()
    cfg = LilBotConfig(workspace=tmp_path)

    class _Mem:
        def context(self): return "(none)"
        def list(self): return []

    ctx = ToolContext(sandbox=None, permissions=None, memory=_Mem(), skills=_Mem(),
                      subagents=None, mcp=None, config=cfg)
    agent = Agent(cfg, provider, ToolRegistry(), ctx)
    agent._turn_count = 4  # hits SESSION_MEMORY_UPDATE_INTERVAL
    agent.messages.append({"role": "user", "content": "wire the loop"})
    agent.messages.append({"role": "assistant", "content": "done"})

    agent._maybe_update_session_memory()
    assert agent._sm_thread is not None
    agent._sm_thread.join(timeout=5)
    assert agent.session_memory.load()["Current State"] == "wired the loop"
