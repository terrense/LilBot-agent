"""Per-teammate progress tracker for the dashboard.

teams/progress.py. Thread-safe (the in-process teammate
loop writes from its own thread while the dashboard reads). Tool descriptions
use LilBot tool names. The ``_lock`` field is excluded from any serialization.
"""

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass, field
from typing import Optional


SPINNER_VERBS = [
    "Accomplishing", "Architecting", "Brewing", "Calculating", "Cascading",
    "Cerebrating", "Churning", "Cogitating", "Composing", "Computing",
    "Concocting", "Considering", "Contemplating", "Crafting", "Creating",
    "Crunching", "Crystallizing", "Deciphering", "Deliberating", "Elucidating",
    "Envisioning", "Forging", "Generating", "Harmonizing", "Hatching",
    "Ideating", "Imagining", "Improvising", "Incubating", "Inferring",
    "Manifesting", "Mulling", "Musing", "Noodling", "Orchestrating",
    "Percolating", "Pondering", "Puzzling", "Ruminating", "Simmering",
    "Sketching", "Spinning", "Synthesizing", "Thinking", "Tinkering",
    "Transmuting", "Unravelling", "Working", "Wrangling",
]


def random_verb() -> str:
    return random.choice(SPINNER_VERBS)


@dataclass
class ToolActivity:
    tool_name: str
    description: str

    @classmethod
    def from_tool_use(cls, tool_name: str, args: dict) -> "ToolActivity":
        return cls(tool_name=tool_name, description=_describe(tool_name, args))


def _describe(tool_name: str, args: dict) -> str:
    args = args or {}
    if tool_name in ("read_file", "handle_read"):
        return f"Reading {args.get('path') or args.get('file_path', '')}"
    if tool_name in ("edit_file", "apply_patch"):
        return f"Editing {args.get('path') or args.get('file_path', '')}"
    if tool_name == "write_file":
        return f"Writing {args.get('path') or args.get('file_path', '')}"
    if tool_name in ("bash", "exec_shell", "exec_shell_wait"):
        cmd = str(args.get("command", ""))
        return f"Running {cmd[:40]}{'…' if len(cmd) > 40 else ''}"
    if tool_name in ("glob", "file_search"):
        return f"Searching {args.get('pattern', '')}"
    if tool_name in ("grep", "grep_files"):
        return f"Grepping {args.get('pattern', '')}"
    if tool_name in ("web_search", "fetch_url", "web_fetch"):
        return f"Researching {args.get('query') or args.get('url', '')}"
    return tool_name


@dataclass
class TeammateProgress:
    name: str
    team_name: str
    status: str = "running"
    tool_use_count: int = 0
    token_count: int = 0
    last_activity: Optional[ToolActivity] = None
    recent_activities: list[ToolActivity] = field(default_factory=list)
    spinner_verb: str = field(default_factory=random_verb)
    start_time: float = field(default_factory=time.monotonic)
    last_message: Optional[str] = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record_tool_use(self, tool_name: str, args: dict) -> None:
        with self._lock:
            self.tool_use_count += 1
            act = ToolActivity.from_tool_use(tool_name, args)
            self.last_activity = act
            self.recent_activities.append(act)
            if len(self.recent_activities) > 5:
                self.recent_activities.pop(0)

    def record_tokens(self, input_tokens: int, output_tokens: int) -> None:
        with self._lock:
            self.token_count = input_tokens + output_tokens

    def set_message(self, text: str) -> None:
        with self._lock:
            self.last_message = text

    @property
    def activity_summary(self) -> str:
        with self._lock:
            if self.last_activity:
                return self.last_activity.description
            return self.spinner_verb

    @staticmethod
    def format_tokens(n: int) -> str:
        if n >= 1_000_000:
            return f"{n / 1_000_000:.1f}M"
        if n >= 1_000:
            return f"{n / 1_000:.1f}k"
        return str(n)
