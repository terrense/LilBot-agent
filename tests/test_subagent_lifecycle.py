from __future__ import annotations

import json
import threading

import pytest

from lilbot.core.events import ProviderTurn
from lilbot.subagents import SubAgentManager


def test_terminal_status_is_published_after_final_transcript_and_snapshot(tmp_path):
    manager = SubAgentManager(lambda messages, tools: ProviderTurn(content="done"), tmp_path / "agents")
    finishing = threading.Event()
    release = threading.Event()
    append_transcript = manager._append_transcript

    def pause_terminal_event(task, event, data):
        if event == "completed":
            finishing.set()
            assert release.wait(3)
        append_transcript(task, event, data)

    manager._append_transcript = pause_terminal_event
    task = manager.open("writer", "draft", background=True)
    try:
        assert finishing.wait(3)
        assert task.status == "running"
        assert not task.terminal
    finally:
        release.set()
        manager.eval(task.id, block=True, timeout=5)

    assert task.status == "completed"
    assert task.finished_at is not None
    records = json.loads(manager.tasks_path.read_text(encoding="utf-8"))
    assert records[0]["status"] == "completed"
    assert records[0]["finished_at"] == task.finished_at
    assert manager.transcript(task.id)["events"][-1]["event"] == "completed"


@pytest.mark.parametrize("failure_point", ["transcript", "snapshot"])
def test_finalization_io_failure_publishes_failed_without_more_writes(tmp_path, failure_point):
    manager = SubAgentManager(lambda messages, tools: ProviderTurn(content="done"), tmp_path / "agents")
    append_transcript = manager._append_transcript
    persist_tasks = manager._persist_tasks_locked
    failed = False

    def fail_transcript(task, event, data):
        nonlocal failed
        assert not failed, "no transcript writes should follow finalization failure"
        if failure_point == "transcript" and event == "completed":
            failed = True
            raise PermissionError("terminal transcript unavailable")
        append_transcript(task, event, data)

    def fail_snapshot(task_snapshot=None):
        nonlocal failed
        assert not failed, "no task-state writes should follow finalization failure"
        if failure_point == "snapshot" and task_snapshot is not None:
            failed = True
            raise PermissionError("terminal snapshot unavailable")
        persist_tasks(task_snapshot)

    manager._append_transcript = fail_transcript
    manager._persist_tasks_locked = fail_snapshot
    task = manager.open("writer", "draft", background=False)

    assert failed
    assert task.status == "failed"
    assert task.terminal
    assert task.finished_at is not None
    assert task.last_event == "failed"
    assert "Subagent finalization failed (PermissionError)" in task.error
    assert f"terminal {failure_point} unavailable" in manager.projection(task)["error"]
    assert manager._semaphore.acquire(blocking=False)
    manager._semaphore.release()
