from __future__ import annotations

from ..memory import MemoryStore
from ..skills import SkillRegistry
from .constitution import build_constitution


def build_system_prompt(memory: MemoryStore, skills: SkillRegistry) -> str:
    """Build the full system prompt: Constitution + Skills + Memory.

    The Constitution (transplanted from CodeWhale) provides the tiered
    rule hierarchy that drives planning, parallelization, and delegation.
    Skills and memory are appended as supporting context.
    """
    constitution = build_constitution()

    skill_lines = "\n".join(
        f"- {s.name}: {s.description}" + (f" Use when: {s.when_to_use}" if s.when_to_use else "")
        for s in skills.list()
    ) or "- none"

    return f"""{constitution}

## Available Skills

{skill_lines}

## Persistent Memory

{memory.context()}
"""
