from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

from ..config import LilBotConfig
from .events import ProviderTurn, TextDelta, ToolFinished, ToolStarted, TurnFinished
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
        steps = 0
        while steps < self.config.max_steps:
            turn = self.provider.complete(self.messages, self.registry.schemas())
            self._add_usage(turn)
            if turn.content:
                yield TextDelta(turn.content, interim=bool(turn.tool_calls))
            if not turn.tool_calls:
                self.messages.append({"role": "assistant", "content": turn.content})
                yield TurnFinished(steps, dict(self.usage))
                return

            remaining_steps = self.config.max_steps - steps
            calls_to_run = turn.tool_calls[:remaining_steps]
            self.messages.append(self._assistant_tool_message(ProviderTurn(turn.content, calls_to_run)))
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
        self.messages.append({"role": "assistant", "content": final})
        yield TextDelta(final)
        yield TurnFinished(steps, dict(self.usage))

    def compact(self) -> str:
        if len(self.messages) <= 8:
            return "Nothing to compact yet."
        keep = self.messages[-8:]
        older = self.messages[1:-8]
        summary_lines = []
        for message in older[-12:]:
            role = message.get("role", "?")
            content = " ".join(str(message.get("content", "")).split())
            if content:
                summary_lines.append(f"- {role}: {content[:180]}")
        summary = "Conversation summary before compaction:\n" + "\n".join(summary_lines)
        self.messages = [self.messages[0], {"role": "system", "content": summary}, *keep]
        return f"Compacted context. Messages now: {len(self.messages)}"

    def _maybe_compact(self) -> None:
        if len(self.messages) > self.config.compact_after_messages:
            self.compact()

    def _add_usage(self, turn: ProviderTurn) -> None:
        for key, value in turn.usage.items():
            if isinstance(value, int):
                self.usage[key] = self.usage.get(key, 0) + value

    def _synthesize_after_step_limit(self) -> str:
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
                return turn.content.strip()
        except Exception as exc:  # pragma: no cover - final safety net
            return self._fallback_step_limit_answer(str(exc))
        return self._fallback_step_limit_answer()

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

    def _assistant_tool_message(self, turn: ProviderTurn) -> dict[str, Any]:
        return {
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
