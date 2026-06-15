from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from ..core.events import ProviderTurn


ProviderCallable = Callable[[list[dict], list[dict]], ProviderTurn]
SUBAGENT_MAX_TOOL_STEPS = 6

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
]
DIAGNOSTIC_TOOLS = ["diagnostics", "run_tests", "task_gate_run"]
WRITE_TOOLS = ["write_file", "edit_file", "apply_patch"]
WEB_TOOLS = ["web_search", "fetch_url", "web_fetch", "web_run"]
AGENT_TOOLS = ["agent_open", "agent_eval", "agent_close", "agent_spawn", "agent_status", "tool_agent", "Agent", "Task"]
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
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    messages: list[str] = field(default_factory=list)

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
    def __init__(self, provider: ProviderCallable, agents_dir: Path | None = None):
        self.provider = provider
        self.agents_dir = agents_dir
        self.registry: Any | None = None
        self.ctx: Any | None = None
        self.definitions = {d.name: d for d in DEFAULT_AGENT_TYPES}
        self.tasks: dict[str, SubAgentTask] = {}
        self._lock = threading.RLock()
        self.reload_custom_agents()

    def configure_tools(self, registry: Any, ctx: Any) -> None:
        self.registry = registry
        self.ctx = ctx

    def list_types(self) -> list[AgentDefinition]:
        return sorted(self.definitions.values(), key=lambda item: item.name)

    def list_tasks(self) -> list[SubAgentTask]:
        with self._lock:
            return sorted(self.tasks.values(), key=lambda item: item.created_at, reverse=True)

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
    ) -> SubAgentTask:
        canonical = self.resolve_type(agent_type)
        definition = self.definitions[canonical]
        task_id = f"sub_{uuid4().hex[:10]}"
        task = SubAgentTask(
            id=task_id,
            name=name or task_id,
            agent_type=canonical,
            prompt=prompt,
            allowed_tools=_dedupe_tools(allowed_tools or definition.allowed_tools),
            model=model,
            fork_context=fork_context,
        )
        with self._lock:
            self.tasks[task.id] = task
        if background:
            thread = threading.Thread(target=self._run, args=(task,), daemon=True)
            thread.start()
        else:
            self._run(task)
        return task

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
        with self._lock:
            if not task.terminal:
                task.status = "cancelled"
                task.error = "Cancelled by parent."
                task.finished_at = time.time()
        return task

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
            "duration_ms": duration_ms,
            "result": task.result if task.terminal else None,
            "error": task.error or None,
        }

    def _run(self, task: SubAgentTask) -> None:
        definition = self.definitions[task.agent_type]
        with self._lock:
            if task.status == "cancelled":
                return
            task.status = "running"
        messages = [
            {"role": "system", "content": f"{definition.system_hint}\n\n{self._tool_policy_hint(definition, task)}\n\n{OUTPUT_CONTRACT}"},
            {"role": "user", "content": task.prompt},
        ]
        transcript: list[str] = []
        try:
            content = ""
            tool_schemas = self._tool_schemas_for_task(definition, task)
            steps = 0
            while steps <= SUBAGENT_MAX_TOOL_STEPS:
                turn = self.provider(messages, tool_schemas)
                if turn.content.strip():
                    content = turn.content.strip()
                    transcript.append(content)
                if not turn.tool_calls:
                    break
                if steps >= SUBAGENT_MAX_TOOL_STEPS:
                    content = self._step_limit_report(transcript)
                    break
                messages.append(_assistant_tool_message(turn))
                for call in turn.tool_calls[: max(0, SUBAGENT_MAX_TOOL_STEPS - steps)]:
                    tool_output = self._execute_tool_call(definition, task, call.name, call.arguments)
                    steps += 1
                    transcript.append(f"{call.name}: {tool_output[:1000]}")
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call.call_id,
                        "name": call.name,
                        "content": tool_output,
                    })
                    if steps >= SUBAGENT_MAX_TOOL_STEPS:
                        break
            content = content or "(subagent returned no text)"
            task.result = self._ensure_output_contract(content)
            with self._lock:
                if task.status != "cancelled":
                    task.status = "completed"
        except Exception as exc:  # pragma: no cover - defensive boundary
            with self._lock:
                if task.status != "cancelled":
                    task.error = str(exc)
                    task.status = "failed"
        finally:
            with self._lock:
                task.finished_at = task.finished_at or time.time()

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
        return _dedupe_tools(task.allowed_tools or definition.allowed_tools)

    def _tool_schemas_for_task(self, definition: AgentDefinition, task: SubAgentTask) -> list[dict[str, Any]]:
        if self.registry is None:
            return []
        allowed = self._effective_allowed_tools(definition, task)
        if not allowed:
            return []
        disallowed = {self._resolve_tool_name(name) or name for name in definition.disallowed_tools}
        schemas = []
        for schema in self.registry.schemas():
            name = str(schema.get("name") or "")
            if name in disallowed:
                continue
            if self._tool_allowed(name, allowed):
                schemas.append(schema)
        return schemas

    def _tool_allowed(self, name: str, allowed: list[str]) -> bool:
        if "*" in allowed:
            return True
        resolved = self._resolve_tool_name(name) or name
        allowed_resolved = {self._resolve_tool_name(item) or item for item in allowed}
        return resolved in allowed_resolved or name in allowed

    def _resolve_tool_name(self, name: str) -> str | None:
        if self.registry is None:
            return name
        try:
            return self.registry.resolve(name)
        except Exception:
            return name

    def _execute_tool_call(self, definition: AgentDefinition, task: SubAgentTask, name: str, arguments: dict[str, Any]) -> str:
        if self.registry is None or self.ctx is None:
            return f"Tool unavailable in this subagent runtime: {name}"
        allowed = self._effective_allowed_tools(definition, task)
        if not self._tool_allowed(name, allowed):
            return f"Tool denied for {task.agent_type}: {name}. Allowed tools: {', '.join(allowed) or '(none)'}"
        resolved = self._resolve_tool_name(name) or name
        disallowed = {self._resolve_tool_name(item) or item for item in definition.disallowed_tools}
        if resolved in disallowed:
            return f"Tool denied for {task.agent_type}: {name}. It is disallowed by the role preset."
        result, elapsed_ms = self.registry.execute(name, arguments or {}, self.ctx)
        status = "ok" if result.ok else "error"
        return f"{status} {name} {elapsed_ms}ms\n{result.output}"

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


def _as_bool(value: object, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}
