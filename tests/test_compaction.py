"""Tests for two-layer compaction + RecoveryState."""
from __future__ import annotations

from lilbot.core.compaction import (
    PRUNED_TOOL_RESULT_PLACEHOLDER,
    CompactCircuitBreaker,
    RecoveryState,
    auto_compact,
    compute_keep_start,
    compute_threshold,
    estimate_tokens,
    is_context_overflow_error,
    prune_tool_results,
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
    # Retries are exhausted first, then exactly one breaker failure is recorded.
    assert breaker.consecutive_failures == 1


# --- new: local prune (microcompact) ----------------------------------------

def _msgs_with_big_tool_result(tool_chars: int):
    filler = "z" * tool_chars
    return [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "please read the file"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": filler},  # huge, stale
        {"role": "assistant", "content": "done reading"},
        {"role": "user", "content": "recent 1"},
        {"role": "assistant", "content": "recent 2"},
        {"role": "user", "content": "current question"},
    ]


def test_prune_only_avoids_llm_when_enough():
    calls = {"n": 0}

    def counting_summarizer(_s, _t):
        calls["n"] += 1
        return "SUMMARY"

    # Big tool result pushes us over a small window; pruning it alone clears it.
    msgs = _msgs_with_big_tool_result(80_000)  # ~20k tokens of tool output
    out = auto_compact(msgs, counting_summarizer, context_window=20_000, manual=False)
    assert out is not None
    assert out.method == "prune"          # took the cheap path
    assert calls["n"] == 0                # LLM summarizer never called
    assert out.pruned > 0
    # The stale tool result is cleared; recent tail is untouched.
    assert any(m.get("content") == PRUNED_TOOL_RESULT_PLACEHOLDER for m in out.messages)
    assert out.messages[-1]["content"] == "current question"


def test_prune_keeps_recent_tool_output_verbatim():
    body = [
        {"role": "tool", "tool_call_id": "a", "content": "OLD"},
        {"role": "tool", "tool_call_id": "b", "content": "RECENT"},
    ]
    pruned, saved = prune_tool_results(body, keep_start=1)
    assert pruned[0]["content"] == PRUNED_TOOL_RESULT_PLACEHOLDER
    assert pruned[1]["content"] == "RECENT"      # in the kept tail, untouched
    assert saved == len("OLD")


# --- new: summary retry with backoff ----------------------------------------

def test_summary_retries_then_succeeds():
    attempts = {"n": 0}

    def flaky(_s, _t):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise RuntimeError("transient")
        return "STRUCTURED SUMMARY after retries"

    breaker = CompactCircuitBreaker()
    msgs = _msgs(20_000)
    out = auto_compact(msgs, flaky, context_window=128_000, manual=True, breaker=breaker)
    assert out is not None
    assert "after retries" in out.messages[1]["content"]
    assert attempts["n"] == 3
    assert breaker.consecutive_failures == 0     # success resets the breaker


# --- new: reactive overflow detector ----------------------------------------

def test_overflow_detector():
    assert is_context_overflow_error("Error: prompt is too long: 210000 tokens > 200000")
    assert is_context_overflow_error("context_length_exceeded")
    assert not is_context_overflow_error("connection reset by peer")
    assert not is_context_overflow_error("")
