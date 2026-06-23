"""Cycle memory archive.

When the conversation is compacted, the summarized prefix used to be discarded.
This archives each compaction as a "cycle" — a dated briefing written to
``.lilbot/archives/cycle-<ts>.md`` — so the knowledge survives beyond the
current context and can be searched later with the ``recall_archive`` tool.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


@dataclass
class CycleInfo:
    cycle_id: str
    path: str
    archived_at: float
    briefing: str


class CycleArchive:
    def __init__(self, state_dir: Path) -> None:
        self.dir = Path(state_dir) / "archives"

    def archive(self, briefing: str, summarized_messages: int = 0, before_tokens: int = 0) -> str | None:
        """Write a cycle briefing and return its file path (or None on failure)."""
        briefing = (briefing or "").strip()
        if not briefing:
            return None
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            ts = time.time()
            # Suffix keeps cycles distinct even when archived in the same second.
            cycle_id = "cycle-" + datetime.fromtimestamp(ts).strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:4]
            iso = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            body = (
                f"# {cycle_id}\n"
                f"- archived: {iso}\n"
                f"- summarized_messages: {summarized_messages}\n"
                f"- before_tokens: {before_tokens}\n\n"
                f"{briefing}\n"
            )
            path = self.dir / f"{cycle_id}.md"
            path.write_text(body, encoding="utf-8")
            return str(path)
        except OSError:
            return None

    def list(self) -> list[CycleInfo]:
        if not self.dir.is_dir():
            return []
        out: list[CycleInfo] = []
        for path in self.dir.glob("*.md"):
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
                mtime = path.stat().st_mtime
            except OSError:
                continue
            out.append(CycleInfo(cycle_id=path.stem, path=str(path), archived_at=mtime, briefing=text))
        out.sort(key=lambda c: c.archived_at, reverse=True)
        return out

    def search(self, query: str, limit: int = 5) -> list[CycleInfo]:
        q = (query or "").lower()
        items = self.list()
        if not q:
            return items[:limit]
        hits = [c for c in items if q in c.briefing.lower()]
        return hits[:limit]
