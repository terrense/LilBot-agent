---
name: feishu
description: Work with Feishu or Lark bots, docs, sheets, bitables, approval flows, and API setup.
mode: inline
---
# Feishu / Lark

Use this skill for Feishu or Lark integration work.

Rules:

- Never hardcode app secrets, tenant tokens, or user tokens.
- Keep credentials in environment variables or local ignored config.
- Separate OpenAPI auth, resource IDs, and business logic.
- Add dry-run or read-only modes before write operations.
- Document required scopes and callback URLs.

User task: {{args}}
