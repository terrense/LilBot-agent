"""L0 — per-turn tool-result budget with a frozen/fresh/replaced state machine.

Faithful port of Claude Code's ``utils/toolResultStorage.ts::enforceToolResultBudget``,
adapted from Anthropic's "many tool_results per user message" shape to LilBot's
OpenAI-style transcript (one ``role=="tool"`` message per call).

The point is to shed oversized tool output *without ever rewriting content that
has already been sent to the model* — because that content sits in the
provider's cached prefix, and mutating it busts the cache. Every tool_use_id is
in one of three states:

  * ``fresh``    — first time we see it this session; eligible for replacement.
  * ``frozen``   — already seen and sent to the model at full size; NEVER touched
                   again (its bytes are in the server cache prefix).
  * ``replaced`` — already swapped for a preview; stays a preview (idempotent).

When a window's tool output exceeds the budget we only ever replace *fresh*
candidates (largest first), so the cached prefix stays byte-stable. Fresh
candidates we don't replace are marked seen → they become frozen next pass.

This mirrors CC's ordering discipline: unselected candidates are marked seen
immediately; selected ones are marked seen together with the replacement so no
observer sees ``id ∈ seen but id ∉ replacements`` (which would misclassify it as
frozen and send full content — a cache miss).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Aggregate budget (chars) for tool output in the scanned window. ~50k chars
# ≈ 12.5k tokens, matching CC's per-message MAX_TOOL_RESULTS_PER_MESSAGE_CHARS
# order of magnitude.
DEFAULT_TOOL_RESULT_BUDGET_CHARS = 50_000

# Always keep this many most-recent tool results at full size — the model is
# most likely still working with them.
KEEP_RECENT_TOOL_RESULTS = 4

TOOL_RESULT_PREVIEW_HEAD = 600
TOOL_RESULT_CLEARED_SUFFIX = "\n[... {n:,} chars offloaded to save context; re-run the tool if you need the full output ...]"


@dataclass
class ToolResultReplacement:
    tool_call_id: str
    preview: str
    original_size: int


@dataclass
class ToolBudgetState:
    """Session-scoped state that survives across turns.

    ``seen_ids`` are tool_call_ids sent to the model at least once (frozen unless
    also in ``replacements``); ``replacements`` maps a tool_call_id to the preview
    text that permanently stands in for its output.
    """

    seen_ids: set[str] = field(default_factory=set)
    replacements: dict[str, str] = field(default_factory=dict)


def _tool_call_id(msg: dict[str, Any]) -> str:
    return str(msg.get("tool_call_id") or msg.get("name") or "")


def _build_preview(content: str) -> str:
    head = content[:TOOL_RESULT_PREVIEW_HEAD]
    shed = len(content) - len(head)
    return head + TOOL_RESULT_CLEARED_SUFFIX.format(n=shed)


def enforce_tool_result_budget(
    messages: list[dict[str, Any]],
    state: ToolBudgetState,
    *,
    budget_chars: int = DEFAULT_TOOL_RESULT_BUDGET_CHARS,
    keep_recent: int = KEEP_RECENT_TOOL_RESULTS,
    skip_tools: set[str] | None = None,
) -> tuple[list[dict[str, Any]], list[ToolResultReplacement]]:
    """Shed oversized tool output, cache-safely.

    Returns ``(new_messages, newly_replaced)``. ``new_messages`` is a shallow copy
    with over-budget *fresh* tool results swapped for previews; previously
    replaced ids are re-applied idempotently. Frozen ids are never touched.
    """
    skip = skip_tools or set()

    # Locate tool-result messages, newest-first index bookkeeping so we can keep
    # the most recent `keep_recent` at full size.
    tool_positions = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    protected = set(tool_positions[-keep_recent:]) if keep_recent > 0 else set()

    # Re-apply existing replacements first (idempotent; survives resume) and
    # classify the rest.
    frozen_size = 0
    replaced_size = 0
    fresh: list[tuple[int, str, int]] = []  # (index, id, size)
    for i in tool_positions:
        if i in protected:
            continue
        msg = messages[i]
        tid = _tool_call_id(msg)
        if not tid or str(msg.get("name") or "") in skip:
            continue
        content = str(msg.get("content") or "")
        if tid in state.replacements:
            replaced_size += len(state.replacements[tid])
            continue
        if tid in state.seen_ids:
            frozen_size += len(content)  # untouchable, but counts against budget
            continue
        fresh.append((i, tid, len(content)))

    fresh_size = sum(size for _, _, size in fresh)
    total = frozen_size + replaced_size + fresh_size

    # Under budget: nothing to replace. Mark all fresh as seen so they freeze
    # next pass (their full content was — or is about to be — sent).
    if total <= budget_chars:
        for _, tid, _ in fresh:
            state.seen_ids.add(tid)
        return _apply(messages, state), []

    # Over budget: replace largest fresh candidates until under budget.
    fresh.sort(key=lambda t: t[2], reverse=True)
    running = total
    newly: list[ToolResultReplacement] = []
    for i, tid, size in fresh:
        if running <= budget_chars:
            state.seen_ids.add(tid)  # kept full -> frozen going forward
            continue
        content = str(messages[i].get("content") or "")
        preview = _build_preview(content)
        # Mark seen + replacement together (atomic under observation).
        state.replacements[tid] = preview
        state.seen_ids.add(tid)
        running -= (size - len(preview))
        newly.append(ToolResultReplacement(tid, preview, size))

    return _apply(messages, state), newly


def _apply(messages: list[dict[str, Any]], state: ToolBudgetState) -> list[dict[str, Any]]:
    """Return messages with every replaced tool_call_id swapped for its preview."""
    if not state.replacements:
        return messages
    out: list[dict[str, Any]] = []
    for m in messages:
        if m.get("role") == "tool":
            tid = _tool_call_id(m)
            preview = state.replacements.get(tid)
            if preview is not None and str(m.get("content") or "") != preview:
                m = {**m, "content": preview}
        out.append(m)
    return out
