# 深挖专题：Claude Code 的上下文压缩流水线（逐层逐行解读）

> 源码根：`../claude-code-source-code/src`
> 核心文件：`query.ts`（编排）、`services/compact/`（11 个文件，约 4000 行）、`utils/toolResultStorage.ts`（1040 行）
> 注：`snipCompact` / `reactiveCompact` / `contextCollapse` 三个模块在本份源码里被 `feature()` 编译剔除（内部实验），但 `query.ts` 的调用点与注释足以完整还原其设计，下文标注【还原】。
> 对照对象：LilBot 的两层压缩（`lilbot/core/compaction.py`）。

---

## 0. 全景：一次 API 请求发出前，消息数组经过什么

`query.ts` 主循环每次迭代（= 每次要调一次模型）都按固定顺序把 `messagesForQuery` 过一遍流水线。**排序原则：越便宜、越保缓存的手段越先跑；前一级若已解除压力，后一级自动不触发。**

```
用户消息进入
   │
   ▼
┌─ L0 工具结果预算 applyToolResultBudget ──── 超大工具输出落盘换预览（不看内容，按 tool_use_id）
│      query.ts:379-394
├─ L1 snip compact【还原】────────────────── 直接删掉部分历史消息，报告 tokensFreed
│      query.ts:400-410
├─ L2 microcompact ───────────────────────── 清旧工具结果正文；ant 版用 API cache_edits 服务端原位删
│      query.ts:413-426 → microCompact.ts
├─ L3 context collapse【还原】────────────── 把历史"折叠"为读取时投影，粒度化保留
│      query.ts:428-447
├─ L4 autocompact ────────────────────────── 阈值触发时 fork 子代理做 9 段结构化摘要
│      query.ts:453-543 → autoCompact.ts / compact.ts
├─ (阻塞下限预检 isAtBlockingLimit) ───────── 仅当没有任何反应式恢复者"认领"时才合成 413
│      query.ts:615-648
▼
callModel 发请求
   │ 若真的 413 / 媒体超限 / 输出截断……
   ▼
└─ L5 反应式恢复链【还原】────────────────── 扣留错误 → collapse drain → reactive compact → 放弃
       query.ts:788-825（扣留）、1062-1183（恢复）
```

关键编排细节（全部有注释背书）：

* **L0 在 L2 之前**，因为 cached microcompact 纯按 `tool_use_id` 操作、从不检查内容，所以"内容被替换成预览"对它不可见，两者可以安全组合（`query.ts:370-373`）。
* **L1 在 L2 之前、且 `snipTokensFreed` 要一路传给 L4**：snip 删了消息，但幸存的 assistant 消息里的 `usage` 仍反映删之前的上下文大小，`tokenCountWithEstimation` 看不到节省——所以显式把省下的 token 数作为参数传给 autocompact 的阈值判断（`query.ts:396-399`，`autoCompact.ts:164-167`）。**教训：一切从旧响应读出来的 token 计数都可能是陈旧的，谁改了历史谁负责补差。**
* **L3 在 L4 之前**，注释直说：如果 collapse 能把我们压回阈值之下，autocompact 就成为 no-op，"保住粒度化上下文，而不是换成一份摘要"（`query.ts:429-432`）。
* **阻塞预检会"让位"**：当 reactive compact 或 collapse 开启时，跳过发请求前的合成 413——因为合成错误在 API 调用之前就返回了，真正的恢复者永远等不到一个可以恢复的真错误（`query.ts:602-614`）。**这是"多个防护相互抢跑"问题的标准解法：明确谁拥有这个故障。**

---

## 1. L0：工具结果预算（源头减排）

文件：`utils/toolResultStorage.ts`

两个维度的预算：

**① 单工具维度**——每个工具声明 `maxResultSizeChars`（`Tool.ts:466`）。超限的输出不是截断，而是**整体落盘**到 `<session>/tool-results/`，模型收到 `<persisted-output>` 包裹的预览 + 文件路径，需要时可以用 Read 读回全文。特例：Read 工具设 `Infinity` 硬性豁免——把 Read 的输出再存成文件让模型用 Read 读回来是循环（`toolResultStorage.ts:59-61`）。LilBot 的 `tools/offload.py` 是同思想的简化版。

**② 单消息聚合维度**——`enforceToolResultBudget`（`toolResultStorage.ts:820-910`）对每条 user 消息里全部 tool_result 的总量设预算，超预算时选择部分结果落盘替换。这里有一个值得细看的**缓存一致性状态机**：

* 每个 tool_use_id 有三态：`fresh`（本轮新出现）、`frozen`（此前已见过且以原文发给过模型）、`replaced`（已被替换成预览）。
* **frozen 的永不再动**——它的原文已经进入服务端缓存前缀，现在替换它等于自砸缓存；只从 fresh 里挑替换对象。
* `seenIds` 与 `replacements` 两个集合的更新顺序被刻意设计成"对观察者原子"：先选中的 ID 在 `await` 落盘完成之后才同时写入两个集合，未选中的立即标 seen。注释解释了反例——若并发读者看到"X ∈ seenIds 但 X ∉ replacements"，会把 X 误判为 frozen 而发原文，与主线程发的预览不一致 → 缓存 miss（`toolResultStorage.ts:833-841,858-864`）。
* 落盘失败的 ID 标记 seen 但不替换：原文已经发给过模型，从此按 frozen 处理——**失败路径的语义也想清楚了**。
* 替换记录会写回会话文件，但只对"会被 resume 读回"的 querySource 持久化（`query.ts:376-378`）；短命的 fork 调用者不写。

> **LilBot 对照**：`offload.py` 只有维度①，且没有 frozen/fresh 区分——LilBot 的 prune 直接清旧工具结果，等价于"永远牺牲缓存换空间"。CC 的做法是空间和缓存两个都要。

## 2. L1：snip compact【还原】

编译剔除，但从 `query.ts:400-410,114-117` 可还原：按某种规则**直接删除**历史消息（区别于清空内容），返回 `{messages, tokensFreed, boundaryMessage}`；boundary message 会 yield 给 UI 标注"这里删过东西"。它与 microcompact "都可能运行——不互斥"（`query.ts:396`）。上文的 `snipTokensFreed` 传递就是它引出的陈旧计数问题。

## 3. L2：microcompact（微压缩）——两条路径

文件：`services/compact/microCompact.ts`（530 行）

**只压这 8 类工具**的结果：Read / Shell / Grep / Glob / WebSearch / WebFetch / Edit / Write（`COMPACTABLE_TOOLS`, 41-50 行）。用户消息、模型文本、thinking 一概不碰——微压缩的定位就是"清理可再生的机械输出"。

### 3a. 时间触发路径（time-based MC，446-529 行）

触发条件：距上一条 assistant 消息的间隔超过阈值（配置分钟数）。逻辑链非常漂亮：

> 间隔太久 ⇒ **服务端缓存已过期** ⇒ 无论如何整个前缀都要重写 ⇒ 那就趁现在、在发请求之前，把旧工具结果正文清掉（保留最近 N 条），反正没有缓存可保了。（262-266 行注释）

也就是说：**清内容破坏缓存这件事，只在缓存已死时做，损失为零。** 细节：

* `keepRecent` 下限钳到 1：`slice(-0)` 会返回整个数组（悖论式地全保留），而清光所有结果会让模型失去全部工作上下文——两个退化情形都不允许（458-461 行）。
* 清完必须 `resetMicrocompactState()`：cached-MC 的模块级状态里记着以前登记过的 tool_use_id，内容已经变了还按旧状态发 cache_edits 会去删服务端已不存在的条目（512-517 行）。
* 通知缓存断裂检测器"接下来 cache read 掉是我干的，不是事故"（518-527 行）——**每个主动破坏缓存的动作都要向监控自首，否则报警全是误报**（BQ 数据：漏了一处自首导致 20% 的缓存断裂事件是误报，`autoCompact.ts:299-302`）。

### 3b. 缓存编辑路径（cached MC，ant-only，305-399 行）

不改本地消息内容，而是登记要删的 tool_use_id，由 API 层附加 `cache_edits` 块让**服务端在缓存里原位删除**这些工具结果。三个精细点：

* 记账不靠客户端估算：API 返回 `cache_deleted_input_tokens`（**累计值、粘性**），所以先在请求前记 baseline，响应后做差分，boundary 消息推迟到拿到真实差值再发（`query.ts:866-892`，`microCompact.ts:370-383`）。
* 只允许主线程跑：fork 出的 agent（session_memory 等）若把自己的 tool_result 登记进全局状态，主线程会去删自己会话里不存在的工具（273-276 行）。
* `isMainThreadSource` 用 `startsWith('repl_main_thread')` 前缀匹配——注释坦白旧代码用 `===` 是潜伏 bug：换了 output style 的用户 querySource 变成 `repl_main_thread:outputStyle:x`，被静默排除在 cached MC 之外（243-251 行）。

> **LilBot 对照**：`prune_tool_results` 等价于 time-based 路径的"清正文"，但无条件执行（每次自动压缩都先 prune）。CC 的洞见是把它绑定在"缓存已冷"的判定上；LilBot 若接了支持缓存编辑的 API 可考虑 3b，否则至少可以学"缓存冷热感知"：距上次调用超过缓存 TTL 才用 prune，否则直接跳到摘要评估。

## 4. L3：context collapse【还原】

编译剔除，但 `query.ts:428-447,1085-1117` + `autoCompact.ts:201-223` 的长注释还原出完整设计：

* 历史的"折叠"是**读取时投影（read-time projection）**：REPL 的完整历史数组不动，折叠摘要存在独立的 collapse store 里，每次进入循环 `projectView()` 重放提交日志得到投影视图。**历史零销毁、折叠可持久、视图幂等**（"the next projectView() no-ops because the archived messages are already gone from its input"）。
* 阈值分工：90% 开始提交折叠，95% 阻塞生成——autocompact 的触发点（约 93%）正好卡在中间，会跟 collapse 抢跑并通常获胜，"把 collapse 正要拯救的粒度化上下文核爆掉"，所以 collapse 开启时直接禁用 proactive autocompact（`autoCompact.ts:201-215`）。**两套上下文管理系统不能共存竞争，必须一个拥有问题。**
* 413 恢复时 `recoverFromOverflow` 先"泄洪"：把已暂存（staged）的折叠全部提交再重试，只重试一次（transition 标记 `collapse_drain_retry` 防循环），还不行才轮到 reactive compact（`query.ts:1085-1117`）。
* 一个模块级状态的惨案预防：ctx-agent（querySource=marble_origami）自己上下文爆了触发 autocompact 的话，`runPostCompactCleanup` 会 `resetContextCollapse()`——而那是**主线程共享的模块级提交日志**，等于 fork 把主线程的折叠历史清了。所以这个 querySource 直接禁 autocompact（`autoCompact.ts:174-183`）。

## 5. L4：autocompact（主压缩）——触发、执行、重建三段

### 5a. 触发算术（autoCompact.ts:28-91）

```
有效窗口 = min(模型窗口, env 覆盖) − min(模型 maxOutput, 20_000)   ← 给摘要输出留座
触发阈值 = 有效窗口 − 13_000（AUTOCOMPACT_BUFFER）
警告阈值 = 触发阈值 − 20_000；阻塞下限 = 有效窗口 − 3_000（手动 /compact 保命额度）
```

20k 这个数不是拍的：**"基于 compact 摘要输出的 p99.99 = 17,387 tokens"**（29-30 行）。阻塞下限特意留 3k，是为了用户在关掉自动压缩时还能手动跑 /compact 自救（`query.ts:592-593`）。

熔断：连续失败 ≥3 次后本会话不再尝试。依据同样是遥测：**"BQ 2026-03-10：1,279 个会话单会话连续失败 50+ 次（最高 3,272 次），全球每天浪费约 25 万次 API 调用"**（67-70 行）。失败计数通过 `AutoCompactTrackingState.consecutiveFailures` 在主循环 State 里跨迭代传递，成功清零。

递归守卫：querySource 为 `compact` / `session_memory` 的调用（它们自己就是 fork 出来做压缩的 agent）绝不触发 autocompact——否则死锁："compact agent 需要运行才能降低 token 数，却因 token 太多被阻塞"（`query.ts:601-603`，`autoCompact.ts:169-173`）。

### 5b. 执行（compact.ts:387-643）

顺序：PreCompact hooks（可注入自定义摘要指令，与用户指令合并）→ 生成摘要 → 清 readFileState → 造重建附件 → SessionStart hooks → 组装边界与摘要消息。

**摘要由 fork 的子代理完成，且默认复用主对话的 prompt 缓存**：fork 继承主对话消息 + 冻结的系统提示词字节，摘要请求只是在缓存前缀之后追加一条 user 消息。实验注释给了量化依据：不共享缓存的路径 98% cache miss，占全舰队 cache_creation 的 0.76%（约 380 亿 token/天）（431-438 行）。**摘要本身是最贵的一次调用，它的成本也要被缓存工程覆盖。**

**摘要请求自己也会 413**（CC-1180）：解法是 `truncateHeadForPTLRetry`——按 **API 轮次分组**（`grouping.ts`：以 assistant message.id 变化为界切组，保证 tool_use/tool_result 永不被拆开）从头部丢最旧的组，重试至多 3 次，丢掉的位置插 `[earlier conversation truncated for compaction retry]` 标记。分组器还有一段防御性论证：畸形对话（悬空 tool_use）不在分组层修，交给 fork 侧的 `ensureToolResultPairing` 在 API 层修——**在错误的层修复会把门闩永久卡死，把所有后续轮次并成一组**（`grouping.ts:34-42`）。

**摘要 Prompt 解剖**（`prompt.ts`）：

* 开头是激进的 **NO_TOOLS_PREAMBLE**："工具调用会被拒绝并浪费你唯一的一轮"。数据背书：缓存共享的 fork 必须继承父级完整工具集（缓存键匹配所需），Sonnet 4.6 上模型有 2.79% 概率无视弱提示去调工具（4.5 上只有 0.01%），maxTurns:1 下一次被拒的工具调用=零文本输出=整轮报废（19-26 行）。**提示词强度是按失败率数据校准的，而且开头一遍结尾（NO_TOOLS_TRAILER）再一遍。**
* 要求先输出 `<analysis>` 草稿块再输出 `<summary>`——analysis 是"提升摘要质量但写完即无价值的打草稿区"，`formatCompactSummary` 会把它整块剥掉，不进上下文（311-335 行）。**让模型先想后写，但只为结果付上下文成本。**
* `<summary>` 固定 9 段：主要请求与意图 / 关键技术概念 / 文件与代码段 / 错误与修复（特别强调用户纠正）/ 问题解决 / **全部用户消息**（原话保留，"理解用户反馈和意图变化的关键"）/ 待办 / 当前工作 / 可选下一步。第 9 段有防漂移设计：下一步必须与用户最近的显式请求直接一致，且**必须附上最近对话的逐字引用**证明衔接点（76-77 行）。
* 还有 partial compact 双向变体：`from`（摘要近段、保留早段）与 `up_to`（摘要早段、缓存命中友好——模型只看被摘要的前缀）两套措辞（145-267 行）。
* 支持用户/hook 注入的 Compact Instructions（"压缩时重点保留测试输出"之类）。

> **LilBot 对照**：`SUMMARY_INSTRUCTION` 的 9 段与 CC 几乎一一对应（LilBot 就是从这套结构学的），但缺：NO_TOOLS 双重防线（LilBot 的摘要器不给工具所以暂无此风险）、`<analysis>` 草稿区、摘要请求自身 413 的截头重试、fork 复用缓存（LilBot 的 `_summarize` 是独立小对话，摘要调用 100% cache miss——**这是 LilBot 可落地的高价值借鉴点**：把摘要请求改为"完整历史 + 尾部追加摘要指令"的形式，与主对话共享前缀缓存）。

### 5c. 压缩后重建（compact.ts:517-643 + 常量 122-130）

摘要替换历史后，模型"失忆"的不只是对话，还有工作状态。CC 逐项重建，全部带预算：

| 重建项 | 预算 | 说明 |
|---|---|---|
| 最近读过的文件 | ≤5 个文件、单文件 ≤5k tokens、总计 ≤50k tokens | 从压缩前的 readFileState 快照恢复（`POST_COMPACT_*` 常量） |
| 已加载 skills | 单个 ≤5k、总计 ≤25k | 只恢复被调用过的 skill 内容；**特意不重发约 4k 的完整 skill 目录**——纯 cache_creation 开销、边际收益小（524-529 行） |
| 计划/计划模式 | — | plan 附件 + "你仍在 plan mode"的指令附件，防止压缩后越权写文件 |
| 已发现的延迟工具 | — | 压缩前从消息里提取 `preCompactDiscoveredTools` 写进边界消息元数据，压缩后的 schema 过滤器据此继续发送这些工具的完整 schema（603-611 行） |
| 工具/agent/MCP 目录增量 | — | "压缩吃掉了此前的 delta 附件"，对空历史重放 delta 生成器 = 重新公告全集（563-585 行） |
| 全量记录逃生门 | — | 摘要消息里附 transcript 路径："若需要压缩前的精确代码/报错，读这个文件"（350 行）——**摘要有损，但原始数据永远可达** |
| SessionStart hooks | — | 以 `compact` 来源重跑，用户的会话初始化注入不因压缩丢失 |

另外 autocompact 会先尝试 **session-memory 压缩**（实验路径）：如果那份持续维护的结构化会话笔记（10 段模板，由 fork agent 用 Edit 工具增量更新）足够新，可以直接"笔记 + 修剪消息"替代一次全量摘要（`autoCompact.ts:287-310`）。

> **LilBot 对照**：`RecoveryState`（最近文件 + skill + 工具清单）对应重建表的前三行，且 LilBot 的 Cycle 归档（压缩内容存 `.lilbot/archives/` 可用 recall_archive 搜回）与 CC 的 transcript 逃生门思想相同。LilBot 缺的是：plan 状态恢复、已发现延迟工具的跨压缩传递（压缩后模型要重新 ToolSearch 一遍）、预算化的恢复上限（LilBot 是固定 5 文件 × 4k 字符，无总预算）。

## 6. L5：反应式恢复链【还原】

`query.ts` 的扣留-恢复机制（详见对比大表 #8/#9，这里只列压缩相关的骨架）：

1. 流式过程中检测到 prompt-too-long / 媒体超限 / max_output_tokens 错误 → **不 yield 给消费者**（SDK 消费者见 error 字段直接杀会话，而恢复循环还在跑，"nobody is listening"，`query.ts:166-179`），推入内部数组。
2. 流结束后：413 先走 collapse drain（一次）→ 还不行走 reactive compact（一次，`hasAttemptedReactiveCompact` 防螺旋）→ 都不行才 yield 被扣留的错误并返回 `prompt_too_long`。
3. **绝不对 API 错误跑 stop hooks**：模型没产出真响应，hook 评估它会造成"错误→hook 注入更多 token→重试→错误"的死亡螺旋——这个 guard 在两处独立出现（1168-1175, 1258-1265 行），注释记录了它防住的真实无限循环事故（1292-1297 行："resetting to false here caused an infinite loop … burning thousands of API calls"）。
4. task_budget 的跨压缩记账：压缩后服务端只能看到摘要，会少算已花预算，所以每次压缩前把"压缩前最终上下文窗口"累计进 `taskBudgetRemaining` 传回 API（`query.ts:283-291,504-515`）。**任何依赖"服务端能看到完整历史"的机制，在压缩边界都需要显式补账。**

---

## 7. 汇总：CC 压缩流水线的 10 条设计律

1. **便宜的先跑，贵的当兜底**：内容替换 → 删消息 → 清正文 → 折叠 → 摘要 → 反应式，每级都可能让下一级变 no-op。
2. **缓存与空间是两个目标，逐级权衡**：frozen 结果永不替换；清正文只在缓存已冷时做；摘要 fork 复用缓存；服务端 cache_edits 是终极形态（删内容且不碰缓存）。
3. **谁改历史谁补账**：snip 补 tokensFreed，压缩补 task_budget remaining，主动破坏缓存要向断裂检测器自首。
4. **恢复者唯一性**：collapse 开启就关 autocompact，reactive 开启就关合成 413——一个故障一个 owner，防止抢跑。
5. **失败路径也要预算**：摘要 413 截头重试 ≤3，熔断 ≤3，反应压缩每故障一次。
6. **有损压缩必配无损逃生门**：transcript 路径 / Cycle 归档 / 落盘工具结果，模型永远能读回原文。
7. **压缩后重建工作状态**，且逐项带 token 预算，不是"能恢复多少恢复多少"。
8. **阈值来自分布，不来自直觉**：20k = 摘要输出 p99.99；熔断 3 = BQ 浪费分析；NO_TOOLS 双保险 = 2.79% 失败率。
9. **拆分永不破坏 API 不变量**：一切切分以 API 轮次为界，tool_use/tool_result 配对神圣不可分割；畸形输入在正确的层（API 序列化层）修复。
10. **token 计数信 API、防陈旧**：以响应回报的 usage 为准，估算只兜底，并把"计数何时失效"写进类型与参数。

## 8. LilBot 借鉴清单（压缩专项，按性价比排序）

| 优先级 | 借鉴项 | LilBot 落点 | 预估工作量 |
|---|---|---|---|
| ★★★ | 摘要 fork 复用前缀缓存：摘要请求改为「完整消息数组 + 尾部追加摘要指令 user 消息」而非独立小对话 | `agent.py::_summarize` / `compaction.py::_summarize_with_retry` | 小 |
| ★★★ | token 计数优先用上次响应 usage，chars/4 兜底；snip/prune 后显式补差 | `compaction.py::estimate_tokens` + `agent.py::_add_usage` 已有数据 | 小 |
| ★★☆ | prune 绑定"缓存冷热"判定：距上次调用超过缓存 TTL（5 分钟）才清正文，否则跳过 prune 直接评估摘要 | `compaction.py::auto_compact` Layer 1 入口 | 小 |
| ★★☆ | 摘要 prompt 增加 `<analysis>` 草稿区并在入上下文前剥离；第 9 段"下一步"要求逐字引用锚点 | `compaction.py::SUMMARY_INSTRUCTION` | 小 |
| ★★☆ | 摘要请求自身溢出的截头重试（按 assistant 轮次分组丢头部，≤3 次） | `compaction.py` 新增 `truncate_head_for_retry` | 中 |
| ★☆☆ | 压缩边界消息记录 `pre_compact_discovered_tools`，压缩后延迟工具不用重新 ToolSearch | `agent.py::compact` + `registry.py::_discovered` | 小 |
| ★☆☆ | 恢复附件加总 token 预算（对齐 50k/25k 的预算化思路） | `compaction.py::RecoveryState.build_attachment` | 小 |

---

# 第二部分：全量补遗（compact/ 目录 11 个文件全部读完后的增补）

> 第一部分基于骨架阅读；本部分补齐剩余源码：`streamCompactSummary`、`partialCompactConversation`、`sessionMemoryCompact.ts`（630 行）、`apiMicrocompact.ts`、`postCompactCleanup.ts`、`timeBasedMCConfig.ts`、`truncateHeadForPTLRetry`。
> 修正一个结论：压缩不是"五级"，完整数下来是 **八条路径**——L0 工具结果预算、L1 snip、L2 microcompact（时间触发 / 缓存编辑双路）、L3 collapse、L4a session-memory compact、L4b LLM autocompact、L4c partial compact（手动选点）、L5 reactive、外加 **L6：API 原生 context management**。

## 9. L6（新发现）：API 原生 context management —— 把压缩下沉到服务端

文件：`apiMicrocompact.ts`（153 行）

请求体里声明式的 `context_management.edits` 策略，让 **API 服务端**在采样前自己清理上下文：

* `clear_thinking_20251015`：控制历史 thinking 块保留。触发条件与 L2 时间路径同源——**空闲 >1h（缓存必死）时只保留最后一轮 thinking**，否则全保。细节：API schema 要求 `value >= 1`，且"省略这条 edit"会落回模型策略默认（往往是 all，等于没清）——所以想清就必须显式发 `{thinking_turns: 1}`（77-87 行）。
* `clear_tool_uses_20250919`：服务端按 `input_tokens` 触发（默认 180k），`clear_at_least` 指定至少清回 140k（180k−40k 目标），`clear_tool_inputs` 指定可清结果的工具清单（Shell/Grep/Glob/Read/WebFetch/WebSearch——与客户端 L2 的 COMPACTABLE_TOOLS 刻意对齐，15 行注释）。

**意义**：这是压缩演进的终点形态——L2 客户端清正文（破坏缓存）→ L2b cache_edits（服务端缓存原位删）→ L6 完全声明式（连触发判断都交给服务端）。客户端压缩逻辑逐层"上收"到 API。

## 10. `streamCompactSummary` 内幕：fork 缓存共享的三个坑（compact.ts:1136-1396）

1. **禁止设置 maxOutputTokens**（1181-1187 行注释）：fork 想蹭主线程缓存，就必须让缓存键参数（system、tools、model、消息前缀、**thinking config**）逐字节一致；而 `maxOutputTokens` 会经 `Math.min(budget, maxOutputTokens-1)` 钳制 thinking 的 budget_tokens → thinking config 不一致 → 缓存作废。**缓存键的传染面比直觉大：一个看似无关的输出参数会经由推理配置间接改写缓存键。**
2. **30 秒心跳**（1159-1176 行）：压缩调用要跑 5-10+ 秒，期间传输层没有任何消息流动，远程会话的 WebSocket 会因 idle 被服务器关掉。解法：`setInterval` 每 30s 发会话活动信号 + 重发 'compacting' 状态。**长静默的后台调用必须自己维持连接活性。**
3. **中止伪成功防护**（1205-1210 行）：`query()` 会把 API 错误（含用户按 Esc 的 abort）包装成合成 assistant 消息 yield 出来。abort 的文本是 "Request was aborted."——**不以 "API Error" 开头**，所以调用方的 `startsWithApiErrorPrefix` 守卫会漏掉它，一次被中止的压缩就"成功"地把这句话当成了摘要。修复：显式检查 `isApiErrorMessage` 标志位而非文本前缀。**用结构化标志判错，文本前缀匹配总有一天会漏。**

另有：缓存命中率作为一等遥测指标上报（cacheHitRate = cache_read / (read+creation+input)，1220-1226 行）；fork 失败落回普通流式路径（可安全设 maxOutputTokens，因为不共享缓存），带重试与指数退避。

## 11. `truncateHeadForPTLRetry` 逐行（compact.ts:243-291）

摘要请求自身 413 的截头重试，四个防御点：

1. **先剥掉自己上次插入的 marker**（250-255 行）：否则 marker 自成 group 0，20% 兜底每次只丢它又重新插回——重试 2+ 零进展死循环。**重试逻辑必须对自己的产物幂等。**
2. **精确 vs 兜底双模式**（260-272 行）：能从 413 错误里解析出 token 缺口就按组累计丢到刚好覆盖缺口；解析不出（某些 Vertex/Bedrock 错误格式）就丢 20% 的组。
3. **保底一组**（274-276 行）：全丢光就没东西可摘要了。
4. **首消息必须是 user**（281-288 行）：分组器把 preamble 放 group 0，后续每组以 assistant 开头；丢掉 group 0 后序列以 assistant 开头会被 API 拒绝 → 前插合成 user marker，由此产生的孤儿 tool_result 交给 `ensureToolResultPairing` 在 API 层修。

注释还诚实标注了债务来源：reactive 路径有"正经的从尾部剥离重试"，这个头部截断是 proactive/manual 路径在 bfdb472f 统一重构时没迁移完留下的"笨但安全"的兜底（236-241 行）。

## 12. L4a：session-memory compact 完整机制（sessionMemoryCompact.ts，630 行）

免 LLM 调用的压缩路径：用那份持续维护的会话笔记直接充当摘要。完整流程：

1. **前置**：等待进行中的笔记抽取完成（带超时）；笔记文件不存在或仍是空模板 → 返回 null 走 LLM 压缩。
2. **保留窗计算**（`calculateMessagesToKeepIndex`，324-397 行）：从 `lastSummarizedMessageId`（笔记覆盖到哪条消息）之后起步，**向后扩张**直到同时满足 min 10k tokens 且 ≥5 条含文本块的消息，触到 max 40k 硬顶即停。扩张的 **floor 是上一个压缩边界**——越过边界会破坏磁盘上 preserved-segment 链的重连不变量（364-371 行）。三个参数全部 GrowthBook 可调，且远程值必须为正数才生效（"零值不许覆盖合理默认"，113-114 行）。
3. **API 不变量对齐**（`adjustIndexToPreserveAPIInvariants`，232-314 行）：切点回退两步走——① kept 范围内**所有** tool_result 的 tool_use 必须在范围内，否则向前扩到包含配对的 assistant（注释给出完整 bug 复盘：只检查第一条消息时，orphan tool_use 被排除但 tool_result 还在 → API 报错）；② 流式会把同一响应按内容块拆成多条 message.id 相同的 assistant 消息，若切点落在中间，前面的 **thinking 块**会丢失合并对象——同 id 的都要收进来。**LilBot 对照**：`_align_keep_start` 只处理"tool 消息不做队首"这一种情形，因为 LilBot 不按内容块拆消息，②天然不存在；①的跨距搜索在 OpenAI 消息格式（tool 紧跟 assistant）下也退化为连续回退——简化是格式差异换来的，不是偷工。
4. **恢复会话特例**（561-566 行）：resume 后 `lastSummarizedMessageId` 丢了但笔记有内容 → 保留窗从零开始纯靠扩张规则重建。找不到 ID（消息被改过）→ 老实返回 null。
5. **事后验收**（604-614 行）：构建完压缩结果后**再算一遍总 token**，若仍 ≥ autocompact 阈值 → 返回 null 让 LLM 压缩接手。**便宜路径必须自证有效，否则让位，不能假装成功。**
6. **笔记超长的处理**：按节截断防止笔记吃光压缩后预算，并在摘要里附全文路径（459-474 行）——又是"有损必配逃生门"。

## 13. L4c：partial compact——用户选点的双向部分压缩（compact.ts:772-1106）

用户在消息选择器里挑一个 pivot，选方向：`from`（摘要 pivot 之后、保留之前）或 `up_to`（摘要 pivot 之前、保留之后）。三个值得记的点：

* **`up_to` 必须从保留段剥掉旧的压缩边界/摘要**（785-799 行）：新摘要 summary_B 位于 kept **之前**，若 kept 里还留着旧 boundary_A，`findLastCompactBoundaryIndex` 的**后向扫描**会先命中 boundary_A，把 summary_B 整个丢掉。`from` 方向则相反——摘要在 kept 之后，后向扫描无碍，且删旧摘要会丢它覆盖的历史。**同一个数据结构（压缩边界），扫描方向不同，清理规则完全相反。**
* **缓存策略随方向变**（852-854 行）：`up_to` 只发被摘要的前缀（正好是缓存前缀，直接命中）；`from` 发全量（尾巴反正缓存不了）。
* **delta 附件对 kept 做 diff**（955-975 行）：全量压缩对空历史重放（重新公告全集），部分压缩只补"被摘要掉的那半"缺的公告——kept 里已公告过的跳过。`preCompactDiscoveredTools` 则干脆对 allMessages 取并集："set union 幂等，比追踪每个工具住在哪一半简单"（1021-1022 行）。

另：`annotateBoundaryWithPreservedSegment`（349-367 行）给边界消息写 `{headUuid, anchorUuid, tailUuid}` 重连元数据——保留段消息在磁盘上保持原 parentUuid 不重写（dedup-skip），resume 加载器靠这三个 UUID 把链重新接好。**持久化层不改写历史，用元数据描述新拓扑。**

## 14. `postCompactCleanup`：压缩后的统一失效清单（77 行）

一次压缩会使一批模块级缓存/追踪状态失效，CC 把清理集中到一个函数，所有压缩路径（auto/manual/reactive/SM）统一调用：

* **要清的**：microcompact 状态、collapse 提交日志、getUserContext 记忆缓存（注释记录了分层缓存的坑：只清内层 getMemoryFiles 缓存没用，外层 memoized 的 getUserContext 命中后根本走不到内层——**多层缓存要么一起清，要么白清**）、系统提示词分节、分类器批准、投机检查、beta tracing、会话消息缓存。
* **有意不清的**：invoked skill 内容（要跨多次压缩存活供附件重建用）、sentSkillNames（重发 4k 的技能目录纯属浪费）。**不清什么和清什么一样需要写出理由。**
* **主线程守卫**：子代理与主线程同进程共享模块级状态，子代理压缩时若重置 collapse 日志/记忆缓存，等于把**主线程的**状态清了——按 querySource 前缀区分，只有主线程压缩才动共享状态。

## 15. 补遗后的增量设计律（接第一部分的 10 条）

11. **缓存键的传染面比直觉大**：maxOutputTokens → thinking budget → 缓存键。凡想共享缓存，参数必须逐字节审计。
12. **判错用结构化标志，不用文本前缀**：abort 文本不带 "API Error" 前缀的伪成功是真实事故。
13. **重试对自己的产物幂等**：截头 marker 不剥掉就是死循环。
14. **便宜路径要事后自证**：SM-compact 压完再验一遍阈值，不达标就让位给贵路径。
15. **不清什么和清什么一样要有理由**：失效清单集中管理，每一项"留"都写明为什么。
16. **同一结构在不同扫描方向下清理规则可以相反**：up_to 剥旧边界 / from 保旧边界。
17. **持久层不改写历史**：用 head/anchor/tail 重连元数据描述新拓扑，原始链原样保留。

## 16. 借鉴清单增补（接第一部分表格）

| 优先级 | 借鉴项 | LilBot 落点 | 说明 |
|---|---|---|---|
| ★★★ | 压缩后统一失效清单 `_post_compact_cleanup` | `agent.py::compact` | LilBot 压缩后 RecoveryState、`_surfaced_memory_ids`、catalog 缓存均无统一清理点；照 CC 集中成一个函数并注明"不清什么" |
| ★★☆ | SM-compact 思路：Cycle 归档升级为"活文档充当摘要" | `cycles.py` + `compaction.py` | LilBot 已有归档；差一步"归档新鲜时跳过 LLM 摘要，直接用归档 + 保留窗"，并加事后阈值自证 |
| ★★☆ | 判错用结构化标志 | `compaction.py::is_context_overflow_error` | LilBot 目前纯文本匹配错误串（_OVERFLOW_MARKERS）——Provider 层应把 HTTP 413/400 状态码带出来作为结构化信号，文本匹配降为兜底 |
| ★☆☆ | API 原生 context management | `llm/providers.py` | 若接入支持 `context_management` 的 API（Anthropic），一行声明可替代整个 L2 |
| ★☆☆ | 保留段重连元数据 | `core/session.py` | LilBot 会话是整体快照无此需求；若做增量持久化再考虑 |

---

---

*本报告基于 2026-07 的源码快照；行号以该快照为准。*
