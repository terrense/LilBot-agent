# CodeWhale Parity And Overtake Roadmap

## Principle

LilBot should not stop at having similarly named tools. A tool is considered
aligned only when its schema, safety boundary, output contract, persistence,
tests, and failure behavior are strong enough for an agent to rely on.

## Correspondence Summary

### Subagents

CodeWhale and LilBot both expose 8 roles:

- `general`
- `explore`
- `plan`
- `review`
- `implementer`
- `verifier`
- `tool_agent`
- `custom`

Target overtake criteria:

- persistent session records
- transcript handles
- tool allowlists
- concurrency caps
- forked context
- role-specific prompts
- mandatory structured final reports

### Skills

CodeWhale bundles 11 skills. LilBot contains those 11 plus the original
`commit`, `plan`, `review`, and `summarize`.

Target overtake criteria:

- directory `SKILL.md` support
- companion files
- loader tool
- installer/creator workflows
- trust metadata
- project-local override precedence

### Tools

CodeWhale active/reference surface: 91 names in this audit. LilBot currently
registers 116 names because it keeps compatibility aliases and adds a few local
helpers. The important measure is behavior parity, not raw count.

## Claude Code Origin Delta

Additional source reviewed:
`F:\Experiment_laborotory\collection-claude-code-source-code-main\claude-code-source-code`
(`@anthropic-ai/claude-code-source` version `2.1.88`).

Claude adds a deeper ecosystem than CodeWhale in five areas:

- `Skill` tool: validates skill existence, permissions, fork vs inline mode,
  model invocation eligibility, and plugin/MCP skill sources.
- Skill loader: supports `allowed-tools`, `when_to_use`, aliases, hidden skills,
  `context: fork`, agent routing, hooks, effort, shell snippets, path filters,
  managed/user/project/plugin/MCP sources, and dynamic nested discovery.
- Agent runtime: supports built-in plus custom/plugin agents, agent memory,
  skills preloading, max turns, permission mode, MCP requirements, background
  tasks, progress, cancellation, worktree isolation, teams, and remote launch.
- Tool pool: includes `LSP`, `PowerShell`, `EnterWorktree`, `ExitWorktree`,
  `TeamCreate`, `TeamDelete`, `TaskCreate/Get/Update/List`, `SendMessage`,
  `AskUserQuestion`, `ToolSearch`, cron/remote trigger, and MCP resource tools.
- Command layer: permissions, hooks, checkpoint/rewind, compact/context,
  doctor/status, agents/tasks/skills/MCP/plugin, and review/security-review
  commands are product-level workflows worth imitating after core tools mature.

LilBot improvements completed in the Claude audit pass:

- Skill frontmatter parser now understands Claude-style metadata.
- Skills can define aliases, allowed tools, `when_to_use`, hidden visibility,
  fork/inline context, agent hints, model hints, effort, paths, and shell hints.
- Skill rendering supports `$ARGUMENTS`, `{{args}}`, named arguments, and
  `${CLAUDE_SKILL_DIR}` / `${LILBOT_SKILL_DIR}`.
- Directory skills now discover companion files recursively.
- `load_skill` exposes the full skill contract.
- Added a Claude-style `Skill` tool alias.
- Added Claude-style `Agent` and `Task` tool aliases.
- Added project custom agent loading from `.lilbot/agents/*.md`.
- Added 15 Claude-inspired bundled skills:
  `verify`, `debug`, `skillify`, `remember`, `simplify`, `batch`, `stuck`,
  `update-config`, `lorem-ipsum`, `keybindings-help`, `loop`, `schedule`,
  `claude-api`, `claude-in-chrome`, and `run-skill-generator`.
- Tests cover Claude-style skill metadata/loading and custom agent loading.

Current LilBot count after this pass:

- Tools: 116
- Skills: 30 total, 29 user-invocable
- Built-in subagent roles: 8, plus project custom agents

## Batch Plan

### Batch 1: Workspace Foundation

Status: in progress. Completed in this pass:

- `read_file`: line ranges, head/tail, query projections, PDF dependency probe.
- `list_dir`: structured entries, noisy directory filtering, hidden-file switch.
- `grep_files`: regex matching, glob filtering, context lines, structured matches.
- `file_search`: noisy directory filtering and scored metadata.
- `retrieve_tool_result` / `handle_read`: shared bounded projections.
- `git_status`: porcelain parsing into branch/change metadata.
- `git_diff`: patch output plus file list metadata.
- `git_log`: structured commit metadata.
- `git_show` / `git_blame`: raw output plus structured metadata.
- `diagnostics`: local dependency availability in structured output.
- Tests: `tests/test_batch1_workspace_tools.py`.

Remaining before Batch 1 is closed:

- pure-Python `apply_patch` fallback for non-git workspaces
- richer `run_tests` classification and log artifact storage
- `validate_data` schema-aware validation beyond JSON/CSV/TSV shape checks
- `project_map` language/framework summary rather than file listing only

Tools:

- `read_file`
- `list_dir`
- `write_file`
- `edit_file`
- `apply_patch`
- `retrieve_tool_result`
- `handle_read`
- `grep_files`
- `file_search`
- `git_status`
- `git_diff`
- `git_log`
- `git_show`
- `git_blame`
- `diagnostics`
- `run_tests`
- `validate_data`
- `project_map`

Acceptance:

- file reads support line ranges and bounded projections
- directory listings skip noisy generated folders and return structured entries
- search is regex-based with context lines
- handle reads support `head`, `tail`, `lines`, and `query`
- git tools return structured metadata
- tests cover the above

### Batch 2: Execution And Durable Work

Tools:

- `exec_shell`
- `exec_shell_wait`
- `exec_shell_interact`
- `exec_shell_cancel`
- `task_shell_start`
- `task_shell_wait`
- `task_create`
- `task_list`
- `task_read`
- `task_cancel`
- `task_gate_run`
- `checklist_*`
- `todo_*`
- `update_plan`
- `create_goal`
- `get_goal`
- `update_goal`

Acceptance:

- background jobs stream incremental output
- task records persist timeline, gates, artifacts, and linked jobs
- verification gates store compact evidence plus logs
- checklist and goal state survive restarts

### Batch 3: Subagent Runtime

Tools:

- `agent_open`
- `agent_eval`
- `agent_close`
- `tool_agent`
- legacy aliases for migration

Acceptance:

- named sessions persist under `.lilbot/state`
- role prompts and tool allowlists are enforced
- concurrency cap is configurable
- final reports follow SUMMARY/CHANGES/EVIDENCE/RISKS/BLOCKERS
- parent can retrieve transcript slices through `handle_read`

Claude-origin additions for this batch:

- custom agent definitions from `.lilbot/agents/*.md` (initial support done)
- agent aliases and frontmatter fields: tools, disallowed tools, skills,
  initial prompt, model, effort, max turns, memory, and permission mode
- subagent transcript handles and progress events
- worktree isolation probe and explicit unsupported result when unavailable
- `Agent`/`Task` compatibility aliases for Claude-style invocation (done)

### Batch 4: Skills And Plugins

Tools/skills:

- `Skill`
- `load_skill`
- `skill-creator`
- `skill-installer`
- `plugin-creator`
- plugin script registry
- trust records

Acceptance:

- skills can be installed from local folders
- `SKILL.md` metadata is validated
- companion files are discoverable
- plugin scripts expose schemas and approval modes

Status: partially upgraded by the Claude audit pass.

Remaining Claude-origin work:

- enforce `allowed-tools` during skill execution
- support true forked skill execution through the subagent runtime
- support dynamic nested skill discovery after file reads/edits
- support path-filtered conditional skills
- support hooks and shell expansion safely
- add project/user/managed/plugin/MCP source precedence

### Batch 4B: Claude Workflow Tools

Tools:

- `AskUserQuestion`
- `SendMessage`
- `TaskCreate`
- `TaskGet`
- `TaskUpdate`
- `TaskList`
- `TaskOutput`
- `TaskStop`
- `EnterPlanMode`
- `ExitPlanMode`
- `EnterWorktree`
- `ExitWorktree`
- `ToolSearch`
- `LSP`
- `PowerShell`

Acceptance:

- Claude-style names resolve to existing LilBot behavior where appropriate
- task tools expose durable records and output handles
- plan-mode tools gate implementation until approval
- worktree tools create or report an explicit unsupported state
- LSP reports symbols/definitions when a local language server is available
- PowerShell tool applies command parsing and destructive-operation checks

### Batch 5: External Integrations

Tools:

- MCP resource/tool/prompt discovery
- `github_*`
- `web_search`
- `fetch_url`
- `web_run`
- `finance`
- `automation_*`

Acceptance:

- MCP servers expose discovered tool schemas
- web results include ref ids and citations
- GitHub write tools require evidence and permission
- automations enqueue durable tasks

### Batch 6: Analysis, Documents, And Media

Tools:

- `rlm_*`
- `pandoc_convert`
- `image_ocr`
- `image_analyze`
- document/spreadsheet/presentation skill workflows

Acceptance:

- RLM sessions return handles for large values
- document tools verify outputs
- OCR/Pandoc/Vision report dependency/config status precisely

### Batch 7: Overtake Layer

New LilBot differentiators:

- parity dashboard with red/yellow/green tool maturity
- per-tool contract tests generated from schemas
- local replay harness for subagent sessions
- safety policy simulator for shell/GitHub/plugin writes
- benchmark suite comparing CodeWhale-inspired workflows
