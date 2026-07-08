"""Tests for the CC-parity compaction mechanisms replicated into LilBot.

Covers: L0 tool-result budget (frozen/fresh/replaced), API-round grouping +
head-truncation retry, <analysis> stripping, real-token trigger, cache-cold
prune-only path, and the prompt-cache-sharing message summarizer.
"""
from __future__ import annotations

from pathlib import Path

from lilbot.config import LilBotConfig
from lilbot.core.agent import Agent
from lilbot.core.compaction import (
    auto_compact,
    compute_threshold,
    format_compact_summary,
    group_messages_by_round,
    partial_compact,
    truncate_head_for_retry,
)
from lilbot.core.events import ProviderTurn
from lilbot.core.tool_budget import (
    ToolBudgetState,
    enforce_tool_result_budget,
)
from lilbot.tools import ToolContext, ToolRegistry


# ── L0 tool-result budget ────────────────────────────────────────────────

def _tool_msg(tid: str, content: str) -> dict:
    return {"role": "tool", "tool_call_id": tid, "name": "read_file", "content": content}


def test_budget_replaces_largest_fresh_over_budget():
    msgs = [
        {"role": "system", "content": "sys"},
        _tool_msg("t1", "A" * 40_000),
        _tool_msg("t2", "B" * 40_000),
    ]
    state = ToolBudgetState()
    out, replaced = enforce_tool_result_budget(
        msgs, state, budget_chars=50_000, keep_recent=0
    )
    # One of the two 40k results must be shed to get under 50k.
    assert len(replaced) == 1
    replaced_id = replaced[0].tool_call_id
    body = {m.get("tool_call_id"): m["content"] for m in out if m.get("role") == "tool"}
    assert "offloaded to save context" in body[replaced_id]
    # The other stays full and both are now seen (frozen going forward).
    assert state.seen_ids == {"t1", "t2"}


def test_budget_never_touches_frozen():
    # First pass: under budget -> both marked seen (frozen), nothing replaced.
    msgs = [
        {"role": "system", "content": "sys"},
        _tool_msg("t1", "A" * 10_000),
        _tool_msg("t2", "B" * 10_000),
    ]
    state = ToolBudgetState()
    _, replaced = enforce_tool_result_budget(msgs, state, budget_chars=50_000, keep_recent=0)
    assert replaced == []
    assert state.seen_ids == {"t1", "t2"}

    # Second pass: now way over budget, but both are frozen (already sent full).
    # Frozen content is never rewritten, so nothing is replaced.
    big = [
        {"role": "system", "content": "sys"},
        _tool_msg("t1", "A" * 40_000),
        _tool_msg("t2", "B" * 40_000),
    ]
    out, replaced2 = enforce_tool_result_budget(big, state, budget_chars=10_000, keep_recent=0)
    assert replaced2 == []
    assert all(len(m["content"]) == 40_000 for m in out if m.get("role") == "tool")


def test_budget_keep_recent_protects_tail():
    msgs = [{"role": "system", "content": "sys"}]
    for i in range(6):
        msgs.append(_tool_msg(f"t{i}", "X" * 20_000))
    state = ToolBudgetState()
    out, replaced = enforce_tool_result_budget(
        msgs, state, budget_chars=10_000, keep_recent=2
    )
    # The last two tool results are protected and stay full size.
    tool_out = [m for m in out if m.get("role") == "tool"]
    assert len(tool_out[-1]["content"]) == 20_000
    assert len(tool_out[-2]["content"]) == 20_000
    protected_ids = {"t4", "t5"}
    assert all(r.tool_call_id not in protected_ids for r in replaced)


def test_budget_reapply_is_idempotent():
    msgs = [
        {"role": "system", "content": "sys"},
        _tool_msg("t1", "A" * 40_000),
        _tool_msg("t2", "B" * 40_000),
    ]
    state = ToolBudgetState()
    out1, _ = enforce_tool_result_budget(msgs, state, budget_chars=50_000, keep_recent=0)
    # Feed the already-replaced messages back: the preview stays, no new work.
    out2, replaced2 = enforce_tool_result_budget(out1, state, budget_chars=50_000, keep_recent=0)
    assert replaced2 == []
    prev = {m.get("tool_call_id"): m["content"] for m in out1 if m.get("role") == "tool"}
    now = {m.get("tool_call_id"): m["content"] for m in out2 if m.get("role") == "tool"}
    assert prev == now


# ── API-round grouping + head truncation ─────────────────────────────────

def test_group_messages_by_round():
    msgs = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "tool", "tool_call_id": "t", "content": "r"},
        {"role": "assistant", "content": "a2"},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a3"},
    ]
    groups = group_messages_by_round(msgs)
    # Group 0 = preamble (system+user), then one group per new assistant.
    assert [len(g) for g in groups] == [2, 2, 2, 1]
    assert groups[0][0]["role"] == "system"


def test_truncate_head_drops_oldest_and_keeps_system():
    msgs = [{"role": "system", "content": "s"}]
    for i in range(10):
        msgs.append({"role": "user", "content": f"u{i}"})
        msgs.append({"role": "assistant", "content": f"a{i}"})
    out = truncate_head_for_retry(msgs)
    assert out is not None
    assert out[0]["role"] == "system"
    assert out[1]["content"] == "[earlier conversation truncated so the summary request fits]"
    # Fewer messages than we started with.
    assert len(out) < len(msgs)


def test_truncate_head_marker_is_idempotent():
    msgs = [{"role": "system", "content": "s"}]
    for i in range(10):
        msgs.append({"role": "user", "content": f"u{i}"})
        msgs.append({"role": "assistant", "content": f"a{i}"})
    out1 = truncate_head_for_retry(msgs)
    out2 = truncate_head_for_retry(out1)
    assert out2 is not None
    # Only one marker survives (the old one is stripped before regrouping).
    markers = [m for m in out2 if m.get("content", "").startswith("[earlier conversation")]
    assert len(markers) == 1


def test_truncate_head_returns_none_when_single_round():
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    assert truncate_head_for_retry(msgs) is None


# ── analysis stripping ───────────────────────────────────────────────────

def test_format_compact_summary_strips_analysis_and_unwraps():
    raw = "<analysis>scratch thoughts here</analysis>\n<summary>REAL SUMMARY</summary>"
    assert format_compact_summary(raw) == "REAL SUMMARY"


def test_format_compact_summary_tolerates_no_tags():
    assert format_compact_summary("plain summary text") == "plain summary text"


# ── auto_compact: real-token trigger + cache-cold + shared summarizer ─────

def _convo(n_pairs: int, filler: str = "word " * 200) -> list[dict]:
    msgs = [{"role": "system", "content": "sys"}]
    for i in range(n_pairs):
        msgs.append({"role": "user", "content": f"q{i} {filler}"})
        msgs.append({"role": "assistant", "content": f"a{i} {filler}"})
    return msgs


def test_actual_tokens_overrides_estimate_for_trigger():
    # Conversation whose char-estimate is well below threshold, so a normal
    # auto pass does nothing — but large enough to clear the summary floor.
    msgs = _convo(40)
    assert auto_compact(msgs, lambda s, p: "SUM", context_window=200_000, manual=False) is None
    # But if the provider reports a real prompt-token count above threshold,
    # the trigger fires.
    over = compute_threshold(200_000) + 5_000
    out = auto_compact(
        msgs, lambda s, p: "SUM", context_window=200_000, manual=False, actual_tokens=over
    )
    assert out is not None
    assert out.method == "summarize"


def test_cache_cold_prunes_below_threshold_only():
    # Below threshold, but cache is cold and there is prunable old tool output.
    msgs = [{"role": "system", "content": "sys"}]
    for i in range(8):
        msgs.append({"role": "user", "content": f"q{i}"})
        msgs.append({"role": "assistant", "content": None, "tool_calls": [
            {"id": f"c{i}", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}
        ]})
        msgs.append(_tool_msg(f"c{i}", "Z" * 3_000))
    calls = {"n": 0}

    def summarizer(s, p):
        calls["n"] += 1
        return "SUM"

    out = auto_compact(
        msgs, summarizer, context_window=200_000, manual=False, cache_cold=True
    )
    assert out is not None
    assert out.method == "prune"      # cold-cache path prunes...
    assert calls["n"] == 0            # ...and never pays for an LLM summary


def test_cache_cold_noop_when_nothing_to_prune():
    msgs = _convo(3)  # no tool results to prune, below threshold
    out = auto_compact(
        msgs, lambda s, p: "SUM", context_window=200_000, manual=False, cache_cold=True
    )
    assert out is None


def test_message_summarizer_path_shares_prefix_and_strips_analysis():
    msgs = _convo(10)
    seen = {}

    def message_summarizer(system, to_summarize):
        seen["system"] = system
        seen["prefix_len"] = len(to_summarize)
        return "<analysis>draft</analysis><summary>SHARED SUMMARY</summary>"

    out = auto_compact(
        msgs,
        lambda s, p: "unused text path",
        context_window=200_000,
        manual=True,
        message_summarizer=message_summarizer,
    )
    assert out is not None
    # The summarizer received the real system object and prefix messages.
    assert seen["system"] is msgs[0]
    assert seen["prefix_len"] > 0
    # The analysis scratchpad was stripped from the stored summary.
    summary_msg = out.messages[1]["content"]
    assert "SHARED SUMMARY" in summary_msg
    assert "draft" not in summary_msg


# ── L4c partial compact ──────────────────────────────────────────────────

def test_partial_compact_up_to_summarizes_head_keeps_tail():
    msgs = _convo(8)  # system + 16 messages
    pivot = 9  # summarize messages[1:9], keep messages[9:]
    out = partial_compact(msgs, lambda s, p: "<summary>HEAD</summary>", pivot, direction="up_to")
    assert out is not None
    assert out.messages[0] is msgs[0]                 # system preserved
    assert out.messages[1]["role"] == "system"        # summary at head
    assert "HEAD" in out.messages[1]["content"]
    assert out.messages[2:] == msgs[9:]               # tail kept verbatim


def test_partial_compact_from_keeps_head_summarizes_tail():
    msgs = _convo(8)
    pivot = 9  # keep messages[1:9], summarize messages[9:]
    out = partial_compact(msgs, lambda s, p: "<summary>TAIL</summary>", pivot, direction="from")
    assert out is not None
    assert out.messages[0] is msgs[0]
    assert out.messages[1:9] == msgs[1:9]             # head kept verbatim
    assert out.messages[-1]["role"] == "system"       # summary at tail
    assert "TAIL" in out.messages[-1]["content"]


def test_partial_compact_rejects_out_of_range_pivot():
    msgs = _convo(3)
    assert partial_compact(msgs, lambda s, p: "x", 0) is None
    assert partial_compact(msgs, lambda s, p: "x", len(msgs)) is None


# ── Agent-level integration ──────────────────────────────────────────────

class _StreamProvider:
    """Capable provider (has complete_stream) so shared summary path is used."""

    def __init__(self):
        self.summary_calls: list[list[dict]] = []

    def complete(self, messages, tools):
        system = str(messages[0].get("content", ""))
        # Shared summarizer sends the agent's own system prompt + trailing
        # SUMMARY instruction; detect that trailing instruction.
        last = str(messages[-1].get("content", ""))
        if "wrap the summary itself in <summary>" in last or "<summary>" in last:
            self.summary_calls.append(messages)
            return ProviderTurn(content="<summary>SUM</summary>", usage={"prompt_tokens": 50})
        return ProviderTurn(content="ok", usage={"prompt_tokens": 100})

    def complete_stream(self, messages, tools):
        from lilbot.core.events import StreamEvent
        yield StreamEvent(final=self.complete(messages, tools))


class _EmptyMemory:
    def context(self) -> str:
        return "(none)"


class _EmptySkills:
    def list(self) -> list:
        return []


def _agent(tmp_path: Path, provider) -> Agent:
    ctx = ToolContext(
        sandbox=None, permissions=None, memory=_EmptyMemory(), skills=_EmptySkills(),
        subagents=None, mcp=None, config=None,
    )
    cfg = LilBotConfig(workspace=tmp_path, context_window=40_000)
    return Agent(cfg, provider, ToolRegistry(), ctx)


def test_agent_message_summarizer_reuses_system_prefix(tmp_path):
    provider = _StreamProvider()
    agent = _agent(tmp_path, provider)
    # Build a prefix and call the shared summarizer directly.
    system = agent.messages[0]
    prefix = [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}]
    text = agent._message_summarizer(system, prefix)
    assert "SUM" in text
    sent = provider.summary_calls[-1]
    assert sent[0] is system              # same system object -> cache prefix reused
    assert sent[1:3] == prefix            # prefix objects passed through verbatim


def test_agent_cache_cold_detection(tmp_path):
    import time as _time
    provider = _StreamProvider()
    agent = _agent(tmp_path, provider)
    assert agent._cache_is_cold() is False        # no activity yet
    agent._last_activity_ts = _time.time()
    assert agent._cache_is_cold() is False        # just now
    agent._last_activity_ts = _time.time() - 10_000
    assert agent._cache_is_cold() is True         # long idle -> cold


def test_agent_post_compact_cleanup_clears_stale_token_count(tmp_path):
    provider = _StreamProvider()
    agent = _agent(tmp_path, provider)
    agent._last_input_tokens = 99_999
    agent._pending_diagnostics = "stale"
    agent._post_compact_cleanup("summarize")
    assert agent._last_input_tokens == 0     # stale after prefix rewrite
    assert agent._pending_diagnostics == ""
    # Prune keeps the token count (no prefix rewrite).
    agent._last_input_tokens = 12_345
    agent._post_compact_cleanup("prune")
    assert agent._last_input_tokens == 12_345
