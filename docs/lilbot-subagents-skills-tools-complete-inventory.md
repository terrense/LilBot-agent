# LilBot Subagent, Skill, Tool Complete Inventory

更新时间：2026-06-15

项目根目录：`F:\Experiment_laborotory\collection-lilbot-source-code-main\LilBot-agent-code`

当前数量：

- Tools: 116
- Skills: 30 total, 29 user-invocable
- Built-in subagent roles: 8, plus project custom agents under `.lilbot/agents`

## How To Read This File

这份表不是单纯“数名字”。我把每个能力按下面几个维度列出来：

- `Name`: 模型或用户看到的能力名。
- `Description`: 当前 LilBot 代码里注册的说明，或 skill/agent 自身描述。
- `Project Position`: 在项目中的主要实现或配置位置。
- `How To Use`: 推荐调用方式。工具通常由模型自动调用；手动调试可按示例参数理解。
- `My Notes`: 我对成熟度、定位、下一步补强方向的判断。

## Subagents

实现位置：

- Built-in role definitions: `lilbot/subagents/manager.py`
- Runtime integration: `lilbot/tools/builtin.py`
- CLI runtime wiring: `lilbot/cli.py`
- Custom agent directory: `.lilbot/agents/*.md` or `.lilbot/agents/<name>/AGENT.md`

通用调用方式：

```json
agent_open({"type": "explore", "name": "scan", "prompt": "Map the auth flow"})
agent_eval({"name": "scan", "block": true})
agent_close({"name": "scan"})
```

Claude-compatible aliases:

```json
Agent({"subagent_type": "general", "description": "audit", "prompt": "Audit the changed files"})
Task({"subagent_type": "verification", "prompt": "Run verification and report evidence"})
```

Custom agent markdown example:

```markdown
---
name: security-auditor
description: Review code for security issues.
tools: read_file, grep_files
writes: false
shell: read-only
model: pro
---
You are a focused security auditor. Do not edit files.
```

| Name | Description | Project Position | How To Use | My Notes |
|---|---|---|---|---|
| `general` | Flexible worker for multi-step tasks. | `lilbot/subagents/manager.py` | `agent_open({"type":"general","prompt":"..."})` | 默认泛用 worker。适合不确定该用哪个角色时启动。 |
| `explore` | Read-only explorer for fast local evidence gathering. | `lilbot/subagents/manager.py` | `agent_open({"type":"explore","prompt":"map files"})` | 读代码、找证据、做范围定位。应保持只读。 |
| `plan` | Planner that decomposes work, risks, and verification. | `lilbot/subagents/manager.py` | `agent_open({"type":"plan","prompt":"plan migration"})` | 只做计划，不实现。后续要和 `EnterPlanMode/ExitPlanMode` 更强绑定。 |
| `review` | Reviewer that looks for bugs, regressions, and missing tests. | `lilbot/subagents/manager.py` | `agent_open({"type":"review","prompt":"review diff"})` | 用于代码审查，重点找风险和缺测。 |
| `implementer` | Focused implementer for a specified code change. | `lilbot/subagents/manager.py` | `agent_open({"type":"implementer","prompt":"make this change"})` | 允许写入的实现型 agent。下一步要强制 tool allowlist。 |
| `verifier` | Validation runner that reports pass/fail evidence without fixing failures. | `lilbot/subagents/manager.py` | `agent_open({"type":"verifier","prompt":"verify this"})` | 验证型 agent，不修 bug，只报告证据。 |
| `tool_agent` | Fast tool-bound executor for simple lookups and probes. | `lilbot/subagents/manager.py` | `tool_agent({"prompt":"quickly inspect ..."})` | 快速执行小型工具任务，不适合架构判断。 |
| `custom` | Caller-constrained role with an explicit allowed tool list. | `lilbot/subagents/manager.py` | `agent_open({"type":"custom","allowed_tools":["read_file"],"prompt":"..."})` | 给调用方显式约束工具范围。当前是提示级约束，后续要运行时强制。 |
| custom project agents | User/project-defined agents loaded from markdown. | `.lilbot/agents/*.md` | `agent_open({"type":"security-auditor","prompt":"..."})` | 已支持初版。可模仿 Claude 的 custom agents 继续扩展 memory/hooks/max turns。 |

## Skills

实现位置：

- Loader and metadata parser: `lilbot/skills/registry.py`
- Bundled skills: `lilbot/skills/bundled/`
- Skill tools: `lilbot/tools/builtin.py`
- System prompt listing: `lilbot/core/prompts.py`

通用调用方式：

```text
/skills
/skill verify "changed auth module"
```

工具调用方式：

```json
skill_list({})
load_skill({"name":"verify"})
Skill({"skill":"verify","args":"changed auth module"})
```

Skill frontmatter 当前支持：

- `name`
- `description`
- `aliases`
- `allowed-tools` / `allowed_tools`
- `argument-hint`
- `arguments`
- `when_to_use`
- `context` / `mode`
- `agent`
- `model`
- `effort`
- `paths`
- `shell`
- `disable-model-invocation`
- `user-invocable`

| Name | Description | Mode | Allowed Tools | Project Position | How To Use | My Notes |
|---|---|---:|---|---|---|---|
| `batch` | Break a large request into a tracked batch of smaller tasks. | inline | `update_plan`, `checklist_write`, `checklist_update`, `checklist_add`, `task_create`, `task_list`, `agent_open`, `agent_eval`, `agent_close` | `lilbot/skills/bundled/batch/SKILL.md` | `Skill({"skill":"batch","args":"large migration"})` | 适合 PM 式拆批。是你要求“spec 模式”的核心技能之一。 |
| `claude-api` | Help with  Claude API usage, models, and integration patterns. | fork | `web_search`, `fetch_url`, `read_file`, `grep_files` | `lilbot/skills/bundled/claude-api/SKILL.md` | `Skill({"skill":"claude-api","args":"tool use migration"})` | 涉及 API 当前信息时必须查官方资料；本 skill 已要求验证。 |
| `claude-in-chrome` | Coordinate browser-assisted work when a Chrome/browser connector is available. | inline | `mcp_servers`, `mcp_call`, `web_search`, `fetch_url` | `lilbot/skills/bundled/claude-in-chrome/SKILL.md` | `Skill({"skill":"claude-in-chrome","args":"inspect page"})` | 目前依赖 MCP/browser connector；没有 connector 时会降级说明。 |
| `commit` | Prepare a concise commit plan and suggested commit message. | inline | none declared | `lilbot/skills/bundled/commit.md` | `/skill commit` | 老 LilBot skill。后续可补 git diff 读取和 conventional commit 格式。 |
| `debug` | Investigate a bug or failing behavior and isolate the likely cause. | fork | `read_file`, `list_dir`, `grep_files`, `file_search`, `git_status`, `git_diff`, `run_tests`, `agent_open`, `agent_eval` | `lilbot/skills/bundled/debug/SKILL.md` | `Skill({"skill":"debug","args":"test X failing"})` | 适合先调查再动手，避免一上来乱改。 |
| `delegate` | Use subagents to split exploration, implementation, review, and verification work. | inline | none declared | `lilbot/skills/bundled/delegate/SKILL.md` | `/skill delegate "..."` |  对齐 skill。下一步可自动生成 agent plan。 |
| `documents` | Create, inspect, edit, or convert document-style deliverables. | inline | none declared | `lilbot/skills/bundled/documents/SKILL.md` | `/skill documents "..."` | 文档工作流入口，依赖 pandoc/office 能力继续增强。 |
| `feishu` | Work with Feishu or Lark bots, docs, sheets, bitables, approval flows, and API setup. | inline | none declared | `lilbot/skills/bundled/feishu/SKILL.md` | `/skill feishu "..."` | 目前是指导型 skill；真实 Feishu API 需要 connector/tool。 |
| `keybindings-help` | Explain or design keyboard shortcut bindings. | inline | `read_file`, `list_dir`, `grep_files` | `lilbot/skills/bundled/keybindings-help/SKILL.md` | hidden; use through `load_skill({"name":"keybindings-help"})` | `user-invocable:false`，不在普通列表里主动暴露。 |
| `loop` | Set up a recurring check or repeated agent workflow. | inline | `automation_create`, `automation_list`, `automation_update`, `automation_delete`, `task_create`, `update_plan` | `lilbot/skills/bundled/loop/SKILL.md` | `Skill({"skill":"loop","args":"watch CI daily"})` | 当前 automation 是 durable record，不是完整调度 daemon。 |
| `lorem-ipsum` | Generate placeholder copy with controllable tone and length. | inline | none declared | `lilbot/skills/bundled/lorem-ipsum/SKILL.md` | `Skill({"skill":"lorem-ipsum","args":"3 short UI cards"})` | 低风险内容生成 skill。 |
| `mcp-builder` | Design, build, configure, or debug Model Context Protocol servers. | inline | none declared | `lilbot/skills/bundled/mcp-builder/SKILL.md` | `/skill mcp-builder "..."` |  对齐。后续要结合 MCP discovery/schema。 |
| `pdf` | Read, extract, split, merge, rotate, watermark, fill, OCR, or create PDF files. | inline | none declared | `lilbot/skills/bundled/pdf/SKILL.md` | `/skill pdf "extract pages"` | 依赖 `pdftotext`、OCR、pandoc 等本地工具时会报告状态。 |
| `plan` | Break a task into implementation steps, decisions, and verification. | inline | none declared | `lilbot/skills/bundled/plan.md` | `/skill plan "..."` | 轻量计划 skill。后续应和 plan-mode tool 合并。 |
| `plugin-creator` | Scaffold or plan LilBot/local plugins. | inline | none declared | `lilbot/skills/bundled/plugin-creator/SKILL.md` | `/skill plugin-creator "..."` | 插件生态入口。当前插件执行沙箱还要补。 |
| `presentations` | Create, edit, inspect, or convert slide decks and PPTX-style presentations. | inline | none declared | `lilbot/skills/bundled/presentations/SKILL.md` | `/skill presentations "..."` | Office 类工作流，后续接 python-pptx。 |
| `remember` | Decide whether user information should become durable memory. | inline | `memory_save`, `memory_search`, `memory_list` | `lilbot/skills/bundled/remember/SKILL.md` | `Skill({"skill":"remember","args":"..."})` | 有边界：只保存稳定偏好/项目事实，不保存瞬时任务。 |
| `review` | Review code or a change request for bugs, risks, and missing tests. | inline | none declared | `lilbot/skills/bundled/review.md` | `/skill review "..."` | 老 LilBot review skill。后续要自动读取 diff。 |
| `run-skill-generator` | Generate a complete skill package from a short request. | fork | `skill_list`, `load_skill`, `write_file`, `edit_file`, `list_dir` | `lilbot/skills/bundled/run-skill-generator/SKILL.md` | `Skill({"skill":"run-skill-generator","args":"..."})` | 用来批量扩展技能库。需要写权限。 |
| `schedule` | Schedule a future task, reminder, or remote-style agent action. | inline | `automation_create`, `automation_list`, `automation_update`, `automation_delete`, `request_user_input` | `lilbot/skills/bundled/schedule/SKILL.md` | `Skill({"skill":"schedule","args":"tomorrow 9am check logs"})` | 当前只持久化记录，真正唤醒机制后续补。 |
| `simplify` | Simplify code, prose, or a plan without changing its intent. | inline | `read_file`, `grep_files`, `edit_file`, `run_tests` | `lilbot/skills/bundled/simplify/SKILL.md` | `Skill({"skill":"simplify","args":"this module"})` | 适合小范围重构。 |
| `skill-creator` | Create or improve LilBot skills and decide when a skill is the right abstraction. | inline | none declared | `lilbot/skills/bundled/skill-creator/SKILL.md` | `/skill skill-creator "..."` |  对齐 skill。 |
| `skill-installer` | Install, update, trust, or inspect local/community skill folders. | inline | none declared | `lilbot/skills/bundled/skill-installer/SKILL.md` | `/skill skill-installer "..."` | 后续要加信任记录和来源校验。 |
| `skillify` | Turn a repeated workflow into a reusable LilBot SKILL.md. | fork | `read_file`, `list_dir`, `grep_files`, `write_file`, `edit_file`, `load_skill`, `skill_list` | `lilbot/skills/bundled/skillify/SKILL.md` | `Skill({"skill":"skillify","args":"..."})` | Claude inspired。用于把重复工作产品化。 |
| `spreadsheets` | Create, inspect, clean, analyze, or convert spreadsheet and tabular files. | inline | none declared | `lilbot/skills/bundled/spreadsheets/SKILL.md` | `/skill spreadsheets "..."` | 后续接 openpyxl/csv validation。 |
| `stuck` | Recover when progress stalls or the agent may be looping. | fork | `read_file`, `list_dir`, `grep_files`, `git_status`, `git_diff`, `run_tests`, `update_plan`, `agent_open`, `agent_eval` | `lilbot/skills/bundled/stuck/SKILL.md` | `Skill({"skill":"stuck","args":"..."})` | 非常适合长任务中断后复盘。 |
| `summarize` | Summarize a topic, file, or tool result into terse engineering notes. | inline | none declared | `lilbot/skills/bundled/summarize.md` | `/skill summarize "..."` | 老 LilBot skill。 |
| `update-config` | Help inspect and update LilBot project or user configuration. | inline | `read_file`, `list_dir`, `grep_files`, `write_file`, `edit_file`, `diagnostics` | `lilbot/skills/bundled/update-config/SKILL.md` | `Skill({"skill":"update-config","args":"switch model"})` | 配置变更入口，要保持最小修改。 |
| `v4-best-practices` | Use for DeepSeek V4-style multi-step or plan-driven tasks. | inline | none declared | `lilbot/skills/bundled/v4-best-practices/SKILL.md` | `/skill v4-best-practices "..."` | DeepSeek V4 任务风格说明。 |
| `verify` | Verify that implementation work is correct before reporting done. | fork | `read_file`, `list_dir`, `grep_files`, `git_status`, `git_diff`, `run_tests`, `agent_open`, `agent_eval` | `lilbot/skills/bundled/verify/SKILL.md` | `Skill({"skill":"verify","args":"changed auth module"})` | 关键质量 skill。后续要和 verifier agent 自动联动。 |

## Tools

实现位置：

- Tool definitions and handlers: `lilbot/tools/builtin.py`
- Tool registry, schema listing, name normalization: `lilbot/tools/registry.py`
- Runtime context construction: `lilbot/cli.py`

通用调用方式：

工具主要由模型自动调用。手动阅读时可按下面格式理解：

```json
tool_name({"required_arg": "value"})
```

`ToolRegistry.resolve()` 支持大小写、连字符/下划线、camelCase、`*Tool` 后缀兼容。因此 `ReadMcpResourceTool`、`read_mcp_resource` 这类名字能尽量互通。

成熟度标记：

- `Core`: 已经是主要可依赖底座。
- `Compat`: 兼容别名或迁移入口。
- `Phase 1`: 有真实实现，但还需要增强生命周期/权限/输出。
- `Probe`: 会诚实报告依赖缺失或功能未配置。

| Category | Name | Description | Required Args | How To Use | Project Position | My Notes |
|---|---|---|---|---|---|---|
| Agent | `Agent` | Launch a Claude-style subagent. | `prompt` | `Agent({"prompt":"...","subagent_type":"explore"})` | `lilbot/tools/builtin.py` | Compat; Claude-style entry. |
| Skill | `Skill` | Execute a skill within the main conversation, LilBot style. | `skill` | `Skill({"skill":"verify","args":"..."})` | `lilbot/tools/builtin.py` | Compat; currently renders/loads skill, true fork execution is next. |
| Agent | `Task` | Legacy Claude-style alias for Agent. | `prompt` | `Task({"prompt":"..."})` | `lilbot/tools/builtin.py` | Compat; mirrors Claude legacy `Task`. |
| Agent | `agent_close` | Cancel or close a subagent session. | none | `agent_close({"name":"scan"})` | `lilbot/tools/builtin.py` | Phase 1; cancellation is local status update. |
| Agent | `agent_eval` | Fetch, wait on, or message a subagent session. | none | `agent_eval({"name":"scan","block":true})` | `lilbot/tools/builtin.py` | Phase 1; transcript slicing still needed. |
| Agent | `agent_list` | List sub-agent types and tasks. | none | `agent_list({})` | `lilbot/tools/builtin.py` | Core inventory view. |
| Agent | `agent_open` | Open a named subagent session. | `prompt` | `agent_open({"type":"review","prompt":"..."})` | `lilbot/tools/builtin.py` | Core subagent entry. |
| Agent | `agent_spawn` | Spawn a lightweight sub-agent. | `prompt` | `agent_spawn({"agent_type":"planner","prompt":"..."})` | `lilbot/tools/builtin.py` | Compat old LilBot name. |
| Agent | `agent_status` | Check a sub-agent task. | `task_id` | `agent_status({"task_id":"sub_x"})` | `lilbot/tools/builtin.py` | Compat status lookup. |
| Workspace | `apply_patch` | Apply a unified diff to the workspace with permission approval. | `patch` | `apply_patch({"patch":"..."})` | `lilbot/tools/builtin.py` | Core write path; pure Python fallback still planned. |
| Automation | `automation_create` | Create a durable automation record. | `prompt` | `automation_create({"name":"...","prompt":"..."})` | `lilbot/tools/builtin.py` | Phase 1; persistent record, not scheduler daemon. |
| Automation | `automation_delete` | Delete an automation. | `automation_id` | `automation_delete({"automation_id":"..."})` | `lilbot/tools/builtin.py` | Phase 1. |
| Automation | `automation_list` | List automation records. | none | `automation_list({})` | `lilbot/tools/builtin.py` | Phase 1. |
| Automation | `automation_pause` | Pause an automation. | `automation_id` | `automation_pause({"automation_id":"..."})` | `lilbot/tools/builtin.py` | Phase 1. |
| Automation | `automation_read` | Read an automation record. | `automation_id` | `automation_read({"automation_id":"..."})` | `lilbot/tools/builtin.py` | Phase 1. |
| Automation | `automation_resume` | Resume an automation. | `automation_id` | `automation_resume({"automation_id":"..."})` | `lilbot/tools/builtin.py` | Phase 1. |
| Automation | `automation_run` | Run an automation now by creating a task record. | `automation_id` | `automation_run({"automation_id":"..."})` | `lilbot/tools/builtin.py` | Phase 1; bridges automation to task. |
| Automation | `automation_update` | Update an automation record. | `automation_id` | `automation_update({"automation_id":"..."})` | `lilbot/tools/builtin.py` | Phase 1. |
| Shell | `bash` | Run a shell command in the workspace after permission approval. | `command` | `bash({"command":"python -m pytest -q"})` | `lilbot/tools/builtin.py` | Core but needs Claude-grade shell safety classifier. |
| Workflow | `checklist_add` | Add one checklist item. | `content` | `checklist_add({"content":"..."})` | `lilbot/tools/builtin.py` | Core PM state. |
| Workflow | `checklist_list` | List checklist items. | none | `checklist_list({})` | `lilbot/tools/builtin.py` | Core PM state. |
| Workflow | `checklist_update` | Update one checklist item. | `id` | `checklist_update({"id":"...","status":"done"})` | `lilbot/tools/builtin.py` | Core PM state. |
| Workflow | `checklist_write` | Replace the active checklist. | `items` | `checklist_write({"items":[...]})` | `lilbot/tools/builtin.py` | Core PM state. |
| Execution | `code_execution` | Execute Python code with permission approval. | `code` | `code_execution({"code":"print(1)"})` | `lilbot/tools/builtin.py` | Phase 1; local Python execution. |
| Goal | `create_goal` | Create the active goal. | `objective` | `create_goal({"objective":"..."})` | `lilbot/tools/builtin.py` | Core long-task tracking. |
| Diagnostics | `diagnostics` | Report workspace, git, Python, model, and permission diagnostics. | none | `diagnostics({})` | `lilbot/tools/builtin.py` | Core health check. |
| Workspace | `edit_file` | Replace text in a workspace file. | `path`, `old`, `new` | `edit_file({"path":"...","old":"...","new":"..."})` | `lilbot/tools/builtin.py` | Core write tool. |
| Shell | `exec_interact` | Alias for exec_shell_interact. | `task_id` | `exec_interact({"task_id":"...","input":"..."})` | `lilbot/tools/builtin.py` | Compat. |
| Shell | `exec_shell` | Run a shell command, optionally in the background. | `command` | `exec_shell({"command":"npm test","background":true})` | `lilbot/tools/builtin.py` | Phase 1 background jobs. |
| Shell | `exec_shell_cancel` | Cancel a background shell task. | none | `exec_shell_cancel({"task_id":"..."})` | `lilbot/tools/builtin.py` | Phase 1. |
| Shell | `exec_shell_interact` | Send stdin to a background shell task. | `task_id` | `exec_shell_interact({"task_id":"...","input":"q"})` | `lilbot/tools/builtin.py` | Phase 1. |
| Shell | `exec_shell_wait` | Wait for a background shell task. | `task_id` | `exec_shell_wait({"task_id":"..."})` | `lilbot/tools/builtin.py` | Phase 1. |
| Shell | `exec_wait` | Alias for exec_shell_wait. | `task_id` | `exec_wait({"task_id":"..."})` | `lilbot/tools/builtin.py` | Compat. |
| Web | `fetch_url` | Fetch a known public HTTP/HTTPS URL and return readable content. | `url` | `fetch_url({"url":"https://..."})` | `lilbot/tools/builtin.py` | Phase 1 with SSRF guard. |
| Workspace | `file_search` | Fuzzy-match workspace filenames. | `query` | `file_search({"query":"settings"})` | `lilbot/tools/builtin.py` | Core. |
| Editing | `fim_edit` | FIM edit placeholder with explicit unavailable result. | none | `fim_edit({...})` | `lilbot/tools/builtin.py` | Probe; needs configured FIM model. |
| Data | `finance` | Fetch simple quote CSV data from stooq. | none | `finance({"ticker":"AAPL.US"})` | `lilbot/tools/builtin.py` | Probe/data helper. |
| Goal | `get_goal` | Read the active goal. | none | `get_goal({})` | `lilbot/tools/builtin.py` | Core. |
| Git | `git_blame` | Show git blame for a line range. | `path` | `git_blame({"path":"file.py"})` | `lilbot/tools/builtin.py` | Core git evidence. |
| Git | `git_diff` | Inspect git diff. | none | `git_diff({})` | `lilbot/tools/builtin.py` | Core. |
| Git | `git_log` | Inspect recent git commits. | none | `git_log({"limit":5})` | `lilbot/tools/builtin.py` | Core. |
| Git | `git_show` | Show a git revision. | none | `git_show({"revision":"HEAD"})` | `lilbot/tools/builtin.py` | Core. |
| Git | `git_status` | Inspect git status. | none | `git_status({})` | `lilbot/tools/builtin.py` | Core. |
| GitHub | `github_close_issue` | Close a GitHub issue through gh with permission approval. | `issue` | `github_close_issue({"issue":"123"})` | `lilbot/tools/builtin.py` | Phase 1; depends on `gh`. |
| GitHub | `github_close_pr` | Close a GitHub PR through gh with permission approval. | `pr` | `github_close_pr({"pr":"12"})` | `lilbot/tools/builtin.py` | Phase 1; depends on `gh`. |
| GitHub | `github_comment` | Post a GitHub issue/PR comment through gh with permission approval. | `target`, `body` | `github_comment({"target":"12","body":"..."})` | `lilbot/tools/builtin.py` | Phase 1 write action. |
| GitHub | `github_issue_context` | Read GitHub issue context through gh. | none | `github_issue_context({"issue":"123"})` | `lilbot/tools/builtin.py` | Phase 1. |
| GitHub | `github_pr_context` | Read GitHub PR context through gh. | none | `github_pr_context({"pr":"12"})` | `lilbot/tools/builtin.py` | Phase 1. |
| Workspace | `glob` | Find files by glob pattern. | `pattern` | `glob({"pattern":"**/*.py"})` | `lilbot/tools/builtin.py` | Compat search alias. |
| Workspace | `grep` | Search text in workspace files. | `pattern` | `grep({"pattern":"TODO"})` | `lilbot/tools/builtin.py` | Compat search alias. |
| Workspace | `grep_files` | Regex-like text search in workspace files. | `pattern` | `grep_files({"pattern":"def .*","glob":"*.py"})` | `lilbot/tools/builtin.py` | Core. |
| Handle | `handle_read` | Read a bounded projection from a path-like handle. | none | `handle_read({"handle":"file.py","lines":"1-50"})` | `lilbot/tools/builtin.py` | Core large-result pattern. |
| Media | `image_analyze` | Report image metadata; vision API is not configured in phase 1. | `image_path` | `image_analyze({"image_path":"x.png"})` | `lilbot/tools/builtin.py` | Probe; honest no-vision state. |
| Media | `image_ocr` | Run OCR with tesseract when installed. | `image_path` | `image_ocr({"image_path":"x.png"})` | `lilbot/tools/builtin.py` | Probe; depends on tesseract. |
| Execution | `js_execution` | Execute JavaScript through node when installed. | `code` | `js_execution({"code":"console.log(1)"})` | `lilbot/tools/builtin.py` | Phase 1; depends on node. |
| Workspace | `list_dir` | List files under a workspace path. | none | `list_dir({"path":".","max_depth":2})` | `lilbot/tools/builtin.py` | Core. |
| MCP | `list_mcp_resource_templates` | List MCP resource templates known to LilBot. | none | `list_mcp_resource_templates({})` | `lilbot/tools/builtin.py` | Phase 1. |
| MCP | `list_mcp_resources` | List MCP resources known to LilBot. | none | `list_mcp_resources({})` | `lilbot/tools/builtin.py` | Phase 1. |
| Skill | `load_skill` | Load a skill body and companion-file list by name. | `name` | `load_skill({"name":"verify"})` | `lilbot/tools/builtin.py` | Core skill discovery. |
| MCP | `mcp_call` | Call a tool on an MCP-style server. | `server`, `tool` | `mcp_call({"server":"x","tool":"y","arguments":{}})` | `lilbot/tools/builtin.py` | Phase 1 JSON-RPC adapter. |
| MCP | `mcp_read_resource` | Alias for read_mcp_resource. | `uri` | `mcp_read_resource({"uri":"..."})` | `lilbot/tools/builtin.py` | Compat. |
| MCP | `mcp_servers` | List configured MCP-style servers. | none | `mcp_servers({})` | `lilbot/tools/builtin.py` | Core MCP inventory. |
| Memory | `memory_delete` | Delete a memory by id or name. | `id_or_name` | `memory_delete({"id_or_name":"style"})` | `lilbot/tools/builtin.py` | Core memory management. |
| Memory | `memory_list` | List memories. | none | `memory_list({})` | `lilbot/tools/builtin.py` | Core. |
| Memory | `memory_save` | Save persistent project memory. | `name`, `text` | `memory_save({"name":"style","text":"..."})` | `lilbot/tools/builtin.py` | Core. |
| Memory | `memory_search` | Search memories. | `query` | `memory_search({"query":"style"})` | `lilbot/tools/builtin.py` | Core. |
| Orchestration | `multi_tool_use_parallel` | Execute multiple LilBot tool calls and return structured results. | `tool_uses` | `multi_tool_use_parallel({"tool_uses":[...]})` | `lilbot/tools/builtin.py` | Phase 1 parallel wrapper. |
| Memory | `note` | Append a project note to memory. | `content` | `note({"content":"..."})` | `lilbot/tools/builtin.py` | Convenience alias. |
| UX | `notify` | Emit a lightweight notification message. | none | `notify({"message":"done"})` | `lilbot/tools/builtin.py` | Phase 1 local notification text. |
| Document | `pandoc_convert` | Convert documents through pandoc when installed. | `input`, `output` | `pandoc_convert({"input":"a.md","output":"a.docx"})` | `lilbot/tools/builtin.py` | Probe; depends on pandoc. |
| PR Workflow | `pr_attempt_list` | List recorded PR attempts. | none | `pr_attempt_list({})` | `lilbot/tools/builtin.py` | Phase 1 review workflow. |
| PR Workflow | `pr_attempt_preflight` | Run git apply --check for a recorded attempt. | `attempt_id` | `pr_attempt_preflight({"attempt_id":"..."})` | `lilbot/tools/builtin.py` | Phase 1. |
| PR Workflow | `pr_attempt_read` | Read a recorded PR attempt. | `attempt_id` | `pr_attempt_read({"attempt_id":"..."})` | `lilbot/tools/builtin.py` | Phase 1. |
| PR Workflow | `pr_attempt_record` | Record current git diff as a PR attempt. | none | `pr_attempt_record({})` | `lilbot/tools/builtin.py` | Phase 1. |
| Workspace | `project_map` | Summarize project directories and key source files. | none | `project_map({})` | `lilbot/tools/builtin.py` | Phase 1; framework summary still planned. |
| Workspace | `read_file` | Read a UTF-8 text file inside the workspace. | `path` | `read_file({"path":"file.py","lines":"1-80"})` | `lilbot/tools/builtin.py` | Core. |
| MCP | `read_mcp_resource` | Read a supported MCP resource. | `uri` | `read_mcp_resource({"uri":"..."})` | `lilbot/tools/builtin.py` | Phase 1. |
| Memory | `recall_archive` | Search local LilBot archive notes. | none | `recall_archive({"query":"..."})` | `lilbot/tools/builtin.py` | Phase 1 archive helper. |
| Memory | `remember` | Persist a durable memory. | `text` | `remember({"text":"..."})` | `lilbot/tools/builtin.py` | Compat; prefer `memory_save` for structured memory. |
| UX | `request_user_input` | Request interactive user input; currently reports unavailable inside tool calls. | none | `request_user_input({"question":"..."})` | `lilbot/tools/builtin.py` | Probe; real UI integration pending. |
| Handle | `retrieve_tool_result` | Read a stored path/handle for a prior large result. | none | `retrieve_tool_result({"handle":"..."})` | `lilbot/tools/builtin.py` | Core handle pattern. |
| Rewind | `revert_turn` | Report snapshot-based revert support status. | none | `revert_turn({})` | `lilbot/tools/builtin.py` | Probe; checkpoint system pending. |
| Review | `review` | Run a lightweight review scan. | none | `review({})` | `lilbot/tools/builtin.py` | Phase 1; stronger diff-aware review planned. |
| RLM | `rlm_close` | Close an RLM session. | `name` | `rlm_close({"name":"analysis"})` | `lilbot/tools/builtin.py` | Phase 1 Python analysis session. |
| RLM | `rlm_configure` | Update RLM session config. | `name` | `rlm_configure({"name":"analysis"})` | `lilbot/tools/builtin.py` | Phase 1. |
| RLM | `rlm_eval` | Execute Python in an RLM session. | `name`, `code` | `rlm_eval({"name":"analysis","code":"x=1"})` | `lilbot/tools/builtin.py` | Phase 1. |
| RLM | `rlm_open` | Open a lightweight Python analysis session. | none | `rlm_open({"name":"analysis"})` | `lilbot/tools/builtin.py` | Phase 1. |
| RLM | `rlm_session_objects` | List RLM sessions and symbolic objects. | none | `rlm_session_objects({})` | `lilbot/tools/builtin.py` | Phase 1. |
| Testing | `run_tests` | Run the local test command with permission approval. | none | `run_tests({"command":"python -m pytest -q"})` | `lilbot/tools/builtin.py` | Phase 1; richer classification planned. |
| Skill | `skill_list` | List available skills. | none | `skill_list({})` | `lilbot/tools/builtin.py` | Core. |
| Skill | `skill_run` | Render a skill template. | `name` | `skill_run({"name":"verify","args":"..."})` | `lilbot/tools/builtin.py` | Core. |
| Slop Ledger | `slop_ledger_append` | Append architectural residue to the slop ledger. | `title` | `slop_ledger_append({"title":"...","body":"..."})` | `lilbot/tools/builtin.py` | Phase 1 architecture debt ledger. |
| Slop Ledger | `slop_ledger_export` | Export the slop ledger as Markdown. | none | `slop_ledger_export({})` | `lilbot/tools/builtin.py` | Phase 1. |
| Slop Ledger | `slop_ledger_query` | Query the slop ledger. | none | `slop_ledger_query({"query":"..."})` | `lilbot/tools/builtin.py` | Phase 1. |
| Slop Ledger | `slop_ledger_update` | Update one slop ledger entry. | `id` | `slop_ledger_update({"id":"..."})` | `lilbot/tools/builtin.py` | Phase 1. |
| Task | `task_cancel` | Cancel a durable task record. | `task_id` | `task_cancel({"task_id":"..."})` | `lilbot/tools/builtin.py` | Phase 1. |
| Task | `task_create` | Create a durable task record. | none | `task_create({"prompt":"..."})` | `lilbot/tools/builtin.py` | Phase 1. |
| Task | `task_gate_run` | Run a verification command and attach gate evidence. | `command` | `task_gate_run({"command":"pytest"})` | `lilbot/tools/builtin.py` | Phase 1 verification gate. |
| Task | `task_list` | List durable task records. | none | `task_list({})` | `lilbot/tools/builtin.py` | Phase 1. |
| Task | `task_read` | Read a durable task record. | `task_id` | `task_read({"task_id":"..."})` | `lilbot/tools/builtin.py` | Phase 1. |
| Shell | `task_shell_start` | Start a long-running command in the background. | `command` | `task_shell_start({"command":"npm run dev"})` | `lilbot/tools/builtin.py` | Phase 1. |
| Shell | `task_shell_wait` | Wait for a task_shell_start command. | `task_id` | `task_shell_wait({"task_id":"..."})` | `lilbot/tools/builtin.py` | Phase 1. |
| Workflow | `todo_add` | Compatibility alias for checklist_add. | `content` | `todo_add({"content":"..."})` | `lilbot/tools/builtin.py` | Compat. |
| Workflow | `todo_list` | Compatibility alias for checklist_list. | none | `todo_list({})` | `lilbot/tools/builtin.py` | Compat. |
| Workflow | `todo_update` | Compatibility alias for checklist_update. | `id` | `todo_update({"id":"..."})` | `lilbot/tools/builtin.py` | Compat. |
| Workflow | `todo_write` | Compatibility alias for checklist_write. | `items` | `todo_write({"items":[...]})` | `lilbot/tools/builtin.py` | Compat. |
| Agent | `tool_agent` | Open a fast tool-bound subagent. | `prompt` | `tool_agent({"prompt":"..."})` | `lilbot/tools/builtin.py` | Core fast lane. |
| Discovery | `tool_search_tool_bm25` | Search registered tool names/descriptions with simple term scoring. | `query` | `tool_search_tool_bm25({"query":"git diff"})` | `lilbot/tools/builtin.py` | Phase 1 tool discovery. |
| Discovery | `tool_search_tool_regex` | Search registered tool names/descriptions with a regex. | `query` | `tool_search_tool_regex({"query":"git_.*"})` | `lilbot/tools/builtin.py` | Phase 1 tool discovery. |
| Goal | `update_goal` | Update active goal status. | `status` | `update_goal({"status":"complete"})` | `lilbot/tools/builtin.py` | Core. |
| Workflow | `update_plan` | Write high-level plan state. | `plan` | `update_plan({"plan":[...]})` | `lilbot/tools/builtin.py` | Core PM plan state. |
| Data | `validate_data` | Validate JSON/CSV/TSV data. | none | `validate_data({"path":"data.json"})` | `lilbot/tools/builtin.py` | Phase 1; schema-aware validation planned. |
| Web | `web_fetch` | Alias for fetch_url. | `url` | `web_fetch({"url":"https://..."})` | `lilbot/tools/builtin.py` | Compat. |
| Web | `web_run` | Compatibility web runner: search when given query, fetch when given url. | none | `web_run({"query":"..."})` | `lilbot/tools/builtin.py` | Compat web meta tool. |
| Web | `web_search` | Search the public web and return ranked results with URLs and snippets. | none | `web_search({"query":"..."})` | `lilbot/tools/builtin.py` | Phase 1; cite sources when used. |
| Workspace | `write_file` | Write a UTF-8 file inside the workspace. | `path`, `content` | `write_file({"path":"x.md","content":"..."})` | `lilbot/tools/builtin.py` | Core write tool. |

## My PM Notes

My current reading:

- LilBot is no longer an empty shell. The inventory is broad and several core
  flows are functional and tested.
- The biggest gap is no longer "quantity"; it is enforcement and lifecycle.
  The next serious quality jump is to make `allowed-tools`, forked skill
  execution, subagent transcripts, worktree isolation, LSP, and PowerShell
  safety real instead of just compatible names.
-  parity gave us breadth. LilBot origin gives us the next target:
  custom agents, skill ecosystem, task lifecycle, permissions, hooks, and
  product-level workflows.

Recommended next batch:

1. Enforce skill and subagent `allowed_tools` at runtime.
2. Implement true forked skill execution through subagents.
3. Persist subagent transcripts and expose transcript handles.
4. Add `EnterPlanMode` / `ExitPlanMode` plus plan approval state.
5. Add `EnterWorktree` / `ExitWorktree` with honest unsupported fallback on systems where git worktree is unavailable.
