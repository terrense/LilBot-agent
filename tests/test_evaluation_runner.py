import json
from pathlib import Path

from evaluation import runner
from evaluation.dataset import load_dataset
from evaluation.trace import read_trace

_SMOKE = Path(__file__).resolve().parents[1] / "evaluation" / "datasets" / "smoke.jsonl"


def _task(task_id, script, *, graders=None, timeout=8, max_steps=20, writable_files=None):
    return {
        "task_id": task_id,
        "input": "Complete the test task.",
        "setup": {"script": script, "writable_files": writable_files or []},
        "graders": graders or [{"type": "run_completed"}],
        "timeout": timeout,
        "max_steps": max_steps,
    }


def _load(tmp_path, *tasks):
    dataset = tmp_path / "tasks.jsonl"
    dataset.write_text("".join(json.dumps(task) + "\n" for task in tasks), encoding="utf-8")
    return load_dataset(dataset)


def _results(dataset, output):
    return {result["task_id"]: result for result in runner.run_dataset(dataset, output)}


def test_smoke_real_tools_and_isolated_state(tmp_path):
    output = tmp_path / "report"
    results = _results(load_dataset(_SMOKE), output)
    assert results["read_fixture"]["success"] is True
    assert results["write_result"]["success"] is True
    assert results["incorrect_result"]["error_type"] == "AGENT_FAILURE"
    # A previous task wrote this same relative name, but it must be absent here.
    claimed = results["claimed_write"]
    assert claimed["error_type"] == "AGENT_FAILURE"
    assert claimed["checks"][0]["passed"] is False
    assert claimed["tool_calls"] == 0
    assert claimed["token_usage"] is None
    assert results["denied_write"]["success"] is True

    forbidden = results["forbidden_shell"]
    assert forbidden["error_type"] == "AGENT_FAILURE"
    trace = read_trace(output / forbidden["trace_path"])
    assert [event["event_type"] for event in trace] == [
        "runtime_started", "tool_started", "tool_finished", "runtime_finished", "trial_finished",
    ]
    assert trace[1]["tool_name"] == "exec_shell"
    assert trace[1]["tool_seq"] == trace[2]["tool_seq"]
    assert trace[2]["status"] == "failed"
    assert trace[2]["tool_result"] == "Unknown tool: exec_shell"
    assert trace[-1]["status"] == "FAIL"
    assert len({result["trial_id"] for result in results.values()}) == len(results)


def test_provider_timeout_and_grader_errors_are_distinct(tmp_path):
    dataset = _load(
        tmp_path,
        _task("provider_failure", [{"error": "provider_error"}]),
        _task("timeout", [
            {"tool": "read_file", "args": {"path": "missing.txt"}}, {"sleep": 12},
        ], timeout=4),
        _task("grader_error", [
            {"tool": "write_file", "args": {"path": "directory.txt/child.txt", "content": "42"}},
            {"final": "Done."},
        ], writable_files=["directory.txt/child.txt"], graders=[
            {"type": "file_contains", "path": "directory.txt", "text": "42"},
        ]),
        _task("after_errors", [{"final": "Finished."}]),
    )
    output = tmp_path / "report"
    results = _results(dataset, output)
    assert results["provider_failure"]["error_type"] == "AGENT_FAILURE"
    assert "ProviderError" in results["provider_failure"]["detail"]
    assert results["timeout"]["error_type"] == "TIMEOUT"
    timeout_trace = read_trace(output / results["timeout"]["trace_path"])
    assert any(event["event_type"] == "tool_finished" for event in timeout_trace)
    assert timeout_trace[-1]["status"] == "TIMEOUT"
    assert results["grader_error"]["error_type"] == "GRADER_ERROR"
    assert "requires a regular file" in results["grader_error"]["detail"]
    assert results["after_errors"]["success"] is True


def test_step_limit_and_infrastructure_error(tmp_path, monkeypatch):
    dataset = _load(
        tmp_path,
        _task("budget", [
            {"tool": "read_file", "args": {"path": "missing.txt"}},
            {"tool": "read_file", "args": {"path": "second.txt"}},
        ], max_steps=1),
    )
    row = _results(dataset, tmp_path / "budget")["budget"]
    assert row["termination_reason"] == "step_limit"
    assert row["tool_calls"] == 1
    assert row["error_type"] == "AGENT_FAILURE"

    def fail_start(*args):
        raise OSError("Injected worker startup failure")

    monkeypatch.setattr(runner, "_run_worker", fail_start)
    output = tmp_path / "infra"
    row = _results(dataset, output)["budget"]
    assert row["error_type"] == "INFRA_ERROR"
    assert row["tool_calls"] is None
    assert read_trace(output / row["trace_path"])[-1]["status"] == "INFRA_ERROR"


def test_host_secret_removed_from_tool_and_final_artifacts(tmp_path, monkeypatch):
    secret = "host-owned-sensitive-example-734911"
    monkeypatch.setenv("EXAMPLE_PASSWORD", secret)
    task = _task("redaction", [
        {"tool": "write_file", "args": {"path": "answer.txt", "content": secret}},
        {"final": f"Finished {secret}"},
    ], writable_files=["answer.txt"])
    output = tmp_path / "report"
    row = _results(_load(tmp_path, task), output)["redaction"]
    assert row["success"] is True
    assert secret not in row["final_output"]
    trace = (output / row["trace_path"]).read_text(encoding="utf-8")
    assert secret not in trace
    assert "REDACTED" in trace
