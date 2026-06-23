# LilBot 技术报告：安全 / 纠错 / 协议增强（M1–M8）

> 本报告讲清楚这批改进**做了什么、放在哪、怎么实现的**，每节附简化伪代码。
> 测试数 184 → 257，无回归。

---

## 0. 这些改进在主流程的什么位置

一次 `Agent.run_turn`（一轮用户对话）大致是这样跑的：

```
用户输入
  └─ 召回相关记忆 ─ 触发 turn_start 钩子
     └─ while 没到步数上限:
          provider.complete( 发给模型的消息, registry.schemas() )   ← M5 工具目录缓存
            ├─ 模型回文本 → 显示（显示前过一遍 M1 密钥脱敏）
            └─ 模型回工具调用:
                 按"只读可并行"分组 → 只读组用线程池并发
                 每个调用:
                   pre_tool 钩子（可拦截）
                   编辑前快照（给 rewind 用）
                   M3 命令安全：危险命令拦、安全只读命令放行
                   registry.execute → 大结果落盘
                   记录被编辑的文件
                 一组跑完 → M2 编辑后自动诊断，结果留给下一轮
       上下文太长 → 压缩，并把摘要 M4 归档成一个"周期"

MCP：
  启动时   build_runtime → 连接配置的 MCP server，自动发现并注册工具（M7 客户端）
  服务端   python -m lilbot --mcp-server → 把自己的工具暴露给别的客户端（M8）
```

---

## M1 —— 密钥脱敏

**要解决的问题。** 工具输出曾把真实 API key 打印进可见的 trace，`.env` 的 diff 又暴露了一次。之前没有任何遮挡密钥的机制。

**思路。** 在**展示层**做脱敏：模型自己的上下文保留原值（功能不受影响），只有送到屏幕 / trace / 日志的内容被打码。两个界面（`classic.py`、`dashboard.py`）都接。

**伪代码（`lilbot/security/secrets.py`）。**
```python
密钥形状 = [sk-…, ghp_…, AKIA…, AIza…, xox…, JWT, Bearer …]
赋值形状 = /(KEY|TOKEN|SECRET|PASSWORD|…)\s*[=:]\s*(值{6,})/

def 脱敏(text):
    先把私钥块整体换成 [REDACTED PRIVATE KEY]
    对赋值形状: 若值是纯数字则跳过(放过 MAX_TOKENS=128000), 否则给值打码
    对每个密钥形状: 命中就打码
    return text

def 打码(s):                      # 保留末 4 位, 让你能认出是哪把 key
    return "[REDACTED]" if len(s)<=8 else "[REDACTED]…" + s[-4:]
```
误报防护：纯数字配置、`AUTHOR=` 不会被打码（`AUTH` 只匹配 `AUTHORIZATION`/`AUTH_TOKEN`）。

---

## M2 —— 编辑后自动诊断注入

**要解决的问题。** 模型改完代码后，语法 / 类型错误只能靠它自己碰巧再读一遍才发现。

**思路。** 改完代码文件后，自动跑诊断工具（有 LSP 用 LSP，Python 走语法兜底），把错误作为**一次性提示**塞进**下一轮**模型调用，让它当场看到并修。

**伪代码（`lilbot/core/agent.py`）。**
```python
# 一组工具跑完后:
def 编辑后诊断():
    文件 = [本轮编辑过的代码文件][:5]
    问题 = []
    for 路径 in 文件:
        诊断 = registry.execute("lsp_diagnostics", {"path": 路径})
        for d in 诊断:
            if d.严重级 in ("error","warning"):
                问题.append(f"{路径}:L{d.行} [{d.严重级}] {d.信息}")
    if 问题:
        待注入诊断 = "修好这些再继续:\n" + "\n".join(问题)

# 下一轮拼消息时:
if 待注入诊断:
    extras.append({"role":"system","content": 待注入诊断})
    待注入诊断 = ""        # 只注入一次
```
按扩展名筛（只诊断 `.py/.js/.ts/.go/.rs/...`），每轮最多 5 个文件，`config.auto_diagnostics` 可关。

---

## M3 —— 命令安全引擎

**要解决的问题。** 每条 shell 命令都弹审批，既没有危险命令黑名单，也没有"安全只读命令直接放行"的智能。

**思路。**
- **拦**：灾难性命令直接拒。
- **放**：已知只读命令用"忽略 flag 的前缀匹配"自动放行（`git status` 能匹配 `git status -s`，但匹配不到 `git push`）。
- 其余照常弹审批。

**伪代码（`lilbot/sandbox/execpolicy.py`）。**
```python
def 匹配放行规则(cmd, 白名单):
    if 含shell运算符(cmd): return False         # ; && | > ` $() 一律不自动放行
    位置参数 = [非 flag 的词 in shlex.split(cmd)]
    return 任一规则: 位置参数[:len(规则)] == 规则

def 判定(cmd):
    if rm目标是根/家/当前目录/通配全部 or 命中危险模式: return "拒"
    if 匹配放行规则(cmd, 安全只读集): return "放"
    return "问"
```
`rm -rf /tmp/foo`（删子目录）**不拦**；`rm -rf /`、`~`、`*`、`.` 才拦。`config.auto_allow_safe_commands` 可关。

---

## M4 —— 周期记忆 + recall_archive

**要解决的问题。** 上下文压缩时，被摘要掉的早期内容直接丢了；`recall_archive` 工具存在但没人往里写。

**思路。** 每次压缩把摘要归档成一个带日期的"周期简报"，`recall_archive` 能按关键词翻回去。

**伪代码（`lilbot/core/cycles.py` + `agent.compact()`）。**
```python
# 压缩时:
结果 = 自动压缩(messages)
if 结果:
    简报 = 结果.messages[1]["content"]            # 模型生成的摘要
    周期归档.写入(简报)                            # .lilbot/archives/cycle-<时间>.md
messages = 结果.messages

# recall_archive(关键词): 读 archives/*.md, 按关键词过滤, 按时间倒序返回
```

---

## M5 —— 工具目录缓存（稳定前缀）

**要解决的问题。** 每轮都重建工具列表；而 DeepSeek 前缀缓存要求发出去的 `tools` 字节**稳定**才命中。

**思路。** 缓存"可见工具"的序列化目录，只有工具集变化时才重建；动态部分（子 agent 描述）改的是一份拷贝，不动缓存。再给一个指纹 `catalog_fingerprint()`（`/tokens` 里能看）。

**伪代码（`lilbot/tools/registry.py`）。**
```python
def 基础目录():
    签名 = frozenset(可见工具名)
    if 缓存 is None or 缓存签名 != 签名:           # 只在变化时重建
        缓存 = [工具schema for 可见工具]; 缓存签名 = 签名
    return 缓存                                     # 同一个对象 → 同样的字节

def schemas(动态上下文):
    base = 基础目录()
    if 动态上下文 is None: return base             # 缓存命中, 字节稳定
    s = [dict(x) for x in base]                    # 改之前先拷贝
    渲染子agent描述(s, 动态上下文); return s
```

---

## M7 —— MCP 客户端（自动发现 + 一等工具）

**要解决的问题。** 之前 MCP 只是"一次性子进程 + 手动 `mcp_call`"，没有发现、没有持久连接。

**思路。** 一个同步的 JSON-RPC over stdio 客户端（持久子进程 + 读线程，**不引入异步依赖**）：`initialize` 握手 → `tools/list` 自动发现 → `tools/call`。把发现的每个工具注册成一等**延迟**工具 `mcp__<server>__<tool>`，模型像用内置工具一样用。

**伪代码（`lilbot/mcp/client.py` + `manager.py`）。**
```python
class 客户端:
    def 启动():
        起子进程; 起读线程(按 id 路由响应)
        请求("initialize", {协议版本, 能力, 客户端信息})
        通知("notifications/initialized")
    def 请求(method, params):
        id=下一个id; 待回应[id]=队列
        写({jsonrpc,id,method,params}); msg=待回应[id].取(超时)
        return msg["result"] 或 抛出 msg["error"]
    def 列工具():  return 请求("tools/list")["tools"]
    def 调工具(名, 参):
        r=请求("tools/call",{name:名,arguments:参})
        return r["content"]里的文本, r.get("isError")

# 启动时: 连接每个 server → 列工具 → 逐个注册成 mcp__server__tool(延迟加载)
```

---

## M8 —— MCP 服务端

**要解决的问题。** 之前只能当客户端，不能把自己的工具暴露给别人。

**思路。** `python -m lilbot --mcp-server` 用 stdio JSON-RPC 把 LilBot 工具暴露出去。**默认只暴露只读工具**（安全），`.lilbot/mcp_server.json` 的 `expose_tools` 可调。客户端 + 服务端合起来就是**双向** MCP 节点。

**伪代码（`lilbot/mcp/server.py`）。**
```python
def 处理(msg):
    if msg.method=="initialize": return 成功({协议版本, 能力, 服务信息})
    if msg.method=="tools/list": return 成功({tools: 暴露的工具描述()})
    if msg.method=="tools/call":
        名,参 = msg.params.name, msg.params.arguments
        if 名 not in 暴露集: return 错误("tool not exposed")
        结果 = registry.execute(名, 参)
        return 成功({content:[{type:"text",text:结果.output}], isError: not 结果.ok})
def 暴露集(): return 配置的暴露列表 or 只读工具集     # 默认安全
```
已用 LilBot 自己的客户端驱动自己的服务端端到端跑通验证。

---

## 一览

| 节点 | 文件 | 测试数 |
|---|---|---|
| M1 密钥脱敏 | `security/secrets.py` | 10 |
| M2 编辑后诊断 | `core/agent.py` | 6 |
| M3 命令安全 | `sandbox/execpolicy.py` | 35 |
| M4 周期记忆 | `core/cycles.py` | 5 |
| M5 目录缓存 | `tools/registry.py` | 5 |
| M7 MCP 客户端 | `mcp/client.py` | 4 |
| M8 MCP 服务端 | `mcp/server.py` | 8 |

怎么动手验证见 `docs/VERIFY_M1-M8.md`。
