---
name: plugin-creator
description: Scaffold or plan LilBot/CodeWhale-style local plugins.
mode: inline
---
# Plugin Creator

Use this skill when the user wants a plugin or external tool wrapper.

Plugin contract:

- One narrow tool per script.
- JSON input on stdin and JSON result on stdout.
- Frontmatter or manifest includes name, description, schema, and approval.
- Fail closed on invalid input.
- Include a smoke test command and an example call.

User task: {{args}}
