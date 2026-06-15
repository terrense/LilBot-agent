---
name: update-config
description: Help inspect and update LilBot project or user configuration.
allowed-tools: read_file, list_dir, grep_files, write_file, edit_file, diagnostics
when_to_use: Use when the user wants to change configuration, defaults, model settings, permissions, or local agent behavior.
context: inline
---
Inspect and update configuration for this request:

{{args}}

Find the relevant config file before editing. Preserve unrelated settings.
Explain the exact setting changed and how to verify it. If the requested setting
does not exist, propose the smallest compatible addition.
