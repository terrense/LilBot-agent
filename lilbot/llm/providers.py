from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Iterator

from ..config import LilBotConfig
from ..core.events import ProviderTurn, StreamEvent, ToolCall


class ProviderError(RuntimeError):
    """Provider failure. Carries a structured ``status_code`` and ``is_overflow``
    flag so the agent can react to a context-overflow (413 / context_length_exceeded)
    by compacting and retrying, instead of matching error strings (CC's lesson:
    judge errors by structured signals, use text only as a fallback)."""

    def __init__(self, message: str, *, status_code: int | None = None, is_overflow: bool = False):
        super().__init__(message)
        self.status_code = status_code
        self.is_overflow = is_overflow


class BaseProvider:
    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ProviderTurn:
        raise NotImplementedError

    def complete_stream(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> Iterator[StreamEvent]:
        """Default: no real streaming — run the blocking call and emit the turn.

        Providers without an incremental transport (rule-based, test fakes) keep
        working: the agent sees a single terminal event and renders one block.
        """
        yield StreamEvent(final=self.complete(messages, tools))


def _normalize_usage(usage: dict[str, Any]) -> dict[str, Any]:
    """Flatten provider usage and surface prompt-cache hits as a plain int.

    DeepSeek/OpenAI-compatible endpoints cache common prefixes server-side and
    report the hit count differently:
      * DeepSeek: ``prompt_cache_hit_tokens`` / ``prompt_cache_miss_tokens``
      * OpenAI:   ``prompt_tokens_details.cached_tokens``
    We normalize both into ``cache_read_tokens`` so the agent's usage accounting
    (and the /status view) can show how much of the prompt was served from cache.
    A stable message prefix — system prompt first, deferred-tool reminder kept at
    the tail — is what makes these hits happen in the first place.
    """
    out: dict[str, Any] = {k: v for k, v in usage.items() if isinstance(v, int)}
    cached = usage.get("prompt_cache_hit_tokens")
    if not isinstance(cached, int):
        details = usage.get("prompt_tokens_details")
        if isinstance(details, dict) and isinstance(details.get("cached_tokens"), int):
            cached = details["cached_tokens"]
    if isinstance(cached, int):
        out["cache_read_tokens"] = cached
    return out


def _truncate(text: str, limit: int = 2000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "... truncated ..."


def _is_overflow_error(status_code: int, detail: str) -> bool:
    """Structured context-overflow classification (CC parity).

    A 413 ("request entity too large") is always overflow; otherwise fall back to
    the error-body text markers (``context_length_exceeded`` / "prompt too long"
    are usually returned as 400 with a specific message).
    """
    if status_code == 413:
        return True
    from ..core.compaction import is_context_overflow_error  # local: avoids cycle
    return is_context_overflow_error(detail)


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


def _http_timeout() -> Any:
    """Split timeout: fail fast on a stalled connect, stay patient on reads.

    A dead network no longer blocks the whole 120s window with zero feedback;
    a genuinely slow (but alive) generation still gets a generous read budget,
    and with streaming the read clock resets on every chunk.
    """
    import httpx

    return httpx.Timeout(120.0, connect=10.0)


def _iter_stream_events(lines: Iterable[str]) -> Iterator[StreamEvent]:
    """Parse an OpenAI-compatible SSE line stream into StreamEvents.

    Pure and transport-free (takes any iterable of raw SSE lines) so it can be
    unit-tested without a live socket. Yields incremental ``text`` / ``reasoning``
    deltas as they arrive, then one terminal event carrying the assembled turn.
    """
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_slots: dict[int, dict[str, str]] = {}
    usage: dict[str, Any] = {}
    finish_reason: str = ""

    for raw in lines:
        if not raw:
            continue
        line = raw.strip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if payload == "[DONE]":
            break
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            continue

        raw_usage = obj.get("usage")
        if isinstance(raw_usage, dict) and raw_usage:
            usage = _normalize_usage(raw_usage)

        choices = obj.get("choices") or []
        if not choices:
            continue
        if choices[0].get("finish_reason"):
            finish_reason = str(choices[0]["finish_reason"])
        delta = choices[0].get("delta") or {}

        text = delta.get("content")
        if text:
            content_parts.append(text)
            yield StreamEvent(text=text)

        reasoning = delta.get("reasoning_content")
        if reasoning:
            reasoning_parts.append(reasoning)
            yield StreamEvent(reasoning=reasoning)

        for call in delta.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            index = call.get("index", 0)
            slot = tool_slots.setdefault(index, {"id": "", "name": "", "args": ""})
            if call.get("id"):
                slot["id"] = str(call["id"])
            fn = call.get("function") or {}
            if fn.get("name"):
                slot["name"] = str(fn["name"])
            if fn.get("arguments"):
                slot["args"] += str(fn["arguments"])

    calls: list[ToolCall] = []
    for index in sorted(tool_slots):
        slot = tool_slots[index]
        if not slot["name"]:
            continue
        try:
            args = json.loads(slot["args"] or "{}")
        except json.JSONDecodeError:
            args = {"raw": slot["args"]}
        calls.append(ToolCall(slot["name"], args, slot["id"] or "tool"))

    yield StreamEvent(
        final=ProviderTurn(
            "".join(content_parts),
            calls,
            usage,
            "".join(reasoning_parts),
            finish_reason=finish_reason,
        )
    )


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
        with httpx.Client(timeout=_http_timeout()) as client:
            response = client.post(url, headers=headers, json=body)
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                detail = _response_error_detail(exc.response)
                code = exc.response.status_code
                raise ProviderError(
                    f"{self.config.provider} chat request failed: "
                    f"{code} {exc.response.reason_phrase} "
                    f"for {exc.request.url} (model={self.config.model}). {detail}",
                    status_code=code,
                    is_overflow=_is_overflow_error(code, detail),
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
        usage = _normalize_usage(data.get("usage") or {})
        return ProviderTurn(
            message.get("content") or "",
            calls,
            usage,
            str(message.get("reasoning_content") or ""),
            finish_reason=str(data["choices"][0].get("finish_reason") or ""),
        )

    def complete_stream(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> Iterator[StreamEvent]:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - dependency hint
            raise ProviderError("httpx is required for OpenAI-compatible provider") from exc
        if not self.config.api_key:
            raise ProviderError("missing LILBOT_API_KEY or OPENAI_API_KEY")

        body: dict[str, Any] = {
            "model": self.config.model,
            "messages": self._messages(messages),
            "stream": True,
            # Ask the server to include a usage block on the final SSE chunk so
            # cache-hit accounting survives the streaming path.
            "stream_options": {"include_usage": True},
        }
        if tools:
            body["tools"] = [self._tool_schema(tool) for tool in tools]
            body["tool_choice"] = "auto"
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }
        url = f"{self.config.base_url.rstrip('/')}/chat/completions"
        try:
            with httpx.Client(timeout=_http_timeout()) as client:
                with client.stream("POST", url, headers=headers, json=body) as response:
                    if response.status_code >= 400:
                        response.read()
                        detail = _response_error_detail(response)
                        code = response.status_code
                        raise ProviderError(
                            f"{self.config.provider} chat stream failed: "
                            f"{code} {response.reason_phrase} "
                            f"for {response.request.url} (model={self.config.model}). {detail}",
                            status_code=code,
                            is_overflow=_is_overflow_error(code, detail),
                        )
                    yield from _iter_stream_events(response.iter_lines())
        except ProviderError:
            raise
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"{self.config.provider} chat stream failed for {url} "
                f"(model={self.config.model}): {exc}"
            ) from exc

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
