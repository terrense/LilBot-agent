# Changelog

All notable changes to LilBot are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- One permission pipeline with explicit `deny > ask > tool > mode > allow`
  ordering. Explicit ask rules prompt even under accept-all, and deny-all is a
  hard override.
- Inline skills narrow the advertised and executable tool set to their declared
  `allowed_tools`; forked skills pass the same allowlist to the subagent.
- A 40-case dangerous-command corpus and a 12-case path/symlink/junction
  boundary corpus in the test suite.

### Changed

- Test suite: 733 passed, 7 skipped.

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
