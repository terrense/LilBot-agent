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

### 🔄 修复 2：MiniMax `<think>` 泄漏（进行中）
- 已实测：MiniMax 默认把推理内联为 `<think>…</think>`；请求加 `reasoning_split: true` 后推理进入 `reasoning_content` 字段（流式与非流式均如此）
- 计划：
  1. `llm/providers.py`：MiniMax 端点自动加 `reasoning_split: true`；`KNOWN_PROVIDERS` 增加 `minimax`（中国区）与 `minimax-intl`
  2. 通用兜底：开头的内联 `<think>` 块剥离到 `reasoning_content`（流式需处理标签被切分）
  3. 测试 + 真实多轮工具调用验证

### ⏳ 待办
- [ ] 推送 `dev/nayang`：需先接受 terrense 的协作邀请并在本机登录 GitHub
- [ ] Week 2：Durable Runtime MVP
