"""Tests for M5 — tool-catalog prefix-cache stability."""
from __future__ import annotations

from lilbot.tools import ToolRegistry, register_builtins
from lilbot.tools.builtin import _tool_search_load


def _registry():
    r = ToolRegistry()
    register_builtins(r)
    return r


def test_catalog_is_byte_stable_across_turns():
    r = _registry()
    fp1 = r.catalog_fingerprint()
    fp2 = r.catalog_fingerprint()
    # Same tool set -> identical fingerprint (stable prefix for caching).
    assert fp1 == fp2


def test_base_catalog_returns_same_object_when_unchanged():
    r = _registry()
    a = r.schemas()  # render_ctx None -> cached canonical list
    b = r.schemas()
    assert a is b  # identical object => identical serialized bytes


def test_fingerprint_changes_when_deferred_tool_discovered():
    r = _registry()
    fp_before = r.catalog_fingerprint()
    _tool_search_load({"query": "select:git_show"}, r)  # reveal a deferred tool
    fp_after = r.catalog_fingerprint()
    assert fp_before != fp_after
    # And stabilizes again after the change.
    assert r.catalog_fingerprint() == fp_after


def test_render_context_does_not_corrupt_cache():
    r = _registry()
    base_fp = r.catalog_fingerprint()
    ctx = {"agent_types": [], "active_tasks": []}
    # Calling with a render context mutates a copy, not the cached catalog.
    r.schemas(ctx)
    r.schemas(ctx)
    assert r.catalog_fingerprint() == base_fp


def test_register_invalidates_cache():
    from lilbot.tools.registry import ToolDef, ToolResult
    r = _registry()
    fp_before = r.catalog_fingerprint()
    r.register(ToolDef("brand_new_core_tool", "d", {"type": "object", "properties": {}},
                       lambda a, c: ToolResult(True, "x")))
    # New (non-deferred) tool is visible -> catalog changed.
    assert r.catalog_fingerprint() != fp_before
