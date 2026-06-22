from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import VALID_ACTIONS, VALID_EVENTS, Hook, HookAction, HookMatch


def _parse_hook(raw: dict[str, Any], index: int) -> Hook | None:
    event = str(raw.get("event") or "").strip()
    if event not in VALID_EVENTS:
        return None
    action_raw = raw.get("action")
    if not isinstance(action_raw, dict):
        return None
    atype = str(action_raw.get("type") or "").strip()
    if atype not in VALID_ACTIONS:
        return None
    action = HookAction(
        type=atype,
        command=str(action_raw.get("command") or ""),
        message=str(action_raw.get("message") or ""),
        timeout=int(action_raw.get("timeout") or 15),
    )
    match_raw = raw.get("match") or {}
    match = HookMatch(
        tool=str(match_raw.get("tool") or ""),
        path_regex=str(match_raw.get("path_regex") or ""),
    )
    return Hook(
        id=str(raw.get("id") or f"hook_{index}"),
        event=event,
        action=action,
        match=match,
        reject=bool(raw.get("reject", False)),
        run_once=bool(raw.get("run_once", False)),
    )


def load_hooks(state_dir: Path | None) -> list[Hook]:
    """Load hooks from ``<state_dir>/hooks.json``.

    The file shape is ``{"hooks": [ {id, event, match, action, reject} ... ]}``.
    Malformed entries are skipped silently — hooks are opt-in convenience, never
    a hard dependency of the agent.
    """
    if state_dir is None:
        return []
    path = Path(state_dir) / "hooks.json"
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    raw_hooks = data.get("hooks") if isinstance(data, dict) else data
    if not isinstance(raw_hooks, list):
        return []
    hooks: list[Hook] = []
    for i, raw in enumerate(raw_hooks):
        if isinstance(raw, dict):
            hook = _parse_hook(raw, i)
            if hook is not None:
                hooks.append(hook)
    return hooks
