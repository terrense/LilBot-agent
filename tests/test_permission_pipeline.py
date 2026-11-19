"""One ordered permission pipeline: deny > ask > tool > mode > allow."""

from __future__ import annotations

from types import SimpleNamespace

from lilbot.sandbox.permission_rules import PermissionRuleSet, parse_rule
from lilbot.sandbox.permissions import PermissionManager
from lilbot.tools.builtin import _shell_permission
from lilbot.tools.registry import (
    ApprovalRequirement,
    ToolDef,
    ToolResult,
    _tool_permission_gate,
)


def _ctx(tmp_path, *, mode="ask", answers=None, rules=None):
    answers = list(answers or [])
    prompts: list[str] = []

    def prompt(label: str) -> str:
        prompts.append(label)
        return answers.pop(0) if answers else "n"

    state = tmp_path / "state"
    state.mkdir()
    permissions = PermissionManager(state, mode=mode, prompt=prompt, interactive=True)
    permissions.quiet = True
    ctx = SimpleNamespace(
        permissions=permissions,
        permission_rules=PermissionRuleSet(list(rules or [])),
        config=SimpleNamespace(auto_allow_safe_commands=True),
        sandbox=SimpleNamespace(root=tmp_path),
    )
    return ctx, prompts


def _write_tool(*, check=None):
    return ToolDef(
        "write_file",
        "write",
        {},
        lambda args, ctx: ToolResult(True, "ok"),
        approval_requirement=ApprovalRequirement.Required,
        permission_check=check,
    )


def test_force_check_bypasses_accept_all_and_prompts(tmp_path):
    ctx, prompts = _ctx(tmp_path, mode="accept-all", answers=["y"])
    decision = ctx.permissions.check("rule:test", "explicit ask", force=True)
    assert decision.allowed is True
    assert prompts  # accept-all did not swallow the explicit ask


def test_force_check_deny_all_still_wins(tmp_path):
    ctx, prompts = _ctx(tmp_path, mode="deny-all", answers=["y"])
    decision = ctx.permissions.check("rule:test", "explicit ask", force=True)
    assert decision.allowed is False
    assert prompts == []


def test_rule_ask_forces_prompt_even_in_accept_all(tmp_path):
    rules = [parse_rule("write_file(*.env)", "ask")]
    ctx, prompts = _ctx(tmp_path, mode="accept-all", answers=["n"], rules=rules)
    result = _tool_permission_gate(_write_tool(), {"path": ".env"}, ctx)
    assert result is not None and result.ok is False
    assert prompts


def test_rule_ask_can_be_approved(tmp_path):
    rules = [parse_rule("write_file(*.env)", "ask")]
    ctx, prompts = _ctx(tmp_path, mode="accept-all", answers=["y"], rules=rules)
    result = _tool_permission_gate(_write_tool(), {"path": ".env"}, ctx)
    assert result is None
    assert prompts


def test_rule_deny_beats_tool_allow(tmp_path):
    rules = [parse_rule("write_file", "deny")]
    tool = _write_tool(check=lambda args, ctx: None)
    ctx, prompts = _ctx(tmp_path, mode="accept-all", rules=rules)
    result = _tool_permission_gate(tool, {"path": "x"}, ctx)
    assert result is not None and result.ok is False
    assert prompts == []


def test_tool_check_runs_before_allow_rule(tmp_path):
    rules = [parse_rule("write_file", "allow")]
    tool = _write_tool(check=lambda args, ctx: ToolResult(False, "tool policy says no"))
    ctx, prompts = _ctx(tmp_path, mode="ask", rules=rules)
    result = _tool_permission_gate(tool, {"path": "x"}, ctx)
    assert result is not None and "tool policy" in result.output
    assert prompts == []


def test_allow_rule_skips_default_prompt_in_ask_mode(tmp_path):
    rules = [parse_rule("write_file", "allow")]
    ctx, prompts = _ctx(tmp_path, mode="ask", rules=rules)
    result = _tool_permission_gate(_write_tool(), {"path": "x"}, ctx)
    assert result is None
    assert prompts == []


def test_shell_ask_rule_forces_prompt_in_accept_all(tmp_path):
    rules = [parse_rule("Bash(git push*)", "ask")]
    ctx, prompts = _ctx(tmp_path, mode="accept-all", answers=["y"], rules=rules)
    allowed, _safety, blocked = _shell_permission(
        ctx, "bash:git push", "run git push", "git push origin main"
    )
    assert allowed is True and blocked is None
    assert prompts


def test_shell_ask_rule_can_be_denied(tmp_path):
    rules = [parse_rule("Bash(git push*)", "ask")]
    ctx, prompts = _ctx(tmp_path, mode="accept-all", answers=["n"], rules=rules)
    allowed, _safety, blocked = _shell_permission(
        ctx, "bash:git push", "run git push", "git push origin main"
    )
    assert allowed is False and blocked is not None
    assert prompts


def test_shell_deny_all_beats_allow_rule(tmp_path):
    rules = [parse_rule("Bash(git status)", "allow")]
    ctx, prompts = _ctx(tmp_path, mode="deny-all", rules=rules)
    allowed, _safety, blocked = _shell_permission(
        ctx, "bash:git status", "run git status", "git status"
    )
    assert allowed is False and blocked is not None
    assert prompts == []


def test_allow_rule_never_reopens_catastrophic_command(tmp_path):
    rules = [parse_rule("Bash(rm *)", "allow")]
    ctx, prompts = _ctx(tmp_path, mode="ask", rules=rules)
    allowed, _safety, blocked = _shell_permission(
        ctx, "bash:rm -rf /", "run rm", "rm -rf /"
    )
    assert allowed is False and blocked is not None
    assert prompts == []

def test_rule_ask_does_not_prompt_twice_in_ask_mode(tmp_path):
    rules = [parse_rule("write_file", "ask")]
    ctx, prompts = _ctx(tmp_path, mode="ask", answers=["y"], rules=rules)
    result = _tool_permission_gate(_write_tool(), {"path": "x"}, ctx)
    assert result is None
    assert len(prompts) == 1

def test_force_ask_remembers_always_allow(tmp_path):
    rules = [parse_rule("write_file", "ask")]
    ctx, prompts = _ctx(tmp_path, mode="accept-all", answers=["a"], rules=rules)
    first = _tool_permission_gate(_write_tool(), {"path": "x"}, ctx)
    assert first is None
    assert len(prompts) == 1
    second = _tool_permission_gate(_write_tool(), {"path": "x"}, ctx)
    assert second is None
    assert len(prompts) == 1  # the "always" answer was remembered

