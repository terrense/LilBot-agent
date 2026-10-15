"""Inline <think> reasoning (MiniMax et al.) must not leak into visible content."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lilbot.config import LilBotConfig
from lilbot.llm.providers import (
    KNOWN_PROVIDERS,
    OpenAICompatibleProvider,
    _InlineThinkSplitter,
    _iter_stream_events,
    _vendor_body_extras,
    split_inline_think,
)


def _run(chunks: list[str]) -> tuple[str, str]:
    splitter = _InlineThinkSplitter()
    parts = [p for c in chunks for p in splitter.feed(c)] + splitter.flush()
    content = "".join(t for k, t in parts if k == "content")
    reasoning = "".join(t for k, t in parts if k == "reasoning")
    return content, reasoning


@pytest.mark.parametrize("chunks", [
    ["<think>plan it</think>\n\nAnswer."],
    ["<think>plan", " it</think>Answer."],
    ["<", "thi", "nk>plan it</th", "ink>", "\n\nAns", "wer."],
    ["\n  <think>plan it</think>Answer."],
])
def test_leading_think_block_routed_to_reasoning(chunks):
    content, reasoning = _run(chunks)
    assert content == "Answer."
    assert reasoning.strip() == "plan it"


def test_no_think_passes_through_unchanged():
    assert _run(["Hello", " world"]) == ("Hello world", "")


def test_think_later_in_answer_is_left_alone():
    content, reasoning = _run(["Use the <think> tag like this: <think>x</think>"])
    assert content == "Use the <think> tag like this: <think>x</think>"
    assert reasoning == ""


def test_unterminated_think_is_reasoning():
    content, reasoning = _run(["<think>cut off mid-thou", "ght"])
    assert content == ""
    assert reasoning == "cut off mid-thought"


def test_short_non_tag_prefix_is_released():
    assert _run(["<", "b>bold</b>"]) == ("<b>bold</b>", "")


def test_split_inline_think_merges_with_existing_reasoning():
    content, reasoning = split_inline_think("<think>inline</think>Done", "field")
    assert content == "Done"
    assert reasoning == "field\ninline"
    assert split_inline_think("plain", "") == ("plain", "")


def _sse(deltas: list[dict]) -> list[str]:
    lines = [f"data: {json.dumps({'choices': [{'delta': d}]})}" for d in deltas]
    return lines + ["data: [DONE]"]


def test_stream_events_strip_inline_think():
    events = list(_iter_stream_events(_sse([
        {"content": "<thi"}, {"content": "nk>reason</think>\n\nHi"}, {"content": " there"},
    ])))
    visible = "".join(e.text for e in events if getattr(e, "text", ""))
    assert visible == "Hi there"
    final = events[-1].final
    assert final.content == "Hi there"
    assert final.reasoning_content == "reason"


def test_stream_events_keep_reasoning_content_field():
    events = list(_iter_stream_events(_sse([{"reasoning_content": "r"}, {"content": "Hi"}])))
    final = events[-1].final
    assert (final.content, final.reasoning_content) == ("Hi", "r")


def test_minimax_presets_and_body_extras():
    assert KNOWN_PROVIDERS["minimax"] == "https://api.minimaxi.com/v1"
    assert KNOWN_PROVIDERS["minimax-intl"] == "https://api.minimax.io/v1"
    assert _vendor_body_extras("https://api.minimaxi.com/v1") == {"reasoning_split": True}
    assert _vendor_body_extras("https://api.minimax.io/v1") == {"reasoning_split": True}
    assert _vendor_body_extras("https://api.deepseek.com") == {}


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeClient:
    sent: dict = {}

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, headers=None, json=None):
        _FakeClient.sent = json
        return _FakeResponse({"choices": [{"message": {"content": "<think>hmm</think>\n\nOK"}, "finish_reason": "stop"}]})


def test_complete_sends_reasoning_split_and_strips_think(monkeypatch, tmp_path: Path):
    import httpx

    monkeypatch.setattr(httpx, "Client", _FakeClient)
    cfg = LilBotConfig(workspace=tmp_path, provider="minimax", model="MiniMax-M3",
                       base_url="https://api.minimaxi.com/v1", api_key="k" * 20)
    turn = OpenAICompatibleProvider(cfg).complete([{"role": "user", "content": "hi"}], [])
    assert _FakeClient.sent["reasoning_split"] is True
    assert turn.content == "OK"
    assert turn.reasoning_content == "hmm"
