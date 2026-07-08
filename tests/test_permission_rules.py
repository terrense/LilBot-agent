"""Tests for layered permission rules + shadowed-rule detection (#15)."""
from __future__ import annotations

import json

from lilbot.sandbox.execpolicy import classify
from lilbot.sandbox.permission_rules import (
    PermissionRule,
    PermissionRuleSet,
    load_rules,
    parse_rule,
)


def test_parse_rule_forms():
    r = parse_rule("Bash(git *)", "allow")
    assert r.tool == "Bash" and r.pattern == "git *" and r.behavior == "allow"
    r2 = parse_rule("read_file", "deny", source="policy")
    assert r2.tool == "read_file" and r2.pattern == "" and r2.source == "policy"


def test_precedence_deny_beats_allow():
    rs = PermissionRuleSet([
        parse_rule("Bash(git *)", "allow"),
        parse_rule("Bash(git push*)", "deny"),
    ])
    assert rs.evaluate("Bash", "git status")[0] == "allow"
    assert rs.evaluate("Bash", "git push origin")[0] == "deny"  # deny wins
    assert rs.evaluate("Bash", "npm test")[0] == ""             # no rule -> fall through


def test_whole_tool_rule_matches_any_input():
    rs = PermissionRuleSet([parse_rule("write_file", "deny")])
    assert rs.evaluate("write_file", "anything.txt")[0] == "deny"
    assert rs.evaluate("read_file", "x")[0] == ""


def test_shadowed_rule_detection():
    rs = PermissionRuleSet([
        parse_rule("Bash(git *)", "allow"),
        parse_rule("Bash", "deny"),          # whole-tool deny shadows the allow
    ])
    shadowed = rs.find_shadowed()
    assert len(shadowed) == 1
    narrow, broad = shadowed[0]
    assert narrow.spec() == "Bash(git *)" and broad.spec() == "Bash"


def test_no_false_shadow_when_disjoint():
    rs = PermissionRuleSet([
        parse_rule("Bash(git *)", "allow"),
        parse_rule("Bash(rm *)", "deny"),    # different pattern, no coverage
    ])
    assert rs.find_shadowed() == []


def test_classify_consults_rules_but_denies_catastrophic_first():
    rules = PermissionRuleSet([parse_rule("Bash(rm build)", "allow")])
    # A user allow rule can green-light a normal rm...
    assert classify("rm build", rules=rules)[0] == "allow"
    # ...but never a catastrophic one.
    assert classify("rm -rf /", rules=rules)[0] == "deny"
    # Empty ruleset -> unchanged built-in behavior (git status auto-allowed).
    assert classify("git status")[0] == "allow"


def test_load_rules_layers_sources(tmp_path):
    (tmp_path / "permissions.json").write_text(
        json.dumps({"allow": ["Bash(git *)"], "deny": ["Bash(git push*)"]}),
        encoding="utf-8",
    )
    user_dir = tmp_path / "user"
    user_dir.mkdir()
    (user_dir / "permissions.json").write_text(
        json.dumps({"ask": ["write_file(*.env)"]}), encoding="utf-8",
    )
    rs = load_rules(state_dir=tmp_path, user_dir=user_dir)
    sources = {r.source for r in rs.rules}
    assert sources == {"project", "user"}
    assert rs.evaluate("Bash", "git push origin")[0] == "deny"
    assert rs.evaluate("write_file", "secrets.env")[0] == "ask"
