---
name: skill-installer
description: Install, update, trust, or inspect local/community skill folders.
mode: inline
---
# Skill Installer

Use this skill when inspecting or installing skills.

Safety checklist:

- Inspect `SKILL.md` before trusting a skill.
- List companion files and any executable scripts.
- Prefer project-local installs under `.lilbot/skills`.
- Do not run installer scripts without explicit permission.
- Record source, version, and trust decision.

User task: {{args}}
