"""Session persistence + resume.

LilBot kept the whole conversation only in memory, so closing the process lost
it. This stores each session's messages + usage to
``.lilbot/sessions/<id>.json`` after every turn, so a session can be resumed
later (``--resume`` / ``/resume``) instead of starting over.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class SessionInfo:
    session_id: str
    updated_at: float
    message_count: int
    preview: str


class SessionStore:
    def __init__(self, state_dir: Path) -> None:
        self.dir = Path(state_dir) / "sessions"

    def _path(self, session_id: str) -> Path:
        return self.dir / f"{session_id}.json"

    def save(
        self,
        session_id: str,
        messages: list[dict[str, Any]],
        usage: dict[str, int],
        meta: dict[str, Any] | None = None,
    ) -> None:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            payload = {
                "session_id": session_id,
                "updated_at": time.time(),
                "messages": messages,
                "usage": usage,
                "meta": meta or {},
            }
            # Atomic-ish write: temp then replace, so a crash mid-write doesn't
            # corrupt the session file.
            tmp = self._path(session_id).with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self._path(session_id))
        except OSError:
            pass  # persistence is best-effort, never break a turn

    def load(self, session_id: str) -> dict[str, Any] | None:
        path = self._path(session_id)
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def latest_id(self) -> str | None:
        infos = self.list()
        return infos[0].session_id if infos else None

    def list(self) -> list[SessionInfo]:
        if not self.dir.is_dir():
            return []
        out: list[SessionInfo] = []
        for path in self.dir.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            messages = data.get("messages") or []
            preview = ""
            for m in messages:
                if m.get("role") == "user" and m.get("content"):
                    preview = " ".join(str(m["content"]).split())[:60]
                    break
            out.append(SessionInfo(
                session_id=str(data.get("session_id") or path.stem),
                updated_at=float(data.get("updated_at") or path.stat().st_mtime),
                message_count=len(messages),
                preview=preview,
            ))
        out.sort(key=lambda s: s.updated_at, reverse=True)
        return out

    def delete(self, session_id: str) -> bool:
        path = self._path(session_id)
        try:
            path.unlink()
            return True
        except OSError:
            return False
