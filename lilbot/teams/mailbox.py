"""File-based mailbox for inter-agent messaging.

Ported from mewcode's teams/mailbox.py. Each agent gets a ``{agent_id}.json``
inbox under *base_dir*, guarded by a companion ``.lock`` file (O_EXCL create,
10 retries, 10s staleness reclaim) so concurrent teammate threads — and even
separate processes — can append safely.
"""

from __future__ import annotations

import json
import os
import random
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable


@dataclass
class MailboxMessage:
    id: str
    from_agent: str
    to_agent: str
    content: str
    summary: str = ""
    message_type: str = "text"  # text | shutdown_request | shutdown_response
    timestamp: float = 0.0
    read: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MailboxMessage":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


class Mailbox:
    def __init__(self, base_dir: str | Path) -> None:
        self._base_dir = Path(base_dir)
        self._base_dir.mkdir(parents=True, exist_ok=True)

    @property
    def base_dir(self) -> Path:
        return self._base_dir

    def _inbox_path(self, agent_id: str) -> Path:
        return self._base_dir / f"{agent_id}.json"

    def _lock_path(self, agent_id: str) -> Path:
        return self._base_dir / f"{agent_id}.json.lock"

    def _with_lock(self, agent_id: str, fn: Callable[[list], list]) -> Any:
        lock_file = self._lock_path(agent_id)
        lock_fd = None
        last_err: Exception | None = None
        for _ in range(40):
            try:
                fd = os.open(str(lock_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
                lock_fd = fd
                os.close(fd)
                break
            except FileExistsError:
                try:
                    info = lock_file.stat()
                    if time.time() - info.st_mtime > 10:
                        lock_file.unlink(missing_ok=True)
                except OSError:
                    pass
                sleep_ms = 5 + random.randint(0, 95)
                time.sleep(sleep_ms / 1000)
            except PermissionError as e:
                # Windows raises this (not FileExistsError) when another thread is
                # mid-create/unlink of the lock file — transient, so retry.
                last_err = e
                sleep_ms = 5 + random.randint(0, 95)
                time.sleep(sleep_ms / 1000)
            except OSError as e:
                last_err = e
                break

        if lock_fd is None and last_err is not None:
            raise last_err

        try:
            messages = self._read_inbox(agent_id)
            messages = fn(messages)
            self._write_inbox(agent_id, messages)
        finally:
            try:
                lock_file.unlink(missing_ok=True)
            except OSError:
                pass  # Windows: another thread may already be reclaiming it

    def _read_inbox(self, agent_id: str) -> list[MailboxMessage]:
        path = self._inbox_path(agent_id)
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return [MailboxMessage.from_dict(item) for item in data]
        except (json.JSONDecodeError, KeyError, TypeError):
            return []

    def _write_inbox(self, agent_id: str, messages: list[MailboxMessage]) -> None:
        path = self._inbox_path(agent_id)
        data = json.dumps([m.to_dict() for m in messages], ensure_ascii=False, indent=2)
        path.write_text(data, encoding="utf-8")

    def write(self, agent_id: str, message: MailboxMessage) -> None:
        def _append(msgs: list[MailboxMessage]) -> list[MailboxMessage]:
            message.read = False
            if message.timestamp == 0.0:
                message.timestamp = time.time()
            msgs.append(message)
            return msgs
        self._with_lock(agent_id, _append)

    def read(self, agent_id: str) -> list[MailboxMessage]:
        """Return unread messages without marking them read."""
        return [m for m in self._read_inbox(agent_id) if not m.read]

    def consume(self, agent_id: str) -> list[MailboxMessage]:
        """Return unread messages and mark them read (thread-safe)."""
        result: list[MailboxMessage] = []

        def _mark_read(msgs: list[MailboxMessage]) -> list[MailboxMessage]:
            for m in msgs:
                if not m.read:
                    result.append(m)
                    m.read = True
            return msgs
        self._with_lock(agent_id, _mark_read)
        return result

    def broadcast(self, team_members: list[str], message: MailboxMessage, exclude: str = "") -> None:
        for agent_id in team_members:
            if agent_id == exclude:
                continue
            self.write(agent_id, message)

    def cleanup(self, agent_id: str) -> None:
        self._inbox_path(agent_id).unlink(missing_ok=True)
        self._lock_path(agent_id).unlink(missing_ok=True)

    def cleanup_all(self) -> None:
        if not self._base_dir.exists():
            return
        for f in self._base_dir.iterdir():
            f.unlink(missing_ok=True)


def create_message(
    from_agent: str,
    to_agent: str,
    content: str,
    summary: str = "",
    message_type: str = "text",
    metadata: dict[str, Any] | None = None,
) -> MailboxMessage:
    return MailboxMessage(
        id=uuid.uuid4().hex[:12],
        from_agent=from_agent,
        to_agent=to_agent,
        content=content,
        summary=summary,
        message_type=message_type,
        timestamp=time.time(),
        metadata=metadata or {},
    )
