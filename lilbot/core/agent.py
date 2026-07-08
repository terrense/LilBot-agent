from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from ..config import LilBotConfig
from .compaction import (
    CACHE_TTL_SECONDS,
    SUMMARY_INSTRUCTION,
    CompactCircuitBreaker,
    RecoveryState,
    auto_compact,
    is_context_overflow_error,
    partial_compact,
)
from .tool_budget import ToolBudgetState, enforce_tool_result_budget
from .delegation import (
    parse_semantic_delegation_plan,
    semantic_delegation_messages,
    should_consult_semantic_delegation,
)
from ..hooks import HookContext, HookEngine, load_hooks
from ..memory import extract_memories, recall
from .cycles import CycleArchive
from .history import FileHistory
from .session import SessionStore

# Tools that mutate a file at a `path` arg — snapshot before they run so /rewind
# can undo them.
MUTATING_PATH_TOOLS = {"write_file", "edit_file", "fim_edit"}

# File extensions worth running diagnostics on after an edit (M2). Others are skipped to avoid noise/latency.
DIAGNOSABLE_EXTS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java",
    ".c", ".cc", ".cpp", ".h", ".hpp", ".vue", ".rb", ".php",
}
MAX_DIAGNOSED_FILES = 5

MEMORY_EXTRACTION_INTERVAL = 3

# Max times a stop hook may force the turn to continue before we let it end
# anyway — CC's death-spiral guard against a hook that always blocks.
MAX_STOP_CONTINUATIONS = 3

# Bounded wait (seconds) for the recall prefetch before the FIRST provider call
# of a turn. A fast side-query still lands in the very first completion; a slow
# one is skipped there and picked up by a later iteration's non-blocking poll.
# (Claude Code's memory prefetch never blocks at all; the bounded first wait is
# a deliberate trade-off to keep recall coverage for single-shot answers.)
RECALL_FIRST_WAIT_S = 1.0


class SideQueryPrefetch:
    """One-shot background side-query on a daemon thread.

    Mirrors Claude Code's startRelevantMemoryPrefetch pattern: fire the side
    LLM query at turn start so it runs concurrently with the main provider
    call, then let the consume point poll for the result without blocking.
    The job must not raise (failures resolve to None) and must not touch
    agent state that the main thread mutates — thread-shared bits (usage)
    are guarded by locks in the Agent.
    """

    def __init__(self, job: Callable[[], Any]) -> None:
        self.result: Any = None
        self._done = threading.Event()
        threading.Thread(target=self._run, args=(job,), daemon=True).start()

    def _run(self, job: Callable[[], Any]) -> None:
        try:
            self.result = job()
        except Exception:
            self.result = None
        finally:
            self._done.set()

    def wait(self, timeout: float = 0.0) -> bool:
        """True when the job finished within ``timeout`` seconds (0 = poll)."""
        return self._done.wait(timeout)


from .events import ProviderTurn, TextDelta, ToolCall, ToolFinished, ToolStarted, TurnFinished
from .prompts import build_system_prompt
from ..llm.providers import BaseProvider
from ..tools import ToolContext, ToolRegistry, ToolResult


class Agent:
    def __init__(
        self,
        config: LilBotConfig,
        provider: BaseProvider,
        registry: ToolRegistry,
        ctx: ToolContext,
    ):
        self.config = config
        self.provider = provider
        self.registry = registry
        self.ctx = ctx
        self.agent_id = "lead"
        self.messages: list[dict[str, Any]] = [
            {"role": "system", "content": build_system_prompt(ctx.memory, ctx.skills)}
        ]
        self.usage: dict[str, int] = {}
        # Context compaction state.
        self.recovery = RecoveryState()
        self.compact_breaker = CompactCircuitBreaker()
        # CC-parity: real prompt-token count from the last provider response
        # (trigger decisions trust this over the char/4 estimate), and the
        # timestamp of the last model call (for cache-cold detection).
        self._last_input_tokens = 0
        self._last_activity_ts = 0.0
        # L0 tool-result budget: frozen/fresh/replaced state that survives turns.
        self._tool_budget = ToolBudgetState()
        # Lifecycle hooks. Loaded from .lilbot/hooks.json.
        state_dir = getattr(config, "state_dir", None)
        workspace = getattr(config, "workspace", None)
        self.hooks = HookEngine(load_hooks(state_dir), cwd=workspace)
        # Hot-reload support: re-read hooks.json when it changes, so edits take
        # effect on the next turn without restarting the session.
        self._hooks_path = (Path(state_dir) / "hooks.json") if state_dir else None
        self._hooks_mtime = self._current_hooks_mtime()
        # Memory recall / extraction state.
        self._turn_count = 0
        self._recent_tools: list[str] = []
        self._surfaced_memory_ids: set[str] = set()
        self._pending_recall = ""
        # Recall prefetch (M9): the selector side-query runs on a daemon thread
        # concurrently with the main provider call instead of blocking the turn.
        self._recall_prefetch: SideQueryPrefetch | None = None
        self._recall_waited = False
        # usage is now written from side-query threads too — guard the
        # read-modify-write in _add_usage.
        self._usage_lock = threading.Lock()
        # Serializes background extraction runs (and their read-modify-write
        # against the memory store).
        self._extract_lock = threading.Lock()
        self._extract_thread: threading.Thread | None = None
        # Session persistence. One file per session.
        self.sessions = SessionStore(state_dir) if state_dir else None
        self.session_id = time.strftime("%Y%m%d-%H%M%S")
        # File history / rewind. Snapshots before edits.
        self.file_history = (
            FileHistory(state_dir, workspace) if state_dir and workspace else None
        )
        # Auto diagnostics injection (M2).
        self._edited_this_turn: list[str] = []
        self._pending_diagnostics = ""
        # Cycle memory archive (M4). Each compaction
        # archives a briefing recoverable via the recall_archive tool.
        self.cycles = CycleArchive(state_dir) if state_dir else None
        # Stop-hook continuations this turn (CC's death-spiral guard).
        self._stop_continuations = 0

    def run_turn(self, user_text: str) -> Iterator[object]:
        # ============================================================
        # 【简历·1 Agent 执行框架｜ReAct 主循环】
        # 这是整个 Runtime 的心脏：一次用户输入 -> 一轮或多轮
        # “思考(LLM) -> 行动(工具) -> 观察(工具结果)”的 ReAct 循环。
        #   · 入口先做三件“回合级”准备：压缩上下文(_maybe_compact)、
        #     召回相关记忆(_maybe_recall)、触发生命周期钩子(turn_start)。
        #   · 下面的 while 循环就是 ReAct 的 Reason–Act–Observe：
        #       1) _stream_turn  -> 让模型思考并流式产出文本/工具调用(Reason)
        #       2) 若无 tool_calls -> 收尾返回最终答案(纯对话，单 Agent 直答)
        #       3) 若有 tool_calls -> 执行工具(Act) 并把结果回灌进 messages(Observe)
        #   · max_steps 是“工具步数预算”，是复杂任务不失控的护栏；超预算后
        #     由 _synthesize_after_step_limit 用已有证据强制收敛出答案。
        # 简单任务：模型第一轮就不产工具调用 -> 单 Agent 直接响应。
        # 复杂任务：模型多轮调用工具/子代理，逐步执行并校验中间结果。
        # ============================================================
        self._reload_hooks_if_changed()
        self.messages.append({"role": "user", "content": user_text})
        self._turn_count += 1
        self._edited_this_turn = []
        self._stop_continuations = 0
        if self.hooks.has_hooks():
            self.hooks.run("user_prompt_submit", HookContext(event="user_prompt_submit", message=user_text))
        self._maybe_compact()      # 回合开始先按 token 预算压缩历史（见 compaction.py）
        # 记忆召回改为“并行预取”（M9，对标 CC 的 startRelevantMemoryPrefetch）：
        # 这里只是发射后台侧查询，不再阻塞；结果由 _provider_messages 里的
        # _consume_recall_prefetch 轮询消费（首次调用给一个有界等待）。
        self._start_recall_prefetch(user_text)
        if self.hooks.has_hooks():
            self.hooks.run("turn_start", HookContext(event="turn_start", message=user_text))
        steps = self._auto_delegate(user_text)
        while steps < self.config.max_steps:
            self._drain_team_notifications()
            render_ctx = self.ctx.subagents.get_render_context() if getattr(self.ctx, "subagents", None) else None
            turn, streamed = yield from self._stream_turn(render_ctx)
            self._add_usage(turn)
            # When the text already streamed live, don't re-emit it as a block.
            if turn.content and not streamed:
                yield TextDelta(turn.content, interim=bool(turn.tool_calls))
            if not turn.tool_calls:
                # Record the model's final message first, then consult stop hooks:
                # one may force the model to keep working (CC's handleStopHooks),
                # bounded so a misbehaving hook can't spin forever.
                self.messages.append(self._assistant_content_message(turn.content, turn.reasoning_content))
                cont = self._run_stop_hook()
                if cont is not None:
                    self.messages.append({
                        "role": "user",
                        "content": (
                            "A stop hook requires more work before ending. "
                            "Do not ask the user; continue directly.\n" + cont
                        ),
                    })
                    continue
                self._fire_turn_end()
                self._maybe_extract()
                self._persist_session()
                yield TurnFinished(steps, dict(self.usage))
                return

            # --- ReAct 的 Act + Observe 阶段 ---
            # 把本轮工具调用切成若干 batch：连续的“只读”工具会并到同一批里
            # 并行执行（_run_calls_parallel），写文件/执行代码/需审批的工具则单独
            # 成批串行，保证副作用可控。这一步既是“调用工具”，也是把工具结果
            # 以 role=tool 消息写回 messages，构成下一轮模型能“观察”到的证据。
            remaining_steps = self.config.max_steps - steps
            calls_to_run = turn.tool_calls[:remaining_steps]
            self.messages.append(self._assistant_tool_message(
                ProviderTurn(turn.content, calls_to_run, turn.usage, turn.reasoning_content)
            ))
            for batch in self._partition_calls(calls_to_run):
                # Announce the whole batch, then execute (in parallel when the
                # batch is >1 read-only tool), then report results in order.
                for call in batch:
                    self._recent_tools.append(call.name)
                    yield ToolStarted(call.name, call.arguments)
                if len(batch) > 1:
                    results = self._run_calls_parallel(batch)
                else:
                    results = [self._run_one_call(batch[0])]
                for call, (result, elapsed_ms) in zip(batch, results):
                    steps += 1
                    # 【简历·5 执行观测】ToolFinished 携带 ok/耗时(elapsed_ms)/metadata，
                    # 上层 TUI 与会话持久化据此记录 Tool Call、Observation、工具耗时。
                    yield ToolFinished(call.name, result.ok, result.output, elapsed_ms, result.metadata)
                    # Observe：把工具输出作为 role=tool 消息回灌，成为下一轮模型的证据。
                    self.messages.append({
                        "role": "tool",
                        "tool_call_id": call.call_id,
                        "name": call.name,
                        "content": result.output,
                    })
            # After this turn's tool batch, diagnose freshly edited files so the
            # next LLM call sees type/syntax errors and can self-correct.
            self._run_post_edit_diagnostics()

        self._fire_turn_end()
        self._maybe_extract()
        final = self._synthesize_after_step_limit()
        self.messages.append(self._assistant_content_message(final.content, final.reasoning_content))
        self._persist_session()
        yield TextDelta(final.content)
        yield TurnFinished(steps, dict(self.usage))

    def _provider_messages(self) -> list[dict[str, Any]]:
        """Build the message list for one provider call.

        Appends transient system messages (NOT persisted into history): the
        deferred-tool reminder plus any pending hook prompt/notification output.
        Keeping these at the tail preserves the stable prefix that drives
        server-side prompt caching.
        """
        # Adopt the recall prefetch result (if ready) before building extras,
        # so the reminder rides along on this provider call.
        self._consume_recall_prefetch()
        extras: list[dict[str, Any]] = []

        deferred = self.registry.deferred_tool_names()
        if deferred:
            extras.append({
                "role": "system",
                "content": (
                    "Some tools are available but their schemas are not loaded, to keep context small. "
                    "To use one, first call ToolSearch with query \"select:<name>[,<name>...]\" (exact names) "
                    "or keywords to load the schema, then call the tool on the next step.\n"
                    "Deferred tools: " + ", ".join(deferred)
                ),
            })

        if self._pending_recall:
            extras.append({"role": "system", "content": self._pending_recall})

        if self._pending_diagnostics:
            extras.append({"role": "system", "content": self._pending_diagnostics})
            self._pending_diagnostics = ""  # one-shot: show on the next call only

        for msg in self.hooks.drain_prompt_messages():
            extras.append({"role": "system", "content": f"Hook guidance: {msg}"})
        notes = [n for n in self.hooks.drain_notifications() if n.output]
        for note in notes:
            status = "ok" if note.success else "failed"
            extras.append({
                "role": "system",
                "content": f"Hook [{note.hook_id}] {note.event} ({status}): {note.output}",
            })

        if not extras:
            return self.messages
        return [*self.messages, *extras]

    def _pre_tool_hook(self, call: ToolCall) -> str | None:
        """Run pre-tool hooks; may rewrite call.arguments in place. Returns a
        block reason, or None to allow."""
        if not self.hooks.has_hooks():
            return None
        ctx = HookContext(
            event="pre_tool_use",
            tool_name=call.name,
            tool_args=dict(call.arguments),
            file_path=str(call.arguments.get("path") or call.arguments.get("file_path") or ""),
        )
        outcome = self.hooks.run_pre_tool(ctx)
        # Structured protocol: a hook may rewrite the tool's arguments before it
        # runs (CC's updatedInput). Apply in place so the executed call and its
        # snapshot/diagnostics all see the rewritten input.
        if outcome.updated_input is not None and isinstance(outcome.updated_input, dict):
            call.arguments = outcome.updated_input
        return outcome.block

    def _post_tool_hook(self, call: ToolCall, result: Any) -> None:
        if not self.hooks.has_hooks():
            return
        ctx = HookContext(
            event="post_tool_use",
            tool_name=call.name,
            tool_args=dict(call.arguments),
            file_path=str(call.arguments.get("path") or call.arguments.get("file_path") or ""),
            message=getattr(result, "output", ""),
        )
        self.hooks.run("post_tool_use", ctx)

    def _run_stop_hook(self) -> str | None:
        """Stop hook (CC parity): a hook may force the turn to continue.

        Returns a continuation instruction, or None to let the turn end. Capped
        by MAX_STOP_CONTINUATIONS so a hook that always blocks can't loop forever
        (CC's death-spiral guard — the same reason API-error paths skip stop
        hooks entirely).
        """
        if not self.hooks.has_hooks():
            return None
        if self._stop_continuations >= MAX_STOP_CONTINUATIONS:
            return None
        cont = self.hooks.run_stop(HookContext(event="stop"))
        if cont:
            self._stop_continuations += 1
            return cont
        return None

    def _fire_turn_end(self) -> None:
        if self.hooks.has_hooks():
            self.hooks.run("turn_end", HookContext(event="turn_end"))

    def _current_hooks_mtime(self) -> float:
        try:
            return self._hooks_path.stat().st_mtime if self._hooks_path else 0.0
        except OSError:
            return 0.0

    def _reload_hooks_if_changed(self) -> None:
        """Reload hooks.json if it appeared or changed since last turn.

        Removes the "must restart to pick up hooks" friction: editing
        .lilbot/hooks.json takes effect on the next turn.
        """
        if self._hooks_path is None:
            return
        mtime = self._current_hooks_mtime()
        if mtime != self._hooks_mtime:
            self._hooks_mtime = mtime
            self.hooks = HookEngine(
                load_hooks(self._hooks_path.parent),
                cwd=getattr(self.config, "workspace", None),
            )

    # -- Session persistence / resume ----------------

    def _persist_session(self) -> None:
        if self.sessions is None:
            return
        self.sessions.save(
            self.session_id,
            self.messages,
            self.usage,
            meta={"turns": self._turn_count, "surfaced_memory_ids": sorted(self._surfaced_memory_ids)},
        )

    def resume(self, session_id: str | None = None) -> str:
        """Load a saved session into this agent. None => most recent."""
        if self.sessions is None:
            return "Session persistence is unavailable (no state dir)."
        sid = session_id or self.sessions.latest_id()
        if not sid:
            return "No saved session to resume."
        data = self.sessions.load(sid)
        if not data:
            return f"Session '{sid}' not found."
        messages = data.get("messages") or []
        if not messages:
            return f"Session '{sid}' is empty."
        self.messages = messages
        self.usage = dict(data.get("usage") or {})
        self.session_id = sid
        meta = data.get("meta") or {}
        self._turn_count = int(meta.get("turns") or 0)
        self._surfaced_memory_ids = set(meta.get("surfaced_memory_ids") or [])
        return f"Resumed session '{sid}' ({len(messages)} messages)."

    # -- Memory recall / extraction ------------------

    def _provider_is_capable(self) -> bool:
        """The offline rule provider returns canned text; skip LLM meta-queries."""
        return type(self.provider).__name__ != "RuleBasedProvider"

    def _meta_query(self, system_prompt: str, user_message: str) -> str:
        turn = self.provider.complete(
            [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_message}],
            [],
        )
        self._add_usage(turn)
        return turn.content

    def _memory_store(self):
        store = getattr(self.ctx, "memory", None)
        if store is not None and hasattr(store, "list") and hasattr(store, "add"):
            return store
        return None

    def _start_recall_prefetch(self, query: str) -> None:
        """Launch memory recall as a background side-query (M9 prefetch).

        Previously ``_maybe_recall`` blocked the turn on a full selector LLM
        round-trip before the first provider call. Now the side-query runs on
        a daemon thread while the turn proceeds; ``_consume_recall_prefetch``
        picks the result up before each provider call. Inputs (entries, recent
        tools, surfaced ids) are snapshotted here on the main thread so the
        job never reads mutable agent state.
        """
        self._pending_recall = ""
        self._recall_prefetch = None
        self._recall_waited = False
        if not self._provider_is_capable():
            return
        store = self._memory_store()
        if store is None:
            return
        try:
            entries = store.list()
        except Exception:
            return
        if not entries:
            return
        recent = list(self._recent_tools[-8:])
        surfaced = set(self._surfaced_memory_ids)

        def job() -> tuple[str, list[str]]:
            # _meta_query is thread-safe: provider.complete builds a fresh HTTP
            # client per call and _add_usage is guarded by _usage_lock.
            return recall(query, entries, recent, surfaced, self._meta_query)

        self._recall_prefetch = SideQueryPrefetch(job)

    def _consume_recall_prefetch(self) -> None:
        """Adopt the prefetched recall result if it is ready.

        First consume of the turn waits up to RECALL_FIRST_WAIT_S so a fast
        selector still lands in the very first completion; later consumes are
        pure polls (mirrors Claude Code's "consume point polls settledAt,
        never blocks"). An unconsumed prefetch is simply dropped at turn end —
        surfaced ids stay unmarked, so those memories remain eligible next turn.
        """
        prefetch = self._recall_prefetch
        if prefetch is None:
            return
        timeout = 0.0
        if not self._recall_waited:
            self._recall_waited = True
            timeout = RECALL_FIRST_WAIT_S
        if not prefetch.wait(timeout):
            return
        self._recall_prefetch = None
        result = prefetch.result
        if not result:
            return
        reminder, ids = result
        if reminder:
            self._pending_recall = reminder
            self._surfaced_memory_ids.update(ids)

    def _maybe_extract(self) -> None:
        """Fire-and-forget memory extraction on a daemon thread (M9).

        The extraction result is never needed within the current turn, so
        there is no reason to block turn completion on an LLM side-query.
        ``_extract_lock`` serializes runs so two extractions can't interleave
        their read-modify-write against the store; the conversation snapshot
        is taken here on the main thread.
        """
        if not self._provider_is_capable():
            return
        if self._turn_count % MEMORY_EXTRACTION_INTERVAL != 0:
            return
        store = self._memory_store()
        if store is None:
            return
        text = self._recent_conversation_text()
        if not text.strip():
            return

        def job() -> None:
            with self._extract_lock:
                try:
                    index = "\n".join(f"- {e.name}" for e in store.list())
                    extract_memories(text, index, self._meta_query, store)
                except Exception:
                    pass

        self._extract_thread = threading.Thread(target=job, daemon=True)
        self._extract_thread.start()

    def _recent_conversation_text(self, max_messages: int = 20, max_chars: int = 8000) -> str:
        lines: list[str] = []
        for msg in self.messages[-max_messages:]:
            role = msg.get("role")
            if role not in {"user", "assistant"}:
                continue
            content = str(msg.get("content") or "").strip()
            if content:
                lines.append(f"{role}: {content}")
        return "\n".join(lines)[-max_chars:]

    def _drain_team_notifications(self) -> None:
        """Inject teammate messages / idle reports addressed to the lead.

        Called at the top of every agent-loop iteration so the lead learns of
        teammate progress mid-turn without blocking or polling. Mirrors 's
        drain_lead_mailbox -> system-reminder injection.
        """
        teams = getattr(self.ctx, "teams", None)
        if teams is None:
            return
        try:
            notes = teams.drain_lead_mailbox()
        except Exception:
            return
        for note in notes:
            self.messages.append({
                "role": "user",
                "content": (
                    "Internal LilBot team notification (coordination signal, not a new user "
                    "request). Use it to decide next steps; reply to teammates with send_message.\n"
                    + note
                ),
            })

    def drain_team_notifications(self) -> None:
        """Public hook for the UI to pull teammate updates between turns."""
        self._drain_team_notifications()

    def _auto_delegate(self, user_text: str) -> int:
        """No-op: Dynamic Agent Tool Prompt Parity replaces keyword-based auto-delegation.

        The LLM now reads live agent type descriptions in the agent_open tool schema
        and autonomously decides when to launch parallel subagents. The runtime
        (SubAgentManager gates) continues to enforce security.
        """
        return 0

    def _semantic_delegation_plan(
        self,
        user_text: str,
        max_agents: int,
        max_question_agents: int,
    ):
        if not should_consult_semantic_delegation(user_text):
            return None
        try:
            turn = self.provider.complete(
                semantic_delegation_messages(user_text, max_agents, max_question_agents),
                [],
            )
            self._add_usage(turn)
        except Exception:
            return None
        return parse_semantic_delegation_plan(turn.content, max_agents, max_question_agents)

    def _run_scheduled_tool(self, name: str, arguments: dict[str, Any]) -> Iterator[object]:
        call = ToolCall(name, arguments)
        yield ToolStarted(call.name, call.arguments)
        result, elapsed_ms = self.registry.execute(call.name, call.arguments, self.ctx)
        yield ToolFinished(call.name, result.ok, result.output, elapsed_ms, result.metadata)
        if name == "agent_eval":
            self.messages.append(self._internal_observation_message(name, arguments, result.output))
        return result

    def _is_concurrency_safe(self, call: ToolCall) -> bool:
        # Per-input concurrency (CC parity): a tool like bash is safe to fan out
        # only for a read-only command (`ls`) and not a mutating one (`rm`), so
        # ask the ToolDef with the actual arguments.
        tool = self.registry.get(call.name)
        return tool is not None and tool.is_concurrency_safe(call.arguments)

    def _partition_calls(self, calls: list[ToolCall]) -> list[list[ToolCall]]:
        """Group a run of consecutive read-only calls so they can run in parallel.

        Order is preserved: a concurrency-safe call extends the current safe
        batch; anything else starts its own singleton batch.

        【简历·1 并行执行】这是“Executor 按状态逐步执行”的性能优化点：
        只读工具(read_file/grep/git_* 等)之间没有副作用，可安全并行，
        因此把相邻只读调用合批交给线程池(_run_calls_parallel)一起跑；
        一旦遇到写文件/执行代码/需审批的工具，就单独成批、保持串行顺序，
        避免副作用交叉。是否“只读安全”由 ToolDef.concurrency_safe 判定。
        """
        batches: list[list[ToolCall]] = []
        for call in calls:
            safe = self._is_concurrency_safe(call)
            if safe and batches and len(batches[-1]) >= 1 and self._is_concurrency_safe(batches[-1][0]):
                batches[-1].append(call)
            else:
                batches.append([call])
        return batches

    def _run_one_call(self, call: ToolCall) -> tuple[ToolResult, int]:
        block = self._pre_tool_hook(call)
        if block is not None:
            return ToolResult(False, f"Blocked by hook: {block}"), 0
        self._snapshot_before_edit(call)
        result, elapsed_ms = self.registry.execute(call.name, call.arguments, self.ctx)
        self._record_for_recovery(call, result)
        if call.name in MUTATING_PATH_TOOLS and getattr(result, "ok", False):
            path = str(call.arguments.get("path") or call.arguments.get("file_path") or "")
            if path:
                self._edited_this_turn.append(path)
        self._post_tool_hook(call, result)
        return result, elapsed_ms

    def _run_calls_parallel(self, batch: list[ToolCall]) -> list[tuple[ToolResult, int]]:
        max_workers = min(len(batch), max(1, getattr(self.config, "subagent_max_concurrent", 8)))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            return list(pool.map(self._run_one_call, batch))

    def _run_post_edit_diagnostics(self) -> None:
        """Diagnose files edited this turn; stash errors for the next LLM call.

        LSP-injection loop: after an edit, run the
        diagnostics tool (LSP where available, Python-syntax fallback) and feed
        any problems back so the model can self-correct on the next step.
        """
        if not getattr(self.config, "auto_diagnostics", True) or not self._edited_this_turn:
            return
        # Unique, diagnosable, capped.
        seen: list[str] = []
        for p in self._edited_this_turn:
            ext = ("." + p.rsplit(".", 1)[-1].lower()) if "." in p else ""
            if ext in DIAGNOSABLE_EXTS and p not in seen:
                seen.append(p)
        self._edited_this_turn = []
        if not seen:
            return

        lines: list[str] = []
        for path in seen[:MAX_DIAGNOSED_FILES]:
            try:
                result, _ = self.registry.execute("lsp_diagnostics", {"path": path}, self.ctx)
            except Exception:
                continue
            meta = getattr(result, "metadata", {}) or {}
            diags = meta.get("diagnostics") or []
            problems = [d for d in diags if str(d.get("severity")) in ("error", "warning")]
            if not problems:
                continue
            lines.append(f"{path}:")
            for d in problems[:10]:
                lines.append(
                    f"  L{d.get('line', '?')} [{d.get('severity')}] "
                    f"{d.get('message', '')} ({d.get('source', '')})"
                )
        if lines:
            self._pending_diagnostics = (
                "Diagnostics for files you just edited (fix these before continuing; "
                "if a warning is intentional, say so):\n" + "\n".join(lines)
            )

    def _snapshot_before_edit(self, call: ToolCall) -> None:
        if self.file_history is None or call.name not in MUTATING_PATH_TOOLS:
            return
        path = str(call.arguments.get("path") or call.arguments.get("file_path") or "")
        if path:
            self.file_history.record(path, call.name, self._turn_count)

    def _record_for_recovery(self, call: ToolCall, result: Any) -> None:
        """Snapshot read_file bytes and loaded skill bodies for post-compaction recovery."""
        if not getattr(result, "ok", False):
            return
        name = call.name.lower()
        if name in {"read_file", "read"}:
            path = str(call.arguments.get("path") or call.arguments.get("file_path") or "")
            if path:
                self.recovery.record_file_read(path, result.output)
        elif name in {"skill", "skill_run", "load_skill"}:
            skill = str(call.arguments.get("skill") or call.arguments.get("name") or "skill")
            self.recovery.record_skill(skill, result.output)

    def _summarize(self, system_prompt: str, prefix_text: str) -> str:
        turn = self.provider.complete(
            [{"role": "system", "content": system_prompt}, {"role": "user", "content": prefix_text}],
            [],
        )
        self._add_usage(turn)
        return turn.content

    def _message_summarizer(self, system: dict[str, Any], to_summarize: list[dict[str, Any]]) -> str:
        """Prompt-cache-sharing summary (CC's fork-based cache reuse, adapted).

        Send the SAME message objects the main loop used (system + prefix) plus a
        trailing summary instruction, so the provider's cached prefix is reused
        instead of paying a full cache-miss on a fresh 2-message conversation.
        Tools are passed empty so the model cannot call any (the instruction also
        forbids it). Only used when the provider streams (a real API); the offline
        rule provider keeps the text summarizer.
        """
        messages = [system, *to_summarize, {"role": "user", "content": SUMMARY_INSTRUCTION}]
        turn = self.provider.complete(messages, [])
        self._add_usage(turn)
        return turn.content

    def _tool_names_for_recovery(self) -> list[str]:
        try:
            return [str(s.get("name")) for s in self.registry.schemas() if s.get("name")]
        except Exception:
            return []

    def _cache_is_cold(self) -> bool:
        """True when the server prompt cache has likely expired since last call.

        Past the cache TTL the whole prefix is re-tokenized anyway, so a proactive
        tool-result prune is "free". Mirrors CC's time-based microcompact trigger.
        """
        if self._last_activity_ts <= 0.0:
            return False
        return (time.time() - self._last_activity_ts) > CACHE_TTL_SECONDS

    def _shared_summary_available(self) -> bool:
        # Prompt-cache sharing only helps against a real caching provider; the
        # offline rule provider returns canned text, so keep the text path there.
        return self._provider_is_capable()

    def compact(self, manual: bool = True) -> str:
        result = auto_compact(
            self.messages,
            self._summarize,
            self.config.context_window,
            manual=manual,
            recovery=self.recovery,
            tool_names=self._tool_names_for_recovery(),
            breaker=self.compact_breaker,
            actual_tokens=self._last_input_tokens,
            cache_cold=self._cache_is_cold(),
            message_summarizer=self._message_summarizer if self._shared_summary_available() else None,
        )
        if result is None:
            return "Nothing to compact yet." if manual else ""
        # Archive the summarized prefix as a cycle before replacing history, so
        # the knowledge is recoverable later via recall_archive. Only a
        # summary carries a briefing at messages[1]; a prune-only pass has no
        # summary to archive.
        if result.method == "summarize" and self.cycles is not None and len(result.messages) > 1:
            briefing = str(result.messages[1].get("content") or "")
            self.cycles.archive(briefing, result.summarized, result.before_tokens)
        self.messages = result.messages
        # A summary rewrites the prefix — the real prompt-token count is now
        # stale (it reflected the pre-compact window). Clear it so the next
        # trigger falls back to the estimate until a fresh response arrives.
        self._post_compact_cleanup(result.method)
        if result.method == "prune":
            return (
                f"Pruned context: ~{result.before_tokens:,} -> ~{result.after_tokens:,} est. tokens "
                f"(cleared {result.pruned:,} chars of old tool output, no summary needed)."
            )
        return (
            f"Compacted context: ~{result.before_tokens:,} -> ~{result.after_tokens:,} est. tokens "
            f"(summarized {result.summarized} msgs, kept {result.kept})."
        )

    def partial_compact(self, pivot: int, direction: str = "up_to") -> str:
        """User-selected partial compaction around a pivot message (CC's L4c).

        ``up_to`` summarizes everything before the pivot (keep recent); ``from``
        keeps the old context and compresses everything from the pivot onward.
        """
        result = partial_compact(
            self.messages,
            self._summarize,
            pivot,
            direction=direction,
            recovery=self.recovery,
            tool_names=self._tool_names_for_recovery(),
            message_summarizer=self._message_summarizer if self._shared_summary_available() else None,
        )
        if result is None:
            return "Nothing to compact on that side of the pivot."
        if self.cycles is not None and direction == "up_to" and len(result.messages) > 1:
            briefing = str(result.messages[1].get("content") or "")
            self.cycles.archive(briefing, result.summarized, result.before_tokens)
        self.messages = result.messages
        self._post_compact_cleanup("summarize")
        return (
            f"Partial compact ({direction}): ~{result.before_tokens:,} -> "
            f"~{result.after_tokens:,} est. tokens (summarized {result.summarized}, kept {result.kept})."
        )

    def _post_compact_cleanup(self, method: str) -> None:
        """Unified post-compact reset (CC's runPostCompactCleanup analogue).

        Centralizes what a compaction invalidates so every path behaves the same:
          * the real prompt-token count is stale after a summary rewrite — clear
            it so the trigger uses the estimate until a fresh response arrives;
          * a one-shot pending-diagnostics message may reference pruned context;
          * discovered deferred tools are INTENTIONALLY kept (registry state
            survives) so the model needn't ToolSearch them again — CC carries
            preCompactDiscoveredTools across the boundary for the same reason.
        """
        if method == "summarize":
            self._last_input_tokens = 0
        self._pending_diagnostics = ""

    def reset_conversation(self) -> str:
        self.messages = [
            {"role": "system", "content": build_system_prompt(self.ctx.memory, self.ctx.skills)}
        ]
        self.usage.clear()
        return "Conversation reset. Messages now: 1"

    def _compaction_tail_start(self, target_tail: int) -> int:
        start = max(1, len(self.messages) - target_tail)
        # Tool messages are only valid immediately after their assistant tool_calls message.
        while start > 1 and self.messages[start].get("role") == "tool":
            start -= 1
        return start

    def _apply_tool_budget(self) -> None:
        """L0: shed oversized tool output cache-safely before compaction.

        Runs before the token-budget trigger (CC's ordering: tool-result budget
        precedes microcompact/autocompact). Only fresh (never-sent) tool results
        are replaced with previews, so the cached prefix stays byte-stable; the
        Recovery/offload paths let the model re-run a tool if it needs the full
        output. Composes cleanly with prune/summary — both key off tool_call_id.
        """
        new_messages, replaced = enforce_tool_result_budget(self.messages, self._tool_budget)
        if replaced:
            self.messages = new_messages

    def _maybe_compact(self) -> None:
        # L0 tool-result budget first (cheapest, cache-safe), then the
        # token-budget trigger (primary), with the legacy message-count trigger
        # as a cheap secondary safety net for pathological short-but-huge messages.
        self._apply_tool_budget()
        self.compact(manual=False)
        if len(self.messages) > self.config.compact_after_messages * 4:
            self.compact(manual=True)

    def _stream_turn(self, render_ctx: object):
        """Drive one model completion as a stream, with reactive overflow recovery.

        Yields ``TextDelta`` chunks live (when streaming is enabled) and returns
        ``(turn, streamed)`` via ``yield from``. On a live context-overflow error
        — raised at request start, before any text is yielded — compact once and
        retry, so mid-turn growth past the window doesn't crash the turn.
        """
        try:
            result = yield from self._drive_stream(
                self._provider_messages(), self.registry.schemas(render_ctx)
            )
        except Exception as exc:
            if not is_context_overflow_error(str(exc)):
                raise
            # Reactive compaction: bypass the token floor and summarize now.
            self.compact(manual=True)
            result = yield from self._drive_stream(
                self._provider_messages(), self.registry.schemas(render_ctx)
            )
        return result

    def _drive_stream(self, messages: list[dict[str, Any]], schemas: list[dict[str, Any]]):
        """Consume the provider's stream, emitting live deltas; return (turn, streamed).

        Falls back to a single blocking call for duck-typed providers that only
        implement ``complete``. ``streamed`` is True only when incremental text
        was actually surfaced to the UI, so the caller knows whether to also emit
        a final block.
        """
        stream_fn = getattr(self.provider, "complete_stream", None)
        if stream_fn is None:
            turn = self.provider.complete(messages, schemas)
            return turn, False

        show = bool(getattr(self.config, "stream_output", True))
        final: ProviderTurn | None = None
        parts: list[str] = []
        streamed = False
        for event in stream_fn(messages, schemas):
            if getattr(event, "final", None) is not None:
                final = event.final
                continue
            text = getattr(event, "text", "")
            if text:
                parts.append(text)
                if show:
                    streamed = True
                    yield TextDelta(text, interim=True, streaming=True)
            # reasoning deltas are consumed (drive the stream) but not displayed.
        if final is None:
            final = ProviderTurn(content="".join(parts))
        return final, streamed

    def _add_usage(self, turn: ProviderTurn) -> None:
        # Locked: recall-prefetch and extraction side-queries add usage from
        # daemon threads, and dict read-modify-write is not atomic.
        with self._usage_lock:
            for key, value in turn.usage.items():
                if isinstance(value, int):
                    self.usage[key] = self.usage.get(key, 0) + value
            # Snapshot the real prompt-token count for the next compaction
            # trigger, and mark activity for cache-cold detection. Only main-loop
            # calls carry a prompt-token count worth trusting; side-queries
            # (summary, recall) report their own smaller prompts, so ignore those.
            prompt_tokens = turn.usage.get("prompt_tokens") or turn.usage.get("input_tokens")
            if isinstance(prompt_tokens, int) and prompt_tokens > 0:
                self._last_input_tokens = prompt_tokens
            self._last_activity_ts = time.time()

    def _synthesize_after_step_limit(self) -> ProviderTurn:
        prompt = (
            f"Tool step budget reached after {self.config.max_steps} executed tool step(s). "
            "Do not call any more tools. Use only the conversation and existing tool results above "
            "to provide the best possible final answer now. If evidence is incomplete, say what is "
            "uncertain, but still answer directly and helpfully."
        )
        try:
            turn = self.provider.complete([*self.messages, {"role": "user", "content": prompt}], [])
            self._add_usage(turn)
            if turn.content.strip():
                turn.content = turn.content.strip()
                return turn
        except Exception as exc:  # pragma: no cover - final safety net
            return ProviderTurn(content=self._fallback_step_limit_answer(str(exc)))
        return ProviderTurn(content=self._fallback_step_limit_answer())

    def _fallback_step_limit_answer(self, error: str = "") -> str:
        snippets = []
        for message in self.messages[-16:]:
            if message.get("role") != "tool":
                continue
            name = message.get("name", "tool")
            content = " ".join(str(message.get("content", "")).split())
            if content:
                snippets.append(f"- {name}: {content[:500]}")
        body = "\n".join(snippets) or "- No tool output was captured before the step limit."
        suffix = f"\n\nFinal synthesis failed: {error}" if error else ""
        return (
            f"Reached the {self.config.max_steps}-step tool budget. Here is the best answer I can "
            f"provide from the information already gathered:\n\n{body}{suffix}"
        )

    def _assistant_content_message(self, content: str, reasoning_content: str = "") -> dict[str, Any]:
        message = {"role": "assistant", "content": content}
        if reasoning_content:
            message["reasoning_content"] = reasoning_content
        return message

    def _internal_observation_message(self, name: str, arguments: dict[str, Any], output: str) -> dict[str, str]:
        args = json.dumps(arguments, ensure_ascii=False, default=str)
        content = (
            "Internal LilBot orchestration result for the previous user request. "
            "Do not treat this as a new user request; use it only as evidence.\n"
            f"Tool: {name}\n"
            f"Arguments: {args}\n"
            f"Result:\n{output[:12000]}"
        )
        return {"role": "user", "content": content}

    def _delegation_guidance_message(self, reason: str, names: list[str]) -> dict[str, str]:
        content = (
            "Internal LilBot orchestration guidance for the previous user request. "
            "The parent agent has already delegated focused evidence gathering to subagents: "
            f"{', '.join(names)}.\n"
            f"Delegation reason: {reason}\n"
            "Use the subagent results above as primary evidence for final synthesis. "
            "Avoid repeating the same web/search/tool calls in the parent unless a subagent failed, "
            "lacked source evidence, or a critical fact is still missing."
        )
        return {"role": "user", "content": content}

    def _assistant_tool_message(self, turn: ProviderTurn) -> dict[str, Any]:
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
