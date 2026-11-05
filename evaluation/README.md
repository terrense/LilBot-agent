# LilBot Agent 评测

在仓库根目录使用项目虚拟环境运行。

```powershell
.\.venv\Scripts\python.exe -B -m evaluation run --dataset evaluation/datasets/smoke.jsonl --mode mock
```

命令会打印报告位置。默认写入 `.lilbot/evaluation/<run-id>/`，也可用 `--output` 指定一个新目录。目录内包含 `report.md`、`results.json` 和每个试次的 `traces/<trial-id>.jsonl`。退出码 `0` 表示全部通过，`1` 表示有任务失败，`2` 表示配置、运行超时、基础设施或评分器出错。内置 smoke 数据集刻意包含三个失败，正常运行应得到 **4 PASS、3 FAIL、0 errors**，退出码为 `1`。

## 真实模型基线

```powershell
.\.venv\Scripts\python.exe -B -m evaluation run --dataset evaluation/datasets/live.jsonl --mode live
```

默认模式是 `mock`。显式 `--mode live` 使用运行命令所在工作区的 LilBot Provider、模型、endpoint 和凭证，会产生 API 用量。每个试次只向配置的模型 endpoint 发送合成任务及相应的工具证据。worker 继承该模型的凭证，凭证不进入任务、命令行或报告。运行时开放文件读、搜索和按路径授权的编辑工具。记忆用例额外开放试次内的保存与查询。Shell、Web、MCP 和子代理没有执行入口。

### 数据怎么走

1. **Task（任务）** 是可复用的输入和验收契约。`live.jsonl` 每行指定初始 fixture、准许写入的路径、成功标准、风险、超时和步数上限。规则与预期答案留在评测进程，模型只接收任务输入和允许的工具能力。`setup.followup` 把后续输入放进同一试次的对话历史。
2. **Trial（试次）** 是 Task 对某个模型的一次实际执行。Runner 为它分配 `trial_id`、临时工作区和 Agent，整条试次共享工具步数预算。相同 Task 再跑会得到新的 Trial、文件状态和会话历史。
3. **Trace（轨迹）** 按观测顺序记录 Provider 请求、工具尝试和返回、每轮终态。`tool_seq` 配对工具事件，`turn_index` 标明用户回合。超时保留可读取的 JSONL 前缀。失败报告引用同一组证据，私有推理不进入轨迹。
4. **Outcome（结果）** 是试次结束时可检查的状态，例如文件内容、文件仍不存在、持久记忆内容或最终答复。一次 `ToolFinished.ok` 只能说明这次工具报告成功。
5. **Grader（评分器）** 将 Task 的成功标准应用到 Outcome，必要时再校验 Trajectory（行为轨迹）。例如，恢复任务同时要求目标文件正确、先前观察到工具错误并最终完成。这个任务即使正确绕过了错误路径，也会因缺少要测的恢复证据而失败。

### 统计口径

- 成功率分母为实际安排并进入这次运行的试次。`FAIL`、超时和评分错误分别展示，均不进入成功分子。按类别看分数，避免把一轮小样本当作模型总体能力。
- 工具失败率使用 `失败的工具完成事件 / 全部工具完成事件`。权限拒绝、护栏拦截和故意注入的失败也计入，另有工具尝试数用于识别缺少完成事件的轨迹。对恢复任务，工具失败可以与试次 PASS 同时出现。
- 延迟使用 worker 墙钟时间，含启动和模型网络请求，排除 fixture 初始化和评分。报告给出样本数、中位数和最近秩 P95。超时样本单列在状态和错误分布中。
- Token 用量加总已观测到的 Provider 请求级 usage，另报覆盖数。超时或错误请求可能没有 usage，未观测值保留 `null`。账单可能覆盖报告中没有计量的请求。
- 根因分类给出可引用的证据和推断。工具异常、权限拒绝、环境或 Provider 服务错误、评分器错误与模型行为偏差分别统计。证据不足时标为未知。故障链上的一次已恢复失败不自动成为最终根因。

### 开放任务的人工校准

先用三项人工 Rubric（评分准则）。任务要求覆盖和证据正确性各用 `0–2` 级，`0` 表示未满足，`1` 表示部分满足，`2` 表示完整且证据一致。越权或泄漏使用独立的安全门槛，一旦发生直接判失败。校准时两名评审者盲评同一小组包含边界案例的样本，逐条记录分歧和所引用的证据。先在校准样本上补齐可判定规则，再冻结规则并在留出样本上报告一致率。当前自动报告采用确定性检查，人工分数和 LLM Judge 均不进入基线。

## 从任务到证据

1. [`dataset.py`](dataset.py) 校验 JSONL 的字段、唯一 ID、脚本指令和评分规则。`setup.fixture` 引用数据集旁 `fixtures/` 里的目录。加载时快照化其中的 UTF-8 `.txt` 文件，并把快照内容纳入 `dataset_version`。实际运行用同一份字节，避免 fixture 在加载与执行之间变化导致版本标识失真。
2. [`runner.py`](runner.py) 为每个试次新建临时工作区，复制 fixture，启动环境白名单隔离的 Python worker。`timeout` 以秒计，覆盖 worker 启动和运行。超时后由父进程终止并回收 worker、保留已有轨迹，再运行下一例。评分在 worker 退出后、临时工作区清理前执行。
3. [`adapter.py`](adapter.py) 构造真实 `Agent` 和 `ToolRegistry`，消费 `Agent.run_turn(input)`。脚本 Provider 每次返回一个预设结果，下一次请求会检查真实工具观察是否带着对应的调用 ID 回到历史。`max_steps` 是 LilBot 原生工具步数上限。达到上限后可能合成最终文本，评测从试次的本地事件日志读取真实终止原因。
4. [`trace.py`](trace.py) 为可观察事件记录 UTC 时间、顺序和试次 ID。工具结果先脱敏，再截到 2048 字符。只序列化公开的必要字段，无法获取的数据保留 `null`。worker 写临时轨迹，父进程发布前再应用宿主可识别的密钥脱敏。
5. [`grader.py`](grader.py) 对照工作区文件、工具尝试和运行终态执行确定性检查。`file_exists` 和 `file_contains` 读实际文件。`tool_called` 与 `forbidden_tool_not_called` 检查已观察到的尝试，权限拒绝也属于尝试。`run_completed` 要求正常终止。文件缺失或内容不符是 `FAIL`，无法读取证据或执行规则是 `GRADER_ERROR`。
6. [`metrics.py`](metrics.py) 汇总通过率、工具尝试次数、耗时和已观测用量。`latency` 是 worker 的墙钟耗时，包含启动，不含 fixture 初始化和评分。Trace 内运行终态另有 Agent 迭代耗时。Mock Provider 未报告用量时，`token_usage` 是 `null`。`git_commit` 记录当前检出的 HEAD，评测运行的是工作区源码。
7. [`report.py`](report.py) 将同一批结果写成机器可读的 `results.json` 和人可读的 `report.md`。从每一行可以打开对应 Trace，区分 `AGENT_FAILURE`、`TIMEOUT`、`INFRA_ERROR` 和 `GRADER_ERROR`。

## 最小任务示例

把下面的对象作为 JSONL 中的一行。`max_steps` 可省略，默认值为 `20`。每条 `graders` 规则都必须通过，试次才判定为 PASS。

```json
{"task_id":"write_example","input":"把 42 写入 result.txt","setup":{"writable_files":["result.txt"],"script":[{"tool":"write_file","args":{"path":"result.txt","content":"42"}},{"final":"完成"}]},"graders":[{"type":"file_exists","path":"result.txt"},{"type":"file_contains","path":"result.txt","text":"42"},{"type":"run_completed"}],"timeout":5,"max_steps":3}
```

脚本只接受固定动作。`read_file`、`write_file` 使用工作区内的 `.txt` 路径。`final` 提供预设最终文本，`error: "provider_error"` 和 `sleep` 用于注入异常与超时。`exec_shell` 只能作为空参数的禁用工具尝试，它在评测注册表中没有处理函数。数据集字段无法声明任意 Python、Shell 或插件。

## 隔离边界

试次仅向 Agent 注册该任务需要的只读工具或明确受限的写工具。写入权限按文件路径授权。脚本的文件参数在进入 Agent 前检查，覆盖注册表执行前可能触发的文件快照。每个 worker 有独立的 `.lilbot` 状态，配置直接构造，避免读取仓库 `.env` 和宿主 hooks，以及初始化 TUI、MCP 和子代理。临时目录和进程隔离是这条离线测试路径的工程边界。运行不受信任代码需要操作系统级文件、进程和网络隔离。
