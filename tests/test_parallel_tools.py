"""Tests for parallel read-only tool execution."""
from __future__ import annotations

import time
from pathlib import Path

from lilbot.config import LilBotConfig
from lilbot.core.agent import Agent
from lilbot.core.events import ProviderTurn, ToolCall, ToolFinished
from lilbot.tools import ToolContext, ToolDef, ToolRegistry, ToolResult
from lilbot.tools.registry import ToolCapability


class _Mem:
    def context(self, *a, **k):
        return "(none)"


class _Skills:
    def list(self, *a, **k):
        return []


def _agent(tmp_path, registry, scripted_turns):
    turns = iter(scripted_turns)

    class P:
        def complete(self, messages, tools):
            return next(turns)

    cfg = LilBotConfig(workspace=tmp_path, max_steps=20)
    ctx = ToolContext(None, None, _Mem(), _Skills(), None, None, cfg)
    return Agent(cfg, P(), registry, ctx)


def test_read_only_calls_run_in_parallel(tmp_path):
    def slow_read(args, ctx):
        time.sleep(0.2)
        return ToolResult(True, f"read {args.get('n')}")

    registry = ToolRegistry()
    registry.register(ToolDef("slow_read", "ro", {"type": "object", "properties": {}},
                              slow_read, criteria=ToolCapability.READ))

    calls = [ToolCall("slow_read", {"n": i}) for i in range(4)]
    agent = _agent(tmp_path, registry, [
        ProviderTurn(tool_calls=calls),
        ProviderTurn(content="done"),
    ])

    started = time.perf_counter()
    events = list(agent.run_turn("read everything"))
    elapsed = time.perf_counter() - started

    # 4 x 0.2s sequential would be ~0.8s; parallel should be well under.
    assert elapsed < 0.6, f"too slow ({elapsed:.2f}s) — not parallel?"
    finished = [e for e in events if isinstance(e, ToolFinished)]
    assert [f.output for f in finished] == [f"read {i}" for i in range(4)]


def test_write_tools_run_sequentially(tmp_path):
    order: list[str] = []

    def writer(args, ctx):
        order.append(f"start {args.get('n')}")
        time.sleep(0.05)
        order.append(f"end {args.get('n')}")
        return ToolResult(True, "ok")

    registry = ToolRegistry()
    # No READ criteria -> not concurrency-safe.
    registry.register(ToolDef("writer", "w", {"type": "object", "properties": {}}, writer))

    calls = [ToolCall("writer", {"n": i}) for i in range(3)]
    agent = _agent(tmp_path, registry, [
        ProviderTurn(tool_calls=calls),
        ProviderTurn(content="done"),
    ])
    list(agent.run_turn("write things"))

    # Sequential execution never interleaves start/end.
    assert order == ["start 0", "end 0", "start 1", "end 1", "start 2", "end 2"]


def test_partition_groups_consecutive_safe_calls(tmp_path):
    registry = ToolRegistry()
    registry.register(ToolDef("ro", "r", {"type": "object", "properties": {}},
                              lambda a, c: ToolResult(True, "r"), criteria=ToolCapability.READ))
    registry.register(ToolDef("rw", "w", {"type": "object", "properties": {}},
                              lambda a, c: ToolResult(True, "w")))
    agent = _agent(tmp_path, registry, [ProviderTurn(content="x")])

    calls = [ToolCall("ro"), ToolCall("ro"), ToolCall("rw"), ToolCall("ro")]
    batches = agent._partition_calls(calls)
    sizes = [len(b) for b in batches]
    assert sizes == [2, 1, 1]
