# Changelog

All notable changes to LilBot are recorded here.
Format loosely follows [Keep a Changelog](https://keepachangelog.com/);
this project records dated entries per improvement batch so we can track
progress over time and in GitHub.

## [Unreleased] — 2026-06-22 (batch 2) — Surpassing mewcode: persistence & depth

The four persistence/depth areas where mewcode still led are now closed.
Combined with batch 1, LilBot meets mewcode on engine intelligence and exceeds
it on breadth (tools, TUI, Windows safety, hooks hot-reload). Tests: 160 → 184.

### Added
- **Session persistence + resume** — conversation + usage are written to
  `.lilbot/sessions/<id>.json` after every turn (atomic temp-then-replace).
  Resume the latest or a specific session via `--resume[=id]`, `/sessions`
  (list), `/resume [id]`. (`lilbot/core/session.py`)
- **File-based memory store** — each memory is its own frontmatter `.md` file,
  routed by kind into user-level (`~/.lilbot/memory`: user/feedback) or
  project-level (`.lilbot/memory`: project/reference/note) dirs, each with a
  `MEMORY.md` index. Drop-in for `MemoryStore` (same API + `MemoryEntry`), so
  recall/extraction/tools are unchanged; legacy `memory.jsonl` is migrated on
  first run. (`lilbot/memory/file_store.py`)
- **File history + rewind** — files are snapshotted before
  write_file/edit_file/fim_edit; `/rewind [n]` undoes the last n edits (restores
  modified files, deletes newly created ones), `/history` lists recent edits.
  An undo for agent edits, independent of git. (`lilbot/core/history.py`)
- **Worktree depth** — `EnterWorktree` now auto-generates a readable branch slug
  and **symlinks heavy dependency dirs** (node_modules/.venv/vendor/…) from the
  main checkout into the new worktree (junction fallback on Windows) so it is
  usable without reinstalling. New `worktree_prune` removes stale worktrees.
  (`lilbot/tools/builtin.py`)

### Tests
- Added `test_session`, `test_file_memory`, `test_file_history`,
  `test_worktree_depth` (24 new tests).

## [Unreleased] — 2026-06-22 (batch 1) — Engine upgrades ported from mewcode

A batch of runtime/"engine" improvements studied from the `mewcode-python`
reference agent and adapted to LilBot's synchronous, OpenAI-compatible
architecture. LilBot's existing strengths (full tool catalog, TUI dashboard,
PowerShell safety gate, teams/subagents) were preserved. Tests: 118 → 160,
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
  shell command and report output. Hooks **hot-reload**: editing
  `.lilbot/hooks.json` takes effect on the next turn, no restart needed.
  A `match.tools` list lets one rule cover a whole family of tools (e.g.
  write_file + edit_file + fim_edit) so a guard cannot be sidestepped by a
  sibling tool. (`lilbot/hooks/`, `Agent._reload_hooks_if_changed`)
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

### Known limitations
- A `pre_tool_use` path-regex hook matches on the file path inferred from a
  tool's `path`/`file_path` argument. Tools that carry the target elsewhere
  (`apply_patch` embeds it in the diff; `bash` via shell redirection) cannot be
  matched by `path_regex` — block those by tool name if needed.
- Async streaming output and remote/cloud agents (mewcode `remote.py`) are
  intentionally not ported — large effort, niche value for a sync CLI.
