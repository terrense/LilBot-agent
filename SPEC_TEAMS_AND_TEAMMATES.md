# SPEC: LilBot Teams & Teammates（团队 / 长驻队友）

状态：草案，待评审
为 LilBot 设计
作者：与 Claude 协作
日期：2026-06-18

---

## 0. 目标与非目标

### 目标
给 LilBot 增加**团队（team）+ 长驻队友（teammate）**能力：

1. **Team**：一个有名字的协作组，含一个 lead（主代理）+ 若干 member（队友），持久化到磁盘。
2. **长驻队友**：队友跑完一轮后**不退出**，进入 idle，等待 lead 下发新任务或 shutdown。区别于现有"一次性 subagent"。
3. **代理间通信（Mailbox）**：点对点 + 广播，文件邮箱 + 文件锁，跨进程安全。
4. **共享任务板（SharedTaskStore）**：pending/in_progress/completed/blocked，带 `assignee` 与 `blocks`/`blocked_by` 依赖。
5. **按名字寻址**：`SendMessage(to="reviewer")`，全局 name→agent_id 注册表。
6. **异步结果回流**：lead 主循环每轮开头自动 drain 邮箱 + 收完成通知，作为消息注入对话——lead 不必阻塞轮询。
7. **仪表盘面板**：在 `tui/dashboard.py` 增加队友实时进度面板（移植 `progress.py`）。

### 非目标（本期不做）
- tmux / iTerm2 可视化后端（Windows 不适用；只做 in-process）。
- coordinator mode（把 lead 工具收窄成纯调度）——留作后续可选项。
- 队友再开队友的多层嵌套。

### 保留 LilBot 既有优势（迁移时不得回退）
- ✅ `SubAgentManager` 的创建门禁 gates 与运行时 gate。
- ✅ subagent-tasks.json 持久化 + 重启恢复 + transcript。
- ✅ `subagents/render.py` 动态工具描述渲染。
- ✅ 现有线程模型（不引入 asyncio；用线程实现（不引入 asyncio））。

---

## 1. 架构总览

```
lilbot/
  teams/                      ← 新增模块
    __init__.py
    models.py                 ← AgentTeam / TeammateInfo
    mailbox.py                ← 文件邮箱 + 文件锁
    shared_task.py            ← 共享任务板
    registry.py               ← AgentNameRegistry 单例
    progress.py               ← TeammateProgress
    manager.py                ← TeamManager（改写：复用 LilBot worktree + 线程）
    spawn_inprocess.py        ← 长驻队友线程循环（asyncio→threading 改写）
  tools/
    builtin.py                ← 新增 TeamCreate / TeamDelete / SendMessage 工具；
                                Agent/agent_open 增加 team_name 分支
  subagents/manager.py        ← 队友复用此处的线程执行 + gates；新增长驻模式钩子
  core/agent.py               ← run_turn 主循环开头注入 team 通知回流
  tools/registry.py           ← ToolContext 增加 teams 字段
  cli.py                      ← build_runtime 装配 TeamManager
  tui/dashboard.py            ← 队友进度面板
```

持久化根目录：`<workspace>/.lilbot/teams/<slug>/`
- `config.json`     —— AgentTeam（成员列表）
- `tasks.json`      —— SharedTaskStore
- `mailbox/<agent_id>.json`(+ `.lock`) —— 每个代理一个收件箱

> 说明： 用 `~/./teams`（用户级）。LilBot 改用 **workspace 级** `.lilbot/teams`，与现有 subagent 持久化（`.lilbot/agents`、`.lilbot/subagent-tasks.json`）保持一致，便于按项目隔离与清理。

---

## 2. 数据结构（照抄为主）

### 2.1 models.py
- `TeammateInfo`: `name, agent_id, agent_type, model, worktree_path, backend_type, is_active, progress`
  - `backend_type` 固定 `"in-process"`（枚举保留以备扩展）。
  - `progress` 为运行时字段，序列化时排除。
- `AgentTeam`: `name, lead_agent_id, members[], config_path, description`
  - 方法：`get_member / add_member / remove_member / set_member_active / all_idle / active_members / save / load`。
- `resolve_team_dir(name)` → `config.state_dir / "teams" / slug`（需要 state_dir，见 §6 注入）。
- `_sanitize_name / unique_team_name` 照抄。

### 2.2 mailbox.py（照抄）
- `MailboxMessage`: `id, from_agent, to_agent, content, summary, message_type, timestamp, read, metadata`。
- `Mailbox`: `write / read / consume / broadcast / cleanup / cleanup_all`，`_with_lock` 用 `O_CREAT|O_EXCL` 文件锁 + 10 次重试 + 10s 过期。
- `create_message(...)` 工厂。
- **Windows 兼容性检查点**：`os.open(..., O_CREAT|O_EXCL|O_WRONLY)` 在 Windows 上可用；`lock_file.stat().st_mtime` 可用。✅ 无需改动。

### 2.3 shared_task.py（照抄）
- `SharedTask`: `id, title, description, status, assignee, blocks[], blocked_by[], created_by`。
- `SharedTaskStore`: `create / get / list_tasks / update / init_empty`，每次操作前 `_load()` 重读磁盘（多线程/多进程一致性）。

### 2.4 registry.py（照抄）
- `AgentNameRegistry` 进程内单例：`register / resolve / unregister / list_all`。
- ⚠️ 注意：单例是**进程内**的。LilBot 队友都是 in-process 线程，单例足够。

### 2.5 progress.py（照抄）
- `TeammateProgress`: 记录 `name, team_name, spinner_verb, status, last_message, tool_call_count, input_tokens, output_tokens`，线程安全（内部 Lock）。
- 供 dashboard 渲染；序列化时排除 Lock。

---

## 3. TeamManager（改写）

参照 `/teams/manager.py`，**去掉 tmux/iterm2 pane 逻辑**，worktree 复用 LilBot。

构造：`TeamManager(state_dir: Path, subagents: SubAgentManager)`
- 内部：`_teams, _task_stores, _mailboxes, _inprocess_handles, _teammate_team_map`。

核心方法：
- `create_team(name, lead_agent_id, description) -> AgentTeam`
  - 建目录、写 config.json、init tasks.json、建 mailbox/。
- `get_team / get_task_store / get_mailbox`（带磁盘懒加载缓存，照抄）。
- `register_member(team_name, member)` → 写 config + `AgentNameRegistry.register` + map。
- `set_member_idle(team_name, member_name)` → 标记 is_active=False + 给 lead 发 idle 通知。
- `on_teammate_completed(agent_id)` → 找队 → set_member_idle。
- `delete_team(team_name)`：拒绝有 active 成员；清理 handle.cancel、worktree（复用 SubAgentManager 的 `_cleanup_worktree` 思路）、mailbox、目录、注册表。
- **`drain_lead_mailbox() -> list[str]`**（关键，回流用）：遍历所有 team，`mailbox.consume(lead_agent_id)`，拼成：
  ```
  <team-notification team="{name}">
  from={from_agent}: {content}
  ...
  </team-notification>
  ```
- `get_all_teammate_progress() -> list[TeammateProgress]`（dashboard 用）。

> lead_agent_id：LilBot 的主 `Agent` 目前没有 agent_id。**新增**一个稳定 id：`Agent.agent_id = "lead"`（固定字符串即可，单 lead 场景）。 的 LEAD_NAME 也是 `"lead"`，邮箱按 `"lead"` 收件。统一用 `"lead"` 简化。

---

## 4. 长驻队友执行（spawn_inprocess.py，asyncio→threading 改写）

 用 `asyncio.Task` + `await asyncio.sleep` 轮询邮箱。LilBot 用线程改写：

```
class InProcessTeammateHandle:
    thread: threading.Thread
    name: str
    progress: TeammateProgress
    _stop: threading.Event
    done: bool（线程结束置 True）
    def cancel(): self._stop.set()

def spawn_inprocess_teammate(run_one_turn, name, mailbox, team_manager, team_name, prompt, progress):
    # run_one_turn(prompt, on_event) -> result_text  ← 由 SubAgentManager 提供
    def _loop():
        next_prompt = prompt
        while not stop.is_set():
            # 1. 注入邮箱堆积消息（拼进 next_prompt 前缀）
            pending = _inject_pending_messages(mailbox, name)
            turn_prompt = (pending + "\n\n" + next_prompt).strip() if pending else next_prompt
            # 2. 跑一整轮（复用 SubAgentManager 的 provider+tool loop）
            result = run_one_turn(turn_prompt, progress)
            next_prompt = ""
            # 3. 标记 idle + 通知 lead（写 lead 邮箱）
            progress.status = "idle"
            mailbox.write("lead", _idle_notification(name))
            team_manager.set_member_idle(team_name, name)
            # 4. 轮询等待新任务 / shutdown
            new_prompt, shutdown = _wait_for_next_prompt_or_shutdown(mailbox, name, stop)
            if shutdown or stop.is_set():
                progress.status = "completed"; return
            next_prompt = new_prompt
    thread = threading.Thread(target=_loop, daemon=True); thread.start()
```

- `_wait_for_next_prompt_or_shutdown`：`while not stop.is_set(): time.sleep(0.5); msgs = mailbox.consume(name); ...`
  - shutdown 判定：消息以 `[shutdown]` 前缀开头（照抄 `SHUTDOWN_PREFIX`）。
- 轮询间隔 0.5s（照抄 `IDLE_POLL_INTERVAL`）。

> **关键集成点**：`run_one_turn` 必须复用 `SubAgentManager` 现有的「provider → tool_schemas → 工具执行 → gates」循环（`_run` 内那段），而不是重写一遍。为此把 `SubAgentManager._run` 的「单轮执行核心」抽成一个可复用方法 `run_agent_turn(task, prompt, progress_cb) -> str`，长驻队友循环每轮调用它。这样 gates / 工具过滤 / transcript 全部自动继承。

---

## 5. 工具层（builtin.py）

### 5.1 Agent / agent_open 增加 `team_name`、`name` 参数
- 现有 `_agent_open(args, ctx)`：若 `args.get("team_name")` 非空 → 走 `_spawn_teammate(args, ctx)`；否则维持现有一次性 subagent 行为（**完全不动**）。
- `_spawn_teammate`：
  1. `team = ctx.teams.get_team(team_name)`，不存在则报错提示先 TeamCreate。
  2. 解析 teammate 名（重名加 `-2/-3`）。
  3. 解析 agent_type → `SubAgentManager.resolve_type`，拿到 definition（含 gates、allowed_tools）。
  4. 队友拿到的工具 = definition 工具集 **∪ 协调工具**（TaskCreate/TaskGet/TaskList/TaskUpdate/SendMessage）。协调工具实例绑定 `(team_manager, team_name, agent_id, agent_name)`。
  5. （可选）worktree 隔离：复用 SubAgentManager `_prepare_worktree`，路径 `.lilbot/worktrees/team-<team>/<name>`。
  6. `team_manager.register_member(...)`。
  7. `spawn_inprocess_teammate(...)` 启动长驻线程；`register_inprocess_handle`。
  8. 返回：队友名、agent_id、worktree、"系统将在其 idle/完成时通知 lead"。

### 5.2 新增工具
- **TeamCreate** `{team_name, description?}` → `ctx.teams.create_team(name, "lead", desc)`。描述里写清「用 Agent + team_name 派队友」「队友 idle 正常，发消息即唤醒」「消息会自动作为提醒回流」——实现 `TeamCreateTool.description`。
- **TeamDelete** `{team_name}` → `ctx.teams.delete_team`。
- **SendMessage** `{to, message, summary?, message_type?, metadata?}`：
  - lead 侧实例：`from_agent="lead"`。
  - `to="*"` 广播给所有 member；否则 `AgentNameRegistry.resolve(to)` → `mailbox.write`。
  - text 类型要求 summary（照抄校验）。
- **协调工具**（注入给队友，不在主注册表常驻）：TaskCreate/TaskGet/TaskList/TaskUpdate 操作 `ctx.teams.get_task_store(team_name)`；SendMessage（队友侧，from=队友名）。

### 5.3 注册
- 主注册表常驻：`TeamCreate, TeamDelete, SendMessage`（lead 用）。
- 协调工具（TaskCreate 等 + 队友侧 SendMessage）**不进主注册表**，由 `_spawn_teammate` 为每个队友单独构造、塞进该队友的私有 registry（类比  `build_teammate_tools`）。

### 5.4 gates 兼容
- 队友的工具集仍走 `SubAgentManager` 的 `SUBAGENT_ALWAYS_DISALLOWED_TOOLS` 等 gates。
- 把协调工具（TaskCreate/SendMessage 等）加入「允许」白名单，避免被 gate 误杀。
- `SUBAGENT_ALWAYS_DISALLOWED_TOOLS` 仍禁止队友再开 Agent/teammate（保持扁平）。

---

## 6. ToolContext / 装配改动

### 6.1 tools/registry.py
```python
@dataclass
class ToolContext:
    sandbox: Any
    permissions: Any
    memory: Any
    skills: Any
    subagents: Any
    mcp: Any
    config: Any
    teams: Any = None   # ← 新增
```

### 6.2 cli.py build_runtime
```python
from .teams import TeamManager
...
teams = TeamManager(cfg.state_dir, subagents)
ctx = ToolContext(sandbox, permissions, memory, skills, subagents, mcp, cfg, teams)
subagents.configure_tools(registry, ctx)
agent = Agent(cfg, provider, registry, ctx)
agent.agent_id = "lead"   # 固定 lead id
```

---

## 7. 异步结果回流（core/agent.py）

在 `run_turn` 的 `while steps < max_steps` 循环**开头**注入团队通知：

```python
while steps < self.config.max_steps:
    self._drain_team_notifications()   # ← 新增
    render_ctx = ...
    turn = self.provider.complete(self.messages, self.registry.schemas(render_ctx))
    ...
```

```python
def _drain_team_notifications(self) -> None:
    teams = getattr(self.ctx, "teams", None)
    if not teams:
        return
    for note in teams.drain_lead_mailbox():
        self.messages.append({
            "role": "user",
            "content": (
                "Internal LilBot team notification (not a new user request; "
                "use as coordination signal only).\n" + note
            ),
        })
```

- 复用现有「internal observation」消息约定（见 `_internal_observation_message`），告诉模型这是协调信号不是新用户请求。
- 这样 lead 在自己的工具循环中途，就能看到「reviewer 说 X 完成了」并据此决定下一步 SendMessage。

> **轮次外回流**：在 `interactive_loop` 每次 `ui.prompt()` 阻塞等待用户输入期间，队友可能完成但 lead 没在跑循环。可在 prompt 返回后、`run_turn` 前也 drain 一次（或在 dashboard 的刷新 tick 里 drain）。MVP：靠下一次 run_turn 开头的 drain 兜底即可。

---

## 8. Dashboard 面板（tui/dashboard.py）

- 新增「Teammates」面板：`ctx.teams.get_all_teammate_progress()` → 每行渲染
  `{name} [{status}] {spinner_verb}… tools={n} tok={in/out}  last: {last_message[:60]}`。
- 刷新 tick 里顺带 `ctx.teams.drain_lead_mailbox()` 注入（解决 §7 的轮次外回流）。
- 无队友时面板隐藏或显示「No teammates」。

---

## 9. 新增 slash 命令（cli.py，可选但建议）
- `/team list`：列出 teams 与成员状态。
- `/team new NAME`：本地建团队。
- `/team msg NAME TEXT`：手动给某队友发消息（调试用）。
- `/teams`：等价 `/team list`。
（与现有 `/agents` 并列。）

---

## 10. 实施顺序（建议分 PR / 提交）

1. **T1 数据层**：`teams/{models,mailbox,shared_task,registry,progress}.py`（照抄 + 路径改 state_dir）。纯单元可测，无副作用。
2. **T2 TeamManager**：`teams/manager.py`（去 pane、复用 worktree）。+ ToolContext 加 `teams` + cli 装配。
3. **T3 SubAgentManager 抽取**：把单轮执行核心抽成 `run_agent_turn(...)`，现有 `_run` 改为调用它（保证旧行为不变，跑现有 tests）。
4. **T4 长驻队友**：`teams/spawn_inprocess.py`（线程版）+ `_spawn_teammate` + Agent/agent_open 的 team_name 分支。
5. **T5 工具**：TeamCreate/TeamDelete/SendMessage + 队友协调工具注入 + gates 白名单。
6. **T6 回流**：`core/agent.py._drain_team_notifications`。
7. **T7 Dashboard**：队友进度面板 + tick drain。
8. **T8 slash 命令 + README + tests**。

每步可独立验证；T1–T3 不改变现有对用户可见行为。

---

## 11. 验收场景（端到端）

> 用户：「建个团队，让一个 implementer 改 `foo.py` 的 bug，再让一个 reviewer 复查它的改动。」

期望行为：
1. lead 调 `TeamCreate(team_name="bugfix")`。
2. lead 调 `Agent(team_name="bugfix", name="impl", subagent_type="implementer", prompt="...")` → 队友长驻线程启动。
3. impl 跑完一轮 → `SendMessage(to="lead", ...)` 报告 → idle。
4. 下一轮 lead 循环开头收到 `<team-notification>`，得知 impl 完成。
5. lead 调 `Agent(team_name="bugfix", name="rev", subagent_type="review", prompt="复查 impl 在 foo.py 的改动 ...")`。
6. rev 复查 → SendMessage 回报 → idle。
7. lead 收到回流 → 综合两者 → 回复用户。
8. lead 调 `TeamDelete("bugfix")` 清理（或保留）。
9. Dashboard 全程显示 impl/rev 的实时状态。

---

## 12. 风险与注意点

- **线程 vs 进程的单例**：`AgentNameRegistry` 进程内单例对 in-process 队友够用；若将来上多进程后端需改文件持久化。
- **回流时机**：lead 空闲等用户输入时不跑循环 → 靠 dashboard tick 或下次 run_turn 兜底（§7）。
- **mailbox 文件锁**：Windows 上 `O_EXCL` 可用，但杀进程留下的 `.lock` 由 10s 过期清理兜底（照抄）。
- **worktree 隔离对非 git 工作区**：复用 SubAgentManager 既有的「非 git 仓库则降级不隔离」逻辑，不报硬错。
- **gates 误杀协调工具**：务必把 TaskCreate/TaskGet/TaskList/TaskUpdate/SendMessage 加入队友允许集。
- **provider 线程安全**：多个队友线程并发调用同一 `provider.complete`；需确认 provider 无共享可变状态（现有 subagent 已并发，沿用其约束 + 现有 semaphore 限流）。
