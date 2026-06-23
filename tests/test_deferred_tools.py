"""Tests for deferred tool loading + ToolSearch."""
from __future__ import annotations

from lilbot.tools import ToolRegistry, register_builtins
from lilbot.tools.builtin import CORE_TOOLS, _tool_search_load


def _registry() -> ToolRegistry:
    r = ToolRegistry()
    register_builtins(r)
    return r


def test_most_tools_are_deferred():
    r = _registry()
    total = len(r.list())
    visible = len(r.schemas())
    # The per-turn payload must be a small fraction of the full catalog.
    assert visible < total // 2
    assert visible == len(CORE_TOOLS) or visible <= len(CORE_TOOLS)
    assert len(r.deferred_tool_names()) == total - visible


def test_core_tools_are_always_visible():
    r = _registry()
    visible = {s["name"] for s in r.schemas()}
    for name in ("read_file", "write_file", "bash", "Agent", "ToolSearch"):
        assert name in visible


def test_toolsearch_select_loads_exact_names():
    r = _registry()
    assert "lsp_definition" in r.deferred_tool_names()
    res = _tool_search_load({"query": "select:lsp_definition,github_comment"}, r)
    assert res.ok
    assert set(res.metadata["loaded"]) == {"lsp_definition", "github_comment"}
    visible = {s["name"] for s in r.schemas()}
    assert "lsp_definition" in visible and "github_comment" in visible


def test_toolsearch_keyword_search():
    r = _registry()
    res = _tool_search_load({"query": "worktree", "max_results": 3}, r)
    assert res.ok
    assert any("orktree" in name for name in res.metadata["loaded"])


def test_toolsearch_unknown_query_is_graceful():
    r = _registry()
    res = _tool_search_load({"query": "select:does_not_exist"}, r)
    assert not res.ok
    assert "No matching" in res.output


def test_direct_call_reveals_deferred_tool():
    r = _registry()
    assert "git_show" in r.deferred_tool_names()
    # Executing a deferred tool directly should still work and reveal it.
    r.execute("git_show", {"rev": "HEAD"}, None)
    assert "git_show" not in r.deferred_tool_names()
    assert "git_show" in {s["name"] for s in r.schemas()}


def test_all_schemas_ignores_deferral():
    r = _registry()
    assert len(r.all_schemas()) == len(r.list())
