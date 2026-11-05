import json

from lilbot.memory import FileMemoryStore

from evaluation.grader import grade
from evaluation.metrics import classify_failure, make_result, provider_observations, summarize, tool_observations
from evaluation.report import write_report
from evaluation.trace import TraceWriter


def test_live_checks_use_final_output_tool_results_and_trial_memory(tmp_path):
    (tmp_path / "answer.txt").write_bytes(b"42\r\n")
    (tmp_path / "protected.txt").mkdir()
    state = tmp_path / ".lilbot"
    store = FileMemoryStore(state, user_dir=state / "eval-user-memory")
    store.add("Owner", "Visible only to the trial", kind="user")
    checks = grade(
        (
            {"type": "file_equals", "path": "answer.txt", "text": "42\n"},
            {"type": "file_equals", "path": "answer.txt", "text": "42"},
            {"type": "file_not_exists", "path": "protected.txt"},
            {"type": "file_not_exists", "path": "missing.txt"},
            {"type": "final_contains", "text": "ANSWER 42"},
            {"type": "memory_contains", "name": "owner", "text": "only to the trial"},
            {"type": "tool_succeeded", "name": "write_file"},
            {"type": "tool_failed", "name": "read_file"},
            {"type": "guardrail_observed", "name": "read_file", "kind": "repeated_failure_warning"},
            {"type": "guardrail_observed", "name": "read_file", "kind": "repeated_failure_block"},
        ),
        tmp_path, tool_names=["read_file", "write_file"], completed=True, final_output="Answer\n 42.",
        events=[
            {"event_type": "tool_finished", "tool_name": "read_file", "status": "failed", "guardrails": ["repeated_failure_warning"]},
            {"event_type": "tool_finished", "tool_name": "write_file", "status": "ok"},
        ],
    )

    assert [row["passed"] for row in checks] == [True, False, False, True, True, True, True, True, True, False]


def test_observed_counts_exclude_unfinished_results_and_retain_usage_coverage():
    events = [
        {"event_type": "provider_started"},
        {"event_type": "provider_finished", "status": "ok", "usage": {"prompt_tokens": 11, "completion_tokens": 0}},
        {"event_type": "tool_started"},
        {"event_type": "tool_finished", "status": "ok"},
        {"event_type": "tool_started"},
        {"event_type": "tool_finished", "status": "failed"},
        {"event_type": "tool_started"},
        {"event_type": "provider_started"},
        {"event_type": "provider_finished", "status": "failed"},
        {"event_type": "provider_started"},
    ]
    observations = provider_observations(events)
    assert observations == {
        "provider_requests": 3,
        "provider_finished": 2,
        "provider_failures": 1,
        "provider_usage_count": 1,
        "provider_usage_totals": {"prompt_tokens": 11, "completion_tokens": 0},
        "provider_usage_field_coverage": {"prompt_tokens": 1, "completion_tokens": 1},
    }
    result = make_result(
        task_id="count", trial_id="count_trial", dataset_version="test", success=False, latency=1,
        tool_calls=3, token_usage=observations["provider_usage_totals"], error_type="TIMEOUT",
        trace_path="traces/count.jsonl", checks=[], final_output=None, mode="live", model_name="test-live",
    )
    result.update(tool_observations(events))
    result.update(observations)
    summary = summarize([result])
    assert summary["tool_calls"] == 3
    assert summary["tool_failure_rate"] == 0.5
    assert summary["provider_usage_count"] == 1
    assert summary["provider_usage_field_coverage"] == {"prompt_tokens": 1, "completion_tokens": 1}


def test_failure_attribution_uses_observed_policy_and_avoids_recovered_tool_blame():
    events = [
        {"event_type": "tool_finished", "tool_name": "read_file", "status": "failed", "failure_kind": "tool", "seq": 1},
        {"event_type": "tool_finished", "tool_name": "read_file", "status": "ok", "seq": 2},
    ]
    checks = [{"index": 1, "type": "final_contains", "passed": False}]
    assert classify_failure("AGENT_FAILURE", checks, events)["category"] == "model"
    assert classify_failure("AGENT_FAILURE", checks, events[:1])["category"] == "tool"
    assert classify_failure("GRADER_ERROR", checks, events)["category"] == "grader"
    assert classify_failure("TIMEOUT", checks, events)["category"] == "environment"
    assert classify_failure("AGENT_FAILURE", [{"index": 1, "type": "forbidden_tool_not_called", "passed": False}], events)["category"] == "permission"
    assert classify_failure("AGENT_FAILURE", [], [
        {"event_type": "provider_finished", "status": "failed", "reason": "rate_limit", "seq": 1}
    ])["reason"] == "rate_limit"


def test_failure_report_embeds_all_published_steps_and_trace_link(tmp_path):
    trace_path = "traces/failed.jsonl"
    with TraceWriter(tmp_path / trace_path, "failed") as trace:
        trace.record("runtime_started", status="running")
        trace.record("tool_started", tool_name="read_file", tool_args={"path": "missing.txt"})
        trace.record("tool_finished", tool_name="read_file", status="failed", tool_result="Missing")
        trace.record("trial_finished", status="FAIL")
    result = make_result(
        task_id="missing", trial_id="failed", dataset_version="test", success=False, latency=1.2,
        tool_calls=1, token_usage=None, error_type="AGENT_FAILURE", trace_path=trace_path, checks=[],
        final_output=None, mode="live", model_name="test-live", category="file_read", risk="low",
    )
    write_report(tmp_path, [result], metadata={"dataset": "test"})
    report = (tmp_path / "report.md").read_text(encoding="utf-8")
    payload = json.loads((tmp_path / "results.json").read_text(encoding="utf-8"))

    assert report.startswith("# LilBot live evaluation")
    assert f"[Published trace]({trace_path})" in report
    for line in (tmp_path / trace_path).read_text(encoding="utf-8").splitlines():
        assert line in report
    assert payload["run"] == {"dataset": "test"}
