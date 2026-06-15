# CodeWhale Parity Phase 1 Spec

## Objective

Make LilBot-agent a useful local agent instead of a shell: add CodeWhale-style
subagents, bundled skills, and a broad typed tool surface with functional local
handlers.

## Scope

- Subagents: `agent_open`, `agent_eval`, `agent_close`, plus legacy aliases.
- Roles: `general`, `explore`, `plan`, `review`, `implementer`, `verifier`,
  `tool_agent`, and `custom`.
- Skills: directory-based `SKILL.md` loading and the 11 CodeWhale bundled skill
  names.
- Tools: file/search/shell/git/web/memory/skill/subagent/MCP/plan/checklist/
  goal/task/automation/RLM/diagnostic helpers.
- Compatibility: keep existing tests and old LilBot tool names working.

## Non-Goals

- Full Rust/TUI feature parity.
- Real scheduled background automation daemon.
- Full MCP transport implementation beyond the existing JSON-RPC-lines adapter.
- Full OCR, Pandoc, browser, or vision APIs without local dependencies/config.

## Acceptance

- `/tools` and model tool schemas include the CodeWhale-inspired names.
- `SkillRegistry` loads both `bundled/*.md` and `bundled/*/SKILL.md`.
- A subagent can be opened, evaluated, and closed using the new API.
- Plan/checklist/goal/task tools persist state under `.lilbot`.
- Tests pass with the offline provider.
