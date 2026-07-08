from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
from time import perf_counter
from typing import Any, Callable

from ..sandbox import SandboxError


# ── Direct tools crate ──────────────────────────────
# Tool capability + approval-requirement enums.

class ToolCapability:
    """Tool capability flags.

    【简历·2 Tool 标准化接入｜权限边界】
    每个工具用一组 capability 标记它的“能力/风险面”：只读 / 写文件 /
    执行代码 / 联网 / 可沙箱 / 需审批。下面的便捷集合(READ、WRITE_APPROVAL…)
    让注册一个新工具时只写一行就声明清楚它的权限边界，配合
    ApprovalRequirement 决定要不要人工确认——这就是简历里“统一定义工具的
    权限边界、超时策略和错误类型”的“权限边界”落点，也是子代理按角色裁剪
    工具集(subagents/manager.py)和只读并行(concurrency_safe)的判据来源。
    """
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
    """Tool approval mode."""
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
    # 【简历·2 Tool 标准化接入｜统一契约】
    # 一个 ToolDef 就是一次“标准化接入”：name/description 给模型看，
    # input_schema 是 JSON Schema（约束工具的输入输出结构），criteria 声明
    # 权限边界，approval_requirement 声明审批策略，handler 是真正的实现。
    # 搜索/知识库检索/文件处理/DB 查询/代码执行/外部 API 只要各写一个
    # ToolDef 注册进来，主循环就能以完全一致的方式调用——这正是“新 Tool
    # 接入时间由 ~60min 缩短到 ~20min”的结构性原因（模板固定，只填 4 项）。
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[[dict[str, Any], "ToolContext"], ToolResult]
    criteria: frozenset = ToolCapability.NONE
    approval_requirement: str = ApprovalRequirement.Auto
    # When True, this tool's schema is NOT sent to the model on every turn.
    # Its name is advertised in a lightweight reminder, and the model must load
    # the full schema on demand via the ToolSearch tool. This keeps the per-turn
    # tool payload small even though LilBot registers ~150 tools.
    should_defer: bool = False

    @property
    def concurrency_safe(self) -> bool:
        """True when this tool is pure read-only and can run in a parallel batch.

        Mirrors the is_concurrency_safe: a tool is safe to run alongside
        others only if it neither writes files, executes code, nor requires
        approval. Used by the agent loop to fan out independent read calls.
        """
        unsafe = {
            ToolCapability.WritesFiles,
            ToolCapability.ExecutesCode,
            ToolCapability.RequiresApproval,
        }
        return ToolCapability.ReadOnly in self.criteria and not (self.criteria & unsafe)


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
        # Deferred tools whose schema has been loaded on demand (via ToolSearch
        # or by being called directly). Once discovered, a tool is rendered in
        # schemas() like any normal tool for the rest of the session.
        self._discovered: set[str] = set()
        # Cached serialized catalog for byte-stable prefix caching (M5). Keyed by the visible
        # tool set; rebuilt only when that set changes.
        self._catalog_cache: list[dict[str, Any]] | None = None
        self._catalog_sig: frozenset[str] | None = None

    def register(self, tool: ToolDef) -> None:
        self._tools[tool.name] = tool
        self._catalog_cache = None  # invalidate

    def _visible_signature(self) -> frozenset[str]:
        return frozenset(t.name for t in self._tools.values() if self._visible(t))

    def _base_catalog(self) -> list[dict[str, Any]]:
        """Visible-tool schemas, cached and reused while the set is unchanged.

        Returning the same canonical list across turns keeps the serialized
        `tools` payload byte-stable, which is what lets DeepSeek/OpenAI prefix
        caching stay warm. Discovering a deferred tool changes the signature and
        rebuilds the cache.
        """
        sig = self._visible_signature()
        if self._catalog_cache is None or self._catalog_sig != sig:
            self._catalog_cache = [self._schema_of(t) for t in self.list() if self._visible(t)]
            self._catalog_sig = sig
        return self._catalog_cache

    def catalog_fingerprint(self) -> str:
        """Stable hash of the visible tool catalog — equal across turns when the
        tool set is unchanged, different after a deferred tool is discovered."""
        import hashlib
        blob = json.dumps(self._base_catalog(), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]

    # -- Deferred-tool support ----------------------

    def defer_all_except(self, core: set[str]) -> None:
        """Mark every registered tool deferred unless it is in the core set.

        【简历·2/4 面试可重点讲：工具规模化 = 一种上下文压缩】
        LilBot 注册了约 150 个工具，若每轮都把全部 schema 塞进 prompt，会白白
        烧掉大量 token。这里用“核心工具常驻 + 长尾工具延迟加载”的策略：只把
        日常高频工具的 schema 发给模型，其余只在一行提醒里报名字，模型需要时
        用 ToolSearch 按需拉取(mark_discovered)。这既控制了每轮 payload 大小，
        又让工具目录字节稳定(见 _base_catalog 的缓存)以命中服务端 prefix 缓存。

        Allowlist approach: the daily-driver tools stay loaded each turn; the
        long tail (LSP, github, automation, rlm, slop ledger, aliases, …) is
        loaded on demand. New tools added later default to deferred unless
        explicitly added to the core set — keeping the per-turn payload bounded.
        """
        for name, tool in self._tools.items():
            tool.should_defer = name not in core

    def mark_discovered(self, name: str) -> None:
        resolved = self.resolve(name)
        if resolved:
            self._discovered.add(resolved)

    def _visible(self, tool: ToolDef) -> bool:
        return (not tool.should_defer) or (tool.name in self._discovered)

    def deferred_tool_names(self) -> list[str]:
        return sorted(
            t.name
            for t in self._tools.values()
            if t.should_defer and t.name not in self._discovered
        )

    def all_schemas(self) -> list[dict[str, Any]]:
        """Every registered tool's schema, ignoring deferral.

        schemas() answers "what to advertise to the lead agent this turn";
        all_schemas() is the full catalog used for resolution and for building a
        subagent's tool set (a subagent narrows tools via its own allowed-tools
        list, so deferral must not hide candidates from it).
        """
        return [self._schema_of(tool) for tool in self.list()]

    def _schema_of(self, tool: ToolDef) -> dict[str, Any]:
        return {
            "name": tool.name,
            "description": tool.description,
            "input_schema": tool.input_schema,
            "criteria": sorted(tool.criteria) if tool.criteria else [],
        }

    def find_deferred_by_names(self, names: list[str]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for raw in names:
            resolved = self.resolve(raw.strip())
            tool = self._tools.get(resolved or "")
            if tool is not None:
                out.append(self._schema_of(tool))
        return out

    def search_deferred(self, query: str, max_results: int = 5) -> list[dict[str, Any]]:
        terms = [t.lower() for t in re.split(r"[\s,]+", query) if t.strip()]
        scored: list[tuple[int, ToolDef]] = []
        for tool in self._tools.values():
            if not tool.should_defer or tool.name in self._discovered:
                continue
            blob = f"{tool.name} {tool.description}".lower()
            score = sum(blob.count(term) for term in terms)
            # Name hits weigh more than description hits.
            score += sum(3 for term in terms if term in tool.name.lower())
            if score:
                scored.append((score, tool))
        scored.sort(key=lambda item: (item[0], item[1].name), reverse=True)
        return [self._schema_of(tool) for _, tool in scored[: max(1, max_results)]]

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
        listings and active subagent status — single source of truth.
        """
        base = self._base_catalog()
        if subagent_render_context is None:
            return base
        # Copy each schema before the dynamic agent-description mutation so the
        # cached canonical catalog stays byte-stable.
        schemas = [dict(s) for s in base]
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
        # If the model called a deferred tool directly (without ToolSearch),
        # honor it and reveal its schema for subsequent turns — no regression.
        if tool.should_defer and tool.name not in self._discovered:
            self._discovered.add(tool.name)
        plan_gate = _plan_approval_gate(resolved_name, ctx)
        if plan_gate is not None:
            return plan_gate, 0
        # 【简历·2/5 错误类型统一 + 耗时观测】
        # 所有工具都从这一个入口执行：perf_counter 计时得到 elapsed_ms（喂给
        # 执行观测），try/except 把任何异常收敛成统一的 ToolResult(ok=False,...)
        # ——沙箱错误、未知异常都变成“可回灌给模型的错误消息”，绝不让单个
        # 工具异常炸掉整轮。这就是“统一错误类型”的落点。
        started = perf_counter()
        try:
            result = tool.handler(arguments or {}, ctx)
        except SandboxError as exc:
            result = ToolResult(False, f"Sandbox error: {exc}")
        except Exception as exc:  # pragma: no cover - defensive boundary
            result = ToolResult(False, f"Tool error: {type(exc).__name__}: {exc}")
        elapsed_ms = int((perf_counter() - started) * 1000)
        # Offload large outputs to disk with a recoverable preview instead of
        # silently dropping the tail. retrieve_tool_result
        # / handle_read can read the persisted file back.
        from .offload import maybe_offload  # local import avoids a cycle

        state_dir = getattr(getattr(ctx, "config", None), "state_dir", None)
        new_output, extra = maybe_offload(result.output, state_dir)
        if extra:
            result.output = new_output
            result.metadata.update(extra)
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
