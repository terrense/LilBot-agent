"""Structured main-loop event log (CC's logEvent analogue, #18).

A best-effort JSONL sink at ``.lilbot/events.jsonl``. Every record is one line:
``{"ts", "event", ...payload}`` with a namespaced event name (``lilbot_turn_start``,
``lilbot_tool_call``, ``lilbot_compaction``, ``lilbot_recovery`` …). This is the
raw material for the exact metrics the resume claims (tool success rate, failure
recovery rate, complex-task completion) and for bad-case analysis — the point CC
makes with its telemetry→decision→code loop.

Design rules (all from CC's telemetry discipline):
  * never raise into the agent loop — logging is not the critical path;
  * only whitelisted scalar fields are written, so no file paths / secrets /
    large blobs leak into analytics (CC types its metadata for exactly this);
  * one file per session dir, opened append-per-write (crash-safe, no handle to
    leak across a long session).
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

# Only these value types are ever serialized — a guard against dumping arbitrary
# objects (and, with the caller's field discipline, against leaking content).
_SCALAR = (str, int, float, bool, type(None))


class EventLog:
    def __init__(self, state_dir: Path | None) -> None:
        self.path: Path | None = (Path(state_dir) / "events.jsonl") if state_dir else None

    def enabled(self) -> bool:
        return self.path is not None

    def log(self, event: str, **fields: Any) -> None:
        """Append one namespaced event. Silently no-ops without a state dir."""
        if self.path is None:
            return
        record: dict[str, Any] = {"ts": round(time.time(), 3), "event": f"lilbot_{event}"}
        for key, value in fields.items():
            if isinstance(value, _SCALAR):
                record[key] = value
            else:
                # Never serialize non-scalars verbatim (avoids leaking content /
                # unbounded blobs); record only their type so the shape is visible.
                record[key] = f"<{type(value).__name__}>"
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass  # logging must never break a turn

    def read_all(self) -> list[dict[str, Any]]:
        """Read back every event (for analysis / tests)."""
        if self.path is None or not self.path.exists():
            return []
        out: list[dict[str, Any]] = []
        try:
            for line in self.path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        except OSError:
            return []
        return out
