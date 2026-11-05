"""Session persistence + resume.

LilBot kept the whole conversation only in memory, so closing the process lost
it. This stores each session's messages + usage to
``.lilbot/sessions/<id>.json`` after every turn, so a session can be resumed
later (``--resume`` / ``/resume``) instead of starting over.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .session_db import SessionDB


@dataclass
class SessionInfo:
    session_id: str
    updated_at: float
    message_count: int
    preview: str


def interrupted_tool_result_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build protocol-safe markers for tool calls left open by a process crash.

    The most recent assistant tool-call batch is complete only when every call
    has a matching ``role=tool`` result. Missing results are unknowable: a tool
    may have completed a side effect just before the process stopped. Synthetic
    results keep provider history valid and explicitly tell the model to inspect
    state before attempting any repeat action.
    """
    index = len(messages) - 1
    while index >= 0 and messages[index].get("role") == "tool":
        index -= 1
    if index < 0:
        return []
    assistant = messages[index]
    calls = assistant.get("tool_calls") if assistant.get("role") == "assistant" else None
    if not isinstance(calls, list) or not calls:
        return []

    returned = {
        str(message.get("tool_call_id"))
        for message in messages[index + 1:]
        if message.get("role") == "tool" and message.get("tool_call_id")
    }
    missing: list[tuple[str, str]] = []
    for call in calls:
        if not isinstance(call, dict):
            continue
        call_id = str(call.get("id") or "")
        if not call_id or call_id in returned:
            continue
        function = call.get("function") or {}
        name = str(function.get("name") or "unknown_tool") if isinstance(function, dict) else "unknown_tool"
        missing.append((call_id, name))
    if not missing:
        return []

    recovered = [
        {
            "role": "tool",
            "tool_call_id": call_id,
            "name": name,
            "content": (
                "LilBot resumed after an interruption. The execution result for this call is unknown; "
                "the tool may have completed before the process stopped. Do not repeat a side-effecting "
                "operation until you inspect the current state."
            ),
        }
        for call_id, name in missing
    ]
    recovered.append({
        "role": "user",
        "content": (
            "Interruption recovery: one or more tool calls above have unknown execution status. "
            "Inspect the current state first and determine whether each action already happened. "
            "Do not blindly replay interrupted side-effecting operations."
        ),
    })
    return recovered


class SessionStore:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = Path(state_dir)
        self.dir = self.state_dir / "sessions"
        self.db = SessionDB(self.state_dir / "sessions.sqlite3")

    def _path(self, session_id: str) -> Path:
        return self.dir / f"{session_id}.json"

    def ensure_incremental(
        self,
        session_id: str,
        messages: list[dict[str, Any]],
        usage: dict[str, int],
        meta: dict[str, Any] | None = None,
    ) -> None:
        self.db.ensure_session(session_id, messages, usage, meta)

    def append_message(
        self,
        session_id: str,
        message: dict[str, Any],
        usage: dict[str, int],
        meta: dict[str, Any] | None = None,
    ) -> None:
        self.db.append_message(session_id, message, usage, meta)

    def replace_messages(
        self,
        session_id: str,
        messages: list[dict[str, Any]],
        usage: dict[str, int],
        meta: dict[str, Any] | None = None,
    ) -> None:
        self.db.replace_messages(session_id, messages, usage, meta)

    def begin_turn(self) -> None:
        self.db.begin_turn()

    def end_turn(self) -> None:
        self.db.end_turn()

    def save(
        self,
        session_id: str,
        messages: list[dict[str, Any]],
        usage: dict[str, int],
        meta: dict[str, Any] | None = None,
        *,
        sync_database: bool = True,
    ) -> None:
        payload = {
            "session_id": session_id,
            "updated_at": time.time(),
            "messages": messages,
            "usage": usage,
            "meta": meta or {},
        }
        # SQLite is the crash-safe source of truth. JSON remains an export for
        # backwards compatibility and for users/tools that inspect sessions.
        try:
            if sync_database or not self.db.update_metadata(session_id, usage, meta):
                self.db.replace_messages(session_id, messages, usage, meta)
        except (OSError, sqlite3.Error):
            pass
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            # Atomic-ish write: temp then replace, so a crash mid-write doesn't
            # corrupt the session file.
            tmp = self._path(session_id).with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self._path(session_id))
        except OSError:
            pass  # persistence is best-effort, never break a turn

    def load(self, session_id: str) -> dict[str, Any] | None:
        try:
            data = self.db.load(session_id)
        except (OSError, sqlite3.Error):
            data = None
        if data is not None:
            return data
        path = self._path(session_id)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        # One-time import for sessions created before SQLite persistence.
        try:
            self.db.replace_messages(
                session_id,
                list(data.get("messages") or []),
                dict(data.get("usage") or {}),
                dict(data.get("meta") or {}),
            )
        except (OSError, sqlite3.Error):
            pass
        return data

    def latest_id(self) -> str | None:
        infos = self.list()
        return infos[0].session_id if infos else None

    def list(self) -> list[SessionInfo]:
        by_id: dict[str, SessionInfo] = {}
        if self.dir.is_dir():
            for path in self.dir.glob("*.json"):
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if not isinstance(data, dict):
                    continue
                messages = data.get("messages") or []
                preview = ""
                for message in messages:
                    if message.get("role") == "user" and message.get("content"):
                        preview = " ".join(str(message["content"]).split())[:60]
                        break
                try:
                    updated_at = float(data.get("updated_at") or path.stat().st_mtime)
                except OSError:
                    updated_at = float(data.get("updated_at") or 0)
                sid = str(data.get("session_id") or path.stem)
                by_id[sid] = SessionInfo(sid, updated_at, len(messages), preview)
        try:
            rows = self.db.list_sessions()
        except (OSError, sqlite3.Error):
            rows = []
        for row in rows:
            info = SessionInfo(
                session_id=row["session_id"],
                updated_at=row["updated_at"],
                message_count=row["message_count"],
                preview=row["preview"],
            )
            old = by_id.get(info.session_id)
            if old is None or info.updated_at >= old.updated_at:
                by_id[info.session_id] = info
        return sorted(by_id.values(), key=lambda item: item.updated_at, reverse=True)

    def delete(self, session_id: str) -> bool:
        path = self._path(session_id)
        deleted = False
        try:
            path.unlink()
            deleted = True
        except OSError:
            pass
        try:
            deleted = self.db.delete(session_id) or deleted
        except (OSError, sqlite3.Error):
            pass
        return deleted

    def close(self) -> None:
        self.db.close()
