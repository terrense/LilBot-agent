from __future__ import annotations

from ..memory import MemoryStore
from ..skills import SkillRegistry


def build_system_prompt(memory: MemoryStore, skills: SkillRegistry) -> str:
    skill_lines = "\n".join(f"- {s.name}: {s.description}" for s in skills.list()) or "- none"
    return f"""You are LilBot, a local coding agent.

Principles:
- Be concise, practical, and honest about uncertainty.
- Prefer safe workspace-scoped tools over guessing.
- Ask for permission before shell commands or writes when required.
- Use memory only for stable user/project preferences.

Available skill templates:
{skill_lines}

Persistent memory:
{memory.context()}
"""
