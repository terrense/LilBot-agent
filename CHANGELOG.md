# 更新日志（CHANGELOG）

本文件记录 LilBot 的重要改动，按批次 + 日期归档，方便回溯。

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
