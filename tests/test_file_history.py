"""Tests for file history / rewind (ported from mewcode filehistory)."""
from __future__ import annotations

from lilbot.core.history import FileHistory


def _hist(tmp_path):
    return FileHistory(tmp_path / ".lilbot", tmp_path)


def test_rewind_restores_modified_file(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("original", encoding="utf-8")
    hist = _hist(tmp_path)

    hist.record("a.txt", "edit_file", turn=1)   # snapshot "original"
    f.write_text("changed", encoding="utf-8")    # simulate the edit

    out = hist.rewind(1)
    assert any("restored a.txt" in line for line in out)
    assert f.read_text(encoding="utf-8") == "original"


def test_rewind_deletes_newly_created_file(tmp_path):
    hist = _hist(tmp_path)
    new = tmp_path / "new.txt"
    hist.record("new.txt", "write_file", turn=1)  # file did not exist
    new.write_text("brand new", encoding="utf-8")  # simulate create

    out = hist.rewind(1)
    assert any("removed new.txt" in line for line in out)
    assert not new.exists()


def test_rewind_multiple_steps_newest_first(tmp_path):
    a = tmp_path / "a.txt"
    a.write_text("v0", encoding="utf-8")
    hist = _hist(tmp_path)

    hist.record("a.txt", "edit_file", turn=1)  # v0
    a.write_text("v1", encoding="utf-8")
    hist.record("a.txt", "edit_file", turn=2)  # v1
    a.write_text("v2", encoding="utf-8")

    # Rewind one step -> back to v1
    hist.rewind(1)
    assert a.read_text(encoding="utf-8") == "v1"
    # Rewind again -> back to v0
    hist.rewind(1)
    assert a.read_text(encoding="utf-8") == "v0"


def test_journal_lists_changes(tmp_path):
    f = tmp_path / "x.txt"
    f.write_text("hi", encoding="utf-8")
    hist = _hist(tmp_path)
    hist.record("x.txt", "edit_file", turn=3)
    entries = hist.list()
    assert len(entries) == 1
    assert entries[0].rel_path == "x.txt"
    assert entries[0].existed is True
    assert entries[0].tool == "edit_file"


def test_rewind_empty_is_graceful(tmp_path):
    hist = _hist(tmp_path)
    assert hist.rewind(1) == []


def test_agent_snapshots_before_edit(tmp_path):
    from lilbot.config import LilBotConfig
    from lilbot.core.agent import Agent
    from lilbot.core.events import ProviderTurn, ToolCall
    from lilbot.tools import ToolContext, ToolDef, ToolRegistry, ToolResult

    target = tmp_path / "code.py"
    target.write_text("print('old')\n", encoding="utf-8")

    def writer(args, ctx):
        (tmp_path / args["path"]).write_text(args["content"], encoding="utf-8")
        return ToolResult(True, "written")

    reg = ToolRegistry()
    reg.register(ToolDef("write_file", "w", {"type": "object", "properties": {}}, writer))

    turns = iter([
        ProviderTurn(tool_calls=[ToolCall("write_file", {"path": "code.py", "content": "print('new')\n"})]),
        ProviderTurn(content="done"),
    ])

    class P:
        def complete(self, m, t):
            return next(turns)

    class Mem:
        def context(self, *a, **k):
            return "(none)"

    class Sk:
        def list(self, *a, **k):
            return []

    cfg = LilBotConfig(workspace=tmp_path)
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    agent = Agent(cfg, P(), reg, ToolContext(None, None, Mem(), Sk(), None, None, cfg))
    list(agent.run_turn("rewrite code.py"))

    assert target.read_text(encoding="utf-8") == "print('new')\n"
    # The agent recorded a snapshot; rewinding restores the original.
    agent.file_history.rewind(1)
    assert target.read_text(encoding="utf-8") == "print('old')\n"
