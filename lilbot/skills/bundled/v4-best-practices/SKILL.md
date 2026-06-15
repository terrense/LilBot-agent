---
name: v4-best-practices
description: Use for DeepSeek V4-style multi-step or plan-driven tasks.
mode: inline
---
# V4 Best Practices

Use this skill on multi-step work with DeepSeek V4 models.

Rules:

- Verify current facts and local file state before committing to a plan.
- Keep plans short, concrete, and status-tracked.
- Avoid stale references after file edits; re-read the area you changed.
- Prefer dedicated tools over shell when available.
- End with evidence, changed files, tests, risks, and blockers.

User task: {{args}}
