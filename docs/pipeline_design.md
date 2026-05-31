# Pipeline Design

整理日期：2026-05-31

本文档记录 MacroQuant 的运行编排层：CLI 如何调用 feature build、development WFO、held-out control、LLM shadow，以及 Pipeline 如何组合 Environment 和 Agent。Environment 原语见 `docs/environment_design.md`；Agent 决策逻辑见 `docs/agent_design.md`；数据下载和审计见 `docs/data_documentation.md`；QMT 执行端见 `docs/QMT_documentation.md`。

## 导航

- [1. 边界原则](#1-边界原则)
  - [1.1 职责范围](#11-职责范围)
  - [1.2 导入方向](#12-导入方向)
- [2. 代码组织](#2-代码组织)
  - [2.1 文件职责](#21-文件职责)
- [3. CLI 映射](#3-cli-映射)
  - [3.1 子命令](#31-子命令)
  - [3.2 CLI 规则](#32-cli-规则)
- [4. Feature Build Pipeline](#4-feature-build-pipeline)
  - [4.1 命令入口](#41-命令入口)
  - [4.2 当前流程](#42-当前流程)
  - [4.3 输出约束](#43-输出约束)
- [5. Development WFO Pipeline](#5-development-wfo-pipeline)
  - [5.1 命令入口与实现](#51-命令入口与实现)
  - [5.2 运行流程](#52-运行流程)
  - [5.3 训练阶段](#53-训练阶段)
  - [5.4 测试、调仓与事件动作](#54-测试调仓与事件动作)
- [6. Held-Out Control Pipeline](#6-held-out-control-pipeline)
  - [6.1 命令入口](#61-命令入口)
  - [6.2 规则](#62-规则)
- [7. LLM Shadow Pipeline](#7-llm-shadow-pipeline)
  - [7.1 构造 Evidence Pack](#71-构造-evidence-pack)
  - [7.2 调用 Provider](#72-调用-provider)
  - [7.3 Feature 到 Evidence 流程](#73-feature-到-evidence-流程)
  - [7.4 Dry-run 与真实 Shadow](#74-dry-run-与真实-shadow)
- [8. Ledger 和输出路径](#8-ledger-和输出路径)
  - [8.1 本地输出](#81-本地输出)
- [9. Freeze 和可复现](#9-freeze-和可复现)
  - [9.1 Freeze 字段](#91-freeze-字段)
- [10. Fail-Fast 规则](#10-fail-fast-规则)
  - [10.1 失败条件](#101-失败条件)
- [11. 后续 Pipeline 扩展](#11-后续-pipeline-扩展)
  - [11.1 扩展方向](#111-扩展方向)

## 1. 边界原则

Pipeline 回答的是：“按照哪套冻结配置、输入、输出和审计规则，把 Environment 与 Agent 串起来运行一次实验或 shadow 流程。”

### 1.1 职责范围

Pipeline 负责：

- 解析实验配置和运行参数。
- 调用 Environment 构造 PIT feature。
- 调用 Agent 生成候选股或 shadow decision。
- 调用 Environment 回放、撮合、事件检查和 ledger。
- 管理 development/held-out 边界。
- 写入可审计 JSONL ledger。
- 保证 shadow-only 与可交易执行边界不混淆。

Pipeline 不负责：

- 不定义 raw 数据下载策略；那属于 `src/hl_trader/data_sources/tushare/` 的数据源实现、`scripts/tushare/` 命令入口和 `docs/data_documentation.md`。
- 不直接实现 broker 约束；那属于 Environment。
- 不直接实现 prompt/provider/response 合同；那属于 Agent。
- 不绕过 PIT feature 或 evidence pack 读取 raw 数据。

### 1.2 导入方向

导入方向：

```text
scripts/hl.py -> pipelines
pipelines -> environment + agent
environment -> 不依赖 agent
agent -> 不拥有 broker state
```

## 2. 代码组织

### 2.1 文件职责

| 文件 | 职责 |
|---|---|
| `scripts/hl.py` | 单一 HL CLI 入口；只解析参数、调用 pipeline 或环境 feature builder |
| `src/hl_trader/pipelines/experiment.py` | development WFO 和 held-out control runner |
| `src/hl_trader/pipelines/formulaic_wfo.py` | 公式化 Agent + Environment 回放的 WFO 执行编排 |
| `src/hl_trader/pipelines/llm_shadow.py` | evidence pack 构造、LLM shadow dry-run/真实调用编排 |

`build-features` 由 `scripts/hl.py` 调用 `DailyPITFeatureBuilder`，负责 raw 数据到 PIT feature 的构造。

## 3. CLI 映射

`scripts/hl.py` 当前提供四个子命令：

### 3.1 子命令

| CLI | 运行边界 | 当前写入 |
|---|---|---|
| `build-features` | raw -> PIT feature | `data/features/<dataset>/feature_date=<YYYYMMDD>.parquet` |
| `run-development` | held-out 前 rolling WFO | experiment ledger |
| `run-heldout` | 冻结参数 held-out control | held-out ledger |
| `llm-shadow` | evidence pack -> shadow decision | evidence JSONL、shadow ledger |

### 3.2 CLI 规则

CLI 规则：

- 所有命令通过 `PYTHONPATH=src ~/miniconda3/bin/conda run -n stock python scripts/hl.py ...` 运行。
- `--ledger-path` 可覆盖 YAML 默认 ledger，正式 smoke、development、held-out 不应混写到同一 JSONL。
- CLI 捕获异常后输出 JSON error 并返回非 0。
- CLI 输出使用 `to_jsonable` 序列化 dataclass、日期和 numpy/pandas 类型。

## 4. Feature Build Pipeline

### 4.1 命令入口

入口：

```bash
PYTHONUNBUFFERED=1 PYTHONPATH=src ~/miniconda3/bin/conda run -n stock python scripts/hl.py build-features \
  --raw-dir data/raw \
  --output-root data/features \
  --dataset daily_alpha \
  --start-date 20200102 \
  --end-date 20251231
```

### 4.2 当前流程

当前流程：

1. `scripts/hl.py` 解析 `FeatureBuildConfig`。
2. 调用 `DailyPITFeatureBuilder(args.raw_dir)`。
3. 从 raw 日频分区读取 `daily`、`daily_basic`、`stk_limit`、`suspend_d`、可选 `limit_list_d`。
4. 构造下一交易日可交易的 `daily_alpha`。
5. 按 `feature_date` 分区写入 `data/features/<dataset>/`。
6. 返回行数、分区数、首尾分区路径。

### 4.3 输出约束

输出约束：

- 输出是 feature layer，不是 raw layer。
- 写入前不应绕过 Environment 的 PIT 构造和泄漏检查。
- 后续财务、宏观、事件、分钟、文本接入时，应扩展 Environment selector，再由 Pipeline 调用。

## 5. Development WFO Pipeline

### 5.1 命令入口与实现

入口：

```bash
PYTHONUNBUFFERED=1 PYTHONPATH=src ~/miniconda3/bin/conda run -n stock python scripts/hl.py run-development \
  --config configs/experiments/pilot_2020_daily.yaml \
  --features data/features/daily_alpha \
  --ledger-path experiments/trial_ledger/<development_run_id>.jsonl
```

实现：

- `DailyFormulaicExperimentRunner`
- `FormulaicWfoRunner`

### 5.2 运行流程

运行流程：

1. 读取 `ExperimentConfig`。
2. 根据配置生成 `FreezeSpec`。
3. 生成 held-out 前的 development folds。
4. 从 `template.parameter_space` 调用 Agent 的 `parameter_grid`。
5. 写入 `experiment_start`。
6. 对每个 fold：
   - 写入 `fold_start`。
   - `fit_parameters` 在训练窗口选择参数。
   - `run_fold` 在测试窗口运行冻结参数回放。
   - 写入 `fold_result`。
7. 汇总所有 fold，写入 `experiment_result`。

### 5.3 训练阶段

训练阶段：

- `assert_result_available` 要求训练特征有 `result_available_time`，且不晚于训练结束。
- 调仓日期为每个月最后一个 `feature_date`。
- 每组参数在连续月末决策点之间计算入选股票的实现收益均值。
- 没有足够样本时得分为负无穷；全部无效时归零。

### 5.4 测试、调仓与事件动作

测试阶段：

- 每日遍历测试窗口内 `feature_date`。
- 非月末日只处理 event checkpoint。
- 月末日执行常规 rebalance。
- 事件动作和常规调仓共用每日换手预算。
- 事件触发 `event_de_risk` 或 `exit` 的股票会从当日候选中排除。

常规调仓：

- Agent 输出候选股。
- Environment portfolio 工具生成等权目标。
- Pipeline 生成 `enter/add/trim/exit` order reason。
- Environment BrokerSimulator 执行 T+1、lot、涨跌停、停牌、现金和成本约束。

事件动作：

- Environment `CheckpointDetector` 发现 checkpoint。
- Pipeline 先写 `event_checkpoint`。
- 若冻结 `TradeStrategyPolicy` 允许：
  - 负向大幅价格变化触发 `event_de_risk` 或 `exit`。
  - 跌停状态触发 `exit` 或 `event_de_risk`，但实际成交仍受跌停约束。
- Pipeline 写 `event_action` 和 `fill`。
- LLM shadow 不能触发这些动作。

## 6. Held-Out Control Pipeline

### 6.1 命令入口

入口：

```bash
PYTHONUNBUFFERED=1 PYTHONPATH=src ~/miniconda3/bin/conda run -n stock python scripts/hl.py run-heldout \
  --config configs/experiments/pilot_2020_daily.yaml \
  --features data/features/daily_alpha \
  --ledger-path experiments/trial_ledger/<heldout_run_id>.jsonl \
  --top-n 80 \
  --max-pe-ttm-quantile 0.2 \
  --max-pb-quantile 0.2 \
  --min-amount-quantile 0.2 \
  --model-id formulaic_mode_control \
  --treatment control_formulaic_mode
```

### 6.2 规则

规则：

- 必须配置 `heldout_start`。
- 参数由命令行显式传入。
- Held-out 内不拟合、不搜索、不写回参数。
- fold 固定为：
  - `train_start=protocol.start_date`
  - `train_end=heldout_start-1`
  - `test_start=heldout_start`
  - `test_end=protocol.end_date`
- 写入 `heldout_start` 和 `heldout_result`。
- `treatment`、`model_id`、`prompt_id`、`data_contract_id` 进入 freeze context。

Held-out 结果只能用于冻结方案验证，不应反向修改 development 搜索逻辑。

## 7. LLM Shadow Pipeline

### 7.1 构造 Evidence Pack

入口：从 feature file 构造 evidence pack 并 dry-run。

```bash
PYTHONPATH=src ~/miniconda3/bin/conda run -n stock python scripts/hl.py llm-shadow \
  --feature-file data/features/daily_alpha/feature_date=<YYYYMMDD>.parquet \
  --decision-date <YYYYMMDD> \
  --tradable-date <YYYYMMDD> \
  --ts-code <TS_CODE> \
  --evidence-out data/evidence_packs/llm_shadow.jsonl \
  --shadow-ledger experiments/trial_ledger/llm_shadow.jsonl \
  --dry-run
```

### 7.2 调用 Provider

入口：从已有 evidence pack 调用 provider。

```bash
PYTHONPATH=src ~/miniconda3/bin/conda run -n stock python scripts/hl.py llm-shadow \
  --provider deepseek \
  --evidence-jsonl data/evidence_packs/llm_shadow.jsonl \
  --shadow-ledger experiments/trial_ledger/llm_shadow.jsonl \
  --max-packs 1
```

### 7.3 Feature 到 Evidence 流程

Feature file -> evidence pack 流程：

1. 读取 Parquet、CSV、JSON 或 JSONL。
2. 校验必需 PIT 字段：
   - `feature_date`
   - `source_trade_date`
   - `tradable_date`
   - `available_at`
   - `ts_code`
3. 按 `decision_date`、`tradable_date`、`ts_code` 选择横截面行。
4. 调用 Agent `EvidencePackBuilder`。
5. 可选写入 `--evidence-out`。
6. 调用 Environment `CheckpointDetector` 生成 shadow 上下文。

默认纳入 payload 的特征列：

- `pe_ttm`
- `pb`
- `pct_chg`
- `amount`
- `amount_ma20`
- `ret_20d`

### 7.4 Dry-run 与真实 Shadow

Dry-run：

- 不读取 provider API key。
- 不调用网络。
- 校验 pack hash、PIT 字段、checkpoint 和 ledger 写入链路。
- 写入 `llm_shadow_dry_run`。

真实 provider shadow：

- 当前 provider 只开放 `deepseek`。
- API key 只从环境变量或 ignored `.env` 读取。
- 调用 Agent `LLMShadowAdvisor`。
- 写入每只股票的 `nl_shadow_decision`。
- 写入每个 pack 的 `llm_shadow_pack`。
- 所有记录保持 `can_affect_trading=False`。

## 8. Ledger 和输出路径

### 8.1 本地输出

常用本地输出：

| 输出 | 路径 | 说明 |
|---|---|---|
| PIT feature | `data/features/daily_alpha/` | ignored，本地可读 |
| Development ledger | `experiments/trial_ledger/<development_run_id>.jsonl` | ignored，实验审计 |
| Held-out ledger | `experiments/trial_ledger/<heldout_run_id>.jsonl` | ignored，冻结验证 |
| Evidence pack | `data/evidence_packs/llm_shadow.jsonl` | ignored，LLM 输入证据 |
| Shadow ledger | `experiments/trial_ledger/llm_shadow.jsonl` | ignored，LLM shadow 审计 |

正式运行应使用不同 ledger path，避免 smoke、development、held-out 和 shadow 混写。

## 9. Freeze 和可复现

### 9.1 Freeze 字段

Pipeline 必须保留：

- `FreezeSpec`
- `freeze_hash`
- track/template/protocol/trade_policy 内容 hash
- `model_id`
- `prompt_id`
- `data_contract_id`
- phase：development 或 heldout
- fold_id、parameters、metrics、payload
- TrialLedger `record_hash`

Development runner 每个 fold 前会重新生成 freeze spec 并检查配置未漂移。

## 10. Fail-Fast 规则

### 10.1 失败条件

Pipeline 应失败而不是静默 fallback：

- feature 文件不存在或空目录。
- feature 缺少 PIT 字段。
- 训练窗口缺少 `result_available_time`。
- held-out runner 未配置 `heldout_start`。
- held-out 缺少显式冻结参数。
- LLM evidence hash 被篡改。
- provider 返回非 JSON object。
- LLM response 缺失、重复或额外输出股票。
- action 不可交易时不能被转为订单。

## 11. 后续 Pipeline 扩展

### 11.1 扩展方向

建议新增 pipeline 时保持同一边界：

- `feature_build.py`：多数据域 PIT feature/evidence 构造。
- `training.py`：模型训练、cache、scaler、checkpoint 管理。
- `evaluation.py`：benchmark、超额收益、行业暴露、风险归因。
- `agent_assisted_wfo.py`：LLM-assisted held-out 对照，但默认仍 shadow-only。
- `live_payload.py`：只在模型/策略/风控全部冻结后生成 QMT payload。

任何可能产生真实订单的 pipeline 必须先在 `docs/QMT_documentation.md` 补全上线门槛、人工确认、风控、对账和 kill-switch。
