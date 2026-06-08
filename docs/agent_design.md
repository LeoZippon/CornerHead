# Agent 设计

本文档记录单 Agent 方案。Agent 在 Sandbox 内工作，可以调用受控 Tool、写 Python 因子逻辑、运行 Sandbox Shell、总结经验，但不能读取未来数据、不能直接下单、不能绕过 Sandbox。

相关边界：

- 数据下载、单位和 raw 审计见 `docs/data_documentation.md`。
- PIT 窗口、Sandbox、Python 环境、Shell 和 Tool 见 `docs/environment_design.md`。
- Step / Fold / Epoch 编排见 `docs/pipeline_design.md`。
- 实盘部署和 QMT 流程见 `docs/QMT_documentation.md`。

## 术语说明

| 术语 | 含义 |
|---|---|
| Agent | 在一个 Fold 内读取 Sandbox 数据、写策略代码、调用受控 Shell/Tool 并输出策略主函数的模型驱动执行者 |
| Sandbox | Agent 运行的隔离环境，只能读可见数据窗口，只能写本次运行产物 |
| PIT | Point-in-time，只允许使用决策时点已经可见的数据 |
| `available_at` | 某条数据最早可以被 Agent 看到的时间 |
| Step | 一个 Fold 内的一次策略修改和验证尝试 |
| Fold | 一个验证区间加后续测试季度 |
| Epoch | 从起始 Fold 跑到结束 Fold 的一整轮训练流程 |
| `strategy_artifact` | 跨 Fold 共享的因子逻辑和自然语言投资先验 |
| LLM analysis | 大模型基于公告、新闻、研报等文本给候选股票打分 |
| Held-out | 所有训练结束后才运行的冻结测试区间 |

## 目录

- [1. 核心原则](#1-核心原则)
- [2. 共享策略产物](#2-共享策略产物)
- [3. Agent 在一个 Step 内做什么](#3-agent-在一个-step-内做什么)
- [4. 自然语言分析](#4-自然语言分析)
- [5. 修改限制和正则化](#5-修改限制和正则化)
- [6. LLM 调用边界](#6-llm-调用边界)
- [7. 输出格式](#7-输出格式)
- [8. 禁止行为](#8-禁止行为)
- [9. 验收清单](#9-验收清单)

## 1. 核心原则

### 1.1 Agent

系统在每个 Fold 启动一个新的 Agent 会话。它负责：

- 读取当前 Fold 的 Sandbox 数据。
- 写和修改 Python 策略代码。
- 通过 `sandbox_shell_tool` 运行自己的代码，调试候选股票、目标权重或订单意图。
- 在训练期通过受控 `sandbox_shell_tool` 调试 Sandbox 内文件、代码和运行环境。
- 调用 `modification_check_tool` 自查修改量。
- 调用 `backtest_tool` 执行正式验证；回测内部会加载 `agent_output/factor/` 和 `agent_output/nl_prior/`，并调用自然语言分析子步骤。
- 维护一份简短的自然语言投资先验。

Fold 之间不共享 Agent 对话历史。下一个 Fold 只接收上一个 Fold 冻结后的 `factor/` 和 `nl_prior/`，并由 Pipeline 放入新 Sandbox 的 `agent_output/`。

### 1.2 时间墙

Agent 只能看到 Sandbox 中已经准备好的 PIT 窗口数据。任何进入 Agent 的行情、财务、事件、文本和宏观数据都必须满足：

```text
available_at <= decision_time
```

Agent 可以在 Sandbox 内自由探索这些已可见数据，但不能访问：

- 决策日之后的数据。
- 当前 Fold 的测试结果。
- 上一 Fold 作为测试时产生的测试结果、回测明细、复盘结论或 ledger artifact。
- Held-out 结果。
- 原始数据全集。
- 主机文件系统。
- 未授权网络。

滚动窗口中，同一自然季度可以在上一 Fold 中是测试季度、在下一 Fold 中变成验证区间。此时 Agent 可以在当前 Fold 按验证规则重新回放该季度，并读取当前验证产生的结果；禁止的是直接读取或复用上一 Fold 测试阶段已经写出的结果 artifact、复盘文本或对话历史。

### 1.3 窗口长度

Environment 预先为每个 Fold 准备固定最大长度的数据窗口，例如：

| 数据 | Sandbox 准备 | Agent 可怎么用 |
|---|---|---|
| 日频行情 | 最近 21 个月 | 可以只用其中 1、3、6、12、18 个月 |
| 事件/资金 | 最近 21 个月 | 可以只用其中 1、3、6、12 个月 |
| 宏观/全球 | 最近 21 个月 | 可以只取最近几个发布周期 |
| 文本索引 | 最近 21 个月 | 可以只检索最近 7、30、90、180 天 |
| 财务事件 | 最近 21 个月可见披露 | 可以只取最近 1、4、8 条 |
| 分钟线 | 最近 5 个交易日 | 只用于日内、打板和开收盘竞价研究 |

窗口上限由配置决定。Agent 写代码时可以选择更短窗口，但不能要求 Environment 暴露更长窗口。

## 2. 共享策略产物

跨 Fold 共享的内容称为策略产物。策略产物尽量小，避免记住过多特定时间和特定股票的噪声。

| 产物 | 内容 | 上限建议 |
|---|---|---|
| `factor/` | Python 因子逻辑、入口函数和配置 | 少量文件 |
| `nl_prior/` | 可迁移的自然语言投资逻辑，包括文本风险判断和组合选择原则 | 20 条以内 |

以下内容可以写入账本审计，但不能作为下一 Fold 的 Agent 对话输入：上一 Fold 对话历史、Shell/LLM/服务调用明细、自然语言子任务日志、训练/测试回测明细、测试结果、人工复盘长文。若上一 Fold 的测试季度在下一 Fold 中变成验证区间，只能通过当前 Fold 的 `backtest_tool` 重新生成验证结果，不能复用上一 Fold 的测试 ledger。

策略产物由 Pipeline 持久化到宿主机实验目录，并通过 `strategy_artifact_id` 在 Fold 之间传递。物理路径按实验和 Epoch 分层，便于并行实验和保留每个 Epoch 历史：

```text
experiments/<experiment_id>/
  strategy_artifacts/
    <epoch_id>/
      <strategy_artifact_id>/
        manifest.json
        factor/
        nl_prior/
```

`manifest.json` 必须记录 `experiment_id`、`epoch_id`、父产物、创建 Fold、创建 Step、策略产物聚合版本、冻结状态和来源 run。下一 Fold 启动时，Agent 不读取上一 Fold 对话；Pipeline 只把上一 Fold 测试前冻结的 `factor/` 和 `nl_prior/` 初始化到新 Sandbox 的 `/mnt/artifacts/agent_output/`。

策略产物必须可序列化，至少记录：

```json
{
  "experiment_id": "exp_quarterly_single_agent_001",
  "epoch_id": "epoch_001",
  "strategy_artifact_id": "strategy_epoch01_fold202101",
  "parent_strategy_artifact_id": "strategy_epoch01_fold202012",
  "strategy_artifact_hash": "sha256:...",
  "created_at_fold": "202101",
  "source_run_id": "run_..."
}
```

## 3. Agent 在一个 Step 内做什么

Step 是一个 Fold 内的一次尝试。一个 Fold 可以运行多个 Step。

### 3.1 输入

每个 Step 开始时，Agent 看到：

- 当前策略产物：`factor/` 和 `nl_prior/`。
- 本 Fold 的训练 Sandbox。
- 数据窗口路径。
- 可用 Shell 和 Tool 清单。
- 本 Step 修改约束。
- 防过拟合提示词。

每个 Fold 的 Agent 会话从固定系统提示词、当前策略产物和当前 snapshot 开始。上一 Fold 的 messages 不进入本 Fold prompt。

Agent 不负责自己计时。每个 Fold 由 Pipeline 统一限时 20 分钟；Step 不单独限时。距离 Fold deadline 5 分钟以上时，Agent 不会收到剩余时间提示，可以自由探索。剩余时间低于阈值时，Runner/Proxy 会触发固定收尾提示，要求 Agent 输出当前最好版本的 `agent_output/factor/` 和 `agent_output/nl_prior/`。

训练 Sandbox 示例：

```text
/mnt/snapshot/
  daily.parquet
  events.parquet
  fundamentals.parquet
  macro.parquet
  text_index.parquet
  constraints.parquet
  manifest.json

/mnt/artifacts/
  workspace/
  agent_output/
    factor/
      main.py
      factors.json
    nl_prior/
      prior.md
      prior.json
  results/
    valid_000/
      summary.json
      detailed_return.json
      order_plan.parquet
      nl_output/
  logs/
```

`workspace/` 是 Agent 自由探索区。`agent_output/` 是 Agent 的正式策略产物区。`results/` 是 `backtest_tool` 的结果区，Agent 在训练/验证期只读，不能写入；测试和 held-out 结果不反馈给 Agent。

第一次创建策略产物时，Environment 会从 `configs/agent_output_template/` 初始化 `agent_output/factor/main.py`、`agent_output/factor/factors.json`、`agent_output/nl_prior/prior.md` 和 `agent_output/nl_prior/prior.json`。这些文件是给 Agent 的就地格式提示：`main.py` 规定回测入口函数，`factors.json` 登记可统计的因子 ID，`prior.md` 说明自然语言经验的写法，`prior.json` 存放可检查的结构化规则。

### 3.2 操作顺序

一个 Step 的推荐顺序：

1. 读取当前策略产物和数据 manifest。
2. 在 `workspace/` 中用 Python 检查可见数据，写临时代码和调试脚本。
3. 可用 Shell/Python 调试策略代码和数据读取，但这只用于探索，不作为正式回测结果。
4. 确认临时代码可运行后，把最终策略入口写入 `agent_output/factor/main.py`，把可迁移投资经验写入 `agent_output/nl_prior/prior.md` 和 `agent_output/nl_prior/prior.json`。
5. 调用 `modification_check_tool` 自查修改量；不通过就缩小正式产物修改后重试。
6. 总结当前全局投资逻辑，要求语义通用，避免只针对某个月份或某个题材。
7. 调用 `backtest_tool`；它自动加载 `agent_output/factor/` 和 `agent_output/nl_prior/`，调用策略主函数、运行自然语言分析、校验订单计划并通过模拟 Broker 回测。
8. 读取 `results/<phase>_<idx>/summary.json`、`detailed_return.json`、`order_plan.parquet`、`nl_output/` 和拒单/成交记录。
9. 根据验证期回测结果决定是否在下一 Step 小幅修改代码或经验。
10. 提交 Step 输出。Environment 自动记录运行日志、Shell/Tool 调用、关键 manifest、产物路径和 LLM conversation log。

### 3.3 Python 策略代码

Agent 可以写 Python 策略代码。临时代码、探查脚本和调试输出放在 `/mnt/artifacts/workspace/`；确认无误后再把正式入口、配置和必要模块写入 `/mnt/artifacts/agent_output/factor/main.py`，并在 `/mnt/artifacts/agent_output/factor/factors.json` 登记可统计的因子 ID。代码可以包含因子计算、排序、过滤和权重生成。正式验证和测试时，`backtest_tool` 只调用 `agent_output/factor/main.py::generate_orders(context)`，Agent 自己用 Shell 跑出的临时结果只能用于调试。

代码必须满足：

- 代码只读 `/mnt/snapshot/` 或 Environment 服务返回的文件。
- 正式入口必须提供 `generate_orders(context)`，返回结构化表格，例如 `ts_code, action, target_weight, score, reason, source_artifacts`。
- 新增、删除或实质修改因子时，必须同步更新 `factor/factors.json`；格式不合法或登记表和代码不一致时，`modification_check_tool` 应拒绝正式回测。
- 如果返回明确订单意图，字段至少包含 `ts_code, action, order_type, target_weight/volume/amount, reason, source_artifacts`。
- 不能写主仓库。
- 不能联网下载数据或安装依赖。
- 不能硬编码测试季度、未来日期或具体股票结论。
- 每次运行的脚本路径、输出路径、exit code 和 stdout/stderr 由 Environment 自动记录；代码和数据版本在 Fold/run manifest 里聚合记录。Agent 不需要自行写可信日志。

训练期调试和 Python 执行都使用 Environment 提供的 `sandbox_shell_tool`。它不是普通登录 shell，而是受 Runner 管理的 Sandbox Shell；具体读写范围、网络、资源和日志规则以 `docs/environment_design.md` 为准。

常用本地命令：

| 命令 | 用途 | 边界 |
|---|---|---|
| `rg` | 搜索 snapshot、策略代码和日志片段 | 只能在挂载路径内搜索 |
| `sed` | 查看文件片段或做小范围文本处理 | 不应绕过 `apply_patch` 大量改写策略产物 |
| `apply_patch` | 修改 `workspace/` 草稿和 `agent_output/factor/`、`agent_output/nl_prior/` 约定文件 | 受限补丁命令；正式产物修改后仍要过 `modification_check_tool`；不能写 `results/` |
| `python` | 执行 Agent 写的策略代码和分析脚本 | 无网络、无 API key、只读 snapshot；可写 `workspace/` 和 `agent_output/`；不可写 `results/` |

正式入口函数：

```python
def generate_orders(context: dict) -> "pandas.DataFrame":
    """Read PIT window data and return candidate orders or target weights."""
```

返回值示例：

```json
{
  "columns": ["ts_code", "action", "target_weight", "score", "reason", "source_artifacts"],
  "rows": [
    {
      "ts_code": "000001.SZ",
      "action": "target_weight",
      "target_weight": 0.05,
      "score": 1.23,
      "reason": "value_momentum_rank",
      "source_artifacts": ["daily_window", "nl_prior"]
    }
  ]
}
```

订单只是模拟回测输入。Agent 禁止连接真实 QMT、真实券商或生成绕过人工/系统风控的真实订单。

## 4. 自然语言分析

自然语言分析用于补充因子难以表达的逻辑，例如：

- 公告是否有重大风险。
- 新闻是否只是情绪噪声。
- 研报逻辑是否可信。
- 宏观或政策背景是否影响仓位。
- 候选股票是否存在需要规避的事件。

### 4.1 输入

自然语言分析通过 `backtest_tool` 内部步骤完成。该步骤包含文本检索和 LLM Proxy 调用，Agent 不能绕过它直接调用外部 provider。

输入包括：

| 输入 | 说明 |
|---|---|
| `candidate_pool` | Agent 代码生成的候选股票列表 |
| `nl_prior` | 当前自然语言投资先验 |
| `decision_time` | 决策时点 |
| `text_index` / `text_library` | 回测内部可检索的可见文本索引和 as-of 文本库 |
| `constraints_prompt` | 禁止未来知识、要求引用 evidence、要求说明不确定性 |

### 4.2 并行文本分析任务

`backtest_tool` 的自然语言分析步骤可以并行启动多个 LLM 实例。它们是短生命周期文本分析任务，不拥有长期记忆，也不能修改策略产物。

每个任务流程：

1. 根据股票、行业、投资先验生成关键词。
2. Tool 内部从 `/mnt/snapshot/text_index.parquet` 和 `/mnt/snapshot/text_library/` 检索可见文本。
3. 阅读检索结果。
4. 输出自然语言分数、风险标签和引用。

输出示例：

```json
{
  "ts_code": "000001.SZ",
  "nl_score": 0.62,
  "risk_tags": ["no_major_negative"],
  "positive_points": ["业绩披露稳定"],
  "negative_points": [],
  "evidence_ids": ["txt_..."],
  "uncertainty": "medium"
}
```

### 4.3 合成分数

Agent 写入策略主函数时，应显式说明：

- 代码输出的分数如何计算。
- 自然语言分如何进入总分。
- 风险标签如何影响剔除或降权。
- 股票数量、权重和行业集中度约束。
- 订单类型、目标权重、提交时点和无法成交时的处理规则。

### 4.4 用自然语言分析迭代 `nl_prior`

自然语言分析步骤可以在每个训练 Step 的 `backtest_tool` 内部运行。它有两个作用：

| 作用 | 产物 | 是否直接修改 `nl_prior` |
|---|---|---|
| 当前 Step 回测打分 | `results/<phase>_<idx>/nl_output/scores.jsonl`、风险标签、evidence 引用 | 否 |
| 下一 Step 经验更新 | Agent 基于 `results/`、验证回测和失败原因改写 `nl_prior/` | 由 Agent 改，必须过修改约束 |

推荐规则：

- 自然语言分析步骤自身不能写 `nl_prior/`。
- Agent 可以读取训练/验证期 `results/<phase>_<idx>/nl_output/`，把可迁移的结论整理成 `nl_prior` 修改。
- 这些修改通常作为下一 Step 的起点；如果要影响当前 Step，必须重新运行修改约束检查和 `backtest_tool`，并确保使用的 `nl_prior`、`nl_output`、策略主函数返回值、订单计划和回测结果在 manifest 中一致。
- 只允许写可迁移经验，例如“问询函、减持和诉讼在短窗口内降权”；不允许写“这个月买某题材”或“某只股票文本好就长期买”。
- 每个 Step 对 `nl_prior` 的新增、删除和改写都计入 `modification_check_tool`。

## 5. 修改限制和正则化

### 5.1 Fold 内修改限制

除初始化策略产物的第一次外，每个 Fold 只允许在父产物基础上小幅修改。Agent 可以在 `workspace/` 内临时多改，但 Pipeline 只冻结通过修改约束检查的 `agent_output/factor/` 和 `agent_output/nl_prior/`；超出约束的 Step 必须 rejected，不能被部分静默接受。

当前先使用简单可数约束，不做复杂语义判断。约束只看改了多少文件、多少行、多少登记函数、`factors.json` 里新增/删除/修改了多少因子、多少条 `nl_prior` 规则，以及单条文本长度。

| 项目 | 限制 |
|---|---|
| `factor/` 文件 | 每个 Fold 最多修改 `max_factor_files_changed_per_fold` 个文件 |
| `factor/` 行数 | 每个 Fold 最大 diff 行数为 `max_factor_diff_lines_per_fold` |
| `factor/` 函数/登记因子 | 每个 Fold 最多新增、删除或修改固定数量；因子 ID 只从 `factor/factors.json` 统计 |
| `nl_prior/` 规则 | 每个 Fold 最多新增、删除或改写 `max_nl_prior_changes_per_fold` 条 |
| `nl_prior/` 字符数 | 总规则数不超过 `max_nl_prior_rules_total`，单条长度不超过 `max_nl_prior_chars_per_rule` |
| Step 数 | 固定上限，例如 3-5 次 |

修改约束至少包含：

```json
{
  "is_initial_artifact": false,
  "parent_strategy_artifact_id": "strategy_epoch001_fold2022Q1",
  "factor_constraints": {
    "max_files_changed_per_fold": 3,
    "max_diff_lines_per_fold": 160,
    "max_functions_changed_per_fold": 4,
    "max_modified_factor_ids_per_fold": 2,
    "max_new_factor_ids_per_fold": 1,
    "max_deleted_factor_ids_per_fold": 1
  },
  "nl_prior_constraints": {
    "max_changes_per_fold": 3,
    "max_rules_total": 20,
    "max_chars_per_rule": 240
  },
  "max_steps_per_fold": 5
}
```

第一次创建策略产物时 `is_initial_artifact=true`，可使用单独的初始化约束，例如允许创建若干基础因子和初始 `nl_prior`。之后所有 Fold 都必须与 `parent_strategy_artifact_id` 指向的冻结产物做 diff。

`nl_prior/` 的权威格式应是结构化 JSON，每条规则有稳定 `prior_id`、正文、适用范围、创建 Fold、最近修改 Fold 和状态。Markdown 可以作为人读视图，但不能作为唯一权威文件。这样 Pipeline 才能准确统计“新增、删除、改写几条”。

每次修改必须记录父产物 ID、当前产物 ID、diff 摘要、约束消耗和接受理由。如果超过限制，本 Step rejected；如果一个 Fold 内所有 Step 都超过限制，本 Fold 失败，不能静默放行。

Agent 修改 `agent_output/factor/` 或 `agent_output/nl_prior/` 后，可以调用 `modification_check_tool` 做预检查。调用时不传路径、父产物或约束参数，只触发“检查当前正式工作副本”；这些上下文由 run manifest 注入。`workspace/` 和 `results/` 不参与该检查，也不会被冻结。Pipeline 在正式 `backtest_tool` 前也必须调度 Environment 执行一次检查。若返回 `allowed_to_backtest=false`，Environment 直接拒绝继续运行正式回测，本 Step 不能得到验证结果，Agent 只能缩小正式产物修改范围后重新提交。Agent 不能通过自然语言声明自己未超约束，约束结论只认 `strategy_artifact_diff.json`。

### 5.2 Epoch 后正则化

一个 Epoch 结束后，启动正则化 LLM。它不看 held-out，也不读取测试季度收益结果；只审计本 Epoch 的因子逻辑、投资先验、修改记录和验证 Step 结果。

正则化目标：

- 删除只针对特定月份/季度、特定题材或特定股票的经验。
- 合并重复规则。
- 删除没有稳定证据支持的规则。
- 保持经验总条数不超过上限。
- 保持因子逻辑简单。
- 只能删除、合并和抽象化规则，不能新增基于某段表现的交易规则。
- 保留规则必须有跨 Fold 稳定性和可解释逻辑，不能只因为某段验证表现好而保留。

需要删除的例子：

```text
看到芯片股就买。
某个季度券商表现好，所以以后加仓券商。
2023 年某政策出现后同类股票都应该买。
```

可以保留的例子：

```text
短期动量需要配合成交额放大，否则容易是假突破。
财报改善如果伴随明显减持公告，应降低自然语言分。
重大问询函或监管处罚在短窗口内应触发降权。
```

## 6. LLM 调用边界

Agent 不负责写可信日志，也不能在 Sandbox Shell/Python 中直接调用外部 provider。Agent 主对话由 Runner 通过宿主侧 LLM Proxy 调用；正式自然语言评分由 `backtest_tool` 内部步骤通过 LLM Proxy 调用。两类真实 API 调用都必须由 Environment/LLM Proxy 自动记录 conversation log。

Agent 只需要保证：

- 不把 API key 写入 prompt、代码、输出或 artifact。
- 每个自然语言判断引用 `backtest_tool` 在 `results/<phase>_<idx>/nl_output/` 产出的 evidence。
- Step 输出记录使用了哪些 `results/<phase>_<idx>/nl_output/` 和 conversation trace ID。可信日志由 Environment/Pipeline 维护；Agent 不需要分别管理 `execution_calls.jsonl` 和 `llm_conversations.jsonl`。

实验级日志字段和保存路径由 `docs/pipeline_design.md` 第 7 章统一定义；Environment 只负责实际写运行时文件。

## 7. 输出格式

### 7.1 Step 输出

```json
{
  "epoch_id": "epoch_001",
  "fold_id": "fold_2022Q1",
  "step_id": 2,
  "strategy_artifact_id": "strategy_epoch001_fold2022Q1_step02",
  "strategy_artifact_hash": "sha256:...",
  "order_plan_artifact_id": "order_plan_step02",
  "candidate_count": 80,
  "selected_count": 20,
  "validation_backtest": {
    "period": "2021-10..2021-12",
    "return": 0.031,
    "max_drawdown": 0.042
  },
  "changes": [
    "changed momentum window from 60 to 90 inside prepared data"
  ],
  "modified_functions": ["compute_momentum_score"],
  "diff_line_count": 42,
  "parent_strategy_artifact_id": "strategy_epoch001_fold2022Q1_step01",
  "current_strategy_artifact_id": "strategy_epoch001_fold2022Q1_step02",
  "accepted": true,
  "accept_reason": "validation return improved while drawdown stayed below limit",
  "reject_reason": null
}
```

### 7.2 Fold 输出

```json
{
  "fold_id": "fold_2022Q1",
  "input_window": "2020-01..2021-09",
  "validation_period": "2021-10..2021-12",
  "test_period": "2022Q1",
  "frozen_strategy_artifact_id": "strategy_epoch001_fold2022Q1_final",
  "test_result": {
    "return": 0.018,
    "max_drawdown": 0.035,
    "turnover": 1.0
  },
  "no_change_after_test": true
}
```

## 8. 禁止行为

Agent 禁止：

- 读取当前决策时点之后的数据。
- 读取 root-only 测试 snapshot，例如 `/mnt/test_snapshot`。
- 用测试期结果反向改当前 Fold。
- 把任何 Fold 的测试结果 artifact、测试回测明细、复盘结论或测试 ledger 写入下一 Fold prompt、策略产物或正则化输入。
- 在 held-out 上调参。
- 硬编码特定股票、月份或题材结论。
- 绕过 Environment 入口直接访问 raw 数据、主机 shell 或网络。
- 在 `sandbox_shell_tool` 之外启动 shell，或尝试突破 Sandbox 挂载和权限边界。
- 绕过 `backtest_tool` 的内部自然语言分析步骤直接调用外部 LLM API。
- 修改回测接口。
- 直接生成真实订单或连接真实 QMT。
- 把 API key 写入 prompt、输出或 artifact。

## 9. 验收清单

Agent 相关改动至少检查：

- Agent 职责是否清楚，策略产物是否可复现。
- Sandbox 中数据是否只包含 PIT 窗口。
- Agent 是否可以写代码，但只能在 Sandbox 内运行。
- Agent 是否只能通过 Environment 定义的 `sandbox_shell_tool` 做调试和运行 Python。
- 策略主函数返回值、订单计划、自然语言输出和回测输入是否有固定 JSON/schema。
- 每个 Step/Fold 的修改是否受限。
- 测试和 held-out 是否不修改策略产物。
- Environment 是否自动完整记录所有 LLM 调用。
- 正则化 LLM 是否只删除、合并和抽象化经验，不读取 held-out 或测试季度收益结果。
