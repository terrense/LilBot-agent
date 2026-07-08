"""Layered permission rules with `Tool(pattern)` syntax (CC parity, #15).

A small, portable subset of Claude Code's permission engine:

  * rules are written ``Tool(pattern)`` — e.g. ``Bash(git *)``, ``write_file(*.env)``
    — or bare ``Tool`` (whole-tool);
  * rules carry a behavior (allow | deny | ask) and a **source** (policy | project
    | user), so an admin policy can be distinguished from a user override;
  * evaluation precedence is **deny > ask > allow** (a deny always wins);
  * ``find_shadowed`` reports rules that can never fire because a
    higher-precedence rule already covers everything they match — the class of
    misconfiguration CC's shadowed-rule detection surfaces.

The engine is consulted BEFORE the built-in execpolicy classifier (see
``execpolicy.classify(..., rules=...)``): a matching rule wins, and no match
falls through to the existing behavior — so an empty ruleset changes nothing.
"""
from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass, field
from pathlib import Path

BEHAVIORS = ("deny", "ask", "allow")
_PRECEDENCE = {"deny": 3, "ask": 2, "allow": 1}
# Source precedence is for reporting/ordering only; behavior precedence decides
# the outcome. Policy (admin) is the most authoritative.
SOURCES = ("policy", "project", "user")


@dataclass(frozen=True)
class PermissionRule:
    behavior: str            # allow | deny | ask
    tool: str                # tool name, or "*" for any tool
    pattern: str = ""        # glob on the input summary; "" == whole-tool
    source: str = "user"     # policy | project | user

    def spec(self) -> str:
        return f"{self.tool}({self.pattern})" if self.pattern else self.tool


def parse_rule(spec: str, behavior: str, source: str = "user") -> PermissionRule | None:
    """Parse ``Tool(pattern)`` or bare ``Tool`` into a PermissionRule."""
    spec = (spec or "").strip()
    if not spec:
        return None
    if spec.endswith(")") and "(" in spec:
        tool, pattern = spec[:-1].split("(", 1)
        return PermissionRule(behavior, tool.strip() or "*", pattern.strip(), source)
    return PermissionRule(behavior, spec, "", source)


def _matches(rule: PermissionRule, tool: str, text: str) -> bool:
    if rule.tool not in ("*", tool):
        return False
    if not rule.pattern:
        return True
    return fnmatch.fnmatch(text or "", rule.pattern)


def _covers(broad: PermissionRule, narrow: PermissionRule) -> bool:
    """True when ``broad`` matches every input ``narrow`` matches (same tool)."""
    if broad.tool not in ("*", narrow.tool):
        return False
    if not broad.pattern:
        return True                       # whole-tool covers any pattern
    if not narrow.pattern:
        return False                      # specific can't cover whole-tool
    if broad.pattern == narrow.pattern:
        return True
    if broad.pattern.endswith("*") and narrow.pattern.startswith(broad.pattern[:-1]):
        return True
    return False


@dataclass
class PermissionRuleSet:
    rules: list[PermissionRule] = field(default_factory=list)

    def evaluate(self, tool: str, text: str = "") -> tuple[str, PermissionRule | None]:
        """Return (decision, winning_rule). decision is "" when no rule matches
        (caller should fall through to its default policy)."""
        matched = [r for r in self.rules if _matches(r, tool, text)]
        if not matched:
            return "", None
        best = max(matched, key=lambda r: _PRECEDENCE[r.behavior])
        return best.behavior, best

    def find_shadowed(self) -> list[tuple[PermissionRule, PermissionRule]]:
        """Report (shadowed, shadowed_by): a rule that can never fire because a
        strictly-higher-precedence rule covers everything it matches."""
        out: list[tuple[PermissionRule, PermissionRule]] = []
        for narrow in self.rules:
            for broad in self.rules:
                if broad is narrow:
                    continue
                if _PRECEDENCE[broad.behavior] > _PRECEDENCE[narrow.behavior] and _covers(broad, narrow):
                    out.append((narrow, broad))
                    break
        return out


def _rules_from_dict(data: dict, source: str) -> list[PermissionRule]:
    rules: list[PermissionRule] = []
    for behavior in BEHAVIORS:
        for spec in data.get(behavior, []) or []:
            rule = parse_rule(str(spec), behavior, source)
            if rule is not None:
                rules.append(rule)
    return rules


def _load_file(path: Path | None, source: str) -> list[PermissionRule]:
    if path is None or not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return _rules_from_dict(data, source) if isinstance(data, dict) else []


def load_rules(
    state_dir: Path | None = None,
    user_dir: Path | None = None,
    policy_path: Path | None = None,
) -> PermissionRuleSet:
    """Load and layer permission rules from policy + user + project sources.

    File format (``permissions.json``): ``{"allow": ["Bash(git *)"], "deny":
    ["Bash(rm *)"], "ask": [...]}``. Missing files contribute nothing.
    """
    rules: list[PermissionRule] = []
    rules += _load_file(policy_path, "policy")
    if user_dir is not None:
        rules += _load_file(Path(user_dir) / "permissions.json", "user")
    if state_dir is not None:
        rules += _load_file(Path(state_dir) / "permissions.json", "project")
    return PermissionRuleSet(rules)
