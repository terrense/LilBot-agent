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
    # OpenAI-style finish_reason ("stop" | "length" | "tool_calls" | …). "length"
    # means the model was cut off at max output tokens — the agent can inject a
    # "continue from where you left off" message and resume (CC parity, #9).
    finish_reason: str = ""


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

    Incremental deltas set ``text`` or ``reasoning``; a ``tool_call`` event fires
    when one tool call has fully streamed in (its args are complete), enabling
    the agent to start executing it while the model keeps streaming (CC's
    StreamingToolExecutor overlap, #2); the terminal event carries the
    fully-assembled ``final`` turn (content, tool_calls, usage).
    """
    text: str = ""
    reasoning: str = ""
    final: "ProviderTurn | None" = None
    tool_call: "ToolCall | None" = None


# 【简历·5 执行观测与评估闭环｜可观测事件流】
# 主循环(agent.py::run_turn)用 yield 把下面这些事件逐个抛给上层(TUI/持久化)。
# 它们就是简历里“Plan / Tool Call / Observation / Final Answer / 工具耗时 /
# Token 消耗”的结构化载体：
#   · ToolStarted   -> 记录一次工具调用的开始(名字+入参)  = Tool Call
#   · ToolFinished  -> 成功与否 ok、输出 output、耗时 elapsed_ms、元数据
#                      = Observation + 工具耗时 + 错误类型
#   · TurnFinished  -> 本回合总步数 steps 与累计 usage(Token 消耗)
# 子代理侧则把同类事件持久化成 JSONL 轨迹(subagents/manager.py::_append_transcript)，
# 形成可回放、可做 bad case 分析的执行日志链路。
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
    elapsed_ms: int  # 工具耗时（execute() 用 perf_counter 计得），用于观测与优化
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class TurnFinished:
    steps: int  # 本回合实际执行的工具步数
    usage: dict[str, int] = field(default_factory=dict)  # 累计 token 用量
