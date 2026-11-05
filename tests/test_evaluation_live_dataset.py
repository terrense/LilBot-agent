import json
from pathlib import Path

import pytest

from evaluation.dataset import DatasetError, load_dataset


_LIVE_DATASET = Path(__file__).resolve().parents[1] / "evaluation" / "datasets" / "live.jsonl"


def _live_case(**changes):
    task = {
        "task_id": "live_example",
        "input": "Read the note.",
        "category": "read",
        "risk": "low",
        "success_criteria": "The answer includes the note.",
        "setup": {},
        "graders": [{"type": "final_contains", "text": "Note"}, {"type": "run_completed"}],
        "timeout": 60,
        "max_steps": 3,
    }
    task.update(changes)
    return task


def test_live_dataset_covers_all_categories_with_frozen_fixtures():
    dataset = load_dataset(_LIVE_DATASET, mode="live")
    assert 20 <= len(dataset.tasks) <= 30
    assert {task.category for task in dataset.tasks} == {
        "read", "search", "create", "modify", "multi_step", "error_recovery",
        "loop_guardrails", "permission", "memory_context",
    }
    assert all(task.success_criteria and task.risk in {"low", "medium"} for task in dataset.tasks)
    assert all("script" not in task.setup and task.max_steps <= 12 for task in dataset.tasks)
    assert all({"type": "run_completed"} in task.graders for task in dataset.tasks)
    assert b"E-4101" in dict(dataset.fixtures["live-operations"])["logs/api.txt"]
    assert len(dataset.version.removeprefix("sha256:")) == 64
    with pytest.raises(DatasetError):
        load_dataset(_LIVE_DATASET)


@pytest.mark.parametrize("changes", [
    {"risk": "high"},
    {"timeout": 181},
    {"max_steps": 13},
    {"success_criteria": ""},
    {"setup": {"script": [{"final": "mock only"}]}},
    {"setup": {"followup": ["one", "two", "three"]}},
    {"setup": {"memory": 1}},
    {"setup": {"writable_files": ["../outside.txt"]}},
    {"graders": [{"type": "memory_contains", "name": "tag", "text": "value"}]},
])
def test_live_loader_rejects_unsafe_or_invalid_trials(tmp_path, changes):
    path = tmp_path / "live.jsonl"
    path.write_text(json.dumps(_live_case(**changes)) + "\n", encoding="utf-8")
    with pytest.raises(DatasetError, match="live.jsonl:1"):
        load_dataset(path, mode="live")
