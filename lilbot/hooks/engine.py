from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .models import Hook, HookAction, HookContext, HookResult


@dataclass
class HookNotification:
    hook_id: str
    event: str
    output: str
    success: bool


def _run_action(action: HookAction, ctx: HookContext, cwd: Path | None) -> HookResult:
    if action.type == "prompt":
        return HookResult(True, action.message)
    if action.type == "block":
        return HookResult(True, action.message or f"Blocked by hook for {ctx.tool_name}")
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
            return HookResult(proc.returncode == 0, out.strip()[:4000])
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

    def run_pre_tool(self, ctx: HookContext) -> str | None:
        """Run pre_tool_use hooks. Returns a rejection reason if one blocks."""
        rejection: str | None = None
        for hook in self._matching("pre_tool_use", ctx):
            hook.mark_executed()
            result = _run_action(hook.action, ctx, self.cwd)
            self._notifications.append(
                HookNotification(hook.id, "pre_tool_use", result.output, result.success)
            )
            blocks = hook.reject or hook.action.type == "block"
            # A command hook that exits non-zero also blocks (unix convention).
            if hook.action.type == "command" and not result.success:
                blocks = True
            if blocks and rejection is None:
                rejection = result.output or f"Blocked by hook '{hook.id}'"
        return rejection

    def drain_prompt_messages(self) -> list[str]:
        msgs = list(self._prompt_messages)
        self._prompt_messages.clear()
        return msgs

    def drain_notifications(self) -> list[HookNotification]:
        notes = list(self._notifications)
        self._notifications.clear()
        return notes
