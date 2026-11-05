from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

from lilbot.tools.registry import _plan_approval_gate


def test_plan_gate_parses_once_per_file_version_and_sees_updates(tmp_path, monkeypatch):
    path = tmp_path / "plan_mode.json"
    path.write_text(json.dumps({"active": True, "approval_state": "planning"}), encoding="utf-8")
    read_text = Path.read_text
    reads = []

    def counted_read(target, *args, **kwargs):
        if target == path:
            reads.append(target)
        return read_text(target, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counted_read)
    ctx = SimpleNamespace(config=SimpleNamespace(state_dir=tmp_path))
    assert _plan_approval_gate("write_file", ctx) is not None
    assert _plan_approval_gate("write_file", ctx) is not None
    assert len(reads) == 1
    previous_mtime = path.stat().st_mtime_ns
    path.write_text(json.dumps({"active": False, "approval_state": "approved"}), encoding="utf-8")
    os.utime(path, ns=(previous_mtime + 1_000_000, previous_mtime + 1_000_000))
    assert _plan_approval_gate("write_file", ctx) is None
    assert len(reads) == 2


def test_deleted_plan_clears_cached_state_and_recreation_is_read(tmp_path):
    path = tmp_path / "plan_mode.json"
    text = json.dumps({"active": True, "approval_state": "planning"})
    path.write_text(text, encoding="utf-8")
    previous_stat = path.stat()
    ctx = SimpleNamespace(config=SimpleNamespace(state_dir=tmp_path))
    assert _plan_approval_gate("write_file", ctx) is not None
    path.unlink()
    assert _plan_approval_gate("write_file", ctx) is None
    path.write_text(text, encoding="utf-8")
    os.utime(path, ns=(previous_stat.st_atime_ns, previous_stat.st_mtime_ns))
    assert _plan_approval_gate("write_file", ctx) is not None
