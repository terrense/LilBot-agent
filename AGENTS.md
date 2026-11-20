# AGENTS.md — LilBot working agreement

This file is the standing contract for anyone (human or AI assistant) changing
this repository. Read it before writing code, and keep it current: when the
agreement changes, this file changes in the same pull request.

It is short on purpose. The detailed design lives in `docs/`, and the history of
what changed and why lives in `docs/development-log.md`.

---

## 1. What this project is

LilBot is a **local-first coding agent runtime for the terminal**. It gives a
model a workspace, a shell, a permission gate, and durable memory, then makes
every failure path explicit and observable.

Two-person project: ShenXin (owner, ships to `main`) and Nayang (contributes
through pull requests). Assistants work on behalf of one of them.

## 2. Non-negotiables

These are not preferences. Breaking one of them is a defect.

1. **No AI attribution anywhere.** No `Co-Authored-By` trailers, no "generated
   with" lines, no AI mentions in commit messages, pull requests, code comments,
   docs, or release notes. The project's history must read as one person's work.
2. **No attribution of another agent project.** Do not name other coding-agent
   runtimes, and never describe a feature by where it came from ("ported from",
   "inspired by", "mirrors X"). Describe what a feature *does*. Reference
   material used for design comparison is private material: it must never be
   committed, pushed, pasted into an issue or pull request, or published.

   *Model and provider names are not attribution.* The runtime is
   model-agnostic and has to talk to real services, so the provider presets
   and the context-window table name the models and endpoints they support
   (DeepSeek, OpenAI, Qwen, GLM, Moonshot, and the rest). That is an
   interoperability fact, not a claim of origin. What must not appear is an
   agent runtime's name, or a phrase implying one of them is the source of this
   code.
3. **No secrets in the repository.** API keys, tokens, and passwords never enter
   source, tests, fixtures, screenshots, or docs. Build secret-shaped test
   fixtures from fragments at runtime instead of writing a literal.
4. **Never change repository visibility.** Switching public/private permanently
   destroys stars and watchers, and the platform cannot restore them.
5. **No personal paths or session identifiers in committed files or screenshots.**

## 3. Keep the public surface current — this is not optional

A change is not finished until the docs say what changed. Per pull request:

| Artifact | When to update it |
|---|---|
| `README.md` and `README.zh-CN.md` | Any user-visible behavior, command, flag, configuration key, or number |
| `CHANGELOG.md` | Every user-visible change, under `Unreleased` |
| `docs/development-log.md` | Every milestone: what changed, why, and what it means for the architecture |
| `docs/architecture/*.md` | When a module, call path, or module inventory row changes |
| `docs/design/*.md` | When a mechanism's design changes |

The two READMEs must stay in sync: the same section order, the same numbers,
the same links. A badge that says "660 passing" while the suite has 734 is a
defect, not a typo.

Write docs in concrete language. Name the function, the file, the command. A
sentence that could describe any project describes none.

## 4. Git protocol

- ShenXin commits directly to `main`.
- Nayang works on a short-lived branch cut from the latest `main`
  (`feat/<topic>`) and opens a pull request. Never build on a stale long-lived
  branch.
- One logical change per pull request. Do not mix a refactor and a behavior
  change in the same commit.
- Commit messages say what changed and why, in the imperative mood
  (`fix(search): ...`, `feat(permissions): ...`).
- Before every push, confirm the tree is clean of forbidden strings and
  secrets:

  ```bash
  # No agent-runtime names and no provenance phrasing.
  git grep -n -i -E "claude code|hermes|mewcode|codewhale|ported from|inspired by|mirrors" -- .
  git log --all --format='%B' | grep -i -E "co-authored-by|generated with"
  ```

  Both commands must print nothing.

## 5. Definition of done

A change is done when:

1. `python -m pytest tests -q` passes, with no new skips.
2. `ruff check lilbot tests` is clean.
3. A test exists that fails without the change. A bug fix without a regression
   test is incomplete.
4. The CI matrix is green on the branch.
5. The documentation rows in section 3 are updated.
6. The pull-request description states the observed behavior, the change, and
   the evidence (command output, trace, or screenshot).

## 6. Architecture boundaries

| Layer | Owns | Never does |
|---|---|---|
| `lilbot/core` | Agent loop, context management, sessions, events, guardrails | Terminal rendering, provider transport details |
| `lilbot/llm` | Provider transport, retry, error classification | Tool policy, UI state |
| `lilbot/tools` | Tool contracts, registry, built-in tools | Deciding whether the user may run something |
| `lilbot/sandbox` | Workspace paths, permission rules, shell policy | Model-facing messaging |
| `lilbot/memory` | File memory, recall, extraction | Compressing the live transcript |
| `lilbot/subagents`, `lilbot/teams` | Delegation and coordination | Deciding the global permission mode |
| `lilbot/tui`, `lilbot/cli` | Presentation and entrypoints | Policy decisions of any kind |

Two rules that are checked in review:

- **The UI renders events; it never decides policy.**
- **Approval lives in one place.** `ToolRegistry.execute()` enforces it. A tool
  handler must not implement its own approval check, and a new tool cannot skip
  the gate.

## 7. Commands

```bash
python -m pytest tests -q            # full suite
python -m pytest tests -q -k pattern # one area
ruff check lilbot tests               # lint
python -m compileall -q lilbot        # syntax
python -m lilbot --provider mock      # offline run, no key needed
python -m lilbot --provider mock --print "hello"
```

Use `--provider mock` for local verification. Never put a real key in a command
line, a test, or a fixture.

## 8. Cross-platform discipline

Windows is the primary development platform, and the suite must also pass on
Linux and macOS. When touching shell execution, paths, encoding, the clipboard,
or console handling:

- keep the POSIX path working;
- do not assert on a timing margin — use a barrier, a latch, or a value;
- do not assert that an invalid byte decodes to one specific glyph; assert the
  contract instead.

## 9. Definition of "same level as the reference implementation"

Not feature-count parity. It means: every P0 and P1 mechanism is implemented,
covered by a test, and backed by a reproducible measurement. Deferred and
excluded items are recorded explicitly in the development log rather than left
implicit.
