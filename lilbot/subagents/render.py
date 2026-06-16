"""
Dynamic agent-type and active-task renderers for tool description parity.

CodeWhale-style: compact, capability-forward, single source of truth.
Called by ToolRegistry.schemas() at query time so the LLM always sees
the current set of agent types and running tasks.

The system prompt provides strategy (WHEN to use subagents); this module
renders WHAT is available — with concrete "use for" hints per type to
make the LLM's delegation decision immediate and actionable.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .manager import AgentDefinition, SubAgentTask


def _yn(value: bool) -> str:
    return "yes" if value else "no"


def _summarize_tools(agent: "AgentDefinition") -> str:
    """Compact tool summary: category counts, not raw tool names."""
    if not agent.allowed_tools and not agent.disallowed_tools:
        return "none"
    parts: list[str] = []
    if agent.allowed_tools:
        parts.append(f"{len(agent.allowed_tools)} tools")
    if agent.disallowed_tools:
        parts.append(f"{len(agent.disallowed_tools)} blocked")
    return ", ".join(parts)


def _terminal_status(status: str) -> bool:
    return status in {"completed", "done", "failed", "cancelled", "error"}


# ── When-to-use hints per agent type ──────────────────────────────────────
# These map agent type names to concrete triggers that help the LLM decide
# "should I delegate this?" instantly, without reasoning overhead.
_USE_HINTS: dict[str, str] = {
    "explore": "codebase mapping, multi-file reads, architecture exploration, grep tracing",
    "researcher": "web research, fact-finding, comparisons, recommendations, current events, travel planning",
    "plan": "decomposition, milestones, tradeoff analysis, roadmap",
    "writer": "essays, reports, drafts, rewrites, style adaptation, prose",
    "critic": "risk review, assumption checking, quality critique, gap analysis",
    "review": "code review, bug hunting, regression checks, security audit",
    "implementer": "code changes, bug fixes, feature implementation, refactoring",
    "verifier": "test running, validation, pass/fail reporting",
    "general": "multi-step tasks needing writes + reads + web + shell",
    "tool_agent": "fast simple operations: OCR, single fetch, command probe",
    "custom": "user-defined agent with explicit tool list",
}


def _use_hint(name: str, description: str) -> str:
    """Return a one-line 'use for' hint, falling back to description prefix."""
    return _USE_HINTS.get(name, description.split(".")[0].strip())


def render_agent_types(definitions: list["AgentDefinition"]) -> str:
    """Render all registered agent types into an actionable listing.

    Format (CodeWhale-style, with use-for triggers):
        - name: description
          Use for: concrete triggers.
          [writes=yes|no, shell=mode, N tools]
    """
    if not definitions:
        return "No agent types available."

    lines = ["Available agent types — use with agent_open in parallel:"]
    for agent in definitions:
        cap = f"writes={_yn(agent.writes)}, shell={agent.shell}"
        tools = _summarize_tools(agent)
        if tools != "none":
            cap += f", {tools}"
        hint = _use_hint(agent.name, agent.description)
        lines.append(f"- **{agent.name}**: {agent.description}")
        lines.append(f"  Use for: {hint}. [{cap}]")
    # Empty line before active agents section for readability
    lines.append("")
    return "\n".join(lines)


def render_active_agents(tasks: list["SubAgentTask"]) -> str:
    """Render currently active (non-terminal) subagent tasks.

    Format:
        Active subagents — collect results with agent_eval:
        - name [type] status: prompt-truncated-to-80-chars
        No active subagents.  (when empty)
    """
    active = [t for t in tasks if not _terminal_status(t.status)]
    if not active:
        return "No active subagents."

    lines = ["Active subagents — collect results with agent_eval:"]
    for task in active:
        prompt_preview = " ".join(str(task.prompt).split())[:80]
        name = task.name or task.id
        lines.append(
            f"- {name} [{task.agent_type}] {task.status}: {prompt_preview}"
        )
    return "\n".join(lines)
