"""Render live subagent metadata into tool descriptions.

The design: the parent model sees the
available agent types, when to use them, and their tool limits directly in the
Agent tool description. The runtime still enforces the limits.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .manager import AgentDefinition, SubAgentTask


_WHEN_TO_USE: dict[str, str] = {
    "general": "Use for flexible multi-step tasks that may need reads, writes, web research, and validation.",
    "explore": "Use for codebase mapping, multi-file reads, architecture exploration, and grep/LSP tracing.",
    "researcher": "Use for web research, public facts, comparisons, recommendations, current facts, and travel planning.",
    "plan": "Use for decomposition, milestones, tradeoffs, roadmaps, and implementation strategy.",
    "writer": "Use for essays, reports, drafts, rewrites, style adaptation, prose, and copy.",
    "critic": "Use for assumption checks, risk review, quality critique, and gap analysis.",
    "review": "Use for code review, bug hunting, regression checks, and security-oriented audit.",
    "implementer": "Use for focused code changes, bug fixes, feature implementation, and refactoring.",
    "verifier": "Use for tests, diagnostics, validation, and pass/fail evidence without fixing failures.",
    "tool_agent": "Use for fast bounded tool probes such as a single fetch, search, OCR, or command check.",
    "custom": "Use for caller-defined roles with explicit allowed_tools.",
}


def _when_to_use(agent: "AgentDefinition") -> str:
    explicit = getattr(agent, "when_to_use", "") or ""
    return explicit.strip() or _WHEN_TO_USE.get(agent.name, agent.description.strip())


def _tool_name(tool_spec: str) -> str:
    text = str(tool_spec).strip()
    for marker in ("(", "["):
        if marker in text:
            return text.split(marker, 1)[0].strip()
    return text


def _tools_description(agent: "AgentDefinition") -> str:
    allowed = [_tool_name(tool) for tool in getattr(agent, "allowed_tools", []) if _tool_name(tool)]
    blocked = [_tool_name(tool) for tool in getattr(agent, "disallowed_tools", []) if _tool_name(tool)]
    if allowed:
        if blocked:
            blocked_set = set(blocked)
            effective = [tool for tool in allowed if tool not in blocked_set]
            if not effective:
                return "None"
            return f"{', '.join(effective)} ({len(effective)} tools, {len(blocked)} blocked)"
        return f"{', '.join(allowed)} ({len(allowed)} tools)"
    if blocked:
        return "None by default; runtime also blocks " + ", ".join(blocked)
    return "None by default"


def format_agent_line(agent: "AgentDefinition") -> str:
    writes = "yes" if getattr(agent, "writes", False) else "no"
    shell = getattr(agent, "shell", "minimal")
    return (
        f"- **{agent.name}**: {agent.description} "
        f"When to use: {_when_to_use(agent)} "
        f"(Tools: {_tools_description(agent)}) "
        f"[writes={writes}, shell={shell}]"
    )


def render_agent_types(definitions: list["AgentDefinition"]) -> str:
    if not definitions:
        return "No agent types available."
    lines = [
        "Available agent types and the tools they have access to:",
        *[format_agent_line(agent) for agent in definitions],
        "",
        "Usage notes:",
        "- Launch multiple agents in one turn when tasks are independent.",
        "- Before launching a duplicate agent, inspect active subagents in agent_eval and send a follow-up there if it already covers the work.",
        "- Give each agent a narrow prompt; the parent remains responsible for final synthesis.",
        "- Use agent_eval to collect results; do not guess results before an agent completes.",
    ]
    return "\n".join(lines)


def render_active_agents(tasks: list["SubAgentTask"]) -> str:
    active = [task for task in tasks if task.status not in {"completed", "done", "failed", "cancelled", "error"}]
    if not active:
        return "No active subagents."
    lines = ["Active subagents - collect results with agent_eval:"]
    for task in active:
        name = task.name or task.id
        prompt_preview = " ".join(str(task.prompt).split())[:100]
        lines.append(f"- {name} [{task.agent_type}] {task.status}: {prompt_preview}")
    return "\n".join(lines)
