# LilBot × Hermes 优化 — 会话日志（断点恢复入口）

> **终端意外关闭后怎么恢复**：在 `B:\娜样的武装部\LilBot-agent` 启动 `claude`，然后说：
> “读 `docs/hermes-gap/SESSION_LOG.md` 继续 LilBot 优化”。
> Claude 会先读这一页，再读 `02-七天优化计划.md` 里对应的那一天，然后从下面的“当前断点”接着做。
>
> 规则：每完成一个小步骤就更新“当前断点”；每次对话结束前追加一条“会话记录”。

## 环境速查

- 仓库：`B:\娜样的武装部\LilBot-agent`，分支 `dev/nayang`（旧 WORKLOG 里写的是 `A:\`，现在已经搬到 B 盘）
- Hermes 参考代码：`B:\娜样的武装部\hermes-agent-main`（只读，不要修改）
- Python：`.\.venv\Scripts\python`（3.11）
- 模型：MiniMax 中国区，配置在 `.env`（不要提交，不要打印出来）
- 测试：`.\.venv\Scripts\python -m pytest tests -q -p no:cacheprovider`
- 基准：`.\.venv\Scripts\python benchmarks\bench_runtime.py`（离线，不花钱；加 `--json` 输出 JSON）
- ⚠️ 本机装有透明加密软件：
  - 部分 `.md` 文件（README、CHANGELOG、docs 下的一些文件）用 `cat` 读出来是以 TSD 文件头开头的密文。LilBot 里原有 43 个被加密的文件，全是 `.md`；`.py`、`.toml` 都是明文；Hermes 里没有被加密的文件。
  - **文档正文里不要写加密软件的文件头标记原文**（百分号 + TSD-Header…）。实测：哪个文件里出现这个字符串，哪个文件就会被加密（Day 1 时 SESSION_LOG 和 WORKLOG 都因此中招，删掉后恢复正常）。
  - python.exe 能读到明文。万一某个 `.md` 变成了密文：`$env:PYTHONIOENCODING='utf-8'; .\.venv\Scripts\python -c "import sys;sys.stdout.write(open('路径',encoding='utf-8').read())"`；已提交的文件也可以用 `git show HEAD:<路径>`。
  - 改 `.md` 文档优先用 Claude 的 Edit / Write 工具。

## 当前断点

- **阶段**：Day 3、Day 4 已分两次提交；**下一步是 Day 5：回合内增量持久化**
- 提交记录（`dev/nayang`）：
  - Day 4：`perf(search): one pruning walker for every search tool, parallel reads`（含 Day 3/4 的文档更新）
  - Day 3：`058ea96 feat(context): CJK-aware token estimate, live calibration, aux model for side queries`（单独检出跑过全量：481 passed）
  - Day 2：`83b4a20`；Day 0 + Day 1：`3fd83dc`
- 提交约定：提交信息**不要**加 `Co-Authored-By` 行；提交身份用仓库级配置 NaYanyeee
- 测试：**509 passed / 7 skipped / 0 failed**（Day 3 是 481，Day 4 新增 28 个；多出的 1 个 skipped 是 Windows 上无法建符号链接的用例）
- 用户偏好（2026-10-29）：每天的优化除了改代码，还要**单独生成一份优化报告**（策略、代码走读、数据流、坑、知识点），放在 `docs/hermes-gap/0N-DayN-*.md`

## 基准数据（每天更新）

| 指标 | Day 0 基线 | Day 1 | Day 2 | Day 3 | Day 4 | Day 5 | Day 6 | Day 7 |
|---|---|---|---|---|---|---|---|---|
| httpx.Client 创建开销 / 次 | 813 ms | 0.2 ms | 0.2 ms | 0.2 ms | | | | |
| 10 次连续流式请求（本地假服务器） | 7,830 ms / 10 条 TCP | 7.5 ms / 1 条 TCP | 同左 | 同左 | | | | |
| 中文文档 6,000 字符的 token 估算（MiniMax-M3 实测 2,610） | 1,500（-43%） | 同左 | 同左 | 2,965（+14%） | | | | |
| 整个真实请求的估算误差（系统提示 + 工具定义 + 对话，实测 11,408） | 偏低（中文系统提示按 len/4 算） | | | 11,624（+2%），校准后系数 0.98 | | | | |
| 仓库根目录 grep（不存在的字符串） | 1,739 ms | ~1,070 ms（未改，波动） | 未改 | 未改（这次测到 3,179 ms，波动很大） | **54 ms**（3 次中位数） | | | |
| `glob **/*.py`（耗时 / 返回行数） | 1,084 ms / 1,793 行（1,682 行是 .venv） | | | | **9 ms / 113 行** | | | |
| 真实任务“找函数 + 数 .py 文件”（MiniMax-M3） | | | | 19 步 / 26.5 s | **2 步 / 5.8 s** | | | |
| 回合中途崩溃后保留的消息 | 0 | 0 | 回合失败时全部保留（回合内增量写仍待 Day 5） | 同左 | | | | |
| 429 / 5xx 时回合是否存活 | ❌ | ❌ | ✅ 退避重试 3 次 → 备用模型 | ✅ | | | | |
| 全量测试 passed | 417 | 425 | 466 | 481 | 509 | | | |

> 注：Day 0 时“中文 1,450 字”那一行用的是一句话重复 50 次的人造文本，Day 3 实测发现它会被分词器异常压缩（实际只有 701 token），不具代表性，所以换成了 `benchmarks/samples/` 下的真实文本。

## 会话记录

### 2026-10-23 · Day 0：差距分析
- 读完 LilBot 核心模块（agent / providers / registry / compaction / session / sandbox / mcp / memory / config），以及 Hermes 的 AGENTS.md 和 `agent/` 下的关键模块
- 实测了 4 项指标（见上表）。根因：`providers.py` 每次请求都新建 `httpx.Client`，certifi 证书单次加载约 273 ms，在这台机器上建一个 Client 共约 813 ms
- 产出：`01-LilBot与Hermes差距分析报告.md`、`02-七天优化计划.md`、本文件
- 关键结论：架构不需要大改；先补运行时的工程化（连接复用 → 重试 → token 估算 → 搜索 → 增量持久化 → 护栏 → 权限）
- 遗留：无

### 2026-10-23 · Day 1：HTTP 连接复用
- `providers.py`：模块级缓存 `SSLContext`（`_ssl_context()`，certifi 只加载一次，约 275 ms）；`_new_http_client()` 带 keep-alive 连接池（8 条，60 s）
- `OpenAICompatibleProvider` 持有一个懒加载、加锁的 Client；`base_url` 变了就重建并关闭旧的；新增 `close()`（`BaseProvider.close()` 是空实现）
- **额外发现的 bug**：SSE 解析读到 `[DONE]` 就停，响应体没读完，httpx 只能丢弃连接 → 即使复用 Client 也是每次新建 TCP。修复：`complete_stream` 在事件结束后把剩余行读完。修复前 10 请求 10 条连接，修复后 1 条
- `cli.py`：`/model` 切换时关闭旧 provider（`_close_provider`，兼容没有 `close` 的假 provider）；`main()` 用 `try/finally` 在退出时关闭
- 新增 `tests/test_http_client_reuse.py`（8 个用例：复用、base_url 重建、close、8 线程并发只建一个 Client、读完流、/model 关闭旧的、SSL 只建一次、真实 Client 的连接池参数）。回退源码后全部失败
- 新增 `benchmarks/bench_runtime.py`
- 真实验证：MiniMax-M3 `--print` 一次，正常回复
- 踩坑：文档里写了加密软件的文件头标记原文 → 文件被加密。已改写为明文并在“环境速查”里记下规则
- 遗留：`complete()`（非流式）遇到连接错误抛的是 `httpx.HTTPError` 而不是 `ProviderError` → Day 2 一起处理（✅ 已在 Day 2 修复）

### 2026-10-27 · Day 2：错误分类 + 重试 + 备用模型
- 新增 `lilbot/llm/errors.py`：`FailoverReason`（auth / billing / rate_limit / overloaded / server_error / timeout / network / context_overflow / model_not_found / format_error / unknown）+ `classify(exc) -> ClassifiedError(retryable, should_compress, should_fallback, retry_after)`。先看结构化信号（`is_overflow`、`reason`、`status_code`、httpx 异常类型），文字只做兜底（如 429 + `insufficient_quota` 判为 billing）。我们自己代码的 bug（如 `KeyError`）判为 unknown，**不重试**
- 新增 `lilbot/llm/retry.py`：`jittered_backoff`（2s、4s、8s… 加最多 50% 抖动，上限 60s）、`parse_retry_after`（秒数或 HTTP 日期）、`retry_delay`（有 Retry-After 就听服务器的，上限 60s）
- `providers.py`：`ProviderError` 增加 `reason`、`retry_after`；两条请求路径都把 httpx 传输错误包装成 `reason="timeout"/"network"`；解析 `Retry-After` 头；缺 key 标为 `auth`
- `agent.py:_stream_turn` 恢复链：①已经流式输出过文字 → 直接抛出（重放会重复输出）②上下文溢出 → 压缩重试（原逻辑）③临时错误 → 退避重试，最多 `max_retries` 次 ④仍失败或模型不存在/欠费 → 本回合切到备用模型（只切一次，下一回合回到主模型）⑤其他 → 抛出。每次恢复都 yield `ProviderRetry` 事件并记录 transition
- `run_turn` 包了一层：失败（含 Ctrl+C）时先 `_persist_session()` 再抛出；新增 `Agent.close()`（关闭主模型和备用模型的连接）
- 配置：`LILBOT_MAX_RETRIES`（默认 3）、`LILBOT_FALLBACK_MODEL`、`LILBOT_FALLBACK_BASE_URL`（备用模型复用同一个 api_key，没有新增密钥字段）
- UI：两个界面都显示一行黄色提示，如 `↻ rate_limit: retry 1 in 2.4s — …`；`--print` 模式最终失败时只打一行错误、退出码 1（原来是整段 traceback）
- 新增 `tests/test_retry.py`（41 个用例）；全量 466 passed
- 真实验证：①MiniMax-M3 正常回复 ②`LILBOT_BASE_URL=http://127.0.0.1:9/v1` → 两次重试（2.8s、5.5s）后报错，会话已保存 ③`LILBOT_MODEL=MiniMax-NoSuchModel` + `LILBOT_FALLBACK_MODEL=MiniMax-M3` → 自动切到备用模型并正常回复
- 遗留：子代理（`ctx.subagents.provider` 走非流式 `complete`）和侧查询（召回、摘要）还没有重试；退避等待用的是 `time.sleep`，Day 6 改成可中断的 `Event.wait`

### 2026-10-28 · Day 3：中文 token 估算 + 校准 + 辅助模型
- **先实测再定系数**：用 MiniMax-M3 真实 `prompt_tokens` 测了 3 份真实文本（存到 `benchmarks/samples/`）。中文文档：实测 2,610，旧 `len/4` 1,500（-43%）。原计划里的“1,450 字重复句”是人造文本，实测只有 701 token，已弃用
- `compaction.py:estimate_tokens_text`：按字符类别估算，ASCII /4、CJK（含全角标点、假名、韩文）×1、其他非 ASCII /2；纯 ASCII 结果和以前完全一样。中文文档 2,965（+14%），偏高是安全方向（早一点压缩，而不是溢出）。恢复附件的截断也改成按这段文本自己的“字符/token”比例
- **修了一个已有 bug**：`_add_usage` 被侧查询（召回、记忆抽取、摘要、委派规划）调用时也会覆盖 `_last_input_tokens`，召回在后台线程完成后会把压缩触发值从几千改成几十，导致该压缩时不压缩。现在只有主循环（`main_loop=True`）更新它。旧代码实测：5000 → 7
- 校准：每次主循环请求前记下估算值（消息 + 工具定义），拿到真实 `prompt_tokens` 后更新 EMA 系数（α=0.3，异常样本 <0.3 或 >3 丢弃）
- 新增 `Agent.context_tokens()`：上次真实值 + 之后新增消息的校准估算（工具结果最容易在这里被低估）。压缩触发、`/tokens`、`/status`、dashboard 顶栏都用它
- **又修了两个显示 bug**：dashboard 顶栏的上下文百分比用的是 `usage["prompt_tokens"]`（全部请求累计值，几轮后就卡在 99%）；不在列表里的模型（如 MiniMax）上下文窗口按 32K 算，和压缩用的 `context_window`（128K）对不上
- 辅助模型：`LILBOT_AUX_MODEL`、`LILBOT_AUX_BASE_URL`（复用 api_key）。`_meta_query`（召回 / 记忆抽取 / 会话笔记）和委派规划走辅助模型；压缩摘要仍用主模型（复用前缀缓存）。`Agent.close()` 一并关闭
- `/tokens` 新增 `estimate_calibration` 行；`/status` 新增 aux_model、fallback_model、context_tokens
- 新增 `tests/test_token_estimate.py`（15 个用例）；全量 481 passed
- 真实验证（MiniMax-M3 两轮 + `LILBOT_AUX_MODEL=MiniMax-M3.1-Flash`）：整个请求估算 11,624 vs 实测 11,408（+2%），校准系数 0.981；侧查询确实走了 M3.1-Flash，主循环仍是 M3
- 可用模型（2026-10-28 查询 `/models`）：MiniMax-M3.1-Flash、MiniMax-M3、MiniMax-M2.7(-highspeed)、M2.5(-highspeed)、M2.1(-highspeed)、M2
- 遗留：校准系数没有持久化（每次启动从 1.0 开始，第一次请求后就校准好了，影响很小）；压缩摘要是否改用辅助模型，等有了缓存命中数据再决定

### 2026-10-29 · Day 4：搜索工具提速
- **详细报告**：[04-Day4-搜索优化报告.md](04-Day4-搜索优化报告.md)（排查过程、策略、代码走读、数据流、14 条坑、知识点）
- 根因（实测拆解）：不是“文件多读得慢”，而是 `rglob` 之后对 4,557 项逐个 `is_file()`，每次 `stat` 约 0.18 ms（加密 + 杀毒过滤驱动），共 831 ms；`os.scandir` 剪枝遍历同样的 183 个文件只要 7 ms
- 新增 `lilbot/sandbox/walk.py`：`walk` / `iter_files`（scandir、进入前剪枝、顺序 == `sorted(rglob)`）、`IgnoreRules`（`.gitignore` 安全子集，否定规则跳过）、`read_text_for_search`（二进制后缀 / NUL 嗅探 / >2MB 跳过）、`read_texts`（8 线程、每批 32、保序）、`glob_to_regex`（兼容 3.11 `rglob` 语义）
- 8 处 `rglob("*")` 全部换掉：`_grep`、`_glob`（经 `Sandbox.glob`）、`_list_dir`、`_file_search`、`_project_map`、`_iter_source_files`、`Sandbox.list_dir/grep`
- 修了 4 个正确性 bug：glob 返回 `.venv`（95K 字符）；grep 单文件路径永远无结果；grep 在 PNG 里假匹配；不尊重 `.gitignore`
- 瓶颈转移后发现：打开文件 0.72 ms/个 → 并行读 120 ms → 30 ms
- 真实任务发现 glob 不给数量导致模型绕路 19 步 → 输出末尾加 `(N matches)` 后 2 步
- **没做 ripgrep**：本机没装、无法验证，纯 Python 已 54 ms；理由写在报告第 5 节
- 新旧输出逐行对比（`git archive` 导出旧版本）：只去掉了 `.venv`、`*.egg-info`、`.env`，顺序完全一致
- 新增 `tests/test_search_walk.py`（28 个用例）；全量 509 passed / 7 skipped
- 遗留：`skills/registry.py:170` 还有一处 `rglob`（技能目录很小，暂不改）；嵌套 `.gitignore` 不支持
