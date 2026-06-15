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


def _truncate(text: str, limit: int = 2000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "... truncated ..."


def _response_error_detail(response: Any) -> str:
    try:
        data = response.json()
    except Exception:
        text = str(getattr(response, "text", "")).strip()
        return _truncate(text) if text else "empty response body"

    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            parts = []
            for key in ("message", "type", "code", "param"):
                value = error.get(key)
                if value:
                    parts.append(f"{key}={value}")
            if parts:
                return "; ".join(parts)
    return _truncate(json.dumps(data, ensure_ascii=False))


def _extract_path(text: str) -> str:
    quoted = re.findall(r"`([^`]+)`|'([^']+)'|\"([^\"]+)\"", text)
    for group in quoted:
        value = next((item for item in group if item), "")
        if value:
            return value
    tokens = re.findall(r"[\w./\\-]+\.[A-Za-z0-9]+|[\w./\\-]+", text)
    skip = {"read", "list", "show", "file", "files"}
    for token in reversed(tokens):
        if token.lower() not in skip:
            return token
    return "."


def _looks_like_web_query(text: str) -> bool:
    terms = {
        "openclaw",
        "latest",
        "current",
        "today",
        "news",
        "website",
        "web",
        "internet",
    }
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
            return ProviderTurn(tool_calls=[ToolCall("exec_shell", {"command": content.strip()[1:].strip()})])
        if any(word in lower for word in ["list files", "list dir"]) and "tool" not in lower:
            return ProviderTurn(tool_calls=[ToolCall("list_dir", {"path": _extract_path(content), "max_depth": 1})])
        if any(word in lower for word in ["read", "show file"]):
            return ProviderTurn(tool_calls=[ToolCall("read_file", {"path": _extract_path(content)})])
        if _looks_like_web_query(content):
            return ProviderTurn(tool_calls=[ToolCall("web_search", {"query": content, "max_results": 5})])
        if any(word in lower for word in ["grep", "search"]):
            words = [w for w in re.split(r"\s+", content.strip()) if w]
            query = words[-1] if words else content
            return ProviderTurn(tool_calls=[ToolCall("grep_files", {"pattern": query, "path": "."})])
        if "remember" in lower:
            return ProviderTurn(tool_calls=[ToolCall("memory_save", {"name": "note", "text": content})])
        if "skill" in lower:
            return ProviderTurn(tool_calls=[ToolCall("skill_list", {})])
        if "agent" in lower:
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
        if "review" in prompt.lower():
            return "Review focus: input boundaries, error handling, permissions, tests, and user-visible behavior."
        if "plan" in prompt.lower():
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
        url = f"{self.config.base_url.rstrip('/')}/chat/completions"
        with httpx.Client(timeout=120) as client:
            response = client.post(url, headers=headers, json=body)
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                detail = _response_error_detail(exc.response)
                raise ProviderError(
                    f"{self.config.provider} chat request failed: "
                    f"{exc.response.status_code} {exc.response.reason_phrase} "
                    f"for {exc.request.url} (model={self.config.model}). {detail}"
                ) from exc
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
        return ProviderTurn(
            message.get("content") or "",
            calls,
            usage,
            str(message.get("reasoning_content") or ""),
        )

    def _messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        clean: list[dict[str, Any]] = []
        pending_tool_ids: set[str] = set()
        pending_start: int | None = None

        def drop_incomplete_tool_block() -> None:
            nonlocal pending_tool_ids, pending_start
            if pending_tool_ids and pending_start is not None:
                del clean[pending_start:]
            pending_tool_ids = set()
            pending_start = None

        for message in messages:
            role = message.get("role")
            if role == "tool":
                tool_call_id = str(message.get("tool_call_id") or "")
                if pending_tool_ids and tool_call_id in pending_tool_ids:
                    clean.append({
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "content": str(message.get("content", "")),
                    })
                    pending_tool_ids.remove(tool_call_id)
                    if not pending_tool_ids:
                        pending_start = None
                continue

            if pending_tool_ids:
                drop_incomplete_tool_block()

            if role == "assistant" and message.get("tool_calls"):
                tool_calls = []
                ids = []
                for call in message.get("tool_calls") or []:
                    if not isinstance(call, dict):
                        continue
                    call_id = str(call.get("id") or "")
                    fn = call.get("function") or {}
                    if not call_id or not isinstance(fn, dict) or not fn.get("name"):
                        continue
                    arguments = fn.get("arguments") or "{}"
                    if not isinstance(arguments, str):
                        arguments = json.dumps(arguments, ensure_ascii=False)
                    tool_calls.append({
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": str(fn.get("name")),
                            "arguments": arguments,
                        },
                    })
                    ids.append(call_id)
                if tool_calls:
                    pending_start = len(clean)
                    pending_tool_ids = set(ids)
                    assistant_message = {
                        "role": "assistant",
                        "content": str(message.get("content") or ""),
                        "tool_calls": tool_calls,
                    }
                    reasoning_content = str(message.get("reasoning_content") or "")
                    if reasoning_content:
                        assistant_message["reasoning_content"] = reasoning_content
                    clean.append(assistant_message)
            elif role in {"system", "user", "assistant"}:
                clean_message = {"role": role, "content": str(message.get("content", ""))}
                reasoning_content = str(message.get("reasoning_content") or "")
                if role == "assistant" and reasoning_content:
                    clean_message["reasoning_content"] = reasoning_content
                clean.append(clean_message)
        if pending_tool_ids:
            drop_incomplete_tool_block()
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
