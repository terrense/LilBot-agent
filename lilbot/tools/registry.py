from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
from time import perf_counter
from typing import Any, Callable

from ..sandbox import SandboxError


PLAN_APPROVAL_GATED_TOOLS = {
    "write_file",
    "edit_file",
    "apply_patch",
    "bash",
    "exec_shell",
    "exec_shell_interact",
    "exec_interact",
    "task_shell_start",
    "run_tests",
    "task_gate_run",
    "code_execution",
    "js_execution",
    "pandoc_convert",
    "github_comment",
    "github_close_issue",
    "github_close_pr",
    "automation_create",
    "automation_update",
    "automation_pause",
    "automation_resume",
    "automation_delete",
    "automation_run",
    "memory_save",
    "memory_delete",
    "remember",
    "note",
    "slop_ledger_append",
    "slop_ledger_update",
    "slop_ledger_export",
    "EnterWorktree",
    "WorktreeMergeBack",
    "worktree_merge_back",
}


@dataclass
class ToolResult:
    ok: bool
    output: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolDef:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[[dict[str, Any], "ToolContext"], ToolResult]


@dataclass
class ToolContext:
    sandbox: Any
    permissions: Any
    memory: Any
    skills: Any
    subagents: Any
    mcp: Any
    config: Any


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, ToolDef] = {}

    def register(self, tool: ToolDef) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> ToolDef | None:
        return self._tools.get(name) or self._tools.get(self.resolve(name) or "")

    def resolve(self, requested: str) -> str | None:
        if requested in self._tools:
            return requested
        lower = requested.lower()
        for name in self._tools:
            if name.lower() == lower:
                return name
        snaked = lower.replace("-", "_").replace(" ", "_")
        if snaked in self._tools:
            return snaked
        camel = re.sub(r"(?<!^)(?=[A-Z])", "_", requested).lower()
        if camel in self._tools:
            return camel
        for suffix in ("_tool", "-tool"):
            if snaked.endswith(suffix) and snaked[: -len(suffix)] in self._tools:
                return snaked[: -len(suffix)]
        return None

    def list(self) -> list[ToolDef]:
        return sorted(self._tools.values(), key=lambda tool: tool.name)

    def schemas(self) -> list[dict[str, Any]]:
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.input_schema,
            }
            for tool in self.list()
        ]

    def execute(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> tuple[ToolResult, int]:
        resolved_name = self.resolve(name) or name
        tool = self.get(resolved_name)
        if not tool:
            return ToolResult(False, f"Unknown tool: {name}"), 0
        plan_gate = _plan_approval_gate(resolved_name, ctx)
        if plan_gate is not None:
            return plan_gate, 0
        started = perf_counter()
        try:
            result = tool.handler(arguments or {}, ctx)
        except SandboxError as exc:
            result = ToolResult(False, f"Sandbox error: {exc}")
        except Exception as exc:  # pragma: no cover - defensive boundary
            result = ToolResult(False, f"Tool error: {type(exc).__name__}: {exc}")
        elapsed_ms = int((perf_counter() - started) * 1000)
        if len(result.output) > 12000:
            result.output = result.output[:12000] + "\n... truncated ..."
            result.metadata["truncated"] = True
        return result, elapsed_ms


def _plan_approval_gate(tool_name: str, ctx: ToolContext) -> ToolResult | None:
    if tool_name not in PLAN_APPROVAL_GATED_TOOLS:
        return None
    config = getattr(ctx, "config", None)
    state_dir = getattr(config, "state_dir", None)
    if state_dir is None:
        return None
    try:
        data = json.loads((state_dir / "plan_mode.json").read_text(encoding="utf-8"))
    except (OSError, TypeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    approval_state = str(data.get("approval_state") or "").strip().lower()
    requires_approval = bool(data.get("requires_approval", approval_state == "pending_approval"))
    active_planning = bool(data.get("active")) and approval_state == "planning"
    pending_approval = approval_state == "pending_approval" and requires_approval
    if not active_planning and not pending_approval:
        return None
    metadata = {
        "gate": "plan_approval",
        "tool": tool_name,
        "approval_state": approval_state,
        "active": bool(data.get("active")),
        "requires_approval": requires_approval,
    }
    return ToolResult(
        False,
        (
            f"Plan approval gate denied {tool_name}. "
            "Approve or reject the current plan with ExitPlanMode before running write or execution tools."
        ),
        metadata,
    )
