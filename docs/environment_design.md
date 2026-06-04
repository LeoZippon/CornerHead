# Environment Design

整理日期：2026-06-03

本文档记录 MacroQuant 的量化环境层：PIT 数据可见性、市场状态、回放、撮合、交易约束、事件检查、WFO fold、配置合同和可审计 ledger 原语。Agent 决策逻辑见 `docs/agent_design.md`；Pipeline 编排流程见 `docs/pipeline_design.md`；数据下载和 raw 审计见 `docs/data_documentation.md`。

## 导航

- [1. 边界原则](#1-边界原则)
  - [1.1 职责范围](#11-职责范围)
  - [1.2 导入方向](#12-导入方向)
- [2. 代码组织](#2-代码组织)
  - [2.1 子模块职责](#21-子模块职责)
- [3. 配置合同](#3-配置合同)
  - [3.1 ExperimentConfig 对象](#31-experimentconfig-对象)
  - [3.2 核心校验](#32-核心校验)
- [4. PIT 数据读取](#4-pit-数据读取)
  - [4.1 数据合同与读取器](#41-数据合同与读取器)
  - [4.2 当前日频可见性](#42-当前日频可见性)
- [5. PIT 特征构造](#5-pit-特征构造)
  - [5.1 日频特征构造](#51-日频特征构造)
  - [5.2 财务事件 PIT 层](#52-财务事件-pit-层)
  - [5.3 竞价分钟条校正](#53-竞价分钟条校正)
- [6. 泄漏检查](#6-泄漏检查)
  - [6.1 日频泄漏规则](#61-日频泄漏规则)
- [7. 跨域 PIT Selector](#7-跨域-pit-selector)
  - [7.1 Selector 规则](#71-selector-规则)
  - [7.2 当前扩展边界](#72-当前扩展边界)
- [8. WFO Fold](#8-wfo-fold)
  - [8.1 Fold 生成规则](#81-fold-生成规则)
  - [8.2 Pilot 配置示例](#82-pilot-配置示例)
- [9. 执行环境](#9-执行环境)
  - [9.1 Broker 对象](#91-broker-对象)
  - [9.2 交易约束](#92-交易约束)
- [10. 回放环境](#10-回放环境)
  - [10.1 Replay 骨架](#101-replay-骨架)
  - [10.2 当前使用边界](#102-当前使用边界)
- [11. 事件检查](#11-事件检查)
  - [11.1 Checkpoint 定义](#111-checkpoint-定义)
  - [11.2 交易转换边界](#112-交易转换边界)
- [12. Portfolio 和 Evaluation](#12-portfolio-和-evaluation)
  - [12.1 Portfolio 工具](#121-portfolio-工具)
  - [12.2 Evaluation 工具](#122-evaluation-工具)
- [13. Protocol 和 Ledger](#13-protocol-和-ledger)
  - [13.1 FreezeSpec](#131-freezespec)
  - [13.2 Ledger](#132-ledger)
- [14. 待实现环境边界](#14-待实现环境边界)

## 1. 边界原则

Environment 回答的是：“在某个决策时点，市场环境能给决策者看到什么、能成交什么、如何记录状态。”

### 1.1 职责范围

Environment 负责：

- 从 raw 数据读取和构造 PIT feature。
- 检查 feature 是否存在时间泄漏。
- 提供交易日、rolling fold、held-out 边界和结果可见性 guard。
- 提供回放、BrokerSimulator、订单、成交、组合状态和交易约束。
- 提供 deterministic event checkpoint。
- 提供 portfolio target、evaluation metric、TrialLedger 和 ExperimentLedger 原语。

Environment 不负责：

- 不选择股票、不学习参数、不决定 action。
- 不调用 LLM，不构造 prompt，不解释自然语言。
- 不直接运行 development/held-out/LLM shadow pipeline。
- 不读取 Agent 输出作为订单；订单只能由 pipeline 在冻结策略下交给执行环境。

### 1.2 导入方向

导入方向固定：

```text
environment -> 不依赖 agent
agent -> 可以消费 environment 产出的 PIT feature、checkpoint、ledger 基础能力
pipelines -> 可以同时组合 environment 和 agent
scripts -> 只做 CLI 参数和 pipeline 调度
```

`tests/unit/test_protocol_architecture.py` 会阻止 `environment` 反向 import `agent`。

## 2. 代码组织

环境层代码集中在 `src/hl_trader/environment/`。

### 2.1 子模块职责

| 子模块 | 职责 |
|---|---|
| `data` | TuShare 数据合同、PIT 分区读取、日期解析 |
| `features` | 日频 PIT 特征构造、历史竞价分钟条校正 |
| `leakage` | 特征层时间泄漏检查 |
| `wfo` | rolling fold 生成 |
| `backtest` | 日频 replay 骨架 |
| `execution` | BrokerSimulator、Order、Fill、PortfolioState、Position |
| `events` | deterministic event checkpoint 检测 |
| `portfolio` | 目标权重和归一化工具 |
| `evaluation` | 收益、长/短拆分、回撤、Sharpe 等指标 |
| `protocols` | FreezeSpec、development/held-out 边界、结果可见性 guard |
| `schemas` | HorizonTrack、Protocol、TradeStrategyPolicy、HeuristicTemplate、ExperimentConfig |
| `storage` | TrialLedger、ExperimentLedger、稳定 hash、UTC 时间 |

## 3. 配置合同

示例配置：`configs/experiments/pilot_2020_daily.yaml`。

### 3.1 ExperimentConfig 对象

`ExperimentConfig` 包含 5 个核心对象：

| 对象 | 关键字段 | 用途 |
|---|---|---|
| `HorizonTrack` | `target_holding_months`、`train_length_months`、`test_length_months`、`step_months` | 研究周期和 rolling fold 步长 |
| `Protocol` | `start_date`、`end_date`、`heldout_start`、`decision_anchor`、`rebalance_frequency`、`nl_weight`、`cost_model` | 实验时间、held-out 边界、调仓频率、成本 |
| `TradeStrategyPolicy` | `data_granularity`、`settlement_mode`、`max_daily_turnover_pct`、`event_de_risk_pct`、`event_exit_loss_pct`、`allowed_actions` | 可交易动作和执行约束 |
| `HeuristicTemplate` | `strategy_family`、`variable_families`、`parameter_space`、`objective` | Agent 参数搜索空间的配置记录 |
| `universe` | 交易所、ST、上市天数、流动性阈值等 | 当前为配置记录，后续接入 universe selector |

### 3.2 核心校验

核心校验：

- `target_holding_months`、训练长度、测试长度和 step 必须为正。
- 训练长度至少覆盖一个目标持有周期。
- `heldout_start` 必须在 protocol 时间范围内。
- Development 阶段 `nl_weight` 必须为 `0.0`。
- 当前初始实验配置从 2020 年以后开始；更早窗口需要先扩展和审计特征合同。
- `TradeStrategyPolicy.allowed_actions` 不能为空。

## 4. PIT 数据读取

### 4.1 数据合同与读取器

实现：

- `src/hl_trader/environment/data/contracts.py`
- `src/hl_trader/environment/data/pit.py`

`DatasetContract` 定义数据项的可见性规则。`PITDataStore` 负责从 `data/raw/<dataset>/trade_date=<YYYYMMDD>.parquet` 读取分区，并提供基础可见性检查。

Data 文档只定义 raw 下载、单位、sidecar 和可见时间候选；Environment 才负责在 `decision_time` 下选择“此刻可见”的记录，并构造 feature、observation 或 event checkpoint。

### 4.2 当前日频可见性

当前日频主路径默认遵循：

- `daily` 和 `daily_basic` 只能用于当日收盘后或下一交易日决策。
- 分钟数据可见性应使用 bar close 时间。
- 财务、事件、宏观、文本要先经过对应 selector，不能 raw join。

完整 raw 数据规则见 `docs/data_documentation.md`。

## 5. PIT 特征构造

### 5.1 日频特征构造

入口由 Pipeline/CLI 调用，环境层实现为：

```text
src/hl_trader/environment/features/daily_pit.py::DailyPITFeatureBuilder
```

当前输入：

- `daily`
- `daily_basic`
- `stk_limit`
- `suspend_d`
- 可选 `limit_list_d`
- 可选 `fundamental_events`

当前输出：

```text
data/features/daily_alpha/feature_date=<YYYYMMDD>.parquet
```

构造逻辑：

- 读取窗口向前扩展 `lookback_days`。
- `daily`、`daily_basic`、`stk_limit` 的 `(trade_date, ts_code)` 必须唯一。
- 数值字段显式转 numeric。
- `ret_1d = pct_chg / 100`；缺少 `pct_chg` 时才用 close pct change。
- `ret_5d`、`ret_20d`、`ret_60d` 为 trailing 复合收益。
- `amount_ma20` 为 `daily.amount` 的 20 日滚动均值，单位仍为千元。
- `volatility_20d` 为 `ret_1d` 的 20 日滚动标准差。
- `is_suspended` 来自 `suspend_d`。
- 涨跌停价格来自 `stk_limit`。
- `limit_list_d` 只允许 `limit` 进入 `daily_alpha`；`limit_amount` 因历史源端回写不稳定被隔离在 raw/audit 层，不作为特征字段。
- 如果传入 `fundamental_events_dir`，按 `available_at <= feature available_at` 选择最新可见财务指标和分红事件，生成 `fund_*`、`dividend_*` 字段。
- `feature_date = source_trade_date = trade_date`。
- `tradable_date = 下一交易日`；优先用 SSE `trade_cal` 映射，因此最后一个已落库日线分区也可以映射到次日盘前交易日；缺少交易日历时才回退到 `daily` 分区序列，没有下一交易日的末尾样本丢弃。
- `available_at` 和 `result_available_time` 使用日频合同的收盘后可见时间。
- 分区写入采用临时文件替换，避免下游读取半成品。

### 5.2 财务事件 PIT 层

实现：

```text
src/hl_trader/environment/features/fundamental_events.py
```

`FundamentalEventsBuilder` 从 `data/raw` 的财务与基本面 raw 文件构造 PIT-ready 事件层：

```text
data/features/fundamental_events/<dataset>/available_month=<YYYYMM>.parquet
```

构造规则：

- raw 层不改写：报表仍按 `period`，预告/快报按 `ann_month`，分红/审计意见/主营业务构成按 `ts_code` 快照。
- 输出层统一带 `dataset`、`ts_code`、`available_at`、`available_at_rule`、`available_month`、`business_key`、`source_path`、`source_hash`。
- 三大报表优先用 `f_ann_date`，否则用 `ann_date`；财务指标用 `ann_date`。
- 业绩预告和业绩快报优先用 `first_ann_date`，否则用 `ann_date`。
- 分红优先用 `imp_ann_date`，否则用 `ann_date`；如果二者均缺失，该行不进入 PIT 事件层，`ex_date/record_date/pay_date` 只作为已可见分红事件的未来属性。
- 审计意见和主营业务构成缺少公告日时，可用同股票同报告期报表的最晚可见时间兜底。
- 同一业务键多版本记录保留在事件层，具体 feature 或 evidence 选择时再按 `available_at <= decision_time` 取最新可见版本。
- 写入 `available_month` 分区时，完整月份窗口使用 replace 语义以清理源端删除或改期后的旧事件；非完整月份窗口使用 merge 语义，避免短窗口构造误删同月其他事件。

审计入口：

```bash
PYTHONPATH=src ~/miniconda3/bin/conda run -n stock python scripts/hl.py audit-fundamental-events \
  --events-root data/features/fundamental_events \
  --start-date 20200101 \
  --end-date 20260531
```

审计检查分区存在、必需字段、`available_at` 可解析性、`available_month` 与文件分区一致性、审计窗口内外行、行内 `dataset` 与路径一致性、`available_at_rule` allowlist、`source_path/source_hash/source_row_id` 来源可追溯性、重复 `dataset/business_key/available_at`。人工审计下空分区是 warning；cron 在接入 `daily_alpha` 前会额外传 `--require-partitions`，目标窗口没有任何 PIT 事件行时直接 error 并停止后续 feature build。该审计针对 PIT-ready 事件层，不替代 raw 层 `base_research_status.json`。

### 5.3 竞价分钟条校正

实现：

```text
src/hl_trader/environment/features/auction.py
```

历史分钟线 raw 文件不改写。若历史回放需要用 `stk_mins_1min_by_date` 的 `09:30` 分钟条近似实盘 `stk_auction`，先调用 `apply_open_auction_correction`，生成 `vol_pit`、`amount_pit`、`auction_market_bucket` 和校正规则字段，再用这些 PIT 列构造开盘竞价换手、量比、竞价额等特征。

当前规则：

- 只作用于 `09:30` 分钟条。
- `00*.SZ` 使用 `0.76`，`30*.SZ` 使用 `0.58`。
- 沪市、北交所、其他代码和 `15:00` 收盘竞价保持 `1.0`。
- raw `vol/amount` 保持 TuShare 原值；修正值仅用于需要与实盘 `stk_auction` 对齐的历史特征。
- 当前交叉检验发现：深圳 `00*.SZ` 的 09:30 分钟条相对 `stk_auction` 中位约 `1.32`，深圳 `30*.SZ` 中位约 `1.72`，修正后中位回到约 `1.0`；沪市和北交所约 `1.0`，无需修正。
- 全天分钟线汇总与 `daily` 的单位换算正常：`sum(stk_mins.vol) / daily.vol` 约 `100`，`sum(stk_mins.amount) / daily.amount` 约 `1000`，分别对应“股 vs 手”和“元 vs 千元”。
- 系数需要通过 `scripts/tushare/audit.py auction-alignment` 定期复核；若后续接入逐笔或盘口数据，应重新标定或放弃固定系数。

## 6. 泄漏检查

### 6.1 日频泄漏规则

实现：

```text
src/hl_trader/environment/leakage/checks.py
```

通用检查：

- 必需字段：`feature_date`、`tradable_date`、`available_at`、`ts_code`。
- `(feature_date, ts_code)` 必须唯一。
- `tradable_date` 必须严格晚于 `feature_date`。
- `available_at` 必须不早于 `feature_date 15:00 Asia/Shanghai`。
- `available_at` 必须早于 `tradable_date 09:25 Asia/Shanghai`。
- 如果存在 `source_trade_date`，必须满足 `source_trade_date <= feature_date`。

这些规则只证明当前日频下一交易日决策无泄漏；日内策略必须用分钟级 `available_at <= decision_time` 重新定义。

## 7. 跨域 PIT Selector

后续将财务、事件、宏观、文本和分钟数据接入 observation 时，统一放在 Environment selector，而不是在 Agent 或 Pipeline 中 raw join。

### 7.1 Selector 规则

Selector 规则：

- 输入只能来自 `data/raw` 的保留边界和 `docs/data_documentation.md` 定义的 raw PIT 数据合同。
- 输出必须带 `feature_date` 或 `decision_time`、`available_at`、`source_*` 时间字段、单位信息和源数据 hash。
- 同一业务键多版本数据必须先过滤 `available_at <= decision_time`，再选择最新可见版本。
- 事件生效日只能作为未来属性暴露，不能作为该事件的可见时间。
- 文本进入 Agent 前必须先通过 Environment 的时间过滤，再由 Agent evidence pack 生成 `evidence_id` 和 prompt payload。
- 每个 selector 都必须有泄漏测试或审计 case study，证明未来日期不会提前进入 observation。

### 7.2 当前扩展边界

当前扩展边界：

- 财务 selector：使用 `fundamental_events` 的 `available_at`，按 `ts_code + business_key` 或具体特征业务键选择最新可见版本。
- 事件 selector：分红、解禁、回购、股东事件用公告日期控制可见性，事件生效日只作为未来事件字段。
- 资金 selector：资金流、两融和大宗交易使用审计中的盘后或下一日可见规则。
- 宏观 selector：先使用 raw 保守可见时间，后续优先用 `cn_schedule.publish_date` 或更精确发布时间修正。
- 分钟 selector：使用分钟 bar close 时间，日内策略必须以 `available_at <= decision_time` 过滤。
- 文本 selector：只输出时间过滤后的候选 evidence 元数据，正文截断、hash 复核和 prompt 合同属于 Agent。

## 8. WFO Fold

### 8.1 Fold 生成规则

实现：

```text
src/hl_trader/environment/wfo/splitter.py
```

`generate_rolling_folds` 只负责生成 fold，不拟合、不调仓、不评估。

规则：

- 从 `protocol.start_date` 开始。
- 训练窗口长度为 `track.train_length_months`。
- 测试窗口长度为 `track.test_length_months`。
- 每次向前移动 `track.step_months`。
- `development_folds` 会在 `heldout_start` 前截断。

### 8.2 Pilot 配置示例

以当前 pilot 配置为例：

- 训练 36 个月。
- 测试 6 个月。
- 每 3 个月滚动一次。
- `heldout_start=2025-01-01`，所以 2025 年以后只允许由 held-out pipeline 评估。

## 9. 执行环境

### 9.1 Broker 对象

实现：

```text
src/hl_trader/environment/execution/broker.py
```

核心对象：

- `Order`：交易日期、代码、方向、股数、reason。
- `Fill`：成交记录。
- `Position`：持仓股数和可用股数。
- `PortfolioState`：现金和持仓。
- `BrokerSimulator`：撮合和约束检查。

### 9.2 交易约束

交易约束：

- 当前只支持 long-only 股票订单。
- A 股 lot 为 100 股。
- T+1 下，买入当日不可卖出；每日开始前调用 `settle_t_plus_1`。
- 停牌股票不成交。
- 买入达到涨停约束时阻断。
- 卖出达到跌停约束时阻断。
- 买入需要现金覆盖名义金额和买入成本。
- 卖出不能超过可用股数。
- 成本模型包含佣金、印花税和滑点，单位为 bps。

## 10. 回放环境

### 10.1 Replay 骨架

实现：

```text
src/hl_trader/environment/backtest/daily_replay.py
```

`DailyReplayEngine` 是日频 replay 骨架，负责：

- 按日期顺序执行决策函数生成的订单。
- 调用 BrokerSimulator 撮合。
- 记录成交、现金、持仓和权益事件。
- 要求 replay 日期单调递增。

### 10.2 当前使用边界

当前完整 development/held-out 流程不直接使用该骨架，而是在 `pipelines/formulaic_wfo.py` 中围绕 PIT 横截面实现了更具体的日频回放。

## 11. 事件检查

### 11.1 Checkpoint 定义

实现：

```text
src/hl_trader/environment/events/checkpoints.py
```

当前 deterministic checkpoint：

- `large_price_move`：`abs(pct_chg) >= 9.5`，`pct_chg` 使用 TuShare 百分比口径。
- `large_amount_spike`：`amount / amount_ma20 >= 3.0`，金额沿用日线千元口径。
- `price_limit_status`：来自 `limit_status` 或 `limit`。

### 11.2 交易转换边界

Environment 只检测 checkpoint。是否把 checkpoint 转为 `event_de_risk` 或 `exit`，由 Pipeline 在冻结 `TradeStrategyPolicy` 下执行；LLM shadow 不得触发交易。

## 12. Portfolio 和 Evaluation

### 12.1 Portfolio 工具

Portfolio 工具：

- `equal_weight_targets(selected, max_names=...)`
- `normalize_targets(targets, max_weight=...)`

### 12.2 Evaluation 工具

Evaluation 工具：

- `annualized_return`
- `max_drawdown`
- `sharpe_ratio`
- `theoretical_short_return`
- `long_short_return_breakdown`

`theoretical_short_return` 按入场价、退出价、持有天数、现金担保比例和年化融券费率计算理论做空收益。默认假设是 100% 现金担保、18% 年化融券费率，其中 18% 来自中信证券[融资融券费用公示](https://pb.citics.com/trading/xxgs/fy/)的融券费率参考值。该函数只用于研究侧收益拆分，不表示券商实际可融券源、担保品折算、强平线或集中度规则已经可执行；中信证券[维持担保比例要求](https://pb.citics.com/trading/xxgs/wcdbbl/)的普通平仓线/安全线/提取线可作为未来风控参数参考，但当前没有接入执行模型。

这些都是原语，不负责决定候选股票或实验目标。

## 13. Protocol 和 Ledger

### 13.1 FreezeSpec

`FreezeSpec` 覆盖：

- `experiment_id`
- `track_id`
- `template_id`
- `protocol_id`
- `trade_policy_id`
- track/template/protocol/trade_policy 内容 hash
- `horizon_months`
- `model_id`
- `prompt_id`
- `data_contract_id`

`assert_result_available` 用于确认训练窗口内的 `result_available_time` 不晚于训练结束。

### 13.2 Ledger

`TrialLedger`：

- JSONL append-only。
- 每条记录写入 `record_hash`。
- 读取时复核 hash，被手工篡改应失败。

`ExperimentLedger`：

- 在每条实验事件上注入 freeze context。
- 保留 phase、fold_id、parameters、metrics、payload。
- 不替代真实 broker 成交状态。

## 14. 待实现环境边界

- `universe` 配置尚未系统性接入股票池 selector。
- 财务 raw 已下载并审计，`fundamental_events` 已可构造并可选进入 `daily_alpha`。
- 宏观、全球、文本和分钟数据尚未进入默认公式化特征。
- 日内交易 track 需要单独使用分钟级 `available_at <= decision_time` 的 PIT 过滤规则。
- Benchmark、行业中性、风险暴露和超额收益归因需要补充环境/评估原语。
