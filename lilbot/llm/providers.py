from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from ..config import LilBotConfig
from ..core.events import ProviderTurn, ToolCall


class ProviderError(RuntimeError):
    pass


class BaseProvider:
    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ProviderTurn:
        raise NotImplementedError


def _last_message(messages: list[dict[str, Any]], role: str | None = None) -> dict[str, Any] | None:
    for message in reversed(messages):
        if role is None or message.get("role") == role:
            return message
    return None


def _extract_path(text: str) -> str:
    quoted = re.findall(r"`([^`]+)`|'([^']+)'|\"([^\"]+)\"", text)
    for group in quoted:
        value = next((item for item in group if item), "")
        if value:
            return value
    tokens = re.findall(r"[\w./\\-]+\.[A-Za-z0-9]+|[\w./\\-]+", text)
    skip = {"read", "list", "show", "file", "files", "读取", "列出", "查看", "文件"}
    for token in reversed(tokens):
        if token.lower() not in skip:
            return token
    return "."


def _looks_like_web_query(text: str) -> bool:
    terms = [
        "openclaw",
        "latest",
        "current",
        "today",
        "news",
        "website",
        "web",
        "internet",
        "最新",
        "今天",
        "新闻",
        "网页",
        "网站",
        "网上",
        "联网",
        "搜索网络",
        "检索网页",
    ]
    lower = text.lower()
    return any(term in lower for term in terms)


class RuleBasedProvider(BaseProvider):
    """Offline provider for testing the agent shell without API keys."""

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ProviderTurn:
        last = messages[-1] if messages else {"role": "user", "content": ""}
        content = str(last.get("content", ""))
        if last.get("role") == "tool":
            name = last.get("name", "tool")
            return ProviderTurn(content=f"Tool `{name}` returned:\n\n{content}")

        if not tools:
            return ProviderTurn(content=self._small_answer(content))

        lower = content.lower()
        if lower.strip().startswith("!"):
            return ProviderTurn(tool_calls=[ToolCall("bash", {"command": content.strip()[1:].strip()})])
        if any(word in lower for word in ["list files", "list dir", "列出", "目录"]) and "tool" not in lower:
            return ProviderTurn(tool_calls=[ToolCall("list_dir", {"path": _extract_path(content), "max_depth": 1})])
        if any(word in lower for word in ["read", "show file", "读取", "查看文件"]):
            return ProviderTurn(tool_calls=[ToolCall("read_file", {"path": _extract_path(content)})])
        if _looks_like_web_query(content):
            return ProviderTurn(tool_calls=[ToolCall("web_search", {"query": content, "max_results": 5})])
        if any(word in lower for word in ["grep", "search", "搜索", "查找"]):
            words = [w for w in re.split(r"\s+", content.strip()) if w]
            query = words[-1] if words else content
            return ProviderTurn(tool_calls=[ToolCall("grep", {"pattern": query, "path": "."})])
        if any(word in lower for word in ["remember", "记住"]):
            return ProviderTurn(tool_calls=[ToolCall("memory_save", {"name": "note", "text": content})])
        if "skill" in lower or "技能" in lower:
            return ProviderTurn(tool_calls=[ToolCall("skill_list", {})])
        if "agent" in lower or "子agent" in lower or "子 agent" in lower:
            return ProviderTurn(tool_calls=[ToolCall("agent_list", {})])
        return ProviderTurn(
            content=(
                "LilBot is ready. This is the offline rule provider, so it can demo tools and framework flow, "
                "but it will not plan complex work like a full LLM.\n"
                "Try `/tools`, `/skills`, `/memory list`, `! python --version`, or set `LILBOT_API_KEY` "
                "for an OpenAI-compatible model."
            )
        )

    def _small_answer(self, prompt: str) -> str:
        if "review" in prompt.lower() or "审查" in prompt:
            return "Review focus: input boundaries, error handling, permissions, tests, and user-visible behavior."
        if "plan" in prompt.lower() or "计划" in prompt:
            return "Plan in three steps: confirm constraints, build the smallest runnable version, then verify and document."
        return f"Received: {prompt}\n\nThis is a short response from LilBot's offline provider."


@dataclass
class OpenAICompatibleProvider(BaseProvider):
    config: LilBotConfig

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ProviderTurn:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - dependency hint
            raise ProviderError("httpx is required for OpenAI-compatible provider") from exc
        if not self.config.api_key:
            raise ProviderError("missing LILBOT_API_KEY or OPENAI_API_KEY")

        body: dict[str, Any] = {
            "model": self.config.model,
            "messages": self._messages(messages),
        }
        if tools:
            body["tools"] = [self._tool_schema(tool) for tool in tools]
            body["tool_choice"] = "auto"
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }
        url = f"{self.config.base_url}/chat/completions"
        with httpx.Client(timeout=120) as client:
            response = client.post(url, headers=headers, json=body)
            response.raise_for_status()
        data = response.json()
        message = data["choices"][0]["message"]
        calls: list[ToolCall] = []
        for call in message.get("tool_calls") or []:
            fn = call.get("function", {})
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {"raw": fn.get("arguments", "")}
            calls.append(ToolCall(fn.get("name", ""), args, call.get("id") or "tool"))
        usage = data.get("usage") or {}
        return ProviderTurn(message.get("content") or "", calls, usage)

    def _messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        clean: list[dict[str, Any]] = []
        for message in messages:
            role = message.get("role")
            if role == "tool":
                clean.append({
                    "role": "tool",
                    "tool_call_id": message.get("tool_call_id", "tool"),
                    "content": str(message.get("content", "")),
                })
            elif role == "assistant" and message.get("tool_calls"):
                clean.append(message)
            elif role in {"system", "user", "assistant"}:
                clean.append({"role": role, "content": str(message.get("content", ""))})
        return clean

    def _tool_schema(self, tool: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool["description"],
                "parameters": tool["input_schema"],
            },
        }


def choose_provider(config: LilBotConfig) -> BaseProvider:
    provider = config.provider.lower()
    if provider in {"openai", "deepseek"} or (provider == "auto" and config.api_key):
        return OpenAICompatibleProvider(config)
    return RuleBasedProvider()
