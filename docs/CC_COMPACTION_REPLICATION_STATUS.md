# CC 压缩机制复刻清单：他有多少，我们复刻了多少

> 目标：把 Claude Code（`../claude-code-source-code`，51.3 万行）的上下文压缩机制，凡是**架构上可移植**的，全部复刻进 LilBot（`lilbot/`，Python，OpenAI 兼容 Provider）。
> 本轮改动：`lilbot/core/compaction.py`（扩写）、`lilbot/core/tool_budget.py`（新增）、`lilbot/core/agent.py`（接线）、`tests/test_compaction_cc.py`（+20 测试）。
> 状态图例：✅ 已复刻 ｜ 🔶 适配复刻（因 Provider/存储差异做了等价改造）｜ ⛔ 不可移植（依赖 Anthropic 私有 API，已说明原因）｜ 📄 已设计留档（未接线）

---

## 一、逐条对照总表（CC 全部压缩要素）

| # | CC 机制 | CC 位置 | LilBot 复刻 | 落点 |
|---|---------|---------|------------|------|
| 1 | **L0 工具结果预算**（frozen/fresh/replaced 三态状态机，缓存安全地卸载超大工具输出） | `toolResultStorage.ts:820-910` | ✅ | `tool_budget.py`（新模块）+ `agent.py::_apply_tool_budget` |
| 2 | 单工具落盘阈值（超限整体落盘换预览） | `Tool.ts:466` | 🔶 已有 | `tools/offload.py`（本就存在） |
| 3 | **L1 snip**（直接删除历史消息） | `query.ts:400-410`（编译剔除） | 📄 | 价值低于 prune，留档未接线 |
| 4 | **L2 microcompact 清正文** | `microCompact.ts:446-529` | ✅ 已有 | `compaction.py::prune_tool_results` |
| 5 | **L2 缓存冷热感知触发**（缓存过期才清正文，损失为零） | `microCompact.ts:262-266`、`timeBasedMCConfig.ts` | ✅ | `compaction.py::CACHE_TTL_SECONDS` + `agent.py::_cache_is_cold` + `auto_compact(cache_cold=)` |
| 6 | **L2b cache_edits**（服务端缓存原位删 + `cache_deleted_input_tokens` 差分记账） | `microCompact.ts:305-399` | ⛔ | 依赖 Anthropic `cache_edits` API；OpenAI/DeepSeek 无此能力 |
| 7 | **L3 context collapse**（读取时投影 + 提交日志） | `services/contextCollapse/`（编译剔除） | 📄 | 架构级大工程，留档；LilBot 的 Cycle 归档是其精神简化版 |
| 8 | **L4a session-memory compact**（活文档充当摘要，免 LLM） | `sessionMemoryCompact.ts` | 🔶 部分 | `cycles.py`（归档已存在）；"归档新鲜则跳过 LLM"的快路径留待接线 |
| 9 | **L4b LLM autocompact 主摘要** | `compact.ts:387-643` | ✅ 已有 | `compaction.py::auto_compact` |
| 10 | **触发算术**（窗口−摘要预留−buffer；p99.99 定阈值） | `autoCompact.ts:28-91` | ✅ 已有 | `compaction.py::compute_threshold` |
| 11 | **熔断**（连续失败 N 次停止重试） | `autoCompact.ts:67-70` | ✅ 已有 | `compaction.py::CompactCircuitBreaker` |
| 12 | **失败计数跨迭代传递** | `autoCompact.ts:257-265` | ✅ 已有 | breaker 在 Agent 上持有 |
| 13 | **摘要指数退避重试** | `compact.ts:1257-1372` | ✅ 已有 | `compaction.py::_summarize_with_retry` |
| 14 | **摘要请求自身溢出的截头重试**（按 API 轮次分组丢头部） | `compact.ts:243-291`、`grouping.ts` | ✅ | `compaction.py::group_messages_by_round` + `truncate_head_for_retry` + `_summarize_messages_with_retry` |
| 15 | **`<analysis>` 草稿区 + 入库前剥离** | `prompt.ts:311-335` | ✅ | `compaction.py::SUMMARY_INSTRUCTION` + `format_compact_summary` |
| 16 | **NO_TOOLS 双保险**（开头+结尾禁工具） | `prompt.ts:19-26,269-272` | ✅ | `compaction.py::_NO_TOOLS_PREAMBLE/_TRAILER` |
| 17 | **9 段结构化摘要**（含"全部用户消息原话"、"下一步逐字引用"） | `prompt.ts:61-143` | ✅ 已有+增强 | `compaction.py::SUMMARY_INSTRUCTION` |
| 18 | **摘要 fork 复用主对话 prompt 缓存** | `compact.ts:1136-1248` | 🔶 | `agent.py::_message_summarizer`（发同一 system+prefix 对象 + 尾部指令，复用前缀缓存） |
| 19 | **token 计数信 API usage、估算兜底** | `query.ts:88`、`autoCompact.ts:225` | ✅ | `agent.py::_add_usage` 记 `_last_input_tokens` → `auto_compact(actual_tokens=)` |
| 20 | **L4c partial compact**（用户选点 from/up_to 双向） | `compact.ts:772-1106` | ✅ | `compaction.py::partial_compact` + `agent.py::partial_compact` |
| 21 | **L5 反应式恢复**（超长报错→压缩→重试） | `query.ts:1062-1183` | ✅ 已有 | `agent.py::_stream_turn` |
| 22 | **扣留错误**（可恢复错误先不外泄） | `query.ts:788-825` | 🔶 | LilBot 同步生成器模型下以 try/except 等价实现（错误不 yield，压缩后重试） |
| 23 | **判错用结构化标志而非文本前缀** | `compact.ts:1205-1210` | 🔶 部分 | `is_context_overflow_error` 仍文本匹配；已列为待办（Provider 需带出 HTTP 状态码） |
| 24 | **max_output_tokens 断点续写**（升档→续写→放弃） | `query.ts:1188-1256` | 📄 | 留档；LilBot 目前依赖模型自然停止 |
| 25 | **压缩后统一失效清单** | `postCompactCleanup.ts` | ✅ | `agent.py::_post_compact_cleanup`（清陈旧 token 数、pending 诊断；注明"不清什么"） |
| 26 | **已发现延迟工具跨压缩传递** | `compact.ts:603-611` | ✅ | registry `_discovered` 天然存活；cleanup 注释说明有意保留 |
| 27 | **恢复附件**（最近文件/skill/工具清单重注入） | `compact.ts:1415+` | ✅ 已有 | `compaction.py::RecoveryState` |
| 28 | **恢复附件总 token 预算** | `compact.ts:122-130` | ✅ | `compaction.py::RECOVERY_TOTAL_TOKENS` |
| 29 | **有损压缩配无损逃生门**（transcript 路径 / 归档可搜回） | `compact.ts:350` | ✅ 已有 | `cycles.py`（Cycle 归档 + recall_archive） |
| 30 | **保留段重连元数据**（head/anchor/tail UUID） | `compact.ts:349-367` | ⛔ 不需要 | LilBot 会话是整体 JSON 快照，无增量重连需求 |
| 31 | **L6 API 原生 context_management**（声明式服务端清理） | `apiMicrocompact.ts` | ⛔ | 依赖 Anthropic `context_management` 请求字段；OpenAI 兼容端不支持 |
| 32 | **心跳保活**（压缩慢调用防 WebSocket 断连） | `compact.ts:1159-1176` | ⛔ 不需要 | LilBot 无远程会话 WebSocket 传输层 |
| 33 | **grouping 不拆 tool_use/tool_result 配对** | `grouping.ts` | ✅ | `group_messages_by_round` + `compute_keep_start` 对齐 |
| 34 | **保留尾部不孤立 tool 消息** | `compact.ts` | ✅ 已有 | `compaction.py::_align_keep_start` |

**统计**：CC 压缩相关要素 34 项 →
✅ 已复刻/已有 **21 项**，🔶 适配复刻 **6 项**，📄 设计留档 **3 项**，⛔ 不可移植（含 2 项"LilBot 架构下不需要"）**4 项**。
**可移植部分覆盖率 ≈ 27/30 = 90%**（分母剔除 4 项 Anthropic 私有/不需要项）。

---

## 二、本轮新增代码逐一说明

### 1. `lilbot/core/tool_budget.py`（新模块，L0）
frozen/fresh/replaced 三态状态机，`ToolBudgetState` 跨回合存活：
- **frozen**（已原文发给模型、进了缓存前缀）→ 永不改写，保证前缀字节稳定；
- **fresh**（本会话首见）→ 超预算时按大小降序替换成预览；
- **replaced** → 幂等地保持预览。
未选中替换的 fresh 立即标 seen（下轮变 frozen），选中的与 replacement 同时写入——复刻 CC "对观察者原子"的顺序纪律，杜绝"seen 但无 replacement 被误判 frozen 发原文→缓存 miss"。

### 2. `compaction.py` 扩写
- `CACHE_TTL_SECONDS=300` + `auto_compact(cache_cold=)`：缓存冷时（>5min 空闲）prune 变"免费"，可在阈值下主动清；且冷缓存路径**只许 prune 不许付费摘要**。
- `group_messages_by_round` + `truncate_head_for_retry`：按 assistant 边界分 API 轮，摘要请求自身溢出时丢最旧轮重试，marker 幂等（防死循环）、保系统消息、至少留一轮。
- `SUMMARY_INSTRUCTION` 重写：NO_TOOLS 双保险 + `<analysis>` 草稿区；`format_compact_summary` 剥离草稿、解包 `<summary>`，无标签时原样返回（离线 Provider 兼容）。
- `MessageSummarizer` + `_summarize_messages_with_retry`：prompt-cache-sharing 摘要通道，把真实 system+prefix 对象交回 Agent 复用缓存前缀。
- `auto_compact(actual_tokens=)`：触发判断优先用 Provider 回报的真实 prompt token。
- `RecoveryState.build_attachment` 加 `RECOVERY_TOTAL_TOKENS` 总预算。
- `partial_compact(pivot, direction)`：from/up_to 双向部分压缩。

### 3. `agent.py` 接线
- `_add_usage` 记录 `_last_input_tokens`（真实 token）与 `_last_activity_ts`（缓存冷热）。
- `_apply_tool_budget` 在 `_maybe_compact` 最前跑（L0 先于压缩，CC 顺序）。
- `_message_summarizer`：发 `[system, *prefix, {SUMMARY_INSTRUCTION}]`，复用缓存前缀。
- `compact()` 传 `actual_tokens/cache_cold/message_summarizer`，收尾调 `_post_compact_cleanup`。
- `_post_compact_cleanup`：摘要后清陈旧 token 数 + pending 诊断；**有意保留** discovered 工具（注明理由）。
- `partial_compact()`：用户级部分压缩入口。

### 4. 测试 `tests/test_compaction_cc.py`（20 个，全绿）
覆盖：三态预算（替换/冻结不动/keep_recent/幂等）、API 轮分组、截头重试（幂等/保系统/单轮返 None）、analysis 剥离、真实 token 触发、冷缓存 prune-only、共享摘要+剥离、partial from/up_to、Agent 级共享摘要复用 system、冷热检测、cleanup 清陈旧计数。
**全量回归：266 passed, 6 skipped**（较上轮 246 增 20）。

---

## 三、明确不复刻的 4 项及原因（诚实边界）

| 机制 | 为什么不复刻 |
|------|-------------|
| cache_edits 服务端原位删除（#6） | 依赖 Anthropic Messages API 的 `cache_edits` 块与 `cache_deleted_input_tokens` 回报；OpenAI/DeepSeek 兼容端无此协议。LilBot 用 prune（清正文）达到相近的空间目标，代价是牺牲被清部分的缓存——这是 Provider 能力差异，不是实现取舍。 |
| API 原生 context_management（#31） | 同上，`context_management.edits` 是 Anthropic 请求字段。若将来 LilBot 接入 Anthropic Provider，可一行声明替代整个 L2——已在 `providers.py` 留了扩展位注释。 |
| 心跳保活（#32） | LilBot 无远程会话 WebSocket 传输层，压缩慢调用不会触发 idle 断连。 |
| 保留段重连元数据（#30） | LilBot 会话是整体 JSON 快照持久化，不做增量 parentUuid 重连，天然无需。 |

context collapse（#7）与 max_output_tokens 断点续写（#24）、snip（#3）为**已留档未接线**：可移植但价值/复杂度比不划算，或需要更大改动，列入后续。

---

## 四、剩余待办（按性价比）

1. **判错结构化**（#23 ★★★）：`providers.py` 把 HTTP 413/400 状态码作为结构化信号带出，`is_context_overflow_error` 降级为兜底——避免文本匹配漏判。
2. **归档快路径**（#8 ★★☆）：Cycle 归档足够新时，直接"归档摘要 + 保留窗"跳过 LLM，压完自验阈值（CC 的 SM-compact 精髓）。
3. **max_output_tokens 续写**（#24 ★★☆）：升档→断点续写→放弃三段链。
4. **context collapse**（#7 ★☆☆）：读取时投影 + 提交日志，架构级，独立里程碑。

---

*基于 2026-07 源码快照；行号以该快照为准。全部改动纯增量，266 测试通过。*
