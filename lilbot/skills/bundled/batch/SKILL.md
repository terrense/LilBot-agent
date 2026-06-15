---
name: batch
description: Break a large request into a tracked batch of smaller tasks.
allowed-tools: update_plan, checklist_write, checklist_update, checklist_add, task_create, task_list, agent_open, agent_eval, agent_close
when_to_use: Use for multi-part work, migrations, audits, large refactors, or anything that needs parallel exploration.
context: inline
---
Convert the request into a managed batch:

{{args}}

Create a short execution plan, group related work, identify dependencies, and
decide what can run in parallel. Use subagents for independent investigation or
verification. Keep each task small enough to review and test.

Output:

PLAN: ordered batch list.
PARALLEL: items safe to run concurrently.
GATES: validation required before proceeding.
RISKS: the main ways the batch can go wrong.
