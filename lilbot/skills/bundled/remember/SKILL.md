---
name: remember
description: Decide whether user information should become durable memory.
allowed-tools: memory_save, memory_search, memory_list
when_to_use: Use when the user says to remember something or reveals stable preferences, project facts, or working agreements.
context: inline
---
Evaluate whether this should be saved as memory:

{{args}}

Save only durable, useful information:

- Stable user preferences.
- Project conventions.
- Reusable facts that will help future work.

Do not save secrets, transient tasks, private data that is not clearly useful,
or facts that may change frequently. If saving, choose a short name, concise
text, a kind such as `preference`, `project`, or `fact`, and the right scope.
