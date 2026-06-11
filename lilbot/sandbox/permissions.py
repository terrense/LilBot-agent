from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class PermissionDecision:
    allowed: bool
    remembered: bool = False


class PermissionManager:
    def __init__(
        self,
        state_dir: Path,
        mode: str = "ask",
        prompt: Callable[[str], str] | None = None,
        interactive: bool = True,
    ):
        self.state_dir = state_dir
        self.mode = mode
        self.prompt = prompt or input
        self.interactive = interactive
        self.rules_path = state_dir / "permissions.json"
        self.rules = self._load_rules()
        self.quiet = False

    def _load_rules(self) -> dict[str, bool]:
        try:
            return json.loads(self.rules_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def save(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.rules_path.write_text(json.dumps(self.rules, indent=2), encoding="utf-8")

    def set_mode(self, mode: str) -> None:
        if mode not in {"ask", "accept-all", "deny-all"}:
            raise ValueError("permission mode must be ask, accept-all, or deny-all")
        self.mode = mode

    def check(self, action: str, description: str) -> PermissionDecision:
        if self.mode == "accept-all":
            return PermissionDecision(True)
        if self.mode == "deny-all":
            return PermissionDecision(False)
        if action in self.rules:
            return PermissionDecision(self.rules[action], True)
        if not self.interactive:
            return PermissionDecision(False)

        if not self.quiet:
            print()
            print(f"? permission required: {description}")
            print("  y = allow once, a = always allow, n = deny once, d = always deny")
        answer = self.prompt("permission> ").strip().lower()
        if answer in {"a", "always", "always allow"}:
            self.rules[action] = True
            self.save()
            return PermissionDecision(True, True)
        if answer in {"d", "deny", "always deny"}:
            self.rules[action] = False
            self.save()
            return PermissionDecision(False, True)
        return PermissionDecision(answer in {"y", "yes"})
