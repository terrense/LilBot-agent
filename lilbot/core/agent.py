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
                yield TextDelta(turn.content)
            if not turn.tool_calls:
                self.messages.append({"role": "assistant", "content": turn.content})
                yield TurnFinished(steps, dict(self.usage))
                return

            self.messages.append(self._assistant_tool_message(turn))
            for call in turn.tool_calls:
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

        yield TextDelta(f"Stopped after max_steps={self.config.max_steps}.")
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
