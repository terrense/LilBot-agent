---
name: simplify
description: Simplify code, prose, or a plan without changing its intent.
allowed-tools: read_file, grep_files, edit_file, run_tests
when_to_use: Use when the user asks to simplify, reduce complexity, clarify, or make something easier to maintain.
context: inline
---
Simplify the target while preserving behavior and intent:

{{args}}

Prefer removing accidental complexity, duplicated logic, unclear naming, and
unnecessary branching. Keep changes small and validate if code changes are made.

Report what became simpler and what validation was performed.
