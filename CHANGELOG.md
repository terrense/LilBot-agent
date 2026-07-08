# 更新日志（CHANGELOG）

本文件记录 LilBot 的重要改动，按批次 + 日期归档，方便回溯。

---

## 2026-07-08（深夜）—— 模型无关接入 + CLI 输入区改进

测试数 304 → 319，无回归。

- **模型无关（hermes-agent 风格）**：LilBot 自身不含模型，任何 OpenAI 兼容的 Chat Completions 端点都可接入。新增 `KNOWN_PROVIDERS` 预设端点表（openai / deepseek / moonshot(kimi) / zhipu(glm) / dashscope(qwen) / openrouter / together / groq / mistral / siliconflow / xai(grok) / perplexity / fireworks，以及本地 ollama / vllm / lmstudio / localai）。`choose_provider` 改为：显式 mock/offline → 离线规则桩；否则**只要有 API key 或本地端点即用真实传输**（本地自托管无需 key）。`--provider` 放开为任意字符串，`--base-url` 可覆盖任意自定义端点。文件：`llm/providers.py`、`cli.py`。
- **`/model` 全面放开**：`/model NAME`（任意模型 id）、`/model provider:model`（解析已知端点，如 `moonshot:kimi-k2`、`openrouter/anthropic/claude-3.5`）、`/model flash|pro`（DeepSeek 预设）都支持；裸模型名保留当前 provider 只换模型。`/models` 改为展示当前接线 + 已知 provider 端点表。文件：`cli.py`。
- **CLI 输入区改进（参考 Claude Code）**：Composer 新增**上/下方向键历史回溯**（在首行/末行时回溯，不影响多行编辑；提交即入历史、连续去重、跳过空行）；**Ctrl+C 先清空非空输入、再二次确认退出**（与 Claude Code 一致）。历史导航逻辑抽成无依赖的 `tui/input_history.py` 以便单测。文件：`tui/dashboard.py`、`tui/input_history.py`、`tui/classic.py`。

---

## 2026-07-08（夜间）—— 会话级记忆活文档（#12）

测试数 298 → 304，无回归。

- **会话记忆活文档（#12）**：新增固定 7 段模板的会话笔记（`.lilbot/session-memory.md`：Session Title / Current State / Task Specification / Files and Functions / Errors and Corrections / Learnings / Worklog）。每几回合在**后台**（不阻塞回合）跑一次侧查询，模型**只返回改动的段**，`merge_updates` 写回时保留其余段与模板结构——增量、可 diff、模板即 schema，替代每次全文重写。压缩时活文档进入 recovery 附件，模型压缩后仍保有运行笔记。文件：`core/session_memory.py`、`core/agent.py`、`core/compaction.py`（`RecoveryState.record_note`）。

至此可移植且合理工程量的对标短板基本补齐；剩余 #2（流式与工具执行重叠，需 ~530 行并发敏感的 StreamingToolExecutor）作为独立里程碑，#19/#24（VCR 测试基建）为纯测试面增强。#20（Skills 预取）经评估为低价值（skill 已全量在系统提示词中）。详见 `docs/LILBOT_VS_CLAUDE_CODE_COMPARISON.md` 的"复刻进度"。

---

## 2026-07-08（傍晚）—— 对标短板批量补齐（第 3 批：工具契约 / 缓存 / 观测 / 权限）

测试数 280 → 298，无回归。

- **工具契约标准化（#4）**：`ToolDef` 增加 `validate`（执行前输入校验——给模型可读的失败原因、不跑 handler、不问用户，区别于权限）、`context_modifier`（非并发安全工具执行后以纯函数改写后续上下文）、`is_destructive`（per-input 判定不可逆操作）、`max_result_chars`（per-tool 落盘阈值，-1 表示永不落盘）。示例：`edit_file` 校验 old 非空且 old≠new；shell 工具标注 rm/mv/git push --force 等为 destructive。文件：`tools/registry.py`、`tools/builtin.py`、`tools/offload.py`、`core/agent.py`。
- **缓存纪律（#17）**：把 prompt-cache 命中率变成可运营指标（`cache_stats()`），并统计**缓存断裂**（大 prompt 却零 cache-read）。文件：`core/agent.py`。
- **结构化事件日志（#18）**：`.lilbot/events.jsonl` 记录 turn/tool/compaction/recovery 事件（含 ok/耗时/命中率/断裂/恢复次数），**只序列化标量**以防内容或密钥泄漏，best-effort 永不抛错。这是"工具成功率/失败恢复率/复杂任务完成率"等指标与 bad-case 分析的原始数据。文件：`core/eventlog.py`。
- **分层权限规则（#15）**：新增 `Tool(pattern)` 语法的权限规则引擎——按来源分层（policy/project/user）、优先级 deny>ask>allow、**被遮蔽规则检测**（永远不会触发的规则），从 `permissions.json` 加载。灾难命令永远先 deny（用户 allow 规则也无法放行 `rm -rf /`）；空规则集零行为改变。文件：`sandbox/permission_rules.py`、`sandbox/execpolicy.py`。

---

## 2026-07-08（下午）—— 对标短板批量补齐（第 2 批）

测试数 266 → 280，无回归。按"弱于 CC 且不依赖 Claude 模型本身"的可移植差距，按价值顺序继续模仿。

- **输入级并发安全**：`isConcurrencySafe` 从"工具级"下沉到"输入级"——`bash("ls")`/`git status` 可加入并行只读批，`bash("rm -rf x")`/`git push` 保持串行，复用 execpolicy 的只读命令白名单。`ToolDef` 新增 `concurrency_check` 谓词，谓词抛错则保守判不安全。文件：`tools/registry.py`、`tools/builtin.py`、`core/agent.py`。
- **Hooks 结构化输出协议 + Stop hook**：命令钩子可在 stdout 打印 JSON 来 `decision:block`、`updatedInput`（改写工具入参）、`additionalContext`（注入上下文）、`continue:false`；新增 `stop` 事件——模型想结束时钩子可强制其继续工作（带最多 3 次的死亡螺旋熔断），及 `user_prompt_submit` 事件。纯文本钩子与退出码语义不变。文件：`hooks/engine.py`、`hooks/models.py`、`core/agent.py`。
- **有序有界的溢出恢复 + 可观测 transition**：`_stream_turn` 的反应式压缩重试改为有界（最多 2 次）循环，超限则让错误浮出而非死循环；每次恢复记录一条 transition（`reactive_compact_retry` 等）到可断言的 `_recovery_transitions`。文件：`core/agent.py`。
- **结构化判错**：`ProviderError` 携带 `status_code` 与 `is_overflow`（413 或错误体文本判定），agent 结构化优先、文本匹配兜底。文件：`llm/providers.py`。
- **输出截断续写**：模型因 `finish_reason=="length"` 被截断时，注入"从断点续写、不道歉不复述"并继续，最多 3 次。`ProviderTurn` 新增 `finish_reason`。文件：`core/events.py`、`llm/providers.py`、`core/agent.py`。
- **searchHint**：`ToolDef` 新增 `search_hint`，参与延迟工具检索加权，提高按能力词（而非工具名）召回。文件：`tools/registry.py`。

配对不变量（每个 tool_use 必有 tool_result）经核查 LilBot 本就满足：`registry.execute` 把异常收敛成 `ToolResult`，未执行的 tool_calls 不写入 assistant 消息。详见 `docs/LILBOT_VS_CLAUDE_CODE_COMPARISON.md` 的"复刻进度"。

---

## 2026-07-08 —— 上下文压缩机制全面补齐

测试数 246 → 266，无回归。系统性对标业界成熟实现，把可移植的压缩要素全部补齐（可移植部分覆盖率约 90%，详见 `docs/CC_COMPACTION_REPLICATION_STATUS.md`）。

- **L0 工具结果预算（缓存安全卸载）**：新增 `tool_budget.py` 的 frozen/fresh/replaced 三态状态机。已原文发给模型（进了缓存前缀）的工具结果永不改写，只把本会话首见的超大结果按大小替换成预览——既卸载空间又不打掉前缀缓存。在压缩之前运行。文件：`lilbot/core/tool_budget.py`、`lilbot/core/agent.py`（`_apply_tool_budget`）。
- **真实 token 触发**：压缩触发判断优先采用 Provider 回报的真实 `prompt_tokens`，字符估算仅作兜底。文件：`lilbot/core/agent.py`（`_add_usage` → `_last_input_tokens`）。
- **缓存冷热感知 prune**：距上次模型调用超过缓存 TTL（5min）时缓存已冷，清理工具正文"免费"（前缀反正会重算），可在阈值下主动清；冷缓存路径只许 prune 不许付费摘要。文件：`lilbot/core/compaction.py`（`CACHE_TTL_SECONDS`、`cache_cold`）、`agent.py`（`_cache_is_cold`）。
- **摘要请求自溢出的截头重试**：按 API 轮次分组（不拆 `tool_calls`/`tool` 配对），摘要请求本身过长时丢最旧轮重试，marker 幂等防死循环。文件：`compaction.py`（`group_messages_by_round`、`truncate_head_for_retry`）。
- **`<analysis>` 草稿区 + NO_TOOLS 双保险**：摘要提示词让模型先写可剥离的思考草稿再产出 `<summary>`，开头结尾各一遍禁工具。`format_compact_summary` 入库前剥离草稿。文件：`compaction.py`。
- **前缀缓存共享摘要**：摘要调用改为复用主对话的 system+prefix 消息对象 + 尾部指令，命中 Provider 前缀缓存，而非发全新 2 消息小对话（原来 100% cache miss）。文件：`agent.py`（`_message_summarizer`）。
- **部分压缩（from/up_to）**：用户可选一个 pivot，`up_to` 压缩其前（保留近段）、`from` 保留旧上下文而压缩近段。文件：`compaction.py`（`partial_compact`）、`agent.py`。
- **压缩后统一清理**：摘要重写前缀后清除陈旧的真实 token 计数与一次性诊断提示；有意保留已发现的延迟工具（免得再 ToolSearch）。恢复附件加总 token 预算。文件：`agent.py`（`_post_compact_cleanup`）、`compaction.py`（`RECOVERY_TOTAL_TOKENS`）。

详见 `docs/CC_COMPACTION_REPLICATION_STATUS.md`（逐条复刻清单）与 `docs/CC_DEEPDIVE_COMPACTION_PIPELINE.md`（机制深挖）。

---

## 2026-07-03 —— 短期记忆压缩强化

测试数 257 → 261，无回归。对照两套成熟实现全面补齐上下文压缩短板。

- **本地 prune 层（microcompact）**：压缩时先清理"保留尾部之前"的旧 `tool` 结果内容（替换为占位符，保留 `tool_call_id`/`name` 使配对仍合法）。若光靠 prune 就把预算降到阈值下，**直接返回、完全不调用 LLM**——省一次模型调用，且保留消息结构、对前缀缓存更友好。手动 `/compact` 仍走完整摘要以给出真正的交接。文件：`lilbot/core/compaction.py`（`prune_tool_results`）。
- **窗口自适应摘要下限**：会破坏缓存的整段 LLM 摘要只在"可回收前缀足够大"时才做（自动模式取 `max(1500, 窗口×0.02)`；手动模式保留固定下限）。避免为一点点回收就重写前缀、打掉缓存。
- **摘要重试退避**：摘要调用改为最多 3 次指数退避（0.35s 起），耗尽才记一次熔断失败。一次瞬时抖动不再浪费一次压缩机会。文件：`lilbot/core/compaction.py`（`_summarize_with_retry`）。
- **反应式溢出兜底**：主循环中若单轮请求真的触发 provider 的"prompt too long"，检测到后强制压缩并**重试一次**，不再让整轮崩溃。文件：`lilbot/core/agent.py`（`_complete_with_overflow_recovery`、`is_context_overflow_error`）。
- **压缩结果结构**：`CompactResult` 增加 `pruned`（清理字符数）与 `method`（`prune`/`summarize`）；prune-only 结果不再误当摘要归档为 cycle。

详见 `docs/CONTEXT_COMPACTION.md`（三方对比 + 演进路线）。

---

## 2026-06-23 —— 安全 / 纠错 / 协议增强

测试数 184 → 257，无回归。

- **密钥脱敏**：在内容送到屏幕 / trace / 日志之前，自动给 API key、token、私钥、`KEY=value` 形式的密钥打码。模型自身上下文仍保留原值，不影响功能。误报防护：纯数字配置（如 `MAX_TOKENS=128000`）和 `AUTHOR=` 不打码。文件：`lilbot/security/secrets.py`。
- **编辑后自动诊断注入**：`write_file/edit_file/fim_edit` 改完代码文件后，自动跑诊断（有 LSP 用 LSP，Python 走语法兜底），把错误作为一次性提示喂给下一轮，模型自我纠错。按扩展名筛选、每轮最多 5 个文件，`config.auto_diagnostics` 可关。文件：`lilbot/core/agent.py`。
- **命令安全引擎**：危险命令（`rm -rf /`/`~`/`*`/`.`、fork 炸弹、`mkfs`、`dd` 写设备、`curl|sh`、`shutdown`）硬拦；已知只读命令（`git status -s`、`ls -la`、`cat`…，忽略 flag）自动放行省去审批。普通子目录删除不拦；复合命令不自动放行。`config.auto_allow_safe_commands` 可关。文件：`lilbot/sandbox/execpolicy.py`。
- **周期记忆 + recall_archive**：每次上下文压缩把摘要归档到 `.lilbot/archives/cycle-<时间>.md`，`recall_archive` 工具可按关键词检索，长会话不再"压完即忘"。文件：`lilbot/core/cycles.py`。
- **工具目录缓存**：缓存可见工具的序列化目录，仅当工具集变化才重建，使发给模型的 `tools` 字节稳定、提升前缀缓存命中。`/tokens` 新增 `tool_catalog_fp`、`tools_visible`。文件：`lilbot/tools/registry.py`。
- **MCP 客户端**：同步 JSON-RPC over stdio 客户端（持久子进程 + 读线程，无异步依赖），`initialize` 握手 + `tools/list` 自动发现 + `tools/call`。把每个发现的工具注册成一等延迟工具 `mcp__<server>__<tool>`，模型像用内置工具一样用任意 MCP server。文件：`lilbot/mcp/client.py`。
- **MCP 服务端**：`python -m lilbot --mcp-server` 把 LilBot 工具暴露给别的 MCP 客户端（默认只读，`.lilbot/mcp_server.json` 的 `expose_tools` 可调）。客户端 + 服务端组成双向 MCP 节点。文件：`lilbot/mcp/server.py`。

详见 `docs/TECH_REPORT_M1-M8.md`（技术报告）与 `docs/VERIFY_M1-M8.md`（验证指南）。

---

## 2026-06-22 —— 引擎与持久化增强

测试数 118 → 184。

- **延迟工具加载 + ToolSearch**：每轮只发约 33 个常用工具的 schema，其余按需用 `ToolSearch` 加载，单轮工具负载下降约 77%。
- **大工具结果落盘**：超 16KB 的结果写入 `.lilbot/session/tool-results/` 并给 2KB 预览，`retrieve_tool_result`/`handle_read` 可回读，不再硬截断丢数据。
- **两层上下文压缩 + 恢复**：结构化摘要 + 保留近期原文尾部 + 恢复附件（重附最近读过的文件 / 技能）+ 熔断器。
- **缓存命中上报**：归一化 DeepSeek/OpenAI 前缀缓存命中数，`/tokens` 可见。
- **生命周期 Hooks**：`.lilbot/hooks.json`，`pre_tool_use` 可拦截工具调用，支持热加载、一条规则匹配一组工具。
- **记忆智能召回 + 自动提取**：用小旁路查询挑相关记忆（带新鲜度提示）注入；每 3 轮自动提炼可长期记忆。
- **只读工具并行**：连续只读工具用线程池并发，保序；写 / 执行类仍串行。
- **会话持久化 + resume**：每轮把对话写入 `.lilbot/sessions/`，`--resume`、`/sessions`、`/resume` 可续。
- **文件式记忆**：每条记忆一个 frontmatter `.md`，用户级 / 项目级分目录 + `MEMORY.md` 索引。
- **文件历史 + rewind**：编辑前快照，`/rewind [n]` 撤销最近 n 次编辑，`/history` 查看。
- **Worktree 增强**：自动生成分支名、把重依赖目录软链进新 worktree（Windows 用 junction）、`worktree_prune` 清理过期 worktree。
