import json
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from evaluation.dataset import (
    DatasetError,
    load_dataset,
    resolve_safe_path,
    safe_relative_path,
)


def _task(**overrides):
    return {
        "task_id": "example",
        "input": "Write the result.",
        "setup": {"script": [{"final": "Done."}]},
        "graders": [{"type": "run_completed"}],
        "timeout": 5,
        **overrides,
    }


def _jsonl(tmp_path, *tasks):
    path = tmp_path / "tasks.jsonl"
    path.write_text("".join(json.dumps(task) + "\n" for task in tasks), encoding="utf-8")
    return path


def test_load_smoke_dataset():
    path = Path(__file__).resolve().parents[1] / "evaluation" / "datasets" / "smoke.jsonl"
    dataset = load_dataset(path)
    assert 5 <= len(dataset.tasks) <= 10
    assert len({task.task_id for task in dataset.tasks}) == len(dataset.tasks)
    assert dataset.tasks[0].max_steps == 20
    assert dataset.version == load_dataset(path).version


def test_duplicate_task_id_is_rejected_with_line(tmp_path):
    path = _jsonl(tmp_path, _task(), _task())
    with pytest.raises(DatasetError, match=r"tasks\.jsonl:2: duplicate task_id"):
        load_dataset(path)


def test_rejects_non_json_and_duplicate_json_keys(tmp_path):
    path = tmp_path / "tasks.jsonl"
    path.write_text('{"task_id": nope}\n', encoding="utf-8")
    with pytest.raises(DatasetError, match="invalid JSON"):
        load_dataset(path)
    path.write_text('{"task_id":"example","task_id":"second"}\n', encoding="utf-8")
    with pytest.raises(DatasetError, match="duplicate JSON key"):
        load_dataset(path)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"timeout": float("inf")}, "timeout"),
        ({"timeout": True}, "timeout"),
        ({"timeout": 0}, "timeout"),
        ({"max_steps": 0}, "max_steps"),
        ({"graders": []}, "graders"),
        ({"graders": [{"type": "exec", "code": "print(42)"}]}, "unsupported grader"),
        ({"graders": [{"type": "file_exists", "path": "../outside.txt"}]}, "unsafe relative"),
        ({"setup": {"script": [{"python": "print(42)"}]}}, "invalid script operation"),
        (
            {"setup": {"script": [{"tool": "exec_shell", "args": {"command": "git status"}}]}},
            "invalid fields",
        ),
        (
            {"setup": {"script": [{"tool": "read_file", "args": {"path": "report.pdf"}}]}},
            r"\.txt",
        ),
        (
            {"setup": {"script": [{"tool": "write_file", "args": {"path": "../x.txt", "content": "x"}}]}},
            "unsafe relative",
        ),
    ],
)
def test_rejects_invalid_schema(tmp_path, overrides, message):
    with pytest.raises(DatasetError, match=message):
        load_dataset(_jsonl(tmp_path, _task(**overrides)))


@pytest.mark.parametrize(
    "path",
    [
        "../outside.txt", "out/../outside.txt", "/out.txt", "C:/out.txt", "C:out.txt",
        "//server/share/out.txt", r"out\file.txt", "out.txt:stream", "folder//out.txt",
        ".lilbot/events.txt", "a/.GIT/config", ".env", "con.txt", "a/COM¹.txt",
        "foo./out.txt", "out.txt ", "LILBOT~1/events.txt",
    ],
)
def test_rejects_nonportable_or_unsafe_relative_paths(path):
    with pytest.raises(DatasetError):
        safe_relative_path(path)


def test_safe_path_accepts_missing_file_and_rejects_reparse_parent(tmp_path, monkeypatch):
    assert resolve_safe_path(tmp_path, "docs/结果.txt") == tmp_path / "docs" / "结果.txt"
    original_lstat = Path.lstat

    def fake_lstat(path):
        if path == tmp_path / "docs":
            return SimpleNamespace(
                st_mode=stat.S_IFDIR, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT
            )
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", fake_lstat)
    with pytest.raises(DatasetError, match="reparse points"):
        resolve_safe_path(tmp_path, "docs/结果.txt")


def test_fixture_is_snapshotted_and_changes_dataset_version(tmp_path):
    path = _jsonl(tmp_path, _task(setup={"fixture": "sample", "script": [{"final": ""}]}))
    fixture = tmp_path / "fixtures" / "sample"
    fixture.mkdir(parents=True)
    note = fixture / "note.txt"
    note.write_bytes(b"first")
    first = load_dataset(path)
    note.write_bytes(b"second")
    second = load_dataset(path)
    assert first.fixtures["sample"] == (("note.txt", b"first"),)
    assert second.fixtures["sample"] == (("note.txt", b"second"),)
    assert first.version != second.version
