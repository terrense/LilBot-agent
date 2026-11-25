"""Session-memory living document (Session-memory living document, #12).

A single structured Markdown file, ``.lilbot/session-memory.md``, with a FIXED
set of sections. Instead of re-summarizing the whole conversation from scratch
each time, the model **incrementally updates the sections that changed** — the
template is the schema, so updates are small, diffable, and preserve structure.

Adaptation to LilBot: A fork subagent is handed the Edit tool to patch the file
in place. LilBot doesn't need that machinery — a side-query returns a JSON
``{section: new_content}`` of only the changed sections, and ``merge_updates``
writes them back while preserving every header and the untouched sections. Same
property (incremental, template-preserving), less plumbing.

The document survives compaction: the agent feeds it into the recovery
attachment, so after the transcript is summarized the model still has its
running notes.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

# Title used when the document rides along in a compaction's recovery
# attachment. Shared with the compaction layer so the same note is never
# attached twice.
SESSION_MEMORY_NOTE_TITLE = "Session memory (running notes)"

# How much conversation may accumulate before the living document is rewritten.
# A turn count alone is a poor clock: one turn can add a hundred tool calls or
# none at all. Token and tool-call thresholds track the two things that
# actually change the session's state.
SESSION_MEMORY_TOKEN_THRESHOLD = 8_000
SESSION_MEMORY_TOOL_CALL_THRESHOLD = 12

# A handoff needs state, task and worklog — not a single populated line. Below
# this many populated sections the document cannot stand in for a prefix
# summary, so the summary path runs instead.
MIN_SESSION_MEMORY_SECTIONS = 3

# Ordered sections + their template guidance (shown until real content lands).
SECTIONS: list[tuple[str, str]] = [
    ("Session Title", "A short, distinctive 5-10 word title for this session."),
    ("Current State", "What is being worked on right now; immediate next steps."),
    ("Task Specification", "What the user asked to build; key decisions / constraints."),
    ("Files and Functions", "Important files/functions and why they matter."),
    ("Errors and Corrections", "Errors hit and how they were fixed; user corrections."),
    ("Learnings", "What worked, what didn't, what to avoid."),
    ("Worklog", "Terse step-by-step of what was attempted/done."),
]
_SECTION_NAMES = [name for name, _ in SECTIONS]
_PLACEHOLDER = {name: f"_{desc}_" for name, desc in SECTIONS}

_HEADER_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


class SessionMemory:
    def __init__(self, state_dir: Path | None) -> None:
        self.path: Path | None = (Path(state_dir) / "session-memory.md") if state_dir else None

    # -- parse / render ---------------------------------------------------

    def load(self) -> dict[str, str]:
        """Return {section: content}. Missing/absent file -> template placeholders."""
        content = ""
        if self.path is not None and self.path.exists():
            try:
                content = self.path.read_text(encoding="utf-8")
            except OSError:
                content = ""
        parsed = _parse_sections(content)
        # Fill any missing section with its placeholder so the shape is stable.
        return {name: parsed.get(name, _PLACEHOLDER[name]) for name in _SECTION_NAMES}

    def render(self, sections: dict[str, str] | None = None) -> str:
        data = sections if sections is not None else self.load()
        parts = ["# Session Memory", ""]
        for name in _SECTION_NAMES:
            parts.append(f"## {name}")
            parts.append((data.get(name) or _PLACEHOLDER[name]).strip())
            parts.append("")
        return "\n".join(parts).strip() + "\n"

    def save(self, sections: dict[str, str]) -> None:
        """Write the document atomically.

        The file is written to a sibling temp file, flushed to disk, and then
        moved into place with ``os.replace``. A reader therefore always sees
        either the previous document or the new one — never a half-written
        file — and a crash mid-write leaves the old document intact.
        """
        if self.path is None:
            return
        tmp: Path | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(self.render(sections))
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except OSError:
            if tmp is not None:
                try:
                    tmp.unlink()
                except OSError:
                    pass

    # -- incremental update ----------------------------------------------

    def merge_updates(self, updates: dict[str, str]) -> dict[str, str]:
        """Apply only the changed sections; keep headers and untouched sections.

        Unknown section names in ``updates`` are ignored (template is the schema).
        Returns the merged sections and persists them.
        """
        current = self.load()
        for name, value in (updates or {}).items():
            if name in _SECTION_NAMES and isinstance(value, str) and value.strip():
                current[name] = value.strip()
        self.save(current)
        return current

    def is_empty(self) -> bool:
        """True when no section has real content yet (all still placeholders)."""
        data = self.load()
        return all(data[name].strip() == _PLACEHOLDER[name] for name in _SECTION_NAMES)

    def populated_sections(self) -> int:
        """How many sections carry real content (not the template placeholder)."""
        data = self.load()
        return sum(
            1 for name in _SECTION_NAMES
            if data[name].strip() and data[name].strip() != _PLACEHOLDER[name]
        )

    def is_handoff_ready(self) -> bool:
        """True when the document can stand in for a compacted prefix.

        A handoff has to carry state, task and worklog. A document with only a
        title (or a title and a stray line) would silently replace a rich
        conversation with a near-empty note, so it is rejected here and the
        summary path runs instead.
        """
        return self.populated_sections() >= MIN_SESSION_MEMORY_SECTIONS

    def text(self) -> str:
        """The document text, or '' when it has no real content yet."""
        return "" if self.is_empty() else self.render()

    def handoff_text(self) -> str:
        """The document with the unfilled sections removed.

        The template placeholders ("_What is being worked on right now_") are
        instructions to the writer, not content. Carrying them into a handoff
        would tell the next model that the section is empty, which is worse
        than not mentioning it at all.
        """
        data = self.load()
        parts = ["# Session Memory", ""]
        for name in _SECTION_NAMES:
            body = data[name].strip()
            if not body or body == _PLACEHOLDER[name]:
                continue
            parts.append(f"## {name}")
            parts.append(body)
            parts.append("")
        if len(parts) <= 2:
            return ""
        return "\n".join(parts).strip() + "\n"


def _parse_sections(content: str) -> dict[str, str]:
    """Split a rendered document back into {section: body}."""
    out: dict[str, str] = {}
    matches = list(_HEADER_RE.finditer(content or ""))
    for i, m in enumerate(matches):
        name = m.group(1).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        out[name] = content[start:end].strip()
    return out
