---
name: skillify
description: Turn a repeated workflow into a reusable LilBot SKILL.md.
aliases: skill-generator, make-skill
allowed-tools: read_file, list_dir, grep_files, write_file, edit_file, load_skill, skill_list
argument-hint: <workflow description>
when_to_use: Use when the user describes a reusable process, asks to create a skill, or repeats an agent workflow.
context: fork
agent: implementer
---
Create or improve a LilBot skill from this workflow:

{{args}}

Target format:

```markdown
---
name: short-kebab-name
description: One sentence describing the capability.
allowed-tools: read_file, grep_files
when_to_use: Use when ...
context: inline
---
Actionable instructions for the model.
```

Rules:

- Keep the skill specific enough to be useful.
- Include only tools that are actually needed.
- Use `context: fork` only for self-contained work that can run independently.
- Add companion files only when they reduce prompt size or improve accuracy.
