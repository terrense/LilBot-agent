"""Session-memory living document (CC's SessionMemory analogue, #12).

A single structured Markdown file, ``.lilbot/session-memory.md``, with a FIXED
set of sections. Instead of re-summarizing the whole conversation from scratch
each time, the model **incrementally updates the sections that changed** — the
template is the schema, so updates are small, diffable, and preserve structure.

Adaptation to LilBot: CC hands a fork subagent the Edit tool to patch the file
in place. LilBot doesn't need that machinery — a side-query returns a JSON
``{section: new_content}`` of only the changed sections, and ``merge_updates``
writes them back while preserving every header and the untouched sections. Same
property (incremental, template-preserving), less plumbing.

The document survives compaction: the agent feeds it into the recovery
attachment, so after the transcript is summarized the model still has its
running notes.
"""
from __future__ import annotations

import re
from pathlib import Path

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
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(self.render(sections), encoding="utf-8")
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

    def text(self) -> str:
        """The document text, or '' when it has no real content yet."""
        return "" if self.is_empty() else self.render()


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
