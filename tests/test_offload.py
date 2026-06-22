"""Tests for large tool-result offload + recovery (ported from mewcode)."""
from __future__ import annotations

from types import SimpleNamespace

from lilbot.tools.registry import ToolContext, ToolDef, ToolRegistry, ToolResult
from lilbot.tools.builtin import _retrieve_tool_result
from lilbot.tools.offload import INLINE_LIMIT


def _ctx(tmp_path):
    cfg = SimpleNamespace(state_dir=tmp_path / ".lilbot")
    return ToolContext(None, None, None, None, None, None, cfg)


def _reg(output):
    r = ToolRegistry()
    r.register(ToolDef("t", "d", {"type": "object", "properties": {}}, lambda a, c: ToolResult(True, output)))
    return r


def test_small_result_passes_through(tmp_path):
    r = _reg("hello")
    res, _ = r.execute("t", {}, _ctx(tmp_path))
    assert res.output == "hello"
    assert "persisted" not in res.metadata


def test_large_result_is_offloaded(tmp_path):
    big = "X" * (INLINE_LIMIT + 40_000)
    r = _reg(big)
    ctx = _ctx(tmp_path)
    res, _ = r.execute("t", {}, ctx)
    assert res.metadata["persisted"] is True
    assert res.metadata["original_chars"] == len(big)
    assert "<persisted-output>" in res.output
    assert len(res.output) < len(big)
    # The full content is recoverable.
    rr = _retrieve_tool_result({"path": res.metadata["persisted_path"]}, ctx)
    assert rr.ok
    assert rr.metadata["total_chars"] == len(big)


def test_retrieve_supports_offset_and_limit(tmp_path):
    big = "ABCDE" * 10_000  # 50k chars
    r = _reg(big)
    ctx = _ctx(tmp_path)
    res, _ = r.execute("t", {}, ctx)
    path = res.metadata["persisted_path"]
    rr = _retrieve_tool_result({"path": path, "offset": 10, "limit": 5}, ctx)
    assert rr.ok
    assert rr.metadata["chars"] == 5
    assert rr.metadata["truncated"] is True


def test_offload_falls_back_to_truncation_without_state_dir():
    big = "Y" * (INLINE_LIMIT + 10_000)
    r = _reg(big)
    ctx = ToolContext(None, None, None, None, None, None, SimpleNamespace(state_dir=None))
    res, _ = r.execute("t", {}, ctx)
    assert res.metadata.get("truncated") is True
    assert len(res.output) < len(big)
