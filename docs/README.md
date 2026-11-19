# LilBot documentation

The README is the front door; this page is the map. If you are new, read the
[project README](../README.md) first, then the architecture overview.

## Getting started

- [Project README](../README.md) — install, configure, run, commands.
- [Windows guide](guides/windows.md) — PowerShell, UTF-8, fonts, clipboard, and
  common Windows failure modes.
- [Provider guide](guides/providers.md) — cloud, local, and offline providers,
  model switching, retries, and fallbacks.

## Architecture

- [Architecture overview](architecture/overview.md) — layers, design decisions,
  and the audit findings that shaped the runtime.
- [Runtime call chain](architecture/runtime-call-chain.md) — entrypoint to model
  turn to tool execution to persistence, with the exact functions involved.
- [Module inventory](architecture/module-inventory.md) — every package and file,
  its responsibilities, and its health.

## Design

- [Context management](design/context-management.md) — tool-result budget,
  pruning, structured summaries, recovery, and cycles.
- [Teams explained](design/teams-explained.md) — mailbox, shared task board, and
  the in-process teammate model.
- [Teams specification](design/teams-spec.md) — the original design contract and
  open questions.
- [Subagents, skills, and tools](design/subagents-skills-tools.md) — the 11
  built-in roles, 28 bundled skills, 146 tools, hooks, MCP, and the safety model.

## Evaluation

- [Evaluation design](evaluation/design.md) — the isolated mock and live harness,
  trace schema, grading, and metrics.
- [Live baseline](evaluation/live-baseline.md) — the 2026-11-04 live run.
- [Baseline test report](evaluation/baseline-test-report.md) — the regression
  baseline and the bugs it surfaced.

## Engineering reports

- [M1-M8 implementation](reports/m1-m8-implementation.md) — secret redaction,
  post-edit diagnostics, command policy, cycle memory, tool catalog caching, and
  bidirectional MCP.
- [M1-M8 verification](reports/m1-m8-verification.md) — how each mechanism was
  verified.

## Assets

- [Asset notes](assets/README.md) — the runtime screenshots and how to reproduce
  them.
