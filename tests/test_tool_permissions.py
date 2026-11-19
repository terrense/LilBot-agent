from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from lilbot.sandbox import PermissionManager, Sandbox
from lilbot.tools import (
    ToolContext,
    ToolDef,
    ToolRegistry,
    ToolResult,
    register_builtins,
)
from lilbot.tools.builtin import _permission_preflight, _shell_permission
from lilbot.tools.registry import ApprovalRequirement, ToolCapability


def _context(tmp_path, mode="ask", answers=None):
    prompts = []
    replies = iter(answers or ["n"])

    def prompt(label):
        prompts.append(label)
        return next(replies)

    state = tmp_path / ".lilbot"
    manager = PermissionManager(state, mode, prompt=prompt)
    manager.quiet = True
    return ToolContext(Sandbox(tmp_path), manager, None, None, None, None, SimpleNamespace(state_dir=state)), prompts


@pytest.mark.parametrize("criteria,requirement", [
    (ToolCapability.WRITE, ApprovalRequirement.Auto),
    (ToolCapability.CODE, ApprovalRequirement.Auto),
    (frozenset({ToolCapability.RequiresApproval}), ApprovalRequirement.Auto),
    (ToolCapability.NONE, ApprovalRequirement.Required),
    (ToolCapability.NONE, ApprovalRequirement.Suggest),
])
def test_declared_effectful_tool_cannot_skip_permission(tmp_path, criteria, requirement):
    ctx, prompts = _context(tmp_path)
    registry = ToolRegistry()
    calls = []
    registry.register(ToolDef("forgotten_check", "custom effect", {}, lambda args, ctx: calls.append(args) or ToolResult(True, "executed"), criteria=criteria, approval_requirement=requirement))

    result, _ = registry.execute("forgotten_check", {}, ctx)

    assert not result.ok
    assert result.metadata["gate"] == "tool_permission"
    assert len(prompts) == 1
    assert calls == []


def test_read_only_tool_works_in_deny_all(tmp_path):
    ctx, prompts = _context(tmp_path, "deny-all")
    registry = ToolRegistry()
    registry.register(ToolDef("read", "read only", {}, lambda args, ctx: ToolResult(True, "read"), criteria=ToolCapability.READ))
    assert registry.execute("read", {}, ctx)[0].ok
    assert prompts == []


def test_allow_once_reuses_exact_handler_action_then_expires(tmp_path):
    ctx, prompts = _context(tmp_path, answers=["y", "y"])
    registry = ToolRegistry()

    def handler(args, ctx):
        return ToolResult(ctx.permissions.check("write:target.txt", "write target").allowed, "done")

    registry.register(ToolDef("writer", "write", {}, handler, criteria=ToolCapability.WRITE,
                              permission_check=lambda args, ctx: _permission_preflight(ctx, "write:target.txt", "write target")))
    assert registry.execute("writer", {}, ctx)[0].ok
    assert registry.execute("writer", {}, ctx)[0].ok
    assert len(prompts) == 2
    assert ctx.permissions.rules == {}


def test_generic_approval_does_not_skip_narrow_handler_check(tmp_path):
    ctx, prompts = _context(tmp_path, answers=["y", "n"])
    registry = ToolRegistry()
    registry.register(ToolDef("writer", "write", {}, lambda args, ctx: ToolResult(ctx.permissions.check("write:protected.txt", "specific target").allowed, "checked"), criteria=ToolCapability.WRITE))
    assert not registry.execute("writer", {}, ctx)[0].ok
    assert len(prompts) == 2


def test_nested_tool_does_not_inherit_allow_once_scope(tmp_path):
    ctx, prompts = _context(tmp_path, answers=["y", "n"])
    registry = ToolRegistry()
    def preflight(args, ctx):
        return _permission_preflight(ctx, "write:target.txt", "write target")
    registry.register(ToolDef("inner", "write", {}, lambda args, ctx: ToolResult(True, "inner executed"), criteria=ToolCapability.WRITE, permission_check=preflight))
    registry.register(ToolDef("outer", "write", {}, lambda args, ctx: registry.execute("inner", {}, ctx)[0], criteria=ToolCapability.WRITE, permission_check=preflight))
    assert not registry.execute("outer", {}, ctx)[0].ok
    assert len(prompts) == 2


def test_builtin_write_preserves_path_rule_and_asks_once(tmp_path):
    ctx, prompts = _context(tmp_path, answers=["y"])
    registry = ToolRegistry()
    register_builtins(registry)
    assert registry.execute("write_file", {"path": "target.txt", "content": "hello"}, ctx)[0].ok
    assert (tmp_path / "target.txt").read_text(encoding="utf-8") == "hello"
    assert len(prompts) == 1
    ctx.permissions.rules["write:target.txt"] = True
    assert registry.execute("write_file", {"path": "target.txt", "content": "updated"}, ctx)[0].ok
    assert len(prompts) == 1


@pytest.mark.parametrize("mode,allowed", [("ask", True), ("deny-all", False)])
def test_safe_shell_policy_respects_deny_all(tmp_path, mode, allowed):
    ctx, prompts = _context(tmp_path, mode)
    executed = []
    ctx.sandbox.run = lambda command, timeout: executed.append(command) or SimpleNamespace(ok=True, output="hello", returncode=0)
    registry = ToolRegistry()
    register_builtins(registry)
    result, _ = registry.execute("bash", {"command": "echo hello"}, ctx)
    assert result.ok is allowed
    assert bool(executed) is allowed
    assert prompts == []


def test_direct_shell_handler_also_respects_deny_all(tmp_path):
    ctx, prompts = _context(tmp_path, "deny-all")
    allowed, _safety, result = _shell_permission(ctx, "bash:echo hello", "echo hello", "echo hello")
    assert not allowed
    assert result is not None and not result.ok
    assert prompts == []


def test_rlm_execution_cannot_bypass_deny_all(tmp_path):
    ctx, prompts = _context(tmp_path, "deny-all")
    registry = ToolRegistry()
    register_builtins(registry)
    assert registry.execute("rlm_open", {"name": "guarded"}, ctx)[0].ok
    result, _ = registry.execute("rlm_eval", {"name": "guarded", "code": "raise RuntimeError('should not run')"}, ctx)
    registry.execute("rlm_close", {"name": "guarded"}, ctx)
    assert not result.ok
    assert result.metadata["gate"] == "tool_permission"
    assert prompts == []


def test_effectful_tool_fails_closed_without_manager():
    registry = ToolRegistry()
    registry.register(ToolDef("writer", "write", {}, lambda args, ctx: ToolResult(True, "executed"), criteria=ToolCapability.WRITE))
    result, _ = registry.execute("writer", {}, SimpleNamespace(config=None, permissions=None))
    assert not result.ok
    assert result.metadata["gate"] == "tool_permission"


def test_required_approval_takes_precedence_over_preview_predicate(tmp_path):
    ctx, prompts = _context(tmp_path)
    registry = ToolRegistry()
    registry.register(ToolDef("always_approve", "required", {}, lambda args, ctx: ToolResult(True, "executed"), approval_requirement=ApprovalRequirement.Required, permission_required=lambda args, ctx: False))
    assert not registry.execute("always_approve", {}, ctx)[0].ok
    assert len(prompts) == 1


@pytest.mark.parametrize("criteria", [ToolCapability.WRITE, frozenset({ToolCapability.RequiresApproval})])
def test_declared_effectful_tool_cannot_bypass_pending_plan(tmp_path, criteria):
    ctx, prompts = _context(tmp_path, answers=["y"])
    ctx.config.state_dir.mkdir()
    (ctx.config.state_dir / "plan_mode.json").write_text(json.dumps({"approval_state": "pending_approval", "requires_approval": True}), encoding="utf-8")
    registry = ToolRegistry()
    registry.register(ToolDef("new_effectful_tool", "effect", {}, lambda args, ctx: ToolResult(True, "executed"), criteria=criteria))
    result, _ = registry.execute("new_effectful_tool", {}, ctx)
    assert not result.ok
    assert result.metadata["gate"] == "plan_approval"
    assert prompts == []


@pytest.mark.parametrize("name,args", [
    ("worktree_prune", {}),
    ("slop_ledger_export", {}),
])
def test_read_only_variants_work_during_plan_and_deny_all(tmp_path, monkeypatch, name, args):
    from lilbot.tools import builtin
    ctx, prompts = _context(tmp_path, "deny-all")
    ctx.config.state_dir.mkdir()
    (ctx.config.state_dir / "plan_mode.json").write_text(json.dumps({"active": True, "approval_state": "planning"}), encoding="utf-8")
    monkeypatch.setattr(builtin, "_git_worktree_support", lambda ctx: (True, {}))
    registry = ToolRegistry()
    register_builtins(registry)
    result, _ = registry.execute(name, args, ctx)
    assert result.ok, result.output
    assert prompts == []


@pytest.mark.parametrize("name,args", [
    ("github_close_issue", {"issue": "12"}),
    ("github_close_pr", {"pr": "34"}),
    ("github_comment", {"target": "56", "body": "hello"}),
    ("pandoc_convert", {"input": "a.md", "output": "a.html"}),
    ("slop_ledger_export", {"path": "ledger.md"}),
])
def test_existing_narrow_actions_only_prompt_once(tmp_path, monkeypatch, name, args):
    from lilbot.tools import builtin
    ctx, prompts = _context(tmp_path, answers=["y"])
    monkeypatch.setattr(builtin.shutil, "which", lambda dependency: dependency)
    ctx.sandbox.run = lambda command, timeout: SimpleNamespace(ok=True, output="done", returncode=0)
    registry = ToolRegistry()
    register_builtins(registry)
    result, _ = registry.execute(name, args, ctx)
    assert result.ok, result.output
    assert len(prompts) == 1


def test_worktree_creation_reuses_generated_target_permission(tmp_path, monkeypatch):
    from lilbot.tools import builtin
    ctx, prompts = _context(tmp_path, answers=["y"])
    monkeypatch.setattr(builtin, "_git_worktree_support", lambda ctx: (True, {}))
    actions = []
    check = ctx.permissions.check

    def recorded_check(action, description):
        actions.append(action)
        return check(action, description)

    ctx.permissions.check = recorded_check

    def run(command, timeout):
        target = Path(actions[-1].removeprefix("worktree:add:"))
        target.mkdir(parents=True)
        return SimpleNamespace(ok=True, output="done", returncode=0)

    ctx.sandbox.run = run
    registry = ToolRegistry()
    register_builtins(registry)
    result, _ = registry.execute("EnterWorktree", {}, ctx)
    assert result.ok, result.output
    assert len(prompts) == 1
    assert len(actions) == 2 and actions[0] == actions[1]
    assert ctx.sandbox.root == Path(actions[0].removeprefix("worktree:add:"))

def test_validator_exception_fails_closed():
    from lilbot.tools.registry import ToolDef, ToolResult

    def broken(args, ctx):
        raise RuntimeError("boom")

    tool = ToolDef(
        "x", "test", {}, lambda args, ctx: ToolResult(True, "ok"), validate=broken
    )
    result = tool.run_validation({}, None)
    assert result.ok is False
    assert "validator error" in result.message

