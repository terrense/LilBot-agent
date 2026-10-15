# 运行时调用链（Week 1）

> 基于 `main` @ `32be16c` 的真实代码追踪，行号以该版本为准。
> 追踪任务：`lilbot --print "读取 README.md，用三句中文总结这个项目是什么。"`（MiniMax-M3 实测）。

## 1. 总览

```text
python -m lilbot
  └─ cli.main()                                  cli.py:760
       ├─ load_config()  ← .env / 环境变量 / .lilbot/config.json   config.py:79,136
       ├─ apply_args()   ← 命令行参数覆盖                          cli.py:270
       ├─ build_runtime()                                          cli.py:155
       │    ├─ Sandbox(workspace)          路径约束 + shell 执行
       │    ├─ PermissionManager           ask / accept-all / deny-all
       │    ├─ FileMemoryStore / SkillRegistry
       │    ├─ choose_provider(cfg)        providers.py:508
       │    ├─ SubAgentManager / MCPManager / TeamManager
       │    ├─ ToolRegistry + register_builtins()   146 个内置 ToolDef
       │    ├─ ToolContext(...)            工具共享的依赖包
       │    └─ Agent(cfg, provider, registry, ctx)
       ├─ maybe_resume()                   --resume 时加载 session JSON
       └─ 三种入口：
            --print / 位置参数 → run_prompt()              cli.py:296
            交互 + TTY        → DashboardUI.run()          tui/dashboard.py
            其他               → interactive_loop()        cli.py:739
                                     └─ handle_slash() 或 run_prompt()

run_prompt(agent, ui, prompt)
  └─ for event in agent.run_turn(prompt): ui.event(event)
```

`Agent.run_turn()` 是一个**生成器**：它不直接打印，而是 `yield` 事件（`TextDelta` / `ToolStarted` / `ToolFinished` / `TurnFinished`，定义在 `core/events.py`），由 UI 层消费渲染。这让 TUI、classic、`--print` 共用同一个循环。

## 2. `Agent.run_turn()` 内部（agent.py:230–358）

```text
run_turn(user_text)
 │
 ├─ 回合准备
 │   ├─ _reload_hooks_if_changed()           hooks.json 热加载
 │   ├─ messages.append(user)
 │   ├─ events.log("turn_start")             → .lilbot/events.jsonl
 │   ├─ hooks: user_prompt_submit
 │   ├─ _maybe_compact()                     L0 工具结果预算 → token 触发压缩 → 消息数兜底
 │   ├─ _start_recall_prefetch()             后台线程做记忆召回（不阻塞）
 │   ├─ hooks: turn_start
 │   └─ steps = _auto_delegate() = 0         旧关键词委派已停用
 │
 ├─ while steps < max_steps (默认 20):
 │   ├─ _drain_team_notifications()          把队友消息注入为 user 消息
 │   ├─ _stream_turn()                       agent.py:1057
 │   │    ├─ _provider_messages()            messages + 临时 system 附加（deferred 工具提示、召回结果、诊断、hook 输出）
 │   │    ├─ registry.schemas()              只发送"可见"工具的 schema（deferred 工具需 ToolSearch 发现）
 │   │    ├─ _drive_stream() → provider.complete_stream()  SSE 流式
 │   │    └─ 上下文溢出异常 → compact(manual=True) 后重试，最多 MAX_OVERFLOW_RECOVERIES 次
 │   │
 │   ├─ 无 tool_calls：
 │   │    ├─ finish_reason=="length" → 注入"继续"指令，continue（有上限）
 │   │    ├─ stop hook 要求继续 → continue（有上限）
 │   │    └─ turn_end hook → 后台记忆抽取 → 后台 session memory 更新
 │   │       → _persist_session() → yield TurnFinished → return
 │   │
 │   └─ 有 tool_calls：
 │        ├─ messages.append(assistant + tool_calls)
 │        ├─ _partition_calls()              相邻的并发安全（只读）调用合批
 │        ├─ 每批：yield ToolStarted ×N
 │        │    ├─ N>1 → ThreadPoolExecutor 并行 _run_one_call
 │        │    └─ N=1 → _run_one_call
 │        ├─ 每个结果：events.log("tool_call") → yield ToolFinished
 │        │           → messages.append(role=tool)
 │        └─ _run_post_edit_diagnostics()    本轮改过的文件跑 lsp_diagnostics，错误注入下一次调用
 │
 └─ 步数用尽：_synthesize_after_step_limit()（无工具再调一次模型）→ 持久化 → yield
```

## 3. 单个工具调用（`_run_one_call` → `ToolRegistry.execute`）

```text
_run_one_call(call)                       agent.py:775
 ├─ _pre_tool_hook()        hook 可阻断，或改写参数
 ├─ _snapshot_before_edit() 写类工具先存文件快照（FileHistory，支持 /rewind）
 ├─ registry.execute(name, args, ctx)     tools/registry.py:417
 │    ├─ resolve(name)                    别名解析
 │    ├─ deferred 工具被直接调用 → 标记为已发现
 │    ├─ tool.run_validation()            参数校验失败 → 返回错误，不执行
 │    ├─ _plan_approval_gate()            计划待审批时阻断写/执行类工具
 │    ├─ tool.handler(args, ctx)          ← 权限检查在这里面，由各 handler 自己调用
 │    │     例：exec_shell → _shell_permission()   builtin.py:199
 │    │          ├─ PowerShell 安全分析（Windows）→ 可直接 block
 │    │          ├─ execpolicy.classify()  deny → 拒绝；allow → 直接放行
 │    │          └─ 其他 → PermissionManager.check()（ask/accept-all/deny-all）
 │    │       → ctx.sandbox.run() → subprocess（Windows: powershell -Command）
 │    ├─ 异常统一包装为 ToolResult(ok=False)
 │    ├─ is_destructive → metadata
 │    └─ maybe_offload()                  超长输出落盘，只回传预览
 ├─ context_modifier（仅非并发工具）
 ├─ _record_for_recovery()               read_file / skill 内容记入压缩恢复状态
 ├─ 记录本轮编辑过的文件（供诊断）
 └─ _post_tool_hook()
```

**注意：权限不是在 registry 统一检查的**，而是由每个 handler 自行调用 `_permission()` / `_shell_permission()`（builtin.py 中约 21 处）。新增工具时如果忘记调用，就不会有权限检查。这是后续"Tool Lifecycle 标准化"要收拢的点。

## 4. 实测轨迹（MiniMax-M3）

```text
user: 读取 README.md，用三句中文总结这个项目是什么。
 → provider call #1   模型返回 tool_call: read_file {"path": "README.md"}
 → registry.execute   read_file（只读，无需审批）
 → role=tool          README 内容回灌
 → provider call #2   模型返回 <think>…</think> + 三句中文总结，无 tool_calls
 → _persist_session   .lilbot/sessions/<id>.json
 → TurnFinished       completed 1 steps
```

## 5. 状态落盘点（与 Durable Execution 相关）

| 数据 | 存储 | 写入时机 |
|---|---|---|
| 对话 messages + usage | `.lilbot/sessions/<id>.json`（tmp + replace 原子写） | **仅在回合结束时**（agent.py:304 / 355） |
| 事件 | `.lilbot/events.jsonl`（追加） | 每次 turn_start / tool_call / compaction / recovery / turn_finished |
| 文件快照 | `FileHistory`（`.lilbot` 下） | 每次写类工具执行前 |
| 记忆 | `FileMemoryStore` | 后台抽取线程 |
| 计划状态 | `.lilbot/plan_mode.json` | EnterPlanMode / ExitPlanMode |

结论：**回合进行中崩溃（断电、Ctrl+C、未捕获的 ProviderError），这一回合内所有模型输出和工具结果都不会写入 session。** 已经执行的副作用（写文件、shell 命令）仍然生效，但重启后无从得知做到了哪一步。`events.jsonl` 有部分记录，但它不是可用于恢复的权威状态。这印证了审计报告 4.1 节的判断，也是 Week 2 RunState / Checkpoint 要解决的问题。
