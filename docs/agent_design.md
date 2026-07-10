# Agent 设计

本文档记录 Fold Agent 的设计边界和维护口径。Agent 运行时直接读取系统 Prompt 和策略产物说明；本文档不作为运行时提示词，也不复制 Environment 或 Pipeline 的完整合同。

**相关边界**

- 数据下载、源单位和 raw 审计见 [数据文档](data_documentation.md)。
- PIT 数据、Sandbox、工具、Broker、NL 和回测见 [Environment 设计](environment_design.md)。
- Step、Fold、Epoch、Held-out、冻结和实验账本见 [Pipeline 设计](pipeline_design.md)。
- 控制台和 QMT 实盘边界见 [部署文档](deployment_documentation.md)。
- 参数默认值速查见 [参数参考](parameters_reference.md)。

**导航**

- [1. 职责与隔离](#1-职责与隔离)
- [2. 可见性、工具与结果使用](#2-可见性工具与结果使用)
- [3. Fold 工作流](#3-fold-工作流)
- [4. 正式策略产物与策略组织](#4-正式策略产物与策略组织)
- [5. 禁止行为与提交前自检](#5-禁止行为与提交前自检)

**Agent 专有术语**

| 术语 | 含义 |
|---|---|
| Agent | 在一个 Fold 内读取 Sandbox 数据、写策略代码、调用受控工具并输出策略产物的模型驱动执行者 |
| Taste | Epoch 开始前元学习会话生成的探索偏好，会注入本 Epoch 的 Fold Agent Prompt |
| 研究者 Fold 指令 | HITL 运行中研究者在单个 Fold 启动前注入的可选探索方向，按待检验假设措辞，不放宽任何硬约束 |
| 策略产物 | 跨 Fold 共享的 `output/` 正式策略产物目录，根目录固定入口为 `main.py` |
| 模型参数产物 | 跨 Fold 共享的 `models/` 可继承模型产物目录，用于保存可复现模型参数和权重 |

## 1. 职责与隔离

**Agent 负责**

- 读取训练窗口、验证回放区间、父策略产物、当前 Fold 的历史验证结果和可见 Step 树。
- 在 `workspace/` 中做临时探索，在 `output/` 中写正式策略，在 `models/` 中保存需要继承的模型参数。
- 调用 `modification_check`、`backtest` 和 `finish_fold`，并根据验证反馈决定是否继续修改。
- 参考 Taste、研究者 Fold 指令、阶段指引和提交验收规则，在收益、风险、修改量、策略复杂度和剩余时间之间取舍。

**Agent 不负责**

| 事项 | 归属 |
|---|---|
| raw 数据下载、补齐、审计和 sentinel | Data 层 |
| 构造 PIT snapshot、切换 `/mnt/snapshot`、执行工具和回测 | Environment |
| 冻结测试、held-out、fallback、账本和跨 Fold 编排 | Pipeline |
| 现金、持仓、成交、负债、费用和收益真相 | Broker / Environment |
| 真实下单、连接券商或管理 QMT | QMT 流程 |
| 直接访问外部网络、LLM provider API key 或真实券商凭据 | 禁止 |

**隔离原则**

- 同一个 Fold 内多个 Step 共享一个 Agent 会话和 `conversation_id`；下一个 Fold 启动新会话。
- Agent 可以看到当前父产物和当前工作副本，但不能看到上一 Fold 的对话历史、工具日志、LLM 日志、测试结果或测试 conversation log。
- 如果某个历史区间在当前 Fold 中成为验证区间，Agent 只能读取当前 Fold 重新生成的验证结果，不能复用它曾作为测试区间时保存的结果。
- 可信日志只能由 Environment / Pipeline 记录；Agent 的解释、理由或输出不能替代工具、回测、Broker 和 LLM 日志。

## 2. 可见性、工具与结果使用

PIT、Timeview、数据槽、路径权限、工具 schema、Shell 边界、网络/代理、NL、Broker 和回测结果目录的权威合同见 [Environment 设计](environment_design.md)。Agent 文档只保留使用原则：

- 正式策略代码只能依赖当前 `ctx`、`/mnt/snapshot`、`output` 自身和 `/mnt/agent/models`；不得硬编码研究槽、结果槽、宿主路径或测试区间。
- `workspace/` 是临时探索区，不冻结、不回放、不复制到下一 Fold；`output/` 是正式策略代码来源；`models/` 是可选正式模型参数来源。
- 数据域用途、字段、单位、可见时间、窗口覆盖和路径权限，以本次运行注入的事实摘要与清单为准；Environment 文档解释这些事实的稳定语义。
- 工具通过原生 function calling 调用；不要在正文里手写 JSON 动作。先用 `grep/glob/read` 做只读定位，再用受控写工具或 Shell 修改正式产物。
- 大表先看 Parquet metadata，再用 DuckDB、pyarrow 或 pandas 按列/日期过滤读取；不要在未知规模时直接全量 `pd.read_parquet()`。
- 普通 Fold 不直接调用外部网络、LLM provider、真实券商或安装新包；稳定新依赖由元学习声明并交 Pipeline 构建派生镜像，或把最小可审计源码整理进 `output`。
- 验证结果、Broker 事件、拒单统计、NL 日志、Step 树和 Barra-lite 归因可用于 development 复盘；测试和 held-out 结果始终不能反馈给 Agent。
- 每次正式回测前都必须通过修改检查；`finish_fold` 只表示 Agent 停止本 Fold 修改，是否冻结仍由 Pipeline 复核。

## 3. Fold 工作流

一个 Fold 内可以有多个 Step。Step 是同一 Agent 会话中一次有记录的候选验证迭代；两次 Step 之间可以穿插任意数量的只读探查、修改和调试工具调用。进入下一 Step 不会重启 Agent，也不会创建新的对话上下文。

**初始 Step 建议**

1. 读取训练窗口、父产物、数据 manifest 和可见文本样本。
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
6. 若继续改进有明确假设，则进入下一 Step；否则调用 `finish_fold`。

**收敛原则**

- 探索期允许围绕明确假设自由试验。
- 收敛阶段先保持已验证表现与可执行性，再减少不必要的代码、文件和复杂度；这是启发式，不是独立验收排序。
- 临近截止时间时，保留当前最好且已完整验证的产物，尽快完成检查、验证或结束 Fold。

## 4. 正式策略产物与策略组织

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
- 重操作只在少数选定时点执行；模型、as-of 数据和特征读取应按 `ctx.asof_version` 或策略自定义 key 缓存，避免每 tick 重算。
- 跨 tick 暂存写 `ctx.state_dir`；Broker 是现金、持仓、负债和在途订单的真相源，`state_dir` 只保存策略自己的目标、计划和轻量状态。
- 仓位 sizing 由策略显式读取现金、价格、可卖量和账户约束后计算；Broker 不接受 `weight` 下单参数，也不会替策略压量或取整。
- 当复杂度确有需要时，把横截面候选生成与逐标的持仓、下单和撤单管理拆成小模块；简单策略不为拆分而拆分。

**NL 使用原则**

- `ctx.nl()` 只是决策阶段证据工具，不是交易真相源；现金、可交易性、成本和风控约束仍以 Broker 为准。
- NL 返回自由文本和证据记录；需要分数、标签或过滤条件时，策略代码必须自行解析，并能在解析失败或证据不足时降级。
- NL 调用应只放在少数选定时点，且必须在 `ctx.substep` 内；不要每 tick 调用。
- 改写 `output` 后若希望影响当前 Step，必须重新通过修改检查并重新回测。

## 5. 禁止行为与提交前自检

**禁止行为**

- 读取测试或 held-out 数据，或把测试/held-out 结果、日志、NL 明细和 Broker 事件反馈进训练。
- 在正式策略中引用 `/mnt/snapshots/`、`/mnt/artifacts`、`/mnt/runtime`、主仓库路径、宿主绝对路径或测试区间。
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
