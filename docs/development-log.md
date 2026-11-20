# Development log

What changed, why it changed, and what it means for the runtime. One entry per
milestone, newest first. The changelog lists *what*; this file explains the
*reasoning* and the consequence.

Each entry follows the same shape:

- **What** — the mechanism, with the files that carry it.
- **Why** — the failure it prevents, or the property it buys.
- **Consequence** — what it changes for the agent, for the operator, or for the
  evaluation.

---

## 2026-11-20 · S5/S6 — Context accounting that matches the real window

**What**

- `lilbot/core/model_windows.py` (new): a per-model context-window table and a
  single resolver, `resolve_context_window(model, base_url, configured,
  explicit)`. Exact model id first, then substring match, then a provider
  default, then the configured fallback.
- `lilbot/config.py`: `context_window_explicit` records whether the operator
  actually chose a window, so an explicit choice is never overridden by the
  table.
- `lilbot/core/agent.py`:
  - `effective_context_window()` is the one source of truth for the window;
    `compact()` now uses it instead of the raw config value.
  - `context_tokens()` now includes the tool schemas in the request estimate,
    and `token_breakdown()` exposes the split for `/tokens`.
  - `_ensure_context_budget()` runs before **every** provider call, not once per
    turn.
- `lilbot/tui/dashboard.py`: the context bar resolves the window through the
  same resolver instead of a private table.

**Why**

Three defects made the context story wrong in ways the evaluation surfaced:

1. **The window was a constant.** `config.context_window` defaulted to 128k
   regardless of the model. A 64k model therefore kept growing until the
   provider rejected the request — the compaction trigger fired *after* the
   window had already been exceeded. The dashboard had its own private table
   (64k for one model family, 1M for another), so the bar the user saw and the
   threshold the runtime enforced disagreed.
2. **Tool schemas were invisible to the trigger.** The provider counts the
   serialized tool schemas in `prompt_tokens`, but the pre-response estimate
   only counted messages. With 146 registered tools the schemas are a large
   fixed cost, and a turn could start under budget and cross it before the
   model ever saw the request.
3. **The check ran once per turn.** Compaction was evaluated at turn entry. A
   long turn — many tool calls, each returning a large result — crossed the
   window *inside* the turn, and the first signal was a provider error rather
   than the cheap local prune that would have prevented it.

**Consequence**

- The trigger and the visible bar now use the same number, so "62% context"
  means the same thing to the operator and to the runtime.
- The window is derived from the model, so an operator does not have to know a
   model's limit and configure it correctly; an explicit override still wins.
- The budget check now runs on every provider call. The cost is one cheap
  estimate (last reported usage plus the estimated tail) per call; the
  expensive path — a local prune, and only then a model summary — still runs
  only when the estimate is genuinely near the limit.
- Overflow recovery changes from "the API told us we were too big" to "the
  runtime noticed first". The evaluation criterion for this work is: on a 64k
  model with a long history, zero provider overflow errors.

**Measurement**

`tests/test_context_window.py` covers the resolver, the explicit override, the
schema accounting, and the per-call check. `tests/test_budget_per_request.py`
covers the mid-turn trigger with a fake provider that reports a small window.

---

## 2026-11-19 · S1/S3 — One permission pipeline, and corpora that hold it

**What**

- `lilbot/sandbox/permissions.py`: `check(..., force=True)` for an explicit
  ask; `deny-all` is a hard override; a forced ask still consults persistent
  exact-action decisions.
- `lilbot/tools/registry.py`: the gate resolves in one order —
  **deny > ask > tool > mode > allow**. Rule denial, `deny-all`, a forced ask,
  the tool's own safety check, and only then the mode.
- `lilbot/tools/builtin.py`: the shell path follows the same order.
- Skill scopes: an inline skill narrows the advertised *and* executable tool
  set to its declared `allowed_tools`; nested skills intersect; the scope is
  reset at every turn boundary; a forked skill passes the allowlist to the
  subagent.
- `tests/corpora/dangerous_commands.json` (new): 40 dangerous or
  over-privileged commands.
- `tests/test_path_boundary.py` (new): 12 path escapes plus symlink and
  Windows junction cases.

**Why**

The rules engine existed but had no production call site, so a project rule
could not actually change a decision. Separately, the read-only shell allowlist
matched on positional tokens and ignored flags, so `find . -exec rm -rf {} +`
and `git branch -D main` were auto-approved. A rule that cannot change an
outcome is documentation, not a control.

**Consequence**

- An operator can write `{"deny": ["Bash(git push*)"]}` in
  `.lilbot/permissions.json` and have it enforced, including over
  `accept-all`.
- A skill's `allowed_tools` is now a real boundary rather than a hint.
- The two corpora turn "the gate is probably fine" into a measured claim: 40/40
  commands gated, 0/12 path escapes.

**Measurement**

`tests/test_permission_pipeline.py` (13 cases) fixes the ordering contract.
`tests/test_skill_scope.py` (5 cases) fixes the scope contract.

---

## 2026-11-19 · v0.2.0 — Release-grade repository

**What**

Repository restructured into `lilbot/`, `tests/`, `evaluation/`, `benchmarks/`,
`docs/`, `examples/`, `.github/`. Bilingual README with real runtime captures.
MIT license, contributing guide, security policy, code of conduct, issue forms,
pull-request template, and a CI matrix over Python 3.10–3.13 on Ubuntu and
Windows. PEP 621 packaging that ships all 28 bundled skill files.

**Why**

A public repository has to be installable, auditable, and honest about what it
does. The previous tree carried scratch files, placeholder images, internal
strategy documents, and no license — none of which survives contact with a
stranger.

**Consequence**

- `pip install lilbot-agent` works; the wheel carries the skills.
- The two READMEs are the front door; the docs index is the map.
- The evaluation baseline is published **with** its failures, so the number is a
  regression target rather than a marketing claim.

**Measurement**

660 passing tests at the tag; the CI matrix green on eight configurations.

---

## 2026-11-19 · Correctness fixes found while preparing the release

**What**

- The offline provider now routes on the latest user turn and answers with a
  tool observation instead of repeating the same call until the step budget.
- `glob("**")` follows the running interpreter's `pathlib` semantics instead of
  one hardcoded behavior.
- Local OpenAI-compatible providers no longer require a placeholder key.
- An SSE `error` frame raises a classified provider error instead of producing
  an empty successful turn.
- A tool validator that raises now fails closed.
- A tool call truncated at the output limit is no longer executed; the model is
  asked to re-issue a complete call.
- MCP startup fails fast when a configured server exits.

**Why**

Each of these let the runtime report success for work that never happened. For
an agent whose value is "I checked and it works", a false success is worse than
a visible failure.

**Consequence**

The runtime now has a consistent rule: **an error is surfaced as an error**.
Every one of these paths has a regression test.

---

## Deferred and excluded

Recorded so that a later reader does not mistake these for oversights.

| Item | Status | Why |
|---|---|---|
| Streaming tool execution (start safe tools while the model is still streaming) | v0.3 milestone | Highest-risk change to the loop; needs a feature flag and golden traces |
| Distributed teammates and a durable task scheduler | v0.4 candidate | The in-process model is sufficient for a two-person project |
| A plugin marketplace | excluded | A local plugin directory carries skills, agents, hooks, and MCP servers for this scale |
| A custom terminal renderer | excluded | The existing renderer is adequate; the effort buys frame-time metrics, not capability |
| Remote feature flags and managed kill switches | excluded | A local flags file plus environment overrides cover the need; a remote gate must never terminate in-flight user work |
| Server-side context management and model-bound signature replay | not portable | Provider-private protocol semantics; only the provider-neutral discipline is portable |
