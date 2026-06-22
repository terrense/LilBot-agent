"""Tests for the file-based memory store (ported from mewcode memdir)."""
from __future__ import annotations

from lilbot.memory import FileMemoryStore, MemoryStore
from lilbot.memory.file_store import ENTRYPOINT


def _store(tmp_path):
    return FileMemoryStore(tmp_path / ".lilbot", user_dir=tmp_path / "userhome" / "memory")


def test_add_and_list_roundtrip(tmp_path):
    s = _store(tmp_path)
    e = s.add(name="deploy", text="Deploy with make ship.", kind="project")
    got = s.list()
    assert len(got) == 1
    assert got[0].name == "deploy"
    assert got[0].text == "Deploy with make ship."
    assert got[0].id == e.id


def test_kind_routes_to_user_or_project_dir(tmp_path):
    s = _store(tmp_path)
    s.add(name="likes_pytest", text="prefers pytest", kind="user")
    s.add(name="repo_layout", text="src/ holds code", kind="project")
    user_files = list((tmp_path / "userhome" / "memory").glob("*.md"))
    proj_files = list((tmp_path / ".lilbot" / "memory").glob("*.md"))
    user_names = [p.name for p in user_files if p.name != ENTRYPOINT]
    proj_names = [p.name for p in proj_files if p.name != ENTRYPOINT]
    assert any("likes" in n for n in user_names)
    assert any("repo" in n for n in proj_names)
    # user/feedback gets user scope
    by_name = {e.name: e for e in s.list()}
    assert by_name["likes_pytest"].scope == "user"
    assert by_name["repo_layout"].scope == "project"


def test_index_file_written(tmp_path):
    s = _store(tmp_path)
    s.add(name="thing", text="some detail", kind="project")
    index = (tmp_path / ".lilbot" / "memory" / ENTRYPOINT).read_text(encoding="utf-8")
    assert "thing" in index
    assert "Memory Index" in index


def test_delete_by_name_and_id(tmp_path):
    s = _store(tmp_path)
    e1 = s.add(name="a", text="t1", kind="project")
    s.add(name="b", text="t2", kind="project")
    assert s.delete("a") is True
    assert s.delete(e1.id) is False  # already gone
    assert [e.name for e in s.list()] == ["b"]


def test_search(tmp_path):
    s = _store(tmp_path)
    s.add(name="db", text="postgres connection pooling notes", kind="project")
    s.add(name="ui", text="react component styling", kind="project")
    hits = s.search("postgres pooling")
    assert hits and hits[0].name == "db"


def test_files_are_plaintext_markdown(tmp_path):
    s = _store(tmp_path)
    s.add(name="readable", text="human can edit this", kind="project")
    f = next(p for p in (tmp_path / ".lilbot" / "memory").glob("*.md") if p.name != ENTRYPOINT)
    raw = f.read_text(encoding="utf-8")
    assert raw.startswith("---")
    assert "human can edit this" in raw


def test_drop_in_with_recall(tmp_path):
    # FileMemoryStore.list() yields the same MemoryEntry recall expects.
    from lilbot.memory.recall import recall
    import json
    s = _store(tmp_path)
    e = s.add(name="pref", text="user likes tabs", kind="user")
    reminder, ids = recall(
        "format the file", s.list(), None, set(),
        lambda sys, user: json.dumps({"selected": [e.id]}),
    )
    assert e.id in ids
    assert "user likes tabs" in reminder


def test_migration_from_jsonl(tmp_path):
    legacy = MemoryStore(tmp_path / ".lilbot")
    legacy.add(name="old1", text="legacy note", kind="project")
    s = _store(tmp_path)
    n = s.import_from(legacy)
    assert n == 1
    assert any(e.name == "old1" for e in s.list())
