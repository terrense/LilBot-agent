"""A 40-case dangerous-command corpus: every entry must be gated."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lilbot.sandbox.execpolicy import classify

CORPUS = Path(__file__).parent / "corpora" / "dangerous_commands.json"
CASES = json.loads(CORPUS.read_text(encoding="utf-8"))


def test_corpus_has_at_least_40_cases():
    assert len(CASES) >= 40


@pytest.mark.parametrize(
    "case",
    CASES,
    ids=[f"{index:02d}-{case['command'][:28]}" for index, case in enumerate(CASES)],
)
def test_dangerous_command_is_gated(case):
    decision, reason = classify(case["command"])
    assert decision in {"deny", "ask"}, (case["command"], decision, reason)
    expected = case.get("expected")
    if expected:
        assert decision == expected, (case["command"], decision, reason)
