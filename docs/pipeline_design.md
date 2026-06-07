# Pipeline 设计

本文档记录训练、测试和 Held-out 的运行顺序。Pipeline 负责按时间顺序调度 Data、Environment 和 Agent，冻结每个阶段的输入输出，并写账本。

相关边界：

- Agent 行为、状态和输出格式见 `docs/agent_design.md`。
- PIT 窗口、Sandbox、Shell 和 Tool 见 `docs/environment_design.md`。
- raw 数据下载和审计见 `docs/data_documentation.md`。
- QMT 实盘流程见 `docs/QMT_documentation.md`。

## 术语说明

| 术语 | 含义 |
|---|---|
| Pipeline | 调度 Data、Environment 和 Agent 的外层程序，不实现投资逻辑 |
| Step | 一个 Fold 内的一次策略修改和验证尝试 |
| Fold | 一个验证区间加后续测试季度 |
| Epoch | 从起始 Fold 到结束 Fold 跑完一遍 |
| Held-out | 所有训练完成后才运行的冻结测试区间 |
| Development | 用于滚动验证和测试的研究区间，不等于最终测试 |
| `strategy_artifact` | Agent 写出的 `agent_output/factor/` 因子逻辑和 `agent_output/nl_prior/` 投资先验 |
| `snapshot_manifest` | 记录本次可见数据窗口、hash、单位和时间覆盖的说明文件 |
| ledger | 记录 Step、Fold、Epoch、Held-out 结果和审计信息的文件 |

## 目录

- [1. 核心循环](#1-核心循环)
- [2. Fold 时间定义](#2-fold-时间定义)
- [3. Step 流程](#3-step-流程)
- [4. Fold 流程](#4-fold-流程)
- [5. Epoch 流程](#5-epoch-流程)
- [6. 测试和 Held-out](#6-测试和-held-out)
- [7. 账本和日志](#7-账本和日志)
- [8. 失败条件](#8-失败条件)
- [9. 验收清单](#9-验收清单)

## 1. 核心循环

Pipeline 使用三层循环：

| 层级 | 含义 | 是否允许修改策略产物 |
|---|---|---|
| Step | 一个 Fold 内的一次尝试 | 允许，在修改约束内 |
| Fold | 一个滚动验证区间和下一测试季度 | 验证期允许；测试期禁止 |
| Epoch | 从起始季度到结束季度跑完所有 Fold | Epoch 结束后只允许正则化 |

主路径：

```text
初始化策略产物
  -> Epoch 1
    -> Fold 2022Q1
      -> 21 个月可见窗口
      -> Step 1..N 在 2021-10..2021-12 验证区间上迭代
      -> 冻结本 Fold 策略产物
      -> 在 2022Q1 测试季度上评估
    -> Fold 2022Q2
    -> ...
    -> Fold 2025Q4
    -> 正则化 LLM 审计并压缩经验
  -> Epoch 2..M
  -> 固定最终策略产物
  -> Held-out 分 Fold 测试
```

Pipeline 不实现投资逻辑，也不改写 Agent 代码。它只做调度、冻结、校验和记录。

## 2. Fold 时间定义

### 2.1 滚动 Fold

首个 Fold 使用 2021-10 到 2021-12 作为启动验证区间，随后按自然季度滚动。以 `fold_2022Q1` 为例：

| 项目 | 示例 |
|---|---|
| 输入窗口 | 2020-01 到 2021-09 |
| 验证区间 | 2021-10 到 2021-12 |
| 测试季度 | 2022-01 到 2022-03 |
| 验证决策时点 | 2021 年 10 月第一个交易日开盘前 |
| 验证可见数据 | 最近 21 个月内、截至验证决策时点已可见的数据 |
| 测试决策时点 | 2022Q1 第一个交易日开盘前 |
| 测试可见数据 | 最近 21 个月内、截至测试决策时点已可见的数据 |

含义：

- Agent 在验证决策时点只能看到验证季度开始前的数据。
- Agent 在验证期回测 2021-10 到 2021-12，并可在 Step 内修改代码和经验。
- 验证结束后冻结本 Fold 策略产物。
- 测试时用冻结策略产物回测 2022Q1，不允许再改代码或经验。

### 2.2 滚动方式

下一个 Fold 向后移动一个季度；上一 Fold 的测试季度会成为下一 Fold 的验证区间：

| Fold | 输入窗口 | 验证区间 | 测试季度 |
|---|---|---|---|
| `fold_2022Q1` | 2020-01 到 2021-09 | 2021-10 到 2021-12 | 2022Q1 |
| `fold_2022Q2` | 2020-01 到 2021-12 | 2022Q1 | 2022Q2 |
| `fold_2022Q3` | 2020-04 到 2022-03 | 2022Q2 | 2022Q3 |

下一个 Fold 只继承上一个 Fold 在测试前已经冻结的策略产物，也就是 `agent_output/factor/` 和 `agent_output/nl_prior/`。上一 Fold 的 Agent 对话历史、Shell/LLM/服务调用明细、测试季度收益、测试明细和复盘长文不能进入下一 Fold prompt 或策略产物。

传递步骤必须显式记录：

1. 上一个 Fold 在验证期结束时写入 `fold_ledger.frozen_strategy_artifact_id`。
2. Pipeline 用 `experiment_id`、`epoch_id` 和该 ID 读取 `experiments/<experiment_id>/strategy_artifacts/<epoch_id>/<frozen_strategy_artifact_id>/manifest.json`。
3. Pipeline 校验策略产物聚合版本、父产物 ID 和冻结标记。
4. 新 Fold 启动时，Pipeline 把冻结产物直接复制到新 Sandbox 的 `/mnt/artifacts/agent_output/factor/` 和 `/mnt/artifacts/agent_output/nl_prior/`；同时创建新的 `conversation_id`。
5. 新 Fold 的 Agent 只能看到复制后的因子逻辑和投资先验，不能看到上一 Fold 的对话、调用日志、测试收益或复盘文本。

如果某个自然季度在后续 Fold 中成为验证季度，Pipeline 可以把该季度重新作为 raw/PIT 回放区间使用；但不能把它在上一 Fold 中作为测试季度时产生的收益摘要、复盘结论或测试 ledger 直接喂给 Agent。

每个 Fold 必须创建新的 `conversation_id` 和 Agent session。Pipeline 不得复用上一 Fold 的对话上下文。

### 2.3 数据窗口

Environment 为每个决策时点准备固定最大长度窗口：主数据域默认最近 21 个月，分钟线默认最近 5 个交易日。这是 Sandbox 可见上限；Agent 可以少用，但不能请求超出窗口的数据。

Pipeline 必须记录：

- `decision_time`
- `input_window`
- `validation_period`
- `test_period`
- `snapshot_id`
- `snapshot_manifest_hash`
- `strategy_artifact_id`

## 3. Step 流程

Step 是验证期的一次策略修改和验证。每个 Fold 的 Step 数固定上限，例如 3-5 次。

### 3.1 Step 输入

| 输入 | 说明 |
|---|---|
| `strategy_artifact` | 上一 Step 或上一 Fold 冻结的 `agent_output/factor/` 和 `agent_output/nl_prior/` |
| `validation_snapshot` | 当前验证决策时点的 PIT 数据窗口 |
| `modification_constraints` | 本 Step / Fold 对 `agent_output/factor/` 和 `agent_output/nl_prior/` 的修改约束 |
| `fold_time_limit` | Fold 运行时长约束，例如 `max_fold_minutes=20`；Step 不设单独时长 |
| `execution_policy` | 允许调用的 Shell/Tool 和调用上限；训练/验证期可启用受控 `sandbox_shell_tool`，测试和 held-out 默认关闭 |
| `anti_overfit_prompt` | 防止记忆特定月份、题材或股票 |

`strategy_artifact` 在 Sandbox 中直接展开到当前运行产物目录：

```text
/mnt/artifacts/
  strategy_artifact_manifest.json
  workspace/
  agent_output/
    factor/
      main.py
      factors.json
    nl_prior/
      prior.md
      prior.json
  results/
```

训练/验证 Step 中，Agent 在 `/mnt/artifacts/workspace/` 写临时代码和调试脚本；确认无误后才把正式策略代码和投资先验写入 `/mnt/artifacts/agent_output/factor/` 和 `/mnt/artifacts/agent_output/nl_prior/`。接受该 Step 时，Pipeline 只冻结 `agent_output/factor/` 和 `agent_output/nl_prior/` 为新的 `strategy_artifact`，不冻结 `workspace/`，也不冻结 `results/`。测试和 held-out 只运行冻结产物，运行后必须校验因子和投资先验 hash 未变化。

首次没有父策略产物时，Pipeline 要求 Environment 使用 `configs/agent_output_template/` 初始化 `agent_output/factor/main.py`、`agent_output/factor/factors.json`、`agent_output/nl_prior/prior.md` 和 `agent_output/nl_prior/prior.json`。之后的新 Fold 只复制上一 Fold 冻结后的同名目录，不复制上一 Fold 的对话历史或 `workspace/`。

除第一次创建策略产物外，Pipeline 进入正式 `backtest_tool` 前必须调用 Environment 执行策略修改约束检查：

1. 读取父产物 `parent_strategy_artifact_id`。
2. 调度 Environment 执行 `modification_check_tool`，比较父产物和当前 `/mnt/artifacts/agent_output/factor/`、`/mnt/artifacts/agent_output/nl_prior/`；Tool 调用不接受 Agent 传入路径、父产物或约束参数，这些上下文来自 run manifest；`workspace/` 和 `results/` 不参与 diff。
3. 对 `agent_output/factor/` 统计变更文件数、diff 行数，并通过 `factors.json` 统计新增、删除和修改的登记因子。
4. 对 `agent_output/nl_prior/` 统计新增、删除、改写规则数，并检查总条数和单条字符数。
5. 写入 `strategy_artifact_diff.json`，包含父 hash、当前 hash、约束、实际修改量和是否允许正式回测。
6. 只有 `strategy_artifact_diff.allowed_to_backtest == true` 时，Environment 才接受运行包含自然语言分析步骤的 `backtest_tool`。
7. 如果返回 false，本 Step 不获得回测结果，Agent 必须继续修改工作副本并重新检查。
8. 只有通过修改约束且验证结果被接受时，Pipeline 才能冻结为新的 `strategy_artifact`。

`allowed_to_backtest` 是 Environment 按 Pipeline 下发约束计算出的继续验证门禁结果。该结果来自确定性计数检查。Pipeline 不重新解释修改约束，只记录该结果，并在验证结果合格后决定是否冻结产物。

第一次创建策略产物时没有父产物，使用 `is_initial_artifact=true` 的初始化约束；从第二个 Fold 开始必须使用父产物 diff，不允许把整个目录当成新产物绕过修改约束。

运行时长由 Pipeline 统一下发，例如：

```json
{
  "max_fold_minutes": 20,
  "fold_deadline_at": "2026-06-07T22:20:00+08:00",
  "finalize_before_deadline_seconds": 300,
  "per_call_timeout_seconds": 300
}
```

每个 Fold 默认限时 20 分钟，Step 共享同一个 Fold deadline，不再单独计时。距离 deadline 5 分钟以上时，Pipeline、Runner 和 Proxy 不提示剩余时间，让 Agent 自由探索。剩余时间低于 `finalize_before_deadline_seconds` 时，Runner/Proxy 触发固定收尾提示，要求 Agent 立即输出当前最好版本的 `agent_output/factor/` 和 `agent_output/nl_prior/`。超过 `fold_deadline_at` 后，Pipeline 必须截断当前 Fold，停止新的 Shell/服务调用和 LLM 调用；已经卡住的 provider 请求只能超时取消，不能被追加 prompt。

收尾结果可以作为 rejected 或 best-effort Step 输出，但不能绕过策略修改约束、订单计划校验或回测规则。若收尾产物不完整，Pipeline 应记录 timeout，并使用最后一个已通过约束和回测的策略产物作为本 Fold 的候选结果。

### 3.2 Step 执行

Pipeline 调度：

1. 启动验证 Sandbox。
2. 挂载验证 snapshot。
3. 把策略产物复制到 Sandbox。
4. Agent 在 `workspace/` 写和调试 Python 策略代码，确认后把正式入口写入 `agent_output/factor/`，把投资先验写入 `agent_output/nl_prior/`。
5. Agent 可以用 Shell/Python 调试策略代码，但临时结果不作为正式回测结果。
6. Agent 可以调用 `modification_check_tool` 自查；该调用只触发当前工作副本检查，不传业务参数。Pipeline 在正式 `backtest_tool` 前必须再次调度同一 Tool 复查。若 `allowed_to_backtest=false`，Environment 拒绝继续运行，Agent 需要缩小修改后重试。
7. Agent 调用 `backtest_tool`。该 Tool 自动加载 `agent_output/factor/` 和 `agent_output/nl_prior/`，调用 `agent_output/factor/main.py::generate_orders(context)`，运行内部自然语言分析步骤，校验订单计划，再通过模拟 Broker 回测验证季度。
8. `backtest_tool` 创建新的 `results/<phase>_<idx>/`，例如 `results/valid_000/`，写入 `summary.json`、`detailed_return.json`、`order_plan.parquet` 和 `nl_output/`。Agent 在训练/验证期只读这些结果；测试和 held-out 结果不反馈给 Agent。
9. Agent 根据这些结果决定是否接受本 Step 修改。
10. Pipeline 校验 Environment 自动生成的 artifact 和日志，写 Step ledger。

自然语言分析步骤可以在每个训练 Step 的 `backtest_tool` 内部运行。它产生的 `results/<phase>_<idx>/nl_output/` 可以用于当前 Step 的订单计划生成，也可以作为下一 Step 改写 `nl_prior` 的证据。若 Agent 在自然语言分析之后又改写 `nl_prior` 并希望该修改影响当前 Step，则 Pipeline 必须重新运行 `modification_check_tool` 和 `backtest_tool`，并要求新的 `nl_prior`、`results/<phase>_<idx>/nl_output/`、策略主函数返回值、订单计划和回测结果在 manifest 中保持一致。

### 3.3 Step 输出

Step 至少输出：

- 新的策略产物或 rejected 状态。
- 策略产物 ID 和聚合版本。
- 策略主函数返回值、订单计划和校验结果。
- 验证回测结果。
- Environment 自动生成的 Shell/LLM/服务调用记录。
- `sandbox_shell_tool` transcript 路径。
- Environment 自动生成的 LLM conversation log。
- `accepted` / `rejected`。
- 接受或拒绝原因。
- 修改 diff 摘要。
- 使用的 provider、model、prompt ID、随机种子、deadline 和资源护栏。
- `fold_deadline_at`、实际耗时、是否触发收尾、是否超时。
- Sandbox 镜像版本。
- snapshot 时间范围。
- 文本 evidence id 列表。

## 4. Fold 流程

### 4.1 验证期

一个 Fold 内按顺序运行 Step。只有验证期可以修改策略产物。

选择本 Fold 最终策略产物时，Pipeline 可以使用验证回测结果和风险约束，但不能使用测试季度结果。

验证期选择规则：

- 验证收益为正。
- 最大回撤不超过阈值。
- 持仓数量和集中度合规。
- 修改次数没有超过约束。
- 经验条数没有超过上限。

### 4.2 Fold 内早停目标

早停只能使用验证期结果，不能使用测试季度或 Held-out。Pipeline 应使用统一的 `validation_score`，而不是只看收益率。评分至少包含：

- 验证收益。
- 最大回撤惩罚。
- 换手率和交易成本惩罚。
- 持仓集中度惩罚。
- 订单计划拒单或约束失败惩罚。

第一个 Epoch 没有上一轮可比结果，每个 Fold 的早停门槛为：

```text
validation_return > 0
and validation_score > 0
and risk_constraints_passed == true
and order_plan_valid == true
```

从第二个 Epoch 开始，每个 Fold 以“好于上一 Epoch 的同一 Fold”为主要目标：

```text
validation_score >= previous_epoch_same_fold.validation_score + min_delta
and risk_constraints_passed == true
and order_plan_valid == true
```

如果上一 Epoch 的同一 Fold 表现很差，目标不能退化成“亏得更少就通过”。可以使用下限：

```text
target_score = max(previous_epoch_same_fold.validation_score + min_delta, baseline_score_floor)
```

`baseline_score_floor` 默认不低于 0。达到早停目标后，Pipeline 可以停止本 Fold 后续 Step，冻结当前最优策略产物并进入测试期。早停只是节省验证期搜索，不代表策略通过最终测试。

### 4.3 测试期

验证期结束后，Pipeline 冻结：

- 策略产物。
- 策略产物 ID 和聚合版本。
- 回测配置。
- 模拟 Broker 配置。
- 入口策略。
- snapshot manifest。

测试期只执行冻结策略产物，可以在同一个 Sandbox 中完成，但测试 snapshot 必须对 Agent 用户不可读，只允许 Runner/root 读取。

1. Pipeline 冻结 `agent_output/factor/`、`agent_output/nl_prior/`、回测配置、prompt 和入口策略。
2. 关闭 Agent 的可写探索阶段；`sandbox_shell_tool` 不再能读取测试 snapshot。
3. Runner/root 读取 root-only 测试 snapshot。
4. Runner/root 调用 `backtest_tool`，自动加载冻结 `agent_output/factor/` 和 `agent_output/nl_prior/`。
5. `backtest_tool` 内部调用策略主函数、运行自然语言分析步骤、订单计划校验和模拟 Broker 回放。
6. 写测试结果并结束本 Fold。

测试结果只作为滚动开发表现记录。不能修改本 Fold，也不能进入后续 Fold prompt、策略产物或 Epoch 正则化输入。

### 4.4 Fold 输出

```json
{
  "fold_id": "fold_2022Q1",
  "input_window": "2020-01..2021-09",
  "validation_period": "2021-10..2021-12",
  "test_period": "2022Q1",
  "parent_strategy_artifact_id": "strategy_epoch001_fold2021Q4",
  "frozen_strategy_artifact_id": "strategy_epoch001_fold2022Q1",
  "frozen_strategy_artifact_path": "experiments/exp_quarterly_single_agent_001/strategy_artifacts/epoch_001/strategy_epoch001_fold2022Q1",
  "validation_result": {
    "return": 0.031,
    "max_drawdown": 0.042
  },
  "test_result": {
    "return": 0.018,
    "max_drawdown": 0.035
  },
  "state_changed_during_test": false
}
```

## 5. Epoch 流程

### 5.1 Epoch 范围

一个 Epoch 从 `fold_2022Q1` 跑到 `fold_2025Q4`。

```text
epoch_001:
  fold_2022Q1
  fold_2022Q2
  ...
  fold_2025Q4
```

每个 Fold 使用上一个 Fold 冻结后的策略产物作为起点；这个产物不包含上一 Fold 的测试结果或对话历史。

### 5.2 Epoch 后正则化

Epoch 结束后，启动正则化 LLM。它读取：

- 本 Epoch 所有 Fold 的验证 Step 结果。
- 策略产物变化记录。
- `agent_output/factor/`。
- `agent_output/nl_prior/`。

正则化 LLM 输出：

- 保留的经验。
- 删除的经验和原因。
- 合并后的规则。
- 需要保留的因子逻辑。
- 正则化后的策略产物。

它不能读取 Fold 测试结果或 held-out，不能新增基于某段表现的交易规则，只能删除、合并和抽象化已有经验。

正则化输入必须由白名单 manifest 指定，只允许包含验证 Step 结果、策略产物变化记录、`agent_output/factor/` 和 `agent_output/nl_prior/`。不能把完整 `fold_ledger` 或测试收益汇总直接喂给正则化 LLM。

### 5.3 多 Epoch

可以重复多个 Epoch。每个 Epoch 从上一个 Epoch 正则化后的策略产物开始。

多 Epoch 的目的不是记住更多历史细节，而是压缩出更稳定的因子和经验。Pipeline 必须限制：

- `nl_prior` 总条数。
- `factor` 复杂度。
- 每个 Fold 的修改数量。

## 6. 测试和 Held-out

### 6.1 Development 表现

Development 期间，每个 Fold 的测试季度表现汇总为滚动开发表现。

Development 表现用于判断系统是否能在滚动验证中逐步改善，但不能代表最终泛化表现。

### 6.2 Held-out

Held-out 在所有 Epoch 完成后运行。Held-out 起止日期必须在实验开始前写入配置并冻结，不能根据验证或 development 结果选择；它不得与 `fold_2022Q1` 到 `fold_2025Q4` 的 development 区间重叠。

规则：

- 使用最终冻结策略产物。
- 按季度分 Fold 测试。
- 不运行 Step。
- 不修改 `agent_output/factor/`。
- 不修改 `agent_output/nl_prior/`。
- 不让 held-out 结果进入验证或正则化。

Held-out 输出是最终测试集表现。

## 7. 账本和日志

本章是实验级日志和账本的唯一权威定义。Agent 文档只说明行为边界；Environment 文档只说明 Sandbox 实际写哪些运行时文件。

### 7.1 账本类型

| 账本 | 内容 |
|---|---|
| `step_ledger` | 每个 Step 的代码、经验、调用和验证回测结果 |
| `fold_ledger` | 每个 Fold 的冻结策略产物、验证结果和测试结果 |
| `epoch_ledger` | 每个 Epoch 的训练过程摘要和正则化结果 |
| `heldout_ledger` | 最终 held-out 分 Fold 结果 |
| `conversation_trace` | 同一 Agent session 下的 `execution_calls.jsonl` 和 `llm_conversations.jsonl`；前者记录 Shell/Tool/回测摘要，后者记录真实 LLM provider messages 和响应 |

### 7.2 版本与完整性记录

Experiment ID 是索引，不是完整性校验。Pipeline 只在关键边界记录聚合版本或 hash，不为每条 Shell/Python 调用生成细粒度 hash。

必须记录：

- `experiment_id`、`epoch_id`、`fold_id`、`step_id`、`run_id` 和 `conversation_id`。
- 冻结策略产物 ID 和聚合 hash。
- snapshot manifest ID 和可选聚合 hash。
- 回测结果文件和可选聚合 hash。
- execution call log ID 和 LLM conversation log ID；二者必须通过同一个 `conversation_id` 和调用 ID 串联。
- Sandbox 镜像版本。
- provider、model、prompt ID、随机种子。
- deadline、资源护栏和入口策略版本。
- 文本 evidence id。

Shell/Python 调用只需记录命令、exit code、stdout/stderr、transcript 路径、脚本路径和产物路径。若后续发现复现实验困难，再升级到更细的调用级 hash。

### 7.3 输出路径

建议路径：

```text
experiments/
  <experiment_id>/
    ledgers/
      step_ledger.jsonl
      fold_ledger.jsonl
      epoch_ledger.jsonl
      heldout_ledger.jsonl
    strategy_artifacts/
      <epoch_id>/
        <strategy_artifact_id>/
    artifacts/
      <run_id>/
    reports/

logs/
  llm_conversations/
  sandbox/
```

这些路径默认不提交 Git。重要结论写入 `LOGBOOK.md` 和 `docs/logbook/DETAILED_LOGBOOK.md`。

## 8. 失败条件

以下情况必须失败：

- snapshot 缺失或关键 manifest 不匹配。
- Sandbox 访问了禁止路径。
- Agent 读取了未来数据。
- Python 代码运行失败但返回成功。
- debug shell 调用没有 Environment 日志。
- Environment 没有为 LLM 调用生成 conversation log。
- 文本分析没有 evidence 引用。
- 回测输入 schema 不合法。
- 测试或 held-out 修改了策略产物。
- Fold 修改次数超过约束。
- 正则化 LLM 读取 Fold 测试结果或 held-out。

## 9. 验收清单

Pipeline 相关改动至少检查：

- Step / Fold / Epoch 时间定义是否清楚。
- 验证决策时点和测试决策时点是否没有未来数据。
- 每个 Fold 是否只有验证期能修改策略产物。
- 测试期是否只执行冻结策略产物。
- Held-out 是否完全冻结。
- 每次 LLM 调用是否由 Environment 自动生成 conversation log。
- 每个关键 artifact 是否有 manifest；冻结策略产物和回测结果是否有聚合版本记录。
- 正则化是否只压缩验证得到的经验，且不读取 Fold 测试结果或 held-out。
