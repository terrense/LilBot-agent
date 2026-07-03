# 短期记忆压缩（Context Compaction）设计与三方对比

本文记录 LilBot 的短期记忆压缩机制、它解决的痛点，以及与两套成熟实现
（终端编程智能体 A、终端编程智能体 B）在同一问题上的取舍对比，最后给出
LilBot 本轮吸收对方优点后的完整方案。

---

## 1. 痛点：为什么需要短期记忆压缩

对话式编程智能体的"短期记忆"就是发给模型的 `messages` 数组。它随每轮工具调用
不断膨胀：一次 `read_file` 可能塞进几万字符，几十轮下来必然逼近甚至超过模型的
上下文窗口。三个直接后果：

1. **硬失败**：请求超过窗口，provider 直接报 `prompt is too long`，整轮崩溃。
2. **成本爆炸**：每轮都把全量历史重新计费，越滚越贵。
3. **前缀缓存失效**：一旦重写历史前缀，provider 的 KV 前缀缓存全部作废，
   下一次要按未命中价重新预填，代价远超省下的那点预算。

所以压缩不是"删了就行"，而是要在**不丢关键信息**、**不频繁破坏缓存**、
**不额外烧钱**三者间平衡。

---

## 2. 三方机制对比

| 维度 | 智能体 A | 智能体 B | LilBot（本轮后） |
|---|---|---|---|
| 触发信号 | token 逼近窗口（≈窗口−20k输出−13k缓冲，约 92%） | token 预算，且低于 500K 硬地板不压（保前缀缓存） | token 预算触发 `窗口−8k−6k`；条数兜底 |
| 压缩前省 token | microcompact：清/删旧 tool_result（8 类工具） | 本地 prune 旧 tool_result，**命中就不调 LLM** | ✅ 本地 prune 旧 tool_result，**命中就不调 LLM** |
| 缓存保护 | 缓存编辑：删历史不破前缀 | 硬地板 + 缓存对齐摘要请求 | ✅ prune 优先保结构 + 窗口自适应摘要下限 |
| 摘要结构 | 9 段结构化（`<analysis>`+`<summary>`） | 自由摘要（字数上限，temp 0.3） | ✅ 9 段结构化，纯文本禁工具 |
| 保留策略 | 最近 N + 缓存保留热前缀 | 尾部 + 工作集文件相关 + 强制 tool 对齐 | 尾部（token 预算）+ 不拆 tool_calls/tool 对 |
| 失败处理 | 熔断器（连续 3 次停手） | 3 次指数退避（1/2/4s，仅瞬时错误） | ✅ 3 次指数退避 + 熔断器（连续 3 次停手） |
| 反应式兜底 | reactive：接 413 prompt_too_long | 紧急溢出预检 | ✅ 检测溢出错误 → 强制压缩并重试一次 |
| 工作记忆回灌 | 指向 transcript 原文路径 | 工作集路径 pin | ✅ RecoveryState：重附最近读过的文件 + 技能正文 + 工具表 |
| 跨会话 | 会话记忆（fork agent） | Cycle 归档（SQLite） | Cycle 归档（Markdown 文件）+ recall_archive |

三家的根本差异：A 是"逼近窗口就多策略抢救"，B 是"没到预算绝不动、动一次就要保住缓存"，
LilBot 取两家之长——**便宜的先做（prune），贵的按需（summary），并且把工作记忆显式回灌**。

---

## 3. LilBot 的完整流水线（本轮后）

代码：`lilbot/core/compaction.py`、`lilbot/core/agent.py`、`lilbot/core/cycles.py`

```
每轮用户输入
   │  agent.run_turn → _maybe_compact()（主动触发）
   ▼
auto_compact(messages, ...)
   ├─ 阈值/熔断闸：未过 窗口−14k 或熔断器打开 → 不压
   ├─ 选保留尾部 compute_keep_start（token 预算，不拆 tool 对）
   ├─【第1层】本地 prune 旧 tool_result（microcompact）
   │      └─ 若光靠 prune 就降到阈值下 → 直接返回，**不调 LLM**（method="prune"）
   ├─【第2层】窗口自适应摘要下限：可回收前缀太小 → 不摘（省缓存）
   ├─ LLM 摘要（9 段结构化）+ 3 次指数退避重试
   ├─ 重组 = [system, 摘要+RecoveryState回灌+近期尾部]
   └─ 归档摘要为 cycle（仅 summarize 路径）
```

运行中若单轮请求仍溢出（工具输出在轮内塞爆窗口）：
`agent._complete_with_overflow_recovery` 检测到 `prompt too long` → 强制 `compact(manual=True)` → 重试一次（反应式兜底）。

---

## 4. 关键常量（`compaction.py`）

| 常量 | 值 | 作用 |
|---|---|---|
| `SUMMARY_OUTPUT_RESERVE` | 8_000 | 给摘要输出预留 |
| `AUTO_COMPACT_SAFETY_MARGIN` | 6_000 | 触发缓冲 |
| `KEEP_RECENT_TOKENS` / `MIN_KEEP_MESSAGES` / `KEEP_MAX_TOKENS` | 6_000 / 4 / 20_000 | 保留尾部窗口 |
| `MIN_SUMMARIZE_PREFIX_TOKENS` | 1_500 | 太小的前缀不值得摘 |
| `PRUNED_TOOL_RESULT_PLACEHOLDER` | `[old tool result cleared…]` | prune 占位符 |
| `MAX_SUMMARY_RETRIES` / `RETRY_BASE_DELAY_S` | 3 / 0.35 | 摘要重试退避 |
| `SUMMARIZE_FLOOR_FRACTION` | 0.02 | 窗口自适应摘要下限（自动模式） |
| `RECOVERY_FILE_LIMIT` / `RECOVERY_CHARS_PER_FILE` | 5 / 4_000 | 工作记忆回灌预算 |

---

## 5. 演进路线（面试可讲的迭代故事）

- **v0（原始）**：把截断后的消息字符串拼成一段扁平摘要——丢结构、丢工作文件、失败即崩。
- **v1（两层压缩 + 恢复）**：token 预算触发 + 9 段结构化摘要 + 保留近期原文尾部 +
  RecoveryState 回灌最近读过的文件/技能 + 熔断器；并把摘要归档为 cycle。
- **v2（本轮：吸收两大成熟实现的优点）**：
  1. **本地 prune 层**：先清旧 tool_result，命中就完全不调 LLM——省一次模型调用、且保结构对缓存友好；
  2. **窗口自适应摘要下限**：可回收前缀不够大就不做会破缓存的整段摘要；
  3. **摘要重试退避**：一次瞬时抖动不再浪费压缩机会，耗尽才熔断；
  4. **反应式溢出兜底**：轮内请求真溢出时，压一次再重试，不再让整轮崩。
