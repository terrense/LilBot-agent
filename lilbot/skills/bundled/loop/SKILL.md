---
name: loop
description: Set up a recurring check or repeated agent workflow.
allowed-tools: automation_create, automation_list, automation_update, automation_delete, task_create, update_plan
when_to_use: Use when the user wants repeated monitoring, recurring work, or a loop until a condition is met.
context: inline
---
Design a recurring loop for:

{{args}}

Clarify the trigger, cadence, stop condition, output destination, and safety
gate. Use automation tools when the loop should persist. If persistence is not
available, create a tracked task plan instead.
