# CodeWhale Tool, Skill, And Subagent Inventory

Source reviewed: `F:\Experiment_laborotory\CodeWhale-main`.

This file tracks the clean-room features LilBot should imitate first. CodeWhale
has a broad agent surface, but the core pattern is simple:

1. Tools are small, typed, permission-aware capabilities exposed to the model.
2. Skills are indexed in the prompt, then loaded on demand to save context.
3. Subagents run as managed tasks with status, result retrieval, and cancellation.
4. The TUI renders only useful milestones, while noisy tool output is summarized.

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

## Design Notes To Preserve

- Prefer structured JSON tool results so the model can reason without scraping UI text.
- Keep raw long outputs in context only as needed; render summarized milestones in Trace.
- Never use shell for common first-class operations when a tool exists.
- Web search should be used for current, niche, or unfamiliar facts before answering.
- Fetching URLs must reject localhost, private IPs, and link-local/cloud-metadata targets.
