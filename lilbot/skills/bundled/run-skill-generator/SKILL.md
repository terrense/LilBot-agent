---
name: run-skill-generator
description: Generate a complete skill package from a short request.
aliases: skillgen
allowed-tools: skill_list, load_skill, write_file, edit_file, list_dir
when_to_use: Use when the user wants a ready-to-install skill directory rather than just guidance.
context: fork
agent: implementer
---
Generate a complete skill package for:

{{args}}

Create a directory containing `SKILL.md` and any useful companion files. Keep the
skill contract explicit: name, description, allowed tools, when_to_use, context,
and concrete instructions. Validate that the new skill loads through
`skill_list` or `load_skill`.
