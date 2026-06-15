---
name: keybindings-help
description: Explain or design keyboard shortcut bindings.
allowed-tools: read_file, list_dir, grep_files
when_to_use: Use when the user asks about keyboard shortcuts or terminal keybinding behavior.
user-invocable: false
context: inline
---
Help with keybindings:

{{args}}

Check existing configuration when available. Prefer familiar shortcuts, avoid
conflicts, and note terminal limitations for modifier keys. Return a concise
mapping table and any config changes needed.
