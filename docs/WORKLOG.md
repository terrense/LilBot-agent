# LilBot 开发工作日志（断点续做用）

> 目的：电脑死机、终端意外退出后，任何人（或新的 Claude Code 会话）读这一页就能接着干。
> 每完成一个小步骤就更新"进度"一节。

## 环境

- 仓库：`A:\LilBot-agent`，分支 `dev/nayang`
- 虚拟环境：`A:\LilBot-agent\.venv`
- 模型：MiniMax 中国区 `https://api.minimaxi.com/v1`，`MiniMax-M3`，配置在 `.env`（已 gitignore，切勿提交）
- ⚠️ C 盘空间不足时，pip 等工具需要把临时目录指到 A 盘：
  ```powershell
  $env:TMP='A:\tmp'; $env:TEMP='A:\tmp'; $env:PIP_CACHE_DIR='A:\tmp\pip-cache'
  ```

## 恢复工作

```powershell
cd A:\LilBot-agent
git status --short --branch                 # 看未提交的改动
git log --oneline -5                        # 看最近提交
.\.venv\Scripts\python -m pytest tests -q   # 跑全量测试
```

然后在 `A:\LilBot-agent` 里启动 `claude`，告诉它"读 docs/WORKLOG.md 继续"。

## 进度

### ✅ Week 1 基线（2026-10-15）
- 4 份文档：`docs/architecture-overview.md`、`module-inventory.md`、`runtime-call-chain.md`、`baseline-test-report.md`
- 基线：353 passed / 6 skipped
- `.gitignore` 增加 `*.egg-info/`

### ✅ 修复 1：凭据隔离（2026-10-15）
- 新增 `lilbot/security/env.py`：`is_secret_env_name` / `scrubbed_env` / `redact_known_secrets`
- 子进程不再继承密钥环境变量：`sandbox/workspace.py`、`tools/builtin.py`（后台进程、code_execution、js_execution）
- `sandbox/execpolicy.py`：读密钥变量 / 列环境变量 / 读 `.env` 与凭据文件的命令永不自动放行（deny 规则仍生效）
- `tools/registry.py`：所有工具输出在回传模型前替换掉 LilBot 持有的密钥原值
- 新增 `tests/test_credential_isolation.py`（51 个用例；回退源码后 19 个失败，证明有效）
- 全量：404 passed / 6 skipped

### ✅ 修复 2：MiniMax `<think>` 泄漏（2026-10-15）
- `llm/providers.py`：MiniMax 端点自动加 `reasoning_split: true`；`KNOWN_PROVIDERS` 增加 `minimax`（中国区）与 `minimax-intl`
- 通用兜底 `_InlineThinkSplitter` / `split_inline_think`：开头的内联 `<think>` 块剥离到 `reasoning_content`，流式下也能处理
- 新增 `tests/test_inline_think.py`（13 个用例）；全量：417 passed / 6 skipped
- 真实验证：MiniMax-M3 两轮对话 + 工具调用 + `--resume`，无 `<think>`，session 中无密钥

### ✅ 提交与推送
- 提交身份（仓库级）：NaYanyeee <948513837@qq.com>
- `dev/nayang` 已推送到 GitHub

### ▶ 2026-10-23 起：对标 Hermes 的七天优化（当前主线）
- 仓库已搬到 `B:\娜样的武装部\LilBot-agent`
- 恢复入口：`docs/hermes-gap/SESSION_LOG.md`（进度、断点、基准数据都在这里）
  - ⚠️ 这个文件可能被本机加密软件加密（`cat` / 编辑器看到的是乱码）。用 Python 读明文：
    `$env:PYTHONIOENCODING='utf-8'; .\.venv\Scripts\python -c "import sys;sys.stdout.write(open('docs/hermes-gap/SESSION_LOG.md',encoding='utf-8').read())"`
- 2026-10-23 Day 1（HTTP 连接复用）已完成，下一步 Day 2（错误分类 + 重试 + 备用模型）；测试 425 passed / 6 skipped
- 报告：`docs/hermes-gap/01-LilBot与Hermes差距分析报告.md`；计划：`docs/hermes-gap/02-七天优化计划.md`
- 下面的 Week 2 Durable Runtime MVP 已并入七天计划的 Day 5

### ⏳ 下一步：Week 2 Durable Runtime MVP
- [ ] 设计 `lilbot/runtime/`（models / state_machine / store(SQLite) / checkpoint / recovery / idempotency）
- [ ] 第一个验收点：回合内增量持久化（每个工具结果之后写 checkpoint），崩溃后不丢失本回合已完成的步骤
- [ ] 故障注入测试：步骤之间崩溃、执行中崩溃、工具超时、结果未知时不盲目重试

### 📝 记下的小问题（未排期）
- `--resume` 参数可选，会吞掉后面的 prompt（需写成 `lilbot --print "问题" --resume`）
- Provider 没有请求级重试（S5）
- 根目录有残留文件 `Untitled`、`hello.txt`、`hello002.txt`（清理前需和 terrense 确认）
