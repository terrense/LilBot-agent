# Changelog

All notable changes to LilBot are recorded here.
Format loosely follows [Keep a Changelog](https://keepachangelog.com/);
this project records dated entries per improvement batch so we can track
progress over time and in GitHub.

## [Unreleased] — 2026-06-23 (batch 3) — Surpassing CodeWhale

Studying the CodeWhale (Rust) agent and closing the depth gaps that mattered,
adapted to LilBot's Python architecture. Roadmap M1–M6; this section is updated
per milestone. Tests start at 184.

### M1 — Secret redaction (added)
- New `lilbot/security/secrets.py`: masks API keys (`sk-…`, GitHub `ghp_…`,
  AWS `AKIA…`, Google `AIza…`, Slack, JWT, Bearer), private-key blocks, and
  secret-looking `KEY=value` assignments, **before anything reaches the screen /
  trace / logs**. Applied at the TUI presentation layer (classic + dashboard);
  the model's own context keeps raw values so functionality is unaffected.
- Directly fixes the incident where a DeepSeek API key was printed into the
  visible trace and a `.env` diff. False-positive guards: numeric config values
  (e.g. `MAX_TOKENS=128000`) and `AUTHOR=` are left untouched.
- Tests: `test_secrets` (10). Suite 184 → 193.

### M2 — Auto diagnostics injection after edits (added)
- After `write_file`/`edit_file`/`fim_edit` on a code file, the agent runs the
  diagnostics tool (LSP where available, Python-syntax fallback) on the edited
  files and injects any errors/warnings as a one-shot system reminder for the
  next LLM call — closing CodeWhale's self-correction loop. Gated by file
  extension, capped at 5 files/turn, toggle via `config.auto_diagnostics`.
- Tests: `test_auto_diagnostics` (6). Suite 193 → 200.

### M3 — Command-safety engine (added)
- New `lilbot/sandbox/execpolicy.py` (port of CodeWhale's execpolicy): hard-deny
  catastrophic shell commands (`rm -rf /`/`~`/`*`/`.`, fork bombs, `mkfs`, `dd`
  to a device, `curl|sh`, `shutdown`, …) and arity-aware **auto-allow** of known
  read-only commands (`git status -s`, `ls -la`, `cat`, … — flags ignored) so
  safe inspection skips approval prompts. Normal sub-dir deletes are NOT denied;
  compound commands are never auto-allowed. Wired into `_shell_permission`;
  toggle via `config.auto_allow_safe_commands`.
- Tests: `test_execpolicy` (35). Suite 200 → 235.

## [Unreleased] — 2026-06-22 (batch 2) — Surpassing mewcode: persistence & depth

The four persistence/depth areas where mewcode still led are now closed.
Tests: 160 → 184.

### Added
- **Session persistence + resume** — conversation + usage written to
  `.lilbot/sessions/<id>.json` each turn; `--resume[=id]`, `/sessions`, `/resume`.
- **File-based memory store** — frontmatter `.md` per memory, user/project dirs,
  `MEMORY.md` index; drop-in for `MemoryStore`; migrates legacy `memory.jsonl`.
- **File history + rewind** — snapshot before write/edit/fim; `/rewind [n]`,
  `/history`.
- **Worktree depth** — auto branch slug + symlink heavy dep dirs into new
  worktrees (junction fallback on Windows); `worktree_prune`.

## [Unreleased] — 2026-06-22 (batch 1) — Engine upgrades ported from mewcode

Runtime/"engine" improvements adapted to LilBot's synchronous, OpenAI-compatible
architecture. Existing strengths (full tool catalog, TUI, PowerShell safety,
teams/subagents) preserved. Tests: 118 → 160.

### Added
- **Deferred tool loading + `ToolSearch`** — per-turn tool schema 145 → 33.
- **Large tool-result offload** — disk + 2 KB preview instead of truncation.
- **Two-layer auto-compaction + RecoveryState** — structured summary, kept tail,
  recovery attachment, circuit breaker.
- **Prompt-cache usage reporting** — normalize DeepSeek/OpenAI cache hits.
- **Lifecycle hooks engine** — `.lilbot/hooks.json`, pre_tool_use can block,
  hot-reload, `match.tools` family rules.
- **Memory recall + auto-extraction** — LLM relevance recall with freshness;
  periodic extraction.
- **Parallel read-only tool execution** — thread pool, order preserved.

### Changed
- `LilBotConfig.context_window` (default 128 000) drives compaction threshold.
- Subagent tool filtering uses the full catalog (`all_schemas()`).

### Known limitations
- Hook path-regex matches on inferred `path`/`file_path`; `apply_patch`/`bash`
  carry the target elsewhere — block by tool name if needed.
- Async streaming and remote/cloud agents intentionally not ported.
