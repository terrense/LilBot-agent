"""One pruning walker behind every search tool."""
from __future__ import annotations

import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from lilbot.config import LilBotConfig
from lilbot.sandbox import Sandbox
from lilbot.sandbox import walk as walk_mod
from lilbot.sandbox.walk import (
    MAX_SEARCH_FILE_BYTES,
    IgnoreRules,
    glob_to_regex,
    iter_files,
    read_text_for_search,
    walk,
)
from lilbot.tools import builtin


def _tree(root: Path, files: dict[str, str | bytes]) -> Path:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
    return root


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return _tree(tmp_path, {
        "README.md": "hello needle\n",
        "app.py": "def main():\n    return 'needle'\n",
        "pkg/__init__.py": "",
        "pkg/core.py": "class Core:\n    pass\n",
        "pkg/sub/deep.py": "NEEDLE = 1\n",
        "pkg/sub/notes.txt": "nothing here\n",
        "docs/logo.png": b"\x89PNG\r\n\x1a\n" + b"needle" * 10,
        "docs/blob.bin": b"needle\x00\x01\x02",
        ".venv/lib/site.py": "needle in venv\n",
        "node_modules/x/index.js": "needle\n",
        "proj.egg-info/PKG-INFO": "needle\n",
        "build/out.py": "needle\n",
        ".env": "SECRET=needle\n",
        "run.log": "needle in log\n",
        "tmp/scratch.py": "needle\n",
        ".gitignore": "# comment\n.env\n*.log\n/tmp/\n!keep.log\n",
    })


def _ctx(root: Path) -> SimpleNamespace:
    return SimpleNamespace(sandbox=Sandbox(root), config=LilBotConfig(workspace=root))


# ── the walker ───────────────────────────────────────────────────────────

def test_noisy_dirs_are_pruned_before_descending(repo, monkeypatch):
    scanned: list[str] = []
    real_scandir = os.scandir

    def spy(path):
        scanned.append(Path(path).name)
        return real_scandir(path)

    monkeypatch.setattr(walk_mod.os, "scandir", spy)
    rels = [e.rel for e in iter_files(repo)]
    assert "pkg/sub/deep.py" in rels
    for noisy in (".venv", "node_modules", "proj.egg-info", "build", "lib"):
        assert noisy not in scanned, noisy  # never even listed
    assert not any(r.startswith((".venv", "node_modules", "build", "proj.egg-info")) for r in rels)


def test_order_matches_sorted_rglob(repo):
    expected = [
        p.relative_to(repo).as_posix() for p in sorted(repo.rglob("*"))
        if not any(part in {".venv", "node_modules", "build", "proj.egg-info"} for part in p.relative_to(repo).parts)
    ]
    assert [e.rel for e in walk(repo, include_dirs=True)] == expected


def test_max_depth_and_dirs(repo):
    top = [(e.rel, e.is_dir) for e in walk(repo, max_depth=1, include_dirs=True)]
    assert ("pkg", True) in top and ("app.py", False) in top
    assert all("/" not in rel for rel, _ in top)
    two = {e.rel for e in walk(repo, max_depth=2, include_dirs=True)}
    assert "pkg/sub" in two and "pkg/sub/deep.py" not in two


def test_explicit_noisy_base_still_walks(repo):
    assert [e.rel for e in iter_files(repo / ".venv", root=repo)] == ["lib/site.py"]


def test_gitignore_subset(repo):
    rels = {e.rel for e in walk(repo, include_dirs=True, respect_gitignore=True)}
    assert ".env" not in rels          # plain name
    assert "run.log" not in rels       # *.ext
    assert "tmp" not in rels and "tmp/scratch.py" not in rels  # anchored dir
    assert "app.py" in rels and ".gitignore" in rels
    rules = IgnoreRules.load(repo)
    assert all(not r.pattern.startswith("!") for r in rules.rules)  # negations skipped


def test_gitignore_cache_refreshes_on_change(repo):
    assert IgnoreRules.load(repo).ignored("app.py", "app.py", False) is False
    time.sleep(0.01)
    (repo / ".gitignore").write_text("app.py\n", encoding="utf-8")
    os.utime(repo / ".gitignore", ns=(time.time_ns(), time.time_ns() + 10_000_000))
    assert IgnoreRules.load(repo).ignored("app.py", "app.py", False) is True


def test_gitignore_anchored_rules_use_root_relative_paths(repo):
    # Walking from a sub-base must still apply root-anchored rules correctly.
    (repo / ".gitignore").write_text("/pkg/sub/\n", encoding="utf-8")
    os.utime(repo / ".gitignore", ns=(time.time_ns(), time.time_ns() + 20_000_000))
    rels = [e.rel for e in iter_files(repo / "pkg", root=repo, respect_gitignore=True)]
    assert rels == ["__init__.py", "core.py"]


@pytest.mark.skipif(os.name == "nt", reason="creating symlinks needs admin/dev mode on Windows")
def test_symlinked_dirs_are_not_followed(repo):
    (repo / "loop").symlink_to(repo, target_is_directory=True)
    rels = [e.rel for e in walk(repo, include_dirs=True)]
    assert "loop" in rels
    assert not any(r.startswith("loop/") for r in rels)


# ── content-search helpers ───────────────────────────────────────────────

def test_read_text_for_search_skips_binary_and_huge(repo, tmp_path):
    assert read_text_for_search(repo / "docs/logo.png") is None      # by suffix
    assert read_text_for_search(repo / "docs/blob.bin") is None      # by suffix
    sneaky = repo / "data.txt"
    sneaky.write_bytes(b"needle\x00binary")
    assert read_text_for_search(sneaky) is None                      # NUL sniff
    big = repo / "big.txt"
    big.write_bytes(b"x" * (MAX_SEARCH_FILE_BYTES + 1))
    assert read_text_for_search(big) is None
    assert read_text_for_search(repo / "app.py").startswith("def main")
    assert read_text_for_search(repo / "missing.py") is None


@pytest.mark.parametrize("pattern", [
    "*.py", "**/*.py", "pkg/*.py", "pkg/**/*.py", "**/sub/*", "?pp.py", "[ap]*", "*.md", "sub", "pkg", "**",
])
def test_glob_matches_pathlib_rglob(repo, pattern):
    noisy = {".venv", "node_modules", "build", "proj.egg-info", "tmp"}
    ignored = {".env", "run.log"}
    expected = sorted(
        p.relative_to(repo).as_posix() for p in repo.rglob(pattern)
        if not (set(p.relative_to(repo).parts) & noisy) and p.name not in ignored and p != repo
    )
    assert Sandbox(repo).glob(pattern) == expected


def test_glob_regex_details():
    rx = glob_to_regex("src/**/*.py")
    assert rx.match("src/a.py") and rx.match("src/x/y/a.py") and rx.match("vendor/src/a.py")
    assert not rx.match("src/a.pyc")
    assert glob_to_regex("*.py").match("deep/dir/a.py")
    assert not glob_to_regex("a?.py").match("a/b.py")
    assert glob_to_regex("[!x]*.md").match("a.md") and not glob_to_regex("[!x]*.md").match("x.md")


# ── tools ────────────────────────────────────────────────────────────────

def test_grep_skips_noise_binaries_and_ignored(repo):
    result = builtin._grep({"pattern": "needle"}, _ctx(repo))
    hits = {m["path"] for m in result.metadata["matches"]}
    assert hits == {"README.md", "app.py", "pkg/sub/deep.py"}
    assert result.metadata["files_skipped"] == 2  # logo.png + blob.bin, never decoded


def test_grep_on_a_single_file(repo):
    result = builtin._grep({"pattern": "needle", "path": "app.py"}, _ctx(repo))
    assert [m["path"] for m in result.metadata["matches"]] == ["app.py"]


def test_grep_can_target_an_ignored_dir_explicitly(repo):
    result = builtin._grep({"pattern": "needle", "path": ".venv"}, _ctx(repo))
    assert [m["path"] for m in result.metadata["matches"]] == [".venv/lib/site.py"]


def test_glob_tool_and_list_dir(repo):
    ctx = _ctx(repo)
    result = builtin._glob({"pattern": "**/*.py"}, ctx)
    assert result.metadata["paths"] == ["app.py", "pkg/__init__.py", "pkg/core.py", "pkg/sub/deep.py"]
    assert result.output.splitlines()[-1] == "(4 matches)"
    assert builtin._glob({"pattern": "*.nope"}, ctx).metadata["count"] == 0
    listing = builtin._list_dir({"path": "."}, ctx).output.splitlines()
    assert "pkg/sub/" in listing and ".venv/" not in listing and "proj.egg-info/" not in listing
    assert ".env" not in listing  # hidden by default, as before
    assert "run.log" in listing   # list_dir shows ignored files; only search tools skip them


def test_file_search_project_map_and_symbols(repo):
    ctx = _ctx(repo)
    assert "pkg/core.py" in builtin._file_search({"query": "core"}, ctx).output
    assert "site.py" not in builtin._file_search({"query": "site"}, ctx).output
    pmap = builtin._project_map({}, ctx).output
    assert ".venv" not in pmap and "egg-info" not in pmap
    files = {p.relative_to(repo).as_posix() for p in builtin._iter_source_files(repo, repo)}
    assert files == {"README.md", "app.py", "pkg/__init__.py", "pkg/core.py", "pkg/sub/deep.py"}


def test_sandbox_grep_and_list_dir(repo):
    sandbox = Sandbox(repo)
    assert set(sandbox.grep("needle")) == {
        "README.md:1: hello needle", "app.py:2: return 'needle'", "pkg/sub/deep.py:1: NEEDLE = 1",
    }
    assert sandbox.grep("needle", "app.py") == ["app.py:2: return 'needle'"]
    assert "pkg/sub/" in sandbox.list_dir(".", 1) and ".venv/" not in sandbox.list_dir(".", 1)


# ── parallel reads ───────────────────────────────────────────────────────

def test_read_texts_keeps_input_order(tmp_path):
    paths = []
    for i in range(70):
        path = tmp_path / f"f{i:02d}.txt"
        path.write_text(f"file {i}", encoding="utf-8")
        paths.append(path)
    out = list(walk_mod.read_texts(reversed(paths), workers=4, batch=8))
    assert [p for p, _ in out] == list(reversed(paths))
    assert [t for _, t in out] == [f"file {i}" for i in reversed(range(70))]


def test_read_texts_stops_after_one_batch_when_caller_breaks(tmp_path, monkeypatch):
    paths = [tmp_path / f"f{i}.txt" for i in range(100)]
    for path in paths:
        path.write_text("x", encoding="utf-8")
    read: list[Path] = []
    real = walk_mod.read_text_for_search
    monkeypatch.setattr(walk_mod, "read_text_for_search", lambda p: read.append(p) or real(p))
    for i, _ in enumerate(walk_mod.read_texts(paths, workers=4, batch=10)):
        if i == 2:
            break
    assert len(read) <= 10  # only the first batch was ever submitted
