# Contributing to LilBot

Thanks for taking the time to improve LilBot. This document describes the
workflow, the quality bar, and the parts of the runtime that need care.

## What LilBot is

LilBot is a local-first coding-agent runtime. It is deliberately small: a
readable agent loop, a tool registry with explicit contracts, a permission gate,
durable context and memory, and a terminal interface. It is not a general agent
framework and does not try to hide control flow behind a plugin runtime.

Good contributions make one of these better:

- correctness of the agent loop or a failure path;
- safety of the permission, sandbox, or secret model;
- observability and reproducibility (traces, tests, benchmarks);
- portability across Windows, Linux, and macOS;
- documentation that lets a new maintainer debug the runtime without the author.

## Setup

```bash
git clone https://github.com/terrense/LilBot-agent.git
cd LilBot-agent
python -m venv .venv
```

Windows:

```powershell
.\.venv\Scripts\Activate.ps1
pip install -e .
```

macOS / Linux:

```bash
source .venv/bin/activate
pip install -e .
```

Copy `.env.example` to `.env` only if you need a live model. Most tests use the
offline provider and need no key.

## Tests and checks

```bash
python -m pytest tests -q
python -m compileall lilbot
```

Optional lint:

```bash
pip install ruff
ruff check lilbot tests
```

Every behavior change needs a test that fails before the change and passes
after it. A bug fix that only edits code without a regression test will usually
be asked to add one.

## Code style

- Python 3.10+. Use type hints on public functions and dataclasses.
- Keep control flow explicit. Prefer a named function and a comment that explains
  *why* over a clever expression.
- Comments should describe the behavior and the failure it prevents. Do not
  describe where the idea came from.
- Do not add a new dependency unless the standard library and the existing three
  runtime dependencies (`httpx`, `prompt_toolkit`, `rich`) cannot do the job.
- Keep Windows behavior working. If you touch shell execution, paths, encoding,
  clipboard, or console handling, run the tests on Windows or add a test that
  simulates the Windows branch.
- New tools belong in the tool registry with a complete contract: JSON schema,
  validation, approval requirement, capability set, concurrency safety, and a
  result size limit. Never let a tool handler perform its own approval check.

## Architecture boundaries

| Layer | Responsibility |
|---|---|
| `lilbot/core` | Agent loop, context, sessions, events, guardrails |
| `lilbot/llm` | Provider transport, retry, error classification |
| `lilbot/tools` | Tool contracts, registry, built-ins |
| `lilbot/sandbox` | Workspace paths, permissions, shell policy |
| `lilbot/memory` | File memory, recall, extraction |
| `lilbot/subagents`, `lilbot/teams` | Delegation and coordination |
| `lilbot/tui`, `lilbot/cli` | Presentation and entrypoints |

Do not put new policy in the TUI. The TUI renders events; the runtime decides.
Do not put new approval logic in a tool handler. The registry decides.

## Pull requests

1. Open an issue first for anything larger than a bug fix or a small docs change.
2. Keep a pull request to one logical change.
3. Describe the observed behavior, the change, and the evidence (test output,
   trace, screenshot, or benchmark).
4. Update `CHANGELOG.md` under `Unreleased` for user-visible changes.
5. Update the relevant page under `docs/` when behavior, configuration, or a
   public contract changes.

Small, focused pull requests are reviewed faster than large ones. If a change
touches the agent loop, permissions, or session persistence, expect a request for
a failure-path test.

## Reporting security issues

Do not open a public issue for a vulnerability. See [SECURITY.md](SECURITY.md).

## License

By contributing, you agree that your contribution is licensed under the MIT
License in [LICENSE](LICENSE).
