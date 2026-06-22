"""Tests for two-layer compaction + RecoveryState (ported from mewcode)."""
from __future__ import annotations

from lilbot.core.compaction import (
    CompactCircuitBreaker,
    RecoveryState,
    auto_compact,
    compute_keep_start,
    compute_threshold,
    estimate_tokens,
)


def _msgs(n_prefix_chars: int):
    filler = "x " * (n_prefix_chars // 2)
    return [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "old request " + filler},
        {"role": "assistant", "content": "old reply " + filler},
        {"role": "user", "content": "recent 1"},
        {"role": "assistant", "content": "recent 2"},
        {"role": "user", "content": "current"},
    ]


def _summarizer(_sys, _text):
    return "STRUCTURED SUMMARY of the earlier conversation."


def test_no_compaction_below_threshold():
    msgs = _msgs(100)
    out = auto_compact(msgs, _summarizer, context_window=128_000, manual=False)
    assert out is None


def test_manual_compaction_triggers_and_summarizes():
    msgs = _msgs(20_000)  # large prefix
    out = auto_compact(msgs, _summarizer, context_window=128_000, manual=True)
    assert out is not None
    assert out.messages[0]["content"] == "sys"  # system preserved
    assert out.messages[1]["role"] == "system"
    assert "STRUCTURED SUMMARY" in out.messages[1]["content"]
    assert out.after_tokens < out.before_tokens
    # recent tail preserved verbatim
    assert out.messages[-1]["content"] == "current"


def test_recovery_attachment_reattaches_files_and_tools():
    rec = RecoveryState()
    rec.record_file_read("src/app.py", "print('hello')\n")
    rec.record_skill("review", "Step 1: read the diff.")
    msgs = _msgs(20_000)
    out = auto_compact(
        msgs, _summarizer, context_window=128_000, manual=True,
        recovery=rec, tool_names=["read_file", "bash"],
    )
    assert out is not None
    body = out.messages[1]["content"]
    assert "src/app.py" in body
    assert "print('hello')" in body
    assert "review" in body
    assert "read_file" in body


def test_token_trigger_fires_when_over_threshold():
    threshold = compute_threshold(20_000)
    # Many medium messages: older ones fall outside the keep window and must be
    # summarized, so the token trigger fires (unlike a single message that fits
    # entirely within the recent-tail budget).
    chunk = "y " * 1_500  # ~750 tokens each
    msgs = [{"role": "system", "content": "sys"}]
    for i in range(12):
        role = "user" if i % 2 == 0 else "assistant"
        msgs.append({"role": role, "content": f"msg {i} " + chunk})
    assert estimate_tokens(msgs) >= threshold
    out = auto_compact(msgs, _summarizer, context_window=20_000, manual=False)
    assert out is not None
    assert out.summarized > 0 and out.kept > 0


def test_keep_start_never_orphans_tool_message():
    msgs = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "t", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "r"},
        {"role": "assistant", "content": "done"},
    ]
    keep = compute_keep_start(msgs)
    # If the tool result is kept, the assistant tool_calls before it is too.
    if keep <= 2:
        assert msgs[keep].get("role") != "tool"


def test_circuit_breaker_blocks_auto_after_failures():
    breaker = CompactCircuitBreaker()
    for _ in range(3):
        breaker.record_failure()
    assert breaker.is_open()
    msgs = _msgs(20_000)
    # Even over budget, an open breaker suppresses auto compaction.
    out = auto_compact(
        msgs, _summarizer, context_window=1, manual=False, breaker=breaker,
    )
    assert out is None


def test_failed_summarizer_records_breaker_failure():
    def boom(_s, _t):
        raise RuntimeError("llm down")

    breaker = CompactCircuitBreaker()
    msgs = _msgs(20_000)
    out = auto_compact(msgs, boom, context_window=128_000, manual=True, breaker=breaker)
    assert out is None
    assert breaker.consecutive_failures == 1
