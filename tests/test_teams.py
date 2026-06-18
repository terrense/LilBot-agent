"""Tests for the teams / teammates layer."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from lilbot import cli
from lilbot.config import LilBotConfig
from lilbot.core.events import ProviderTurn
from lilbot.teams import AgentNameRegistry
from lilbot.teams.mailbox import Mailbox, create_message
from lilbot.teams.manager import TeamManager
from lilbot.teams.models import TeammateInfo
from lilbot.teams.shared_task import SharedTaskStore
from lilbot.tui.classic import LilBotUI


# ── data layer ───────────────────────────────────────────────────────────


def test_mailbox_concurrent_writes_no_loss(tmp_path: Path) -> None:
    mb = Mailbox(tmp_path / "mb")

    def writer(i: int) -> None:
        for j in range(20):
            mb.write("lead", create_message(f"w{i}", "lead", f"{i}-{j}", summary="s"))

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    got = mb.consume("lead")
    assert len(got) == 100
    assert mb.consume("lead") == []  # consume marks read


def test_shared_task_dependencies(tmp_path: Path) -> None:
    store = SharedTaskStore(tmp_path / "tasks.json")
    store.init_empty()
    a = store.create("impl", assignee="impl")
    b = store.create("review", assignee="rev", blocked_by=[a.id])
    store.update(a.id, status="completed")
    assert store.get(a.id).status == "completed"
    assert store.list_tasks(assignee="rev")[0].blocked_by == [a.id]
    assert {t.status for t in store.list_tasks(status="completed")} == {"completed"}


# ── manager ──────────────────────────────────────────────────────────────


def test_team_manager_lifecycle_and_drain(tmp_path: Path) -> None:
    AgentNameRegistry.reset()
    tm = TeamManager(tmp_path)
    team = tm.create_team("Bug Fix", "lead", "fix it")
    assert tm.get_task_store(team.name) is not None
    assert tm.get_mailbox(team.name) is not None

    tm.register_member(team.name, TeammateInfo("impl", "sub_1", "implementer", "m", "", is_active=True))
    tm.notify_lead(team.name, "impl", "[idle] impl: done", "impl idle")

    notes = tm.drain_lead_mailbox()
    assert notes and "team-notification" in notes[0] and "impl" in notes[0]
    assert tm.drain_lead_mailbox() == []  # consumed

    tm.set_member_idle(team.name, "impl")
    assert tm.get_team(team.name).get_member("impl").is_active is False

    tm.delete_team(team.name)
    assert tm.get_team(team.name) is None


# ── end to end with a stub provider ──────────────────────────────────────


class _StubProvider:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, messages, tools):  # noqa: ANN001
        self.calls += 1
        return ProviderTurn(
            content="SUMMARY: did it\nCHANGES: None.\nEVIDENCE: ok\nRISKS: None.\nBLOCKERS: None."
        )


def _runtime(tmp_path: Path):
    AgentNameRegistry.reset()
    cfg = LilBotConfig(workspace=tmp_path)
    agent, registry, ctx = cli.build_runtime(cfg, LilBotUI(enabled=False), interactive=False)
    stub = _StubProvider()
    agent.provider = stub
    ctx.subagents.provider = lambda m, t: stub.complete(m, t)
    return agent, registry, ctx, stub


def _wait(predicate, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


def test_teammate_spawn_idle_and_wake(tmp_path: Path) -> None:
    agent, registry, ctx, stub = _runtime(tmp_path)

    r, _ = registry.execute("team_create", {"team_name": "demo"}, ctx)
    assert r.ok

    r, _ = registry.execute(
        "agent_open",
        {"team_name": "demo", "name": "impl", "subagent_type": "implementer", "prompt": "fix bug"},
        ctx,
    )
    assert r.ok

    # teammate runs one turn then reports idle to the lead
    assert _wait(lambda: bool(ctx.teams.get_mailbox("demo").read("lead")))
    notes = ctx.teams.drain_lead_mailbox()
    assert notes and "impl" in notes[0]

    # waking it with a message triggers a second turn
    calls_before = stub.calls
    r, _ = registry.execute("send_message", {"to": "impl", "message": "add a test", "summary": "test"}, ctx)
    assert r.ok
    assert _wait(lambda: stub.calls > calls_before)


def test_teammate_can_message_lead_via_ctx_identity(tmp_path: Path) -> None:
    from dataclasses import replace

    agent, registry, ctx, _ = _runtime(tmp_path)
    registry.execute("team_create", {"team_name": "demo"}, ctx)
    registry.execute("team_task_create", {"title": "do work"}, ctx)

    teammate_ctx = replace(ctx, team_name="demo", agent_name="impl")
    r, _ = registry.execute("send_message", {"to": "lead", "message": "update"}, teammate_ctx)
    assert r.ok
    notes = ctx.teams.drain_lead_mailbox()
    assert notes and "update" in notes[0]


def test_shared_board_through_tools(tmp_path: Path) -> None:
    agent, registry, ctx, _ = _runtime(tmp_path)
    registry.execute("team_create", {"team_name": "demo"}, ctx)
    r, _ = registry.execute("team_task_create", {"title": "fix", "assignee": "impl"}, ctx)
    assert r.ok
    r, _ = registry.execute("team_task_update", {"task_id": "1", "status": "in_progress"}, ctx)
    assert r.ok
    r, _ = registry.execute("team_task_list", {"status": "in_progress"}, ctx)
    assert r.ok and r.metadata["count"] == 1


def test_one_shot_subagents_have_no_team_tools(tmp_path: Path) -> None:
    """Regression: existing one-shot subagents must not gain team coordination tools."""
    _, _, ctx, _ = _runtime(tmp_path)
    mgr = ctx.subagents
    from lilbot.subagents.manager import SubAgentTask

    explore_def = mgr.definitions["explore"]
    task = SubAgentTask(id="t1", agent_type="explore", prompt="x", allowed_tools=explore_def.allowed_tools)
    schema_names = {s["name"] for s in mgr._tool_schemas_for_task(explore_def, task)}
    assert "send_message" not in schema_names
    assert "team_task_create" not in schema_names
