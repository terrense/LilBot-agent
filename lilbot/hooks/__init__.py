"""Lifecycle hooks for LilBot.

Hooks let users attach automation to agent lifecycle events without modifying
code. They are defined in ``.lilbot/hooks.json`` and run synchronously:

  * ``pre_tool_use``  — can BLOCK a tool call (e.g. refuse writes to .env)
  * ``post_tool_use`` — fire-and-report after a tool runs (e.g. auto-format)
  * ``turn_start`` / ``turn_end`` — per user-turn automation
  * ``prompt`` actions inject a system reminder into the next model call
  * ``command`` actions run a shell command and report its output

The engine never raises into the agent loop; a misbehaving hook degrades to a
notification.
"""
from __future__ import annotations

from .engine import HookEngine, HookNotification, PreToolOutcome
from .loader import load_hooks
from .models import Hook, HookAction, HookContext, HookMatch, HookResult

__all__ = [
    "HookEngine",
    "HookNotification",
    "PreToolOutcome",
    "load_hooks",
    "Hook",
    "HookAction",
    "HookContext",
    "HookMatch",
    "HookResult",
]
