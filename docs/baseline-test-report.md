# 基线测试报告（Week 1）

- 日期：2026-10-15
- 分支：`dev/nayang`（基于 `main` @ `32be16c`，2026-07-08）
- 环境：Windows 10 Pro 19045 / Python 3.11.9 / 独立 `.venv`，`pip install -e . pytest`
- 执行人：娜样（Claude Code 协助）

## 1. 单元测试

```powershell
.\.venv\Scripts\python -m pytest tests -q -rs
```

| 项 | 结果 |
|---|---|
| 测试文件 | 37 个 |
| 通过 | **353** |
| 跳过 | 6 |
| 失败 | 0 |
| 耗时 | ~19 s |

6 个跳过全部位于 `tests/test_agent_loop.py`（第 269/310/349/386/423/480 行），原因一致：

> `SPEC_DYNAMIC_AGENT_TOOL_PROMPT_PARITY: auto-delegation replaced by dynamic tool descriptions`

即旧的关键词自动委派已被"动态 Agent 工具描述"取代（`Agent._auto_delegate()` 现在恒返回 0），属于**预期跳过**，不是缺陷。后续可考虑删除这些测试或改写为针对新机制的测试。

> 测试运行时未设置任何 API Key，`choose_provider()` 会回落到离线 `RuleBasedProvider`，因此单元测试不访问网络、不产生费用。

## 2. 真实模型端到端冒烟

模型：MiniMax（**中国区**）`https://api.minimaxi.com/v1`，模型 `MiniMax-M3`，通过 OpenAI 兼容接口接入，无需改代码。

配置放在仓库根目录 `.env`（已被 `.gitignore` 忽略，**不要提交**）：

```ini
LILBOT_PROVIDER=minimax
LILBOT_BASE_URL=https://api.minimaxi.com/v1
LILBOT_API_KEY=<你的 key>
LILBOT_MODEL=MiniMax-M3
```

命令：

```powershell
.\.venv\Scripts\python -m lilbot --print --no-rich --permission-mode deny-all "读取 README.md，用三句中文总结这个项目是什么。"
```

结果：模型调用 `read_file` 工具 → 读取 README → 给出三句正确的中文总结，`completed 1 steps`。**链路打通。**

该账号可用模型（`GET /v1/models`）：MiniMax-M3.1-Flash-Preview、MiniMax-M3、MiniMax-M2.7(-highspeed)、MiniMax-M2.5(-highspeed)、MiniMax-M2.1(-highspeed)、MiniMax-M2。

## 3. 冒烟中发现的问题

| # | 问题 | 位置 | 影响 |
|---|---|---|---|
| B1 | MiniMax 把推理过程以 `<think>…</think>` 内联在 `content` 里返回，LilBot 原样显示给用户 | `llm/providers.py` `_iter_stream_events` / `complete` 只识别 `reasoning_content` 字段 | 输出噪声大；同时整段思考会写入 `messages` 并持久化到 session |

B1 **已修复**（2026-10-15，`llm/providers.py`，测试 `tests/test_inline_think.py`）：
1. MiniMax 端点（base_url 含 `minimax`）自动在请求体中加 `reasoning_split: true`，实测推理进入 `reasoning_content` 字段（流式与非流式均如此）；
2. 通用兜底：其他模型若在 `content` 开头内联 `<think>…</think>`，provider 层把它剥离到 `reasoning_content`，流式下标签被切到多个 chunk 也能处理；
3. 新增 provider 预设 `minimax`（中国区 `api.minimaxi.com`）与 `minimax-intl`。

实测（MiniMax-M3，两轮对话，含 2 次工具调用，并用 `--resume` 续接）：输出中没有 `<think>`；session 里 4 条 assistant 消息的推理都单独存在 `reasoning_content` 中；MiniMax 接受在历史中回传 `reasoning_content`，第二轮能正确利用上下文；session 文件中没有出现 API Key。

附带发现：`--resume` 的参数是可选的，因此 `lilbot --print --resume "问题"` 会把问题当成 session id。需要写成 `lilbot --print "问题" --resume`。可以以后改进 CLI。

其余在代码审计中发现的问题见 `docs/architecture-overview.md` 第 4 节。

## 4. 复现基线的方法

以后每次改动前后都跑同一套命令，对比通过/跳过/失败数量：

```powershell
.\.venv\Scripts\python -m pytest tests -q -rs -p no:cacheprovider
```

基线数字：**353 passed / 6 skipped / 0 failed**。任何改动后通过数下降或出现失败，都需要解释。
