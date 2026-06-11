from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import uuid4


@dataclass
class MemoryEntry:
    id: str
    name: str
    text: str
    kind: str = "note"
    scope: str = "project"
    created_at: float = 0.0

    def preview(self, width: int = 90) -> str:
        clean = " ".join(self.text.split())
        return clean if len(clean) <= width else clean[: width - 1] + "..."


class MemoryStore:
    def __init__(self, state_dir: Path):
        self.path = state_dir / "memory.jsonl"

    def _read(self) -> list[MemoryEntry]:
        entries: list[MemoryEntry] = []
        if not self.path.exists():
            return entries
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                data = json.loads(line)
                entries.append(MemoryEntry(**data))
            except (TypeError, json.JSONDecodeError):
                continue
        return entries

    def _write(self, entries: list[MemoryEntry]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        text = "\n".join(json.dumps(asdict(e), ensure_ascii=False) for e in entries)
        self.path.write_text(text + ("\n" if text else ""), encoding="utf-8")

    def add(self, name: str, text: str, kind: str = "note", scope: str = "project") -> MemoryEntry:
        entry = MemoryEntry(
            id=f"mem_{uuid4().hex[:10]}",
            name=name.strip() or "untitled",
            text=text.strip(),
            kind=kind.strip() or "note",
            scope=scope.strip() or "project",
            created_at=time.time(),
        )
        entries = self._read()
        entries.append(entry)
        self._write(entries)
        return entry

    def delete(self, memory_id_or_name: str) -> bool:
        entries = self._read()
        kept = [e for e in entries if e.id != memory_id_or_name and e.name != memory_id_or_name]
        changed = len(kept) != len(entries)
        if changed:
            self._write(kept)
        return changed

    def list(self) -> list[MemoryEntry]:
        return sorted(self._read(), key=lambda e: e.created_at, reverse=True)

    def search(self, query: str, limit: int = 8) -> list[MemoryEntry]:
        terms = [t.lower() for t in query.split() if t.strip()]
        if not terms:
            return self.list()[:limit]
        scored: list[tuple[int, MemoryEntry]] = []
        for entry in self._read():
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

