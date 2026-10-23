# LilBot vs Hermes Agent 差距分析报告

- 日期：2026-10-23
- 对比对象：
  - **LilBot**：`B:\娜样的武装部\LilBot-agent`，分支 `dev/nayang` @ `67ebb35`
  - **Hermes Agent**：`B:\娜样的武装部\hermes-agent-main`（NousResearch 开源项目，v0.14 前后的快照）
- 方法：逐文件阅读两边的核心代码；在本机（Windows 10 / Python 3.11）上对 LilBot 做了 4 项微基准测试；跑 LilBot 全量测试作为基线。
- 测试基线：**417 passed / 6 skipped / 0 failed**（20.4 s）

> 结论先行：LilBot 的**主循环设计**（事件流、ToolDef 契约、deferred 工具、多级压缩）已经接近 Hermes 和 Claude Code 的水平，有几处甚至更细。差距主要在**运行时的工程化**：连接复用、请求重试和故障转移、中文 token 估算、搜索性能、回合内持久化、工具循环护栏、权限收拢。这些都不需要大改架构，适合按 7 天逐项补齐。

---

## 0. 规模对比

| 指标 | LilBot | Hermes | 说明 |
|---|---|---|---|
| Python 源码行数（不含测试） | ~19,000 | ~240,000 | Hermes 大约是 LilBot 的 12 倍 |
| 主循环文件 | `core/agent.py` 1,238 行 | `run_agent.py` ~12k 行 + `agent/conversation_loop.py` 4,094 行 | Hermes 已把主循环拆成几十个 `agent/*.py` 模块 |
| 工具数 | 146 个（集中在 `builtin.py` 一个文件，4,609 行） | 72 处 `registry.register`，分散在 79 个 `tools/*.py` 文件，并按 toolset 组织 | LilBot 工具数量多，但很多是别名或兼容名 |
| 模型接入 | 1 种协议（OpenAI Chat Completions）+ 23 个 base_url 预设 | 30 个 model-provider 插件，原生 Anthropic / Bedrock / Gemini / Codex Responses 适配器 | |
| 测试 | 37 个文件 / 417 个用例 | ~1,184 个测试文件 / ~17k 个用例 | |
| 运行入口 | CLI、全屏 TUI、`--print`、MCP server | CLI、Ink TUI、Web Dashboard、20+ 消息平台网关、ACP（IDE）、Cron、Batch | |
| 依赖 | 3 个（httpx / prompt_toolkit / rich），只设下限 | 依赖全部设置上界，`uv.lock` 带哈希 | |

规模本身不是目标。下面只讨论**对 LilBot 有借鉴价值**的差距。

---

## 1. 框架 / 架构层面

### 1.1 主循环

| 维度 | LilBot | Hermes | 差距 |
|---|---|---|---|
| 循环形态 | 生成器 `run_turn()` yield 事件，UI 只消费事件 ✅ | 同步 `run_conversation()` + 大量 callback | **LilBot 更干净**，保留 |
| 步数预算 | `max_steps=20`，每个工具调用算 1 步 | `max_iterations=90`，`IterationBudget` 线程安全，可退还（`execute_code` 不计步），父子 agent 各自独立预算，耗尽后有一次“grace call” | 中 |
| 中断 | 无。Ctrl+C 只能杀进程，回合内的状态全部丢失 | `tools/interrupt.py`：按线程设置中断标志，工具和重试等待都会检查，中断后回合能干净收尾 | **高** |
| 职责划分 | `agent.py` 同时管压缩、记忆、hooks、持久化、团队、缓存统计（God Object） | `agent/` 下拆成 `conversation_loop`、`tool_executor`、`context_compressor`、`memory_manager`、`error_classifier`… | 中（可维护性） |

### 1.2 Provider / 模型层（**差距最大的一块**）

| 维度 | LilBot (`llm/providers.py`) | Hermes | 差距 |
|---|---|---|---|
| HTTP 连接 | **每次请求 `with httpx.Client(...)` 新建并关闭**。没有 keep-alive，每次都要重新 TCP + TLS 握手，并重新加载 certifi 证书包 | 客户端按 (provider, base_url, key) 缓存复用（`auxiliary_client._client_cache`），带连接池 | **高（性能）** |
| 错误分类 | `ProviderError(status_code, is_overflow)`，只区分“是否上下文溢出” | `error_classifier.py`：`FailoverReason` 枚举 17 类（auth / billing / rate_limit / overloaded / server_error / timeout / context_overflow / model_not_found / format_error…），每类对应一种恢复动作 | **高** |
| 重试 | **主循环没有任何重试**：一次 429/5xx/断网，整个回合直接抛异常中断（WORKLOG 记录的 S5） | `jittered_backoff` 指数退避加抖动（5s → 120s），默认重试 3 次；中断时立即退出等待 | **高（可靠性）** |
| 故障转移 | 无 | `fallback_model`：主模型重试耗尽后自动切换到备用 provider/model；`credential_pool` 支持同一 provider 多个 key 轮换 | 中 |
| 限流感知 | 无 | `rate_limit_tracker.py` 解析 `x-ratelimit-*` 响应头，在 `/usage` 里展示 | 低 |
| 辅助模型 | 记忆召回、记忆抽取、session 笔记、压缩摘要**全部用主模型** | `auxiliary_client`：压缩、标题、视觉、记忆审查等辅助任务可以分别指定更便宜、更快的模型 | 中（成本和延迟） |
| 原生协议 | 只支持 OpenAI Chat Completions | 原生 Anthropic（含 `cache_control` 断点策略）、Bedrock、Gemini、Codex Responses | 低（当前用 MiniMax 不受影响） |

**实测：HTTP 客户端开销**

```text
httpx.Client 创建+关闭（不含 TLS 握手）：  813 ms / 次
  其中 ssl.create_default_context(certifi)： 273 ms
  复用同一个 SSLContext 后创建 Client：      0.2 ms
```

一个 20 步的回合会调用主模型约 20 次，再加上召回、抽取、session 笔记这些辅助调用，**每回合仅建连接就浪费约 15–20 秒**。在中国区调用 MiniMax 时，还要另外加上每次的 TCP + TLS 握手（通常 100–300 ms）。

### 1.3 工具层

| 维度 | LilBot | Hermes | 差距 |
|---|---|---|---|
| 契约 | `ToolDef`：capability、approval、并发安全（可按参数判断）、destructive、validate、max_result_chars ✅ | `registry.register(name, toolset, schema, handler, check_fn, requires_env)` | **LilBot 的契约更丰富**，保留 |
| 组织方式 | 146 个工具全部在 `builtin.py`（4,609 行） | 每个工具一个文件，自动发现；按 **toolset** 分组，可以按平台启用或禁用 | 中（可维护性） |
| 可用性检查 | 无。比如没装 `gh` 也会注册 github 工具，调用时才失败 | `check_fn` / `requires_env`：缺依赖的工具不会暴露给模型 | 中（减少无效调用） |
| 权限 | **分散在各个 handler 里**，约 21 处 `_permission()` 调用；忘记调用就等于没有权限控制（S3） | `approval.py` 统一处理，在 `tool_executor` 里执行，按命令危险模式检测 | **高（安全）** |
| 循环护栏 | 无。模型可以反复用相同参数调用同一个失败的工具，直到步数耗尽 | `tool_guardrails.py`：相同调用失败 2 次警告、5 次阻断；同一工具失败 3 次警告、8 次停止；幂等工具“无进展”检测 | **高（省 token 和步数）** |
| 工具超时 | shell 有 timeout（≤120 s）；其他工具没有统一超时 | 每种 terminal backend 有超时；delegate 有 `child_timeout_seconds`；cron 有 3 分钟硬中断 | 中 |
| 大输出 | `offload.py` 写盘 + 预览；`tool_budget.py` L0 预算 ✅ | `tool_result_storage.py` 写盘 + `enforce_turn_budget` | 持平 |
| 并行执行 | 相邻的只读调用合批，用线程池执行 ✅ | `_PARALLEL_SAFE_TOOLS` / `_NEVER_PARALLEL_TOOLS`，最多 8 个 worker | 持平 |

**实测：`grep` 工具**

```text
在 LilBot 仓库根目录 grep 一个不存在的字符串：1,739 ms（rglob 遍历了 4,431 个条目，包括 .venv 里的 3,800 多个文件）
限定到 lilbot/ 子目录：                       113 ms
```

原因：`_grep` 先用 `base.rglob("*")` 遍历全部文件，**再**用 `_is_noisy_path` 过滤。被过滤的 `.venv` / `node_modules` / `.git` 仍然会被完整遍历一遍。Hermes 的做法是优先调用 ripgrep（`rg`），它会遵守 `.gitignore`、跳过隐藏目录，找不到 `rg` 时再回退到 `grep` 或 `find`。

### 1.4 上下文与压缩

| 维度 | LilBot | Hermes | 差距 |
|---|---|---|---|
| 多级压缩 | L0 工具结果预算 → 剪掉旧工具输出 → 结构化摘要 → 压缩后恢复（文件、技能、笔记）→ 溢出时被动压缩 → 熔断 ✅ | 先剪工具输出，再做 LLM 摘要；迭代式摘要；按 token 预算保护尾部 | **持平，LilBot 某些方面更细** |
| token 估算 | `len(text) / 4` | `estimate_messages_tokens_rough`，图片按固定值计，并结合模型元数据 | **高（对中文用户）** |
| 摘要模型 | 主模型 | 辅助模型（可配置成便宜模型） | 中 |
| 缓存保护 | 系统提示稳定，临时提示放在尾部，工具目录做了字节稳定缓存 ✅ | “Prompt Caching Must Not Break”作为硬性规则写进开发文档；记忆采用冻结快照 | 持平 |

**实测：中文 token 估算**

```text
中文 1,450 字 → LilBot 估算 362 tokens；实际 BPE 约 1,000–1,450 tokens（低估 3–4 倍）
英文 3,600 字符 → 估算 900 tokens（基本准确）
```

后果：在中文对话里，压缩的触发阈值相当于被放大了 3–4 倍。实际已经接近上下文上限时，估算值还远低于阈值，结果是**先撞上 413 / context_length_exceeded，才触发被动压缩**（多一次失败请求，还要多一次摘要调用）。`agent.py` 用真实的 `prompt_tokens` 做了部分校正，但回合内新增的工具输出仍然按 /4 估算。

### 1.5 持久化与恢复

| 维度 | LilBot | Hermes | 差距 |
|---|---|---|---|
| 会话存储 | 每个会话一个 JSON 文件，**只在回合结束时写入** | SQLite（`hermes_state.py`，WAL 模式，多读一写），逐条消息写入 | **高** |
| 回合内崩溃 | 这一回合的所有模型输出和工具结果全部丢失；副作用已经发生，但无从得知做到了哪一步 | 消息已经落库；后台进程有 JSON checkpoint，可以恢复 | **高** |
| 历史检索 | 无（`/resume` 只能按 id 恢复整个会话） | `session_search`：FTS5 全文检索所有历史会话，零 LLM 成本，支持锚点上下文滚动 | 中 |
| 文件回滚 | `FileHistory` 写前快照，`/rewind` ✅ | `checkpoint_manager.py`：影子 git 仓库，跨项目去重，自动清理 | 持平（Hermes 更省空间） |
| 事件日志 | `events.jsonl`，best-effort | `agent.log` / `errors.log` 分级 + `hermes logs` 命令 | 中（LilBot 几乎没有用 `logging`，全库只有 2 处） |

### 1.6 可观测性与错误处理

- LilBot 有 47 处 `except Exception:` 后什么都不做，33 处裸 `pass`。持久化、记忆抽取、MCP 连接失败都会被静默吞掉，出问题时没有任何线索。
- Hermes 用统一的 `hermes_logging` 写 `agent.log` / `errors.log`，另外有 `insights.py`（使用洞察）、`usage_pricing.py`（成本）、`stream_diag.py`（流式诊断）。

---

## 2. 功能层面

| 功能 | LilBot | Hermes | 建议 |
|---|---|---|---|
| 长期记忆 | 文件型记忆（每条一个 `.md`）+ LLM 选择召回 + 新鲜度提示 + 后台抽取 ✅ | `MEMORY.md` / `USER.md` 冻结快照 + 8 种外部记忆插件（mem0、honcho…） | 先不追插件。可以借鉴“冻结快照”这一点保护缓存 |
| 自我改进 | 无“经验 → 技能”闭环 | `background_review.py`：每回合结束后 fork 一个 agent 审查本回合，自动写入或更新 memory 和 skill；`curator.py` 定期清理过时的 skill | 中期可以做 |
| 子代理 | 内置 + Markdown 自定义类型，工具白名单，后台运行，transcript，worktree 隔离 ✅ | `delegate_task`：leaf / orchestrator 两种角色，最大深度 2，并发 3，子代理超时 | **LilBot 更丰富**；缺少子代理超时和深度限制 |
| 多代理协作 | Teams：线程内长驻队友 + 文件信箱 + 任务看板（不是 DAG） | Kanban：SQLite 看板 + dispatcher + 失败 N 次自动 block | 低优先级 |
| 定时任务 | 有 `schedule` / `loop` 技能，没有真正的调度器 | `cron/`：调度器、文件锁、补跑窗口、3 分钟硬中断 | 低 |
| 执行环境 | 本机 PowerShell / shell（只做了路径约束，不是 OS 级隔离） | local / docker / ssh / modal / daytona / singularity / vercel 沙箱 | 中（Docker 后端是 Week 3 计划） |
| 消息平台 | 无 | 20+ 平台（含飞书、企业微信、钉钉、QQ、微信） | 不在本次范围 |
| IDE 集成 | MCP server（只读工具） | ACP 适配器（VS Code / Zed / JetBrains） | 低 |
| MCP 客户端 | 只支持 stdio，同步 JSON-RPC，**子进程继承完整环境变量** | stdio + HTTP，支持 OAuth | 中 |
| Web 搜索 | 抓取 DuckDuckGo / Bing 的 HTML 页面并解析 | 可插拔的 `web_search_provider`（多后端） | 中（HTML 抓取容易失效） |
| 浏览器、视觉、TTS、图像生成 | 无 | 有 | 不在本次范围 |

---

## 3. 性能层面汇总（按收益排序）

| # | 问题 | 位置 | 实测或估计影响 | Hermes 参考 | 计划 |
|---|---|---|---|---|---|
| P1 | 每次 LLM 请求都新建 httpx.Client（重新加载证书 + 重新握手） | `llm/providers.py:394,458` | **~0.8 s / 次**，20 步的回合浪费约 15–20 s | `auxiliary_client._client_cache` | Day 1 |
| P2 | 没有请求重试，429/5xx 直接让回合失败 | `providers.py`、`agent.py:_stream_turn` | 失败的回合需要用户手动重来，之前的 token 全部白花 | `error_classifier` + `retry_utils` | Day 2 |
| P3 | 中文 token 低估 3–4 倍 | `core/compaction.py:estimate_tokens_text` | 先撞溢出再压缩，多一次失败请求和一次摘要调用 | `model_metadata` | Day 3 |
| P4 | 辅助任务全部用主模型 | `agent.py:_meta_query/_summarize` | 召回、抽取、摘要都按主模型价格和延迟计费 | `auxiliary_client` | Day 3 |
| P5 | grep 遍历 `.venv` 等目录 | `tools/builtin.py:_grep`、`sandbox/workspace.py:grep/glob` | 仓库根目录 1.7 s → 目标 < 0.15 s | `file_operations.py`（rg 优先） | Day 4 |
| P6 | 回合结束才持久化 | `core/session.py`、`agent.py:_persist_session` | 崩溃时丢失整个回合 | `hermes_state.py`（SQLite WAL） | Day 5 |
| P7 | 没有工具循环护栏 | `agent.py:run_turn` | 模型反复失败时会耗尽全部 20 步 | `tool_guardrails.py` | Day 6 |
| P8 | 计划闸门每次门控工具调用都读一次 `plan_mode.json` | `tools/registry.py:_plan_approval_gate` | 小（每次一次磁盘 IO） | — | Day 6 顺手修 |
| P9 | `_provider_messages` 每次调用都在 `_messages()` 里完整清洗一遍历史 | `providers.py:_messages` | O(n) × 步数，长会话里会逐渐变明显 | — | 观察，必要时再做 |
| P10 | 冷启动导入耗时 0.33 s（`lilbot.cli`） | import 链 | 小 | — | 不做 |

---

## 4. 安全层面（简述）

| 问题 | 状态 |
|---|---|
| S1 密钥可被 agent 读取 | ✅ 已在 Week 1 修复（`security/env.py`） |
| S2 `deny-all` 仍放行“安全只读”命令 | ⚠️ 部分修复（涉及密钥的命令已拒绝） |
| S3 权限检查分散在各个 handler | ❌ 未修，见 Day 7 |
| MCP 子进程继承完整环境（含 API Key） | ❌ 未修（`mcp/client.py:54` 用的是 `{**os.environ, ...}`）。Hermes 有 `env_passthrough.py` 控制透传 |
| 依赖没有设置上界 | ❌ Hermes 在供应链攻击事件之后强制要求上界，LilBot 只设了下限 |

---

## 5. LilBot 做得比 Hermes 好、应该保留的地方

1. **事件流主循环**：UI 和循环完全解耦，Hermes 用 callback 回调，耦合更重。
2. **ToolDef 契约**：可以按参数判断并发安全和破坏性，还有 validate 钩子。Hermes 的 registry 没有这么细。
3. **Deferred 工具 + ToolSearch**，以及工具目录的字节稳定缓存。
4. **压缩后恢复**：重新注入最近读过的文件、加载过的技能和 session 笔记，比 Hermes 的纯摘要更好用。
5. **子代理体系**：5 道工具白名单闸门、transcript、worktree 隔离。
6. **Windows 优先**：PowerShell 安全分析（`powershell_safety.py`）。Hermes 的 PTY 和 dashboard 在原生 Windows 上不可用。

---

## 6. 优化路线（7 天）

详见 `02-七天优化计划.md`。总体原则：

- **每天只改一个主题**，改完必须通过测试，并有前后对比数据。
- 优先做**性能和可靠性**（P1–P7），功能扩展（消息网关、插件、浏览器等）不在 7 天范围内。
- 每天的关键信息记录在 `SESSION_LOG.md`，终端意外关闭后可以从那里恢复。

| 天 | 主题 | 对应问题 |
|---|---|---|
| Day 1 | 基准测试脚手架 + HTTP 连接复用 | P1 |
| Day 2 | 错误分类 + 指数退避重试 + 备用模型 | P2 |
| Day 3 | 中文 token 估算 + 辅助模型路由 | P3、P4 |
| Day 4 | 搜索工具提速（ripgrep 优先 + 目录剪枝） | P5 |
| Day 5 | 回合内增量持久化（SQLite WAL） | P6 |
| Day 6 | 工具循环护栏 + 中断 + 计划闸门缓存 | P7、P8 |
| Day 7 | 权限收拢到 registry + MCP 环境隔离 + 总回归和复盘 | S3 等 |
