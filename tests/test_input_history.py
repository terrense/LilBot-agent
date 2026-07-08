"""Tests for the composer input-history navigation (Claude-Code-style recall)."""
from __future__ import annotations

from lilbot.tui.input_history import InputHistory


def test_record_dedups_and_skips_blank():
    h = InputHistory()
    h.record("first")
    h.record("first")     # consecutive dup collapsed
    h.record("")          # blank skipped
    h.record("second")
    assert h.items == ["first", "second"]


def test_up_walks_oldest_and_saves_draft():
    h = InputHistory()
    for line in ("a", "b", "c"):
        h.record(line)
    # Up from a live draft saves it and shows the newest entry first.
    assert h.older("draft") == "c"
    assert h.older("c") == "b"
    assert h.older("b") == "a"
    assert h.older("a") is None      # already oldest -> no change


def test_down_restores_draft_past_newest():
    h = InputHistory()
    for line in ("a", "b"):
        h.record(line)
    assert h.older("my draft") == "b"
    assert h.older("b") == "a"
    assert h.newer("a") == "b"
    assert h.newer("b") == "my draft"   # past newest -> draft restored
    assert h.newer("my draft") is None  # no longer navigating


def test_newer_without_navigation_is_noop():
    h = InputHistory()
    h.record("a")
    assert h.newer("draft") is None


def test_record_resets_navigation():
    h = InputHistory()
    h.record("a")
    h.record("b")
    assert h.older("d") == "b"
    h.record("c")               # submitting resets navigation
    assert h.older("d2") == "c"  # newest is now 'c', draft is fresh


def test_empty_history_noop():
    h = InputHistory()
    assert h.older("x") is None
    assert h.newer("x") is None
