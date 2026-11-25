"""Session memory as the first compaction path.

The living document is maintained incrementally as the session runs, so a
compaction can use it instead of paying a model call to re-summarize the
prefix. These tests pin the three properties that make that safe:

  * the document is used *instead of* the summary model, not in addition to it;
  * it is refused once the conversation has moved too far past the last write
    (a stale handoff is worse than a fresh one);
  * the document file survives a crash mid-write intact.
"""
from __future__ import annotations

import json
import os

from lilbot.config import LilBotConfig
from lilbot.core.agent import Agent
from lilbot.core.compaction import (
    RecoveryState,
    SessionMemoryState,
    auto_compact,
)
from lilbot.core.events import ProviderTurn, StreamEvent
from lilbot.core.session_memory import (
    MIN_SESSION_MEMORY_SECTIONS,
    SESSION_MEMORY_TOKEN_THRESHOLD,
    SESSION_MEMORY_TOOL_CALL_THRESHOLD,
    SessionMemory,
)
from lilbot.tools import ToolContext, ToolRegistry

# ── document helpers ──────────────────────────────────────────────────────

def _populated_document(sm: SessionMemory) -> str:
    """Write a document with enough real sections to be a handoff."""
    sm.merge_updates({
        "Session Title": "Building the parser",
        "Current State": "The tokenizer is wired up; the grammar is next.",
        "Task Specification": "Parse the config grammar into an AST.",
        "Files and Functions": "src/parser.py holds parse_config().",
        "Errors and Corrections": "The old lexer dropped comments.",
        "Learnings": "Grammar rules must be ordered longest-first.",
        "Worklog": "1. wrote the lexer  2. wired the tokenizer",
    })
    return sm.text()


def _convo(n_pairs: int, filler: str = "word " * 400) -> list[dict]:
    msgs = [{"role": "system", "content": "sys"}]
    for i in range(n_pairs):
        msgs.append({"role": "user", "content": f"q{i} {filler}"})
        msgs.append({"role": "assistant", "content": f"a{i} {filler}"})
    return msgs


def _state(document: str, *, tokens: int = 0, calls: int = 0,
           sections: int = MIN_SESSION_MEMORY_SECTIONS) -> SessionMemoryState:
    return SessionMemoryState(
        document=document,
        populated_sections=sections,
        covered_tokens=tokens,
        covered_tool_calls=calls,
    )


# ── the layer itself ──────────────────────────────────────────────────────

def test_session_memory_replaces_the_summary_model():
    calls = {"n": 0}

    def counting_summarizer(_s, _p):
        calls["n"] += 1
        return "SUMMARY"

    msgs = _convo(30)
    out = auto_compact(
        msgs,
        counting_summarizer,
        context_window=20_000,
        manual=True,
        session_memory=_state("## Current State\nthe tokenizer is wired up"),
    )
    assert out is not None
    assert out.method == "session_memory"
    # The whole point: no summary model call at all.
    assert calls["n"] == 0
    assert "the tokenizer is wired up" in out.messages[1]["content"]
    assert out.messages[0]["content"] == "sys"
    # The recent tail is preserved verbatim.
    assert out.messages[-1]["content"] == msgs[-1]["content"]


def test_session_memory_is_refused_when_the_document_is_stale():
    calls = {"n": 0}

    def counting_summarizer(_s, _p):
        calls["n"] += 1
        return "SUMMARY"

    msgs = _convo(30)
    out = auto_compact(
        msgs,
        counting_summarizer,
        context_window=20_000,
        manual=True,
        session_memory=_state(
            "## Current State\nold notes",
            tokens=SESSION_MEMORY_TOKEN_THRESHOLD + 1,
        ),
    )
    assert out is not None
    assert out.method == "summarize"
    assert calls["n"] == 1
    assert "old notes" not in out.messages[1]["content"]


def test_session_memory_is_refused_past_the_tool_call_threshold():
    out = auto_compact(
        _convo(30),
        lambda s, p: "SUMMARY",
        context_window=20_000,
        manual=True,
        session_memory=_state(
            "## Current State\nold notes",
            calls=SESSION_MEMORY_TOOL_CALL_THRESHOLD + 1,
        ),
    )
    assert out is not None
    assert out.method == "summarize"


def test_a_thin_document_is_refused():
    """One populated section is not a handoff."""
    out = auto_compact(
        _convo(30),
        lambda s, p: "SUMMARY",
        context_window=20_000,
        manual=True,
        session_memory=_state("## Session Title\njust a title", sections=1),
    )
    assert out is not None
    assert out.method == "summarize"


def test_an_empty_document_is_refused():
    out = auto_compact(
        _convo(30),
        lambda s, p: "SUMMARY",
        context_window=20_000,
        manual=True,
        session_memory=_state(""),
    )
    assert out is not None
    assert out.method == "summarize"


def test_session_memory_path_falls_back_when_it_would_not_reclaim():
    """Notes longer than the prefix they replace must not be used."""
    msgs = _convo(30)
    calls = {"n": 0}

    def counting_summarizer(_s, _p):
        calls["n"] += 1
        return "SUMMARY"

    huge_notes = "x" * 200_000
    out = auto_compact(
        msgs,
        counting_summarizer,
        context_window=20_000,
        manual=True,
        session_memory=_state(huge_notes),
    )
    assert out is not None
    # It fell through to the summary instead of making the context bigger.
    assert out.method == "summarize"
    assert calls["n"] == 1


def test_session_memory_does_not_double_count_the_note():
    """The document is the handoff; the recovery attachment must not repeat it."""
    rec = RecoveryState()
    rec.record_note("Session memory (running notes)", "## Current State\nrunning notes body")
    rec.record_file_read("src/app.py", "print('hello')\n")
    out = auto_compact(
        _convo(30),
        lambda s, p: "SUMMARY",
        context_window=20_000,
        manual=True,
        recovery=rec,
        tool_names=["read_file"],
        session_memory=_state("## Current State\nthe document handoff"),
    )
    assert out is not None
    assert out.method == "session_memory"
    body = out.messages[1]["content"]
    assert body.count("the document handoff") == 1
    # The other recovery material still rides along.
    assert "src/app.py" in body
    assert "print('hello')" in body


def test_summary_path_still_carries_the_document_as_a_note():
    """When the document is refused, it still rides along as a recovery note."""
    rec = RecoveryState()
    rec.record_note("Session memory (running notes)", "## Current State\nrunning notes body")
    out = auto_compact(
        _convo(30),
        lambda s, p: "SUMMARY",
        context_window=20_000,
        manual=True,
        recovery=rec,
        session_memory=_state("## Current State\nold notes", tokens=SESSION_MEMORY_TOKEN_THRESHOLD + 1),
    )
    assert out is not None
    assert out.method == "summarize"
    assert "running notes body" in out.messages[1]["content"]


def test_auto_compaction_below_threshold_still_does_nothing():
    """The session-memory path must not turn compaction into a free rewrite."""
    out = auto_compact(
        _convo(3),
        lambda s, p: "SUMMARY",
        context_window=200_000,
        manual=False,
        session_memory=_state("## Current State\nnotes"),
    )
    assert out is None


def test_manual_compaction_needs_a_prefix_worth_replacing():
    """A tiny prefix is not worth a rewrite, whatever the document says."""
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
    out = auto_compact(
        msgs,
        lambda s, p: "SUMMARY",
        context_window=200_000,
        manual=True,
        session_memory=_state("## Current State\nnotes"),
    )
    assert out is None


# ── the state object ──────────────────────────────────────────────────────

def test_state_is_unusable_when_nothing_was_written():
    assert _state("").is_usable() is False
    assert _state("notes", sections=0).is_usable() is False
    assert _state("notes", sections=MIN_SESSION_MEMORY_SECTIONS).is_usable() is True


def test_state_coverage_thresholds_are_inclusive():
    """At the threshold the document is still usable; past it, it is not."""
    assert _state("n", tokens=SESSION_MEMORY_TOKEN_THRESHOLD).is_usable() is True
    assert _state("n", tokens=SESSION_MEMORY_TOKEN_THRESHOLD + 1).is_usable() is False
    assert _state("n", calls=SESSION_MEMORY_TOOL_CALL_THRESHOLD).is_usable() is True
    assert _state("n", calls=SESSION_MEMORY_TOOL_CALL_THRESHOLD + 1).is_usable() is False


# ── the document file ─────────────────────────────────────────────────────

def test_document_is_written_atomically(tmp_path):
    """A crash mid-write must leave the previous document intact."""
    sm = SessionMemory(tmp_path)
    sm.merge_updates({"Current State": "first version"})
    assert sm.path is not None
    original = sm.path.read_text(encoding="utf-8")

    # Simulate a crash during the write: the temp file is written, then the
    # process dies before the rename.
    def crash_before_replace(*_args, **_kwargs):
        raise OSError("simulated crash")

    real_replace = os.replace

    def fake_replace(src, dst, *args, **kwargs):
        raise OSError("simulated crash")

    os.replace = fake_replace
    try:
        sm.merge_updates({"Current State": "second version"})
    finally:
        os.replace = real_replace

    # The document on disk is still the complete first version.
    assert sm.path.read_text(encoding="utf-8") == original
    assert sm.load()["Current State"] == "first version"
    # And no partial file was left behind as the document.
    assert not sm.path.with_name(sm.path.name + ".tmp").exists()


def test_document_write_replaces_in_one_step(tmp_path):
    """The write is a rename over the old file, not an in-place truncation."""
    sm = SessionMemory(tmp_path)
    sm.merge_updates({"Current State": "v1"})
    first = sm.path.read_text(encoding="utf-8")
    sm.merge_updates({"Current State": "v2"})
    second = sm.path.read_text(encoding="utf-8")
    assert first != second
    assert "v2" in second
    assert "v1" not in second


def test_populated_sections_counts_only_real_content(tmp_path):
    sm = SessionMemory(tmp_path)
    assert sm.populated_sections() == 0
    assert sm.is_handoff_ready() is False
    sm.merge_updates({"Current State": "real", "Worklog": "also real"})
    assert sm.populated_sections() == 2
    assert sm.is_handoff_ready() is False
    sm.merge_updates({"Task Specification": "and more"})
    assert sm.populated_sections() == 3
    assert sm.is_handoff_ready() is True


def test_handoff_message_is_bounded():
    from lilbot.core.compaction import session_memory_compaction_message

    state = _state("y" * 100_000)
    msg = session_memory_compaction_message(state)
    assert len(msg) < 100_000
    assert "truncated" in msg


def test_handoff_text_omits_unfilled_sections(tmp_path):
    """Placeholders are instructions to the writer, not content."""
    sm = SessionMemory(tmp_path)
    sm.merge_updates({
        "Current State": "the tokenizer is wired up",
        "Task Specification": "parse the grammar",
        "Worklog": "1. read  2. changed",
    })
    handoff = sm.handoff_text()
    assert "the tokenizer is wired up" in handoff
    assert "parse the grammar" in handoff
    assert "1. read  2. changed" in handoff
    # Unfilled sections carry no placeholder text.
    assert "Errors and Corrections" not in handoff
    assert "Learnings" not in handoff
    # The template text itself never reaches the handoff.
    assert "What is being worked on right now" not in handoff
    assert "_" not in handoff.split("## Current State")[0]


def test_handoff_text_is_empty_when_nothing_is_written(tmp_path):
    sm = SessionMemory(tmp_path)
    assert sm.handoff_text() == ""


# ── agent wiring ──────────────────────────────────────────────────────────

class _Provider:
    """Answers the session-notes side-query with a full document."""

    def __init__(self):
        self.summary_calls = {"n": 0}

    def complete(self, messages, tools):
        system = str(messages[0].get("content", ""))
        if "session-notes document" in system:
            return ProviderTurn(content=json.dumps({
                "Session Title": "Session work",
                "Current State": "halfway through the change",
                "Task Specification": "ship the feature",
                "Files and Functions": "core/agent.py runs the loop",
                "Errors and Corrections": "the first attempt broke the cache",
                "Learnings": "measure before optimizing",
                "Worklog": "1. read  2. changed  3. tested",
            }))
        self.summary_calls["n"] += 1
        return ProviderTurn(content="<summary>SUMMARY</summary>", usage={"prompt_tokens": 50})

    def complete_stream(self, messages, tools):
        yield StreamEvent(final=self.complete(messages, tools))


class _EmptyMemory:
    def context(self) -> str:
        return "(none)"

    def list(self) -> list:
        return []


def _agent(tmp_path, provider) -> Agent:
    ctx = ToolContext(
        sandbox=None, permissions=None, memory=_EmptyMemory(), skills=_EmptyMemory(),
        subagents=None, mcp=None, config=None,
    )
    cfg = LilBotConfig(workspace=tmp_path, context_window=40_000)
    return Agent(cfg, provider, ToolRegistry(), ctx)


def test_agent_uses_the_document_and_skips_the_summary_model(tmp_path):
    provider = _Provider()
    agent = _agent(tmp_path, provider)

    # The document is written by this session (background refresh).
    agent._turn_count = 4
    agent.messages.append({"role": "user", "content": "do the thing"})
    agent.messages.append({"role": "assistant", "content": "ok"})
    agent._maybe_update_session_memory()
    assert agent._sm_thread is not None
    agent._sm_thread.join(timeout=5)
    assert agent._sm_written_here is True
    assert agent.session_memory.populated_sections() >= MIN_SESSION_MEMORY_SECTIONS

    # A conversation big enough to compact.
    agent.messages = _convo(30)
    provider.summary_calls["n"] = 0
    agent.compact(manual=True)
    assert provider.summary_calls["n"] == 0
    assert "halfway through the change" in agent.messages[1]["content"]


def test_agent_refuses_a_document_from_another_session(tmp_path):
    """A document on disk that this session never wrote is not a handoff."""
    provider = _Provider()
    agent = _agent(tmp_path, provider)

    # Write a document directly to disk, bypassing the agent's refresh path.
    stale = SessionMemory(tmp_path)
    stale.merge_updates({
        "Session Title": "Some other session",
        "Current State": "notes from a previous session",
        "Task Specification": "something else entirely",
        "Files and Functions": "other.py",
        "Errors and Corrections": "other errors",
        "Learnings": "other learnings",
        "Worklog": "other worklog",
    })
    assert agent._sm_written_here is False

    provider.summary_calls["n"] = 0
    agent.messages = _convo(30)
    agent.compact(manual=True)
    # It fell back to the summary model rather than trusting the stale notes.
    assert provider.summary_calls["n"] == 1
    assert "notes from a previous session" not in agent.messages[1]["content"]


def test_refresh_is_due_on_the_turn_interval(tmp_path):
    agent = _agent(tmp_path, _Provider())
    agent._turn_count = 4
    assert agent._session_memory_refresh_due() is True
    agent._turn_count = 5
    assert agent._session_memory_refresh_due() is False


def test_refresh_is_due_on_the_token_threshold(tmp_path):
    agent = _agent(tmp_path, _Provider())
    agent._turn_count = 1  # not on the interval
    agent._sm_tokens_total = SESSION_MEMORY_TOKEN_THRESHOLD
    agent._sm_tokens_at_update = 0
    assert agent._session_memory_refresh_due() is True
    agent._sm_tokens_at_update = agent._sm_tokens_total
    assert agent._session_memory_refresh_due() is False


def test_refresh_is_due_on_the_tool_call_threshold(tmp_path):
    agent = _agent(tmp_path, _Provider())
    agent._turn_count = 1
    agent._sm_tool_calls_total = SESSION_MEMORY_TOOL_CALL_THRESHOLD
    agent._sm_tool_calls_at_update = 0
    assert agent._session_memory_refresh_due() is True
    agent._sm_tool_calls_at_update = agent._sm_tool_calls_total
    assert agent._session_memory_refresh_due() is False


def test_coverage_grows_with_the_conversation(tmp_path):
    agent = _agent(tmp_path, _Provider())
    _populated_document(agent.session_memory)
    agent._sm_written_here = True
    agent._sm_tokens_at_update = 100
    agent._sm_tokens_total = 100 + SESSION_MEMORY_TOKEN_THRESHOLD
    state = agent._session_memory_state()
    assert state.covered_tokens == SESSION_MEMORY_TOKEN_THRESHOLD
    assert state.is_usable() is True
    agent._sm_tokens_total += 1
    assert agent._session_memory_state().is_usable() is False


def test_coverage_grows_with_tool_calls(tmp_path):
    """Tool calls are state changes the document should have learned about."""
    agent = _agent(tmp_path, _Provider())
    _populated_document(agent.session_memory)
    agent._sm_written_here = True
    agent._sm_tool_calls_at_update = 10
    agent._sm_tool_calls_total = 10 + SESSION_MEMORY_TOOL_CALL_THRESHOLD
    state = agent._session_memory_state()
    assert state.covered_tool_calls == SESSION_MEMORY_TOOL_CALL_THRESHOLD
    assert state.is_usable() is True
    agent._sm_tool_calls_total += 1
    assert agent._session_memory_state().is_usable() is False


def test_appended_messages_count_toward_coverage(tmp_path):
    """The agent's own appends are what make the document stale."""
    agent = _agent(tmp_path, _Provider())
    _populated_document(agent.session_memory)
    agent._sm_written_here = True
    agent._sm_tokens_at_update = agent._sm_tokens_total
    agent._append({"role": "user", "content": "x" * 40_000})
    assert agent._sm_tokens_total > 0
    assert agent._session_memory_state().covered_tokens > 0


def test_reset_invalidates_the_document(tmp_path):
    agent = _agent(tmp_path, _Provider())
    agent._sm_written_here = True
    agent.reset_conversation()
    assert agent._sm_written_here is False
    assert agent._session_memory_state().is_usable() is False
