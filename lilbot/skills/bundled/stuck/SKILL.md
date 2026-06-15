---
name: stuck
description: Recover when progress stalls or the agent may be looping.
allowed-tools: read_file, list_dir, grep_files, git_status, git_diff, run_tests, update_plan, agent_open, agent_eval
when_to_use: Use when work is blocked, repeated attempts failed, or the next step is unclear.
context: fork
agent: review
---
Unstick this work:

{{args}}

Audit the current state:

1. What is actually known?
2. What assumption might be false?
3. What evidence is missing?
4. What is the smallest reversible next step?
5. Should a specialist subagent inspect a narrower question?

Return a short recovery plan with concrete commands/files to inspect next.
