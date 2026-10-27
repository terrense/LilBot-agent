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

- **阶段**：Day 2 已完成（代码 + 测试 + 真实冒烟），**未提交**，等用户确认；**下一步是 Day 3：中文 token 估算 + 辅助模型路由**
- 最近提交：`3fd83dc perf(llm): reuse one keep-alive httpx client per provider`（Day 0 + Day 1，已推送到 GitHub `origin/dev/nayang`）
- Day 2 未提交的改动：`lilbot/llm/errors.py`、`lilbot/llm/retry.py`（新）、`tests/test_retry.py`（新）、`lilbot/llm/providers.py`、`lilbot/core/agent.py`、`lilbot/core/events.py`、`lilbot/config.py`、`lilbot/cli.py`、`lilbot/tui/classic.py`、`lilbot/tui/dashboard.py`、本文件、`02-七天优化计划.md`、`docs/WORKLOG.md`
- 提交约定：提交信息**不要**加 `Co-Authored-By` 行；提交身份用仓库级配置 NaYanyeee
- 测试：**466 passed / 6 skipped / 0 failed**（Day 1 是 425，Day 2 新增 41 个）

## 基准数据（每天更新）

| 指标 | Day 0 基线 | Day 1 | Day 2 | Day 3 | Day 4 | Day 5 | Day 6 | Day 7 |
|---|---|---|---|---|---|---|---|---|
| httpx.Client 创建开销 / 次 | 813 ms | 0.2 ms | 0.2 ms | | | | | |
| 10 次连续流式请求（本地假服务器） | 7,830 ms / 10 条 TCP | 7.5 ms / 1 条 TCP | 同左 | | | | | |
| 中文 1,450 字的 token 估算（实际约 1,000–1,450） | 362 | 362 | 362 | | | | | |
| 仓库根目录 grep（不存在的字符串） | 1,739 ms | ~1,070 ms（未改，波动） | 未改 | | | | | |
| 回合中途崩溃后保留的消息 | 0 | 0 | 回合失败时全部保留（回合内增量写仍待 Day 5） | | | | | |
| 429 / 5xx 时回合是否存活 | ❌ | ❌ | ✅ 退避重试 3 次 → 备用模型 | | | | | |
| 全量测试 passed | 417 | 425 | 466 | | | | | |

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
