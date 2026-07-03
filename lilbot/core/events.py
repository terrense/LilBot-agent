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
    # True when this is one incremental chunk of a live model stream (rendered
    # inline/growing rather than as a finished block). Additive: defaults False
    # so every existing producer/consumer keeps its current behavior.
    streaming: bool = False


@dataclass
class StreamEvent:
    """One event from a provider's streaming completion.

    Incremental deltas set ``text`` or ``reasoning``; the terminal event carries
    the fully-assembled ``final`` turn (content, tool_calls, usage).
    """
    text: str = ""
    reasoning: str = ""
    final: "ProviderTurn | None" = None


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
