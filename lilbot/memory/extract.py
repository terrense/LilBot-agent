"""Periodic memory extraction.

Every few turns the agent asks the model to distill durable facts from the
recent conversation and writes them to the JSONL store. Extraction is
best-effort: any failure (bad JSON, provider error) is swallowed so it can never
break a turn. It is gated to capable providers — the offline rule provider
returns canned text and must not pollute memory.
"""
from __future__ import annotations

import json
from typing import Callable

from .store import MemoryStore

Extractor = Callable[[str, str], str]

VALID_KINDS = {"user", "feedback", "project", "reference", "note"}
# kind -> scope routing, mirroring  (user/feedback follow the user).
_USER_SCOPE = {"user", "feedback"}

MAX_NEW_PER_RUN = 5

EXTRACT_SYSTEM_PROMPT = "You extract durable memories from a conversation. Output JSON only."

EXTRACT_INSTRUCTION = """\
From the conversation below, extract any durable facts worth remembering for
future sessions. Use these kinds:
  * user      — who the user is, their role/preferences (follows the user)
  * feedback  — corrections or confirmed ways of working (follows the user)
  * project   — project knowledge, decisions, progress (this repo)
  * reference — external resources / links (this repo)

Only extract genuinely durable, non-obvious facts. Do not record one-off task
details, transient state, or anything already obvious from the code.

Respond with JSON only, exactly:
{"memories": [{"name": "...", "text": "...", "kind": "user|feedback|project|reference"}]}
If nothing is worth saving, return {"memories": []}."""


def _extract_json(raw: str) -> str:
    s = raw.strip()
    if s.startswith("{"):
        return s
    start = s.find("{")
    end = s.rfind("}")
    if start < 0 or end < start:
        return ""
    return s[start : end + 1]


def _scope_for(kind: str) -> str:
    return "user" if kind in _USER_SCOPE else "project"


def extract_memories(
    conversation_text: str,
    existing_index: str,
    extractor: Extractor,
    store: MemoryStore,
) -> list[str]:
    """Run one extraction pass, persisting new memories. Returns saved names.

    Duplicate names already present in the store are skipped.
    """
    if not conversation_text.strip():
        return []
    user_msg = (
        f"{EXTRACT_INSTRUCTION}\n\n"
        f"Existing memory names (avoid duplicates):\n{existing_index or '(none)'}\n\n"
        f"--- conversation ---\n{conversation_text}"
    )
    try:
        raw = extractor(EXTRACT_SYSTEM_PROMPT, user_msg)
    except Exception:
        return []
    clean = _extract_json(raw)
    if not clean:
        return []
    try:
        parsed = json.loads(clean)
    except json.JSONDecodeError:
        return []
    items = parsed.get("memories") if isinstance(parsed, dict) else None
    if not isinstance(items, list):
        return []

    existing_names = {e.name for e in store.list()}
    saved: list[str] = []
    for item in items[:MAX_NEW_PER_RUN]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        text = str(item.get("text") or "").strip()
        kind = str(item.get("kind") or "note").strip().lower()
        if not name or not text or name in existing_names:
            continue
        if kind not in VALID_KINDS:
            kind = "note"
        store.add(name=name, text=text, kind=kind, scope=_scope_for(kind))
        existing_names.add(name)
        saved.append(name)
    return saved
