from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

VALID_EVENTS = {
    "session_start",
    "turn_start",
    "pre_tool_use",
    "post_tool_use",
    "turn_end",
    "session_end",
    # CC parity: "stop" fires when the model is about to end the turn; a hook can
    # force continuation. "user_prompt_submit" fires on each new user message.
    "stop",
    "user_prompt_submit",
}

VALID_ACTIONS = {"command", "prompt", "block"}


@dataclass
class HookContext:
    """Everything a hook can match on or receive as input."""

    event: str
    tool_name: str = ""
    tool_args: dict[str, Any] = field(default_factory=dict)
    file_path: str = ""
    message: str = ""


@dataclass
class HookMatch:
    """Predicate deciding whether a hook fires for a given context."""

    tool: str = ""                   # single tool name, or "*"/"" for any
    tools: list[str] = field(default_factory=list)  # match any tool in this set
    path_regex: str = ""             # regex tested against file_path

    def matches(self, ctx: HookContext) -> bool:
        names = set(self.tools)
        if self.tool:
            names.add(self.tool)
        # Empty set or "*" means "any tool"; otherwise the tool must be in the set.
        if names and "*" not in names and ctx.tool_name not in names:
            return False
        if self.path_regex:
            try:
                if not re.search(self.path_regex, ctx.file_path or ""):
                    return False
            except re.error:
                return False
        return True


@dataclass
class HookAction:
    type: str                       # command | prompt | block
    command: str = ""               # for type=command
    message: str = ""               # for type=prompt / block
    timeout: int = 15               # command timeout (seconds)


@dataclass
class HookResult:
    success: bool
    output: str
    # CC-parity structured protocol: a command hook may print JSON on stdout to
    # do more than pass/fail. All optional; empty/None means "not specified".
    decision: str = ""                       # "approve" | "block"
    updated_input: dict[str, Any] | None = None  # rewrite the tool's arguments
    additional_context: str = ""             # extra context injected next call
    continue_run: bool | None = None         # False (on a stop hook) => keep going
    system_message: str = ""                 # user-facing note / block reason


@dataclass
class Hook:
    id: str
    event: str
    action: HookAction
    match: HookMatch = field(default_factory=HookMatch)
    # When True, a pre_tool_use hook blocks the tool (reject). Implied for
    # action.type == "block".
    reject: bool = False
    run_once: bool = False
    _executed: bool = field(default=False, init=False)

    def should_run(self) -> bool:
        return not (self.run_once and self._executed)

    def mark_executed(self) -> None:
        self._executed = True
