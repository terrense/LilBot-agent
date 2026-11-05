"""Incremental SQLite persistence for agent sessions.

Each message is committed in its own short transaction. SQLite's WAL mode lets
readers inspect a session while the agent appends new messages, and NORMAL
synchronous mode keeps the per-message write cost low without buffering a turn
in memory. A connection is reused while a turn is running and released when the
turn ends; one-off operations close it immediately so temporary workspaces can
release the database file on Windows.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _from_json(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


class SessionDB:
    """SQLite-backed append log plus a materialized session snapshot."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._initialized = False
        self._lock = threading.RLock()
        self._keep_connection = False
        self._active_connection: sqlite3.Connection | None = None

    @contextmanager
    def _connection(self, *, create: bool = True) -> Iterator[sqlite3.Connection | None]:
        if self._active_connection is not None:
            yield self._active_connection
            return
        if not create and not self.path.is_file():
            yield None
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), timeout=5.0, check_same_thread=False)
        keep = self._keep_connection
        retained = False
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout = 5000")
            if not self._initialized:
                mode = str(conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]).lower()
                if mode != "wal":
                    raise sqlite3.OperationalError(f"SQLite WAL mode unavailable (got {mode!r})")
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS sessions (
                        session_id TEXT PRIMARY KEY,
                        updated_at REAL NOT NULL,
                        message_count INTEGER NOT NULL DEFAULT 0,
                        usage_json TEXT NOT NULL DEFAULT '{}',
                        meta_json TEXT NOT NULL DEFAULT '{}'
                    );
                    CREATE TABLE IF NOT EXISTS messages (
                        session_id TEXT NOT NULL,
                        seq INTEGER NOT NULL,
                        message_json TEXT NOT NULL,
                        PRIMARY KEY (session_id, seq),
                        FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
                    );
                    CREATE INDEX IF NOT EXISTS idx_sessions_updated
                        ON sessions(updated_at DESC);
                    """
                )
                self._initialized = True
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute("PRAGMA foreign_keys = ON")
            if keep:
                self._active_connection = conn
                retained = True
            yield conn
        finally:
            if not retained:
                conn.close()

    def begin_turn(self) -> None:
        """Reuse one SQLite connection across a turn, while still committing each message."""
        with self._lock:
            if self._active_connection is not None:
                return
            self._keep_connection = True
            try:
                with self._connection():
                    pass
            except BaseException:
                self._keep_connection = False
                raise

    def end_turn(self) -> None:
        with self._lock:
            conn = self._active_connection
            self._active_connection = None
            self._keep_connection = False
            if conn is not None:
                conn.close()

    @staticmethod
    def _insert_messages(
        conn: sqlite3.Connection,
        session_id: str,
        messages: Iterable[dict[str, Any]],
    ) -> int:
        rows = [(session_id, seq, _json(message)) for seq, message in enumerate(messages)]
        conn.executemany(
            "INSERT INTO messages(session_id, seq, message_json) VALUES (?, ?, ?)",
            rows,
        )
        return len(rows)

    def ensure_session(
        self,
        session_id: str,
        messages: list[dict[str, Any]],
        usage: dict[str, int] | None = None,
        meta: dict[str, Any] | None = None,
    ) -> None:
        """Seed a new session (usually its system prompt) exactly once."""
        with self._lock, self._connection() as conn:
            assert conn is not None
            conn.execute("BEGIN IMMEDIATE")
            try:
                exists = conn.execute(
                    "SELECT 1 FROM sessions WHERE session_id = ?", (session_id,)
                ).fetchone()
                if exists is None:
                    now = time.time()
                    count = len(messages)
                    conn.execute(
                        """INSERT INTO sessions
                           (session_id, updated_at, message_count, usage_json, meta_json)
                           VALUES (?, ?, ?, ?, ?)""",
                        (session_id, now, count, _json(usage or {}), _json(meta or {})),
                    )
                    self._insert_messages(conn, session_id, messages)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def append_message(
        self,
        session_id: str,
        message: dict[str, Any],
        usage: dict[str, int] | None = None,
        meta: dict[str, Any] | None = None,
    ) -> int:
        """Append one message and commit it before returning; return its sequence."""
        with self._lock, self._connection() as conn:
            assert conn is not None
            conn.execute("BEGIN IMMEDIATE")
            try:
                now = time.time()
                conn.execute(
                    """INSERT INTO sessions
                       (session_id, updated_at, message_count, usage_json, meta_json)
                       VALUES (?, ?, 0, ?, ?)
                       ON CONFLICT(session_id) DO NOTHING""",
                    (session_id, now, _json(usage or {}), _json(meta or {})),
                )
                row = conn.execute(
                    "SELECT message_count FROM sessions WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                seq = int(row[0])
                conn.execute(
                    "INSERT INTO messages(session_id, seq, message_json) VALUES (?, ?, ?)",
                    (session_id, seq, _json(message)),
                )
                if usage is None and meta is None:
                    conn.execute(
                        "UPDATE sessions SET updated_at = ?, message_count = ? WHERE session_id = ?",
                        (now, seq + 1, session_id),
                    )
                else:
                    conn.execute(
                        """UPDATE sessions SET updated_at = ?, message_count = ?,
                           usage_json = ?, meta_json = ? WHERE session_id = ?""",
                        (now, seq + 1, _json(usage or {}), _json(meta or {}), session_id),
                    )
                conn.commit()
                return seq
            except BaseException:
                conn.rollback()
                raise

    def replace_messages(
        self,
        session_id: str,
        messages: list[dict[str, Any]],
        usage: dict[str, int] | None = None,
        meta: dict[str, Any] | None = None,
    ) -> None:
        """Replace the snapshot after compaction/reset, or during turn export."""
        with self._lock, self._connection() as conn:
            assert conn is not None
            conn.execute("BEGIN IMMEDIATE")
            try:
                now = time.time()
                conn.execute(
                    """INSERT INTO sessions
                       (session_id, updated_at, message_count, usage_json, meta_json)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(session_id) DO UPDATE SET
                         updated_at = excluded.updated_at,
                         message_count = excluded.message_count,
                         usage_json = excluded.usage_json,
                         meta_json = excluded.meta_json""",
                    (session_id, now, len(messages), _json(usage or {}), _json(meta or {})),
                )
                conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
                self._insert_messages(conn, session_id, messages)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def update_metadata(
        self,
        session_id: str,
        usage: dict[str, int],
        meta: dict[str, Any] | None = None,
    ) -> bool:
        """Update turn metadata without rewriting an already-appended history."""
        with self._lock, self._connection(create=False) as conn:
            if conn is None:
                return False
            cursor = conn.execute(
                """UPDATE sessions SET updated_at = ?, usage_json = ?, meta_json = ?
                   WHERE session_id = ?""",
                (time.time(), _json(usage), _json(meta or {}), session_id),
            )
            conn.commit()
            return cursor.rowcount > 0

    def load(self, session_id: str) -> dict[str, Any] | None:
        with self._lock, self._connection(create=False) as conn:
            if conn is None:
                return None
            row = conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            if row is None:
                return None
            messages = [
                _from_json(item[0], {})
                for item in conn.execute(
                    "SELECT message_json FROM messages WHERE session_id = ? ORDER BY seq",
                    (session_id,),
                )
            ]
            return {
                "session_id": session_id,
                "updated_at": float(row["updated_at"]),
                "messages": messages,
                "usage": _from_json(row["usage_json"], {}),
                "meta": _from_json(row["meta_json"], {}),
            }

    def list_sessions(self) -> list[dict[str, Any]]:
        with self._lock, self._connection(create=False) as conn:
            if conn is None:
                return []
            sessions = conn.execute(
                "SELECT session_id, updated_at, message_count FROM sessions ORDER BY updated_at DESC"
            ).fetchall()
            out: list[dict[str, Any]] = []
            for row in sessions:
                preview = ""
                rows = conn.execute(
                    "SELECT message_json FROM messages WHERE session_id = ? ORDER BY seq LIMIT 8",
                    (row["session_id"],),
                )
                for item in rows:
                    message = _from_json(item[0], {})
                    if message.get("role") == "user" and message.get("content"):
                        preview = " ".join(str(message["content"]).split())[:60]
                        break
                out.append({
                    "session_id": str(row["session_id"]),
                    "updated_at": float(row["updated_at"]),
                    "message_count": int(row["message_count"]),
                    "preview": preview,
                })
            return out

    def delete(self, session_id: str) -> bool:
        with self._lock, self._connection(create=False) as conn:
            if conn is None:
                return False
            cursor = conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
            conn.commit()
            return cursor.rowcount > 0

    def journal_mode(self) -> str:
        with self._lock, self._connection() as conn:
            assert conn is not None
            return str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()

    def synchronous(self) -> int:
        with self._lock, self._connection() as conn:
            assert conn is not None
            return int(conn.execute("PRAGMA synchronous").fetchone()[0])

    def close(self) -> None:
        self.end_turn()
