from __future__ import annotations

import json
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from ..config import LilBotConfig
from .compaction import (
    CompactCircuitBreaker,
    RecoveryState,
    auto_compact,
    estimate_tokens,
)
from .delegation import (
    parse_semantic_delegation_plan,
    semantic_delegation_messages,
    should_consult_semantic_delegation,
)
from ..hooks import HookContext, HookEngine, load_hooks
from ..memory import extract_memories, recall

MEMORY_EXTRACTION_INTERVAL = 3
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
        # Context compaction state (ported from mewcode).
        self.recovery = RecoveryState()
        self.compact_breaker = CompactCircuitBreaker()
        # Lifecycle hooks (ported from mewcode). Loaded from .lilbot/hooks.json.
        state_dir = getattr(config, "state_dir", None)
        workspace = getattr(config, "workspace", None)
        self.hooks = HookEngine(load_hooks(state_dir), cwd=workspace)
        # Memory recall / extraction state (ported from mewcode).
        self._turn_count = 0
        self._recent_tools: list[str] = []
        self._surfaced_memory_ids: set[str] = set()
        self._pending_recall = ""

    def run_turn(self, user_text: str) -> Iterator[object]:
        self.messages.append({"role": "user", "content": user_text})
        self._turn_count += 1
        self._maybe_compact()
        self._maybe_recall(user_text)
        if self.hooks.has_hooks():
            self.hooks.run("turn_start", HookContext(event="turn_start", message=user_text))
        steps = self._auto_delegate(user_text)
        while steps < self.config.max_steps:
            self._drain_team_notifications()
            render_ctx = self.ctx.subagents.get_render_context() if getattr(self.ctx, "subagents", None) else None
            turn = self.provider.complete(self._provider_messages(), self.registry.schemas(render_ctx))
            self._add_usage(turn)
            if turn.content:
                yield TextDelta(turn.content, interim=bool(turn.tool_calls))
            if not turn.tool_calls:
                self.messages.append(self._assistant_content_message(turn.content, turn.reasoning_content))
                self._fire_turn_end()
                self._maybe_extract()
                yield TurnFinished(steps, dict(self.usage))
                return

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
                    yield ToolFinished(call.name, result.ok, result.output, elapsed_ms, result.metadata)
                    self.messages.append({
                        "role": "tool",
                        "tool_call_id": call.call_id,
                        "name": call.name,
                        "content": result.output,
                    })

        self._fire_turn_end()
        self._maybe_extract()
        final = self._synthesize_after_step_limit()
        self.messages.append(self._assistant_content_message(final.content, final.reasoning_content))
        yield TextDelta(final.content)
        yield TurnFinished(steps, dict(self.usage))

    def _provider_messages(self) -> list[dict[str, Any]]:
        """Build the message list for one provider call.

        Appends transient system messages (NOT persisted into history): the
        deferred-tool reminder plus any pending hook prompt/notification output.
        Keeping these at the tail preserves the stable prefix that drives
        server-side prompt caching.
        """
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
        if not self.hooks.has_hooks():
            return None
        ctx = HookContext(
            event="pre_tool_use",
            tool_name=call.name,
            tool_args=dict(call.arguments),
            file_path=str(call.arguments.get("path") or call.arguments.get("file_path") or ""),
        )
        return self.hooks.run_pre_tool(ctx)

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

    def _fire_turn_end(self) -> None:
        if self.hooks.has_hooks():
            self.hooks.run("turn_end", HookContext(event="turn_end"))

    # -- Memory recall / extraction (ported from mewcode) ------------------

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

    def _maybe_recall(self, query: str) -> None:
        self._pending_recall = ""
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
        try:
            reminder, ids = recall(
                query, entries, self._recent_tools[-8:],
                self._surfaced_memory_ids, self._meta_query,
            )
        except Exception:
            return
        if reminder:
            self._pending_recall = reminder
            self._surfaced_memory_ids.update(ids)

    def _maybe_extract(self) -> None:
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
        try:
            index = "\n".join(f"- {e.name}" for e in store.list())
            extract_memories(text, index, self._meta_query, store)
        except Exception:
            pass

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
        teammate progress mid-turn without blocking or polling. Mirrors mewcode's
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
        tool = self.registry.get(call.name)
        return tool is not None and tool.concurrency_safe

    def _partition_calls(self, calls: list[ToolCall]) -> list[list[ToolCall]]:
        """Group a run of consecutive read-only calls so they can run in parallel.

        Order is preserved: a concurrency-safe call extends the current safe
        batch; anything else starts its own singleton batch. Ported from
        mewcode's partition_tool_calls.
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
        result, elapsed_ms = self.registry.execute(call.name, call.arguments, self.ctx)
        self._record_for_recovery(call, result)
        self._post_tool_hook(call, result)
        return result, elapsed_ms

    def _run_calls_parallel(self, batch: list[ToolCall]) -> list[tuple[ToolResult, int]]:
        max_workers = min(len(batch), max(1, getattr(self.config, "subagent_max_concurrent", 8)))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            return list(pool.map(self._run_one_call, batch))

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

    def _tool_names_for_recovery(self) -> list[str]:
        try:
            return [str(s.get("name")) for s in self.registry.schemas() if s.get("name")]
        except Exception:
            return []

    def compact(self, manual: bool = True) -> str:
        before = estimate_tokens(self.messages)
        result = auto_compact(
            self.messages,
            self._summarize,
            self.config.context_window,
            manual=manual,
            recovery=self.recovery,
            tool_names=self._tool_names_for_recovery(),
            breaker=self.compact_breaker,
        )
        if result is None:
            return "Nothing to compact yet." if manual else ""
        self.messages = result.messages
        return (
            f"Compacted context: ~{result.before_tokens:,} -> ~{result.after_tokens:,} est. tokens "
            f"(summarized {result.summarized} msgs, kept {result.kept})."
        )

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

    def _maybe_compact(self) -> None:
        # Token-budget trigger (primary), with the legacy message-count trigger
        # as a cheap secondary safety net for pathological short-but-huge messages.
        self.compact(manual=False)
        if len(self.messages) > self.config.compact_after_messages * 4:
            self.compact(manual=True)

    def _add_usage(self, turn: ProviderTurn) -> None:
        for key, value in turn.usage.items():
            if isinstance(value, int):
                self.usage[key] = self.usage.get(key, 0) + value

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
