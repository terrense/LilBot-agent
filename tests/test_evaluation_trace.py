import json

from evaluation.trace import TOOL_RESULT_LIMIT, TraceWriter, publish_trace, read_trace


def test_trace_limits_redacts_and_keeps_unknown_fields_null(tmp_path, monkeypatch):
    monkeypatch.setenv("LILBOT_API_KEY", "synthetic-environment-secret")
    path = tmp_path / "trace.jsonl"
    with TraceWriter(path, "trial-1") as trace:
        trace.record(
            "tool_started",
            tool_name="write_file",
            tool_args={
                "password": "abc",
                "nested": {"authorization": "Bearer very-short", "reasoning_content": "private reasoning"},
                "content": "API_KEY: top-secret\nactual=synthetic-environment-secret\nAuthorization: Basic basic-value",
            },
        )
        trace.record(
            "tool_finished", tool_name="write_file", tool_result="x" * 3000,
            status="ok", reasoning_content="private reasoning",
        )

    raw = path.read_text(encoding="utf-8")
    for secret in ("abc", "very-short", "top-secret", "synthetic-environment-secret", "private reasoning", "basic-value"):
        assert secret not in raw
    start, finish = read_trace(path)
    assert start["trial_id"] == "trial-1"
    assert start["tool_result"] is start["final_output"] is start["status"] is None
    assert start["tool_args"]["password"] == "[REDACTED]"
    assert start["tool_args"]["nested"] == {"authorization": "[REDACTED]"}
    assert len(finish["tool_result"]) == TOOL_RESULT_LIMIT
    assert finish["seq"] == 2
    assert finish["timestamp"].endswith("+00:00")


def test_publish_redacts_host_secret_and_keeps_partial_trace_prefix(tmp_path, monkeypatch):
    source = tmp_path / "worker.jsonl"
    with TraceWriter(source, "trial", staging=True) as trace:
        trace.record("tool_finished", tool_result="synthetic-opaque-value", usage={"total_tokens": 0})
        trace.record("tool_finished", tool_result="x" * (TOOL_RESULT_LIMIT - 18) + "synthetic-opaque-value")
    with source.open("ab") as handle:
        handle.write(b'{"event_type":')
    monkeypatch.setenv("TEST_API_KEY", "synthetic-opaque-value")

    destination = tmp_path / "traces" / "trial.jsonl"
    publish_trace(source, destination, "trial", status="TIMEOUT", final_output=None)
    event, boundary, terminal = read_trace(destination)
    assert event["timestamp"] == json.loads(source.read_bytes().splitlines()[0])["timestamp"]
    assert event["tool_result"] == "[REDACTED:TEST_API_KEY]"
    assert event["usage"] == {"total_tokens": 0}
    assert "synthetic" not in boundary["tool_result"]
    assert len(boundary["tool_result"]) <= TOOL_RESULT_LIMIT
    assert terminal["status"] == "TIMEOUT"
    assert terminal["final_output"] is None
