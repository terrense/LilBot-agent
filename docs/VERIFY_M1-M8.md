# 怎么感知和验证这批改进（动手版）

每一项都给：**改了什么**、**日常怎么感觉到**、**一条命令自己验证**（不用真连大模型也能跑）。
所有命令在仓库根目录执行。

> 提示：本机会把 `.md` 文件加密存盘，双击本文是乱码。请在 GitHub 上看，或用
> `git show HEAD:docs/VERIFY_M1-M8.md`。下面每条 `python -c` 都实跑核对过输出。

一键全量自检：
```bash
python -m pytest tests/ -q          # 期望：257 passed
```

---

## 1. 密钥脱敏

**改了什么。** API key / token / 私钥再也不会出现在界面、trace、日志里（展示时打码；模型内部仍用真值，功能不受影响）。

**怎么感觉到。** 让它读 / 写 `.env`：以前会打印 `sk-…一长串`，现在显示 `DEEPSEEK_API_KEY=[REDACTED]…末4位`。

**自己验证。**
```bash
python -c "from lilbot.security import redact_secrets as r; \
k='sk-'+'0123456789abcdef'*2; \
print(r('DEEPSEEK_API_KEY='+k)); \
print(r('LILBOT_BASE_URL=https://api.deepseek.com')); \
print(r('MAX_TOKENS=128000'))"
# -> DEEPSEEK_API_KEY=[REDACTED]…cdef
# -> LILBOT_BASE_URL=https://api.deepseek.com   （正常 URL 不动）
# -> MAX_TOKENS=128000                          （纯数字配置不动）
```

---

## 2. 编辑后自动诊断

**改了什么。** 改完代码文件后自动跑诊断，把错误喂回给自己，下一步就改掉——不用你指出来。

**怎么感觉到。** 让它写个 Python 文件，如果写出语法错，会收到"修好这些再继续"的提示并自我修正。

**自己验证。**
```bash
python -m pytest tests/test_auto_diagnostics.py -q   # 6 passed
```
关掉：`.lilbot/config.json` 里设 `auto_diagnostics: false`。

---

## 3. 命令安全引擎

**改了什么。** 灾难性命令直接拦；安全只读命令直接放行，不再为 `git status` 这种事弹审批。

**怎么感觉到。** 让它 `git status` / `ls -la`——立刻执行无需确认；让它干危险的事——被拦并给出原因。

**自己验证。**
```bash
python -c "from lilbot.sandbox.execpolicy import classify as c; \
print(c('rm -rf /')); print(c('rm -rf build/')); \
print(c('git status -s')); print(c('git push'))"
# -> ('deny', 'delete of a root/home/cwd/glob-everything path')
# -> ('ask', '')          删子目录只是照常弹审批, 不拦
# -> ('allow', 'known safe read-only command')
# -> ('ask', '')
```
关掉自动放行：`auto_allow_safe_commands: false`。

---

## 4. 周期记忆 + recall_archive

**改了什么。** 长会话压缩时，摘要被归档到 `.lilbot/archives/`，之后能用 `recall_archive` 按关键词翻回去。

**怎么感觉到。** 长会话里 `/compact` 之后，问"我们前面关于 X 是怎么定的"——它能从归档里召回。

**自己验证。**
```bash
python -m pytest tests/test_cycles.py -q     # 5 passed
# 真正压缩过之后会看到归档文件:
ls .lilbot/archives/    # cycle-年月日-时分秒-xxxx.md
```

---

## 5. 工具目录缓存（省钱）

**改了什么。** 发给模型的工具列表字节稳定，让 DeepSeek 前缀缓存持续命中（省 token）。`/tokens` 里多了 `tool_catalog_fp`（指纹）和 `tools_visible`（可见工具数）。

**怎么感觉到。** 会话里跑 `/tokens`——干活时 `cache_read_tokens` 在涨，`tool_catalog_fp` 保持不变。

**自己验证。**
```bash
python -c "from lilbot.tools import ToolRegistry, register_builtins; \
r=ToolRegistry(); register_builtins(r); \
print('指纹稳定:', r.catalog_fingerprint()==r.catalog_fingerprint()); \
print('目录同一对象:', r.schemas() is r.schemas())"
# -> 指纹稳定: True
# -> 目录同一对象: True
```

---

## 6. MCP 客户端（用任意外部工具 server）

**改了什么。** 启动时自动连接 MCP server、**发现**它们的工具、注册成一等工具 `mcp__<server>__<tool>`（延迟加载，不撑爆每轮）。不用再手动 `mcp_call`。

**怎么感觉到。** 在 `.lilbot/mcp.json` 配一个 server：
```json
{ "servers": {
    "fs": { "command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "."] }
}}
```
重启 LilBot，跑 `/tools`——会看到 `mcp__fs__*`。模型能 ToolSearch 后像内置工具一样调用。

**自己验证（不用外部 server，测试里自带一个假 MCP server）。**
```bash
python -m pytest tests/test_mcp_client.py -q   # 4 passed（握手 / 发现 / 调用 / 注册）
```

---

## 7. MCP 服务端（让别人用 LilBot）

**改了什么。** LilBot 能把自己的工具暴露给别的 MCP 客户端（编辑器、其它 agent）。默认只暴露只读工具。

**怎么感觉到。** 让任意 MCP 客户端这样连：
```
command: python   args: ["-m", "lilbot", "--mcp-server"]
```
它会看到 LilBot 的只读工具（`read_file`、`git_status`、`grep`…）。增减用 `.lilbot/mcp_server.json`：
```json
{ "expose_tools": ["read_file", "grep", "git_status", "project_map"] }
```

**自己验证（LilBot 自己的客户端驱动自己的服务端，端到端）。**
```bash
python -m pytest tests/test_mcp_server.py -q   # 8 passed（含客户端↔服务端往返）
```

---

## 一次性在真实会话里多看几个

```bash
python -m lilbot          # 启动界面（用 .env 里的 DeepSeek）
# 然后:
/tools                    # 看 tools_visible; 配了 MCP 会看到 mcp__*
/tokens                   # 看 tool_catalog_fp、cache_read_tokens
往 .env 加一行 FOO=bar     # 密钥显示被打码; 命令安全可能拦写
写一个有语法错误的 python 文件然后修好它   # 它从诊断里自我纠错
/sessions  /history  /rewind             # 续会话 / 撤销编辑
```

## 凭据在哪
- 每一步的改动：GitHub 提交历史（`commits/main`），一节点一提交。
- 人话总结：`CHANGELOG.md`。
- 带伪代码的细节：`docs/TECH_REPORT_M1-M8.md`。
- 测试：257 个，按功能分文件 `tests/test_{secrets,auto_diagnostics,execpolicy,cycles,catalog_cache,mcp_client,mcp_server}.py`。
