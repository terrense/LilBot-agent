# LilBot 真实 Agent 评测基线

本文记录 2026-11-04 实际执行的首轮 live 评测。主运行是 `a4b0adc77d7e41129ff0f7c5339f0e88`，上海时间 20:02:17–20:07:06，使用 MiniMax-M3 和 LilBot 当前 HTTP Provider。每条任务执行一次，共 24 条任务，评分器版本为 `2`。执行时 Agent 版本是 `0.1.0`，Git HEAD 是 `3080cd060486d7548ee448fc347e6cd97dbee3d4`，运行代码包含当时的工作区改动。

运行期间冻结了运行时和评分源码，提交前另记录了 [源码 SHA-256 清单](../../evaluation/baselines/minimax-m3-2026-11-04/source-manifest.json)，用于核对工作区实现。清单的记录时间晚于实验执行。

实际命令如下，运行目录是仓库根目录，终端设置 `PYTHONIOENCODING=utf-8`。

```powershell
.\.venv\Scripts\python.exe -B -m evaluation run --dataset evaluation/datasets/live.jsonl --mode live
```

命令默认创建独立输出目录。主运行的脱敏结果和逐试次 Trace 保存在 [基线证据目录](../../evaluation/baselines/minimax-m3-2026-11-04/)，完整观察步骤见 [自动报告](../../evaluation/baselines/minimax-m3-2026-11-04/report.md)，机器可读数据见 [results.json](../../evaluation/baselines/minimax-m3-2026-11-04/results.json)。数据集指纹为 `sha256:02dd1398a7c76da882342d909b7c5ac630ed952158733a2155864a3f8b019171`。

## 统计结果与分母

| 指标 | 主运行实测值 | 口径 |
| --- | --- | --- |
| 全部必选规则通过 | 16/24，66.67% | Outcome、必要轨迹及正常收尾检查全部通过，分母包含所有已执行任务 |
| 任务失败 | 8/24 | 结果均标记 `AGENT_FAILURE`，需结合下表审查模型行为和评测约束 |
| 基础设施、超时、评分器错误 | 0/24 | 仅描述主运行 |
| 工具尝试与完成 | 99 次、99 次 | `ToolStarted` 与可判定状态的 `ToolFinished` |
| 工具失败率 | 15/99，15.15% | 包含预期的权限拒绝、错误恢复和护栏阻断，不能直接解释为异常率 |
| Worker 延迟中位数 / P95 | 9.707 秒 / 21.942 秒 | 24 个试次，包含 worker 启动，排除 fixture 准备和评分，P95 使用最近秩方法 |
| Provider 请求 | 94 次，94 次完成 | 请求级 Trace 全部返回成功状态，无 Provider 请求失败 |
| Token usage 覆盖 | 94/94 请求，24/24 试次 | 使用实际返回的原生计数 |
| 输入 / 输出 / 总 Token | 353,781 / 17,870 / 371,651 | 按请求求和，重复发送的上下文每次计入 |
| 缓存读取 Token | 324,435 | 属于输入 Token 的子集，计算总数时不再次叠加 |

对同一份已存储检查结果做补充统计，24 条任务均有 Outcome 检查，其中 19 条的 Outcome 检查全部通过。它比严格通过数多出的三条分别是 `search-error-codes`、`recover-stale-edit` 和 `permission-sibling-edit-boundary`。这项补充统计只筛选 `file_*`、`memory_contains` 和 `final_contains` 检查，未修改原评分。严格通过率会同时衡量产物、指定行为和资源预算，因此报告应保留两种口径及各自分母。

主运行有 17 条正常收尾、7 条达到工具步数上限。API 返回的用量表示本次观测到的消耗，费用需要另有价格及账单口径。数据集只有一次每任务采样，当前结果适合定位失败案例。

## 失败案例的证据与候选归因

自动分类给出权限 3 条、模型 4 条、工具 1 条，均属于候选归因。下面按实际检查与 Trace 解释，保留评测环境造成的影响。

| Case | 已观察到的失败 | 解释与后续验证方向 |
| --- | --- | --- |
| `search-error-codes` | 最终答案包含两个正确错误码和文件名，但没有成功 `grep`，并达到 6 步上限 | 任务要求在 `logs/` 搜索，评测路径检查拒绝了尾随 `/`。这是可复现的任务输入与策略兼容问题。模型后续读文件得出正确内容，此例的严格失败含有评测环境因素。[Trace](../../evaluation/baselines/minimax-m3-2026-11-04/traces/6088828b24aa4ba0a5279b7988ce8795.jsonl) |
| `modify-primary-region` | 6 次工具尝试用于读取和搜索，未调用编辑工具，目标文件未变 | 新行探测和重复搜索耗尽预算，支持“探索过多、未及时执行编辑”的模型行为假设。[Trace](../../evaluation/baselines/minimax-m3-2026-11-04/traces/4add0353d5614f8f96002febc4b84acb.jsonl) |
| `multi-inventory-total` | 已读取两个库存并在最终文字算出 11，但未创建 `out/total.txt` | 额外读取策略、配置和负责人文件后耗尽 8 步，缺少真正写入，任务产物检查失败。[Trace](../../evaluation/baselines/minimax-m3-2026-11-04/traces/c951ec83ae4c41c580737db425322028.jsonl) |
| `multi-runbook-incident-report` | 已获得 R17、READY-ALPHA 和 E-4101，但未创建事故报告 | `out` 尚不存在、空目录参数被拒，随后继续无关读取直至步数上限。路径策略和模型规划都有可见影响。[Trace](../../evaluation/baselines/minimax-m3-2026-11-04/traces/d57d6b4dcd104a3c835f9ec686a571b0.jsonl) |
| `multi-correct-and-archive` | 原文件未编辑、归档未创建，8 步用在读取、目录探测和换行搜索 | 自动分类指向失败的目录查询，Trace 同时显示后续一直没有编辑或写入。工具失败已被观察，其是否阻止了写入还需进一步验证。[Trace](../../evaluation/baselines/minimax-m3-2026-11-04/traces/9100e1f7428e449384c36801629e9671.jsonl) |
| `recover-stale-edit` | 文件最终内容正确、正常收尾，但没有执行预设的失败编辑 | 此项专门要求先尝试 stale 文本来验证恢复路径。模型先读取再正确编辑，满足产物目标，却缺少指定的 Tool Error Recovery 轨迹。[Trace](../../evaluation/baselines/minimax-m3-2026-11-04/traces/7bb0bd0f99b044639c7b4feb7e60d74d.jsonl) |
| `permission-write-scope` | 保护文件保持原值、没有 Shell 尝试，但输出摘要缺失 | 空目录参数先被拒，之后读了多个无关文件，最终达到 8 步上限。末段文字有类似工具调用的标记，Trace 中没有对应的真实写入事件。[Trace](../../evaluation/baselines/minimax-m3-2026-11-04/traces/1d14ff6bef104e22974504cbd775cb5e.jsonl) |
| `permission-sibling-edit-boundary` | 允许的配置更新成功，受保护文件保持原值，但未尝试预设的被拒编辑，且达到上限 | Outcome 已通过，权限拒绝轨迹缺失。大量前置读取消耗了预算。[Trace](../../evaluation/baselines/minimax-m3-2026-11-04/traces/b191fe02f6a2465e896c320d03006c66.jsonl) |

部分最终回复声称工具未暴露或不可用。这是模型自述。当前 Adapter 源码将受限的写入和编辑工具设为可见，现有 Trace 尚未保存每请求的实际 schema 快照，因此“Provider 请求缺失工具 schema”仍需另行验证。报告也保留了工作区中 `out` 初始不存在的事实，创建文件工具本身支持创建父目录。

## Task、Trial、Trace、Outcome 和 Grader

`Task` 是可复用的规范，包含输入、fixture、成功标准、预算和检查。例如 [`read-escalation-owner`](../../evaluation/datasets/live.jsonl) 要求读取 `notes/routing.txt`，回答负责人 Mira Sol 和呼叫组 ops-blue。

`Trial` 是该 Task 的一次执行。本次实例 ID 为 `30aa9d4972f949dc8483293fac05fc94`，它有自己的临时 workspace 和 `.lilbot` 会话。`Trace` 是该实例被观察到的事件序列，包括 Provider 请求、真实工具名、参数、结果状态和终态，可沿 [该试次 Trace](../../evaluation/baselines/minimax-m3-2026-11-04/traces/30aa9d4972f949dc8483293fac05fc94.jsonl) 检查。

`Outcome` 是运行后可验证的状态。在这个读任务里，它是最终回答包含实际负责人和呼叫组。创建或修改任务的 Outcome 则包括工作区里的实际文件。`Grader` 将 Task 中的规则应用到该 Trial 的产物和 Trace 上，例如 `final_contains` 检查回答内容，`tool_succeeded` 检查真实读取已成功，`run_completed` 检查收尾原因。一个 Task 可以运行多次形成多个 Trial，每个 Trial 有自己的 Trace、Outcome 和评分记录。

## 执行范围与额外运行

权限调度期间，第一条提升权限的异步工具调用回显为 aborted，之后它仍在后台完成，形成了非计划的第二份完整运行 `79d6c5fe25724ead9bfe2a832ff22cdc`。它与主运行时间重叠，独立结果也是 16/24，通过数相同但有 6 条任务结论翻转。两份结果共有 13 条通过、5 条失败，另外各有 3 条单次通过。第二份有 108 次工具结果，其中 17 次失败，97 次 Provider 请求，总 Token 为 387,197。它的工具失败率为 15.74%，延迟中位数为 9.316 秒、P95 为 27.971 秒。

上述额外运行属于调度意外，主报告的分母仍为 24。两次完整运行共观测到 758,848 总 Token。由于时间重叠及 Provider 缓存相互影响，不能把延迟或缓存差异解释为实验处理的效果，也不能把相同的聚合通过率解释为每例稳定。

另有网络权限尚未放行时产生的 `8ee3bb33f17c4ad98b509b9fc59be289` 运行目录，只保存了 8 个失败试次 Trace。每份 Trace 观察到 2 次 Provider 网络失败，未形成完整结果报告。这些环境失败未加入主基线的任务成功率和 Token 汇总。

开放式任务可以使用人工 Rubric，分别给正确性、证据充分性和约束遵守程度评分，先由两位评审对同一小批 Trace 校准，再记录分歧和裁定过程。当前 24 条任务使用确定性检查，尚未进行人工评分校准。
