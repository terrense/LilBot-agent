"""LLM-based memory recall + freshness.

LilBot stores memories in a flat JSONL store. On its own that store only does
keyword search. This module adds the part  had and LilBot lacked:

  * a small side-query that asks the model which memories are actually relevant
    to the current user request (rather than dumping the newest few), and
  * point-in-time freshness warnings so the model treats stale memories as
    observations to re-verify, not live facts.

The selector is any callable ``(system_prompt, user_message) -> raw_text``; the
agent wires it to the provider. Failures are silent — recall is best-effort and
must never block or break a turn.
"""
from __future__ import annotations

import json
import time
from typing import Callable

from .store import MemoryEntry

Selector = Callable[[str, str], str]

MAX_SELECTED = 5

SELECTOR_SYSTEM_PROMPT = (
    "You are selecting memories that will help LilBot answer the user's current "
    "request. You are given the request and a list of stored memories (id, kind, "
    "scope, age, and a preview).\n\n"
    "Return the ids of the memories that are clearly useful for this request "
    f"(at most {MAX_SELECTED}). Be selective: if you are unsure a memory helps, "
    "leave it out; if none clearly help, return an empty list.\n"
    "If a list of recently used tools is given, do not select memories that are "
    "merely usage/reference docs for those tools, but DO keep warnings or gotchas "
    "about them.\n\n"
    'Respond with JSON only, exactly: {"selected": ["id1", "id2"]}'
)


def memory_age_days(created_at: float) -> int:
    if not created_at:
        return 0
    return max(0, int((time.time() - created_at) // 86_400))


def memory_age(created_at: float) -> str:
    d = memory_age_days(created_at)
    if d == 0:
        return "today"
    if d == 1:
        return "yesterday"
    return f"{d} days ago"


def freshness_text(created_at: float) -> str:
    d = memory_age_days(created_at)
    if d <= 1:
        return ""
    return (
        f"This memory is {d} days old. Memories are point-in-time observations, "
        "not live state — verify against current code/files before relying on it."
    )


def format_manifest(entries: list[MemoryEntry], recent_tools: list[str] | None) -> str:
    lines = []
    for e in entries:
        lines.append(
            f"- id={e.id} [{e.kind}/{e.scope}] ({memory_age(e.created_at)}): "
            f"{e.name} — {e.preview(140)}"
        )
    out = "\n".join(lines)
    if recent_tools:
        out += "\n\nRecently used tools: " + ", ".join(recent_tools)
    return out


def _extract_json(raw: str) -> str:
    s = raw.strip()
    if s.startswith("{"):
        return s
    start = s.find("{")
    end = s.rfind("}")
    if start < 0 or end < start:
        return ""
    return s[start : end + 1]


def select_relevant(
    query: str,
    entries: list[MemoryEntry],
    recent_tools: list[str] | None,
    selector: Selector,
) -> list[str]:
    if not entries:
        return []
    valid_ids = {e.id for e in entries}
    manifest = format_manifest(entries, recent_tools)
    user_message = f"User request: {query}\n\nStored memories:\n{manifest}"
    try:
        raw = selector(SELECTOR_SYSTEM_PROMPT, user_message)
    except Exception:
        return []
    clean = _extract_json(raw)
    if not clean:
        return []
    try:
        parsed = json.loads(clean)
    except json.JSONDecodeError:
        return []
    arr = parsed.get("selected") if isinstance(parsed, dict) else None
    if not isinstance(arr, list):
        return []
    out: list[str] = []
    for item in arr:
        if isinstance(item, str) and item in valid_ids and item not in out:
            out.append(item)
    return out[:MAX_SELECTED]


def render_reminder(entries: list[MemoryEntry]) -> str:
    if not entries:
        return ""
    parts = ["The following stored memories may be relevant to this request:\n"]
    for e in entries:
        parts.append(f"## Memory {e.id} [{e.kind}/{e.scope}] (saved {memory_age(e.created_at)})")
        parts.append(f"{e.name}: {e.text}")
        note = freshness_text(e.created_at)
        if note:
            parts.append(note)
        parts.append("")
    return "\n".join(parts).strip()


def recall(
    query: str,
    entries: list[MemoryEntry],
    recent_tools: list[str] | None,
    already_surfaced: set[str] | None,
    selector: Selector,
) -> tuple[str, list[str]]:
    """Pick relevant, not-yet-surfaced memories and render a reminder.

    Returns (reminder_text, surfaced_ids). reminder_text is "" when nothing was
    selected.
    """
    surfaced = already_surfaced or set()
    candidates = [e for e in entries if e.id not in surfaced]
    if not candidates:
        return "", []
    selected_ids = select_relevant(query, candidates, recent_tools, selector)
    if not selected_ids:
        return "", []
    by_id = {e.id: e for e in candidates}
    chosen = [by_id[i] for i in selected_ids if i in by_id]
    return render_reminder(chosen), [e.id for e in chosen]
