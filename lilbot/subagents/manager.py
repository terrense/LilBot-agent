from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable
from uuid import uuid4

from ..core.events import ProviderTurn


ProviderCallable = Callable[[list[dict], list[dict]], ProviderTurn]


@dataclass
class AgentDefinition:
    name: str
    description: str
    system_hint: str


@dataclass
class SubAgentTask:
    id: str
    agent_type: str
    prompt: str
    status: str = "queued"
    result: str = ""
    error: str = ""
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None


DEFAULT_AGENT_TYPES = [
    AgentDefinition("coder", "Implements focused code changes.", "You are a careful coding sub-agent."),
    AgentDefinition("reviewer", "Reviews code for bugs and missing tests.", "You are a skeptical code reviewer."),
    AgentDefinition("researcher", "Collects facts from local files and summarizes them.", "You are a precise researcher."),
    AgentDefinition("planner", "Breaks large work into steps and risks.", "You are a pragmatic technical planner."),
]


class SubAgentManager:
    def __init__(self, provider: ProviderCallable):
        self.provider = provider
        self.definitions = {d.name: d for d in DEFAULT_AGENT_TYPES}
        self.tasks: dict[str, SubAgentTask] = {}

    def list_types(self) -> list[AgentDefinition]:
        return sorted(self.definitions.values(), key=lambda item: item.name)

    def list_tasks(self) -> list[SubAgentTask]:
        return sorted(self.tasks.values(), key=lambda item: item.created_at, reverse=True)

    def get(self, task_id: str) -> SubAgentTask | None:
        return self.tasks.get(task_id)

    def spawn(self, agent_type: str, prompt: str, background: bool = False) -> SubAgentTask:
        if agent_type not in self.definitions:
            agent_type = "planner"
        task = SubAgentTask(
            id=f"sub_{uuid4().hex[:10]}",
            agent_type=agent_type,
            prompt=prompt,
        )
        self.tasks[task.id] = task
        if background:
            thread = threading.Thread(target=self._run, args=(task,), daemon=True)
            thread.start()
        else:
            self._run(task)
        return task

    def _run(self, task: SubAgentTask) -> None:
        definition = self.definitions[task.agent_type]
        task.status = "running"
        messages = [
            {"role": "system", "content": definition.system_hint},
            {"role": "user", "content": task.prompt},
        ]
        try:
            turn = self.provider(messages, [])
            task.result = turn.content.strip() or "(subagent returned no text)"
            task.status = "done"
        except Exception as exc:  # pragma: no cover - defensive boundary
            task.error = str(exc)
            task.status = "error"
        finally:
            task.finished_at = time.time()
