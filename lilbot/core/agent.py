from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

from ..config import LilBotConfig
from .delegation import (
    parse_semantic_delegation_plan,
    semantic_delegation_messages,
    should_consult_semantic_delegation,
)
from .events import ProviderTurn, TextDelta, ToolCall, ToolFinished, ToolStarted, TurnFinished
from .prompts import build_system_prompt
from ..llm.providers import BaseProvider
from ..tools import ToolContext, ToolRegistry


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
        self.messages: list[dict[str, Any]] = [
            {"role": "system", "content": build_system_prompt(ctx.memory, ctx.skills)}
        ]
        self.usage: dict[str, int] = {}

    def run_turn(self, user_text: str) -> Iterator[object]:
        self.messages.append({"role": "user", "content": user_text})
        self._maybe_compact()
        self._inject_subagent_nudge(user_text)
        steps = self._auto_delegate(user_text)
        while steps < self.config.max_steps:
            render_ctx = self.ctx.subagents.get_render_context() if getattr(self.ctx, "subagents", None) else None
            turn = self.provider.complete(self.messages, self.registry.schemas(render_ctx))
            self._add_usage(turn)
            if turn.content:
                yield TextDelta(turn.content, interim=bool(turn.tool_calls))
            if not turn.tool_calls:
                self.messages.append(self._assistant_content_message(turn.content, turn.reasoning_content))
                yield TurnFinished(steps, dict(self.usage))
                return

            remaining_steps = self.config.max_steps - steps
            calls_to_run = turn.tool_calls[:remaining_steps]
            self.messages.append(self._assistant_tool_message(
                ProviderTurn(turn.content, calls_to_run, turn.usage, turn.reasoning_content)
            ))
            for call in calls_to_run:
                steps += 1
                yield ToolStarted(call.name, call.arguments)
                result, elapsed_ms = self.registry.execute(call.name, call.arguments, self.ctx)
                yield ToolFinished(call.name, result.ok, result.output, elapsed_ms, result.metadata)
                self.messages.append({
                    "role": "tool",
                    "tool_call_id": call.call_id,
                    "name": call.name,
                    "content": result.output,
                })
                if steps >= self.config.max_steps:
                    break

        final = self._synthesize_after_step_limit()
        self.messages.append(self._assistant_content_message(final.content, final.reasoning_content))
        yield TextDelta(final.content)
        yield TurnFinished(steps, dict(self.usage))

    def _auto_delegate(self, user_text: str) -> int:
        """No-op: Dynamic Agent Tool Prompt Parity replaces keyword-based auto-delegation.

        The LLM now reads live agent type descriptions in the agent_open tool schema
        and autonomously decides when to launch parallel subagents. The runtime
        (SubAgentManager gates) continues to enforce security.
        """
        return 0

    def _inject_subagent_nudge(self, user_text: str) -> None:
        """Inject a system message when the query clearly benefits from sub-agents.

        This is the runtime enforcement of the Constitution's Sub-Agent First rule.
        It does NOT pre-launch sub-agents — it tells the LLM to use them.

        For flash models (weaker instruction-following), triggers are broader.
        """
        if not self.registry.get("agent_open"):
            return

        text = user_text.strip()
        lower = text.lower()

        # Model detection: flash needs more aggressive nudging
        is_flash = "flash" in getattr(self.config, "model", "").lower()

        # Count independent questions
        question_marks = text.count("?") + text.count("？")
        numbered_items = sum(1 for line in text.split("\n") if line.strip() and line.strip()[0].isdigit())

        # Comparison / multi-topic / research patterns
        comparison = any(kw in lower for kw in (
            "compare", "对比", "比较", "vs", "哪个", "区别", "difference", "versus",
        ))
        multi_search = question_marks >= 2 or numbered_items >= 3
        research_topic = any(kw in lower for kw in (
            "research", "latest", "trend", "recommend", "研究", "调研", "最新", "趋势",
            "推荐", "建议", "攻略", "怎么", "如何", "为什么", "是什么",
        )) and not any(kw in lower for kw in ("read file", "show file", "list files", "读", "打开"))
        writing_task = any(kw in lower for kw in (
            "write", "essay", "draft", "article", "写", "文章", "作文", "报告", "草稿", "大纲",
        ))
        codebase_explore = any(kw in lower for kw in (
            "analyze the codebase", "analyze this project", "architecture",
            "分析代码", "分析项目", "分析架构", "代码结构", "项目结构", "源码分析",
        )) and not any(kw in lower for kw in ("read file", "show file", "list files", "读", "打开"))

        # For flash: almost any non-trivial query gets the nudge
        is_trivial = len(text.split()) < 4 or lower in ("hello", "hi", "hey", "你好", "谢谢", "help")

        if comparison or multi_search:
            self.messages.append({
                "role": "system",
                "content": (
                    "This query requires multiple independent searches or comparisons. "
                    "Open parallel researcher sub-agents using agent_open(type=\"researcher\", ...) "
                    "— one per search topic. Collect results with agent_eval, then synthesize. "
                    "Do NOT call web_search yourself."
                ),
            })
        elif codebase_explore:
            self.messages.append({
                "role": "system",
                "content": (
                    "This query requires multi-file codebase exploration. "
                    "Open parallel explore sub-agents using agent_open(type=\"explore\", ...) "
                    "— one per directory or investigation axis. Collect with agent_eval, then synthesize. "
                    "Do NOT read files one at a time yourself."
                ),
            })
        elif research_topic:
            self.messages.append({
                "role": "system",
                "content": (
                    "This is a research / fact-finding query. "
                    "Use a researcher sub-agent via agent_open(type=\"researcher\", ...) "
                    "instead of calling web_search yourself. The sub-agent will gather "
                    "evidence with citations; you synthesize the final answer."
                ),
            })
        elif writing_task:
            self.messages.append({
                "role": "system",
                "content": (
                    "This is a writing task. Consider using agent_open(type=\"writer\", ...) "
                    "and agent_open(type=\"critic\", ...) in parallel for draft + review."
                ),
            })
        elif is_flash and not is_trivial:
            # Flash fallback: any non-trivial query gets a general nudge
            self.messages.append({
                "role": "system",
                "content": (
                    "Use agent_open to delegate work to sub-agents instead of doing it yourself. "
                    "Check the agent_open tool description for available types (researcher for web, "
                    "explore for code, writer for text, etc.). Parallel sub-agents are faster."
                ),
            })

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

    def compact(self) -> str:
        if len(self.messages) <= 8:
            return "Nothing to compact yet."
        tail_start = self._compaction_tail_start(8)
        if tail_start <= 1:
            return "Nothing to compact safely yet."
        keep = self.messages[tail_start:]
        older = self.messages[1:tail_start]
        summary_lines = []
        for message in older[-12:]:
            role = message.get("role", "?")
            content = " ".join(str(message.get("content", "")).split())
            if content:
                summary_lines.append(f"- {role}: {content[:180]}")
        summary = "Conversation summary before compaction:\n" + "\n".join(summary_lines)
        self.messages = [self.messages[0], {"role": "system", "content": summary}, *keep]
        return f"Compacted context. Messages now: {len(self.messages)}"

    def _compaction_tail_start(self, target_tail: int) -> int:
        start = max(1, len(self.messages) - target_tail)
        # Tool messages are only valid immediately after their assistant tool_calls message.
        while start > 1 and self.messages[start].get("role") == "tool":
            start -= 1
        return start

    def _maybe_compact(self) -> None:
        if len(self.messages) > self.config.compact_after_messages:
            self.compact()

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
