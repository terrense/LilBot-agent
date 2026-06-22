# Changelog

All notable changes to LilBot are recorded here.
Format loosely follows [Keep a Changelog](https://keepachangelog.com/);
this project records dated entries per improvement batch so we can track
progress over time and in GitHub.

## [Unreleased] — 2026-06-22 — Engine upgrades ported from mewcode

A batch of runtime/"engine" improvements studied from the `mewcode-python`
reference agent and adapted to LilBot's synchronous, OpenAI-compatible
architecture. LilBot's existing strengths (full tool catalog, TUI dashboard,
PowerShell safety gate, teams/subagents) were preserved. Tests: 118 → 157,
zero regressions.

### Added
- **Deferred tool loading + `ToolSearch`** — only a ~33-tool core set is sent to
  the model each turn; the other ~112 tools are advertised by name and loaded on
  demand via `ToolSearch` (`select:<name>` or keyword search). Per-turn tool
  schema payload cut ~77%. Directly calling a deferred tool still works and
  auto-reveals it. (`lilbot/tools/registry.py`, `lilbot/tools/builtin.py`)
- **Large tool-result offload** — results over 16 KB are written to
  `.lilbot/session/tool-results/` with a 2 KB preview pointer instead of being
  truncated and lost. `retrieve_tool_result` / `handle_read` now actually read
  the persisted file back (offset/limit supported). (`lilbot/tools/offload.py`)
- **Two-layer auto-compaction + RecoveryState** — token-budget trigger,
  LLM-generated structured summary of the older prefix, a verbatim recent tail
  (never splitting a tool_calls/tool pair), a recovery attachment that
  re-injects recently read files / skill SOPs / the tool list, and a circuit
  breaker. Replaces the old 180-char string-join summary.
  (`lilbot/core/compaction.py`)
- **Prompt-cache usage reporting** — DeepSeek/OpenAI server-side prefix-cache
  hits are normalized to `cache_read_tokens` and shown in `/tokens` with a hit
  rate. Transient reminders are appended at the message tail to keep the cached
  prefix stable. (`lilbot/llm/providers.py`, `lilbot/cli.py`)
- **Lifecycle hooks engine** — user automation via `.lilbot/hooks.json` on
  `turn_start`/`pre_tool_use`/`post_tool_use`/`turn_end`; `pre_tool_use` can
  block a tool call, `prompt` actions inject guidance, `command` actions run a
  shell command and report output. (`lilbot/hooks/`)
- **Memory recall + auto-extraction** — a small side-query selects the memories
  relevant to the current request (with point-in-time freshness warnings)
  instead of dumping the newest few; every 3 turns the agent distills durable
  memories into the store (user/feedback → user scope, project/reference → project
  scope). Gated to capable providers. (`lilbot/memory/recall.py`,
  `lilbot/memory/extract.py`)
- **Parallel read-only tool execution** — runs of consecutive read-only tools
  execute concurrently via a thread pool, order preserved; write/execute tools
  stay sequential. (`lilbot/core/agent.py`, `READ_ONLY_TOOLS`)

### Changed
- `LilBotConfig` gains `context_window` (default 128 000, env
  `LILBOT_CONTEXT_WINDOW`) driving the compaction threshold.
- Subagent tool filtering now selects from the full catalog (`all_schemas()`),
  so deferral never hides a tool a subagent is allowed to use.

### Tests
- Added `test_deferred_tools`, `test_offload`, `test_compaction`,
  `test_cache_usage`, `test_hooks`, `test_memory_recall`, `test_parallel_tools`
  (39 new tests). Updated `test_compact_does_not_orphan_tool_messages` for the
  new compaction algorithm.

### Notes / not yet done
- Memory still uses the JSONL store (the intelligence layer was added on top);
  frontmatter-file storage with physical user/project dirs is a future option.
- Hooks: `session_start` / `session_end` events not yet wired into the loop.
- Compaction boundary is not yet persisted to disk for session resume.
