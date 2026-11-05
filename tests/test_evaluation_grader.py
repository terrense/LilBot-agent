from pathlib import Path

import pytest

from evaluation.grader import GraderError, grade
from evaluation.metrics import make_result, summarize


def test_graders_check_workspace_and_observed_attempts(tmp_path: Path):
    (tmp_path / "result.txt").write_bytes(b"status: ready\r\nvalue: 42\r\n")

    checks = grade(
        (
            {"type": "file_exists", "path": "result.txt"},
            {"type": "file_contains", "path": "result.txt", "text": "ready\nvalue: 42"},
            {"type": "tool_called", "name": "write_file"},
            {"type": "forbidden_tool_not_called", "name": "exec_shell"},
            {"type": "run_completed"},
        ),
        tmp_path,
        tool_names=["write_file"],
        completed=True,
    )

    assert [check["passed"] for check in checks] == [True] * 5


def test_missing_file_and_forbidden_attempt_are_failed_checks(tmp_path: Path):
    checks = grade(
        (
            {"type": "file_exists", "path": "missing.txt"},
            {"type": "file_contains", "path": "missing.txt", "text": "expected"},
            {"type": "forbidden_tool_not_called", "name": "exec_shell"},
            {"type": "run_completed"},
        ),
        tmp_path,
        tool_names=["exec_shell"],
        completed=False,
    )

    assert [check["passed"] for check in checks] == [False] * 4


def test_unreadable_text_is_a_grader_error_without_echoing_content(tmp_path: Path):
    (tmp_path / "broken.txt").write_bytes(b"\xff private-evidence")

    with pytest.raises(GraderError, match="Check 1 requires a UTF-8 file") as error:
        grade(
            ({"type": "file_contains", "path": "broken.txt", "text": "secret"},),
            tmp_path,
            tool_names=[],
            completed=True,
        )

    assert "private-evidence" not in str(error.value)
    assert "secret" not in str(error.value)


def test_path_outside_workspace_cannot_be_graded(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (tmp_path / "outside.txt").write_text("sentinel", encoding="utf-8")

    with pytest.raises(GraderError, match="could not read or validate"):
        grade(
            ({"type": "file_exists", "path": "../outside.txt"},),
            workspace,
            tool_names=[],
            completed=True,
        )


def test_unknown_completion_is_a_grader_error(tmp_path: Path):
    with pytest.raises(GraderError, match="completion evidence is unavailable"):
        grade(({"type": "run_completed"},), tmp_path, tool_names=[], completed=None)


def test_summary_separates_pass_fail_and_errors_and_preserves_missing(monkeypatch):
    monkeypatch.setattr("evaluation.metrics._git_commit", lambda: None)

    def result(number, success, error_type, latency, tool_calls, token_usage):
        return make_result(
            task_id=f"task{number}", trial_id=f"trial{number}", dataset_version="test-v1",
            success=success, latency=latency, tool_calls=tool_calls, token_usage=token_usage,
            error_type=error_type, trace_path=f"traces/trial{number}.jsonl", checks=[],
            final_output=None,
        )

    results = [
        result(1, True, None, 0.3, 2, {"prompt_tokens": 0}),
        result(2, False, "AGENT_FAILURE", 0.1, 0, None),
        result(3, None, "GRADER_ERROR", None, None, {"completion_tokens": 2}),
    ]
    summary = summarize(results)

    assert results[0]["git_commit"] is None
    assert results[1]["token_usage"] is None
    assert summary["passed"] == summary["failed"] == summary["errors"] == 1
    assert summary["success_rate"] == pytest.approx(1 / 3)
    assert summary["latency"]["median_seconds"] == pytest.approx(0.2)
    assert summary["latency"]["p95_seconds"] == 0.3
    assert summary["tool_calls"] == 2
    assert summary["tool_calls_coverage"] == 2
    assert summary["token_usage_coverage"] == 2
    assert summary["token_usage_totals"] == {"prompt_tokens": 0, "completion_tokens": 2}
    assert summary["token_usage_field_coverage"] == {"prompt_tokens": 1, "completion_tokens": 1}
    assert summarize([])["success_rate"] is None
    assert summarize([])["token_usage_totals"] is None
