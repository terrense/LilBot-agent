from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import Hook, HookAction, HookContext, HookResult


@dataclass
class HookNotification:
    hook_id: str
    event: str
    output: str
    success: bool


@dataclass
class PreToolOutcome:
    """Structured result of running the pre_tool_use hooks for one tool call."""

    block: str | None = None                 # rejection reason, or None to allow
    updated_input: dict[str, Any] | None = None  # rewritten tool arguments


def _parse_structured(stdout: str) -> dict[str, Any]:
    """Parse a hook's stdout as the CC-style structured JSON protocol.

    Returns {} when stdout isn't a JSON object — so a plain-text hook (the common
    case) keeps working exactly as before. Only a top-level JSON object with
    recognized keys activates the structured behaviors.
    """
    text = (stdout or "").strip()
    if not text.startswith("{"):
        return {}
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _run_action(action: HookAction, ctx: HookContext, cwd: Path | None) -> HookResult:
    if action.type == "prompt":
        return HookResult(True, action.message)
    if action.type == "block":
        return HookResult(True, action.message or f"Blocked by hook for {ctx.tool_name}", decision="block")
    if action.type == "command":
        env = dict(os.environ)
        env.update({
            "LILBOT_HOOK_EVENT": ctx.event,
            "LILBOT_TOOL_NAME": ctx.tool_name,
            "LILBOT_FILE_PATH": ctx.file_path,
        })
        try:
            if os.name == "nt":
                argv = ["powershell", "-NoProfile", "-NonInteractive", "-Command", action.command]
                shell = False
            else:
                argv = action.command  # type: ignore[assignment]
                shell = True
            proc = subprocess.run(
                argv, shell=shell, cwd=str(cwd) if cwd else None, env=env,
                capture_output=True, text=True, timeout=max(1, action.timeout),
            )
            out = (proc.stdout or "") + (proc.stderr or "")
            result = HookResult(proc.returncode == 0, out.strip()[:4000])
            # Structured protocol (CC parity): JSON on stdout can rewrite the
            # tool input, block, inject context, or forbid stopping.
            data = _parse_structured(proc.stdout)
            if data:
                result.decision = str(data.get("decision") or "")
                ui = data.get("updatedInput", data.get("updated_input"))
                if isinstance(ui, dict):
                    result.updated_input = ui
                result.additional_context = str(
                    data.get("additionalContext", data.get("additional_context")) or ""
                )
                if "continue" in data:
                    result.continue_run = bool(data.get("continue"))
                result.system_message = str(
                    data.get("systemMessage", data.get("reason")) or ""
                )
            return result
        except subprocess.TimeoutExpired:
            return HookResult(False, f"hook command timed out after {action.timeout}s")
        except OSError as exc:
            return HookResult(False, f"hook command failed: {exc}")
    return HookResult(False, f"unknown action type: {action.type}")


class HookEngine:
    """Runs lifecycle hooks synchronously. Never raises into the agent loop."""

    def __init__(self, hooks: list[Hook] | None = None, cwd: Path | None = None) -> None:
        self.hooks: list[Hook] = hooks or []
        self.cwd = cwd
        self._prompt_messages: list[str] = []
        self._notifications: list[HookNotification] = []

    def has_hooks(self) -> bool:
        return bool(self.hooks)

    def _matching(self, event: str, ctx: HookContext) -> list[Hook]:
        out = []
        for hook in self.hooks:
            if hook.event != event or not hook.should_run():
                continue
            if not hook.match.matches(ctx):
                continue
            out.append(hook)
        return out

    def run(self, event: str, ctx: HookContext) -> None:
        """Run all non-blocking hooks for an event, collecting their output."""
        for hook in self._matching(event, ctx):
            hook.mark_executed()
            result = _run_action(hook.action, ctx, self.cwd)
            if hook.action.type == "prompt" and result.success and result.output:
                self._prompt_messages.append(result.output)
            self._notifications.append(
                HookNotification(hook.id, event, result.output, result.success)
            )

    def run_pre_tool(self, ctx: HookContext) -> PreToolOutcome:
        """Run pre_tool_use hooks; return block reason and/or rewritten input.

        CC-parity structured protocol: a command hook may print JSON to
        ``decision: "block"`` (reject), ``updatedInput`` (rewrite the tool's
        arguments before it runs), and ``additionalContext`` (inject guidance for
        the next model call). Plain-text hooks and exit codes keep their old
        meaning (non-zero exit blocks).
        """
        outcome = PreToolOutcome()
        for hook in self._matching("pre_tool_use", ctx):
            hook.mark_executed()
            result = _run_action(hook.action, ctx, self.cwd)
            self._notifications.append(
                HookNotification(hook.id, "pre_tool_use", result.output, result.success)
            )
            if result.additional_context:
                self._prompt_messages.append(result.additional_context)
            # Last writer wins for input rewriting, so a later hook can refine an
            # earlier one's edit.
            if result.updated_input is not None:
                outcome.updated_input = result.updated_input
            blocks = hook.reject or hook.action.type == "block" or result.decision == "block"
            # A command hook that exits non-zero also blocks (unix convention),
            # UNLESS it explicitly approved via structured decision.
            if hook.action.type == "command" and not result.success and result.decision != "approve":
                blocks = True
            if blocks and outcome.block is None:
                outcome.block = result.system_message or result.output or f"Blocked by hook '{hook.id}'"
        return outcome

    def run_stop(self, ctx: HookContext) -> str | None:
        """Run stop hooks; return a continuation instruction, or None to stop.

        CC parity (handleStopHooks): when the model is about to end the turn, a
        stop hook can force it to keep working. A hook forces continuation by
        blocking (``decision: "block"`` / ``continue: false`` / non-zero exit);
        its reason/output becomes an injected instruction. The caller caps how
        many times this can fire so a misbehaving hook can't spin forever
        (CC's death-spiral guard).
        """
        for hook in self._matching("stop", ctx):
            hook.mark_executed()
            result = _run_action(hook.action, ctx, self.cwd)
            self._notifications.append(
                HookNotification(hook.id, "stop", result.output, result.success)
            )
            forces = (
                hook.reject
                or hook.action.type == "block"
                or result.decision == "block"
                or result.continue_run is False
                or (hook.action.type == "command" and not result.success)
            )
            if forces:
                return result.system_message or result.output or f"Continue working (stop hook '{hook.id}')."
        return None

    def drain_prompt_messages(self) -> list[str]:
        msgs = list(self._prompt_messages)
        self._prompt_messages.clear()
        return msgs

    def drain_notifications(self) -> list[HookNotification]:
        notes = list(self._notifications)
        self._notifications.clear()
        return notes
