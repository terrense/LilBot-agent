---
name: verify
description: Verify that implementation work is correct before reporting done.
allowed-tools: read_file, list_dir, grep_files, git_status, git_diff, run_tests, agent_open, agent_eval
when_to_use: Use after non-trivial implementation, backend/API changes, infrastructure changes, or before claiming completion.
context: fork
agent: verifier
---
Verify the work described by the user or by these arguments:

{{args}}

Produce a compact verdict:

STATUS: PASS, FAIL, or PARTIAL.
EVIDENCE: commands run, files inspected, and exact outcomes.
RISKS: unresolved edge cases or missing coverage.
NEXT: smallest next action if not PASS.

Prefer concrete validation over reassurance. If tests cannot be run, explain why
and substitute targeted code inspection with file references.
