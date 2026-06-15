---
name: delegate
description: Use subagents to split exploration, implementation, review, and verification work.
mode: inline
---
# Delegate

Use this skill when the task is large enough to benefit from parallel or staged
work.

Recommended pattern:

1. Open `explore` agents for independent evidence gathering.
2. Open `plan` when the implementation path is unclear.
3. Open `implementer` only after the desired change is specific.
4. Open `review` or `verifier` to check the result.
5. Merge child results by citing evidence, changes, risks, and blockers.

User task: {{args}}
