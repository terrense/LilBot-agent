"""Adapt LilBot turns and tool events to the evaluation trace."""

from __future__ import annotations

from collections import deque
from dataclasses import replace
from pathlib import Path
from sqlite3 import Error as SQLiteError
from time import perf_counter, sleep
from types import SimpleNamespace
from typing import Any

from lilbot.config import LilBotConfig
from lilbot.core.agent import Agent
from lilbot.core.events import ProviderRetry, ProviderTurn, ToolCall, ToolFinished, ToolStarted, TurnFinished
from lilbot.llm.providers import BaseProvider, ProviderError, RuleBasedProvider, choose_provider
from lilbot.memory.file_store import FileMemoryStore
from lilbot.sandbox.permissions import PermissionManager
from lilbot.sandbox.workspace import Sandbox
from lilbot.tools.builtin import register_builtins
from lilbot.tools.registry import ToolContext, ToolRegistry, ToolResult

from .dataset import resolve_safe_path, safe_relative_path
from .live_provider import BlockedArguments, LiveProvider, LiveToolRegistry
from .metrics import MODEL_NAME
from .trace import TraceWriter


_FILE_TOOLS = {"read_file", "write_file"}
_LIVE_TOOLS = _FILE_TOOLS | {"edit_file", "list_dir", "grep", "glob"}
_MEMORY_TOOLS = {"memory_save", "memory_search"}


class ScriptedProvider(BaseProvider):
    """Replay literal responses and check that real tool observations are fed back."""

    def __init__(self, script: list[dict[str, Any]], workspace: Path):
        self._operations = iter(script)
        self._workspace = workspace
        self._expected_call: ToolCall | None = None
        self._call_number = 0
        self.remaining = len(script)

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ProviderTurn:
        if self._expected_call is not None:
            last = messages[-1]
            if last.get("role") == "user" and not tools:
                # The real step-limit synthesis appends a user message after the tool result.
                last = messages[-2]
            if last.get("role") != "tool" or last.get("tool_call_id") != self._expected_call.call_id:
                raise RuntimeError("LilBot did not return the expected tool observation to the provider")
            self._expected_call = None

        operation = next(self._operations, None)
        if operation is None:
            raise RuntimeError("LilBot requested a response after the mock script ended")
        self.remaining -= 1
        if "tool" in operation:
            name, args = operation["tool"], operation["args"]
            if name in _FILE_TOOLS:
                path = resolve_safe_path(self._workspace, args["path"])
                if path.suffix.lower() != ".txt":
                    raise ValueError("Mock file tools support only UTF-8 .txt files")
            elif name != "exec_shell" or args:
                raise ValueError("Unsupported mock tool call")
            # Validate before yielding. Agent takes an edit snapshot before the registry gate.
            self._call_number += 1
            call = ToolCall(name, args, call_id=f"mock_{self._call_number}")
            self._expected_call = call
            return ProviderTurn(tool_calls=[call])
        if "error" in operation:
            raise ProviderError("Injected mock provider failure", reason="auth")
        if "sleep" in operation:
            sleep(operation["sleep"])
            return ProviderTurn()
        return ProviderTurn(content=operation["final"])


class LilBotAdapter:
    def run(
        self, user_input: str, setup: dict[str, Any], max_steps: int, workspace: Path, trace: TraceWriter,
        *, live_config: LilBotConfig | None = None,
    ) -> None:
        live = live_config is not None
        use_memory = live and setup.get("memory", False)
        mode = "ask" if setup.get("writable_files") or use_memory else "deny-all"
        config = replace(
            live_config or LilBotConfig(workspace=workspace, provider="mock", model=MODEL_NAME),
            workspace=workspace,
            permission_mode=mode,
            max_steps=max_steps,
            max_retries=1 if live else 0,
            fallback_model="",
            fallback_base_url="",
            aux_model="",
            aux_base_url="",
            auto_diagnostics=False,
            auto_allow_safe_commands=False,
        )
        permissions = PermissionManager(config.state_dir, mode=mode, interactive=False)
        for name in setup.get("writable_files", []):
            permissions.rules[f"write:{safe_relative_path(name)}"] = True
            if live:
                permissions.rules[f"edit:{safe_relative_path(name)}"] = True
        if use_memory:
            permissions.rules["eval:memory_save"] = True

        catalog = ToolRegistry()
        register_builtins(catalog)
        registry = LiveToolRegistry() if live else ToolRegistry()
        if live:
            requested = _LIVE_TOOLS | (_MEMORY_TOOLS if use_memory else set())
        else:
            requested = _FILE_TOOLS & {operation.get("tool") for operation in setup["script"]}
        for name in sorted(requested):
            definition = catalog.get(name)
            if definition is None:
                raise RuntimeError(f"LilBot is missing the {name} builtin")
            if live:
                definition.should_defer = False
            if name == "memory_save":
                definition.permission_check = _memory_permission
            registry.register(definition)

        turns = [user_input, *setup.get("followup", [])] if live else [user_input]
        if live:
            selected = choose_provider(config)
            if isinstance(selected, RuleBasedProvider):
                raise ValueError("Live evaluation requires a configured LLM provider.")
            provider = LiveProvider(selected, registry, workspace, trace, max_steps + 4 * len(turns) + 4)
        else:
            provider = ScriptedProvider(setup["script"], workspace)

        memory = SimpleNamespace(context=lambda: "")
        if use_memory:
            store = FileMemoryStore(config.state_dir, user_dir=config.state_dir / "eval-user-memory")
            # Expose actual memory operations while excluding background recall/extraction.
            memory = SimpleNamespace(context=store.context, add=store.add, search=store.search)
        context = ToolContext(
            sandbox=Sandbox(workspace),
            permissions=permissions,
            memory=memory,
            skills=SimpleNamespace(list=lambda: []),
            subagents=None,
            mcp=None,
            config=config,
        )
        agent = Agent(config, provider, registry, context)
        try:
            self._run_turns(agent, provider, turns, trace, max_steps)
        finally:
            agent.close()

    def _run_turns(
        self, agent: Agent, provider: ScriptedProvider | LiveProvider,
        turns: list[str], trace: TraceWriter, max_steps: int,
    ) -> None:
        trace.record("runtime_started", status="running")
        started = perf_counter()
        live = isinstance(provider, LiveProvider)
        steps = call_number = 0
        terminal = reason = final_output = failure = None
        for turn_index, user_input in enumerate(turns, 1):
            agent.config.max_steps = max_steps - steps
            if live:
                provider.turn_index = turn_index
                trace.record("turn_started", turn_index=turn_index, status="running")
            terminal, failure, call_number = self._consume_turn(agent, user_input, trace, turn_index, call_number)
            reason = final_output = None
            if terminal is None:
                break
            steps += terminal.steps
            if terminal.outcome == "interrupted":
                reason, failure = "interrupted", "LilBot interrupted the turn"
            else:
                # A capped turn also emits TurnFinished(outcome="completed").
                reason = next(
                    (row.get("outcome") for row in reversed(agent.events.read_all())
                     if row.get("event") == "lilbot_turn_finished" and row.get("turn") == turn_index),
                    None,
                )
                if agent.messages:
                    message = agent.messages[-1]
                    if message.get("role") == "assistant" and not message.get("tool_calls"):
                        final_output = message.get("content")
            if live:
                trace.record(
                    "turn_finished", turn_index=turn_index, status=reason,
                    final_output=final_output, steps=terminal.steps,
                )
            if reason != "completed" or steps >= max_steps:
                break

        if terminal is None and failure is None:
            failure = "LilBot ended without a TurnFinished event"
        if not live and reason == "completed" and provider.remaining:
            failure = "LilBot completed before consuming the whole mock script"
        trace.record(
            "runtime_finished", status="AGENT_FAILURE" if failure else "completed",
            final_output=final_output, termination_reason=reason,
            agent_outcome=terminal.outcome if terminal is not None else None,
            steps=steps if terminal is not None else None, usage=agent.usage or None,
            elapsed_ms=(perf_counter() - started) * 1000, detail=failure,
        )

    def _consume_turn(
        self, agent: Agent, user_input: str, trace: TraceWriter, turn_index: int, call_number: int,
    ) -> tuple[TurnFinished | None, str | None, int]:
        pending_tools: deque[tuple[str, int]] = deque()
        terminal = None
        failure = None
        events = agent.run_turn(user_input)
        try:
            while True:
                try:
                    event = next(events)
                except StopIteration:
                    break
                except (OSError, SQLiteError):
                    raise
                except Exception as exc:
                    failure = f"{type(exc).__name__}: {exc}"
                    break

                if isinstance(event, ToolStarted):
                    call_number += 1
                    pending_tools.append((event.name, call_number))
                    blocked = event.arguments if isinstance(event.arguments, BlockedArguments) else None
                    trace.record(
                        "tool_started", tool_name=blocked.name if blocked is not None else event.name,
                        tool_args=blocked.original if blocked is not None else event.arguments,
                        status="started", tool_seq=call_number, turn_index=turn_index,
                    )
                elif isinstance(event, ToolFinished):
                    # LilBot emits completed results in the same order as the starts.
                    tool_seq = None
                    if pending_tools and pending_tools[0][0] == event.name:
                        _, tool_seq = pending_tools.popleft()
                    guardrails = [item["kind"] for item in event.metadata.get("guardrails", [])]
                    trace.record(
                        "tool_finished", tool_name=event.name, tool_result=event.output,
                        status="ok" if event.ok else "failed", elapsed_ms=event.elapsed_ms, tool_seq=tool_seq,
                        turn_index=turn_index, guardrails=guardrails or None,
                        failure_kind=_tool_failure(event, guardrails),
                    )
                elif isinstance(event, ProviderRetry):
                    trace.record(
                        "provider_retry", status=event.action, reason=event.reason,
                        attempt=event.attempt, delay=event.delay, turn_index=turn_index,
                    )
                elif isinstance(event, TurnFinished):
                    terminal = event
        finally:
            events.close()
        return terminal, failure, call_number


def _memory_permission(args: dict[str, Any], ctx: ToolContext) -> ToolResult | None:
    if ctx.permissions.check("eval:memory_save", "write trial-local memory").allowed:
        return None
    return ToolResult(False, "Permission denied.", {"gate": "tool_permission"})


def _tool_failure(event: ToolFinished, guardrails: list[str]) -> str | None:
    if event.ok:
        return None
    if "repeated_failure_block" in guardrails:
        return "guardrail"
    if event.metadata.get("evaluation_failure"):
        return event.metadata["evaluation_failure"]
    if event.metadata.get("gate") == "tool_permission":
        return "permission"
    if event.metadata.get("validation_error"):
        return "validation"
    return "tool"
