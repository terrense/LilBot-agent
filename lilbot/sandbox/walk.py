"""One directory walker for every search tool (grep, glob, list_dir, ...).

Why not ``Path.rglob("*")`` + filter: rglob descends into ``.venv`` /
``node_modules`` first and the caller filters afterwards, paying one
``stat`` per entry for ``is_file()``. On Windows with real-time scanning or
filter drivers that stat is ~0.2 ms, so 4,500 mostly-ignored entries cost
~0.8 s per tool call. ``os.scandir`` reports the entry type from the
directory listing itself, and pruning ``dirnames`` before descending means
ignored trees are never listed at all (a ripgrep-backed search gets the
same effect by honoring ``.gitignore``).

Ordering: children are visited sorted, depth-first, which reproduces
``sorted(base.rglob("*"))`` so tool output stays stable.
"""
from __future__ import annotations

import fnmatch
import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from typing import Iterable, Iterator

# Directories never worth searching. Pruned during descent; naming one as the
# search base itself (``path=".venv"``) still works.
NOISY_DIRS = frozenset({
    ".git", ".hg", ".svn", ".lilbot",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".venv", "venv", "env", "node_modules", "dist", "build",
})
NOISY_DIR_SUFFIXES = (".egg-info",)

# Content search skips these outright (no read at all).
BINARY_SUFFIXES = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".tif", ".tiff", ".psd",
    ".pdf", ".zip", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar", ".jar", ".whl",
    ".exe", ".dll", ".so", ".dylib", ".pyd", ".pyc", ".pyo", ".o", ".a", ".lib", ".obj",
    ".class", ".bin", ".dat", ".db", ".sqlite", ".sqlite3", ".pkl", ".npy", ".npz",
    ".pt", ".pth", ".onnx", ".safetensors", ".h5", ".parquet",
    ".mp3", ".mp4", ".wav", ".flac", ".avi", ".mov", ".mkv", ".ttf", ".otf", ".woff", ".woff2",
})
MAX_SEARCH_FILE_BYTES = 2 * 1024 * 1024
_SNIFF_BYTES = 8192

_CASE_INSENSITIVE = os.name == "nt"


@dataclass(frozen=True)
class WalkEntry:
    path: Path
    rel: str      # posix path relative to the walk base
    is_dir: bool
    depth: int    # 1 = direct child of the base


def _sort_key(name: str) -> str:
    return name.lower() if _CASE_INSENSITIVE else name


def is_noisy_dir(name: str) -> bool:
    return name in NOISY_DIRS or name.endswith(NOISY_DIR_SUFFIXES)


# --- .gitignore (simple subset) ----------------------------------------------

@dataclass(frozen=True)
class _IgnoreRule:
    pattern: str
    dir_only: bool
    anchored: bool  # match against the root-relative path, not just the name


class IgnoreRules:
    """Root ``.gitignore`` subset: name globs (``*.log``, ``build/``) and
    root-anchored paths (``/docs/tmp``). Negations (``!keep``) and nested
    ``.gitignore`` files are not supported — they are skipped, so at worst a
    re-included file is hidden, never an ignored one shown."""

    def __init__(self, rules: list[_IgnoreRule]):
        self.rules = rules

    @classmethod
    def load(cls, root: Path) -> "IgnoreRules":
        path = root / ".gitignore"
        try:
            stat = path.stat()
        except OSError:
            return cls([])
        key = (str(path), stat.st_mtime_ns, stat.st_size)
        cached = _IGNORE_CACHE.get(key[0])
        if cached and cached[0] == key:
            return cached[1]
        rules: list[_IgnoreRule] = []
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            lines = []
        for raw in lines:
            line = raw.strip()
            if not line or line.startswith(("#", "!")):
                continue
            dir_only = line.endswith("/")
            line = line.rstrip("/")
            anchored = line.startswith("/") or "/" in line
            line = line.lstrip("/")
            if line.startswith("**/"):
                line, anchored = line[3:], "/" in line[3:]
            if line:
                rules.append(_IgnoreRule(line, dir_only, anchored))
        rules_obj = cls(rules)
        _IGNORE_CACHE[key[0]] = (key, rules_obj)
        return rules_obj

    def ignored(self, name: str, root_rel: str, is_dir: bool) -> bool:
        for rule in self.rules:
            if rule.dir_only and not is_dir:
                continue
            target = root_rel if rule.anchored else name
            if fnmatch.fnmatch(target.lower(), rule.pattern.lower()) if _CASE_INSENSITIVE \
                    else fnmatch.fnmatchcase(target, rule.pattern):
                return True
        return False


_IGNORE_CACHE: dict[str, tuple[tuple[str, int, int], IgnoreRules]] = {}


# --- walking ---------------------------------------------------------------------

def walk(
    base: Path,
    *,
    root: Path | None = None,
    max_depth: int | None = None,
    include_dirs: bool = False,
    respect_gitignore: bool = False,
) -> Iterator[WalkEntry]:
    """Yield entries under ``base`` depth-first in sorted order.

    Noisy directories (and, with ``respect_gitignore``, ``.gitignore`` matches
    from ``root``) are pruned *before* descending. Symlinked directories are
    listed but not followed, like ``Path.rglob``. Unreadable directories are
    skipped silently.
    """
    base = Path(base)
    root = Path(root) if root is not None else base
    ignore = IgnoreRules.load(root) if respect_gitignore else None
    try:
        base_rel_to_root = base.relative_to(root).as_posix()
    except ValueError:
        base_rel_to_root = ""
    if base_rel_to_root == ".":
        base_rel_to_root = ""

    def scan(directory: Path, rel_prefix: str, depth: int) -> Iterator[WalkEntry]:
        try:
            with os.scandir(directory) as it:
                entries = sorted(it, key=lambda e: _sort_key(e.name))
        except OSError:
            return
        for entry in entries:
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                continue
            rel = f"{rel_prefix}{entry.name}"
            if is_dir and is_noisy_dir(entry.name):
                continue
            if ignore is not None and ignore.rules:
                root_rel = f"{base_rel_to_root}/{rel}" if base_rel_to_root else rel
                if ignore.ignored(entry.name, root_rel, is_dir):
                    continue
            path = Path(entry.path)
            if not is_dir or include_dirs:
                yield WalkEntry(path, rel, is_dir, depth)
            if is_dir and not entry.is_symlink() and (max_depth is None or depth < max_depth):
                yield from scan(path, rel + "/", depth + 1)

    yield from scan(base, "", 1)


def iter_files(base: Path, **kwargs) -> Iterator[WalkEntry]:
    """Files only (the common case for content search)."""
    kwargs.pop("include_dirs", None)
    return walk(base, include_dirs=False, **kwargs)


# --- content search helpers ----------------------------------------------------

def read_text_for_search(path: Path, max_bytes: int = MAX_SEARCH_FILE_BYTES) -> str | None:
    """Text of a file worth searching, or None for binary / oversized files.

    Binary = known binary suffix, or a NUL byte in the first 8 KB (git's rule).
    """
    if path.suffix.lower() in BINARY_SUFFIXES:
        return None
    try:
        if path.stat().st_size > max_bytes:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if b"\0" in data[:_SNIFF_BYTES]:
        return None
    return data.decode("utf-8", errors="ignore")


SEARCH_READ_WORKERS = 8
SEARCH_READ_BATCH = 32


def read_texts(paths: Iterable[Path], *, workers: int = SEARCH_READ_WORKERS,
               batch: int = SEARCH_READ_BATCH) -> Iterator[tuple[Path, str | None]]:
    """``(path, read_text_for_search(path))`` in input order, read in parallel.

    Opening a file costs ~0.7 ms on a Windows box with AV/encryption filter
    drivers, almost all of it waiting in the kernel with the GIL released, so
    8 threads cut a 175-file grep from ~120 ms to ~30 ms (what ripgrep gets
    from its thread pool). Files are read one batch at a time so a caller
    that stops early (max_results) wastes at most one batch.
    """
    it = iter(paths)
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="lilbot-read") as pool:
        while True:
            chunk = list(islice(it, batch))
            if not chunk:
                return
            yield from zip(chunk, pool.map(read_text_for_search, chunk))


# --- glob ------------------------------------------------------------------------

def glob_matches_dirs_only(pattern: str) -> bool:
    """Only an explicit trailing slash selects directories (``pkg/``, ``**/``)."""
    return pattern.replace("\\", "/").rstrip().endswith("/")


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Compile a pathlib-style glob for matching posix relative paths.

    ``*`` / ``?`` / ``[...]`` stay within one path segment, ``**`` spans any
    number of segments. Like ``Path.rglob(pattern)`` the pattern may match at
    any depth (an implicit leading ``**/``). Case-insensitive on Windows, as
    pathlib is there.
    """
    pattern = pattern.replace("\\", "/").lstrip("/").rstrip("/")
    while pattern.startswith("./"):
        pattern = pattern[2:]
    out = ["(?:.*/)?"]
    i, n = 0, len(pattern)
    while i < n:
        c = pattern[i]
        if c == "*":
            if pattern.startswith("**/", i):
                out.append("(?:.*/)?")
                i += 3
                continue
            if pattern.startswith("**", i) and (i + 2 == n):
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[":
            j = pattern.find("]", i + 1)
            if j == -1:
                out.append(re.escape(c))
            else:
                body = pattern[i + 1:j]
                if body.startswith("!"):
                    body = "^" + body[1:]
                out.append(f"[{body}]")
                i = j
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("".join(out) + r"\Z", re.IGNORECASE if _CASE_INSENSITIVE else 0)
