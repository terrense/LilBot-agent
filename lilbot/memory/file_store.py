"""File-based memory store.

Each memory is its own ``.md`` file with YAML-ish frontmatter, organized into
two directories by kind:

  * user-level   (``~/.lilbot/memory``)   — kind ``user`` / ``feedback``;
    follows the human across projects
  * project-level (``<workspace>/.lilbot/memory``) — kind ``project`` /
    ``reference`` / ``note``; belongs to the repo, can be committed & shared

A ``MEMORY.md`` index in each directory lists its memories. This is a drop-in
replacement for ``MemoryStore`` — same ``add/list/search/delete/context`` API
and the same ``MemoryEntry`` objects — so recall, extraction, and the memory
tools keep working unchanged.
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from uuid import uuid4

from .store import MemoryEntry

ENTRYPOINT = "MEMORY.md"
_USER_KINDS = {"user", "feedback"}
_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n(.*)\Z", re.DOTALL)


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", name.strip().lower()).strip("-")
    return slug[:48] or "memory"


def _scope_for_kind(kind: str) -> str:
    return "user" if kind in _USER_KINDS else "project"


def _format_memory_file(entry: MemoryEntry) -> str:
    return (
        "---\n"
        f"id: {entry.id}\n"
        f"name: {entry.name}\n"
        f"kind: {entry.kind}\n"
        f"scope: {entry.scope}\n"
        f"created_at: {entry.created_at}\n"
        "---\n"
        f"{entry.text}\n"
    )


def _parse_memory_file(path: Path) -> MemoryEntry | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    m = _FRONTMATTER_RE.match(raw)
    if not m:
        return None
    fields: dict[str, str] = {}
    for line in m.group(1).split("\n"):
        c = line.find(":")
        if c < 0:
            continue
        fields[line[:c].strip()] = line[c + 1:].strip()
    body = m.group(2).strip()
    try:
        created = float(fields.get("created_at") or 0.0)
    except ValueError:
        created = 0.0
    return MemoryEntry(
        id=fields.get("id") or path.stem,
        name=fields.get("name") or path.stem,
        text=body,
        kind=fields.get("kind") or "note",
        scope=fields.get("scope") or "project",
        created_at=created,
    )


class FileMemoryStore:
    def __init__(self, state_dir: Path, user_dir: Path | None = None) -> None:
        self.project_dir = Path(state_dir) / "memory"
        self.user_dir = Path(user_dir) if user_dir is not None else (Path.home() / ".lilbot" / "memory")

    # -- internals --------------------------------------------------------

    def _dir_for_kind(self, kind: str) -> Path:
        return self.user_dir if kind in _USER_KINDS else self.project_dir

    def _all_dirs(self) -> list[Path]:
        return [self.user_dir, self.project_dir]

    def _unique_path(self, directory: Path, name: str, entry_id: str) -> Path:
        base = _slugify(name)
        candidate = directory / f"{base}.md"
        if candidate.exists():
            candidate = directory / f"{base}-{entry_id[-6:]}.md"
        return candidate

    def _rewrite_index(self, directory: Path) -> None:
        entries = self._scan(directory)
        lines = ["# Memory Index", ""]
        for e in entries:
            fname = _slugify(e.name)
            lines.append(f"- [{e.name}]({fname}.md) — {e.preview(80)}")
        try:
            (directory / ENTRYPOINT).write_text("\n".join(lines) + "\n", encoding="utf-8")
        except OSError:
            pass

    def _scan(self, directory: Path) -> list[MemoryEntry]:
        if not directory.is_dir():
            return []
        out: list[MemoryEntry] = []
        for path in directory.glob("*.md"):
            if path.name == ENTRYPOINT:
                continue
            entry = _parse_memory_file(path)
            if entry is not None:
                out.append(entry)
        return out

    def _find_path(self, memory_id_or_name: str) -> Path | None:
        for directory in self._all_dirs():
            if not directory.is_dir():
                continue
            for path in directory.glob("*.md"):
                if path.name == ENTRYPOINT:
                    continue
                entry = _parse_memory_file(path)
                if entry and (entry.id == memory_id_or_name or entry.name == memory_id_or_name):
                    return path
        return None

    # -- public API (mirrors MemoryStore) ---------------------------------

    def add(self, name: str, text: str, kind: str = "note", scope: str = "project") -> MemoryEntry:
        kind = (kind or "note").strip() or "note"
        entry = MemoryEntry(
            id=f"mem_{uuid4().hex[:10]}",
            name=name.strip() or "untitled",
            text=text.strip(),
            kind=kind,
            scope=_scope_for_kind(kind),
            created_at=time.time(),
        )
        directory = self._dir_for_kind(kind)
        directory.mkdir(parents=True, exist_ok=True)
        path = self._unique_path(directory, entry.name, entry.id)
        path.write_text(_format_memory_file(entry), encoding="utf-8")
        self._rewrite_index(directory)
        return entry

    def delete(self, memory_id_or_name: str) -> bool:
        path = self._find_path(memory_id_or_name)
        if path is None:
            return False
        directory = path.parent
        try:
            path.unlink()
        except OSError:
            return False
        self._rewrite_index(directory)
        return True

    def list(self) -> list[MemoryEntry]:
        entries = self._scan(self.user_dir) + self._scan(self.project_dir)
        entries.sort(key=lambda e: e.created_at, reverse=True)
        return entries

    def search(self, query: str, limit: int = 8) -> list[MemoryEntry]:
        terms = [t.lower() for t in query.split() if t.strip()]
        if not terms:
            return self.list()[:limit]
        scored: list[tuple[int, MemoryEntry]] = []
        for entry in self.list():
            blob = f"{entry.name} {entry.kind} {entry.scope} {entry.text}".lower()
            score = sum(blob.count(term) for term in terms)
            if score:
                scored.append((score, entry))
        scored.sort(key=lambda item: (item[0], item[1].created_at), reverse=True)
        return [entry for _, entry in scored[:limit]]

    def context(self, limit: int = 6) -> str:
        entries = self.list()[:limit]
        if not entries:
            return "No persistent memories yet."
        return "\n".join(f"- [{e.kind}/{e.scope}] {e.name}: {e.preview(160)}" for e in entries)

    def import_from(self, other) -> int:
        """One-time migration: copy entries from a legacy store (e.g. JSONL)."""
        count = 0
        existing = {(e.name, e.text) for e in self.list()}
        for e in other.list():
            if (e.name, e.text) in existing:
                continue
            self.add(name=e.name, text=e.text, kind=e.kind, scope=e.scope)
            count += 1
        return count
