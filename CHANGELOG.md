# Changelog

All notable changes to LilBot are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- A local USD price table (`lilbot/core/model_cost.py`) with per-model input,
  cache-read, and output prices. `/tokens` shows session and last-turn cost,
  the budget, and the price source; the dashboard status strip shows the same
  total.
- `--budget-usd` / `LILBOT_BUDGET_USD`: warn at 80% and stop before the next
  provider request at 100%. `--resume` restores the accumulated total.
- Exact tool-call identity: id-less streaming and non-streaming calls receive a
  unique id, and a subagent step limit only serializes the calls that actually
  execute.

- A per-model context-window table and resolver (`lilbot/core/model_windows.py`).
  The window is derived from the model, so a 64k endpoint no longer gets the
  128k default budget, and the compaction trigger and the context bar now use
  the same number. An explicit `LILBOT_CONTEXT_WINDOW` always wins.
- The context budget is now checked before every provider call, not only once
  per turn, so a long turn can no longer cross the window between iterations.
- The tool schemas are counted in the request estimate. They travel with every
  request but are not part of the message list, and with 146 registered tools
  they are a large fixed cost.
- `AGENTS.md` — the working agreement for anyone changing the repository.
- `docs/development-log.md` — what changed in each milestone, why, and what it
  means for the runtime.

- One permission pipeline with explicit `deny > ask > tool > mode > allow`
  ordering. Explicit ask rules prompt even under accept-all, and deny-all is a
  hard override.
- Inline skills narrow the advertised and executable tool set to their declared
  `allowed_tools`; forked skills pass the same allowlist to the subagent.
- A 40-case dangerous-command corpus and a 12-case path/symlink/junction
  boundary corpus in the test suite.

### Changed

- Test suite: 819 passed, 7 skipped.

### Fixed

- Provider failures are classified and recovered in order (compact, retry,
  fallback) instead of surfacing as an immediate error. A recoverable failure
  after partial text keeps the text but marks it incomplete and never persists
  it as a final answer.
- Exhausted output truncation now ends with an explicit `output_truncated`
  outcome instead of presenting the cut-off text as a completed answer.
- SSE error frames are classified as server failures so the retry/fallback
  chain can handle them.
- A failed compaction falls back to the fallback model when available, and
  otherwise raises a structured error with the partial text preserved.

## [0.2.0] - 2026-11-19

### Added

- Bilingual README (English and Simplified Chinese) with real runtime captures.
- MIT license, contributor guide, security policy, code of conduct, and AUTHORS.
- CI matrix for Python 3.10-3.13 on Ubuntu and Windows.
- Issue forms, pull request template, and CODEOWNERS.
- Documentation index with architecture, design, evaluation, and engineering
  report sections.
- `examples/` with runnable delegation, teams, and benchmark demos.
- `.env.example` with cloud, local, and offline provider recipes.

### Changed

- Reorganized the repository into a standard Python project layout: `lilbot/`,
  `tests/`, `evaluation/`, `benchmarks/`, `docs/`, `examples/`, `.github/`.
- Moved long-form design and evaluation documents under `docs/` with stable
  names and relative links.
- Expanded `pyproject.toml` metadata, dev dependencies, pytest configuration,
  package data, and project URLs.
- Rewrote the changelog in Keep a Changelog format.

### Fixed

- The offline provider now routes on the latest user turn and returns the tool
  observation instead of repeating the same tool call until the step budget.
- `glob("**")` now matches files and directories, consistent with
  `Path.rglob("**")`; only an explicit trailing slash selects directories.
- Local OpenAI-compatible providers (Ollama, vLLM, LM Studio, LocalAI) no longer
  require a placeholder API key or send an empty bearer token.
- MCP startup no longer blocks for the full connect timeout when a configured
  example server cannot be reached.
- The read-only shell allowlist is flag- and subcommand-aware: `find -exec` /
  `find -delete`, `git branch -D`, `git remote add`, and `git diff/log/show
  --output` now reach the approval prompt instead of being auto-allowed.
- SSE streams that carry an `error` object raise a classified `ProviderError`
  instead of producing an empty successful turn.
- Packaging metadata is PEP 621-valid, and the built wheel ships all 28 bundled
  skill files.

### Removed

- Scratch files and placeholder images from the repository root and `docs/assets`.

## [0.1.0] - 2026-06-17

- First public release: event-driven agent loop, OpenAI-compatible providers,
  tool registry, workspace sandbox, permission manager, durable memory, markdown
  skills, subagents, MCP-style adapters, and a Windows-first terminal UI.
