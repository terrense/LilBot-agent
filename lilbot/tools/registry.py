from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
from time import perf_counter
from typing import Any, Callable

from ..sandbox import SandboxError


# ── Direct port from CodeWhale's tools crate ──────────────────────────────
# These enums are transplanted 1:1 from crates/tools/src/lib.rs

class ToolCapability:
    """Tool capability flags — exactly CodeWhale's enum (lines 19-32)."""
    ReadOnly = "read_only"
    WritesFiles = "writes_files"
    ExecutesCode = "executes_code"
    Network = "network"
    Sandboxable = "sandboxable"
    RequiresApproval = "requires_approval"

    # Convenience sets for tool registration
    NONE = frozenset()
    READ = frozenset({ReadOnly})
    READ_NETWORK = frozenset({ReadOnly, Network})
    CODE = frozenset({ExecutesCode})
    CODE_APPROVAL = frozenset({ExecutesCode, RequiresApproval})
    WRITE = frozenset({WritesFiles})
    WRITE_APPROVAL = frozenset({WritesFiles, RequiresApproval})


class ApprovalRequirement:
    """Tool approval mode — exactly CodeWhale's enum (lines 34-44)."""
    Auto = "auto"           # Never needs approval: safe read-only operations
    Suggest = "suggest"     # Suggest but allow skip
    Required = "required"   # Always require explicit user approval


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
    criteria: frozenset = ToolCapability.NONE
    approval_requirement: str = ApprovalRequirement.Auto


@dataclass
class ToolContext:
    sandbox: Any
    permissions: Any
    memory: Any
    skills: Any
    subagents: Any
    mcp: Any
    config: Any
    teams: Any = None
    # Identity of the current agent within a team. None => the lead/root agent.
    # Teammates run with a ctx clone where these are set, so coordination tools
    # (SendMessage / Task*) know who is acting and which team they belong to.
    team_name: Any = None
    agent_name: Any = None


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

    def schemas(self, subagent_render_context: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Return tool schemas for the LLM provider.

        When subagent_render_context is provided (from SubAgentManager.get_render_context()),
        the agent_open and agent_eval descriptions are dynamically expanded with live agent type
        listings and active subagent status — CodeWhale-style single source of truth.
        """
        schemas = [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.input_schema,
                "criteria": sorted(tool.criteria) if tool.criteria else [],
            }
            for tool in self.list()
        ]
        if subagent_render_context is not None:
            self._render_agent_descriptions(schemas, subagent_render_context)
        return schemas

    def _render_agent_descriptions(
        self,
        schemas: list[dict[str, Any]],
        ctx: dict[str, Any],
    ) -> None:
        """Dynamically expand agent_open / agent_eval descriptions from live registry."""
        try:
            from ..subagents.render import render_agent_types, render_active_agents  # noqa: PLC0415
        except ImportError:
            return

        agent_types = ctx.get("agent_types")
        active_tasks = ctx.get("active_tasks")

        for schema in schemas:
            name = schema.get("name", "")
            if name in ("agent_open", "Agent", "Task") and agent_types is not None:
                base = schema["description"]
                listing = render_agent_types(list(agent_types))
                schema["description"] = f"{base}\n\n{listing}"
            elif name == "agent_eval" and active_tasks is not None:
                base = schema["description"]
                listing = render_active_agents(list(active_tasks))
                schema["description"] = f"{base}\n\n{listing}"

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
