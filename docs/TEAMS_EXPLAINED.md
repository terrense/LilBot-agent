# LilBot Teams vs Subagent — 源码级讲义

> 面向"能讲给别人 / 能应对大厂面试 / 能开课讲授"的深度讲解。
> 每个论点都钉在本仓库真实源码上(`文件:行`),并附面试追问应答。
> 配套现场 demo:[`experiment/teams_demo.py`](../experiment/teams_demo.py)。

---

## 0. 一句话抓住本质

> **Subagent 是"一次性外包工":派出去 → 干完 → 销毁,你得主动打电话问结果(拉)。**
> **Teammate 是"入职的正式组员":长期在岗 → 干完转待命 → 主动汇报,结果自己飘回来(推),组里还有共享看板和花名册。**

技术上,差异拆成**三根正交的轴**:

| 轴 | Subagent | Teammate |
|---|---|---|
| **① 生命周期** | one-shot(run→die) | long-lived(run→idle→wake→…) |
| **② 结果流向** | PULL(父代理轮询) | PUSH(邮箱 + 自动回流进主循环) |
| **③ 拓扑结构** | 星型/孤岛(只对父汇报,彼此不可见) | 图(队友互发消息 + 共享任务板 + 按名寻址) |

---

## 1. 轴①:生命周期 —— "干完就死" vs "干完待命"

### Subagent:一锤子买卖

入口 `tools/builtin.py:_agent_open` → `ctx.subagents.open()`(`subagents/manager.py`):

```
open() → 跑创建门禁 _validate_creation_gates → 建 SubAgentTask
       → threading.Thread(target=self._run) 启动后台线程
```

`_run()` 是**线性的、有终点的**(`subagents/manager.py`):

```python
self._semaphore.acquire()            # 占一个并发槽
task.status = "running"
self._prepare_worktree(task)         # 可选 worktree
task.result = self.run_agent_turn(definition, task, task.prompt)  # 只跑【一轮】
task.status = "completed"            # 到达终点
self._cleanup_worktree(task)
self._semaphore.release()            # 线程结束 → 销毁
```

`SubAgentTask.terminal`(`status in {completed, failed, cancelled, ...}`)一旦为真,agent 就死了,线程退出。

### Teammate:长驻循环

入口是**同一个** `_agent_open`,但带了 `team_name` → 分流到 `_spawn_teammate`。它先用 `build_teammate_task` 建任务(`status="running"` 但**不启线程**),再调 `spawn_inprocess_teammate`(`teams/spawn_inprocess.py`)。

关键在 `_loop()`——**while 循环,没有自然终点**:

```python
def _loop():
    next_prompt = prompt
    while not stop.is_set():                        # ← 长驻!
        result = run_one_turn(turn_prompt, progress)    # 跑一轮(复用 run_agent_turn)
        progress.status = "idle"
        team_manager.notify_lead(...)               # 主动汇报 lead
        team_manager.set_member_idle(...)
        new_prompt, shutdown = _wait_for_next(stop)  # ← 阻塞轮询邮箱等下一个任务
        if shutdown or stop.is_set():
            return                                   # 只有 [shutdown]/cancel 才退出
        next_prompt = new_prompt
```

`_wait_for_next` 每 `IDLE_POLL_INTERVAL=0.5s` 调一次 `mailbox.consume(name)`,遇 `SHUTDOWN_PREFIX="[shutdown]"` 退出。

**面试官追问:既然都跑一轮,凭什么说生命周期不同?**
> 看"一轮之后"。Subagent 一轮后线程 return、状态 terminal、被销毁;Teammate 一轮后进 `idle` 并**继续占着线程在 while 里轮询邮箱**,可被 `send_message` 唤醒跑第 N 轮。所以 Teammate 是"有状态的常驻服务",Subagent 是"无状态的函数调用"。这也是为什么队友单轮预算更高(`TEAMMATE_MAX_TOOL_STEPS=16` vs `SUBAGENT_MAX_TOOL_STEPS=6`)——常驻组员一轮要"探索+落地",探针只需快速取证。

**工程亮点**:两者**复用同一个执行核心** `SubAgentManager.run_agent_turn`(从 `_run` 抽出),队友每轮自动继承 subagent 的 **gates、工具过滤、transcript**——单一事实源,安全策略不漂移(DRY + 安全一致性)。

---

## 2. 轴②:结果流向 —— PULL vs PUSH(最值钱的一段)

### Subagent 是 PULL:父代理"打电话问"

`agent_eval`(`builtin.py` → `SubAgentManager.eval`):

```python
def eval(self, task_ref, *, block=True, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if task.terminal: break
        time.sleep(0.05)        # ← 父代理在这儿【阻塞自旋】等结果
    return task
```

代价:父代理**卡住**,等待期间什么都干不了。

### Teammate 是 PUSH:结果自己飘回主循环(系统灵魂)

**第 1 跳 — 写信**:`_loop` 里 `team_manager.notify_lead(...)` → 往 `lead` 收件箱写消息(`teams/manager.py:notify_lead`)。队友也可干活中途用 `send_message` 主动发。

**第 2 跳 — 攒信/取信**:`TeamManager.drain_lead_mailbox()` 遍历所有 team,`mailbox.consume("lead")` 取未读,拼成:

```xml
<team-notification team="demo">
from=impl: [idle] impl: 改完了 ...
</team-notification>
```

**第 3 跳 — 主循环每轮自动吸收**:`core/agent.py` 的 `run_turn`,`while` 循环**最顶端**:

```python
while steps < self.config.max_steps:
    self._drain_team_notifications()        # ← 每轮先把队友消息吸进来
    turn = self.provider.complete(self.messages, ...)
```

`_drain_team_notifications` 把每条通知作为 **user 角色消息**追加进 `self.messages`,并标注"这是协调信号,不是新的用户请求"。

**效果**:lead 干着自己的活,**下一轮 LLM 调用前**就自动"读到"了"impl 改完了",据此决定下一步。**全程不阻塞、不轮询。**

**面试官追问 1:为什么注入成 user 消息而不是 system?**
> ① 它得能被模型当成"对话里发生的新事件"推理,user 角色最自然;② 用 `<team-notification>` XML 标签做**带内信令**,模型一眼区分"队友信号 vs 人类新指令";③ 复用现有 `_internal_observation_message` 约定。

**面试官追问 2:lead 在等用户输入、没跑循环时,队友消息怎么办?**
> 两层兜底:① 下次 `run_turn` 一开始就 drain,绝不丢;② Dashboard 的 `_teammate_work_lines` 刷新也能看到。消息存在**文件邮箱**不会丢,只是"被看到"时机延后——这正是选文件邮箱(持久)而非内存队列的好处。

---

## 3. 轴③:拓扑 —— 孤岛 vs 协作图

### Subagent:孤岛 + 扁平

- 子代理之间**无法通信**,只能各自回结果给父代理(星型,边是"拉")。
- **不能再开子代理**:`SUBAGENT_ALWAYS_DISALLOWED_TOOLS` 含 `AGENT_TOOLS`,`_tool_schemas_for_task` 将其过滤——层级**扁平**,防递归爆炸。

### Teammate:协作图,三个共享设施

全部落在 `<workspace>/.lilbot/teams/<slug>/`:

**(a) 邮箱 `mailbox/`(`teams/mailbox.py`)** —— 点对点 + 广播。
核心 `_with_lock`:`os.open(..., O_CREAT|O_EXCL)` 抢 `.lock` 文件做互斥,10s 过期自动回收(防死锁),失败重试。经典**文件锁**。
> **实战坑**:Windows 多线程争锁,`os.open` 可能抛 `PermissionError`(非 FileExistsError),原版 mewcode 未处理会崩写线程丢消息。本仓库当瞬时争用**重试**修掉(commit `aba5aa1`)。这是"移植开源代码要注意跨平台语义差异"的好例子。

**(b) 共享任务板 `tasks.json`(`teams/shared_task.py`)** —— `SharedTask` 带 `assignee`、`blocks`/`blocked_by` 依赖,状态 pending/in_progress/completed/blocked。每次操作前 `_load()` 重读磁盘保证一致视图。

**(c) 花名册 `AgentNameRegistry`(`teams/registry.py`)** —— 进程内单例 `name → agent_id`,让你 `send_message(to="reviewer")` 按名喊人,配合 `to="*"` 广播。

**面试官追问:邮箱为什么用文件 + 文件锁,不用 `queue.Queue`?**
> ① 持久性(进程崩了消息还在);② 可扩展到跨进程(将来 tmux/iterm2 后端每个队友是独立进程);③ 与 `.lilbot/` 状态目录统一,便于审计清理。代价是自己实现锁 + Windows 兼容细节。典型"用一点复杂度换持久性和可扩展性"。

---

## 4. 两条调用链(白板必画)

```
              ┌─────────────── 同一个入口 _agent_open ───────────────┐
              │                                                       │
       无 team_name                                            有 team_name
              │                                                       │
   SubAgentManager.open()                                    _spawn_teammate()
   ├ 创建门禁 gates                                           ├ build_teammate_task(角色工具 + 协调工具)
   ├ Thread → _run()                                         ├ 身份注入: team_ctx = replace(ctx, team_name, agent_name)
   │   ├ semaphore.acquire                                   ├ 隔离则: 换 accept-all 权限 + worktree sandbox
   │   ├ run_agent_turn() ←──── 同一执行核心 ────→ run_one_turn(run_agent_turn, slot, 16步)
   │   ├ status=completed (终点)                             └ spawn_inprocess_teammate → _loop(while 长驻)
   │   └ 线程结束/销毁                                              ├ 跑一轮 → idle → notify_lead(写邮箱)
   │                                                               └ _wait_for_next(轮询邮箱/0.5s/shutdown)
   父代理 agent_eval(block) 轮询拉结果                         lead run_turn 每轮 _drain_team_notifications 推回
```

---

## 5. 身份与权限:Teammate 多出来的两件"装备"

Subagent 用 `_ctx_for_task` 跑(共享或 worktree 沙箱)。Teammate 在此之上做**身份注入**:

```python
# tools/builtin.py:_spawn_teammate
team_ctx = replace(ctx, team_name=team_name, agent_name=teammate_name)
```

因为本仓库用了 **"全局无状态工具 + ctx 读身份"**(而非 mewcode 的"每队友建私有 registry")。`send_message`/`team_task_*` 从 `ctx.team_name / ctx.agent_name` 知道"我是谁",`lead` 则两者为 None。

**面试官追问:为什么不照搬 mewcode 给每个队友建独立 registry?**
> lilbot 工具本就是"无状态 handler 读 ToolContext",顺着走把身份放进 ctx 克隆即可,代码更少、还让 lead 自动获得这些工具。移植不是复制粘贴,要尊重宿主架构。

**权限安全边界**(commit `aba5aa1`):隔离队友的 `team_ctx` 把 `permissions` 换成 `accept-all`,**但仅当它在自己的 worktree 里**——`PathSandbox(worktree_root)` 已把写操作锁死在那棵独立工作树内,改动碰不到主线,需经 lead 审阅合并才生效。**沙箱隔离 = 放权的安全前提**。非隔离队友继承 lead 权限(默认 ask 写不了)。

> 真实验证:默认 `ask` 模式下,隔离的 implementer 队友自主完成 `a-b → a+b` 修复并自测 `add(2,3)`。

---

## 6. 并发模型:为什么是线程 + 信号量

- lilbot 的 provider 是**同步** httpx 调用,所以队友用 `threading.Thread`(daemon)而非 asyncio(顺应宿主;mewcode 是 asyncio 版)。
- 并发上限:`run_one_turn` 用 `with subagents.slot()`(`BoundedSemaphore`)只包住**活跃的那一轮**,队友 idle 时**不占槽**,避免长驻把并发池占满。这是"长驻 + 限流"如何共存的答案。

**面试官追问:多个队友线程并发调同一个 provider,线程安全吗?**
> 沿用现有 subagent 并发约束 + 信号量限流;`provider.complete` 每次是独立请求、无共享可变状态。真正的共享态(邮箱、任务板)走文件锁 / 重读磁盘保证一致性。

---

## 7. 持久化与清理

- **Subagent**:`subagent-tasks.json` + transcripts + **重启恢复**(`_resume_recovered_tasks`)——本仓库比 mewcode 强(mewcode 后台任务重启即丢),迁移时特意保留。
- **Teammate**:团队三件套(config/tasks/mailbox)持久在 `.lilbot/teams/<slug>/`;`team_delete` 时 `handle.cancel()`(置 stop event 停 while)+ 清 worktree(`_cleanup_worktree` 在 workspace 根跑 `git worktree remove`)+ 清邮箱目录。

---

## 8. 面试速答卡

1. **一句话区别?** Subagent 是无状态一次性函数调用(拉结果);Teammate 是有状态长驻服务(推结果 + 同伴协作)。
2. **何时用谁?** 独立的一次性取证/实现/审查 → subagent 并行;需要多角色长期协作、互相依赖、边干边汇报(implementer+reviewer 往返)→ team。
3. **核心创新点?** 结果从"父代理阻塞轮询"变成"邮箱 + 主循环每轮自动 drain 注入",lead 不阻塞。
4. **怎么保证安全不失控?** 子代理禁止再开子代理(扁平);队友放权只在 worktree 沙箱内;所有创建走 gates;工具按角色过滤。
5. **跨平台坑?** Windows 文件锁的 `PermissionError` 要当瞬时争用重试。
6. **如何复用而不重复?** 队友与子代理共用 `run_agent_turn`,gates/工具/transcript 单一事实源。

---

## 9. 怎么讲给小白(开课类比)

> 把 LLM agent 想成一个"会用工具的实习生"。
> - **Subagent** = 临时叫个跑腿的:"去查下 X",查完递张纸条就走。你能同时叫好几个,但他们互不认识,你还得守着等纸条。
> - **Team** = 组个项目组:招了"开发"和"测试"两名**正式组员**,有**组内群聊**(邮箱)、一块**共享看板**(任务板)、一份**花名册**(按名喊人)。开发改完代码**主动在群里 @ 你**,这条消息会**自动出现在你下一次开口前**;你再让测试去验。整个过程你该忙啥忙啥,进度自己飘过来。

---

## 10. 源码走读路线(照屏逐行讲)

按 **入口 → 分流 → 执行核心 → 回流 → 协作设施** 五站。

| 站 | 位置 | 讲什么 |
|---|---|---|
| 0 开场 | `tools/builtin.py:_agent_open` | 同一入口,看 `team_name` 一个参数分两条命运线 |
| 1 Subagent 线 | `subagents/manager.py` `open()` / `_run()`(线性有终点)/ `eval()`(while 自旋拉结果) | 无状态函数调用 + PULL |
| 2 执行核心(共用) | `subagents/manager.py` `run_agent_turn()` + `_tool_schemas_for_task()` | gates/工具/transcript 单一事实源;队友传 `max_steps=16` |
| 3 Teammate 线 | `tools/builtin.py:_spawn_teammate`(`build_teammate_task` / 身份注入 / `slot`)+ `teams/spawn_inprocess.py:_loop`(while 长驻)/ `_wait_for_next` | 有状态常驻服务 |
| 4 回流三跳(灵魂) | `teams/manager.py:notify_lead` → `teams/manager.py:drain_lead_mailbox` → `core/agent.py` `run_turn` 顶端 `_drain_team_notifications` | PULL→PUSH 就在主循环顶端那一行 |
| 5 协作设施 | `teams/mailbox.py:_with_lock`(文件锁 + Windows 坑)/ `teams/shared_task.py`(依赖)/ `teams/registry.py`(按名喊人 + `broadcast`) | 孤岛 vs 协作图 |

讲法顺序建议:**先跑 demo 建立直觉(现象)→ 再走源码回答"凭什么"(机制)→ 最后抛三根轴 + 速答卡收口。**

现场 demo:
```bash
python experiment/teams_demo.py          # stub 模式:确定性、~2s、零网络翻车(讲机制)
python experiment/teams_demo.py --real   # 真 DeepSeek:联网真改 bug,展示 worktree 隔离
```
