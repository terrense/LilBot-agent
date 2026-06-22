"""Tests for LLM memory recall + extraction (ported from mewcode)."""
from __future__ import annotations

import json
import time

from lilbot.memory import MemoryStore
from lilbot.memory.extract import extract_memories
from lilbot.memory.recall import (
    freshness_text,
    memory_age,
    recall,
    select_relevant,
)


def _store(tmp_path):
    s = MemoryStore(tmp_path)
    s.add(name="user_role", text="User is a backend engineer who prefers pytest.", kind="user", scope="user")
    s.add(name="deploy_cmd", text="Deploy with make ship.", kind="project", scope="project")
    s.add(name="api_key_loc", text="Keys live in .env, never commit.", kind="reference", scope="project")
    return s


def test_memory_age_and_freshness():
    now = time.time()
    assert memory_age(now) == "today"
    assert memory_age(now - 86_400) == "yesterday"
    assert "days ago" in memory_age(now - 5 * 86_400)
    assert freshness_text(now) == ""
    assert "5 days old" in freshness_text(now - 5 * 86_400)


def test_select_relevant_filters_to_valid_ids(tmp_path):
    store = _store(tmp_path)
    entries = store.list()
    target = entries[0]

    def selector(_system, _user):
        # Return one valid id plus one bogus id; bogus must be dropped.
        return json.dumps({"selected": [target.id, "nope"]})

    ids = select_relevant("anything", entries, None, selector)
    assert ids == [target.id]


def test_select_relevant_handles_bad_json(tmp_path):
    store = _store(tmp_path)
    ids = select_relevant("q", store.list(), None, lambda s, u: "not json at all")
    assert ids == []


def test_recall_renders_reminder_and_tracks_surfaced(tmp_path):
    store = _store(tmp_path)
    entries = store.list()
    chosen = entries[0]

    def selector(_s, _u):
        return json.dumps({"selected": [chosen.id]})

    reminder, ids = recall("help me deploy", entries, ["bash"], set(), selector)
    assert chosen.name in reminder
    assert ids == [chosen.id]

    # Already-surfaced memories are not offered again.
    reminder2, ids2 = recall("again", entries, None, set(ids), selector)
    assert ids2 == [] or chosen.id not in ids2


def test_extract_persists_new_memories(tmp_path):
    store = MemoryStore(tmp_path)

    def extractor(_s, _u):
        return json.dumps({"memories": [
            {"name": "likes_tabs", "text": "User prefers tabs over spaces.", "kind": "feedback"},
            {"name": "proj_goal", "text": "Ship v2 by Friday.", "kind": "project"},
        ]})

    saved = extract_memories("user: I prefer tabs", "(none)", extractor, store)
    assert set(saved) == {"likes_tabs", "proj_goal"}
    by_name = {e.name: e for e in store.list()}
    assert by_name["likes_tabs"].scope == "user"   # feedback -> user scope
    assert by_name["proj_goal"].scope == "project"


def test_extract_skips_duplicates(tmp_path):
    store = MemoryStore(tmp_path)
    store.add(name="dup", text="old", kind="note", scope="project")

    def extractor(_s, _u):
        return json.dumps({"memories": [{"name": "dup", "text": "new", "kind": "project"}]})

    saved = extract_memories("conv", "- dup", extractor, store)
    assert saved == []


def test_extract_swallows_bad_json(tmp_path):
    store = MemoryStore(tmp_path)
    saved = extract_memories("conv", "", lambda s, u: "garbage", store)
    assert saved == []
