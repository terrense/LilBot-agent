"""Composer input history — up/down recall of previous prompts (Claude Code style).

Extracted from the dashboard so the navigation logic is unit-testable without
prompt_toolkit. The dashboard wires ``older``/``newer`` to Up/Down when the
cursor is at the first/last line of the composer (so multi-line editing still
works), and ``record`` on submit.

Semantics match a shell/Claude-Code history:
  * Up from a fresh composer saves the current draft, then walks newest→oldest.
  * Down walks back toward newest, and past the newest restores the saved draft.
  * Consecutive duplicates are collapsed; blank lines are never recorded.
"""
from __future__ import annotations


class InputHistory:
    def __init__(self, limit: int = 500) -> None:
        self._items: list[str] = []
        self._index: int | None = None   # None = editing a live draft
        self._draft: str = ""
        self._limit = limit

    @property
    def items(self) -> list[str]:
        return list(self._items)

    def record(self, line: str) -> None:
        """Add a submitted line and reset navigation to the live draft."""
        line = (line or "").rstrip("\n")
        if line and (not self._items or self._items[-1] != line):
            self._items.append(line)
            if len(self._items) > self._limit:
                self._items = self._items[-self._limit:]
        self._index = None
        self._draft = ""

    def older(self, current: str) -> str | None:
        """Move one step toward older history. Returns the text to show, or None
        when there is no older entry (leave the composer unchanged)."""
        if not self._items:
            return None
        if self._index is None:
            self._draft = current
            self._index = len(self._items) - 1
        elif self._index > 0:
            self._index -= 1
        else:
            return None  # already at the oldest entry
        return self._items[self._index]

    def newer(self, current: str) -> str | None:
        """Move one step toward newer history. Past the newest entry, restores
        the saved draft. Returns None when not currently navigating history."""
        if self._index is None:
            return None
        if self._index < len(self._items) - 1:
            self._index += 1
            return self._items[self._index]
        self._index = None
        return self._draft

    def reset(self) -> None:
        self._index = None
        self._draft = ""
