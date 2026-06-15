# CodeWhale Tool, Skill, And Subagent Inventory

Source reviewed: `F:\Experiment_laborotory\CodeWhale-main`.

This file tracks the clean-room features LilBot should imitate first. CodeWhale
has a broad agent surface, but the core pattern is simple:

1. Tools are small, typed, permission-aware capabilities exposed to the model.
2. Skills are indexed in the prompt, then loaded on demand to save context.
3. Subagents run as managed tasks with status, result retrieval, and cancellation.
4. The TUI renders only useful milestones, while noisy tool output is summarized.

## Subagent Roles Found

CodeWhale exposes persistent child sessions through `agent_open`,
`agent_eval`, and `agent_close`. The role taxonomy found in docs and source is:

- `general`: default worker for multi-step tasks.
- `explore`: read-only explorer for mapping code quickly.
- `plan`: strategy and decomposition, minimal writes.
- `review`: read-only bug/risk review.
- `implementer`: scoped code changes with quick verification.
- `verifier`: run validation and report pass/fail evidence.
- `tool_agent`: source-only fast execution lane, also aliased as `fin`,
  `executor`, and `tool-agent`.
- `custom`: explicit tool allowlist supplied by the caller.

Legacy aliases still exist in source for older sessions (`agent_spawn`,
`agent_result`, `agent_cancel`, `agent_list`, `resume_agent`,
`delegate_to_agent`), but the active surface is the three-tool API above.

## Built-In Skills Found

CodeWhale bundles 11 skills under `crates/tui/assets/skills/*/SKILL.md`:

- `delegate`
- `documents`
- `feishu`
- `mcp-builder`
- `pdf`
- `plugin-creator`
- `presentations`
- `skill-creator`
- `skill-installer`
- `spreadsheets`
- `v4-best-practices`

## Tool Families Found

Workspace and code:
- `file`, `file_search`, `search`, `apply_patch`, `diff_format`
- `diagnostics`, `test_runner`, `cargo_failure_summary`, `fim`

Shell and execution:
- `shell`, `shell_output`, `js_execution`, `parallel`
- `approval_cache`, `arg_repair`, `large_output_router`, `truncate`

Web and external data:
- `web_search`, `fetch_url`, `web_run`
- `github`, `finance`, `image_ocr`, `pandoc`

Agent workflow:
- `plan`, `todo`, `tasks`, `goal`, `review`
- `remember`, `recall_archive`, `revert_turn`
- `skill`, `subagent`

Project and platform:
- `project`, `plugin`, `notify`, `user_input`
- `validate_data`, `schema_sanitize`, `handle`

## LilBot Parity Status

Already present:
- Workspace files: `list_dir`, `read_file`, `write_file`, `edit_file`
- Local search: `glob`, `grep`
- Permission-gated shell: `bash`
- Memory: `memory_save`, `memory_list`, `memory_search`, `memory_delete`
- Skills: `skill_list`, `skill_run`
- Subagents: `agent_spawn`, `agent_status`, `agent_list`
- MCP: `mcp_servers`, `mcp_call`

Added in this pass:
- `web_search`: public web search with DuckDuckGo and Bing fallback
- `fetch_url`: direct public URL fetch with basic SSRF guard
- `web_fetch`: alias for `fetch_url`
- `/model`: runtime switch between `deepseek-v4-flash` and `deepseek-v4-pro`

Next high-value targets:
- `load_skill`: name-based `SKILL.md` loader with companion-file listing
- `todo` and `plan`: structured visible task state instead of freeform notes
- `agent_open`, `agent_eval`, `agent_close`: durable subagent sessions
- `git` and `github`: status, diff, commit, PR/issue helpers
- `web_run`: richer browse workflow after `web_search`/`fetch_url`
- `parallel`: safe parallel tool execution for read-only work

Current parity target for LilBot:
- Use exact CodeWhale-style names where OpenAI-compatible function naming
  allows them.
- Keep old LilBot names as compatibility aliases (`bash`, `grep`, `glob`,
  `agent_spawn`, `agent_status`).
- Implement advanced external tools with honest local probes and structured
  fallback results when a dependency is not installed.

## Design Notes To Preserve

- Prefer structured JSON tool results so the model can reason without scraping UI text.
- Keep raw long outputs in context only as needed; render summarized milestones in Trace.
- Never use shell for common first-class operations when a tool exists.
- Web search should be used for current, niche, or unfamiliar facts before answering.
- Fetching URLs must reject localhost, private IPs, and link-local/cloud-metadata targets.
