---
name: schedule
description: Schedule a future task, reminder, or remote-style agent action.
allowed-tools: automation_create, automation_list, automation_update, automation_delete, request_user_input
when_to_use: Use when the user asks to schedule, remind, monitor later, or trigger future agent work.
context: inline
---
Schedule this future action:

{{args}}

Extract the exact time or recurrence, timezone if present, action prompt, and
success condition. If timing is ambiguous, ask one concise clarification. Store
the schedule through automation tools when possible.
