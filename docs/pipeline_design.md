# Pipeline Design

整理日期：2026-06-07

本文档记录 Pipeline 层。Pipeline 只回答：按什么顺序调用 Data、Environment 和 Agent，何时冻结，写哪些账本，失败时如何停止。
Agent 的产物语义见 `docs/agent_design.md`；Environment 的时间墙、撮合、快照和沙箱见 `docs/environment_design.md`；raw 数据和审计见 `docs/data_documentation.md`。

## 导航

- [1. Pipeline 层职责](#1-pipeline-层职责)
  - [1.1 负责什么](#11-负责什么)
  - [1.2 不负责什么](#12-不负责什么)
  - [1.3 常用词](#13-常用词)
  - [1.4 代码入口](#14-代码入口)
- [2. 历史窗口入口](#2-历史窗口入口)
  - [2.1 命令](#21-命令)
  - [2.2 输入和输出](#22-输入和输出)
- [3. WFO、测试和 held-out](#3-wfo测试和-held-out)
  - [3.1 Development 流程](#31-development-流程)
  - [3.2 测试和 held-out 规则](#32-测试和-held-out-规则)
- [4. LLM Shadow 编排](#4-llm-shadow-编排)
  - [4.1 构造证据包](#41-构造证据包)
  - [4.2 调用 provider](#42-调用-provider)
- [5. 冻结、账本和失败条件](#5-冻结账本和失败条件)
  - [5.1 冻结内容](#51-冻结内容)
  - [5.2 本地输出](#52-本地输出)
  - [5.3 失败条件](#53-失败条件)
- [6. 双层 Agent 交接](#6-双层-agent-交接)
  - [6.1 交接流程](#61-交接流程)
  - [6.2 简短案例](#62-简短案例)
  - [6.3 能力开关](#63-能力开关)
- [7. 验收清单](#7-验收清单)

## 1. Pipeline 层职责

### 1.1 负责什么

Pipeline 负责：

- 解析实验配置和命令行参数。
- 调用 Environment 构造历史窗口、决策输入和只读快照。
- 调用 Agent 生成模板、实例、候选股或 LLM shadow。
- 编排训练、测试、held-out 和复盘。
- 冻结模板、实例、prompt、模型、数据快照和代码 hash。
- 写入 Trial Ledger、case、metrics 和 artifact hash。
- 保证 held-out 不回流到 development。

### 1.2 不负责什么

Pipeline 不负责：

- 不下载 raw 数据。
- 不实现 Agent 决策逻辑。
- 不实现撮合、交易约束、PIT 可见性或沙箱隔离。
- 不绕过 Environment 读取 `data/raw`。

### 1.3 常用词

| 词 | 含义 |
|---|---|
| PIT | 按决策时点过滤未来信息 |
| WFO | 滚动训练和测试 |
| held-out | 冻结方案后的最终留出验证 |
| snapshot | 某个决策时点可见的数据包 |
| ledger | 可审计流水账 |
| Template | 外层 Agent 提出的策略模板 |
| Instance | 内层 Agent 在训练窗口内调出的具体实例 |
| shadow | 只记录、不影响交易的 LLM 判断 |

### 1.4 代码入口

导入方向：

```text
scripts/hl.py -> pipelines
pipelines -> environment + agent
environment -> 不依赖 agent
agent -> 不拥有 broker state
```

主要文件：

| 文件 | 职责 |
|---|---|
| `scripts/hl.py` | 单一 HL CLI 入口 |
| `src/hl_trader/pipelines/experiment.py` | development WFO 和 held-out runner |
| `src/hl_trader/pipelines/formulaic_wfo.py` | 公式化控制组编排 |
| `src/hl_trader/pipelines/llm_shadow.py` | Evidence Pack 和 LLM shadow 编排 |

目标子命令：

| CLI | 作用 | 写入 |
|---|---|---|
| `build-history-window` | 构造某个决策时点的历史窗口 | `data/asof_snapshots/<snapshot_id>/` |
| `build-fundamental-events` | 财务 raw -> PIT 事件层 | `data/features/fundamental_events/` |
| `audit-fundamental-events` | 审计财务事件层 | `results/data_quality/fundamental_events_status.json` |
| `run-development` | held-out 前的滚动训练和测试 | development ledger |
| `run-heldout` | 冻结方案的留出验证 | held-out ledger |
| `llm-shadow` | Evidence Pack -> LLM shadow | evidence JSONL、shadow ledger |

## 2. 历史窗口入口

### 2.1 命令

```bash
PYTHONUNBUFFERED=1 PYTHONPATH=src ~/miniconda3/bin/conda run -n stock python scripts/hl.py build-history-window \
  --config configs/experiments/<experiment>.yaml \
  --fold-id <fold_id> \
  --phase train \
  --decision-time <YYYY-MM-DDTHH:MM:SS+08:00> \
  --snapshot-root data/asof_snapshots/<snapshot_id>
```

财务事件层入口：

```bash
PYTHONUNBUFFERED=1 PYTHONPATH=src ~/miniconda3/bin/conda run -n stock python scripts/hl.py build-fundamental-events \
  --raw-dir data/raw \
  --output-root data/features/fundamental_events \
  --start-date 20200101 \
  --end-date <YYYYMMDD>
```

### 2.2 输入和输出

Pipeline 做的事：

1. 读取配置、fold、phase 和 `decision_time`。
2. 接收外层 Agent 或配置提出的数据窗口需求。
3. 把需求交给 Environment/Data Gateway。
4. 校验输出存在、非空、带 manifest 和 hash。
5. 把 snapshot 路径交给后续训练、测试或 LLM shadow。

Pipeline 不直接 raw join。财务、宏观、事件、分钟和文本都必须通过 Environment/Data Gateway。

## 3. WFO、测试和 held-out

### 3.1 Development 流程

Development 是 held-out 前的滚动研发流程：

```text
配置
  -> 生成 folds
  -> 外层 Agent 生成 Template
  -> 构造 train/test snapshot
  -> 内层 Agent 在 train 内生成 Candidate Instance
  -> Pipeline 选择并冻结 Instance
  -> Test sandbox 执行冻结 Instance
  -> 写 fills、metrics、case、ledger
  -> 外层 Agent 读取 development case 后提出 mutation
```

入口：

```bash
PYTHONUNBUFFERED=1 PYTHONPATH=src ~/miniconda3/bin/conda run -n stock python scripts/hl.py run-development \
  --config configs/experiments/<experiment>.yaml \
  --snapshot-root data/asof_snapshots/<train_snapshot_id> \
  --ledger-path experiments/trial_ledger/<development_run_id>.jsonl
```

Pipeline 在训练阶段只负责调度和记录。参数搜索、回放评分和 LLM memo 必须在 train snapshot 内完成；测试结果不能回流训练调参。

### 3.2 测试和 held-out 规则

测试阶段：

- 只执行冻结 Instance。
- 可以调用 LLM 生成 memo 或 proposal，但不能改参数。
- 事件动作和常规调仓都必须走冻结交易策略。
- 回放、撮合、成本、涨跌停、T+1 和仓位约束由 Environment 执行。
- Pipeline 只记录 order reason、fills、metrics 和 case。

held-out 阶段：

- 只验证已经冻结的方案。
- 不允许搜索参数。
- 不允许修改 Template。
- 不允许把 held-out 结果写回 development mutation。
- 必须写独立 held-out ledger。

held-out 入口：

```bash
PYTHONUNBUFFERED=1 PYTHONPATH=src ~/miniconda3/bin/conda run -n stock python scripts/hl.py run-heldout \
  --config configs/experiments/<experiment>.yaml \
  --snapshot-root data/asof_snapshots/<heldout_snapshot_id> \
  --ledger-path experiments/trial_ledger/<heldout_run_id>.jsonl
```

## 4. LLM Shadow 编排

### 4.1 构造证据包

Pipeline 调用 Agent 的 EvidencePackBuilder，但不解释自然语言证据。

流程：

1. 读取 snapshot manifest、历史窗口、市场状态、交易约束和候选股票。
2. 在 snapshot 内的文本库执行白名单检索。
3. 生成 Evidence Pack。
4. 校验 pack hash 和 PIT 字段。
5. 写入 evidence JSONL。

入口：

```bash
PYTHONUNBUFFERED=1 PYTHONPATH=src ~/miniconda3/bin/conda run -n stock python scripts/hl.py llm-shadow \
  --snapshot-root data/asof_snapshots/<snapshot_id> \
  --evidence-out data/evidence_packs/llm_shadow.jsonl \
  --dry-run
```

### 4.2 调用 provider

真实 provider shadow：

- API key 只从环境变量或 ignored `.env` 读取。
- 调用 Agent 的 LLM advisor。
- 写 conversation log。
- 写 shadow ledger。
- 默认 `can_affect_trading=False`。

Dry-run 不读取 API key、不调用网络，只校验证据、hash 和 ledger 链路。

## 5. 冻结、账本和失败条件

### 5.1 冻结内容

进入测试或 held-out 前，Pipeline 必须冻结：

- `template_hash`
- `template_execution_spec_hash`
- `instance_hash`
- 参数、权重和阈值 hash
- `prompt_hash`
- `llm_model_id`
- `llm_settings_hash`
- `trade_policy_hash`
- `data_snapshot_id`
- `code_commit`
- `tool_gateway_policy_hash`

任何冻结内容变化，都必须形成新的 trial。

### 5.2 本地输出

| 输出 | 默认路径 | 说明 |
|---|---|---|
| Development ledger | `experiments/trial_ledger/<run_id>.jsonl` | ignored，本地实验流水 |
| Held-out ledger | `experiments/trial_ledger/<heldout_run_id>.jsonl` | ignored，冻结验证 |
| Snapshot | `data/asof_snapshots/<snapshot_id>/` | ignored，只读输入 |
| Evidence pack | `data/evidence_packs/*.jsonl` | ignored，LLM 输入证据 |
| Conversation log | `logs/llm_conversations/*.jsonl` | ignored，真实 LLM 调用记录 |

Trial Ledger 至少记录：

- `template_created`
- `fold_train_start`
- `candidate_instance`
- `instance_frozen`
- `fold_test_result`
- `post_review`

Case Library 只接收已完成 trial 的复盘结果，并必须带 `case_available_at`。

### 5.3 失败条件

以下情况必须失败，不允许静默降级：

- 输入 snapshot、history window、observation 或 evidence 缺失。
- 缺少 PIT 字段或 hash。
- test/held-out 试图调参。
- held-out 结果回流 development。
- LLM response 不符合 schema。
- 真实 provider 调用无法写 conversation log。
- Evidence Pack hash 被篡改。
- sandbox 产物缺失或写到禁止路径。

## 6. 双层 Agent 交接

### 6.1 交接流程

| 步骤 | 交接 | Pipeline 责任 | 产物 |
|---|---|---|---|
| 1 | 外层 Agent -> Pipeline：Template 候选 | 校验 schema、复杂度、数据域、窗口、股票池、动作和证据规则 | accepted 或 rejected Template |
| 2 | Pipeline -> Environment：执行合同 | 把 Agent intent 转成结构化 `history_window_request`、`feature_spec`、选择器规则和交易规则 | snapshot、history window、constraints、manifest |
| 3 | Pipeline -> 内层 Agent：冻结 Template | 只暴露 train snapshot、冻结 Template、工具白名单和参数空间 | Candidate Instance |
| 4 | 内层 Agent -> Pipeline：候选实例 | 校验未越界、未读 test/held-out、artifact hash 可复核 | selected 或 rejected Instance |
| 5 | Pipeline -> Test sandbox：冻结实例 | 冻结参数、prompt、模型、工具策略、snapshot 和代码 hash | replay、LLM memo、fills、metrics、case |

### 6.2 简短案例

案例：`T_MOM_EARN_NEG_001`

外层 Agent 提出：

- 中期动量：60 日收益。
- 流动性：20 日成交额均值。
- 财务改善：最近可见财报的盈利改善。
- 负面文本规避：过去 30 天公告/新闻/研报中检索监管、问询、亏损、减持、诉讼等关键词。
- 月频调仓，允许 `hold/enter/trim/exit/rebalance/event_de_risk`。

Pipeline 转成执行合同：

- 请求 `daily/fundamentals/events/text_evidence` 历史窗口。
- 冻结 `ret_60d`、`amount_mean_20d`、`latest_profitability_change` 三类计算规则。
- 冻结文本检索规则和 action policy。
- 要求所有输入满足 `available_at <= decision_time`。

内层 Agent 只能在训练窗口内选择：

- `top_n`。
- 三类因子的权重。
- 负面文本惩罚系数。
- 动作策略参数。

测试期只执行冻结后的 Instance。若测试日出现负面文本 evidence，LLM 只能输出 memo 或 `event_de_risk` proposal；是否减仓由冻结 action policy、换手预算和 Environment 交易约束决定。

### 6.3 能力开关

| 能力 | 开关 | 需要补齐 |
|---|---|---|
| 动态历史窗口 | `enable_dynamic_windows` | 窗口 schema、预算、泄漏测试 |
| 内层 Agent 调参 | `enable_inner_agent_search` | Candidate Instance schema、seed、预算、optimizer |
| 自然语言打分 | `enable_nl_score` | Evidence Pack、response schema、score merge policy |
| 事件临时决策 | `enable_event_redesign` | event checkpoint policy、临时决策预算 |
| 做 T/库存交易 | `enable_inventory_trade` | 分钟 selector、可卖库存、日内撮合 |
| 融券做空研究 | `enable_short_sleeve` | 券源、费率、担保品、强平线；缺失时只做理论收益 |

未打开的能力只能保留接口和日志字段，不能在测试期隐式生效。

## 7. 验收清单

Pipeline 改动至少检查：

- 只编排，不实现 Agent 决策或 Environment 撮合。
- 所有数据都经 Environment/Data Gateway。
- 外层 Agent 的 Template 进入训练前已校验并冻结。
- 内层 Agent 只在 train 内调参数、权重和阈值。
- Test/held-out 只执行冻结 Instance。
- LLM shadow 默认不影响交易。
- 所有关键输入、输出、hash、metrics 和 case 写入 ledger。
- held-out 结果不能回流 development。
- conversation log、snapshot manifest、Trial Ledger 和 artifacts 能互相追溯。
