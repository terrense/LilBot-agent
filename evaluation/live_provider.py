"""Guard and observe the existing live Provider at the Agent boundary."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from time import perf_counter
from typing import Any

from lilbot.core.events import ProviderTurn, StreamEvent, ToolCall
from lilbot.llm.errors import classify
from lilbot.llm.providers import BaseProvider, ProviderError
from lilbot.memory.file_store import _slugify
from lilbot.tools.registry import ToolContext, ToolRegistry, ToolResult

from .dataset import resolve_safe_path, safe_relative_path
from .trace import TraceWriter


_PATH_FILES = {"read_file", "write_file", "edit_file"}
_PATH_SEARCH = {"list_dir", "grep", "glob"}
_VALID_TOOL_NAME = re.compile(r"[a-zA-Z0-9_-]{1,64}")
_ARG_TYPES = {"string": str, "integer": int, "boolean": bool}


class BlockedArguments(dict):
    """Carry the rejected input out-of-band, so Agent cannot snapshot its path."""

    def __init__(self, name: str, arguments: Any, kind: str, message: str):
        signature = hashlib.sha256(json.dumps(arguments, sort_keys=True).encode("ascii")).hexdigest()
        super().__init__(evaluation_denied=signature)
        self.name = name
        self.original = arguments
        self.kind = kind
        self.message = message


class LiveToolRegistry(ToolRegistry):
    def execute(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> tuple[ToolResult, int]:
        if isinstance(arguments, BlockedArguments):
            return ToolResult(False, arguments.message, {"evaluation_failure": arguments.kind}), 0
        return super().execute(name, arguments, ctx)


class LiveProvider(BaseProvider):
    def __init__(self, provider: BaseProvider, registry: LiveToolRegistry, workspace: Path,
                 trace: TraceWriter, request_limit: int):
        self.provider = provider
        self.registry = registry
        self.workspace = workspace
        self.trace = trace
        self.request_limit = request_limit
        self.request_count = 0
        self.turn_index = 1

    def _blocked(self, call: ToolCall, kind: str, message: str) -> ToolCall:
        name = call.name if _VALID_TOOL_NAME.fullmatch(call.name) else "evaluation_denied"
        arguments = BlockedArguments(call.name, call.arguments, kind, message)
        return replace(call, name=name, arguments=arguments)

    def _guard_call(self, call: ToolCall) -> ToolCall:
        args = call.arguments
        # Exact names prevent aliases from bypassing the evaluation allowlist.
        tool = self.registry.get(call.name)
        if tool is None or tool.name != call.name:
            return self._blocked(call, "permission", "Tool denied by evaluation policy. Use an offered tool.")
        schema = tool.input_schema
        properties = schema["properties"]
        if (
            not isinstance(args, dict)
            or set(schema.get("required", ())) - args.keys()
            or args.keys() - properties.keys()
            or any(type(value) is not _ARG_TYPES[properties[key]["type"]] for key, value in args.items())
        ):
            return self._blocked(call, "validation", "Tool arguments do not match its schema.")
        try:
            json.dumps(args, ensure_ascii=False).encode("utf-8")
        except UnicodeError:
            return self._blocked(call, "validation", "Tool arguments must be valid UTF-8 text.")
        if call.name in _PATH_FILES | _PATH_SEARCH:
            raw_path = args.get("path", ".")
            if raw_path != "." or call.name in _PATH_FILES:
                try:
                    path = resolve_safe_path(self.workspace, raw_path)
                    if call.name in _PATH_FILES and path.suffix.lower() != ".txt":
                        raise ValueError("Only text files are supported")
                except (OSError, ValueError):
                    return self._blocked(call, "permission", "Path denied by evaluation policy. Use workspace-relative text files or directories.")
        if call.name == "memory_save":
            try:
                safe_relative_path(f"{_slugify(args['name'])}.md")
            except ValueError:
                return self._blocked(call, "validation", "Memory name must be safe as a local filename.")
            if any(char in args.get(key, "") for key in ("name", "kind") for char in "\r\n\x00"):
                return self._blocked(call, "validation", "Memory names and kinds must be single-line text.")
        return call

    def _guard(self, turn: ProviderTurn) -> ProviderTurn:
        return replace(turn, tool_calls=[self._guard_call(call) for call in turn.tool_calls])

    def _begin(self, transport: str) -> tuple[int, float]:
        if self.request_count >= self.request_limit:
            self.trace.record("provider_budget", turn_index=self.turn_index, status="exhausted", reason="request_limit")
            raise ProviderError("Live evaluation request budget reached.")
        self.request_count += 1
        self.trace.record("provider_started", turn_index=self.turn_index, request_index=self.request_count,
                          action=transport, status="started")
        return self.request_count, perf_counter()

    def _finish(self, request: tuple[int, float], *, turn: ProviderTurn | None = None,
                exc: Exception | None = None) -> ProviderError | None:
        index, started = request
        info = classify(exc) if exc is not None else None
        reason = info.reason.value if info is not None else None
        self.trace.record(
            "provider_finished", turn_index=self.turn_index, request_index=index,
            status="failed" if exc is not None else "ok", reason=reason,
            elapsed_ms=(perf_counter() - started) * 1000,
            usage=(turn.usage or None) if turn is not None else None,
        )
        if info is None:
            return None
        # Preserve retry and compaction signals without persisting raw HTTP bodies.
        return ProviderError(
            f"Live provider request failed ({reason}).", reason=reason,
            status_code=info.status_code, retry_after=info.retry_after, is_overflow=info.should_compress,
        )

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ProviderTurn:
        request = self._begin("blocking")
        try:
            turn = self._guard(self.provider.complete(messages, tools))
        except Exception as exc:
            raise self._finish(request, exc=exc) from exc
        self._finish(request, turn=turn)
        return turn

    def complete_stream(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Iterator[StreamEvent]:
        request = self._begin("stream")
        final = None
        try:
            for event in self.provider.complete_stream(messages, tools):
                if event.final is not None:
                    final = self._guard(event.final)
                elif event.text or event.reasoning:
                    # Calls execute only after validation of the complete response.
                    yield replace(event, tool_call=None)
            if final is None:
                raise ProviderError("Live provider stream ended without a final response.", reason="format_error")
        except Exception as exc:
            raise self._finish(request, turn=final, exc=exc) from exc
        self._finish(request, turn=final)
        yield StreamEvent(final=final)

    def close(self) -> None:
        self.provider.close()
