"""Two-layer context compaction with recovery.

LilBot's original compaction joined truncated message strings into a flat
summary. This module replaces that with:

  * a token-budget trigger (compact as we approach the model's context window),
  * an LLM-generated structured summary of the older prefix,
  * a *kept* recent tail (original messages, selected by token budget and never
    splitting a tool_calls/tool pair),
  * a RecoveryState attachment that re-injects recently read files, skill SOPs,
    and the live tool list so the model does not "forget" working context, and
  * a circuit breaker so repeated summary failures fall back gracefully.

It operates on OpenAI-style message dicts (the format LilBot's Agent keeps).
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

# --- tuning -----------------------------------------------------------------

# Reserve room for the summary completion's own output.
SUMMARY_OUTPUT_RESERVE = 8_000
# Trigger compaction this far below the effective window.
AUTO_COMPACT_SAFETY_MARGIN = 6_000

# Recent-tail keep window: keep original messages until we have accumulated
# KEEP_RECENT_TOKENS or MIN_KEEP_MESSAGES (whichever first), stopping before a
# single huge message blows past KEEP_MAX_TOKENS.
KEEP_RECENT_TOKENS = 6_000
MIN_KEEP_MESSAGES = 4
KEEP_MAX_TOKENS = 20_000

# Summarising a tiny prefix costs more than it saves.
MIN_SUMMARIZE_PREFIX_TOKENS = 1_500

_CHARS_PER_TOKEN = 4.0

# Recovery attachment budgets.
RECOVERY_FILE_LIMIT = 5
RECOVERY_CHARS_PER_FILE = 4_000

# Local tool-result prune (microcompact). Clearing stale tool outputs is far
# cheaper than an LLM summary and keeps the message structure intact, so the
# provider's prefix cache survives better than a full summary rewrite. We try
# this first; only escalate to a summary when pruning alone isn't enough.
PRUNED_TOOL_RESULT_PLACEHOLDER = "[old tool result cleared to save context]"

# Summary-call resilience. A single transient provider hiccup should not burn a
# compaction opportunity: retry with exponential backoff, and only trip the
# circuit breaker once every attempt has failed.
MAX_SUMMARY_RETRIES = 3
RETRY_BASE_DELAY_S = 0.35

# Don't pay for an LLM summary (which rewrites the whole prefix and breaks the
# provider's cache) unless the reclaimable prefix is genuinely large. Scales
# with the model's window; a manual /compact bypasses this scaled floor.
SUMMARIZE_FLOOR_FRACTION = 0.02

# --- CC-parity additions ----------------------------------------------------

# Server prompt caches (Anthropic/DeepSeek/OpenAI) expire after ~5 minutes idle.
# Past this gap the prefix is cold: clearing tool bodies is "free" because the
# whole prefix will be re-tokenized regardless. Mirrors CC's time-based
# microcompact (gapThresholdMinutes) — CC uses 60min tied to the 1h server TTL;
# we use the conservative 5min floor common to OpenAI-compatible caches.
CACHE_TTL_SECONDS = 300.0

# Total recovery-attachment budget (tokens). CC caps post-compact file restore
# at ~50k tokens / 5 files; we bound the whole attachment so the summary message
# can't itself blow the window.
RECOVERY_TOTAL_TOKENS = 12_000

# Marker inserted where truncate_head_for_retry drops the oldest rounds.
PTL_RETRY_MARKER = "[earlier conversation truncated so the summary request fits]"


def estimate_tokens_text(text: str) -> int:
    return int(len(text) / _CHARS_PER_TOKEN) if text else 0


def message_tokens(msg: dict[str, Any]) -> int:
    total = estimate_tokens_text(str(msg.get("content") or ""))
    total += estimate_tokens_text(str(msg.get("reasoning_content") or ""))
    for call in msg.get("tool_calls") or []:
        fn = call.get("function") or {}
        total += estimate_tokens_text(str(fn.get("name") or ""))
        total += estimate_tokens_text(str(fn.get("arguments") or ""))
    return total + 4  # per-message overhead


def estimate_tokens(messages: list[dict[str, Any]]) -> int:
    return sum(message_tokens(m) for m in messages)


def compute_threshold(context_window: int) -> int:
    return context_window - SUMMARY_OUTPUT_RESERVE - AUTO_COMPACT_SAFETY_MARGIN


# --- recovery state ---------------------------------------------------------

@dataclass
class _FileRead:
    path: str
    content: str
    ts: float


class RecoveryState:
    """Per-agent snapshot that survives a compaction.

    Records the bytes that read_file returned and skill bodies that were loaded,
    so they can be re-attached to the summary message after the working
    transcript is collapsed.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._files: dict[str, _FileRead] = {}
        self._skills: dict[str, str] = {}

    def record_file_read(self, path: str, content: str) -> None:
        if not path or not content:
            return
        with self._lock:
            self._files[path] = _FileRead(path=path, content=content, ts=time.time())

    def record_skill(self, name: str, body: str) -> None:
        if not name or not body:
            return
        with self._lock:
            self._skills[name] = body

    def _recent_files(self, limit: int) -> list[_FileRead]:
        with self._lock:
            files = sorted(self._files.values(), key=lambda f: f.ts, reverse=True)
        return files[:limit] if limit > 0 else files

    def build_attachment(self, tool_names: list[str] | None) -> str:
        sections: list[str] = []

        files = self._recent_files(RECOVERY_FILE_LIMIT)
        if files:
            buf = ["## Recently read files\n",
                   "Snapshots of what read_file last returned. Re-read for current bytes.\n"]
            for rec in files:
                content = rec.content
                if len(content) > RECOVERY_CHARS_PER_FILE:
                    content = content[:RECOVERY_CHARS_PER_FILE] + "\n… (truncated)"
                buf.append(f"### {rec.path}\n```\n{content}\n```\n")
            sections.append("".join(buf))

        with self._lock:
            skills = dict(self._skills)
        if skills:
            buf = ["## Active skills\n",
                   "These skills were loaded earlier; their guidance still applies.\n"]
            for name, body in skills.items():
                snippet = body if len(body) <= RECOVERY_CHARS_PER_FILE else body[:RECOVERY_CHARS_PER_FILE] + "\n… (truncated)"
                buf.append(f"### {name}\n{snippet}\n")
            sections.append("".join(buf))

        if tool_names:
            sections.append("## Available tools\n" + ", ".join(tool_names) + "\n")

        if not sections:
            return ""
        sections.append(
            "## Note\nThe context above was reconstructed. For exact code, errors, or the "
            "user's original wording, re-read the source rather than guessing from the summary.\n"
        )
        attachment = "\n".join(sections)
        # Total-token budget (CC caps post-compact restore at ~50k tokens): never
        # let the recovery attachment itself grow the post-compact window without
        # bound — trim from the end (tool list / note first).
        if estimate_tokens_text(attachment) > RECOVERY_TOTAL_TOKENS:
            budget_chars = int(RECOVERY_TOTAL_TOKENS * _CHARS_PER_TOKEN)
            attachment = attachment[:budget_chars] + "\n… (recovery attachment truncated to fit budget)\n"
        return attachment


# --- tail selection ---------------------------------------------------------

def compute_keep_start(messages: list[dict[str, Any]]) -> int:
    """Index of the first message to keep verbatim (token-budget tail).

    Walks from the end accumulating tokens; keeps messages until a floor
    (KEEP_RECENT_TOKENS or MIN_KEEP_MESSAGES) is met, never letting a single
    oversized message push the kept total past KEEP_MAX_TOKENS. The result is
    then nudged earlier so a kept ``tool`` message is never split from the
    assistant ``tool_calls`` message it answers.
    """
    n = len(messages)
    if n == 0:
        return 0
    kept_tokens = 0
    kept_count = 0
    keep_start = n
    for i in range(n - 1, -1, -1):
        tok = message_tokens(messages[i])
        if kept_count > 0 and kept_tokens + tok > KEEP_MAX_TOKENS:
            break
        kept_tokens += tok
        kept_count += 1
        keep_start = i
        if kept_tokens >= KEEP_RECENT_TOKENS or kept_count >= MIN_KEEP_MESSAGES:
            break
    return _align_keep_start(messages, keep_start)


def _align_keep_start(messages: list[dict[str, Any]], keep_start: int) -> int:
    # Never start the kept tail on an orphan tool result: walk back onto the
    # assistant message that issued the tool_calls.
    while 0 < keep_start < len(messages) and messages[keep_start].get("role") == "tool":
        keep_start -= 1
    return keep_start


# --- local prune (microcompact) ---------------------------------------------

def prune_tool_results(body: list[dict[str, Any]], keep_start: int) -> tuple[list[dict[str, Any]], int]:
    """Clear stale ``tool`` result bodies that sit before the kept tail.

    Returns ``(new_body, chars_saved)``. Only messages with ``role == "tool"``
    at an index below ``keep_start`` are touched; the recent tail keeps its tool
    output verbatim. Already-cleared placeholders are left alone so re-running
    prune is idempotent. Other fields (``tool_call_id``, ``name``) are preserved
    so the assistant/tool pairing stays valid.
    """
    saved = 0
    out: list[dict[str, Any]] = []
    for i, msg in enumerate(body):
        if i < keep_start and msg.get("role") == "tool":
            content = str(msg.get("content") or "")
            if content and content != PRUNED_TOOL_RESULT_PLACEHOLDER:
                saved += len(content)
                msg = {**msg, "content": PRUNED_TOOL_RESULT_PLACEHOLDER}
        out.append(msg)
    return out, saved


# --- reactive overflow detection --------------------------------------------

_OVERFLOW_MARKERS = (
    "context_length_exceeded",
    "context length",
    "prompt is too long",
    "prompt_too_long",
    "too many tokens",
    "maximum context length",
    "reduce the length",
    "string too long",
)


def is_context_overflow_error(message: str) -> bool:
    """Best-effort check for a provider "prompt too long" / 413-style error.

    Lets the agent react to a live overflow by compacting and retrying instead
    of failing the turn — the reactive counterpart to the proactive token-budget
    trigger.
    """
    if not message:
        return False
    text = message.lower()
    return any(marker in text for marker in _OVERFLOW_MARKERS)


# --- summary prompt ---------------------------------------------------------

SUMMARY_SYSTEM = "You are a conversation-summarizer. Output plain text only. Do not call any tools."

# NO_TOOLS preamble (ported from CC prompt.ts): the cache-sharing summary fork
# inherits the full tool schema set, and some models attempt a tool call despite
# a weak trailer. A rejected tool call wastes the single summary turn, so we
# forbid tools up front AND in the trailer.
_NO_TOOLS_PREAMBLE = (
    "CRITICAL: Respond with TEXT ONLY. Do NOT call any tools — any tool call is "
    "rejected and wastes this turn. You already have everything you need above.\n\n"
)
_NO_TOOLS_TRAILER = (
    "\n\nREMINDER: plain text only — an <analysis> block followed by a <summary> "
    "block. Do not call any tools."
)

SUMMARY_INSTRUCTION = _NO_TOOLS_PREAMBLE + """\
Before the final summary, wrap your thinking in <analysis>...</analysis> to make
sure you cover every point (this scratchpad is stripped before the summary is
stored). Then wrap the summary itself in <summary>...</summary>.

The <summary> must cover, in order:

1. Primary request and intent — what the user is ultimately trying to do
2. Key technical concepts discussed
3. Files and code — which files, and any critical snippets to preserve
4. Errors and fixes — and any user corrections, in the user's own words
5. Problem-solving approach
6. All user messages — preserve the user's own words, do not paraphrase
7. Pending tasks — what is not yet done
8. Current work — most recent activity, in detail
9. Next step — the next action, with a verbatim quote of where you left off""" + _NO_TOOLS_TRAILER


def format_compact_summary(summary: str) -> str:
    """Strip the ``<analysis>`` scratchpad and unwrap ``<summary>`` tags.

    Faithful to CC's formatCompactSummary: the analysis block improves summary
    quality but has no informational value once written, so it never enters
    context. Tolerant of models that ignore the tags entirely (returns the text
    unchanged), so the offline provider and simple models still work.
    """
    import re

    text = re.sub(r"<analysis>[\s\S]*?</analysis>", "", summary or "").strip()
    m = re.search(r"<summary>([\s\S]*?)</summary>", text)
    if m:
        text = m.group(1).strip()
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _render_message_for_summary(msg: dict[str, Any]) -> str:
    role = msg.get("role", "?")
    content = str(msg.get("content") or "").strip()
    calls = msg.get("tool_calls") or []
    if calls:
        names = ", ".join((c.get("function") or {}).get("name", "?") for c in calls)
        tail = f" [called tools: {names}]"
    else:
        tail = ""
    if role == "tool":
        role = "tool_result"
    return f"{role}: {content}{tail}".strip()


def render_prefix_for_summary(prefix: list[dict[str, Any]]) -> str:
    return "\n".join(_render_message_for_summary(m) for m in prefix if _render_message_for_summary(m))


# --- API-round grouping + head truncation (summary self-overflow) -----------

def group_messages_by_round(messages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Group messages into API rounds: one group per assistant response.

    Port of CC's groupMessagesByApiRound. A boundary fires when a new assistant
    message begins and the current group is non-empty; ``tool`` results attach to
    the assistant round that produced them (they follow it). The leading system /
    user preamble forms group 0. Keeping tool_calls together with their tool
    results means a whole round can be dropped without orphaning a tool result.
    """
    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for msg in messages:
        if msg.get("role") == "assistant" and current:
            groups.append(current)
            current = [msg]
        else:
            current.append(msg)
    if current:
        groups.append(current)
    return groups


def truncate_head_for_retry(
    messages: list[dict[str, Any]],
    *,
    keep_system: bool = True,
    drop_fraction: float = 0.2,
) -> list[dict[str, Any]] | None:
    """Drop the oldest API rounds so an over-long summary request fits.

    Port of CC's truncateHeadForPTLRetry (the "dumb but safe" fallback): drop
    ~``drop_fraction`` of the oldest rounds, always keep the system message and at
    least one round, and re-insert a marker so a follow-up retry is idempotent
    (the marker is stripped before regrouping). Returns None when there is
    nothing safe to drop.
    """
    if not messages:
        return None
    system: list[dict[str, Any]] = []
    body = messages
    if keep_system and messages[0].get("role") == "system":
        system = [messages[0]]
        body = messages[1:]

    # Strip our own marker from a prior retry so it doesn't become its own round
    # and stall the 20% fallback.
    if body and body[0].get("role") == "user" and body[0].get("content") == PTL_RETRY_MARKER:
        body = body[1:]

    groups = group_messages_by_round(body)
    if len(groups) < 2:
        return None
    drop = max(1, int(len(groups) * drop_fraction))
    drop = min(drop, len(groups) - 1)  # keep at least one round
    kept = [m for g in groups[drop:] for m in g]
    marker = {"role": "user", "content": PTL_RETRY_MARKER}
    return [*system, marker, *kept]


# --- circuit breaker --------------------------------------------------------

@dataclass
class CompactCircuitBreaker:
    max_failures: int = 3
    consecutive_failures: int = field(default=0, init=False)

    def record_failure(self) -> None:
        self.consecutive_failures += 1

    def record_success(self) -> None:
        self.consecutive_failures = 0

    def is_open(self) -> bool:
        return self.consecutive_failures >= self.max_failures


# --- orchestrator -----------------------------------------------------------

@dataclass
class CompactResult:
    messages: list[dict[str, Any]]
    before_tokens: int
    after_tokens: int
    summarized: int
    kept: int
    # Characters removed by the local tool-result prune (0 when none).
    pruned: int = 0
    # How the budget was reclaimed: "prune" (no LLM call) or "summarize".
    method: str = "summarize"


# A text summarizer takes (system_prompt, prefix_text) and returns summary text.
Summarizer = Callable[[str, str], str]

# A message summarizer takes (system_msg, to_summarize_msgs) and returns summary
# text. It exists so the Agent can send the SAME message objects the main loop
# used (system + prefix) plus a trailing instruction, reusing the provider's
# prompt-cache prefix instead of paying a 100% cache-miss on a fresh 2-message
# conversation. Mirrors CC's fork-based prompt-cache-sharing summary.
MessageSummarizer = Callable[[dict[str, Any], list[dict[str, Any]]], str]


def _summarize_with_retry(
    summarizer: Summarizer,
    prefix_text: str,
    max_retries: int,
    base_delay: float,
) -> str:
    """Call the summarizer with exponential backoff.

    Returns the first non-empty summary, or "" when every attempt failed. Never
    raises — the caller treats "" as a failure and trips the circuit breaker.
    """
    prompt = f"{SUMMARY_INSTRUCTION}\n\n--- conversation ---\n{prefix_text}"
    for attempt in range(max(1, max_retries)):
        if attempt > 0 and base_delay > 0:
            time.sleep(base_delay * (2 ** (attempt - 1)))
        try:
            out = summarizer(SUMMARY_SYSTEM, prompt)
        except Exception:
            continue
        if out and out.strip():
            return out
    return ""


def _summarize_messages_with_retry(
    summarizer: MessageSummarizer,
    system: dict[str, Any],
    to_summarize: list[dict[str, Any]],
    max_retries: int,
    base_delay: float,
) -> str:
    """Prompt-cache-sharing summary with backoff + head-truncation on overflow.

    On a context-overflow error from the summary call itself (the request can be
    almost the whole window), drop the oldest rounds (truncate_head_for_retry)
    and retry — CC's CC-1180 fix. Returns "" when every attempt fails.
    """
    msgs = to_summarize
    for attempt in range(max(1, max_retries)):
        if attempt > 0 and base_delay > 0:
            time.sleep(base_delay * (2 ** (attempt - 1)))
        try:
            out = summarizer(system, msgs)
        except Exception as exc:
            if is_context_overflow_error(str(exc)):
                truncated = truncate_head_for_retry([system, *msgs])
                if truncated is not None:
                    msgs = truncated[1:]  # drop the system we prepended
                    continue
            continue
        if out and out.strip():
            return out
    return ""


def auto_compact(
    messages: list[dict[str, Any]],
    summarizer: Summarizer,
    context_window: int,
    *,
    manual: bool = False,
    recovery: RecoveryState | None = None,
    tool_names: list[str] | None = None,
    breaker: CompactCircuitBreaker | None = None,
    actual_tokens: int | None = None,
    cache_cold: bool = False,
    message_summarizer: MessageSummarizer | None = None,
) -> CompactResult | None:
    """Compact messages in place-by-return. messages[0] (system) is preserved.

    Returns a CompactResult with the rebuilt message list, or None when no
    compaction was needed or possible.

    【简历·4 上下文管理｜两层压缩，是“Token 消耗降低 50%+”的主引擎】
    触发方式有两种（见 agent.py）：接近上下文窗口时“主动压缩”，以及真的超长
    报错时“被动压缩后重试”。压缩分两层，先便宜后昂贵：
      · Layer 1 本地裁剪(microcompact)：把“保留尾部”之前的旧工具结果正文清空
        成占位符(prune_tool_results)。不调用 LLM、保持消息结构不变，因此服务端
        prefix 缓存能存活；若裁剪后就已回到预算内，直接返回，method="prune"。
      · Layer 2 LLM 摘要：裁剪还不够时，才让模型把更早的前缀总结成一段结构化
        handoff(SUMMARY_INSTRUCTION 的 9 段)，只保留最近若干条原文尾巴
        (compute_keep_start，且绝不拆散 tool_calls/tool 配对)。
    另有三重工程护栏：摘要失败按指数退避重试(_summarize_with_retry)、连续失败
    则熔断降级(CompactCircuitBreaker)、并把最近读过的文件/技能/工具清单作为
    RecoveryState 附在摘要后，避免模型“压缩后失忆”。
    """
    if not messages:
        return None
    system = messages[0]
    body = messages[1:]
    before = estimate_tokens(messages)
    # Prefer the provider's real reported prompt-token count for the trigger
    # decision; fall back to the char/4 estimate. CC's lesson: trust API usage,
    # estimate only as a backstop.
    trigger_tokens = actual_tokens if (actual_tokens and actual_tokens > 0) else before
    threshold = compute_threshold(context_window)
    over_threshold = trigger_tokens >= threshold

    if not manual:
        # Below threshold we normally do nothing — unless the cache is cold, in
        # which case a prune is "free" (the prefix is re-tokenized anyway) and
        # worth doing proactively. Cold-cache passes may ONLY prune, never pay
        # for an LLM summary below threshold.
        if not over_threshold and not cache_cold:
            return None
        if breaker is not None and breaker.is_open():
            return None
    prune_only = (not manual) and (not over_threshold) and cache_cold

    keep_start = compute_keep_start(body)

    # --- Layer 1: local tool-result prune (microcompact) --------------------
    # Clear stale tool outputs in the to-summarize prefix first. If that alone
    # brings us back under budget, skip the LLM summary entirely: it's cheaper
    # and it preserves message identity, so the prefix cache survives.
    pruned_chars = 0
    pruned_body, saved = prune_tool_results(body, keep_start)
    if saved > 0:
        pruned_chars = saved
        candidate = [system, *pruned_body]
        after_prune = estimate_tokens(candidate)
        # In auto mode, stop here when pruning cleared the pressure (or when the
        # cache is cold and we're only allowed to prune). Manual /compact always
        # proceeds to a summary so the user gets a real handoff.
        if not manual and (prune_only or after_prune < threshold):
            if breaker is not None:
                breaker.record_success()
            return CompactResult(
                messages=candidate,
                before_tokens=before,
                after_tokens=after_prune,
                summarized=0,
                kept=len(pruned_body),
                pruned=pruned_chars,
                method="prune",
            )
        # Not enough on its own — summarize the (now smaller) pruned prefix.
        body = pruned_body
    elif prune_only:
        # Cold cache but nothing to prune, and we may not summarize below
        # threshold. Nothing to do.
        return None

    # --- Layer 2: LLM summary of the older prefix ---------------------------
    to_summarize = body[:keep_start]
    keep_tail = body[keep_start:]

    # Window-aware summary floor: rewriting the prefix (and breaking the cache)
    # is only worth it for a sizable reclaim. Manual /compact keeps the small
    # fixed floor so an explicit request still works on short conversations.
    summary_floor = (
        MIN_SUMMARIZE_PREFIX_TOKENS
        if manual
        else max(MIN_SUMMARIZE_PREFIX_TOKENS, int(context_window * SUMMARIZE_FLOOR_FRACTION))
    )
    if keep_start <= 0 or estimate_tokens(to_summarize) < summary_floor:
        return None

    # Prompt-cache-sharing path (preferred): hand the real prefix objects to the
    # message summarizer so the provider's cached prefix is reused. Fall back to
    # the text summarizer (offline provider / simple callers).
    if message_summarizer is not None:
        raw_summary = _summarize_messages_with_retry(
            message_summarizer, system, to_summarize, MAX_SUMMARY_RETRIES, RETRY_BASE_DELAY_S
        )
    else:
        prefix_text = render_prefix_for_summary(to_summarize)
        raw_summary = _summarize_with_retry(summarizer, prefix_text, MAX_SUMMARY_RETRIES, RETRY_BASE_DELAY_S)
    if not raw_summary or not raw_summary.strip():
        if breaker is not None:
            breaker.record_failure()
        return None
    # Strip the <analysis> scratchpad and unwrap <summary> (no-op if absent).
    summary = format_compact_summary(raw_summary)

    content = (
        "This session continues from an earlier conversation that was compacted to save context. "
        "Summary of the earlier conversation:\n\n" + summary.strip()
    )
    if keep_tail:
        content += "\n\nThe most recent messages are preserved verbatim below."
    if recovery is not None:
        attachment = recovery.build_attachment(tool_names)
        if attachment:
            content += "\n\n---\n\n" + attachment

    summary_msg = {"role": "system", "content": content}
    new_messages = [system, summary_msg, *keep_tail]

    if breaker is not None:
        breaker.record_success()

    return CompactResult(
        messages=new_messages,
        before_tokens=before,
        after_tokens=estimate_tokens(new_messages),
        summarized=len(to_summarize),
        kept=len(keep_tail),
        pruned=pruned_chars,
        method="summarize",
    )


def partial_compact(
    messages: list[dict[str, Any]],
    summarizer: Summarizer,
    pivot: int,
    *,
    direction: str = "up_to",
    recovery: RecoveryState | None = None,
    tool_names: list[str] | None = None,
    message_summarizer: MessageSummarizer | None = None,
) -> CompactResult | None:
    """User-selected partial compaction (CC's partialCompactConversation).

    ``pivot`` is an index into ``messages`` (system at 0 is always preserved).
      * ``up_to``: summarize ``messages[1:pivot]``, keep ``messages[pivot:]`` —
        the summary sits at the head (a normal compact with an explicit split).
      * ``from``: keep ``messages[1:pivot]`` intact, summarize ``messages[pivot:]``
        — the summary sits at the tail (compress recent, keep the old context).

    Returns None when there is nothing on the chosen side to summarize.
    """
    if not messages or pivot <= 0 or pivot >= len(messages):
        return None
    system = messages[0]
    before = estimate_tokens(messages)

    if direction == "from":
        keep = messages[1:pivot]
        to_summarize = messages[pivot:]
    else:  # "up_to"
        to_summarize = messages[1:pivot]
        keep = messages[pivot:]
    if not to_summarize:
        return None

    if message_summarizer is not None:
        raw = _summarize_messages_with_retry(
            message_summarizer, system, to_summarize, MAX_SUMMARY_RETRIES, RETRY_BASE_DELAY_S
        )
    else:
        raw = _summarize_with_retry(
            summarizer, render_prefix_for_summary(to_summarize), MAX_SUMMARY_RETRIES, RETRY_BASE_DELAY_S
        )
    if not raw or not raw.strip():
        return None
    summary = format_compact_summary(raw)

    content = (
        "Part of this conversation was compacted to save context. "
        "Summary of the compacted portion:\n\n" + summary.strip()
    )
    if recovery is not None:
        attachment = recovery.build_attachment(tool_names)
        if attachment:
            content += "\n\n---\n\n" + attachment
    summary_msg = {"role": "system", "content": content}

    if direction == "from":
        # Keep old context, replace recent tail with its summary.
        new_messages = [system, *keep, summary_msg]
    else:
        new_messages = [system, summary_msg, *keep]

    return CompactResult(
        messages=new_messages,
        before_tokens=before,
        after_tokens=estimate_tokens(new_messages),
        summarized=len(to_summarize),
        kept=len(keep),
        pruned=0,
        method="summarize",
    )
