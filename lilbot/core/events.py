from __future__ import annotations

from dataclasses import dataclass, field
from time import perf_counter
from typing import Any
from uuid import uuid4


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    call_id: str = field(default_factory=lambda: f"tool_{uuid4().hex[:10]}")


@dataclass
class ProviderTurn:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    reasoning_content: str = ""


@dataclass
class TextDelta:
    text: str
    interim: bool = False


@dataclass
class ToolStarted:
    name: str
    arguments: dict[str, Any]
    started_at: float = field(default_factory=perf_counter)


@dataclass
class ToolFinished:
    name: str
    ok: bool
    output: str
    elapsed_ms: int
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class TurnFinished:
    steps: int
    usage: dict[str, int] = field(default_factory=dict)
