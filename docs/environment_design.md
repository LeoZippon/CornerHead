# Environment Design

整理日期：2026-06-07

本文档记录 MacroQuant 的环境层。环境层只回答三件事：

- 在某个决策时点，哪些数据已经可见。
- 在某个交易日，哪些股票可以交易，订单如何成交。
- 一次实验如何留下可复现、可审计的输入和结果。

Agent 如何生成模板见 `docs/agent_design.md`；Pipeline 如何编排训练、测试和日志见 `docs/pipeline_design.md`；数据下载、单位和 raw 审计见 `docs/data_documentation.md`。

## 导航

- [1. 环境层职责](#1-环境层职责)
  - [1.1 负责什么](#11-负责什么)
  - [1.2 不负责什么](#12-不负责什么)
  - [1.3 代码位置](#13-代码位置)
- [2. 时间墙与历史窗口](#2-时间墙与历史窗口)
  - [2.1 基本规则](#21-基本规则)
  - [2.2 历史窗口](#22-历史窗口)
  - [2.3 决策输入](#23-决策输入)
  - [2.4 特殊数据规则](#24-特殊数据规则)
- [3. 数据选择器与股票池](#3-数据选择器与股票池)
  - [3.1 通用规则](#31-通用规则)
  - [3.2 数据选择器](#32-数据选择器)
  - [3.3 股票池](#33-股票池)
  - [3.4 日内数据](#34-日内数据)
- [4. 回放、撮合与评估](#4-回放撮合与评估)
  - [4.1 训练和测试窗口](#41-训练和测试窗口)
  - [4.2 撮合和交易约束](#42-撮合和交易约束)
  - [4.3 事件检查](#43-事件检查)
  - [4.4 评估与账本](#44-评估与账本)
- [5. 数据网关、快照与沙箱](#5-数据网关快照与沙箱)
  - [5.1 数据网关](#51-数据网关)
  - [5.2 只读快照](#52-只读快照)
  - [5.3 沙箱权限](#53-沙箱权限)
  - [5.4 LLM API 代理](#54-llm-api-代理)
- [6. 验收清单](#6-验收清单)

## 1. 环境层职责

### 1.1 负责什么

环境层负责：

- 按 `decision_time` 判断数据是否已经可见。
- 从 raw 或中间层数据构造历史窗口（`history_window`）。
- 从历史窗口构造决策输入（`decision_observation`）。
- 生成每日可交易股票池、交易约束和事件检查结果。
- 执行确定性的回放、撮合、成本、成交和收益统计。
- 生成只读快照，限制沙箱能读什么、能写什么。
- 记录可复现所需的来源、时间、代码和 hash。

### 1.2 不负责什么

环境层不负责：

- 不选择股票。
- 不学习参数。
- 不生成 Agent 模板。
- 不解释自然语言。
- 不构造 LLM prompt。
- 不保存 provider API key。
- 不直接写真实订单。

外层 Agent 负责提出模板；Pipeline 负责校验和冻结模板；环境层只执行 Pipeline 冻结后的结构化合同。环境层不能根据 Agent 的自由文本自行补规则。

### 1.3 代码位置

环境层代码在 `src/hl_trader/environment/`。

| 子模块 | 职责 |
|---|---|
| `data` | 数据合同、日期解析、可见性读取 |
| `features` | 特征和历史窗口构造 |
| `wfo` | 训练/测试窗口切分 |
| `execution` | 订单、成交、现金和持仓 |
| `backtest` | 回放流程 |
| `events` | 事件检查 |
| `portfolio` | 目标权重工具 |
| `evaluation` | 收益、回撤、风险和归因 |
| `protocols` | 冻结合同和结果可见性检查 |
| `storage` | 实验账本和稳定 hash |
| `gateway` | 数据网关和快照清单 |
| `sandbox` | 沙箱启动、资源限制和产物校验 |

导入方向必须保持：

```text
scripts -> pipelines
pipelines -> environment + agent
environment -> 不依赖 agent
agent -> 可以消费 environment 输出
```

`tests/unit/test_protocol_architecture.py` 会阻止 `environment` 反向 import `agent`。

## 2. 时间墙与历史窗口

### 2.1 基本规则

环境层的核心规则是：

```text
available_at <= decision_time
```

含义是：任何数据进入决策前，都必须证明在本次决策时点已经可见。

具体规则：

- 日线、每日指标、涨跌停和停复牌数据，按接口发布时间或盘后/次日盘前规则可见。
- 分钟数据按 bar close 时间可见。
- 财务、事件、宏观和文本数据按公告、发布、报告、采集或保守推断时间可见。
- 除权日、解禁日、事件发生日只能作为事件属性，不能替代公告或发布时间。
- 无法解析发布时间的数据，要么按保守规则延后可见，要么从本次输入中排除。
- 所有输出必须保留来源路径、来源 hash、单位和可见时间规则。

### 2.2 历史窗口

历史窗口（`history_window`）是某个决策时点以前可见的一段数据，不只是行情序列。

| 子集 | 包含内容 | 用途 |
|---|---|---|
| `daily` | 日线、每日指标、涨跌停、停复牌 | 价量、估值、交易约束 |
| `intraday_1min` | 1 分钟线和竞价分钟条 | 日内结构、做 T、打板研究 |
| `fundamentals` | 财报、财务指标、分红、业绩预告/快报、审计意见 | 财务版本和基本面变化 |
| `events` | 资金流、两融、股东、回购、解禁、大宗交易、龙虎榜 | 事件触发和风险控制 |
| `macro` | 宏观、政策、利率、全球事件、指数、外汇 | 市场状态和仓位约束 |
| `text_evidence` | 公告、新闻、研报、政策文本的可见索引 | 关键词检索和 LLM 证据 |

文本在快照中是本地 as-of 文本库。它只包含窗口内已经可见的文本索引、摘要或可审计引用。关键词检索、BM25、证据包和 prompt 细节属于 Agent/Pipeline 文档；环境层只负责时间过滤和来源追溯。

Case Library 不是历史窗口的一部分。它是实验或实盘后的复盘经验库，由 Agent/Pipeline 管理；环境层只在需要时校验 `case_available_at <= outer_agent_decision_time`。

### 2.3 决策输入

决策输入（`decision_observation`）由环境层从历史窗口中计算出来。它必须来自 Pipeline 冻结后的执行合同，不能来自 Agent 自由文本。

| 对象 | 生成方 | 内容 |
|---|---|---|
| `history_window_request` | Pipeline | 决策时间、交易日、阶段、股票池、数据域、最大窗口、权限要求 |
| `history_window` | 数据网关 | 已按时间过滤的数据窗口 |
| `template_execution_spec` | Pipeline | 冻结后的执行合同，包括 `feature_spec`、股票池规则、选择器规则和交易动作规则 |
| `decision_observation` | 环境层 | 根据冻结规则计算出的决策输入和交易约束 |
| `observation_manifest` | 环境层 | 输入 hash、代码 hash、数据状态、单位和行数 |

环境层只执行 `template_execution_spec`。如果执行合同缺字段、引用未授权数据、包含未知算子，或无法证明数据已可见，必须直接失败。

### 2.4 特殊数据规则

财务事件：

- 财务 raw 先构造成 `fundamental_events`。
- 三大报表优先用 `f_ann_date`，没有时用 `ann_date`。
- 财务指标用 `ann_date`。
- 分红优先用 `imp_ann_date`，没有时用 `ann_date`。
- 同一业务键的多版本记录可以保留，但进入决策前必须先过滤可见时间，再选择最新可见版本或保留多行事件。

开盘竞价校正：

- raw 分钟线不改写。
- 如果用 09:30 分钟条近似 `stk_auction`，只在环境层生成校正后的 PIT 字段。
- 深圳主板使用 0.76，创业板使用 0.58；沪市、北交所和收盘竞价保持 1.0。
- 输出字段应带校正规则，便于复核。

日频特征：

- `feature_date` 是来源交易日。
- `tradable_date` 是下一交易日。
- `available_at` 必须晚于当日收盘，且早于下一交易日盘前决策时间。
- 这些规则只适用于日频下一交易日决策；日内策略必须使用分钟级规则。

## 3. 数据选择器与股票池

### 3.1 通用规则

数据选择器（`selector`）负责把 raw 或中间数据变成决策可用的数据。

通用规则：

- 输入只能来自已记录的数据边界。
- 输出必须带 `decision_time`、`tradable_date`、`available_at`、单位、来源路径和来源 hash。
- 先过滤 `available_at <= decision_time`，再做版本选择、聚合或排序。
- 结构性重复业务键不能静默去重，必须扩展键、聚合或保留多行。
- 不稳定字段默认留在 raw/audit 层，例如 `limit_list_d.limit_amount`。
- 每个选择器都要有泄漏测试或 case study。

### 3.2 数据选择器

| 选择器 | 输入 | 输出 | 最低要求 |
|---|---|---|---|
| 日频市场 | 日线、每日指标、涨跌停、停复牌 | 市场状态、交易约束、窗口输入 | 股票覆盖、单位和可见时间可复核 |
| 分钟 | 按日 1 分钟线和竞价分钟条 | 日内输入、撮合输入、事件触发输入 | 不提前读取未来分钟 |
| 财务 | 报表、指标、预告、快报、分红、审计意见、主营业务 | 财务事件和聚合财务观察 | 多版本选择可复现 |
| 事件/资金 | 两融、资金流、股东、回购、大宗交易、解禁、龙虎榜 | 事件序列、风险标签、交易约束 | 重复键处理明确 |
| 宏观/全球 | 宏观、政策、利率、经济日历、指数、外汇 | 市场上下文 | 月度/季度发布滞后不能泄漏 |
| 文本 | 公告、新闻、研报、政策法规、盈利预测 | 可检索文本索引 | 每条证据有 ID、时间和 hash |

### 3.3 股票池

股票池选择器是候选股票的硬边界。Agent 只能在它输出的股票中打分和选择。

输入：

- 冻结后的股票池规则。
- `stock_basic`、`stock_company`、`namechange`。
- `trade_cal`、`suspend_d`、`stk_limit`。
- 历史窗口内的流动性数据。
- 行业、指数成分、黑名单或白名单。

规则：

- 名称变更、ST 状态、行业和指数成分不能使用未来状态。
- 退市、暂停上市、长期停牌和黑名单股票默认排除。
- 流动性阈值只能从历史窗口中计算。
- 停牌和涨跌停既可以作为股票池过滤，也可以作为交易约束，但必须写入冻结策略。
- 空股票池、关键输入缺失或单位不明时，必须失败。

### 3.4 日内数据

日内交易不能复用日频的可见性假设。

| 项目 | 规则 |
|---|---|
| 决策时间 | 每次日内决策必须显式传入 `decision_time` |
| 可见分钟 | 只允许读取 `trade_time <= decision_time` 的分钟条 |
| 日频数据 | 当日盘后才发布的数据不得在盘中使用 |
| 做 T | 必须区分昨日可卖库存和当日买入不可卖库存 |
| 融券做空 | 没有券商券源、费率和担保品数据时，只能作为理论收益参考 |

## 4. 回放、撮合与评估

### 4.1 训练和测试窗口

环境层提供训练/测试窗口切分。

实现位置：

```text
src/hl_trader/environment/wfo/splitter.py
```

规则：

- 训练长度、测试长度和步长由实验配置决定。
- 每个窗口向前滚动。
- `heldout_start` 之后的数据只能由 held-out 流程使用。
- 测试和 held-out 只能执行冻结后的实例，不能调参或修改规则。

### 4.2 撮合和交易约束

撮合和持仓实现位置：

```text
src/hl_trader/environment/execution/
src/hl_trader/environment/backtest/
```

核心对象：

- `Order`：订单。
- `Fill`：成交。
- `PositionLot`：持仓批次。
- `PortfolioState`：现金和持仓。
- `BrokerSimulator`：撮合和约束检查。

交易规则：

- A 股最小交易单位为 100 股。
- T+1 下，买入当日不可卖出。
- 停牌股票不成交。
- 涨停价不能买入，跌停价不能卖出。
- 买入需要现金覆盖名义金额和成本。
- 卖出不能超过可用股数。
- 成本模型包含佣金、印花税和滑点，单位为 bps。
- 做多和理论做空收益分别统计。
- 融券做空缺少券商侧数据时，只能作为理论 short sleeve。

### 4.3 事件检查

事件检查只负责发现事件，不直接改变订单。

实现位置：

```text
src/hl_trader/environment/events/checkpoints.py
```

当前事件：

- `large_price_move`：大幅涨跌。
- `large_amount_spike`：成交额显著放大。
- `price_limit_status`：涨跌停状态。

是否把事件转成 `event_de_risk` 或 `exit`，由 Pipeline 在冻结交易策略下决定。LLM shadow 不能直接触发交易。

### 4.4 评估与账本

评估工具：

- 年化收益。
- 最大回撤。
- Sharpe。
- 基准收益。
- 超额收益。
- 风险暴露。
- 收益归因。
- 做多/理论做空收益拆分。

这些工具不负责选择股票或实验目标。基准、行业分类、风险模型和归因口径必须进入冻结合同，测试期不能临时改变。

账本规则：

- `TrialLedger` 和 `ExperimentLedger` 使用 JSONL。
- 每条记录带稳定 `record_hash`。
- 读取时复核 hash，被手工篡改应失败。
- 账本记录实验事实，不替代真实 broker 成交状态。

冻结合同（`FreezeSpec`）至少记录：

- `experiment_id`
- `track_id`
- `template_id`
- `template_execution_spec_hash`
- `protocol_id`
- `trade_policy_id`
- 模板、协议、交易策略内容 hash
- 模型、prompt、数据合同和代码 hash

## 5. 数据网关、快照与沙箱

### 5.1 数据网关

数据网关（Data Gateway）是时间和权限边界，不是普通文件路径暴露。

输入必须包含：

- `decision_time`
- `tradable_date`
- `fold_id`
- `phase`
- `template_id` 或 `instance_id`
- 数据版本和状态文件 ID

允许输出：

| 输出 | 用途 |
|---|---|
| `market_state` | 指数、流动性、波动和涨跌停结构 |
| `history_window` | 决策前可见的数据窗口 |
| `decision_observation` | 本次决策输入 |
| `text_candidates` | 已按时间过滤的文本候选 |
| `event_checkpoints` | 事件检查结果 |
| `position_state` | 持仓、现金、可用库存和成本 |
| `constraints` | 停牌、涨跌停、T+1、换手和融资融券资格 |

禁止输出：

```text
data/raw 全量路径
held-out 结果
test fold 结果给 train phase
未通过 available_at 过滤的文本
任意 SQL shell
任意 Python 对主机文件系统的访问
```

### 5.2 只读快照

只读快照（as-of snapshot）是给沙箱的物理输入边界。

推荐结构：

```text
data/asof_snapshots/<snapshot_id>/
  manifest.json
  history_window/
    daily.parquet
    intraday_1min.parquet
    fundamentals.parquet
    events.parquet
    macro.parquet
    text_evidence.jsonl
  market_state.parquet
  positions.parquet
  constraints.parquet
  artifacts/
```

`manifest.json` 必须记录：

- `snapshot_id`
- `decision_time` 和 `tradable_date`
- `fold_id` 和 `phase`
- 允许的数据项。
- 来源 hash。
- 可见时间规则。
- 代码提交。
- 数据质量状态 ID。

构造规则：

- 快照不仅按日期过滤，还必须按 `available_at <= decision_time` 过滤。
- TuShare 可能回写历史数据，所以快照必须记录来源 hash 和 revision ledger 状态。
- 沙箱只能以 read-only 方式挂载快照。

### 5.3 沙箱权限

沙箱用于隔离 Agent 生成的分析代码、LLM 推理和实验产物。

| 能力 | Train | Test | Held-out |
|---|---|---|---|
| 读取 train snapshot | 是 | 否 | 否 |
| 读取 test snapshot | 否 | 是 | 否 |
| 读取 held-out snapshot | 否 | 否 | 是 |
| 生成候选实例和参数 | 是 | 否 | 否 |
| 参数搜索 | 是 | 否 | 否 |
| LLM memo | 是 | 是，使用冻结 prompt/model/settings | 是，使用冻结 prompt/model/settings |
| 回放 | 是，用于训练评分 | 是，用于测试结果 | 是，用于冻结验证 |
| 真实订单 | 否 | 否 | 否 |
| 写 artifacts | 是 | 是 | 是 |

沙箱规则：

- 只读挂载快照。
- 只写本次 artifact 目录。
- 默认无网络。
- 如需 LLM，只能访问本地 API 代理。
- API key 不进入沙箱。
- 不能改主仓库。
- 主环境读取 artifact 前必须校验 exit code、hash、manifest 和禁止路径扫描。

### 5.4 LLM API 代理

沙箱内可以实例化 API 驱动的 LLM Agent，但只能通过受控代理调用 provider。

代理负责：

- 在宿主侧保存 API key。
- 限定 provider、model 和 endpoint。
- 记录 request、response、usage、错误和 hash。
- 控制 token 和调用预算。
- 按 prompt/evidence/schema hash 做可选缓存。
- 脱敏 Authorization、header 和 key。
- 不提供互联网搜索。

## 6. 验收清单

每完成一个环境边界，至少检查：

- 文档：本文件与 Agent/Pipeline/Data 文档边界一致。
- 单元测试：覆盖时间墙、选择器输出、缺失值、重复键、单位和沙箱权限拒绝。
- Case study：至少 3 个股票/日期样例，验证股票池、历史窗口、选择器和决策输入。
- 可复现性：输出包含数据状态、来源 hash、代码提交、执行合同 hash 和工具调用 hash。
- 泄漏审计：未来财报、未来新闻、未来分钟条、未来 benchmark 成分不可见。
- 运行审计：Trial Ledger、conversation log、sandbox artifact 和 evaluation report 能互相追溯。
