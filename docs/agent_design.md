# Agent 设计

本文档记录 Agent 自身的工作合同：它在一个已准备好的 Sandbox 内能看到什么、能写什么、如何在一个 Fold 内迭代策略、正式产物应是什么格式，以及哪些行为禁止。Fold / Epoch 编排、策略产物冻结、测试执行和实验账本由 `docs/pipeline_design.md` 维护。

相关边界：

- 数据下载、单位和 raw 审计见 `docs/data_documentation.md`。
- PIT 窗口、Sandbox、Shell、Tool、回测和自然语言评分见 `docs/environment_design.md`。
- Step / Fold / Epoch 编排、策略产物冻结和实验账本见 `docs/pipeline_design.md`。
- 实盘部署和 QMT 流程见 `docs/QMT_documentation.md`。

## 术语说明

| 术语 | 含义 |
|---|---|
| Agent | 在一个 Fold 内读取 Sandbox 数据、写策略代码、调用受控 Shell/Tool 并输出策略产物的模型驱动执行者 |
| Sandbox | Agent 运行的隔离环境，只能读可见数据窗口，只能写本次运行产物 |
| PIT | Point-in-time，只允许使用决策时点已经可见的数据 |
| `available_at` | 某条数据最早可以被 Agent 看到的时间 |
| Step | 一个 Fold 内的一次策略修改和验证尝试 |
| 策略产物 | 跨 Fold 共享的 `factor/` 和 `nl_prior/` |
| 自然语言评分 | `backtest_tool` 基于文本库、公司上下文和投资先验对候选股票打分 |
| Held-out | 所有训练结束后才运行的冻结测试区间；Agent 不可读 |

## 导航

- [1. Agent 职责](#1-agent-职责)
- [2. Agent 的工作区](#2-agent-的工作区)
  - [2.1 可见数据](#21-可见数据)
  - [2.2 可写目录](#22-可写目录)
  - [2.3 可调用入口](#23-可调用入口)
- [3. Fold 内工作流](#3-fold-内工作流)
  - [3.1 初始 Step](#31-初始-step)
  - [3.2 常规 Step](#32-常规-step)
  - [3.3 Fold 结束](#33-fold-结束)
- [4. 正式策略产物](#4-正式策略产物)
  - [4.1 `factor/`](#41-factor)
  - [4.2 `nl_prior/`](#42-nl_prior)
  - [4.3 正式策略入口读取规则](#43-正式策略入口读取规则)
- [5. Tool 使用语义](#5-tool-使用语义)
  - [5.1 `sandbox_shell_tool`](#51-sandbox_shell_tool)
  - [5.2 `modification_check_tool`](#52-modification_check_tool)
  - [5.3 `backtest_tool`](#53-backtest_tool)
  - [5.4 `finish_fold_tool`](#54-finish_fold_tool)
- [6. 修改约束和自然语言经验](#6-修改约束和自然语言经验)
- [7. LLM 调用和日志](#7-llm-调用和日志)
- [8. 禁止行为和验收清单](#8-禁止行为和验收清单)

## 1. Agent 职责

Agent 被 Pipeline 拉起后，只在当前 Sandbox 内工作。它负责：

- 读取训练窗口和验证复盘数据。
- 在 `workspace/` 中写临时代码、做数据探查和调试。
- 把正式 Python 策略入口写入 `agent_output/factor/`。
- 把可迁移的自然语言投资经验写入 `agent_output/nl_prior/`。
- 调用修改检查 Tool，确认正式产物改动没有超过约束。
- 调用验证回测 Tool，读取验证结果，并决定是否继续小幅修改。
- 按 Pipeline 注入 Prompt 的提交验收规则判断当前产物是否足够好。
- 在当前 Fold 准备结束时调用 `finish_fold_tool`。

Agent 的核心输出只有两个目录：

```text
agent_output/factor/
agent_output/nl_prior/
```

Agent 不负责：

| 事项 | 归属文档 |
|---|---|
| Fold / Epoch 如何切分、何时启动 Agent、何时停止 | `docs/pipeline_design.md` |
| 策略产物如何冻结、复制到下一 Fold、写入实验目录 | `docs/pipeline_design.md` |
| 测试和 held-out 如何执行 | `docs/pipeline_design.md` |
| PIT 数据窗口如何构造、单位如何统一 | `docs/environment_design.md` |
| Sandbox 权限、Shell、Tool、回测和自然语言评分内部实现 | `docs/environment_design.md` |
| raw 数据下载、审计和数据风险 | `docs/data_documentation.md` |

Agent 可以通过 Tool 使用这些能力，但不能改写这些能力。Agent 会话只覆盖当前 Fold；同一个 Fold 内多个 Step 共享同一个 Agent 会话和 `conversation_id`，下一个 Fold 会启动新的 Agent 会话。Agent 可以看到当前 Sandbox 中已经放入的 `factor/` 和 `nl_prior/`，但不能看到上一 Fold 的：

- 对话历史。
- Shell/LLM/Tool 调用日志。
- 测试回测结果。
- 测试 conversation log。

如果某个历史季度在当前 Fold 中成为验证区间，Agent 只能读取当前 Fold 重新生成的验证结果，不能复用它在上一 Fold 作为测试区间时保存的结果文件。

可信日志只能由 Environment / Pipeline 记录。Agent 可以输出解释、原因和结构化结果，但不能替代 Shell、Tool、回测和 LLM 调用日志。

## 2. Agent 的工作区

### 2.1 可见数据

训练探索和正式回测输入都必须满足 PIT 可见性，但验证复盘区间有不同用途：

```text
available_at <= decision_time
```

其中 `/mnt/snapshots/train/` 和 `/mnt/snapshot/` 只包含当前决策时点前可见的数据；`/mnt/snapshots/valid/` 是验证回放和复盘区间，Agent 可以读取其中的验证结果和回放数据来改进当前 Fold，但正式 `generate_candidates()` 不能读取它。

`snapshots` 和 `snapshot` 的用途不同：

| 路径 | 用途 | Agent 权限 | `backtest_tool` 用法 |
|---|---|---|---|
| `/mnt/snapshots/train/` | 训练和探索输入 | 只读可见 | 不作为正式策略入口的运行输入 |
| `/mnt/snapshots/valid/` | 验证回放和复盘区间 | 只读可见 | 验证模式读取它做回放 |
| `/mnt/snapshots/test/` | 测试或 held-out 回放区间 | 不可读 | 冻结评估模式读取它做回放 |
| `/mnt/snapshot/` | 当前正式回测输入视图 | 不作为探索入口 | 调用 `generate_candidates()` 时读取 |

默认窗口与 Environment 可见数据域保持同一顺序：

| 数据域 | Sandbox 准备 | Agent 可怎么用 |
|---|---:|---|
| `daily` | 最近 21 个月 | 日频行情、每日指标和横向排序输入；可以只用其中更短窗口 |
| `intraday_1min` | 最近 5 个交易日 | 日内、打板和开收盘竞价研究 |
| `fundamentals` | 最近 21 个月可见披露 | 财务、分红、业绩预告/快报和主营构成；可以只取最近若干披露 |
| `events` | 最近 21 个月 | 资金流、两融、股东、回购、解禁、大宗交易、龙虎榜等事件和资金状态 |
| `macro` | 最近 21 个月 | 宏观、政策、利率、全球事件和跨市场背景；可以只取最近几个发布周期 |
| `text` | 最近 21 个月 | 公告、新闻、研报、政策文本索引和文本库；可以只检索更短时间窗口 |

具体字段、单位和可见时间由 Environment 的 manifest 记录。

### 2.2 可写目录

Agent 有两个写入区域：

```text
/mnt/artifacts/workspace/       # 临时代码、数据探查脚本和草稿
/mnt/artifacts/agent_output/    # 正式策略产物
```

另有一个只读对照目录：

```text
/mnt/artifacts/parent_output/   # 父策略产物副本，可读不可写
```

正式策略产物目录：

```text
/mnt/artifacts/agent_output/
  factor/
    README.md      # 只读说明
    main.py        # Agent 可写
    factors.json   # Agent 可写
  nl_prior/
    README.md      # 只读说明
    prior.json     # Agent 可写
```

`workspace/` 不冻结、不回放、不复制到下一 Fold。只有 `agent_output/factor/` 和 `agent_output/nl_prior/` 可能冻结为下一 Fold 的起点。

`/mnt/artifacts/results/` 由 `backtest_tool` 写入。Agent 在训练/验证期只读验证结果，不能写入；测试和 held-out 结果不反馈给 Agent。

### 2.3 可调用入口

Agent 只通过 Environment 提供的入口行动：

| 入口 | Agent 何时用 | 结果 |
|---|---|---|
| `sandbox_shell_tool` | 探查数据、写临时代码、调试正式策略文件 | 写入 `workspace/` 或可写的 `agent_output/` 文件 |
| `modification_check_tool` | 每次正式验证回测前 | 返回是否允许进入回测 |
| `backtest_tool` | 修改检查通过后 | 写入 `results/valid_<idx>/` |
| `finish_fold_tool` | 当前 Fold 准备结束时 | 锁定 Fold 写入并等待 Pipeline 冻结和测试 |

Agent 不直接调用外部 LLM provider，不直接访问真实券商，不修改 Environment 或 Pipeline。

## 3. Fold 内工作流

一个 Fold 内可以有多个 Step。Step 是同一个 Agent 会话中的一次“修改 -> 检查 -> 验证回测”迭代记录，不会重启 Agent，也不会创建新的对话上下文。Agent 每跑完一次验证回测，就会得到一个新的 `results/valid_<idx>/`，可以据此继续下一 Step；只有调用 `finish_fold_tool` 才表示当前 Fold 不再继续修改。

### 3.1 初始 Step

第一次创建策略产物时，可能没有历史验证结果，也没有 `nl_output/`。Agent 应：

1. 读取训练窗口和可见文本样本。
2. 在 `workspace/` 中做基础数据探查。
3. 建立少量初始因子。
4. 建立少量可迁移自然语言投资先验。
5. 写入正式 `factor/` 和 `nl_prior/`。
6. 调用修改检查和验证回测。

初始规则应保持简单，避免一开始写入过多针对特定时间、题材或股票的经验。

### 3.2 常规 Step

常规 Step 中，Agent 按下面顺序完成一轮验证迭代：

1. 读取当前 `factor/`、`nl_prior/` 和数据 manifest。
2. 在 `workspace/` 中写临时代码，读取训练窗口做探索。
3. 读取验证回放数据和已有 `results/valid_<idx>/` 做复盘。
4. 小幅修改正式 `factor/` 和 `nl_prior/`。
5. 调用 `modification_check_tool`。
6. 修改检查通过后，调用验证模式 `backtest_tool`。
7. 读取验证结果，包括收益、成交、拒单、订单计划和自然语言评分输出。
8. 如果还需要改进，继续下一 Step；如果当前 Fold 已经可以停止，调用 `finish_fold_tool`。

Agent 不选择测试期结果，不冻结产物，也不决定是否进入下一 Fold；这些由 Pipeline 处理。
Agent 是否结束当前 Fold，应参考本次 Prompt 中的提交验收规则，例如验证收益、回撤、持仓集中度、修改约束和经验条数上限。若 Agent 判断继续搜索的边际收益不值得消耗剩余 Fold 时间，应主动调用 `finish_fold_tool`；Pipeline 不会在多个 Step 之间替 Agent 重新挑选因子或投资先验。

### 3.3 Fold 结束

`finish_fold_tool` 成功后，当前 Fold 的 Agent 会话停止，Agent 不再写入产物。后续冻结、测试执行、账本记录和下一 Fold 启动由 Pipeline 文档定义；测试结果不反馈给 Agent。

## 4. 正式策略产物

### 4.1 `factor/`

`factor/main.py` 必须提供无参数入口：

```python
def generate_candidates() -> "pandas.DataFrame":
    """Read PIT window data and return candidate stocks with factor scores."""
```

返回值是 Agent 已经筛选后的候选股票和因子分，不是全市场因子表，也不是最终订单。Agent 应先在可见股票池内完成因子计算、排序和初筛，再返回有限候选池。候选数量上限由运行配置给出，默认建议在 30-100 只；超过上限时 Environment 应拒绝回测，而不是替 Agent 截断或重新筛选。

```json
{
  "columns": ["ts_code", "factor_score", "reason", "source_artifacts"],
  "rows": [
    {
      "ts_code": "000001.SZ",
      "factor_score": 1.23,
      "reason": "value_momentum_rank",
      "source_artifacts": ["daily_window"]
    }
  ]
}
```

`factor/factors.json` 用于让 Environment 统计本 Step 改了哪些因子：

```json
{
  "factors": [
    {
      "id": "momentum_volume_20d",
      "function": "factor_momentum_volume_20d",
      "description": "20日动量和成交额放大组合信号",
      "lookback_days": 20,
      "direction": "positive"
    }
  ]
}
```

不用的因子直接删除。新增、删除或实质修改因子时，必须同步更新 `factors.json`；登记函数应能在 `main.py` 中找到。

### 4.2 `nl_prior/`

`nl_prior/prior.json` 只记录可迁移的自然语言投资经验。自然语言检索、公司上下文构造、LLM 打分、JSON 解析和得分合成都由 `backtest_tool` 完成。

示例：

```json
{
  "rules": [
    {
      "id": "regulatory_inquiry_penalty",
      "text": "近期出现监管问询、处罚、重大诉讼或审计异常时，除非有明确解决证据，否则应降低自然语言分。",
      "evidence": "公告、交易所问询、监管处罚、诉讼公告",
      "effect": "lower_score_or_exclude"
    }
  ]
}
```

适合写入：

- 重大问询函、诉讼、监管处罚、减持在短窗口内应降权。
- 财报改善如果伴随现金流恶化，应降低置信度。
- 新闻只有情绪表达、缺少公告或经营证据时，不能大幅加分。

不应写入：

- 某个月份买某个题材。
- 某只股票文本好就长期买。
- 根据测试或 held-out 表现反推的规则。

### 4.3 正式策略入口读取规则

Agent 探索时可以读取训练和验证数据槽。正式 `generate_candidates()` 不能读取阶段目录或回测结果，只能读取当前正式回测输入、当前投资先验和 Environment 允许的只读文件。

这样同一份策略代码才能被 `backtest_tool` 安全地用于验证、测试和 held-out。

## 5. Tool 使用语义

### 5.1 `sandbox_shell_tool`

`sandbox_shell_tool` 是受 Runner 管理的 Sandbox Shell，不是宿主机登录 shell。

Agent 可以用它：

- `ls`、`cat`、`rg`、`sed` 查看可读文件。
- 在 `workspace/` 写临时代码。
- 运行 Python 调试数据读取和因子计算。
- 用受限 `apply_patch` 修改 `agent_output/factor/` 和 `agent_output/nl_prior/` 中的可写文件。

边界：

- 非 root 用户，无 sudo。
- 无网络。
- 不能读测试区间。
- 不能写 `results/`、只读 README、主仓库或宿主机路径。

### 5.2 `modification_check_tool`

`modification_check_tool` 无业务输入参数。它检查当前正式工作副本，并返回是否允许进入验证回测。

检查内容：

- 当前正式工作副本相对不可写父产物副本的变化。
- `factor/` 改了多少文件和行。
- `factors.json` 新增、删除或修改了多少因子。
- `nl_prior/prior.json` 新增、删除或修改了多少规则。
- 正式文件格式是否合法。

父产物副本位于 `/mnt/artifacts/parent_output/`，由 Environment/Runner 管理，Agent 可以读取它来理解上一版策略，但不能修改或覆盖。Agent 不需要传父产物路径，也不能通过复制、重命名或重建目录来改变 diff 基准。

若返回 `allowed_to_backtest=false`，Agent 只能缩小正式修改后重试。

### 5.3 `backtest_tool`

`backtest_tool` 是唯一正式验证回测入口。Agent 在 Step 内只能请求验证模式。

验证模式可以使用三种自然语言评分状态，最终是否允许由 Pipeline/run manifest 决定：

| `nl_mode` | Agent 何时使用 | 结果含义 |
|---|---|---|
| `off` | 快速检查因子候选池、订单生成和回测链路 | 不运行自然语言评分，结果只能作为因子快速验证或调试参考 |
| `sample` | 控制 API 成本，对部分候选股票抽样检查自然语言评分是否合理 | 只覆盖样本候选，结果必须标记为抽样验证 |
| `on` | 正式验证 Step 或准备结束 Fold 前 | 对候选池运行完整自然语言评分，是当前基础流程的默认正式验证状态 |

测试和 held-out 的自然语言评分固定开启，由 Runner/root 执行，Agent 不能关闭或抽样。

执行顺序：

1. 校验最近一次修改检查结果。
2. 准备当前正式回测输入。
3. 调用 `factor/main.py::generate_candidates()` 得到 Agent 筛选后的候选股票和 `factor_score`。
4. 根据配置执行自然语言评分。
5. 合成最终分。
6. 生成订单计划。
7. 执行交易约束校验和模拟 Broker 回放。
8. 写入 `results/valid_<idx>/`。

测试和 held-out 使用同一 Tool 的冻结评估模式，由 Runner/root 在 Agent 停止后执行，不反馈给 Agent。

### 5.4 `finish_fold_tool`

`finish_fold_tool` 无输入参数。Agent 调用它表示当前 Fold 已完成，不再继续新的 Step。

调用时 Environment 会先做一次轻量合同校验，确认正式文件可加载、主函数存在、格式合法；不执行真实回测。校验失败且 Fold deadline 尚未到达时，Fold 不结束，Agent 需要修复后重试；如果 deadline 已到，Agent 不再获得新的修复调用，Pipeline 使用最后一个有效产物或父产物回退。

成功输出示例：

```json
{
  "status": "fold_finished",
  "fold_status": "pending_pipeline_review",
  "write_locked": true
}
```

## 6. 修改约束和自然语言经验

除第一次创建策略产物外，每个 Fold 只能在父产物基础上小幅修改。约束阈值由 Pipeline 下发，检查由 Environment 执行。

Agent 需要遵守：

- 临时探索写在 `workspace/`，不计入正式产物。
- 正式修改只写 `agent_output/factor/` 和 `agent_output/nl_prior/`。
- 每次正式回测前都调用 `modification_check_tool`。
- 不通过修改检查时，不请求正式回测。

Agent 只能把可迁移经验写入 `nl_prior/prior.json`。验证期 `nl_output/` 可以作为下一 Step 修改经验的证据，但测试和 held-out 结果不能写入经验。

正则化不是 Agent 入口；Agent 只负责在 Step 内保持经验简单、可迁移、可检查。

## 7. LLM 调用和日志

Agent 主对话由 Runner 通过宿主侧 LLM Proxy 调用。正式自然语言评分由 `backtest_tool` 内部步骤通过 LLM Proxy 调用。

Agent 不能在 Sandbox Shell/Python 中直接调用外部 provider，也不能把 API key 写入 prompt、代码、输出或 artifact。

真实 API 调用日志由 Environment/LLM Proxy 自动记录。Agent 只需要在解释中引用相关验证结果和 `nl_output/`；可信日志由 Environment/Pipeline 维护。

## 8. 禁止行为和验收清单

Agent 禁止：

- 读取当前决策时点之后的数据。
- 读取测试或 held-out 回放区间。
- 在正式 `generate_candidates()` 中读取训练/验证阶段目录或回测结果。
- 把验证期未来行情、收益或成交结果硬编码进策略入口。
- 用测试期结果反向修改当前 Fold。
- 在 held-out 上调参。
- 硬编码特定股票、月份或题材结论。
- 绕过 Environment 入口访问 raw 数据、主机 shell 或网络。
- 绕过 `backtest_tool` 的自然语言评分直接调用外部 LLM API。
- 修改回测接口。
- 直接生成真实订单或连接真实 QMT。

Agent 相关改动至少检查：

- Agent 文档是否只描述 Agent 职责，而不重复 Fold / Epoch 编排。
- Agent 能读、能写、能调用的对象是否清楚。
- 策略产物是否只包含 `factor/` 和 `nl_prior/`。
- 正式 `generate_candidates()` 是否无参数，并且不依赖训练/验证/测试阶段目录。
- 修改检查是否发生在正式回测前。
- 测试和 held-out 是否不反馈给 Agent。
- 所有真实 LLM 调用是否由 Environment/LLM Proxy 自动记录。
