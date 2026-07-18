# Agent 设计

本文档记录 Fold Agent 的设计边界和维护口径。Agent 运行时直接读取系统 Prompt 和策略产物说明；本文档不作为运行时提示词，也不复制 Environment 或 Pipeline 的完整合同。

**相关边界**

- 数据下载、源单位和 raw 审计见 [数据文档](data_documentation.md)。
- PIT 数据、Sandbox、工具、Broker、NL 和回测见 [Environment 设计](environment_design.md)。
- Step、Fold、Epoch、Held-out、冻结和实验账本见 [Pipeline 设计](pipeline_design.md)。
- 控制台和 QMT 实盘边界见 [部署文档](deployment_documentation.md)。
- 参数默认值速查见 [参数参考](parameters_reference.md)。

**职责边界**

Agent 负责在单个 Fold 内读取允许的研究输入，探索和验证候选策略，并写出正式策略与可继承模型参数。Agent 不负责准备数据、判定回放与 Broker 结果真相、冻结或编排实验、记录系统审计，也不能访问测试、held-out、真实凭据或实盘券商。

**术语说明**

| 中文名 | 代码/英文名 | 含义 |
|---|---|---|
| 智能体 | `Agent` | 在一个 Fold 内读取 Sandbox 数据、编写策略代码、调用受控工具并输出策略产物的模型驱动执行者 |
| 探索偏好 | `Taste` | Epoch 开始前由元学习会话生成，并注入本 Epoch Fold Agent Prompt 的高层探索偏好 |
| 研究者 Fold 指令 | `fold_directive` | HITL 运行中由研究者在单个 Fold 启动前注入的可选探索方向；应表述为待检验假设，且不放宽任何硬约束 |
| 策略产物 | `strategy_artifact` | 跨 Fold 共享的 `output/` 正式策略产物，根目录固定入口为 `main.py` |
| 模型参数产物 | `model_artifact` | 跨 Fold 共享的 `models/` 可继承模型产物，用于保存可复现的模型参数和权重 |

**导航**

- [1. 可见性、工具与结果使用](#1-可见性工具与结果使用)
- [2. Fold 工作流](#2-fold-工作流)
- [3. 正式策略产物与策略组织](#3-正式策略产物与策略组织)
- [4. 禁止行为与提交前自检](#4-禁止行为与提交前自检)

## 1. 可见性、工具与结果使用

本章说明 Agent 可见的数据和结果，以及使用工具、网络、NL、Broker 和回测的原则。

Agent 遵循以下使用原则：

- 正式策略代码只能依赖当前 `ctx`、`/mnt/snapshot`、`output` 自身和 `/mnt/agent/models`；不得硬编码研究槽、结果槽、宿主路径或测试区间。
- `workspace/` 是临时探索区，不冻结、不回放、不复制到下一 Fold；`output/` 是正式策略代码来源；`models/` 是可选正式模型参数来源。
- 正式回放在一次性隔离容器中执行，看不到开发 `workspace`、阶段槽或结果目录；短窗口 Probe 的 `ctx.nl()` 只返回 `withheld_probe`，因此 `runtime_representative=false` 时墙钟不能外推完整 Valid，但 `nl_cost` 的完整窗口逻辑调用投影和 provider 结构上界可用于成本预检；拒单反馈只含未提交原因和粗粒度策略类别，不含市场/资格信息、收益或成交；完整 Valid 保留完整审计。
- 数据域用途、字段、单位、可见时间、窗口覆盖和路径权限，以本次运行注入的事实摘要与清单为准；Environment 文档解释这些事实的稳定语义。
- 工具通过原生 function calling 调用；不要在正文里手写 JSON 动作。先用 `grep/glob/read` 做只读定位，再用受控写工具或 Shell 修改正式产物。
- 大表先看 Parquet metadata，再用 DuckDB、pyarrow 或 pandas 按列/日期过滤读取；不要在未知规模时直接全量 `pd.read_parquet()`。
- `ctx.asof_dir`、`ctx.snapshot_dir`、`ctx.model_dir` 和 `ctx.state_dir` 是路径字符串，先用 `Path(str(...))` 转换再拼接；Timeview 是 parts 目录：Pandas 直接读目录，DuckDB 使用 `目录/*.parquet`；空 glob 表示该时点没有可见行。
- 普通 Fold 不直接调用外部网络、LLM provider、真实券商或安装新包；稳定新依赖由元学习声明并交 Pipeline 构建派生镜像，或把最小可审计源码整理进 `output`。
- 验证结果、Broker 事件、拒单统计、NL 日志、Step 树和 Barra-lite 归因可用于 development 复盘；测试和 held-out 结果始终不能反馈给 Agent。
- 每次正式回测前都必须通过修改检查；`finish_fold` 只表示 Agent 停止本 Fold 修改，是否冻结仍由 Pipeline 复核。
- 策略迭代先验证“读取→选股→下单→T+1 主动退出”的最小垂直链路，再逐个加入主要组件；回测观察返回已用、上限和剩余次数。零订单且存在吞宽泛异常或盲竞价分支读取 `ctx.price()` 时会给关联诊断，但不阻止回测或冻结。

## 2. Fold 工作流

本章定义单个 Fold 内从读取输入、探索候选到完成提交的 Agent 工作流。

一个 Fold 内可以有多个 Step。Step 是同一 Agent 会话中一次有记录的候选验证迭代；两次 Step 之间可以穿插任意数量的只读探查、修改和调试工具调用。进入下一 Step 不会重启 Agent，也不会创建新的对话上下文。

修改检查和回测会返回非阻断 advisory：未投影的 Parquet 读取、未重新抛出的宽异常、formal Agent 峰值内存，多次 `main(ctx)` 后零 action/零订单，以及订单流与此前某次完整验证完全一致（`behaviorally_identical_validation`——自该版本以来的修改在交易上无效，应复盘或换假设，而不是再付一次完整验证）。它们只帮助 Agent 定位策略问题，不替代 Agent 决策，也不改变 `allowed_to_backtest`、完整验证或冻结资格。

**初始 Step 建议**

1. 读取研究输入窗口、父产物、数据 manifest 和可见文本样本。
2. 在 `workspace/` 中做基础数据探查。
3. 建立少量候选筛选逻辑、交易策略和可选 NL prompt。
4. 写入正式 `output/`。
5. 调用修改检查和验证回测。

**常规 Step 建议**

1. 读取当前 `output/`、父产物和历史 `results/valid_<idx>/`。
2. 复盘收益、拒单、持仓集中度、long/short 拆分、回撤、换手、费用、NL 证据和风格归因。
3. 修改候选筛选、交易策略、NL prompt、模型参数或执行节奏。
4. 将当前版本写入 `output/` / `models/`。
5. 调用 `modification_check` 和 `backtest`。
6. 若继续改进有明确假设，则进入下一 Step；若判断当前方向不如某个历史节点（对比 `steps/<node_id>/` 的源代码与验证明细后），用 `step_rollback` 恢复该节点再分支探索；否则调用 `finish_fold`。

**收敛原则**

- 策略探索阶段允许围绕明确假设自由试验。
- 收敛阶段先保持已验证表现与可执行性，再减少不必要的代码、文件和复杂度；这是启发式，不是独立验收排序。
- 临近截止时间时，保留当前最好且已完整验证的产物，尽快完成检查、验证或结束 Fold；当前改动未通过验证时，可用 `step_rollback` 恢复到本 Fold 已验证节点再结束。

**会话稳态防护**（宿主侧，不增加 Agent 交互面）：backtest 观察随 `backtests_*` 一并回显 `steps_used/steps_limit/steps_remaining`（正式 Step 预算）；最后一个预算 Step 的完整验证完成后注入 Step 收尾提示（恢复最佳已验证版本并 `finish_fold`，再次回测才会终止会话）；主对话 provider 调用按剩余截止时间钳制内部重试；连续 3 次 provider 失败触发熔断并以 `llm_unavailable` 结束会话（避免故障提供方烧光调用预算）；provider 返回上下文超长错误时强制执行一次上下文压缩（绕过 token 估计阈值，保留熔断与结构性保护）。`step_rollback` 仅在启用 Step 树的运行中注册为工具。

## 3. 正式策略产物与策略组织

本章定义正式策略和模型参数产物的入口、结构和组织原则。

`output/main.py` 是唯一必需正式入口，Environment 按回放 tick 逐 tick 调用：

```python
def main(ctx) -> None:
    ...
```

**产物原则**

- `output/` 保存正式策略代码和轻量文本配置；可按功能拆分 helper 文件或子包。
- `models/` 只保存需要跨 Fold 继承的可复现模型参数、权重和轻量模型元数据；依赖包不写入 `models/`。
- 禁止提交缓存、日志、数据 dump、notebook、密钥、隐藏文件/目录和无调用路径的废弃模型或代码。
- 需要跨 Fold 继承的模型参数必须在 `backtest` 前由工具阶段写入 `models/`；正式回放中 `ctx.model_dir` 只读。

**`main(ctx)` 组织原则**

- `main(ctx)` 每个决策 tick 被调用一次，一次覆盖全市场；非决策 Bar 上市场和 Broker 仍继续推进。策略自己决定何时筛选、调仓、撤单和调用 NL。
- 所有实质步骤都包进 `with ctx.substep(name, budget_minutes=B):`，包括状态读写、持仓/在途管理、横截面筛选、模型推理、NL、批量下单计划、broker action、撤单扫描等。
- 普通 off-session tick 只做研究、状态更新和计划交接；报单/撤单只在 Environment 定义的可报单 tick 内由策略显式触发。
- 重操作只在少数选定时点执行；冻结 `snapshot_dir` 特征每次回放只计算一次。`ctx.asof_version` 是含分钟域的全局版本，滚动日线/事件特征应在固定研究时点按实际日期或策略 key 缓存，避免无关分钟更新触发重算。
- 重复处理大 PIT 表时，投影所需列并按标的先截取能精确覆盖最长因子窗口的尾部，再计算横截面和合并；以全历史实现核对因子、排名、候选和订单等价（浮点容差不高于 `1e-12`），不用采样或近似值换速度。
- 跨 tick 暂存写 `ctx.state_dir`（单个文件不超过64 MiB）；它在 substep 内首次访问时才复制可见状态，纯 Broker block 不创建副本，访问后的延迟可见性不变。Broker 是现金、持仓、负债和在途订单的真相源，`state_dir` 只保存策略自己的目标、计划和轻量状态。
- `ctx.account`、`ctx.positions`、`ctx.broker.stock/credit` 是 tick 入口快照；同 tick action 只进入 action 队列（已提交的轻量单也进入 `pending()`），不会回写这些视图。批量 sizing 应读取一次总预算、本地逐笔递减并预留费用/滑点；Broker 不接受 `weight`，也不会替策略压量或取整。
- `ctx.positions` 行 schema 是显式合同（系统提示词与模板 README 均逐键列出）：`account`/`ts_code`/`side`/`quantity`/`sellable_quantity`/`entry_price`/`entry_date`/`entry_cost`/`last_price`/`market_value`。不存在 `qty`/`volume`/`cost_basis`/`avg_price`；`row.get()` 带默认值会静默吞掉键名错误并杀死退出路径（lap-test19/lzp-test18 案例研究的核心缺陷）。modification_check 对 `.positions` 行上的未知常量键返回 `unknown_position_row_key` advisory（warn-only）。
- Broker 在同一 Bar 内按 FIFO 撮合；已成交/拒绝订单立即释放占用，只有更早仍挂单的订单继续冻结。
- 正式策略解释器固定 hash seed 以保证未排序容器跨进程可复现；涉及选股优先级时仍应显式 `sorted(...)`，不要把集合迭代顺序当信号。
- 当复杂度确有需要时，把横截面候选生成与逐标的持仓、下单和撤单管理拆成小模块；简单策略不为拆分而拆分。

**NL 使用原则**

- `ctx.nl()` 只是决策阶段证据工具，不是交易真相源；现金、可交易性、成本和风控约束仍以 Broker 为准。
- NL 默认返回自由文本和证据记录；窄标签任务应声明 `response_format={"type":"enum","values":[...]}` 并直接使用规范值，避免子串解析。通用自由文本仍需处理解析失败。
- 单股 NL 调用应声明可证伪的 `event_filter={"patterns":[...],"lookback_days":N}`，由候选变化或窗口内文本事件驱动，只放在少数选定时点且必须在 `ctx.substep` 内；不要每 tick 调用。`state="no_matching_evidence"` 是成功的证据缺席状态，策略据此走数值退化路径。宿主按匹配证据 revision 复用仍有效的结果，并在事件进入或移出窗口时失效。
- 改写 `output` 后若希望影响当前 Step，必须重新通过修改检查并重新回测。

## 4. 禁止行为与提交前自检

本章列出 Agent 的禁止行为和结束 Fold 前必须完成的自检。

**禁止行为**

- 读取测试或 held-out 数据，或把测试/held-out 结果、日志、NL 明细和 Broker 事件反馈给策略探索或元学习。
- 在正式策略中引用 `/mnt/agent/workspace`、`/mnt/snapshots/`、`/mnt/artifacts`、`/mnt/runtime`、主仓库路径、宿主绝对路径或测试区间。
- 直接调用外部网络、LLM API、真实券商或未授权凭据。
- 写入成交、持仓、现金、收益、Broker 事件或实验账本。
- 用当前验证/测试收益硬编码具体股票、日期、题材或行情事件。
- 修改只读 README、父产物、结果目录、Step 树或测试数据槽。

**提交前自检**

- `output/main.py` 存在并定义 `main(ctx)`；正式 helper 都在 `output/` 树内。
- 模型参数只放在 `models/`，且当前模型 hash 已通过最近一次修改检查。
- 当前 `output`/`models` 是准备提交的最好已验证版本；若不是，先恢复最佳 Step 并重新完成检查与验证。
- 最近一次验证回测成功，当前 `output` hash 和 `models` hash 未变。
- 没有缓存、日志、数据 dump、密钥、notebook、隐藏文件/目录、死代码路径或被放弃研究方向的残留产物。
