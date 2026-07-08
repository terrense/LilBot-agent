# LilBot vs Claude Code 源码对标：架构与技巧全景对比

> 对标对象：`../claude-code-source-code`（TypeScript/React-Ink，1884 个源文件，512,664 行 ≈ 51.3 万行）
> 本方：`lilbot/`（Python，16,725 行核心代码，不含 5,194 行测试）——体量约为 CC 的 1/31
> 关注点：只看架构、设计与技巧细节，不比语言与 UI 华丽程度。

## 总对比大表

| # | 维度 | LilBot 的做法（代码位置） | Claude Code 的做法（代码位置） | 谁强 / 差距 | 可借鉴的技巧 |
|---|------|--------------------------|------------------------------|------------|--------------|
| 1 | **主循环形态** | 同步生成器 `Agent.run_turn` 单 while 循环，`max_steps` 工具步数预算，超限后强制合成答案（`core/agent.py`） | `query()` 异步生成器 + **显式状态机**：`State` 结构体集中 9 项跨迭代状态，每个 continue 点写一次 `state = {...}`，并记录 `transition.reason`（`collapse_drain_retry` / `reactive_compact_retry` / `max_output_tokens_recovery`…）供测试断言恢复路径（`query.ts:204-217,1099-1251`） | CC 强 | ① 把"为什么继续循环"显式建模成 transition 枚举，恢复逻辑可测试、可观测；② 状态集中成单结构体避免散落赋值 |
| 2 | **流式与工具执行的关系** | 先流完一轮文本/工具调用，再分批执行工具（串行阶段化）（`agent.py::_stream_turn` → `_partition_calls`） | **StreamingToolExecutor：模型还在流式输出时，工具调用一到就开始执行**，结果缓冲后按到达顺序发射；并发安全判定逐工具、逐输入；出错时子 AbortController 立刻杀死兄弟子进程（`StreamingToolExecutor.ts`） | CC 强（重大延迟优势） | 流式与执行重叠是单轮延迟最大的优化点；"sibling abort：一个 Bash 失败立即中止同批其余子进程"值得抄 |
| 3 | **只读并行分批** | 相邻只读工具合批 → `ThreadPoolExecutor` 并行；`concurrency_safe` 由 ToolDef 的 capability 集合静态判定（`agent.py::_partition_calls`，`tools/registry.py`） | 同样的"连续只读合批 + 非安全单批"算法（`toolOrchestration.ts::partitionToolCalls`）——但 `isConcurrencySafe(input)` 是**逐输入**判定：同一个 Bash 工具，`ls` 判并发安全、`rm` 不安全；判定抛异常时保守当不安全 | 思路同源（LilBot 就是从这里学的）；CC 粒度更细 | 把并发安全从"工具级"下沉到"输入级"（尤其 shell 类工具解析命令后判定） |
| 4 | **工具契约** | `ToolDef` dataclass：name/description/JSON-schema/handler + capability 集合 + 审批级别 + should_defer（`tools/registry.py`） | `Tool` 接口 60+ 方法：zod 输入 schema（强类型校验）、`validateInput`（模型可读的失败原因）与 `checkPermissions`（权限）分离、`isReadOnly(input)`/`isDestructive(input)` 逐输入、`maxResultSizeChars` 逐工具、`interruptBehavior()`、`contextModifier`（工具执行后函数式修改上下文）、每个工具自带全部 UI 渲染方法（`Tool.ts`）；`buildTool()` 统一填充 fail-closed 默认值（默认不并发安全、默认非只读） | CC 强 | ① `validateInput`（告诉模型为何失败）与 `checkPermissions`（问用户）分离是清晰的职责切分；② fail-closed 默认值集中在一个 builder；③ `contextModifier` 让工具以纯函数方式改写后续上下文 |
| 5 | **工具规模化 / 延迟加载** | `defer_all_except(core)` 白名单常驻 + ToolSearch 按需拉取 + 目录字节稳定缓存 + fingerprint（`tools/registry.py`） | 同一思想：`shouldDefer` / `alwaysLoad`（MCP 经 `_meta['anthropic/alwaysLoad']`）+ `searchHint` 字段专供 ToolSearch 关键词匹配 + API 侧 `defer_loading: true` | 基本打平（LilBot 是有意复刻，且做了 catalog 缓存） | `searchHint`："3-10 词、不与工具名重复的能力短语"，提高延迟工具被搜到的召回率 |
| 6 | **上下文压缩层数** | 两层：本地 prune（清旧工具结果占位符，保缓存）→ LLM 结构化摘要（9 段 handoff）；重试退避 + 熔断 + RecoveryState 防失忆 + Cycle 归档（`core/compaction.py`，`cycles.py`） | **五层以上流水线，按"便宜→昂贵"排序**：① 逐消息工具结果预算（content replacement，按 tool_use_id 落盘换预览）② snip compact ③ microcompact（同 LilBot 的 prune；ant 版更用 API `cache_edits` 让**服务端缓存原位删除**，用 `cache_deleted_input_tokens` 回报精确账）④ context collapse（把历史"折叠"为读取时投影 + 提交日志，粒度化保留，413 时可先 drain 折叠再谈摘要）⑤ autocompact（fork 一个子代理做摘要，阈值 = 窗口 − 20k 摘要预留 − 13k buffer，均来自 p99.99 遥测）⑥ reactive compact（真 413 才触发）（`services/compact/*`，`query.ts:365-543`） | CC 强很多 | ① 压缩做成多级流水线且每级注明"为什么先跑它"；② 阈值用真实分布（p99.99）定，不拍脑袋；③ collapse 的"读取时投影 + 提交日志"设计——历史不销毁、视图可重放 |
| 7 | **token 计数** | 字符数/4 估算（`compaction.py::estimate_tokens`） | **优先用上一次 API 响应回报的真实 usage**（`finalContextTokensFromLastResponse` / `tokenCountWithEstimation` 兜底估算），并明确处理"snip 后 usage 过期"的陈旧性问题（`query.ts:592-648` 大段注释） | CC 强 | 用服务端回报的 token 数为准、估算只做兜底；并把"计数何时会陈旧"当一等公民推理 |
| 8 | **上下文溢出恢复** | 捕获错误串匹配 `context_length_exceeded` 等 → 手动 compact 一次 → 重试（`agent.py::_stream_turn`） | **"扣留错误"模式**：413/max_output_tokens 错误在流中先不 yield 给消费者（否则 SDK 消费者见 error 直接杀会话），推入内部数组；流结束后按链恢复：collapse drain（1 次）→ reactive compact（1 次）→ 恢复失败才浮出错误；且**禁止对 API 错误跑 stop hooks**（防"错误→hook 注入更多 token→重试→错误"死亡螺旋，注释里写明真实事故）（`query.ts:788-825,1062-1183,1258-1265`） | CC 强很多 | ① withheld-error：可恢复错误先扣留、恢复穷尽才暴露；② 恢复路径做成有序链且每级单发；③ 每个防护都注明它防的真实事故 |
| 9 | **输出截断恢复** | 无（依赖模型自然停止） | max_output_tokens 三段恢复：先**同请求 8k→64k 升档重试**（每轮一次）；再注入 meta 消息"不许道歉不许复述，从断点续写"，多轮续写最多 3 次；穷尽才报错（`query.ts:1188-1256`） | CC 独有 | "升档重试→断点续写→放弃"的输出恢复链，meta 提示词的措辞（no apology, no recap, pick up mid-thought）可直接抄 |
| 10 | **模型降级/回退** | Provider 抽象内 HTTP 重试（`llm/providers.py`） | `FallbackTriggeredError` → 切 fallback 模型整轮重试：给已产出的孤儿消息发 **tombstone**（从 UI 和 transcript 抹除）、给孤儿 tool_use 合成 error tool_result、**剥离 thinking 签名块**（签名与模型绑定，回放给别的模型会 400）、丢弃执行器重建（`query.ts:893-953`） | CC 独有 | ① tool_use/tool_result 配对不变量在一切异常路径上都要闭合（合成 error 结果）；② 跨模型重放要剥离模型绑定的块 |
| 11 | **记忆：召回** | JSONL 存储 + LLM 侧查询选相关（≤5 条）+ recent-tools 过滤 + already-surfaced 去重 + 时效警告（`memory/recall.py`） | **memdir**：一条记忆一个带 frontmatter 的 md 文件 + `MEMORY.md` 索引常驻系统提示词（200 行 / 25KB 双上限，超限截断并回写警告）；`findRelevantMemories` 用 Sonnet sideQuery + **json_schema 强制结构化输出**；同样有 recent-tools 过滤与 alreadySurfaced（LilBot 即照此复刻）；**关键差异：prefetch 与主模型流式并行跑**（`using` 自动清理），选择结果在工具执行后才消费，延迟完全被隐藏（`memdir/findRelevantMemories.ts`，`query.ts:301-304`） | 设计同源；CC 的并行预取与结构化输出更成熟 | ① 侧查询全部改并行预取（recall 在 LilBot 是回合开始的阻塞点）；② 用 json_schema 输出格式替代"求你只回 JSON"的提示词 |
| 12 | **记忆：会话级** | 每 3 回合抽取记忆（`memory/extract.py`）+ 压缩时归档 Cycle | **SessionMemory**：一份固定 10 段模板的"会话笔记"文档（Current State / Errors & Corrections / Learnings / Worklog…），由 fork 的子代理**用 Edit 工具增量更新**（不是重写），提示词严格保护模板结构；压缩时同步更新（`services/SessionMemory/`） | CC 强 | "让子代理拿 Edit 工具维护一份结构化活文档"替代每次全文重写——增量、可 diff、模板即 schema |
| 13 | **子代理** | `SubAgentManager`：11 个内置角色 + `.md` frontmatter 自定义、创建/运行两阶段 5 道 gate、信号量并发、JSONL transcript、任务落盘重启恢复、worktree 隔离（`subagents/manager.py`） | AgentTool：`.md` frontmatter 定义（同源）、**fork 子代理复用父级 prompt 缓存**（父系统提示词字节冻结传入，避免重渲染分叉 bust 缓存）、**每个 agent 可在 frontmatter 声明专属 MCP servers**（启动连接、结束清理）、agentMemory 快照、后台运行/resume、sidechain transcript 持久化（`AgentTool/runAgent.ts`，`forkSubagent.ts`） | 各有所长：LilBot 的 gate 体系与重启恢复很硬；CC 的缓存共享与 per-agent MCP 更深 | ① fork 时冻结父提示词字节以共享缓存；② 子代理级 MCP server 声明（能力即插即用）③ LilBot 已有的重启恢复反而是 CC 里不明显的点，面试可讲 |
| 14 | **多 Agent 团队** | TeamManager + 文件信箱（O_EXCL 锁 + 陈旧回收 + Windows 兼容）+ JSON 看板（assignee/blocks/blocked_by）+ lead 每迭代 drain 注入（`teams/*`） | TeamCreate/SendMessage/TaskCreate·Get·List·Update·Stop 全套工具 + InProcessTeammateTask + coordinator 模式（`tools/Team*`, `tasks/InProcessTeammateTask/`） | 结构同源（LilBot 明写 mirrors）；CC 工具面更全 | Task 系工具族的粒度切分（Create/Get/List/Update/Output/Stop 各一个工具而非一个大工具） |
| 15 | **权限体系** | capability + 审批级别 + plan-mode 门（JSON 状态文件）+ execpolicy（灾难命令 deny + **arity 感知前缀白名单**：`git status -s` 放行、`git push` 不放）+ PowerShell 令牌级安全分析 388 行（`sandbox/*`） | 多层：规则源分层（alwaysAllow/Deny/Ask **by source**：用户/项目/策略）、规则语法 `Bash(git *)` 解析与 shell 匹配、权限模式状态机（default/plan/acceptEdits/bypass/auto + prePlanMode 恢复）、路径校验与 additionalWorkingDirectories、**LLM 分类器**（bashClassifier：自然语言描述的允许/询问/拒绝规则由小模型判定；yoloClassifier：auto 模式整批 tool_use 交给带 CLAUDE.md 上下文的分类器）、拒绝计数回退提示、bypass killswitch、被遮蔽规则检测（`utils/permissions/` 23 个文件） | CC 强很多（这是产品级安全面）；但 LilBot 的 arity 前缀匹配与 PowerShell 分析是认真做的、方向正确 | ① 规则按"来源"分层且可检测互相遮蔽；② 规则语法 `Tool(pattern)` 统一表达；③ 自然语言权限规则 + 小模型分类器是新范式 |
| 16 | **Hooks** | `hooks.json`：4 事件（turn_start/pre_tool/post_tool/turn_end）× 3 动作（prompt/block/command），命令非零退出即阻断，mtime 热重载（`hooks/engine.py`） | 设置驱动 + 15+ 事件（PreToolUse/PostToolUse/PostToolUseFailure/UserPromptSubmit/SessionStart/SubagentStart/Stop/PreCompact/PermissionRequest/PermissionDenied/WorktreeCreate/FileChanged/Elicitation…），**结构化 JSON 输出协议**：decision、updatedInput（hook 可改写工具入参！）、additionalContext、continue=false 阻止停止、async hooks、prompt 交互协议；**Stop hooks 能阻止模型停下来并强制续跑**（主循环一等公民，`query.ts::handleStopHooks`）（`utils/hooks.ts` 5022 行） | CC 强很多 | ① hook 返回结构化 JSON 而非仅退出码（能改写入参、注入上下文）；② Stop hook——"模型想停时由 hook 决定能不能停"，做验收闭环极有用 |
| 17 | **Prompt 缓存纪律** | 稳定前缀（瞬态 system 消息放尾部）+ 工具目录字节稳定 + fingerprint（`agent.py::_provider_messages`） | 全库级纪律：**发给 API 的消息对象永不变异**（观察者要看就克隆——`backfillObservableInput` 只改副本，注释明写"mutating it would break prompt caching (byte mismatch)"）、fork 冻结系统提示词、`skipCacheWrite`、**缓存断裂检测服务**（promptCacheBreakDetection：压缩/删除时主动通知，追踪谁弄坏了缓存）、cached microcompact 用 API 缓存编辑原位删除 | CC 强很多 | ① "API-bound 对象不可变，观察用克隆"上升为全库不变量；② 专门做一个"缓存断裂归因"服务，缓存命中率可运营 |
| 18 | **可观测性** | 事件 dataclass 流（ToolStarted/Finished/TurnFinished）+ 子代理 JSONL transcript + usage 累计（`core/events.py`） | 全链路：`logEvent('tengu_*')` 带类型化元数据（连元数据类型名都叫 `AnalyticsMetadata_I_VERIFIED_THIS_IS_NOT_CODE_OR_FILEPATHS` 防泄漏）、queryProfiler/headlessProfiler 打点、Perfetto tracing、TTFT/OTPS、成本追踪、**遥测反哺设计**（注释直接引用 BQ 分析："1279 个会话连续失败 50+ 次、日耗 25 万次 API 调用"→ 于是熔断阈值=3） | CC 强很多 | ① 打点命名空间化 + 元数据类型化防敏感泄漏；② 把"这个常量为什么是 3"用数据写进注释——遥测→决策→代码的闭环 |
| 19 | **会话持久化/回滚** | SessionStore 每会话一 JSON + resume + FileHistory 编辑前快照 /rewind（`core/session.py`，`history.py`） | transcript 全量落盘 + 每子代理 sidechain transcript + content-replacement 记录随会话恢复重建 + VCR 录制回放（测试与 resume 共用同一序列化，靠 hash 防 drift） | CC 强（但 LilBot 五脏俱全） | VCR：把真实 API 交互录成 fixture 回放测试，等价于给主循环上"回归录像带" |
| 20 | **Skills** | md + frontmatter，args 模板、allowed-tools、bundled+项目双源（`skills/registry.py`） | 同构（bundled skills、SkillTool、命令即技能）+ **skill 发现预取与模型流式并行**（97% 的阻塞发现调用在生产中一无所获→改成非阻塞预取）+ 插件系统统一分发 skills/agents/hooks/MCP | 结构打平；CC 的预取与插件分发更进一步 | 又是同一模式：**一切侧查询并行化、藏进主流延迟里**（出现第 3 次了：memory、skill、toolUseSummary 都这么干） |
| 21 | **Provider 抽象** | `BaseProvider` 接口 + OpenAI 兼容实现 + **RuleBasedProvider 离线规则桩**（无网跑全流程/测试）（`llm/providers.py`） | 绑定 Anthropic SDK（一方 + Bedrock/Vertex），无多厂商抽象；但有 fallback 模型、重试、fetch 注入调试 | **LilBot 强**（私有化部署刚需：可接 DeepSeek/内网模型；离线桩让 241 个测试不花一分钱） | —— 这是 LilBot 面试可主打的差异化：多 Provider + 离线可测 |
| 22 | **发布工程** | config + 环境变量 | `feature()`（bun:bundle 编译期死代码消除，ant 内部功能在外发构建里物理不存在）+ GrowthBook 运行时灰度 + 门控实验；同一代码库产内外两种构建 | CC 强（这是发布成熟度，不是架构） | 编译期 feature 门控 = 灰度 + 保密 + 包体三合一的思路 |
| 23 | **UI 架构** | prompt_toolkit 两套 TUI（classic/dashboard），事件驱动渲染 | React+Ink 声明式终端 UI；**渲染责任下放到每个工具**（工具自带 renderToolUse/Progress/Rejected/Error/Grouped 全套方法）；transcript 搜索有"索引=可见文本"保真测试 | CC 强（不作为对标重点） | "工具自渲染"模式：新工具=一个文件闭环（逻辑+权限+UI），无需改中枢 |
| 24 | **测试与验证** | 241 个 pytest（含 subtests），离线 Provider 桩 | VCR 录放、渲染保真测试（索引/高亮一致性）、transition 断言（见 #1）、drift 断言（常量与 source-of-truth 相等测试） | CC 强 | "防 drift 测试"：被迫复制的常量/逻辑写一个相等断言测试锁住 |

## 本质差距的一句话总结

- **CC 的护城河不在"功能多"，而在三件事**：
  1. **失败路径工程**：每一种 API/流式/工具失败都有编号的恢复链（扣留→逐级恢复→穷尽才暴露），且每个防护注释都引用它防住的真实事故（死亡螺旋、25 万次浪费调用、孤儿 thinking 块 400）。LilBot 只覆盖了其中 2~3 条最常见路径。
  2. **延迟工程**：一切侧查询（记忆召回、skill 发现、工具摘要）都与主模型流式并行；工具执行与流式重叠。LilBot 的 recall/extract 是回合边界上的串行阻塞点。
  3. **缓存工程**：从"消息对象不可变"的全库不变量，到缓存断裂归因服务，再到用 API cache_edits 做服务端原位删除——缓存命中率被当成可运营的指标。LilBot 有意识（稳定前缀、目录缓存）但没有闭环度量。

- **LilBot 的相对优势（面试可讲）**：
  1. **多 Provider 抽象 + 离线规则桩**——私有化/内网场景 CC 根本不支持；
  2. **子代理 5 道创建/运行 gate + 任务落盘重启恢复**——权限收敛与崩溃恢复做得比 CC 的对应路径更显式；
  3. **PowerShell 令牌级安全分析、arity 感知命令白名单**——Windows 内网环境的针对性安全，CC 的 shell 匹配以 POSIX 为主；
  4. 体量 1/31，核心机制（分批并行、延迟加载工具、两层压缩、LLM 记忆召回、teams 信箱/看板）与 CC 同构，是一份"可讲清楚每一行"的实现。

## 建议的借鉴优先级（如果只做五件事）

1. **侧查询并行预取**（#11/#20 模式）：把 `_maybe_recall` / `_maybe_extract` 改成回合开始发起、工具执行后消费的后台线程——零风险、立竿见影降延迟。
2. **withheld-error + 有序恢复链**（#8/#9）：溢出/截断错误先扣留，collapse→compact→放弃逐级恢复；补上 max_output_tokens 断点续写。
3. **token 计数改用 API usage 回报**（#7）：LilBot 已在累计 usage，只差把 `estimate_tokens` 的触发判断换成"上次响应回报的真实上下文 + 估算兜底"。
4. **hook 结构化输出协议 + Stop hook**（#16）：让 hook 能改写工具入参、注入上下文、阻止停止——LilBot hooks 引擎 109 行，加这个协议成本很低。
5. **isConcurrencySafe 下沉到输入级**（#3）：至少对 shell 类工具按命令内容判定，能显著扩大可并行面。

## 复刻进度（持续更新）

原则：**凡是弱于 CC 且不依赖 Claude 模型本身的项，按价值顺序逐个模仿。** 当前测试 298 通过（起点 241）。

### ✅ 已复刻（可移植差距已闭合）

| 维度 | 复刻内容 | 落点 | 测试 |
|---|---|---|---|
| #6 上下文压缩 | 八条压缩路径中可移植的 27/30（L0 工具结果预算三态机、缓存冷热感知 prune、截头重试、`<analysis>` 剥离、前缀缓存共享摘要、partial from/up_to、统一 cleanup…） | `compaction.py`、`tool_budget.py` | `test_compaction_cc.py`（20） |
| #7 token 计数 | 优先用 Provider 回报的真实 `prompt_tokens`，估算兜底；摘要后清陈旧计数 | `agent.py::_add_usage/_post_compact_cleanup` | 同上 |
| #11 记忆召回 | 召回改并行预取（daemon 线程，工具执行后消费）+ 抽取后台化 | `agent.py::SideQueryPrefetch` | `test_recall_prefetch.py`（5） |
| #3 只读并行分批 | `isConcurrencySafe` 下沉到**输入级**：`bash("ls")` 可并行、`bash("rm")` 不行，复用 execpolicy 只读白名单 | `registry.py::ToolDef.is_concurrency_safe`、`builtin.py::_read_only_command_safe` | `test_cc_parity_batch2.py`（3） |
| #5 工具规模化 | `searchHint` 字段 + 检索加权，提高延迟工具召回 | `registry.py::search_deferred` | 同上（1） |
| #16 Hooks | 结构化 JSON 输出协议（decision/updatedInput/additionalContext/continue）+ **Stop hook**（可阻止模型停止并强制续跑，带死亡螺旋熔断）+ user_prompt_submit 事件 | `hooks/engine.py`、`agent.py::_run_stop_hook` | 同上（5） |
| #8/#1 溢出恢复 | 有序**有界**反应式恢复链（compact→retry，上限 2 次）+ **transition 观测枚举**（`_recovery_transitions`，可测试可观测） | `agent.py::_stream_turn/_record_transition` | 同上 |
| #23 判错结构化 | `ProviderError.is_overflow`/`status_code`（413 或错误体文本）——结构化优先、文本兜底 | `llm/providers.py::_is_overflow_error` | 同上 |
| #9 输出截断恢复 | `finish_reason=="length"` 时注入"从断点续写、不道歉不复述"并续跑，上限 3 次 | `agent.py` run_turn + `events.py::ProviderTurn.finish_reason` | 同上（2） |
| #10 配对不变量 | **本就满足**：`registry.execute` 把任何异常收敛成 `ToolResult`，未执行的 tool_calls 不写入 assistant 消息，故无孤儿 tool_result | `tools/registry.py::execute` | `test_agent_loop.py` |
| #4 工具契约 | `validateInput`（执行前校验、给模型可读原因、不跑 handler 不问用户）+ `contextModifier`（非并发安全工具执行后函数式改上下文）+ `isDestructive`（per-input）+ `maxResultSizeChars`（per-tool，-1 表 Infinity 不落盘） | `registry.py`、`builtin.py`（edit_file 校验、shell destructive） | `test_cc_parity_batch3.py`（6） |
| #17 缓存纪律 | 缓存命中率作为可运营指标 + **缓存断裂计数**（大 prompt 零 cache-read 即计一次断裂）；消息对象不变异本就基本满足（压缩/预算均返回新 dict） | `agent.py::_record_cache_usage/cache_stats` | 同上（1） |
| #15 权限体系 | 规则按来源分层（policy/project/user）+ `Tool(pattern)` 语法 + deny>ask>allow 优先级 + **被遮蔽规则检测** + 灾难命令永远先 deny；空规则集零行为改变 | `sandbox/permission_rules.py`、`execpolicy.classify(rules=)` | `test_permission_rules.py`（7） |
| #18 可观测性 | 命名空间化结构化事件日志（`.lilbot/events.jsonl`：turn/tool/compaction/recovery，带 ok/耗时/命中率/断裂/恢复次数）；**只序列化标量**防内容泄漏 | `core/eventlog.py` | `test_cc_parity_batch3.py`（4） |

### 🔜 待复刻（按价值顺序，portable）

1. **#12 会话级记忆**：让子代理用 Edit 工具增量维护一份 10 段"活文档"替代每次全文重写。
2. **#20 Skills 预取并行** / **#14 Task 工具族粒度** / **#19·#24 VCR 录放测试基建**。
3. **#2 流式与工具执行重叠**（StreamingToolExecutor，工程量最大，独立里程碑）。
4. **#15 增强**：（可选）自然语言权限规则 + 小模型分类器（任意模型，非 Claude 专属）。

### ⛔ 不复刻（依赖 Claude 模型 / Anthropic 私有 API，已剔除）

- #6 的 cache_edits 服务端原位删除、API 原生 context_management（Anthropic 请求协议）
- #10 的跨模型 fallback + 剥离 thinking 签名（LilBot 单 Provider，且 thinking 签名是 Anthropic 专属）
- #22 feature() 编译期门控、#23 UI 自渲染（非架构项，价值低）

详见 `docs/CC_COMPACTION_REPLICATION_STATUS.md`（压缩专项 34 项逐条）。
