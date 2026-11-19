# 子代理、技能与工具清单

> 基于 `main` @ `a941165` 的源码生成。目的：让接手者在一页内看清 LilBot 的能力面，而不是从 2 万行代码里逐个找。
> 相关文档：[架构总览](../architecture/overview.md) · [运行时调用链](../architecture/runtime-call-chain.md) · [团队设计](teams-explained.md)

## 1. 子代理

`SubAgentManager` 内置 11 种角色。`agent_open` / `Agent` / `Task` 会把当前可用角色、工具白名单和 when-to-use 渲染进工具描述，由父模型自主选择；`agent_spawn` 系列用于显式创建、查询、读取 transcript 和关闭。

| 角色 | 用途 | 写文件 | Shell | 工具白名单 |
|---|---|---:|---|---|
| `general` | 多步骤通用执行 | 是 | 完整 | 只读代码工具 + 写工具 + web + run_tests |
| `explore` | 只读探索、快速收集证据 | 否 | 只读 | 只读代码工具 + diagnostics |
| `researcher` | 公共事实、方案比较、多源综合 | 否 | 仅 web | web 工具 |
| `plan` | 拆解任务、风险与验证步骤 | 否 | 最小 | 无（只产出计划） |
| `review` | 找 bug、回归和缺失测试 | 否 | 只读 | 只读代码工具 + diagnostics |
| `writer` | 提纲、草稿、改写、风格适配 | 否 | 无 | 无 |
| `critic` | 评审计划、写作、建议和推理漏洞 | 否 | 最小 | 无 |
| `implementer` | 指定代码改动的最小实现与验证 | 是 | 完整 | 只读 + 写 + run_tests + bash |
| `verifier` | 只验证、报告 pass/fail，不修失败 | 否 | 测试 | 只读代码工具 + 诊断工具 |
| `tool_agent` | 简单查询和探测的快速执行器 | 否 | 受限 | 项目地图、目录、读文件、搜索、web、诊断 |
| `custom` | 调用方显式限定工具集的角色 | 调用方决定 | 调用方决定 | 调用方传入的 allowed_tools |

生命周期：创建（五道 gate 校验 name/description/prompt/tools/权限）→ 运行（信号量限制并发，默认 8）→ transcript 落盘 `.lilbot/subagent-transcripts/` → 可恢复（任务状态持久化）→ 可关闭。`isolation="worktree"` 时在独立 git worktree 中运行，结束后可 `WorktreeMergeBack`。

## 2. 技能

内置技能 27 个，放在 `lilbot/skills/bundled/`；用户技能放在 `.lilbot/skills/` 或项目 `.lilbot/skills/`。技能是带 frontmatter 的 Markdown，支持 `allowed-tools`、`mode: fork`、`model`、`when_to_use`、参数模板和伴随文件。

| 名称 | 模式 | 描述 | 文件 |
|---|---|---|---|
| `batch` | inline | Break a large request into a tracked batch of smaller tasks. | `lilbot/skills/bundled/batch/SKILL.md` |
| `commit` | inline | Prepare a concise commit plan and suggested commit message. | `lilbot/skills/bundled/commit.md` |
| `debug` | inline | Investigate a bug or failing behavior and isolate the likely cause. | `lilbot/skills/bundled/debug/SKILL.md` |
| `delegate` | inline | Use subagents to split exploration, implementation, review, and verification work. | `lilbot/skills/bundled/delegate/SKILL.md` |
| `documents` | inline | Create, inspect, edit, or convert document-style deliverables. | `lilbot/skills/bundled/documents/SKILL.md` |
| `feishu` | inline | Work with Feishu or Lark bots, docs, sheets, bitables, approval flows, and API setup. | `lilbot/skills/bundled/feishu/SKILL.md` |
| `keybindings-help` | inline | Explain or design keyboard shortcut bindings. | `lilbot/skills/bundled/keybindings-help/SKILL.md` |
| `loop` | inline | Set up a recurring check or repeated agent workflow. | `lilbot/skills/bundled/loop/SKILL.md` |
| `lorem-ipsum` | inline | Generate placeholder copy with controllable tone and length. | `lilbot/skills/bundled/lorem-ipsum/SKILL.md` |
| `mcp-builder` | inline | Design, build, configure, or debug Model Context Protocol servers. | `lilbot/skills/bundled/mcp-builder/SKILL.md` |
| `pdf` | inline | Read, extract, split, merge, rotate, watermark, fill, OCR, or create PDF files. | `lilbot/skills/bundled/pdf/SKILL.md` |
| `plan` | inline | Break a task into implementation steps, decisions, and verification. | `lilbot/skills/bundled/plan.md` |
| `plugin-creator` | inline | Scaffold or plan LilBot-style local plugins. | `lilbot/skills/bundled/plugin-creator/SKILL.md` |
| `presentations` | inline | Create, edit, inspect, or convert slide decks and PPTX-style presentations. | `lilbot/skills/bundled/presentations/SKILL.md` |
| `remember` | inline | Decide whether user information should become durable memory. | `lilbot/skills/bundled/remember/SKILL.md` |
| `review` | inline | Review code or a change request for bugs, risks, and missing tests. | `lilbot/skills/bundled/review.md` |
| `run-skill-generator` | inline | Generate a complete skill package from a short request. | `lilbot/skills/bundled/run-skill-generator/SKILL.md` |
| `schedule` | inline | Schedule a future task, reminder, or remote-style agent action. | `lilbot/skills/bundled/schedule/SKILL.md` |
| `simplify` | inline | Simplify code, prose, or a plan without changing its intent. | `lilbot/skills/bundled/simplify/SKILL.md` |
| `skill-creator` | inline | Create or improve LilBot skills and decide when a skill is the right abstraction. | `lilbot/skills/bundled/skill-creator/SKILL.md` |
| `skill-installer` | inline | Install, update, trust, or inspect local/community skill folders. | `lilbot/skills/bundled/skill-installer/SKILL.md` |
| `skillify` | inline | Turn a repeated workflow into a reusable LilBot SKILL.md. | `lilbot/skills/bundled/skillify/SKILL.md` |
| `spreadsheets` | inline | Create, inspect, clean, analyze, or convert spreadsheet and tabular files. | `lilbot/skills/bundled/spreadsheets/SKILL.md` |
| `stuck` | inline | Recover when progress stalls or the agent may be looping. | `lilbot/skills/bundled/stuck/SKILL.md` |
| `summarize` | inline | Summarize a topic, file, or tool result into terse engineering notes. | `lilbot/skills/bundled/summarize.md` |
| `update-config` | inline | Help inspect and update LilBot project or user configuration. | `lilbot/skills/bundled/update-config/SKILL.md` |
| `v4-best-practices` | inline | Use for DeepSeek V4-style multi-step or plan-driven tasks. | `lilbot/skills/bundled/v4-best-practices/SKILL.md` |
| `verify` | inline | Verify that implementation work is correct before reporting done. | `lilbot/skills/bundled/verify/SKILL.md` |

## 3. 工具分组（146 个内置工具）

| 组 | 代表工具 | 说明 |
|---|---|---|
| 工作区 | `list_dir` `read_file` `write_file` `edit_file` `apply_patch` `fim_edit` `revert_turn` | 读写、补丁、局部编辑和回合回滚 |
| 搜索 | `glob` `grep` `grep_files` `file_search` `project_map` `run_tests` `validate_data` | 一个剪枝 walker 驱动全部搜索；项目地图识别框架与入口 |
| Shell | `bash` `exec_shell` `exec_shell_wait` `exec_interact` `task_shell_start` `code_execution` `js_execution` | 前台/后台进程、交互式会话、取消与等待 |
| Git / GitHub | `git_status` `git_diff` `git_log` `git_show` `git_blame` `github_pr_context` `github_issue_context` | 只读 git 证据与 GitHub 上下文 |
| Worktree | `EnterWorktree` `ExitWorktree` `WorktreeMergeBack` `worktree_prune` | 隔离工作树与合并回主干 |
| LSP | `lsp_symbols` `lsp_definition` `lsp_references` `lsp_diagnostics` `lsp_rename_preview` | 有语言服务器时用 LSP，否则回退 AST/regex |
| Web | `web_search` `fetch_url` `web_fetch` `web_run` | 搜索、抓取、页面内脚本 |
| 记忆 | `memory_save` `memory_list` `memory_search` `memory_delete` `recall_archive` | 文件记忆 + 压缩周期归档 |
| 技能 | `skill_list` `skill_run` `Skill` `load_skill` | 技能发现、加载和执行 |
| 子代理 | `agent_open` `Agent` `Task` `agent_eval` `agent_close` `plan_delegation` | 创建、评估、关闭子代理 |
| 团队 | `team_create` `send_message` `team_task_create` `team_task_update` | 团队、信箱和共享任务看板 |
| 计划 | `EnterPlanMode` `ExitPlanMode` `checklist_*` `todo_*` `create_goal` `update_goal` | 计划模式、清单和目标 |
| PR / 自动化 | `pr_attempt_*` `automation_*` | PR 尝试记录与定时任务 |
| MCP | `mcp_servers` `mcp_call` `list_mcp_resources` `read_mcp_resource` | MCP 客户端；`mcp__<server>__<tool>` 作为延迟工具注册 |
| 媒体 / 交互 | `pandoc_convert` `image_ocr` `image_analyze` `notify` `request_user_input` | 文档转换、图像理解、通知和人工确认 |
| 工具发现 | `ToolSearch` `tool_search_tool_regex` `tool_search_tool_bm25` `multi_tool_use_parallel` | 延迟工具检索和并行包装 |

工具契约（`ToolDef`）：JSON Schema、能力集合、审批级别、`validateInput`、逐输入 `isConcurrencySafe` / `isDestructive`、`maxResultSizeChars`、`should_defer`、`searchHint`、`contextModifier`。权限由 `ToolRegistry.execute` 统一收口，工具 handler 不再各自实现审批。

## 4. Hooks

`hooks.json` 热加载，支持 7 个事件：`session_start`、`turn_start`、`user_prompt_submit`、`pre_tool_use`、`post_tool_use`、`stop`、`turn_end`。

结构化输出字段：`decision`（allow/deny/ask）、`updatedInput`（改写工具入参）、`additionalContext`（注入上下文）、`continue`（stop hook 阻止停止并强制续跑）、`systemMessage`。命令非零退出可阻断；`stop` hook 带死亡螺旋熔断，避免 hook 把模型拖进无限续跑。

## 5. MCP

- 客户端：启动时连接 `.lilbot/mcp.json` 中配置的 server，`initialize` → `tools/list` → `tools/call`，每个工具注册为 `mcp__<server>__<tool>` 的延迟工具。
- 服务端：`python -m lilbot --mcp-server` 用 stdio JSON-RPC 把 LilBot 工具暴露给其他客户端，默认只暴露只读工具，可用 `.lilbot/mcp_server.json` 的 `expose_tools` 调整。

## 6. 权限与隔离

- 模式：`ask` / `accept-all` / `deny-all`。
- 规则：`Tool(pattern)` 语法，按来源分层（policy / project / user），`deny > ask > allow`，并检测被遮蔽的规则。
- Shell：灾难命令先 deny；已知只读命令用 arity 感知前缀匹配放行；PowerShell 再做令牌级风险分析（分隔符、重定向、子进程、后台、删除/移动目标）。
- 密钥：模型发起的子进程默认剥离 `*_KEY` / `*_TOKEN` / `*_SECRET` / `*_PASSWORD`；读取密钥变量、列出全部环境变量、读 `.env` 的命令永不自动放行；工具输出回灌模型前替换 LilBot 持有的密钥原值。
- 沙箱：路径必须落在 workspace 内；不是 OS 级隔离，生产环境应再加容器/虚拟机边界。
