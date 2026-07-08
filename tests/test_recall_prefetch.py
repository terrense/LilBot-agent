"""Tests for the M9 side-query prefetch: parallel memory recall + background extraction.

The recall selector used to run as a blocking LLM call at turn start; it now
runs on a daemon thread and is consumed by a (bounded-then-polling) check
before each provider call. Extraction moved off the turn's critical path onto
a fire-and-forget daemon thread.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

import lilbot.core.agent as agent_mod
from lilbot.config import LilBotConfig
from lilbot.core.agent import Agent
from lilbot.core.events import ProviderTurn
from lilbot.memory import MemoryStore
from lilbot.tools import ToolContext, ToolRegistry


class EmptySkills:
    def list(self) -> list:
        return []


class SideQueryProvider:
    """Provider that answers recall-selector / extraction side-queries.

    ``gate`` (when given) blocks the selector response until set, simulating a
    slow side-query without real sleeps in the assertions.
    """

    def __init__(self, select_id: str = "", gate: threading.Event | None = None):
        self.calls: list[list[dict]] = []
        self.select_id = select_id
        self.gate = gate

    def complete(self, messages: list[dict], tools: list[dict]) -> ProviderTurn:
        self.calls.append(messages)
        system = str(messages[0].get("content", "")) if messages else ""
        if "selecting memories" in system:
            if self.gate is not None:
                self.gate.wait(timeout=5)
            return ProviderTurn(
                content=json.dumps({"selected": [self.select_id]}),
                usage={"total_tokens": 7},
            )
        if "extract durable memories" in system:
            return ProviderTurn(content=json.dumps({"memories": [
                {"name": "bg_fact", "text": "Extracted in background.", "kind": "project"},
            ]}))
        return ProviderTurn(content="final answer", usage={"total_tokens": 11})

    def main_calls(self) -> list[list[dict]]:
        return [
            m for m in self.calls
            if "selecting memories" not in str(m[0].get("content", ""))
            and "extract durable memories" not in str(m[0].get("content", ""))
        ]


def make_agent(tmp_path: Path, provider) -> tuple[Agent, MemoryStore]:
    store = MemoryStore(tmp_path)
    ctx = ToolContext(
        sandbox=None, permissions=None, memory=store, skills=EmptySkills(),
        subagents=None, mcp=None, config=None,
    )
    cfg = LilBotConfig(workspace=tmp_path)
    return Agent(cfg, provider, ToolRegistry(), ctx), store


def test_fast_recall_lands_in_first_provider_call(tmp_path):
    provider = SideQueryProvider()
    agent, store = make_agent(tmp_path, provider)
    entry = store.add(name="deploy_cmd", text="Deploy with make ship.", kind="project")
    provider.select_id = entry.id

    list(agent.run_turn("how do I deploy?"))

    # The bounded first wait lets a fast selector land in the very first call.
    main = provider.main_calls()
    assert main, "expected at least one main provider call"
    joined = "\n".join(str(m.get("content", "")) for m in main[0])
    assert "stored memories may be relevant" in joined
    assert entry.id in agent._surfaced_memory_ids
    # Side-query usage is merged into the agent's usage (thread-safe path).
    assert agent.usage.get("total_tokens", 0) >= 7 + 11


def test_slow_recall_does_not_block_turn(tmp_path, monkeypatch):
    gate = threading.Event()  # never set during the turn -> selector stalls
    provider = SideQueryProvider(gate=gate)
    agent, store = make_agent(tmp_path, provider)
    entry = store.add(name="deploy_cmd", text="Deploy with make ship.", kind="project")
    provider.select_id = entry.id
    monkeypatch.setattr(agent_mod, "RECALL_FIRST_WAIT_S", 0.05)

    list(agent.run_turn("how do I deploy?"))

    # Turn completed without the reminder; nothing was marked surfaced, so the
    # memory stays eligible for the next turn.
    for call in provider.main_calls():
        joined = "\n".join(str(m.get("content", "")) for m in call)
        assert "stored memories may be relevant" not in joined
    assert agent._pending_recall == ""
    assert entry.id not in agent._surfaced_memory_ids
    gate.set()  # release the daemon thread


def test_prefetch_result_adopted_by_pure_poll(tmp_path):
    provider = SideQueryProvider()
    agent, store = make_agent(tmp_path, provider)
    entry = store.add(name="deploy_cmd", text="Deploy with make ship.", kind="project")
    provider.select_id = entry.id

    agent._start_recall_prefetch("deploy?")
    assert agent._recall_prefetch is not None
    assert agent._recall_prefetch.wait(2.0), "prefetch job should finish"
    agent._recall_waited = True  # force the non-blocking poll path

    agent._consume_recall_prefetch()

    assert "deploy_cmd" in agent._pending_recall
    assert entry.id in agent._surfaced_memory_ids
    assert agent._recall_prefetch is None  # consumed exactly once


def test_extraction_runs_in_background_thread(tmp_path):
    provider = SideQueryProvider()
    agent, store = make_agent(tmp_path, provider)
    agent._turn_count = 3  # hits MEMORY_EXTRACTION_INTERVAL
    agent.messages.append({"role": "user", "content": "we decided to ship on friday"})
    agent.messages.append({"role": "assistant", "content": "noted"})

    agent._maybe_extract()

    assert agent._extract_thread is not None
    agent._extract_thread.join(timeout=5)
    assert not agent._extract_thread.is_alive()
    assert any(e.name == "bg_fact" for e in store.list())


def test_memory_store_concurrent_adds_do_not_lose_entries(tmp_path):
    store = MemoryStore(tmp_path)

    def writer(prefix: str) -> None:
        for i in range(20):
            store.add(name=f"{prefix}_{i}", text="x", kind="note")

    threads = [threading.Thread(target=writer, args=(p,)) for p in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(store.list()) == 40
