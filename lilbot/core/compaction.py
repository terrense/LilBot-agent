"""Two-layer context compaction with recovery (ported from mewcode).

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
        return "\n".join(sections)


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


# --- summary prompt ---------------------------------------------------------

SUMMARY_SYSTEM = "You are a conversation-summarizer. Output plain text only. Do not call any tools."

SUMMARY_INSTRUCTION = """\
Summarize the conversation below into a structured handoff. Cover, in order:

1. Primary request and intent — what the user is ultimately trying to do
2. Key technical concepts discussed
3. Files and code — which files, and any critical snippets to preserve
4. Errors and fixes
5. Problem-solving approach
6. All user messages — preserve the user's own words, do not paraphrase
7. Pending tasks — what is not yet done
8. Current work — most recent activity, in detail
9. Next step — what to do next

Output the summary as plain text. Do not call tools."""


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


# A summarizer takes (system_prompt, prefix_text) and returns the summary text.
Summarizer = Callable[[str, str], str]


def auto_compact(
    messages: list[dict[str, Any]],
    summarizer: Summarizer,
    context_window: int,
    *,
    manual: bool = False,
    recovery: RecoveryState | None = None,
    tool_names: list[str] | None = None,
    breaker: CompactCircuitBreaker | None = None,
) -> CompactResult | None:
    """Compact messages in place-by-return. messages[0] (system) is preserved.

    Returns a CompactResult with the rebuilt message list, or None when no
    compaction was needed or possible.
    """
    if not messages:
        return None
    system = messages[0]
    body = messages[1:]
    before = estimate_tokens(messages)

    if not manual:
        if before < compute_threshold(context_window):
            return None
        if breaker is not None and breaker.is_open():
            return None

    keep_start = compute_keep_start(body)
    to_summarize = body[:keep_start]
    keep_tail = body[keep_start:]

    if keep_start <= 0 or estimate_tokens(to_summarize) < MIN_SUMMARIZE_PREFIX_TOKENS:
        return None

    prefix_text = render_prefix_for_summary(to_summarize)
    try:
        summary = summarizer(SUMMARY_SYSTEM, f"{SUMMARY_INSTRUCTION}\n\n--- conversation ---\n{prefix_text}")
    except Exception:
        if breaker is not None:
            breaker.record_failure()
        return None
    if not summary or not summary.strip():
        if breaker is not None:
            breaker.record_failure()
        return None

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
    )
