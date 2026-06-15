# Claude Code Origin Tool, Skill, And Subagent Inventory

Sources reviewed:

- `F:\Experiment_laborotory\collection-claude-code-source-code-main\claude-code-source-code`
- `F:\Experiment_laborotory\collection-claude-code-source-code-main\original-source-code\src`

The organized source reports `@anthropic-ai/claude-code-source` version
`2.1.88` in `package.json`. The practical source of truth for model-callable
tools is `src/tools.ts:getAllBaseTools()`.

## Tool Surface

Core tools found in `getAllBaseTools()`:

- `Agent` with legacy alias `Task`
- `TaskOutput`
- `Bash`
- `Glob`
- `Grep`
- `ExitPlanMode`
- `Read`
- `Edit`
- `Write`
- `NotebookEdit`
- `WebFetch`
- `TodoWrite`
- `WebSearch`
- `TaskStop`
- `AskUserQuestion`
- `Skill`
- `EnterPlanMode`
- `SendMessage`
- `SendUserMessage` with legacy alias `Brief`
- `ListMcpResourcesTool`
- `ReadMcpResourceTool`

Feature-gated or environment-gated tools found:

- `Config`
- `Tungsten`
- `SuggestBackgroundPR`
- `WebBrowser`
- `TaskCreate`
- `TaskGet`
- `TaskUpdate`
- `TaskList`
- `OverflowTest`
- `CtxInspect`
- `TerminalCapture`
- `LSP`
- `EnterWorktree`
- `ExitWorktree`
- `ListPeers`
- `TeamCreate`
- `TeamDelete`
- `VerifyPlanExecution`
- `REPL`
- `Workflow`
- `Sleep`
- `CronCreate`
- `CronDelete`
- `CronList`
- `RemoteTrigger`
- `Monitor`
- `SendUserFile`
- `PushNotification`
- `SubscribePR`
- `PowerShell`
- `Snip`
- `TestingPermission`
- `ToolSearch`

Dynamic MCP tools are created separately through `MCPTool` and `McpAuthTool`;
they are merged with built-ins at runtime by `assembleToolPool()`.

Important behavior details:

- Tool assembly sorts built-ins before MCP tools for prompt-cache stability.
- Deny rules filter tools before the model sees them.
- Simple mode can restrict the tool pool to Bash/Read/Edit.
- REPL mode hides primitive tools and exposes them inside the REPL context.
- Async agents have their own allowed-tool set; agent recursion and plan-mode
  tools are deliberately blocked.

## Skill Surface

Claude Code has a real skill ecosystem, not just markdown templates.

Bundled skills registered in `src/skills/bundled/index.ts`:

- `update-config`
- `keybindings-help` (hidden/non-user-invocable)
- `verify`
- `debug`
- `lorem-ipsum`
- `skillify`
- `remember`
- `simplify`
- `batch`
- `stuck`

Feature-gated bundled skills:

- `dream`
- `hunter`
- `loop`
- `schedule`
- `claude-api`
- `claude-in-chrome`
- `run-skill-generator`

Loader capabilities found in `src/skills/loadSkillsDir.ts`:

- Loads managed, user, project, additional-directory, plugin, bundled, legacy
  command, and MCP skills.
- Supports directory format `skill-name/SKILL.md`.
- Legacy `/commands` markdown can become model-invocable skills.
- Supports dynamic discovery of nested `.claude/skills` directories after file
  operations.
- Supports path-filtered conditional skills.
- Deduplicates skills by real path.

Frontmatter fields parsed by the loader:

- `name`
- `description`
- `allowed-tools`
- `argument-hint`
- `arguments`
- `when_to_use`
- `version`
- `model`
- `disable-model-invocation`
- `user-invocable`
- `hooks`
- `context: fork`
- `agent`
- `effort`
- `shell`
- `paths`

`SkillTool` validates skill existence, permission rules, model invocation
eligibility, inline vs fork execution, plugin/MCP skill sources, and records
skill usage for ranking.

## Subagent Surface

Built-in agents found in `src/tools/AgentTool/built-in`:

- `general-purpose`
- `statusline-setup`
- `Explore`
- `Plan`
- `claude-code-guide`
- `verification`

The built-in list is conditional: Explore/Plan and verification can be gated,
and SDK users can disable built-ins.

Custom agent capabilities found in `loadAgentsDir.ts`:

- User/project/managed/plugin agents.
- JSON and markdown agent definitions.
- Frontmatter fields: `name`, `description`, `tools`, `disallowedTools`,
  `skills`, `initialPrompt`, `mcpServers`, `hooks`, `model`, `effort`,
  `permissionMode`, `maxTurns`, `background`, `memory`, `isolation`, `color`.
- Agent memory can be scoped to user, project, or local.
- Agents can require MCP servers.
- Active agents are merged with precedence across built-in, plugin, user,
  project, flag, and managed sources.

`AgentTool` capabilities:

- Input fields include `description`, `prompt`, `subagent_type`, `model`,
  `run_in_background`, `name`, `team_name`, `mode`, `isolation`, and `cwd`.
- Supports synchronous agents, background agents, teammate/team spawning,
  worktree isolation, remote launch in first-party builds, progress events,
  cancellation, task output files, and task metadata.
- Uses tool filtering and permission mode per agent.

## Slash Command Surface

Claude Code has a large command layer in `src/commands.ts`. These are not all
model-callable tools, but many are important product capabilities:

- Workspace/session: `clear`, `compact`, `resume`, `session`, `context`,
  `rewind`, `files`, `diff`, `status`
- Configuration: `config`, `permissions`, `hooks`, `model`, `output-style`,
  `theme`, `vim`, `keybindings`
- Integrations: `mcp`, `plugin`, `reload-plugins`, `ide`, `desktop`, `mobile`,
  `install-github-app`, `install-slack-app`
- Agent workflow: `agents`, `tasks`, `plan`, `review`, `security-review`,
  `ultrareview`, `doctor`, `cost`, `usage`, `stats`
- Internal/feature-gated: `brief`, `bridge`, `voice`, `workflows`,
  `subscribe-pr`, `fork`, `buddy`, `proactive`, and others.

LilBot should not copy every CLI command first. The high-value command concepts
to imitate are permissions, hooks, rewind/checkpoint, compact/context, agents,
tasks, skills, MCP, plugin, doctor/status, and review/security-review.

## LilBot Gap Assessment After This Pass

Current LilBot counts after the Claude skill-loader and custom-agent upgrades:

- Tools: 116
- Skills: 30 total, 29 user-invocable
- Built-in subagent roles: 8, plus project custom agents under `.lilbot/agents`

Quality status:

- Green: file read/list/search projections, basic git metadata, CodeWhale skill
  names, Claude-style skill metadata parsing, skill aliases, hidden skills,
  companion-file discovery, tested loading, Claude `Skill`/`Agent`/`Task`
  compatibility names, and project custom agent loading.
- Yellow: shell/background jobs, task state, plan/checklist/goal state,
  subagent open/eval/close, web/fetch, GitHub helpers, and MCP calls are useful
  but not yet Claude-grade in permissions, streaming, or lifecycle depth.
- Red: worktree isolation, LSP, PowerShell-specific safety, real forked Skill
  execution, dynamic nested skill discovery, agent memory, hooks, checkpoint
  rewind, team agents, and full MCP prompt/resource parity.

Conclusion: LilBot is no longer an empty shell and is not just a raw number
stack, but its maturity is uneven. The next work should close yellow/red gaps
by behavior contract, not by adding more aliases.
