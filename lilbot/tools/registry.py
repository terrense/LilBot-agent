from __future__ import annotations

from dataclasses import dataclass, field
from time import perf_counter
from typing import Any, Callable

from ..sandbox import SandboxError


@dataclass
class ToolResult:
    ok: bool
    output: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolDef:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[[dict[str, Any], "ToolContext"], ToolResult]


@dataclass
class ToolContext:
    sandbox: Any
    permissions: Any
    memory: Any
    skills: Any
    subagents: Any
    mcp: Any
    config: Any


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, ToolDef] = {}

    def register(self, tool: ToolDef) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> ToolDef | None:
        return self._tools.get(name)

    def list(self) -> list[ToolDef]:
        return sorted(self._tools.values(), key=lambda tool: tool.name)

    def schemas(self) -> list[dict[str, Any]]:
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.input_schema,
            }
            for tool in self.list()
        ]

    def execute(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> tuple[ToolResult, int]:
        tool = self.get(name)
        if not tool:
            return ToolResult(False, f"Unknown tool: {name}"), 0
        started = perf_counter()
        try:
            result = tool.handler(arguments or {}, ctx)
        except SandboxError as exc:
            result = ToolResult(False, f"Sandbox error: {exc}")
        except Exception as exc:  # pragma: no cover - defensive boundary
            result = ToolResult(False, f"Tool error: {type(exc).__name__}: {exc}")
        elapsed_ms = int((perf_counter() - started) * 1000)
        if len(result.output) > 12000:
            result.output = result.output[:12000] + "\n... truncated ..."
            result.metadata["truncated"] = True
        return result, elapsed_ms

