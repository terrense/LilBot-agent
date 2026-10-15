# LilBot 架构总览（Week 1）

> 基于 `main` @ `32be16c`（2026-07-08）的真实代码。目的：让接手者不依赖原作者也能看懂并调试 LilBot。
> 配套文档：[运行时调用链](runtime-call-chain.md) · [模块清单](module-inventory.md) · [基线测试报告](baseline-test-report.md)

## 1. 一句话定位

LilBot 是一个 **Windows 优先、模型可替换（任意 OpenAI 兼容接口）的本地 Coding Agent Harness**。它有完整的 ReAct 主循环和较成熟的工具、上下文、子代理体系，但还**不是**能够跨崩溃恢复执行、具备 OS 级隔离的生产级 Runtime。

## 2. 分层结构

```text
┌──────────────────────────────────────────────────────────┐
│ 入口层   cli.py  ·  tui/dashboard.py  ·  tui/classic.py   │
│          --print 单次 · 交互 TUI · --mcp-server           │
├──────────────────────────────────────────────────────────┤
│ 主循环   core/agent.py  Agent.run_turn()  (生成器，yield 事件) │
│   ├ 上下文  compaction · tool_budget · session_memory · cycles │
│   ├ 记忆    memory/recall · memory/extract（后台线程）        │
│   ├ 钩子    hooks/engine（pre/post tool, stop, turn…）       │
│   └ 持久化  session（回合末）· eventlog · history（文件快照）  │
├──────────────────────────────────────────────────────────┤
│ 模型层   llm/providers.py  OpenAICompatible / RuleBased(离线) │
├──────────────────────────────────────────────────────────┤
│ 工具层   tools/registry.py  校验 → 计划闸门 → handler → 落盘   │
│          tools/builtin.py   146 个内置工具                   │
│          mcp/               外部 MCP 工具（deferred）         │
│          subagents/ teams/  子代理与长驻队友（线程内）           │
├──────────────────────────────────────────────────────────┤
│ 安全层   sandbox/permissions · permission_rules · execpolicy │
│          · powershell_safety · workspace(路径约束+subprocess) │
│          security/secrets（仅显示层脱敏）                     │
└──────────────────────────────────────────────────────────┘
          状态目录：<workspace>/.lilbot/
```

## 3. 关键设计（值得保留）

1. **事件流主循环**：`run_turn()` 只产出事件，UI 只消费事件。新增入口（API、Gateway）可以复用同一个循环。
2. **ToolDef 契约**：工具声明 schema、能力、并发安全性、是否需要审批、是否具破坏性、输出上限。并行执行、子代理白名单、遥测都基于这份契约。
3. **Deferred 工具 + ToolSearch**：只常驻核心工具 schema，其余按需加载，节省上下文。
4. **多级上下文管理**：L0 工具结果预算 → 裁剪 → 结构化摘要 → 压缩后恢复（文件、技能、session 笔记）→ 溢出时被动压缩重试 → 熔断。
5. **只读工具自动并行**：相邻的并发安全调用合批，交给线程池执行。
6. **模型无关**：任何 OpenAI 兼容端点都能接入，实测 MiniMax-M3（中国区）无需改代码。

## 4. 审计报告结论核对

对照 ChatGPT 审计报告（2026-10）逐条核对真实代码：

| 报告结论 | 核对结果 | 证据 |
|---|---|---|
| `builtin.py` 约 4600 行 | ✅ 属实，4605 行 / 146 个工具 | `wc -l`、`grep -c "ToolDef("` |
| `agent.py` 超 1200 行，职责过多 | ✅ 属实，1238 行 | 见模块清单 |
| SessionStore 只存 JSON，回合结束才保存 | ✅ 属实 | `agent.py:304,355`；`session.py` |
| 回合中途崩溃无法精确恢复执行 | ✅ 属实；回合内的消息全部丢失，已发生的副作用不可追踪 | 见调用链第 5 节 |
| EventLog 不是权威状态源 | ✅ 属实，best-effort 追加写，恢复时不读取 | `eventlog.py` |
| Shell 不是 OS 级隔离 | ✅ 属实；`subprocess.run(cwd=root)`，可访问 workspace 外部 | `workspace.py:102` |
| Teams 是 in-process 线程实现 | ✅ 属实 | `teams/spawn_inprocess.py`、`teams/manager.py` docstring |
| 共享任务看板有依赖字段但不是 DAG 调度器 | ✅ 属实 | `teams/shared_task.py` |
| `delegation.py` 含已停用的关键词路由 | ✅ 属实，标记 DEPRECATED，但仍有部分引用 | 见模块清单 |
| 报告流程图中 "Permission Check" 是主循环里的统一步骤 | ❌ **不准确**：registry 只做校验和计划闸门，权限由各工具 handler 自行检查 | `registry.py:417`、`builtin.py:195-221` |
| 历史文档记录测试数 184 → 257 → 304 | ✅ 现为 **353 passed / 6 skipped**，比文档记录还多 | 基线测试报告 |

## 5. 报告未提到、本次新发现的问题

按严重程度排序：

### S1 · 高 — API Key 可被 Agent 读取且无需审批

- `config.load_dotenv()` 把 `.env` 写入 `os.environ`；`Sandbox.run()` 启动子进程时**继承完整环境**（没有传 `env=`）。
- `execpolicy.classify()` 把 `echo $env:LILBOT_API_KEY`、`cat .env` 判为"已知安全只读命令"→ 直接 `allow`。
- 实测：通过 `Sandbox.run()` 执行 `$env:LILBOT_API_KEY.Length` 返回 125，说明密钥可见。
- `secrets.py` 的脱敏只作用于显示层，模型上下文和 `.lilbot/sessions/*.json` 里保存的是原始值。
- **风险**：如果仓库里的 README、网页或 MCP 输出中藏有提示注入，模型可能把密钥读出来，再通过另一条命令（需要一次审批）外发。
- **已修复（2026-10-15，`lilbot/security/env.py`）**，三层防护：
  1. 由模型决定命令的子进程（shell、后台进程、code/js 执行）去掉密钥类环境变量。采用**黑名单**（`*_KEY`、`*_TOKEN`、`*_SECRET`、`*_PASSWORD` 等）而非白名单，因为 Windows 上的 PowerShell 和开发工具依赖大量环境变量，白名单容易把它们弄坏；如需透传，用 `LILBOT_CHILD_ENV_ALLOW=NAME1,NAME2`。hooks 和 MCP server 由用户自行配置，保留完整环境。
  2. `execpolicy`：读取密钥变量、列出全部环境变量、读 `.env` 或凭据文件的命令永远不会自动放行，过宽的用户 allow 规则（如 `Bash(cat *)`）也不能放行它们；用户的 deny 规则依然生效。
  3. `ToolRegistry.execute()`：所有工具输出在交给模型前，把 LilBot 持有的密钥**原值**替换为 `[REDACTED:<变量名>]`，覆盖 `read_file`、`grep` 和 MCP 等所有工具。
  - 测试：`tests/test_credential_isolation.py`。S2 中涉及密钥的命令在 `deny-all` 下也已被拒绝。

### S2 · 中 — `deny-all` 模式并不会拒绝所有命令

`_shell_permission()`（builtin.py:213）在 `execpolicy` 判为 `allow` 时**直接返回放行**，根本不经过 `PermissionManager.check()`。所以即使使用 `--permission-mode deny-all`，"安全只读"命令照样执行。要么修改实现，要么在文档中把 deny-all 的语义说明为"拒绝所有需要审批的操作"。

### S3 · 中 — 权限检查分散在各个 handler

见调用链第 3 节。新增工具如果忘记调用 `_permission()`，就会完全没有权限控制。Week 3 的工具生命周期改造应把授权收拢到 `ToolRegistry.execute()`，根据 `ToolDef.approval` / 能力标记统一判定。

### S4 · 中 — MiniMax 的 `<think>` 内联输出（基线测试 B1）

推理文本混在 `content` 里，会显示给用户、写入历史，并在后续每次请求中重复发送。

### S5 · 低 — Provider 无请求级重试

`OpenAICompatibleProvider` 遇到 429/5xx/网络错误直接抛 `ProviderError`，`run_turn()` 不捕获，本回合就此中断且不持久化（与 S1 无关，但会放大 Durable Execution 的问题）。

### S6 · 低 — 仓库卫生

`pip install -e .` 生成的 `lilbot_agent.egg-info/` 未被 `.gitignore` 忽略；根目录有 `Untitled`、`hello.txt`、`hello002.txt` 等残留文件。

## 6. 对六周路线的影响

- **Week 2（Durable Runtime）**：核对结果完全支持按原计划进行。建议把"回合内增量持久化（每次工具结果后写 checkpoint）"作为 MVP 的第一个验收点。
- **Week 3（安全）**：把 S1/S2/S3 列为本周必做项，**S1 的优先级高于 Docker 后端**：环境变量白名单只需改几行，却能消除当前最直接的风险。
- **B1/S4（`<think>`）**：改动小、影响日常使用体验，建议在 Week 1 收尾或 Week 2 开头单独开一个小分支修复。
