"""Prompt-cache usage normalization (adapted from mewcode for OpenAI-compat)."""
from __future__ import annotations

from lilbot.llm.providers import _normalize_usage


def test_deepseek_cache_hits_normalized():
    out = _normalize_usage({
        "prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150,
        "prompt_cache_hit_tokens": 80, "prompt_cache_miss_tokens": 20,
    })
    assert out["cache_read_tokens"] == 80
    assert out["prompt_tokens"] == 100


def test_openai_cached_tokens_normalized_and_flattened():
    out = _normalize_usage({
        "prompt_tokens": 100, "completion_tokens": 50,
        "prompt_tokens_details": {"cached_tokens": 64},
    })
    assert out["cache_read_tokens"] == 64
    # Nested dict is dropped so the agent's int-summing usage accumulator is safe.
    assert "prompt_tokens_details" not in out


def test_no_cache_field_when_absent():
    out = _normalize_usage({"prompt_tokens": 10, "completion_tokens": 2})
    assert "cache_read_tokens" not in out


def test_non_int_values_dropped():
    out = _normalize_usage({"prompt_tokens": 5, "model": "deepseek"})
    assert out == {"prompt_tokens": 5}
