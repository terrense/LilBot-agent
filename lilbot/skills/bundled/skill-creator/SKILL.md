---
name: skill-creator
description: Create or improve LilBot skills and decide when a skill is the right abstraction.
mode: inline
---
# Skill Creator

Use this skill when adding reusable agent behavior.

Skill design rules:

- A skill is guidance plus lightweight assets, not executable business logic.
- Use a tool when structured execution is required.
- Use MCP when an external service has multiple tools/resources.
- Keep frontmatter complete: name, description, and mode.
- Include trigger guidance, workflow, verification, and failure handling.

User task: {{args}}
