"""In-process long-running teammate loop (threading port of mewcode's asyncio version).

A teammate runs one full agent turn (reusing ``SubAgentManager.run_agent_turn`` so
it inherits gates / tool filtering / transcript), then goes idle, notifies the lead,
and polls its mailbox for the next prompt or a shutdown request — staying alive
across turns, unlike a one-shot subagent.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING, Any, Callable

from .mailbox import Mailbox, MailboxMessage

if TYPE_CHECKING:
    from .progress import TeammateProgress

log = logging.getLogger(__name__)

IDLE_POLL_INTERVAL = 0.5  # seconds, matches mewcode IdlePollInterval
SHUTDOWN_PREFIX = "[shutdown]"


def _is_shutdown(msg: MailboxMessage) -> bool:
    return msg.message_type == "shutdown_request" or msg.content.strip().startswith(SHUTDOWN_PREFIX)


def _inject_pending(mailbox: Mailbox, name: str) -> str:
    msgs = mailbox.consume(name)
    if not msgs:
        return ""
    parts = ["You have new messages from your team:"]
    for m in msgs:
        parts.append(f"From {m.from_agent}: {m.content}")
    return "\n".join(parts)


class InProcessTeammateHandle:
    def __init__(self, thread: threading.Thread, name: str, stop: threading.Event,
                 progress: "TeammateProgress") -> None:
        self.thread = thread
        self.name = name
        self._stop = stop
        self.progress = progress

    @property
    def done(self) -> bool:
        return not self.thread.is_alive()

    def cancel(self) -> None:
        self._stop.set()


def spawn_inprocess_teammate(
    *,
    run_one_turn: Callable[[str, "TeammateProgress"], str],
    name: str,
    team_name: str,
    mailbox: Mailbox,
    team_manager: Any,
    progress: "TeammateProgress",
    prompt: str,
    on_completed: Callable[[str], None] | None = None,
) -> InProcessTeammateHandle:
    """Start the teammate loop on a daemon thread and return a handle."""
    stop = threading.Event()

    def _wait_for_next(stop_evt: threading.Event) -> tuple[str, bool]:
        while not stop_evt.is_set():
            time.sleep(IDLE_POLL_INTERVAL)
            msgs = mailbox.consume(name)
            if not msgs:
                continue
            keep: list[MailboxMessage] = []
            for m in msgs:
                if _is_shutdown(m):
                    return "", True
                keep.append(m)
            if not keep:
                continue
            parts = ["You have new messages from your team:"]
            for m in keep:
                parts.append(f"From {m.from_agent}: {m.content}")
            return "\n".join(parts), False
        return "", True

    def _loop() -> None:
        next_prompt = prompt
        try:
            while not stop.is_set():
                pending = _inject_pending(mailbox, name)
                turn_prompt = (pending + "\n\n" + next_prompt).strip() if pending else next_prompt
                next_prompt = ""

                progress.status = "running"
                result = run_one_turn(turn_prompt, progress)

                progress.status = "idle"
                # Tell the lead this teammate finished a turn and is now free.
                summary = " ".join(str(result).split())[:160]
                team_manager.notify_lead(team_name, name, f"[idle] {name}: {summary}", f"{name} idle")
                team_manager.set_member_idle(team_name, name)

                new_prompt, shutdown = _wait_for_next(stop)
                if shutdown or stop.is_set():
                    progress.status = "completed"
                    return
                next_prompt = new_prompt
        except Exception as exc:  # noqa: BLE001 - defensive boundary for the thread
            progress.status = "failed"
            log.exception("Teammate '%s' loop crashed: %s", name, exc)
            try:
                team_manager.notify_lead(team_name, name, f"[failed] {name}: {exc}", f"{name} failed")
            except Exception:
                pass
        finally:
            if on_completed is not None:
                try:
                    on_completed(name)
                except Exception:
                    pass

    thread = threading.Thread(target=_loop, name=f"teammate-{name}", daemon=True)
    thread.start()
    log.info("Spawned in-process teammate '%s' in team '%s'", name, team_name)
    return InProcessTeammateHandle(thread, name, stop, progress)
