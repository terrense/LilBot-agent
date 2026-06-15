---
name: mcp-builder
description: Design, build, configure, or debug Model Context Protocol servers.
mode: inline
---
# MCP Builder

Use this skill when creating or debugging MCP-style tool servers.

Checklist:

- Define tools, resources, and prompts separately.
- Keep schemas strict and small.
- Make startup errors visible.
- Support a simple local smoke test.
- Avoid leaking secrets through tool output.
- Prefer stdio or JSON-RPC-lines first, then add HTTP/SSE only when needed.

User task: {{args}}
