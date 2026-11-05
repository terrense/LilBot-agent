from pathlib import Path

from evaluation import adapter
from evaluation.live_provider import BlockedArguments, LiveProvider, LiveToolRegistry
from lilbot.config import LilBotConfig
from lilbot.core.events import ProviderTurn, StreamEvent, ToolCall
from lilbot.llm.providers import BaseProvider, ProviderError
from lilbot.tools.builtin import register_builtins
from lilbot.tools.registry import ToolRegistry


class _Trace:
    def __init__(self):
        self.events = []

    def record(self, event_type, **fields):
        self.events.append({"event_type": event_type, **fields})


class _Provider(BaseProvider):
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def complete(self, messages, tools):
        self.requests.append((list(messages), tools))
        turn = next(self.responses)
        if isinstance(turn, Exception):
            raise turn
        return turn


def _call(tool_name, **args):
    return ProviderTurn(tool_calls=[ToolCall(tool_name, args)], usage={"total_tokens": 3})


def _run(monkeypatch, root, responses, setup, *, max_steps=10):
    workspace = root / "workspace"
    workspace.mkdir()
    trace = _Trace()
    provider = _Provider(responses)
    monkeypatch.setattr(adapter, "choose_provider", lambda config: provider)
    adapter.LilBotAdapter().run(
        "Run evaluation", setup, max_steps, workspace, trace,
        live_config=LilBotConfig(workspace=Path("host-config"), provider="deepseek", model="fake-live"),
    )
    return workspace, trace.events, provider.requests


def test_live_prevalidates_paths_before_snapshot_and_allows_recovery(tmp_path, monkeypatch):
    outside = tmp_path / "private.txt"
    outside.write_text("fixture canary", encoding="utf-8")
    workspace, events, requests = _run(monkeypatch, tmp_path, [
        _call("write_file", path=str(outside), content="danger"),
        _call("exec_shell", command="Do not run"),
        _call("read_file", path="manual.pdf"),
        _call("write_file", path="protected.txt", content="denied"),
        _call("write_file", path="allowed.txt", content="yes"),
        ProviderTurn(content="Done."),
    ], {"writable_files": ["allowed.txt"]})

    offered = {tool["name"] for tool in requests[0][1]}
    assert "exec_shell" not in offered and "memory_save" not in offered
    starts = [event for event in events if event["event_type"] == "tool_started"]
    finishes = [event for event in events if event["event_type"] == "tool_finished"]
    assert starts[0]["tool_args"]["path"] == str(outside)
    assert finishes[0]["failure_kind"] == "permission"
    assert finishes[1]["failure_kind"] == "permission"
    assert finishes[2]["failure_kind"] == "permission"
    assert finishes[3]["failure_kind"] == "permission"
    assert finishes[4]["status"] == "ok"
    assert "evaluation policy" in requests[1][0][-1]["content"]
    assert (workspace / "allowed.txt").read_text(encoding="utf-8") == "yes"
    assert not (workspace / "protected.txt").exists()
    assert not list((workspace / ".lilbot" / "filehistory").glob("*.bak"))
    assert outside.read_text(encoding="utf-8") == "fixture canary"


def test_live_preserves_real_repeat_guardrail(tmp_path, monkeypatch):
    _, events, _ = _run(monkeypatch, tmp_path, [
        *[_call("read_file", path="missing.txt") for _ in range(5)],
        ProviderTurn(content="Missing file."),
    ], {})
    finishes = [event for event in events if event["event_type"] == "tool_finished"]
    assert "repeated_failure_warning" in finishes[1]["guardrails"]
    assert "repeated_failure_block" in finishes[-1]["guardrails"]
    assert finishes[-1]["failure_kind"] == "guardrail"
    assert events[-1]["usage"] == {"total_tokens": 15}


def test_live_caps_tool_steps_across_turns_and_counts_observed_requests(tmp_path, monkeypatch):
    workspace, events, requests = _run(monkeypatch, tmp_path, [
        _call("write_file", path="first.txt", content="First"),
        ProviderTurn(content="Saved first."),
        _call("write_file", path="second.txt", content="Second"),
        ProviderTurn(content="Budget answer."),
    ], {"writable_files": ["first.txt", "second.txt"], "followup": ["Second", "Third"]}, max_steps=2)

    assert (workspace / "first.txt").is_file() and (workspace / "second.txt").is_file()
    assert "Saved first." in [row.get("content") for row in requests[2][0]]
    assert requests[-1][1] == []
    assert [event["turn_index"] for event in events if event["event_type"] == "turn_started"] == [1, 2]
    finishes = [event for event in events if event["event_type"] == "provider_finished"]
    assert [event["request_index"] for event in finishes] == [1, 2, 3, 4]
    assert [event["usage"] for event in finishes] == [{"total_tokens": 3}, None, {"total_tokens": 3}, None]
    assert events[-1]["termination_reason"] == "step_limit"
    assert events[-1]["steps"] == 2


def test_live_memory_uses_trial_store_and_followup_context(tmp_path, monkeypatch):
    workspace, events, requests = _run(monkeypatch, tmp_path, [
        _call("memory_save", name="Demo pin", text="violet-mango", kind="user"),
        ProviderTurn(content="Saved the pin."),
        _call("memory_search", query="Demo pin"),
        ProviderTurn(content="violet-mango"),
    ], {"memory": True, "followup": ["Look up the demo pin."]})

    assert len(requests) == 4
    assert (workspace / ".lilbot" / "eval-user-memory" / "demo-pin.md").is_file()
    assert "violet-mango" in requests[-1][0][-1]["content"]
    assert events[-1]["final_output"] == "violet-mango"


def test_provider_failure_keeps_classification_without_raw_http_body(tmp_path, monkeypatch):
    _, events, _ = _run(monkeypatch, tmp_path, [
        ProviderError("private HTTP payload", status_code=401),
    ], {})
    request = next(event for event in events if event["event_type"] == "provider_finished")
    assert request["reason"] == "auth"
    assert request["usage"] is None
    assert events[-1]["status"] == "AGENT_FAILURE"
    assert "private HTTP payload" not in str(events)


def test_live_stream_never_exposes_unvalidated_early_tool_calls(tmp_path):
    forbidden = ToolCall("write_file", {"path": "../outside.txt", "content": "unexpected"})

    class _StreamingProvider(BaseProvider):
        def complete_stream(self, messages, tools):
            yield StreamEvent(tool_call=forbidden)
            yield StreamEvent(text="Working.")
            yield StreamEvent(final=ProviderTurn(
                tool_calls=[forbidden], usage={"total_tokens": 9}, reasoning_content="private reasoning",
            ))

    catalog = ToolRegistry()
    register_builtins(catalog)
    registry = LiveToolRegistry()
    registry.register(catalog.get("write_file"))
    trace = _Trace()
    live = LiveProvider(_StreamingProvider(), registry, tmp_path, trace, request_limit=3)
    events = list(live.complete_stream([], []))
    assert len(events) == 2
    assert all(event.tool_call is None for event in events)
    result = events[-1].final.tool_calls[0].arguments
    assert isinstance(result, BlockedArguments) and "path" not in result
    assert trace.events[-1]["usage"] == {"total_tokens": 9}
    assert "private reasoning" not in str(trace.events)
