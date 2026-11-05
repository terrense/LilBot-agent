from __future__ import annotations

import pytest

from lilbot.config import LilBotConfig
from lilbot.core.agent import Agent
from lilbot.core.events import ProviderTurn, ToolCall, ToolFinished, TurnFinished
from lilbot.core.guardrails import ToolCallGuardrailController
from lilbot.tools import ToolContext, ToolDef, ToolRegistry, ToolResult


class _Memory:
    def context(self):
        return "(none)"

    def list(self):
        return []


class _Skills:
    def list(self):
        return []


def _failure(controller, arguments=None, tool_name="work"):
    return controller.after_call(
        tool_name, arguments or {}, ok=False, output="failed", read_only=False,
    )


def test_signature_normalizes_nested_argument_key_order():
    controller = ToolCallGuardrailController()
    first = {"path": "a", "options": {"b": 2, "a": 1}}
    reordered = {"options": {"a": 1, "b": 2}, "path": "a"}
    for _ in range(4):
        _failure(controller, first)
    assert controller.before_call("work", reordered).kind == "repeated_failure_block"
    assert controller.before_call("other_tool", reordered) is None
    assert controller.before_call("work", {"path": "b"}) is None


def test_repeated_failure_warns_second_and_blocks_fifth_attempt():
    controller = ToolCallGuardrailController()
    notices = []
    for _ in range(4):
        assert controller.before_call("work", {}) is None
        notices.append(_failure(controller))
    assert notices[0] == []
    assert [notice.kind for notice in notices[1]] == ["repeated_failure_warning"]
    assert [notice.kind for notice in notices[2]] == ["tool_failure_warning"]
    blocked = controller.before_call("work", {})
    assert blocked.kind == "repeated_failure_block"
    assert blocked.metadata["failures"] == 4


def test_same_tool_failures_accumulate_with_different_arguments():
    controller = ToolCallGuardrailController()
    _failure(controller, {"n": 1})
    controller.after_call("work", {}, ok=True, output="ok", read_only=False)
    _failure(controller, {"n": 2})
    notices = _failure(controller, {"n": 3})
    assert [notice.kind for notice in notices] == ["tool_failure_warning"]


def test_success_and_turn_reset_clear_identical_call_failure_state():
    controller = ToolCallGuardrailController()
    for _ in range(4):
        _failure(controller)
    controller.after_call("work", {}, ok=True, output="ok", read_only=False)
    assert controller.before_call("work", {}) is None
    assert _failure(controller) == []
    controller.reset()
    assert controller.before_call("work", {}) is None
    assert _failure(controller) == []


def test_read_only_same_output_warns_once_until_output_changes():
    controller = ToolCallGuardrailController()

    def read(output, read_only=True):
        return controller.after_call("read", {}, ok=True, output=output, read_only=read_only)

    assert read("same") == []
    assert [notice.kind for notice in read("same")] == ["read_only_no_progress"]
    assert read("same") == []
    assert controller.before_call("read", {}) is None
    assert read("new") == []
    assert [notice.kind for notice in read("new")] == ["read_only_no_progress"]
    assert read("new", read_only=False) == []


@pytest.mark.parametrize("call_names", [("work",), ("work", "WORK", "work-tool")])
def test_agent_blocks_fifth_identical_failed_call_and_resets_next_turn(tmp_path, call_names):
    class Provider:
        def complete(self, messages, tools):
            user_index = max(i for i, message in enumerate(messages) if message["role"] == "user")
            results = [message for message in messages[user_index + 1:] if message["role"] == "tool"]
            if results and "Guardrail blocked" in results[-1]["content"]:
                return ProviderTurn(content="I will change approach.")
            name = call_names[len(results) % len(call_names)]
            return ProviderTurn(tool_calls=[ToolCall(name, {"path": "missing"})])

    executed = []

    def handler(arguments, ctx):
        executed.append(arguments)
        return ToolResult(False, "file missing")

    registry = ToolRegistry()
    registry.register(ToolDef("work", "demo", {"type": "object"}, handler))
    config = LilBotConfig(workspace=tmp_path)
    ctx = ToolContext(None, None, _Memory(), _Skills(), None, None, config)
    agent = Agent(config, Provider(), registry, ctx)
    try:
        for prompt in ("run work", "try again"):
            events = list(agent.run_turn(prompt))
            results = [event for event in events if isinstance(event, ToolFinished)]
            assert len(results) == 5
            assert "failed twice" in results[1].output
            assert results[-1].metadata["guardrails"][0]["kind"] == "repeated_failure_block"
            assert results[-1].elapsed_ms == 0
            assert isinstance(events[-1], TurnFinished)
            assert events[-1].steps == 5
        assert len(executed) == 8
    finally:
        agent.close()
