"""Tests for deepened worktree helpers."""
from __future__ import annotations

import re

from lilbot.tools.builtin import (
    WORKTREE_SYMLINK_DIRS,
    _create_dir_link,
    _symlink_worktree_deps,
    _worktree_slug,
)


def test_slug_format():
    slug = _worktree_slug()
    assert re.match(r"^[a-z]+-[a-z]+-\d{4}-\d{4}$", slug), slug


def test_create_dir_link_makes_usable_link(tmp_path):
    src = tmp_path / "src"
    (src / "inner").mkdir(parents=True)
    (src / "inner" / "f.txt").write_text("hello", encoding="utf-8")
    dst = tmp_path / "link"

    ok, kind = _create_dir_link(src, dst)
    assert ok, kind
    # The link is traversable to the source content (symlink or junction).
    assert (dst / "inner" / "f.txt").read_text(encoding="utf-8") == "hello"


def test_symlink_deps_links_present_skips_absent_and_existing(tmp_path):
    main = tmp_path / "main"
    wt = tmp_path / "wt"
    (main / "node_modules" / "pkg").mkdir(parents=True)
    (main / "node_modules" / "pkg" / "x.js").write_text("//x", encoding="utf-8")
    wt.mkdir()
    # Pre-existing dir in worktree should be left alone.
    (wt / ".venv").mkdir()

    results = _symlink_worktree_deps(main, wt, ["node_modules", ".venv", "vendor"])
    by_dir = {r["dir"]: r for r in results}

    # node_modules: present in main, absent in wt -> linked
    assert by_dir["node_modules"]["linked"] is True
    assert (wt / "node_modules" / "pkg" / "x.js").read_text(encoding="utf-8") == "//x"
    # .venv exists in wt -> skipped (no record), vendor absent in main -> skipped
    assert ".venv" not in by_dir
    assert "vendor" not in by_dir


def test_default_symlink_dirs_cover_common_ecosystems():
    for d in ("node_modules", ".venv", "vendor"):
        assert d in WORKTREE_SYMLINK_DIRS


def test_worktree_prune_registered():
    from lilbot.tools import ToolRegistry, register_builtins
    r = ToolRegistry()
    register_builtins(r)
    assert r.get("worktree_prune") is not None
