# 模块清单（Week 1）

> 基于 `main` @ `32be16c`。共约 18,400 行 Python（不含测试）。行数为 `wc -l`。
> "状态"列：✅ 在主链路中使用 ｜ ⚠️ 需关注 ｜ 🗑️ 已弃用/候选删除。

## 入口与配置

| 文件 | 行数 | 职责 | 状态 |
|---|---|---|---|
| `__main__.py` | 6 | `python -m lilbot` → `cli.main()` | ✅ |
| `cli.py` | 792 | 参数解析、`build_runtime()` 组装依赖、slash 命令、交互循环、`--print` 单次模式、MCP server 模式 | ✅ |
| `config.py` | 158 | `LilBotConfig`；读取 `.env`（不覆盖已有环境变量）→ 环境变量 → `.lilbot/config.json` | ✅ |

## core/ — Agent 主循环与上下文

| 文件 | 行数 | 职责 | 状态 |
|---|---|---|---|
| `agent.py` | 1238 | `Agent.run_turn()` ReAct 主循环；同时负责压缩触发、记忆召回/抽取、hooks、session 持久化、团队通知、usage/cache 统计、溢出与截断恢复 | ⚠️ 职责过多（God Object 倾向），新功能不要再往这里塞 |
| `compaction.py` | 734 | 两层上下文压缩（裁剪旧工具输出 + 结构化摘要）+ 压缩后恢复 + 熔断器 | ✅ |
| `tool_budget.py` | 157 | L0 单轮工具结果预算（frozen/fresh/replaced 状态机），移植自 Claude Code | ✅ |
| `session.py` | 99 | `SessionStore`：每个会话一个 JSON，回合结束时原子写 | ✅ 仅恢复对话，不恢复执行 |
| `session_memory.py` | 109 | `.lilbot/session-memory.md` 结构化笔记，后台更新，跨压缩保留 | ✅ |
| `history.py` | 123 | `FileHistory`：写文件前快照，支持 `/rewind` | ✅ 只能回滚文件，不能回滚 shell/网络副作用 |
| `cycles.py` | 73 | 每次摘要压缩归档为一个 cycle，可用 `recall_archive` 找回 | ✅ |
| `eventlog.py` | 71 | `.lilbot/events.jsonl` 结构化事件（追加写，best-effort） | ✅ 不是权威状态源 |
| `events.py` | 83 | `run_turn` 产出的 UI 事件类型（TextDelta / ToolStarted / ToolFinished / TurnFinished）及 `ToolCall`、`ProviderTurn` | ✅ |
| `prompts.py` | 31 | 构建 system prompt | ✅ |
| `constitution.py` | 266 | 分层行为规则（"宪法"） | ✅ |
| `delegation.py` | 949 | 旧的关键词/语义自动委派。docstring 标明 **DEPRECATED**，`_auto_delegate()` 恒返回 0 | ⚠️ 主循环已不用，但 `builtin.py:3023` 仍引用其中的 `SEMANTIC_DELEGATION_SYSTEM_PROMPT`，`agent.py` 仍 import，测试与 `experiment/` 也依赖。不能直接删：先把仍在用的部分抽出，再归档其余部分；对应 6 个跳过测试 |

## llm/

| 文件 | 行数 | 职责 | 状态 |
|---|---|---|---|
| `providers.py` | 521 | `OpenAICompatibleProvider`（同步 + SSE 流式）、`RuleBasedProvider`（离线）、`KNOWN_PROVIDERS` 预设、`choose_provider()`；消息清洗（丢弃不完整的 tool_call 块） | ⚠️ 不识别内联 `<think>`（MiniMax）；无请求重试/退避 |

## tools/

| 文件 | 行数 | 职责 | 状态 |
|---|---|---|---|
| `registry.py` | 501 | `ToolDef` / `ToolRegistry` / `ToolContext` / `ToolResult`；能力标记、校验、并发安全判定、deferred 工具与 ToolSearch、计划审批闸门、统一异常包装、大输出落盘 | ✅ 设计良好 |
| `builtin.py` | 4605 | **146 个**内置工具的定义与实现（文件、shell、git、worktree、LSP、web、记忆、技能、子代理、团队、计划模式……） | ⚠️ 过大，按工具组逐步拆分 |
| `offload.py` | 98 | 超长工具输出写入磁盘，只回传预览 | ✅ |

## sandbox/ 与 security/

| 文件 | 行数 | 职责 | 状态 |
|---|---|---|---|
| `workspace.py` | 122 | `Sandbox`：路径必须在 workspace 内；`run()` 用 `subprocess` 执行（Windows: PowerShell），**继承完整环境变量** | ⚠️ 不是 OS 级隔离 |
| `permissions.py` | 69 | `PermissionManager`：ask / accept-all / deny-all，"always"规则持久化 | ✅ |
| `permission_rules.py` | 140 | `Tool(pattern)` 分层规则（对标 CC） | ✅ |
| `execpolicy.py` | 148 | 命令分类：灾难性命令 deny、已知只读命令 allow | ⚠️ `echo $env:…`、`cat .env` 被判为安全只读 |
| `powershell_safety.py` | 388 | PowerShell 语法级风险分析（分隔符、重定向、子进程、后台、删除/移动目标） | ✅ |
| `security/secrets.py` | 89 | 密钥脱敏，**只作用于显示层**；模型上下文与 session 文件保留原值 | ⚠️ 设计如此，需在安全模型中说明 |

## subagents/ 与 teams/

| 文件 | 行数 | 职责 | 状态 |
|---|---|---|---|
| `subagents/manager.py` | 1429 | 子代理类型（内置 + Markdown 自定义）、工具白名单、并发上限、后台运行、transcript、恢复、worktree 隔离 | ✅ |
| `subagents/render.py` | 96 | 把可用子代理类型渲染进 Agent 工具描述，由模型自主选择 | ✅ |
| `teams/manager.py` | 250 | 团队生命周期、持久化、通知路由；**仅 in-process 后端** | ✅ |
| `teams/spawn_inprocess.py` | 129 | 队友以 `threading.Thread` 长驻运行 | ✅ 非分布式 |
| `teams/mailbox.py` | 184 | 基于文件 + 锁的代理间信箱 | ✅ |
| `teams/shared_task.py` | 145 | JSON 任务看板，含 `blocks` / `blocked_by` 依赖 | ✅ 不是 DAG 调度器 |
| `teams/models.py` / `progress.py` / `registry.py` | 147 / 109 / 47 | 数据模型、进度、名字寻址 | ✅ |

## memory/、skills/、hooks/、mcp/

| 文件 | 行数 | 职责 | 状态 |
|---|---|---|---|
| `memory/file_store.py` | 200 | 当前记忆存储：每条记忆一个 `.md`，分 user / project | ✅ |
| `memory/store.py` | 97 | 旧 JSONL 存储，启动时一次性迁移 | 🗑️ 仅迁移用 |
| `memory/recall.py` | 168 | LLM 选择相关记忆 + 新鲜度 | ✅ |
| `memory/extract.py` | 104 | 定期从对话中抽取记忆 | ⚠️ 无来源/置信度/人工审核 |
| `skills/registry.py` | 232 | 加载技能 | ✅ 无经验→技能闭环 |
| `hooks/engine.py` / `loader.py` / `models.py` | 194 / 69 / 95 | 生命周期 hooks（pre/post tool、stop、turn 等），hooks.json 热加载 | ✅ |
| `mcp/client.py` / `manager.py` | 165 / 167 | 同步 JSON-RPC stdio MCP 客户端，工具注册为 `mcp__<server>__<tool>` | ✅ |
| `mcp/server.py` | 124 | 把 LilBot 工具以 MCP 暴露给其他客户端（默认只读） | ✅ |

## tui/

| 文件 | 行数 | 职责 | 状态 |
|---|---|---|---|
| `dashboard.py` | 2003 | 默认全屏 TUI（prompt_toolkit） | ✅ 报告建议近期不投入美化 |
| `classic.py` | 304 | 经典 Rich 打印界面，带密钥脱敏 | ✅ |
| `input_history.py` / `windows_console.py` | 64 / 142 | 输入历史、Windows 控制台字体/编码 | ✅ |

## 仓库根目录杂项（候选清理）

`Untitled`、`hello.txt`、`hello002.txt` 看起来是调试残留；`SPEC_TEAMS_AND_TEAMMATES.md` 可移入 `docs/`。清理前需和 terrense 确认。
