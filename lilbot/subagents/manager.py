from __future__ import annotations

import json
import os
import shutil
import threading
import time
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable
from uuid import uuid4

from ..core.events import ProviderTurn
from ..sandbox import Sandbox

if TYPE_CHECKING:
    from ..tools.registry import ToolContext


ProviderCallable = Callable[[list[dict], list[dict]], ProviderTurn]
SUBAGENT_MAX_TOOL_STEPS = 6
# Long-running teammates explore *and* act within a single turn, so they need a
# larger per-turn tool budget than one-shot subagent probes.
TEAMMATE_MAX_TOOL_STEPS = 16
DEFAULT_SUBAGENT_MAX_CONCURRENT = 8

# Coordination tools injected into every teammate so it can talk to the team and
# use the shared task board. Registered globally in builtin.py; teammates read
# their identity (team_name / agent_name) from a ctx clone.
TEAM_COORDINATION_TOOL_NAMES = [
    "send_message",
    "team_task_create",
    "team_task_list",
    "team_task_get",
    "team_task_update",
]

READ_ONLY_CODE_TOOLS = [
    "project_map",
    "list_dir",
    "read_file",
    "glob",
    "grep",
    "grep_files",
    "file_search",
    "handle_read",
    "retrieve_tool_result",
    "git_status",
    "git_diff",
    "git_log",
    "git_show",
    "git_blame",
    "lsp_symbols",
    "lsp_definition",
    "lsp_workspace_symbols",
    "lsp_references",
    "lsp_diagnostics",
    "lsp_rename_preview",
]
DIAGNOSTIC_TOOLS = ["diagnostics", "run_tests", "task_gate_run"]
WRITE_TOOLS = ["write_file", "edit_file", "apply_patch"]
WEB_TOOLS = ["web_search", "fetch_url", "web_fetch", "web_run"]
AGENT_TOOLS = ["agent_open", "agent_eval", "agent_close", "agent_spawn", "agent_status", "tool_agent", "Agent", "Task"]
SUBAGENT_ALWAYS_DISALLOWED_TOOLS = [
    *AGENT_TOOLS,
    "EnterPlanMode",
    "ExitPlanMode",
    "request_user_input",
    "TaskOutputTool",
    "TaskStop",
    "WorkflowTool",
    "multi_tool_use.parallel",
]
WRITE_AND_EXECUTION_TOOLS = [
    *WRITE_TOOLS,
    "bash",
    "exec_shell",
    "exec_shell_wait",
    "exec_wait",
    "exec_shell_interact",
    "exec_interact",
    "exec_shell_cancel",
    "task_shell_start",
    "task_shell_wait",
    "code_execution",
    "js_execution",
    "pandoc_convert",
    "memory_save",
    "memory_delete",
    "remember",
    "note",
    "slop_ledger_append",
    "slop_ledger_update",
    "slop_ledger_export",
    "automation_create",
    "automation_update",
    "automation_delete",
    "automation_run",
    "github_comment",
    "github_close_issue",
    "github_close_pr",
]

TOOL_COMPATIBILITY_GROUPS = {
    "read": ["read_file", "handle_read", "retrieve_tool_result"],
    "grep": ["grep", "grep_files"],
    "glob": ["glob", "file_search"],
    "write": ["write_file"],
    "edit": ["edit_file"],
    "multiedit": ["edit_file", "apply_patch"],
    "bash": [
        "bash",
        "exec_shell",
        "exec_shell_wait",
        "exec_wait",
        "exec_shell_interact",
        "exec_interact",
        "exec_shell_cancel",
        "task_shell_start",
        "task_shell_wait",
    ],
    "task": ["Agent", "Task", "agent_open", "agent_eval", "agent_close", "tool_agent"],
    "websearch": ["web_search", "web_run"],
    "webfetch": ["fetch_url", "web_fetch", "web_run"],
    "todowrite": [
        "todo_write",
        "todo_add",
        "todo_update",
        "todo_list",
        "checklist_write",
        "checklist_add",
        "checklist_update",
        "checklist_list",
        "update_plan",
    ],
}


class SubAgentGateError(ValueError):
    def __init__(self, failures: list[dict[str, Any]]):
        self.failures = failures
        first = failures[0] if failures else {"gate": "unknown", "message": "Subagent gate failed."}
        super().__init__(str(first.get("message") or "Subagent gate failed."))

    def to_dict(self) -> dict[str, Any]:
        return {"error": str(self), "gates": self.failures}


@dataclass
class AgentDefinition:
    name: str
    description: str
    system_hint: str
    writes: bool = False
    shell: str = "minimal"
    allowed_tools: list[str] = field(default_factory=list)
    disallowed_tools: list[str] = field(default_factory=list)
    model: str | None = None
    source: str = "built-in"
    when_to_use: str = ""


@dataclass
class SubAgentTask:
    id: str
    agent_type: str
    prompt: str
    name: str | None = None
    status: str = "queued"
    result: str = ""
    error: str = ""
    allowed_tools: list[str] = field(default_factory=list)
    model: str | None = None
    fork_context: bool = False
    gate_results: list[dict[str, Any]] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    messages: list[str] = field(default_factory=list)
    transcript_handle: str = ""
    transcript_path: str = ""
    recovered: bool = False
    worktree_isolation: str | None = None
    worktree_path: str = ""
    worktree_branch: str = ""
    worktree_created: bool = False
    worktree_status: str = "none"
    worktree_error: str = ""
    cleanup_worktree: bool = True
    progress_event_count: int = 0
    last_event: str = ""
    resume_count: int = 0

    @property
    def terminal(self) -> bool:
        return self.status in {"completed", "failed", "cancelled", "done", "error"}


OUTPUT_CONTRACT = """When you finish, end with this compact report:
SUMMARY: one paragraph
CHANGES: files modified, or None.
EVIDENCE: concrete observations
RISKS: unresolved risks, or None observed.
BLOCKERS: blockers, or None.
"""


# ============================================================
# 【简历·3 多 Agent 协作｜Specialist Agents 角色库】
# 这张表就是“Supervisor + Specialist”里的一组专家角色定义：
# explore(检索/只读探查)、researcher(联网调研)、review(代码审查)、
# implementer(改代码)、verifier(只跑验证不改代码)、plan/writer/critic…
# 每个 AgentDefinition 绑定了：能力边界(writes)、可用工具白名单
# (allowed_tools)、禁用工具(disallowed_tools)、系统提示(system_hint)。
# Supervisor(主 Agent)按任务把子任务派给对应角色 —— 检索 Agent、分析
# Agent、执行 Agent、验证 Agent 各司其职，正是简历描述的分工结构。
# 关键安全点：子代理的工具集是“角色白名单 ∩ 全局禁用表”，从源头上做到
# 最小权限(见 _tool_schemas_for_task / _execute_tool_call 的多道 gate)。
# ============================================================
DEFAULT_AGENT_TYPES = [
    AgentDefinition(
        "general",
        "Flexible worker for multi-step tasks.",
        "You are a general-purpose subagent. Do the assigned work carefully and report with evidence.",
        writes=True,
        shell="yes",
        allowed_tools=[
            *READ_ONLY_CODE_TOOLS,
            "load_skill",
            "skill_list",
            "diagnostics",
            *WEB_TOOLS,
            *WRITE_TOOLS,
            "run_tests",
        ],
    ),
    AgentDefinition(
        "explore",
        "Read-only explorer for fast local evidence gathering.",
        "You are an explorer. Map relevant files and facts. Do not edit files.",
        writes=False,
        shell="read-only",
        allowed_tools=[*READ_ONLY_CODE_TOOLS, "diagnostics"],
        disallowed_tools=WRITE_AND_EXECUTION_TOOLS,
    ),
    AgentDefinition(
        "researcher",
        "Researcher for public facts, recommendations, and multi-source synthesis.",
        (
            "You are a researcher. Gather facts, compare alternatives, cite URLs when web tools influence "
            "the answer, and call out uncertainty. Do not edit files."
        ),
        writes=False,
        shell="web",
        allowed_tools=[*WEB_TOOLS],
        disallowed_tools=WRITE_AND_EXECUTION_TOOLS,
    ),
    AgentDefinition(
        "plan",
        "Planner that decomposes work, risks, and verification.",
        "You are a planner. Produce a concrete plan and do not implement it.",
        writes=False,
        shell="minimal",
        allowed_tools=[],
        disallowed_tools=WRITE_AND_EXECUTION_TOOLS,
    ),
    AgentDefinition(
        "review",
        "Reviewer that looks for bugs, regressions, and missing tests.",
        "You are a code reviewer. Prioritize correctness risks and cite evidence.",
        writes=False,
        shell="read-only",
        allowed_tools=[*READ_ONLY_CODE_TOOLS, "diagnostics"],
        disallowed_tools=WRITE_AND_EXECUTION_TOOLS,
    ),
    AgentDefinition(
        "writer",
        "Writer for outlines, drafts, rewrites, and style adaptation.",
        (
            "You are a writer and editor. Analyze audience, purpose, rubric, structure, tone, and clarity. "
            "Draft or revise text without editing workspace files."
        ),
        writes=False,
        shell="none",
        allowed_tools=[],
        disallowed_tools=WRITE_AND_EXECUTION_TOOLS,
    ),
    AgentDefinition(
        "critic",
        "General critic for plans, writing, recommendations, and reasoning gaps.",
        (
            "You are a critic. Find weak assumptions, missing constraints, unclear structure, unsupported "
            "claims, and practical risks. Be concise and evidence-oriented."
        ),
        writes=False,
        shell="minimal",
        allowed_tools=[],
        disallowed_tools=WRITE_AND_EXECUTION_TOOLS,
    ),
    AgentDefinition(
        "implementer",
        "Focused implementer for a specified code change.",
        "You are an implementer. Make the smallest correct change and verify it.",
        writes=True,
        shell="yes",
        allowed_tools=[
            *READ_ONLY_CODE_TOOLS,
            *WRITE_TOOLS,
            "run_tests",
            "diagnostics",
            "bash",
        ],
    ),
    AgentDefinition(
        "verifier",
        "Validation runner that reports pass/fail evidence without fixing failures.",
        "You are a verifier. Run or reason about validation and report exact outcomes.",
        writes=False,
        shell="test-focused",
        allowed_tools=[*READ_ONLY_CODE_TOOLS, *DIAGNOSTIC_TOOLS],
        disallowed_tools=[*WRITE_TOOLS, "github_comment", "github_close_issue", "github_close_pr"],
    ),
    AgentDefinition(
        "tool_agent",
        "Fast tool-bound executor for simple lookups and probes.",
        "You are a fast execution agent. Keep output compact and avoid nuanced architecture decisions.",
        writes=False,
        shell="bounded",
        allowed_tools=[
            "project_map",
            "list_dir",
            "read_file",
            "glob",
            "grep",
            "grep_files",
            "file_search",
            *WEB_TOOLS,
            "diagnostics",
        ],
        disallowed_tools=WRITE_AND_EXECUTION_TOOLS,
    ),
    AgentDefinition(
        "custom",
        "Caller-constrained role with an explicit allowed tool list.",
        "You are a constrained custom subagent. Follow the caller's tool limits strictly.",
    ),
]


ROLE_ALIASES = {
    "general-purpose": "general",
    "general_purpose": "general",
    "worker": "general",
    "default": "general",
    "coder": "implementer",
    "exploration": "explore",
    "explorer": "explore",
    "research": "researcher",
    "web-research": "researcher",
    "web_research": "researcher",
    "investigator": "researcher",
    "planning": "plan",
    "awaiter": "plan",
    "planner": "plan",
    "writer": "writer",
    "writing": "writer",
    "essay": "writer",
    "copywriter": "writer",
    "editor": "writer",
    "rewriter": "writer",
    "critic": "critic",
    "critiquer": "critic",
    "code-review": "review",
    "code_review": "review",
    "reviewer": "review",
    "implement": "implementer",
    "implementation": "implementer",
    "builder": "implementer",
    "verify": "verifier",
    "verification": "verifier",
    "validator": "verifier",
    "tester": "verifier",
    "tool-agent": "tool_agent",
    "toolagent": "tool_agent",
    "executor": "tool_agent",
    "execution": "tool_agent",
    "fin": "tool_agent",
}


class SubAgentManager:
    def __init__(
        self,
        provider: ProviderCallable,
        agents_dir: Path | None = None,
        transcripts_dir: Path | None = None,
        max_concurrent: int | None = None,
    ):
        self.provider = provider
        self.agents_dir = agents_dir
        self.state_dir = agents_dir.parent if agents_dir is not None else None
        self.transcripts_dir = transcripts_dir or (
            self.state_dir / "subagent-transcripts" if self.state_dir is not None else None
        )
        self.tasks_path = self.state_dir / "subagent-tasks.json" if self.state_dir is not None else None
        self.max_concurrent = max(1, int(max_concurrent or _env_int("LILBOT_SUBAGENT_MAX_CONCURRENT", DEFAULT_SUBAGENT_MAX_CONCURRENT)))
        self._semaphore = threading.BoundedSemaphore(self.max_concurrent)
        self._resume_started: set[str] = set()
        self.registry: Any | None = None
        self.ctx: Any | None = None
        self.definitions = {d.name: d for d in DEFAULT_AGENT_TYPES}
        self.tasks: dict[str, SubAgentTask] = {}
        self._lock = threading.RLock()
        self.reload_custom_agents()
        self._load_persisted_tasks()

    def configure_tools(self, registry: Any, ctx: Any) -> None:
        self.registry = registry
        self.ctx = ctx
        self._resume_recovered_tasks()

    def list_types(self) -> list[AgentDefinition]:
        return sorted(self.definitions.values(), key=lambda item: item.name)

    def list_tasks(self) -> list[SubAgentTask]:
        with self._lock:
            return sorted(self.tasks.values(), key=lambda item: item.created_at, reverse=True)

    def get_render_context(self) -> dict[str, object]:
        """Snapshot of agent types and active tasks for dynamic tool descriptions.

        Used by ToolRegistry.schemas() to render agent_open/agent_eval descriptions
        with live agent type listings and active subagent status — 
        tool description parity (single source of truth, no keyword heuristics).
        """
        return {
            "agent_types": self.list_types(),
            "active_tasks": self.list_tasks(),
            "max_concurrent": self.max_concurrent,
            "running_count": self._count_status("running"),
        }

    def get(self, task_id: str) -> SubAgentTask | None:
        with self._lock:
            return self.tasks.get(task_id) or next((t for t in self.tasks.values() if t.name == task_id), None)

    def resolve_type(self, agent_type: str | None) -> str:
        self.reload_custom_agents()
        value = (agent_type or "general").strip().lower().replace(" ", "_")
        value = ROLE_ALIASES.get(value, value)
        return value if value in self.definitions else "general"

    def ensure_agents_dir(self) -> Path:
        if self.agents_dir is None:
            raise ValueError("No agents directory configured.")
        self.agents_dir.mkdir(parents=True, exist_ok=True)
        return self.agents_dir

    def reload_custom_agents(self) -> None:
        if self.agents_dir is None or not self.agents_dir.exists():
            return
        custom = {d.name: d for d in DEFAULT_AGENT_TYPES}
        for path in sorted(self.agents_dir.glob("*.md")) + sorted(self.agents_dir.glob("*/AGENT.md")):
            definition = self._parse_agent_file(path)
            if definition:
                custom[definition.name] = definition
        with self._lock:
            self.definitions = custom

    def _parse_agent_file(self, path: Path) -> AgentDefinition | None:
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return None
        meta, body = _parse_agent_markdown(raw)
        name = str(meta.get("name") or path.stem).strip()
        description = str(meta.get("description") or "").strip()
        system_hint = body.strip()
        if not name or not description or not system_hint:
            return None
        return AgentDefinition(
            name=name,
            description=description,
            system_hint=system_hint,
            writes=_as_bool(meta.get("writes"), False),
            shell=str(meta.get("shell") or "minimal"),
            allowed_tools=_as_list(meta.get("tools") or meta.get("allowed-tools") or meta.get("allowed-tools-list")),
            disallowed_tools=_as_list(meta.get("disallowed-tools") or meta.get("disallowedtools")),
            model=str(meta.get("model") or "") or None,
            source=str(path),
            when_to_use=str(
                meta.get("when_to_use")
                or meta.get("when-to-use")
                or meta.get("whenToUse")
                or meta.get("when")
                or description
            ).strip(),
        )

    def spawn(self, agent_type: str, prompt: str, background: bool = False) -> SubAgentTask:
        return self.open(agent_type=agent_type, prompt=prompt, background=background)

    def open(
        self,
        agent_type: str | None,
        prompt: str,
        *,
        name: str | None = None,
        background: bool = True,
        allowed_tools: list[str] | None = None,
        model: str | None = None,
        fork_context: bool = False,
        isolation: str | None = None,
        cleanup_worktree: bool = True,
        worktree_branch: str | None = None,
    ) -> SubAgentTask:
        canonical = self.resolve_type(agent_type)
        definition = self.definitions[canonical]
        isolation = _normalize_isolation(isolation)
        effective_allowed_tools = _dedupe_tools(definition.allowed_tools if allowed_tools is None else allowed_tools)
        creation_gates = self._validate_creation_gates(
            canonical,
            definition,
            effective_allowed_tools,
            explicit_allowed_tools=allowed_tools is not None or bool(definition.allowed_tools),
        )
        failures = [gate for gate in creation_gates if gate.get("status") == "failed" and int(gate.get("gate_number", 0)) <= 3]
        if failures:
            raise SubAgentGateError(failures)
        task_id = f"sub_{uuid4().hex[:10]}"
        task = SubAgentTask(
            id=task_id,
            name=name or task_id,
            agent_type=canonical,
            prompt=prompt,
            allowed_tools=effective_allowed_tools,
            model=model,
            fork_context=fork_context,
            gate_results=creation_gates,
            worktree_isolation=isolation,
            worktree_branch=str(worktree_branch or "").strip(),
            cleanup_worktree=cleanup_worktree,
            worktree_status="requested" if isolation == "worktree" else "none",
        )
        with self._lock:
            self.tasks[task.id] = task
            self._persist_tasks_locked()
        self._append_transcript(task, "queued", {"agent_type": canonical, "name": task.name, "prompt": prompt})
        if background:
            thread = threading.Thread(target=self._run, args=(task,), daemon=True)
            thread.start()
        else:
            self._run(task)
        return task

    def build_teammate_task(
        self,
        agent_type: str | None,
        prompt: str,
        *,
        name: str | None = None,
        allowed_tools: list[str] | None = None,
        model: str | None = None,
    ) -> SubAgentTask:
        """Create (but do not run) a persisted task for a long-running teammate.

        The teammate's tool set = its role definition's tools plus the team
        coordination tools, so it can always message the team and use the shared
        task board. Reuses the same creation gates as ``open``.
        """
        canonical = self.resolve_type(agent_type)
        definition = self.definitions[canonical]
        base_tools = definition.allowed_tools if allowed_tools is None else allowed_tools
        effective = _dedupe_tools([*base_tools, *TEAM_COORDINATION_TOOL_NAMES])
        creation_gates = self._validate_creation_gates(
            canonical,
            definition,
            effective,
            explicit_allowed_tools=allowed_tools is not None or bool(definition.allowed_tools),
        )
        failures = [g for g in creation_gates if g.get("status") == "failed" and int(g.get("gate_number", 0)) <= 3]
        if failures:
            raise SubAgentGateError(failures)
        task_id = f"sub_{uuid4().hex[:10]}"
        task = SubAgentTask(
            id=task_id,
            name=name or task_id,
            agent_type=canonical,
            prompt=prompt,
            allowed_tools=effective,
            model=model,
            gate_results=creation_gates,
            status="running",
            started_at=time.time(),
        )
        with self._lock:
            self.tasks[task.id] = task
            self._persist_tasks_locked()
        self._append_transcript(task, "teammate_spawned", {"agent_type": canonical, "name": task.name})
        return task

    def slot(self):
        """Context manager bounding active teammate turns by max_concurrent."""
        return self._semaphore

    def worktree_available(self) -> bool:
        """True when the workspace can host git worktrees (for teammate isolation)."""
        if self.ctx is None:
            return False
        sandbox = getattr(self.ctx, "sandbox", None)
        if sandbox is None or not shutil.which("git"):
            return False
        try:
            if not sandbox.run("git rev-parse --show-toplevel", 10).ok:
                return False
            return sandbox.run("git worktree list --porcelain", 10).ok
        except Exception:
            return False

    def ctx_for_task(self, task: SubAgentTask) -> Any:
        """Public accessor for a task's worktree-scoped tool context."""
        return self._ctx_for_task(task)

    def eval(
        self,
        task_ref: str,
        *,
        message: str | None = None,
        block: bool = True,
        timeout: float = 30.0,
    ) -> SubAgentTask | None:
        task = self.get(task_ref)
        if not task:
            return None
        if message:
            with self._lock:
                task.messages.append(message)
                if task.status == "queued":
                    task.prompt = f"{task.prompt}\n\nFollow-up:\n{message}"
                self._persist_tasks_locked()
            self._append_transcript(task, "follow_up", {"message": message})
        if block:
            deadline = time.time() + max(0.1, timeout)
            while time.time() < deadline:
                if task.terminal:
                    break
                time.sleep(0.05)
        return task

    def close(self, task_ref: str) -> SubAgentTask | None:
        task = self.get(task_ref)
        if not task:
            return None
        cancelled = False
        with self._lock:
            if not task.terminal:
                task.status = "cancelled"
                task.error = "Cancelled by parent."
                task.finished_at = time.time()
                cancelled = True
                self._persist_tasks_locked()
        if cancelled:
            self._append_transcript(task, "cancelled", {"error": task.error})
        return task

    def transcript(self, task_ref: str, *, after: int = 0, limit: int = 100) -> dict[str, Any] | None:
        task = self.get(task_ref)
        if not task:
            return None
        path = Path(task.transcript_path) if task.transcript_path else None
        events: list[dict[str, Any]] = []
        total = 0
        if path and path.exists():
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                lines = []
            total = len(lines)
            start = max(0, min(int(after), total))
            stop = min(total, start + max(1, min(int(limit), 500)))
            for index, raw in enumerate(lines[start:stop], start):
                try:
                    record = json.loads(raw)
                except json.JSONDecodeError:
                    record = {"event": "transcript_parse_error", "raw": raw}
                record["cursor"] = index + 1
                events.append(record)
        next_cursor = events[-1]["cursor"] if events else max(0, min(int(after), total))
        return {
            "task": self.projection(task),
            "handle": task.transcript_handle or None,
            "events": events,
            "cursor": next_cursor,
            "total_events": total,
            "has_more": next_cursor < total,
        }

    def projection(self, task: SubAgentTask) -> dict[str, object]:
        duration_ms = int(((task.finished_at or time.time()) - task.created_at) * 1000)
        return {
            "name": task.name or task.id,
            "agent_id": task.id,
            "agent_type": task.agent_type,
            "status": task.status,
            "terminal": task.terminal,
            "assignment": {"objective": task.prompt, "role": task.agent_type},
            "allowed_tools": task.allowed_tools,
            "model": task.model,
            "fork_context": task.fork_context,
            "gate_results": task.gate_results,
            "transcript_handle": task.transcript_handle or None,
            "recovered": task.recovered,
            "concurrency": {
                "max": self.max_concurrent,
                "running": self._count_status("running"),
                "queued": self._count_status("queued"),
            },
            "worktree": {
                "isolation": task.worktree_isolation,
                "status": task.worktree_status,
                "path": task.worktree_path or None,
                "branch": task.worktree_branch or None,
                "created": task.worktree_created,
                "cleanup": task.cleanup_worktree,
                "error": task.worktree_error or None,
            },
            "progress": {
                "events": task.progress_event_count,
                "last_event": task.last_event or None,
                "resume_count": task.resume_count,
            },
            "duration_ms": duration_ms,
            "result": task.result if task.terminal else None,
            "error": task.error or None,
        }

    def _run(self, task: SubAgentTask) -> None:
        definition = self.definitions[task.agent_type]
        self._append_transcript(task, "waiting_for_slot", {"max_concurrent": self.max_concurrent})
        self._semaphore.acquire()
        acquired = True
        with self._lock:
            if task.status == "cancelled":
                self._persist_tasks_locked()
                self._semaphore.release()
                return
            task.status = "running"
            task.started_at = time.time()
            if task.recovered:
                task.error = ""
            self._persist_tasks_locked()
        self._append_transcript(task, "running", {"agent_type": task.agent_type})
        try:
            if not self._prepare_worktree(task):
                return
            task.result = self.run_agent_turn(definition, task, task.prompt)
            completed = False
            with self._lock:
                if task.status != "cancelled":
                    task.status = "completed"
                    completed = True
                    self._persist_tasks_locked()
            if completed:
                self._append_transcript(task, "completed", {"result": task.result})
        except Exception as exc:  # pragma: no cover - defensive boundary
            failed = False
            with self._lock:
                if task.status != "cancelled":
                    task.error = str(exc)
                    task.status = "failed"
                    failed = True
                    self._persist_tasks_locked()
            if failed:
                self._append_transcript(task, "failed", {"error": str(exc)})
        finally:
            self._cleanup_worktree(task)
            with self._lock:
                task.finished_at = task.finished_at or time.time()
                self._persist_tasks_locked()
            if acquired:
                self._semaphore.release()

    def run_agent_turn(
        self,
        definition: AgentDefinition,
        task: SubAgentTask,
        prompt: str,
        *,
        progress: Any = None,
        run_ctx: Any = None,
        max_steps: int | None = None,
    ) -> str:
        """Execute one full provider+tool loop and return the contract-wrapped result.

        Extracted from ``_run`` so long-running teammates can reuse the exact same
        gates, tool filtering, and transcript machinery for every turn. ``progress``
        (a TeammateProgress) is updated live for the dashboard; ``run_ctx`` pins the
        tool context (e.g. a teammate's prepared worktree) instead of re-deriving it.
        """
        limit = SUBAGENT_MAX_TOOL_STEPS if max_steps is None else max(1, int(max_steps))
        messages = [
            {"role": "system", "content": f"{definition.system_hint}\n\n{self._tool_policy_hint(definition, task)}\n\n{OUTPUT_CONTRACT}"},
            {"role": "user", "content": prompt},
        ]
        transcript: list[str] = []
        content = ""
        tool_schemas = self._tool_schemas_for_task(definition, task)
        steps = 0
        while steps <= limit:
            turn = self.provider(messages, tool_schemas)
            if progress is not None:
                self._record_progress_usage(progress, turn)
            self._append_transcript(
                task,
                "provider_turn",
                {
                    "content": turn.content,
                    "tool_calls": [
                        {"name": call.name, "arguments": call.arguments, "call_id": call.call_id}
                        for call in turn.tool_calls
                    ],
                    "usage": turn.usage,
                },
            )
            if turn.content.strip():
                content = turn.content.strip()
                transcript.append(content)
                if progress is not None:
                    progress.set_message(content[:400])
            if not turn.tool_calls:
                break
            if steps >= limit:
                content = self._step_limit_report(transcript)
                break
            messages.append(_assistant_tool_message(turn))
            for call in turn.tool_calls[: max(0, limit - steps)]:
                if progress is not None:
                    progress.record_tool_use(call.name, call.arguments)
                self._append_transcript(
                    task,
                    "tool_started",
                    {"name": call.name, "arguments": call.arguments, "call_id": call.call_id},
                )
                tool_output = self._execute_tool_call(definition, task, call.name, call.arguments, run_ctx=run_ctx)
                steps += 1
                transcript.append(f"{call.name}: {tool_output[:1000]}")
                self._append_transcript(
                    task,
                    "tool_finished",
                    {"name": call.name, "call_id": call.call_id, "output": tool_output},
                )
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.call_id,
                    "name": call.name,
                    "content": tool_output,
                })
                if steps >= limit:
                    break
        return self._ensure_output_contract(content or "(subagent returned no text)")

    def _record_progress_usage(self, progress: Any, turn: Any) -> None:
        usage = getattr(turn, "usage", None) or {}
        try:
            progress.record_tokens(int(usage.get("prompt_tokens", 0) or 0), int(usage.get("completion_tokens", 0) or 0))
        except Exception:
            pass

    def _ensure_output_contract(self, content: str) -> str:
        if "SUMMARY:" in content and "BLOCKERS:" in content:
            return content
        return "\n".join(
            [
                "SUMMARY: " + content,
                "CHANGES: None.",
                "EVIDENCE: Subagent response generated by provider.",
                "RISKS: None observed.",
                "BLOCKERS: None.",
            ]
        )

    def _tool_policy_hint(self, definition: AgentDefinition, task: SubAgentTask) -> str:
        allowed = self._effective_allowed_tools(definition, task)
        disallowed = definition.disallowed_tools
        lines = []
        if allowed:
            lines.append("Allowed tools for this agent: " + ", ".join(allowed) + ".")
        if disallowed:
            lines.append("Disallowed tools for this agent: " + ", ".join(disallowed) + ".")
        if definition.model or task.model:
            lines.append("Preferred model: " + str(task.model or definition.model) + ".")
        return "\n".join(lines) or "No additional tool policy."

    def _effective_allowed_tools(self, definition: AgentDefinition, task: SubAgentTask) -> list[str]:
        return _dedupe_tools(task.allowed_tools)

    def _validate_creation_gates(
        self,
        canonical: str,
        definition: AgentDefinition,
        allowed_tools: list[str],
        *,
        explicit_allowed_tools: bool,
    ) -> list[dict[str, Any]]:
        if not self._uses_custom_tool_gates(canonical, definition):
            return []
        gates = []
        gates.append({
            "gate_number": 1,
            "gate": "custom_allowed_tools_shape",
            "status": "passed" if explicit_allowed_tools else "failed",
            "message": (
                "Custom subagents require an explicit allowed_tools list. "
                "Use [] for a no-tool custom agent or provide concrete tool names."
            ),
        })

        invalid_tools = [
            tool for tool in allowed_tools
            if _tool_spec_name(tool) != "*" and not self._tool_spec_resolves(tool)
        ]
        gates.append({
            "gate_number": 2,
            "gate": "custom_allowed_tools_resolve",
            "status": "failed" if invalid_tools else "passed",
            "invalid_tools": invalid_tools,
            "message": (
                "Custom subagent allowed_tools contains unknown tools: " + ", ".join(invalid_tools)
                if invalid_tools
                else "All custom subagent allowed_tools entries resolve to known tools or compatibility aliases."
            ),
        })

        denied_tools = [
            tool for tool in allowed_tools
            if _tool_spec_name(tool) != "*" and self._tool_denied_by_specs(tool, SUBAGENT_ALWAYS_DISALLOWED_TOOLS)
        ]
        gates.append({
            "gate_number": 3,
            "gate": "custom_allowed_tools_disallowed",
            "status": "failed" if denied_tools else "passed",
            "denied_tools": denied_tools,
            "message": (
                "Custom subagent allowed_tools includes tools that subagents may not receive: "
                + ", ".join(denied_tools)
                if denied_tools
                else "Custom subagent allowed_tools does not include subagent lifecycle or plan-control tools."
            ),
        })
        return gates

    def _uses_custom_tool_gates(self, canonical: str, definition: AgentDefinition) -> bool:
        return canonical == "custom" or definition.source != "built-in"

    def _tool_spec_resolves(self, spec: str) -> bool:
        name = _tool_spec_name(spec)
        if not name:
            return False
        if name.startswith("mcp__"):
            return True
        if self.registry is None:
            return True
        registered = {str(schema.get("name") or "") for schema in self.registry.all_schemas()}
        for candidate in self._resolve_tool_family(name):
            if candidate in registered:
                return True
            resolved = self._resolve_tool_name(candidate)
            if resolved in registered:
                return True
        return False

    def _tool_schemas_for_task(self, definition: AgentDefinition, task: SubAgentTask) -> list[dict[str, Any]]:
        if self.registry is None:
            return []
        allowed = self._effective_allowed_tools(definition, task)
        if not allowed:
            return []
        schemas = []
        for schema in self.registry.all_schemas():
            name = str(schema.get("name") or "")
            if self._tool_denied_by_specs(name, SUBAGENT_ALWAYS_DISALLOWED_TOOLS):
                continue
            if self._tool_denied_by_specs(name, definition.disallowed_tools):
                continue
            if self._tool_allowed(name, allowed):
                schemas.append(schema)
        return schemas

    def _tool_allowed(self, name: str, allowed: list[str]) -> bool:
        if "*" in allowed:
            return True
        resolved = self._resolve_tool_name(name) or name
        allowed_resolved = set()
        for item in allowed:
            allowed_resolved.update(self._resolve_tool_family(item))
        return resolved in allowed_resolved or name in allowed

    def _resolve_tool_name(self, name: str) -> str | None:
        if self.registry is None:
            return name
        try:
            return self.registry.resolve(name)
        except Exception:
            return name

    def _resolve_tool_family(self, name: str) -> set[str]:
        spec_name = _tool_spec_name(name)
        normalized = _normalize_tool_key(spec_name)
        candidates = TOOL_COMPATIBILITY_GROUPS.get(normalized, [spec_name])
        resolved: set[str] = set()
        for candidate in candidates:
            resolved.add(candidate)
            resolved_name = self._resolve_tool_name(candidate)
            if resolved_name:
                resolved.add(resolved_name)
        return resolved

    def _tool_denied_by_specs(self, name: str, specs: list[str]) -> bool:
        if not specs:
            return False
        resolved_name = self._resolve_tool_name(_tool_spec_name(name)) or _tool_spec_name(name)
        requested_family = self._resolve_tool_family(name)
        requested_family.add(resolved_name)
        for spec in specs:
            denied_family = self._resolve_tool_family(spec)
            denied_name = self._resolve_tool_name(_tool_spec_name(spec)) or _tool_spec_name(spec)
            denied_family.add(denied_name)
            if resolved_name in denied_family or requested_family.intersection(denied_family):
                return True
        return False

    def _prepare_worktree(self, task: SubAgentTask) -> bool:
        if task.worktree_isolation != "worktree":
            return True
        if self.ctx is None:
            return self._fail_worktree(task, "subagent runtime has no tool context")
        base_sandbox = getattr(self.ctx, "sandbox", None)
        if base_sandbox is None:
            return self._fail_worktree(task, "subagent runtime has no sandbox")
        if not shutil.which("git"):
            return self._fail_worktree(task, "git is not installed or not on PATH", status="unsupported")
        git_root = base_sandbox.run("git rev-parse --show-toplevel", 10)
        if not git_root.ok:
            return self._fail_worktree(task, "workspace is not a git repository", status="unsupported", output=git_root.output)
        support = base_sandbox.run("git worktree list --porcelain", 10)
        if not support.ok:
            return self._fail_worktree(task, "git worktree is unavailable or failed", status="unsupported", output=support.output)

        rel_path = f".lilbot/worktrees/{task.id}"
        branch = task.worktree_branch or f"lilbot/{task.id}"
        try:
            target = base_sandbox.resolve(rel_path)
        except Exception as exc:
            return self._fail_worktree(task, str(exc))
        target.parent.mkdir(parents=True, exist_ok=True)
        command = f"git worktree add -b {_quote_ps(branch)} {_quote_ps(str(target))} HEAD"
        result = base_sandbox.run(command, 120)
        if not result.ok:
            return self._fail_worktree(task, "git worktree add failed", output=result.output)
        with self._lock:
            task.worktree_path = str(target.resolve())
            task.worktree_branch = branch
            task.worktree_created = True
            task.worktree_status = "active"
            task.worktree_error = ""
            self._persist_tasks_locked()
        self._append_transcript(task, "worktree_active", {"path": task.worktree_path, "branch": branch, "created": True})
        return True

    def _fail_worktree(self, task: SubAgentTask, reason: str, *, status: str = "error", output: str = "") -> bool:
        with self._lock:
            task.status = "failed"
            task.error = f"Worktree isolation {status}: {reason}"
            task.worktree_status = status
            task.worktree_error = reason
            task.result = self._ensure_output_contract(task.error + (f"\n{output}" if output else ""))
            self._persist_tasks_locked()
        self._append_transcript(task, "worktree_failed", {"status": status, "reason": reason, "output": output})
        return False

    def _cleanup_worktree(self, task: SubAgentTask) -> None:
        if task.worktree_isolation != "worktree" or not task.cleanup_worktree:
            return
        if not task.worktree_created or not task.worktree_path or self.ctx is None:
            return
        path = Path(task.worktree_path)
        try:
            base_root = self.ctx.sandbox.root.resolve()
            resolved = path.resolve()
        except Exception:
            return
        expected_parent = base_root / ".lilbot" / "worktrees"
        if expected_parent not in resolved.parents:
            with self._lock:
                task.worktree_status = "cleanup_refused"
                task.worktree_error = "worktree cleanup path is outside .lilbot/worktrees"
                self._persist_tasks_locked()
            self._append_transcript(task, "worktree_cleanup_refused", {"path": str(resolved)})
            return
        result = self.ctx.sandbox.run(f"git worktree remove --force {_quote_ps(str(resolved))}", 120)
        with self._lock:
            task.worktree_status = "cleaned" if result.ok else "cleanup_error"
            task.worktree_error = "" if result.ok else result.output
            self._persist_tasks_locked()
        self._append_transcript(
            task,
            "worktree_cleanup",
            {"path": str(resolved), "ok": result.ok, "returncode": result.returncode, "output": result.output},
        )

    def _ctx_for_task(self, task: SubAgentTask) -> "ToolContext":
        if self.ctx is None:
            raise ValueError("Subagent tool context is not configured.")
        if task.worktree_status != "active" or not task.worktree_path:
            return self.ctx
        worktree_root = Path(task.worktree_path)
        config = getattr(self.ctx, "config", None)
        try:
            task_config = replace(config, workspace=worktree_root)
        except Exception:
            try:
                task_config = type(config)(**vars(config))
                task_config.workspace = worktree_root
            except Exception:
                task_config = config
        try:
            return replace(self.ctx, sandbox=Sandbox(worktree_root), config=task_config)
        except Exception:
            clone = type("SubAgentToolContext", (), {})()
            clone.__dict__.update(getattr(self.ctx, "__dict__", {}))
            clone.sandbox = Sandbox(worktree_root)
            clone.config = task_config
            return clone

    def _execute_tool_call(self, definition: AgentDefinition, task: SubAgentTask, name: str, arguments: dict[str, Any], run_ctx: Any = None) -> str:
        if self.registry is None or self.ctx is None:
            return f"Tool unavailable in this subagent runtime: {name}"
        allowed = self._effective_allowed_tools(definition, task)
        if not self._tool_allowed(name, allowed):
            return self._runtime_gate_message(
                task,
                4,
                "runtime_allowed_tools",
                name,
                f"Allowed tools: {', '.join(allowed) or '(none)'}",
            )
        if self._tool_denied_by_specs(name, SUBAGENT_ALWAYS_DISALLOWED_TOOLS):
            return self._runtime_gate_message(
                task,
                5,
                "runtime_role_or_policy",
                name,
                "Subagent lifecycle and plan-control tools are blocked inside subagents.",
            )
        if self._tool_denied_by_specs(name, definition.disallowed_tools):
            return self._runtime_gate_message(
                task,
                5,
                "runtime_role_or_policy",
                name,
                "The role preset disallows this tool.",
            )
        result, elapsed_ms = self.registry.execute(name, arguments or {}, run_ctx or self._ctx_for_task(task))
        if not result.ok and result.metadata.get("gate"):
            return self._runtime_gate_message(
                task,
                5,
                str(result.metadata.get("gate") or "runtime_role_or_policy"),
                name,
                result.output,
            )
        status = "ok" if result.ok else "error"
        return f"{status} {name} {elapsed_ms}ms\n{result.output}"

    def _runtime_gate_message(self, task: SubAgentTask, gate_number: int, gate: str, tool_name: str, reason: str) -> str:
        data = {
            "gate_number": gate_number,
            "gate": gate,
            "status": "failed",
            "agent_type": task.agent_type,
            "tool": tool_name,
            "message": reason,
        }
        self._append_transcript(task, "tool_denied", data)
        return (
            f"Tool denied for {task.agent_type}: {tool_name}. "
            f"Gate {gate_number} ({gate}) failed. {reason}"
        )

    def runtime_status(self) -> dict[str, Any]:
        with self._lock:
            tasks = [self.projection(task) for task in self.list_tasks()[:8]]
            return {
                "max_concurrent": self.max_concurrent,
                "running": self._count_status("running"),
                "queued": self._count_status("queued"),
                "total": len(self.tasks),
                "recent": tasks,
            }

    def _count_status(self, status: str) -> int:
        return sum(1 for task in self.tasks.values() if task.status == status)

    def _resume_recovered_tasks(self) -> None:
        to_resume: list[SubAgentTask] = []
        with self._lock:
            for task in self.tasks.values():
                if not task.recovered or task.terminal or task.status != "queued":
                    continue
                if task.id in self._resume_started:
                    continue
                self._resume_started.add(task.id)
                to_resume.append(task)
                self._append_transcript(
                    task,
                    "resume_scheduled",
                    {"reason": "recovered from persisted non-terminal subagent task"},
                )
        for task in to_resume:
            thread = threading.Thread(target=self._run, args=(task,), daemon=True)
            thread.start()

    def _load_persisted_tasks(self) -> None:
        if self.tasks_path is None or not self.tasks_path.exists():
            return
        try:
            raw = json.loads(self.tasks_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(raw, list):
            return
        changed = False
        loaded: dict[str, SubAgentTask] = {}
        for item in raw:
            if not isinstance(item, dict):
                continue
            task = _task_from_record(item)
            if task is None:
                continue
            if not task.terminal:
                task.status = "queued"
                task.error = task.error or "Recovered after restart; queued for restart resume."
                task.started_at = None
                task.finished_at = None
                task.recovered = True
                task.resume_count += 1
                changed = True
            loaded[task.id] = task
        with self._lock:
            self.tasks.update(loaded)
            if changed:
                self._persist_tasks_locked()
        for task in loaded.values():
            if task.recovered and task.status == "queued":
                self._append_transcript(task, "recovered_after_restart", {"resume_count": task.resume_count})

    def _persist_tasks_locked(self) -> None:
        if self.tasks_path is None:
            return
        records = [_task_to_record(task) for task in sorted(self.tasks.values(), key=lambda item: item.created_at)]
        self.tasks_path.parent.mkdir(parents=True, exist_ok=True)
        self.tasks_path.write_text(json.dumps(records, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    def _append_transcript(self, task: SubAgentTask, event: str, data: dict[str, Any]) -> None:
        # 【简历·5 执行观测｜落盘的 JSONL 执行轨迹】
        # 子代理生命周期里的每一步(queued/running/provider_turn/tool_started/
        # tool_finished/completed/failed/worktree_*…)都追加一行 JSON 到
        # subagent-transcripts/{id}.jsonl，带时间戳、内容、工具入参/输出、usage。
        # 这就是可回放、可分页拉取(transcript())、可做 bad case 复盘的执行日志——
        # 简历“记录 Plan/Tool Call/Observation/错误类型/工具耗时/Token 消耗”的
        # 持久化落点。progress_event_count/last_event 同时驱动实时看板。
        if self.transcripts_dir is None:
            return
        path = Path(task.transcript_path) if task.transcript_path else self.transcripts_dir / f"{task.id}.jsonl"
        if not task.transcript_path:
            task.transcript_path = str(path)
            task.transcript_handle = self._transcript_handle(path)
        record = {"ts": time.time(), "event": event, **data}
        with self._lock:
            task.progress_event_count += 1
            task.last_event = event
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
            self._persist_tasks_locked()

    def _transcript_handle(self, path: Path) -> str:
        if self.state_dir is not None:
            try:
                return f"{self.state_dir.name}/{path.relative_to(self.state_dir).as_posix()}"
            except ValueError:
                pass
        return str(path)

    def _step_limit_report(self, transcript: list[str]) -> str:
        evidence = "\n".join(f"- {item[:300]}" for item in transcript[-8:]) or "- no tool evidence collected"
        return "\n".join(
            [
                f"SUMMARY: Subagent reached the {SUBAGENT_MAX_TOOL_STEPS}-tool-step budget before a final answer.",
                "CHANGES: None.",
                "EVIDENCE:",
                evidence,
                "RISKS: Evidence may be incomplete because the subagent stopped at its tool budget.",
                "BLOCKERS: None.",
            ]
        )


def _assistant_tool_message(turn: ProviderTurn) -> dict[str, Any]:
    message = {
        "role": "assistant",
        "content": turn.content or "",
        "tool_calls": [
            {
                "id": call.call_id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(call.arguments, ensure_ascii=False),
                },
            }
            for call in turn.tool_calls
        ],
    }
    if turn.reasoning_content:
        message["reasoning_content"] = turn.reasoning_content
    return message


def _parse_agent_markdown(raw: str) -> tuple[dict[str, object], str]:
    if not raw.startswith("---"):
        return {}, raw
    parts = raw.split("---", 2)
    if len(parts) != 3:
        return {}, raw
    _, head, body = parts
    meta: dict[str, object] = {}
    current_key: str | None = None
    for raw_line in head.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if line[:1].isspace() and stripped.startswith("-") and current_key:
            existing = meta.setdefault(current_key, [])
            if isinstance(existing, list):
                existing.append(stripped[1:].strip().strip('"').strip("'"))
            continue
        if ":" not in line or line[:1].isspace():
            continue
        key, value = line.split(":", 1)
        current_key = key.strip().lower().replace("_", "-")
        value = value.strip()
        meta[current_key] = [] if not value else value.strip('"').strip("'")
    return meta, body


def _as_list(value: object) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [item.strip() for item in str(value).split(",") if item.strip()]


def _dedupe_tools(tools: list[str]) -> list[str]:
    seen = set()
    result = []
    for tool in tools:
        name = str(tool).strip()
        if not name or name in seen:
            continue
        seen.add(name)
        result.append(name)
    return result


def _normalize_tool_key(name: str) -> str:
    return str(name).strip().replace("_", "").replace("-", "").replace(" ", "").lower()


def _tool_spec_name(spec: str) -> str:
    value = str(spec or "").strip()
    if "(" in value:
        value = value.split("(", 1)[0].strip()
    return value


def _normalize_isolation(value: str | None) -> str | None:
    text = str(value or "").strip().lower().replace("_", "-")
    if not text or text in {"none", "false", "off"}:
        return None
    if text == "worktree":
        return "worktree"
    raise ValueError("isolation must be 'worktree' or empty")


def _task_to_record(task: SubAgentTask) -> dict[str, Any]:
    return {field.name: getattr(task, field.name) for field in fields(SubAgentTask)}


def _task_from_record(item: dict[str, Any]) -> SubAgentTask | None:
    names = {field.name for field in fields(SubAgentTask)}
    data = {key: value for key, value in item.items() if key in names}
    if not data.get("id") or not data.get("agent_type") or data.get("prompt") is None:
        return None
    try:
        return SubAgentTask(**data)
    except TypeError:
        return None


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _quote_ps(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _as_bool(value: object, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}
