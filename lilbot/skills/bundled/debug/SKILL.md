---
name: debug
description: Investigate a bug or failing behavior and isolate the likely cause.
allowed-tools: read_file, list_dir, grep_files, file_search, git_status, git_diff, run_tests, agent_open, agent_eval
when_to_use: Use when behavior is broken, tests fail, errors are unclear, or the user asks for debugging help.
context: fork
agent: explore
---
Debug this issue:

{{args}}

Work like an investigator:

1. Reconstruct the symptom and expected behavior.
2. Find the narrowest relevant code path.
3. Gather evidence from tests, logs, diffs, and nearby code.
4. State the most likely root cause and confidence.
5. Recommend the smallest fix and validation.

Do not make broad edits from inside this skill unless explicitly asked.
