# 数据文档

本文档只记录数据层：从哪里下载、如何落盘、单位是什么、如何更新、如何审计、有哪些已知风险。

**相关边界**

- PIT snapshot、决策输入、回放和泄漏检查见 [Environment 设计](environment_design.md)。
- Agent 的证据使用与策略产物见 [Agent 设计](agent_design.md)。
- 完整实验编排见 [Pipeline 设计](pipeline_design.md)。
- 控制台与 QMT 实盘边界见 [部署文档](deployment_documentation.md)。
- 参数默认值速查见 [参数参考](parameters_reference.md)。

**职责边界**

数据层负责下载和落盘 raw 数据，保存来源、请求、单位、可见时间和内容身份，并执行质量审计与源端修正追踪。数据层不负责构造 PIT Snapshot、生成投资策略、执行回测或编排实验。

**术语说明**

| 中文名 | 代码/英文名 | 含义 |
|---|---|---|
| 原始落盘层 | `raw` | TuShare 或本地来源的原始文件，路径通常是 `data/raw/<dataset>/...` |
| 旁路元数据 | `sidecar` | 每个 parquet 旁边的 `.meta.json`，记录请求参数、抓取时间和 hash |
| 可见时间 | `available_at` | 数据在回测或决策中最早可以使用的时间 |
| 状态文件 | `status` | 当前数据质量审计结果 |
| 修正账本 | `revision ledger` | 源端回写或本地/远端不一致事件账本 |
| 研究发布版本 | `research release` | 某个 committed generation 的只读 raw/PIT 输入；Parquet 以硬链接按需发布 |
| 触顶风险 | `source cap risk` | 接口命中返回行数上限，可能被截断 |
| 按时点可见 | PIT | 按决策时点过滤未来信息；数据层只保存支撑该规则的原始时间字段 |

**导航**

- [1. 数据域与原始口径](#1-数据域与原始口径)
  - [1.1 数据域总览](#11-数据域总览)
  - [1.2 原始单位](#12-原始单位)
  - [1.3 基础研究数据](#13-基础研究数据)
  - [1.4 宏观与全球上下文](#14-宏观与全球上下文)
  - [1.5 历史分钟线](#15-历史分钟线)
  - [1.6 事件、资金与打板专题数据](#16-事件资金与打板专题数据)
  - [1.7 文本数据](#17-文本数据)
- [2. 下载、更新与落库任务](#2-下载更新与落库任务)
  - [2.1 初始下载与日常更新](#21-初始下载与日常更新)
  - [2.2 定时任务、限频与代码入口](#22-定时任务限频与代码入口)
- [3. 状态文件、审计与可见时间](#3-状态文件审计与可见时间)
  - [3.1 状态文件与审计规则](#31-状态文件与审计规则)
  - [3.2 原始数据时间可见性合同](#32-原始数据时间可见性合同)
  - [3.3 Timeview 刷新节点与环境层交接](#33-timeview-刷新节点与环境层交接)
- [4. 数据风险、修正账本与官方索引](#4-数据风险修正账本与官方索引)

## 1. 数据域与原始口径

本章说明各数据域的覆盖内容、原始单位、落盘形态和来源口径。

### 1.1 数据域总览

**数据流向**

```mermaid
flowchart LR
    TS[TuShare API] -->|download / update| RAW["data/raw/<dataset>\n(parquet + sidecar, available_at)"]
    RAW -->|audit| STATUS["数据质量报告"]
    RAW -->|cn_nightly_pit_event_build| PIT["fundamental_events\n财务事件可见性索引"]
    RAW --> RELEASE["research release\n实验固定的数据世代"]
    PIT --> RELEASE
    STATUS --> RELEASE
    RELEASE --> SNAP["Environment PIT snapshot\n裸数据窗口 + 交易文件单位归一 + 可见性过滤"]
    SNAP --> AGENT[Agent / backtest]
```

**当前数据域**

| 数据域 | 覆盖内容 | 当前状态文件 |
|---|---|---|
| 基础研究数据 | 基础维表、日频行情、交易约束、财务基本面 | `base_research_status.json` |
| 宏观与全球上下文 | 国内宏观、政策、利率、全球事件、指数、外汇 | `macro_context_status.json` |
| 历史分钟线 | 全 A 1 分钟历史数据和按日整理层 | `intraday_minutes_status.json` |
| 事件与资金数据 | 两融、资金流、股东、回购、解禁、大宗交易 | `event_flow_status.json` |
| 打板专题数据 | 开盘啦、同花顺榜单、龙虎榜、热榜、连板概念 | `board_trading_status.json` |
| 文本数据 | 公告、新闻、研报、政策法规、盈利预测 | `text_evidence_status.json` |

### 1.2 原始单位

机读单一事实源是 `src/autotrade/environment/data/units.py` 的 `UNIT_RULES`：结构化注册表，每条规则以 `file + dataset + 字段族` 定位（dataset 使用与快照完全一致的标识，不用组合键或别名），并携带 `source_unit / factor / normalized_unit / columns / status / evidence / agent_visible` 字段。它单向投影到全部消费面，任何一处都不得另行手工维护单位口径：

1. 快照换算表 `DAILY_UNIT_CONVERSIONS`/`AUCTION_UNIT_CONVERSIONS` 与 `DatasetContract.unit_rules` 由带 `factor`/`columns` 的规则派生；
2. Agent 合同 `AGENT_UNIT_CONTRACT["source_unit_rules"]`（经 `data_summary.json` 下发，仅含 `agent_visible` 条目，离线 Fold Agent 可完整读取）；
3. 各数据审计报告的 `unit_rules` 元数据按各审计域自己的数据集清单投影，清单中的数据集缺少注册表规则即抛错；
4. 人读完整单位表 `docs/units_reference.md` 由 `scripts/dev/export_units.py` 生成，禁止手工编辑，回归测试重新生成并比对。

`status=verified` 的条目已经真实数据比对核验（如 `share_float_complete.float_share` 为股、`repurchase.high_limit/low_limit` 为元/股均由回算核验），`official` 依据供应商官方字段合同，`inferred` 仅由局部证据推断。注册表必须覆盖全部默认快照数据集（回归测试强制包含关系）。

数据层只记录和审计原始单位，不改写原始字段。daily、分钟和竞价交易文件的单位归一，以及其他研究域的源单位使用规则见 Environment 的“单位合同”。宏观与跨资产只读上下文不存在统一“亿元”规则，单位必须按 `dataset+字段族` 逐条解释；同名列在不同 `dataset` 下量级可以相差 10^4（如 `index_dailybasic` 为元/股 vs `daily_basic` 为万元/万股，`daily_info` 为亿股/亿元 vs `sz_daily_info` 为元），严禁靠列名猜单位。

**单位文档的完整性边界**

不维护“仓库全部 raw 表 × 全部字段”的人工逐列单位抄录。大量字段是标识、日期、文本、类别或无量纲计数；其余源表 schema 会随供应商扩展。完整复制官方字段表会快速漂移，并把真正危险的跨源换算淹没在低价值文本中。

单位合同按使用风险分层：

1. Agent 直接使用的归一化文件必须完整声明字段族和换算因子；当前 `daily.parquet`、`intraday_1min.parquet` 和 `auction.parquet` 属于这一层。
2. `events.parquet`、`macro.parquet`、`fundamentals.parquet` 等异构 source union 保留源单位，按字段族为全部默认快照数据集显式维护规则（回归测试强制覆盖），但不逐列抄录供应商全字段表。规则必须用“文件 + `dataset` + 字段”定位，不能靠同名列。
3. 仅 raw 留档、文本、标识、类别和未进入数值研究的字段，以本地审计中的官方引用和供应商字段合同为准，不重复抄写到 Agent Prompt。

未映射的 source 字段不是“默认无量纲”：在核实上游合同并显式换算前，不得用于绝对阈值或跨数据集算术。某个新字段族一旦进入正式策略研究，同一变更必须补齐 `UNIT_RULES` 注册表条目和回归测试；Agent 合同、审计元数据与 `docs/units_reference.md` 由投影自动跟随，不单独修改。实际运行的优先级为：归一化 snapshot 合同与 `unit_conversions` > 本仓库 dataset-specific 规则 > 上游官方字段合同；观测数值范围只能用于校验，不能反向猜单位。

### 1.3 基础研究数据

基础研究数据包含三类：基础维表、日频行情与交易约束、财务与基本面。

**基础维表**

| 数据 | 接口 | 拉取方式 | 用途 |
|---|---|---|---|
| 股票列表 | `stock_basic` | `list_status=L/D/P` | 股票池基表 |
| 上市公司信息 | `stock_company` | 按交易所 | 公司属性补充 |
| 历史每日股票列表 | `bak_basic` | 按交易日，2016 起 | 每日行业、估值、股本快照补充 |
| 交易日历 | `trade_cal` | SSE/SZSE/BSE | WFO、调仓和交易日判断 |
| 曾用名/ST 历史 | `namechange` | 全量或按股票代码 | 名称/ST 变化，按公告可见性处理 |
| 行业分类 | `index_classify` | `src=SW2021` | 申万行业层级 |
| 行业成分 | `index_member_all` | 按一级行业 | 历史行业暴露 |

当前公司简介缺少可用于历史回测的可靠发布时间：

- 历史自然语言分析不得直接使用。
- 公司业务上下文优先取历史名称、当时行业、主营业务构成和 as-of 文本。
- forward run 若使用当前简介，最早可见时间只能从下载时间开始。

**日频行情与交易约束**

| 数据 | 接口 | 拉取方式 | 用途 |
|---|---|---|---|
| 日线行情 | `daily` | 按交易日 | OHLCV、成交额 |
| 复权因子 | `adj_factor` | 按交易日 | 复权价格构造和收益校验 |
| 每日指标 | `daily_basic` | 按交易日 | 估值、市值、换手率、股本 |
| 涨跌停价格 | `stk_limit` | 按交易日 | 交易约束 |
| 开盘集合竞价 | `stk_auction` | 按交易日（2025-01-16 起） | 最终竞价成交价、成交量和成交额；覆盖期内用于 Broker 开盘竞价撮合和研究 |
| 停复牌 | `suspend_d` | 按交易日或日期区间 | 停复牌约束 |
| 涨跌停/炸板列表 | `limit_list_d` | 按交易日 | 日终涨跌停和炸板标签 |

`daily`、`daily_basic`、`stk_limit` 覆盖口径不同，Environment Snapshot 必须显式处理缺失或连接方式。开盘竞价结果写入独立 `auction.parquet`：09:25 是交易所撮合时间，Agent 只能在盘前任务实际完整落地后看到结果；历史无观测记录时保守按09:29可见，内容修订按修订落地时间可见。正成交行必须量额均为正且 `price` 与 `amount/vol` 的差不超过0.005元；无成交行必须量额均为零且不产生清算价；价量额全为 NaN 的行是源端「无竞价观测」形态（停牌股、北交所 2025-08 换号期间的旧代码别名及当期正常交易但缺竞价印的 920 新码），构建时按缺行剔除并计入清单 `unobserved_rows_dropped`（Broker 沿用带标记的 09:30 代理），其余不一致组合仍然构建失败。2025-01-16以前使用明确标记的分钟代理。收盘集合竞价直接采用15:00官方收盘价，不需要重复数据源。`limit_list_d` 虽被打板研究复用，但主归属仍是日频交易约束。

**财务与基本面**

| 数据 | 接口 | 拉取方式 | 用途与边界 |
|---|---|---|---|
| 利润表 | `income_vip` | 按报告期 | 保留 `f_ann_date/report_type/comp_type` |
| 资产负债表 | `balancesheet_vip` | 按报告期 | 保留多版本记录 |
| 现金流量表 | `cashflow_vip` | 按报告期 | 保留多版本记录 |
| 财务指标 | `fina_indicator_vip` | 按报告期 | 无 `f_ann_date` 时用 `ann_date` |
| 业绩预告 | `forecast_vip` | 按公告月 | 预期修正事件 |
| 业绩快报 | `express_vip` | 按公告月 | 财报前置信息 |
| 分红送股 | `dividend` | 按股票代码 | 公告可见时间与除权实施日分离；Broker 如何消费实施事实见 Environment |
| 审计意见 | `fina_audit` | 按股票代码 | 审计风险 |
| 主营业务构成 | `fina_mainbz_vip` | 按股票代码 | 业务结构 |
| 披露计划 | `disclosure_date` | 按报告期 | 披露计划和实际披露日 |

财务原始层保留多版本、重复业务键和稀疏分区。环境层会把它们构造成 `fundamental_events` 后，再按当前视图的 Timeview cutoff 选择可见版本。

### 1.4 宏观与全球上下文

宏观/全球数据先作为市场背景和文本证据，不直接替代股票日频数据或策略信号。

| 数据 | 接口 | 拉取方式 | 用途 |
|---|---|---|---|
| 经济数据发布日程 | `cn_schedule` | 按月 | 运维参考；当前未接入历史快照的发布时间修正 |
| GDP | `cn_gdp` | 按季度 | 宏观 regime |
| CPI/PPI/PMI | `cn_cpi` / `cn_ppi` / `cn_pmi` | 按月 | 通胀和景气度 |
| 货币供应与社融 | `cn_m` / `sf_month` | 按月 | 流动性 |
| 利率与回购 | `shibor` / `shibor_quote` / `shibor_lpr` / `repo_daily` | 按年 | 资金价格 |
| 港/外币拆借利率 | `hibor` / `libor` | 按年或货币+年份 | 离岸/外币流动性 |
| 美国利率 | `us_tycr` / `us_trycr` / `us_tbr` / `us_tltr` | 按年 | 全球利率环境 |
| 全球财经日历 | `eco_cal` | 按月 | 全球事件 |
| 全球指数 | `index_global` | 按指数代码+年份 | 跨市场风险偏好 |
| 外汇日线 | `fx_daily` | 按外汇代码+年份 | 汇率上下文 |
| 央行货币政策执行报告 | `monetary_policy` | 按发布年份 | 政策文本 evidence |
| A 股核心宽基指数 | `index_daily` | 按指数代码+年份 | 指数择时、β 管理、相对强弱和冻结运行基准 |

**衍生品市场上下文（不可交易，Agent 自行计算信号）**

| 数据 | 接口 | 拉取方式 | 用途 |
|---|---|---|---|
| 期货合约注册表 | `fut_basic` | 按交易所整表刷新（`exchange=*.parquet`，全部 6 所） | 合约乘数/上市退市日；股指期货基差年化 |
| 期货主力/连续映射 | `fut_mapping` | 按交易日 | 主力合约代码解析（如 `IF.CFX`→当月合约） |
| 期货日线 | `fut_daily` | 按交易日（全市场，商品期货保留作宏观/通胀背景） | 股指期货结算价/持仓量 → 基差(贴水)、IC/IM−IF/IH 价差 |
| 期权合约注册表 | `opt_basic` | 按交易所整表刷新（限 SSE/SZSE/CFFEX） | `call_put`/行权价/到期日；PCR 与 IV 计算输入 |
| 期权日线 | `opt_daily` | 按交易所×交易日（`exchange=*/trade_date=*.parquet`，限 SSE/SZSE/CFFEX；SZSE/CFFEX 自 2019-12-23） | ETF/股指期权成交与持仓 → PCR、IV/偏度（IV 需自行反解） |
| 可转债注册表 | `cb_basic` | 单文件整表刷新 | `stk_code` 正股映射与固定条款 |
| 可转债日线 | `cb_daily` | 按交易日 | `cb_value`/`cb_over_rate` 等当日转股/纯债溢价（须显式请求非默认字段；历史溢价一律用本表） |
| 可转债赎回公告 | `cb_call` | 单文件整表刷新 | 强赎/到期赎回事件阶梯（对正股的负面事件信号） |
| 中债国债收益率曲线 | `yc_cb` | 按交易日单次拉取（`ts_code=1001.CB/trade_date=*.parquet`，~500 期限点/日，自 2016-06） | 无风险利率（IV/基差年化输入）、期限利差 |

衍生品日频表按 `trade_date` 当日收盘后保守可见（EOD 盖章 + 晚间节点），只能用于次一交易日及以后的决策；注册表行按 `list_date`（`cb_call` 按 `ann_date`）可见。合约/转债注册表体量小、整表刷新并走修订感知覆盖；**决策快照**侧注册表豁免宏观窗口下限（老合约/老转债的条款仍可见），回放槽不豁免（避免与冻结快照在 Timeview 合并后重复行），逐行 `available_at` 照常执行 PIT 墙。

**PIT 硬禁令（cb_basic 为当前状态表）**：`cb_basic` 每晚整表刷新反映**当前**状态，其 `conv_price`/`remain_size`/`newest_rating`/`delist_date` 在历史回测中即前视信息，禁止使用——历史任意时点的转股价按 `conv_price_t = 100 × 正股close_t / cb_daily.cb_value_t` 由当日行 PIT 推出，溢价直接用 `cb_daily.cb_over_rate`，赎回结局用 `cb_call`。期货/期权合约的 `delist_date` 是上市即定的合约条款，可正常使用。`fut_basic` 中连续/主力合约行（如 `IF.CFX`）无 `list_date`、在快照中不可见——主力解析一律走 `fut_mapping`。

只有日期或月份的数据不得用于同日开盘决策；进入环境层后应优先使用精确发布时间或保守延后时间。

**研究快照范围**

- 默认纳入国内宏观、回购利率、美国收益率曲线、全球指数、外汇、A 股核心宽基指数，以及上表全部衍生品市场上下文数据。
- 发布日程不进入研究快照，也没有与月度或季度宏观数据做历史关联。
- 港币和外币拆借利率、部分美国利率表不进入默认快照，因为历史覆盖不足、停更或与已纳入数据重复。
- 宏观可见时间只使用源表明确时间；缺少明确时间时采用按月、季度或日期的保守延后规则。

### 1.5 历史分钟线

| 数据 | 接口/层 | 拉取方式 | 用途 |
|---|---|---|---|
| 历史 1 分钟源 | `stk_mins` | 全 A，按 `ts_code + year` | 可追溯源层 |
| 按日分钟最终层 | 本地整理 | 每交易日全市场文件 | 日内回放和增量更新 |
| 开盘竞价 | `stk_auction` | 2025-01-16 起按交易日全量 | 更早历史由09:30分钟条按代理口径承载 |

源层路径：`data/raw/stk_mins_1min/ts_code=<TS_CODE>/year=<YYYY>.parquet`。
最终层路径：`data/raw/stk_mins_1min_by_date/trade_date=<YYYYMMDD>.parquet`。

最终层字段必须包含 `ts_code, trade_time, open, high, low, close, vol, amount, trade_date, available_at, available_at_rule`。分钟数据使用前要按有效股票池过滤。

决策输入包含按配置截取的近期分钟样本；验证和测试回放覆盖由各 Fold 区间决定，不受研究样本窗口限制。当前窗口默认值见 [参数参考](parameters_reference.md#1-快照窗口snapshotconfig)。

### 1.6 事件、资金与打板专题数据

| 数据 | 接口/文件 | 拉取方式 | 用途与边界 |
|---|---|---|---|
| 两融汇总 | `margin` | 按交易日 | 市场杠杆 |
| 两融明细 | `margin_detail` | 按交易日 | 个股融资融券压力 |
| 融资融券标的 | `margin_secs` | 按交易日 | 交易所标的资格；源表不区分担保品、融资和融券标的 |
| 个股资金流 | `moneyflow` | 按交易日 | 资金行为 |
| 东财个股资金流 | `moneyflow_dc` | 按交易日（2023-12 起） | 分档资金行为（超大/大/中/小单，含占比） |
| 同花顺个股资金流 | `moneyflow_ths` | 按交易日（2025 起） | 分档资金行为 + 5 日净额 |
| 东财板块资金流 | `moneyflow_ind_dc` | 按交易日（2023-12 起） | 行业/概念板块资金轮动（键含 content_type） |
| 同花顺行业/概念资金流 | `moneyflow_ind_ths` / `moneyflow_cnt_ths` | 按交易日（2025 起） | 板块级资金轮动 |
| 筹码分布汇总 | `cyq_perf` | 按交易日（2018 起） | 获利盘比例与成本分位（5/15/50/85/95%） |
| 备用日行情 | `bak_daily` | 按交易日（2017 起） | 31 列衍生行情（量比/强弱度/活跃度等） |
| 盘前静态表 | `stk_premarket` | 按交易日（当日 09:00 可见） | 盘前股本与涨跌停价 |
| 转融通余额 | `slb_len` / `slb_len_mm` | 按交易日（2024-07 转融券暂停后部分为零） | 券源供给背景 |
| 前十大股东 | `top10_holders` / `top10_floatholders` | 按公告月（季频披露） | 股权集中度与变动 |
| 股权质押明细 | `pledge_detail` | 按公告月 | 质押风险事件（`pledge_stat` 无批量拉取路径，已缓采） |
| 机构调研 | `stk_surv` | 按交易日（2022 起） | 机构关注度软信号 |
| IPO 新股 | `new_share` | 按公告月（ipo_date EOD 保守可见） | 新股日历与供给 |
| 股东人数 | `stk_holdernumber` | 按公告月 | 筹码集中度 |
| 股东增减持 | `stk_holdertrade` | 按公告月 | 治理和事件 |
| 回购 | `repurchase` | 按公告月 | 资本配置 |
| 解禁 | `share_float_complete` | 专用补全 union | 供给压力 |
| 大宗交易 | `block_trade` | 按交易日 | 特殊交易行为 |

`share_float_complete` 是解禁最终保留边界。普通 `share_float` 过程文件可归档，但 union 不得静默缩水。触顶分区使用 candidate 级补充；如果最细粒度仍正好 6000 行，只能标记 `source_cap_risk`。

数据层只保证融资融券标的表的可见时间与源口径。回放近似及真实券商数据缺口见 Environment。

**打板专题数据**

打板专题数据用于日终标签、情绪和分钟回放。真实盘中打板不能提前使用日终汇总字段。

| 数据 | 接口 | 拉取方式 | 用途 |
|---|---|---|---|
| 涨跌停价格 | `stk_limit` | 按交易日 | 涨跌停交易约束；主归属为基础研究 |
| 日终涨跌停/炸板标签 | `limit_list_d` | 按交易日 | 涨停、炸板、回封标签；主归属为基础研究 |
| 开盘啦榜单 | `kpl_list` | 按交易日 + `tag` | 开盘啦涨停、炸板、跌停、竞价标签 |
| 连板高度 | `limit_step` | 按交易日 | 连板高度 |
| 连板概念 | `limit_cpt_list` | 按交易日 | 概念聚类和板块强度 |
| 同花顺榜单 | `limit_list_ths` | 按交易日 + `limit_type`，官方历史从 2023-11-01 起 | 同花顺涨停池、炸板池、跌停池 |
| 龙虎榜 | `top_list` | 按交易日 | 龙虎榜资金性质和上榜原因 |
| 开盘啦概念成分 | `kpl_concept_cons` | 按交易日（2025 起，次日 08:30 可见） | 概念成员与热度 |
| 东财板块指数/成分 | `dc_index` / `dc_member` | 按交易日（2025 起，当日 20:00 可见） | 板块轮动与成员图谱 |
| 机构席位 | `top_inst` | 按交易日 | 机构席位买卖和净额 |
| 游资名单 | `hm_list` | 静态全量 | 游资席位参考表 |
| 游资明细 | `hm_detail` | 按交易日，官方历史从 2022-08-01 起 | 游资席位映射和交易痕迹 |
| 同花顺热榜 | `ths_hot` | 按交易日 + `market` + `is_new` | 人气、概念和行业热度 |
| 东方财富热榜 | `dc_hot` | 按交易日 + `market` + `type` + `is_new` | 人气、概念和行业热度 |
| 分钟触板/开板 | `stk_mins_1min_by_date` + `stk_limit` | 按交易日分钟文件和涨跌停价格联动推导 | 用已走完分钟 bar 推导盘中触板/开板 |

**重要边界**

- `kpl_list` 按次日 08:30 可见处理。
- `top_list/top_inst` 按当日 20:00 可见处理。
- `limit_list_d` 与 `limit_list_ths` 口径不同，不能互相覆盖。
- `first_time/open_times/fd_amount/limit_amount` 等日终字段不能用于盘中决策。

**研究快照范围**

- 默认纳入开盘啦榜单、连板高度与概念、同花顺榜单、热榜和游资数据，并按来源标签区分。
- 这些数据只作情绪或题材描述性弱信号；空值、口径变化和重复键不能作为成交、可交易性、资金或风控真相。
- 日终涨跌停标签不进入默认研究快照：其行级可见时间不完整，且部分历史金额字段存在源端回写风险。

### 1.7 文本数据

数据层保存文本原文、来源和可见时间。进入 Agent 前，快照层生成本次快照内唯一的文本标识、标题、原始证券代码（若有）、截断文本载荷的 hash 和正文分片引用；当前没有通用实体解析或来源质量评分。

| 数据 | 接口 | 拉取方式 | 可见性 |
|---|---|---|---|
| 上市公司公告 | `anns_d` | 按公告月 | `rec_time` 仅在与 `ann_date` 相差 -1~+3 天内可信；否则回退 `ann_date 23:59:59` |
| 长新闻 | `major_news` | 按月份 | `pub_time` |
| 新闻联播 | `cctv_news` | 按日期 | 只有日期时按晚间可见 |
| 政策法规库 | `npr` | 按月份 | `pubtime` |
| 券商研究报告 | `research_report` | 按月份 | 只有日期时不能用于同日开盘 |
| 盈利预测 | `report_rc` | 按月份 | `create_time` 仅在与 `report_date` 相差 -1~+3 天内可信；否则回退 `report_date 22:00`（官方当日 19:00-22:00 更新） |
| 新闻快讯 | `news` | 按来源+日期 | 使用明确时间；默认纳入全部来源并跟随文本窗口，按截断文本载荷跨来源去重，保留最早可见副本；可按来源或窗口收紧 |

带日期基准的文本时间字段只在接近源日期时才视为发布时间。回填历史中的采集时间可能远晚于真实发布日；超出允许偏差时按表中规则保守回退并标记原因。

修复历史可见时间后，必须同步重建对应元数据并重新审计。当前修复入口不会自动更新既有摘要，不能把单独重写数据文件视为完整修复。

## 2. 下载、更新与落库任务

本章说明 raw 数据的首次下载、日常更新、调度顺序、限频和落库流程。

### 2.1 初始下载与日常更新

**初始建库顺序**

1. 基础研究数据：`reference`、`daily`、`fundamental`。
2. 宏观与全球上下文：`macro`、`global`。
3. 历史分钟线：下载 `intraday` 源层，再整理为按日最终层。
4. 事件与资金数据：`event_flow`，再生成 `share_float_complete`。
5. 打板专题数据：`board_trading`。
6. 文本数据：`text_evidence`。

统一数据任务按数据域建库；分钟线下载后还需整理为按日层，解禁数据还需生成完整 union。日常更新接收开始和结束日期，并覆盖该闭区间内需要新增或刷新的分区。

**通用规则**

- `update` 从 `start_date` 扫到 `end_date`，不是只更新当天。
- cron 按配置的滚动窗口回看，不只更新当天；当前数值见 [参数参考](parameters_reference.md#7-数据层任务参数)。
- 已存在且旁路元数据覆盖请求范围的分区跳过。
- 开放月份、开放年份和近期交易日会按配置强制刷新。
- 远端空响应不会覆盖本地非空分区，除非显式允许。
- 重拉若会删除已有业务键，默认阻断覆盖并写修正账本；接受源端真实撤回需先删除对应分区文件。事件/打板整分区拉取按源端修正接受删键（账本记录），但单次删除超过 20 个键且超过现有键 20% 时按截断风险阻断（`blocked_shrink_overwrite`）。
- 公告月分区始终按完整自然月拉取；窗口起点截断到月中会在整月覆盖时删除早先公告。
- 触发源端修正时写入修正账本。
- 交易日历会向后补足，供次日盘前判断。
- 宏观和全球 range 型数据使用固定历史下界维护，固定写入唯一 `range=<下界>_latest.parquet` 并在写后清理旧的按结束期命名文件；宏观审计将残留的多余 range 文件记为 error，快照域构造再按数据集防御性去重。

| 数据域 | 日常刷新规则 | 风险控制 |
|---|---|---|
| 基础维表 | 股票列表、公司信息、行业、曾用名每日强刷；`trade_cal` 覆盖不足时补齐 | 空响应不覆盖非空本地 |
| 日频行情与约束 | 强刷近期滚动窗口 | 差异写修正账本；`limit_list_d` 不稳定字段不进入冻结交易输入 |
| 财务与基本面 | 按报告期和公告月滚动强刷；分红/审计/主营业务只定向刷新候选股票 | 避免全市场按日期误刷；保留多版本 |
| 宏观与全球 | 每晚刷新开放窗口 | 月度/季度数据使用保守可见时间 |
| 历史分钟线 | 每晚补最近窗口，只强刷尾部短窗口 | 新交易日按 `daily` 股票池尝试下载；已有按日文件按本地分钟覆盖口径校验，严格 `daily` 覆盖只做专项排查 |
| 事件/资金 | 晚间滚动强刷；两融和融资融券标的盘前回补 | 非交易日前一天自动跳过 |
| 解禁 union | 每晚重建 `share_float_complete` | union 缩水默认阻断覆盖 |
| 打板专题 | 晚间滚动强刷，08:50 回补关键榜单 | 官方历史起点前不视为缺失 |
| 文本数据 | 晚间滚动强刷，08:55 回补短新闻 | 重复推送保留在 raw，快照构造阶段按截断文本载荷跨来源去重 |

### 2.2 定时任务、限频与代码入口

TuShare 接口更新时间和 cron 策略维护在 `configs/tushare_update_schedule.json`。

**当前北京时间任务**

| 任务 | 时间 | 目的 |
|---|---:|---|
| `cn_evening_full` | 23:35 | 滚动更新全域 raw 和近期开放窗口 |
| `cn_nightly_full_audit` | 02:30 | 刷新 6 个顶层状态文件；事件/资金域统一按最晚发布的次晨两融边界审计 |
| `cn_nightly_pit_event_build` | 03:35 | 构造并审计财务事件 PIT 可见性索引 `fundamental_events`（状态文件为 `fundamental_events_status.json`，不属于 6 个 raw 状态文件） |
| `cn_daily_revision_sentinel` | 04:00 | 抽样检查历史分区是否被源端回写 |
| `cn_preopen_board_backfill_0850` | 08:50 | 回补前一日打板专题关键榜单 |
| `cn_preopen_text_backfill_0855` | 08:55 | 回补短新闻和新闻联播 |
| `cn_preopen_margin_secs_backfill_0903` / `cn_preopen_margin_secs_retry_0913` | 09:03 / 09:13 | 刷新当日融资融券标的资格 |
| `cn_preopen_margin_backfill_0905` / `cn_preopen_margin_retry_0915` | 09:05 / 09:15 | 回补上一交易日两融汇总和明细 |
| `cn_preopen_event_flow_audit_0920` | 09:20 | 盘前刷新事件/资金状态 |
| `cn_open_auction_capture_0927` | 09:27 / 09:31 | 轮询并严格发布当日开盘竞价结果 |
| `cn_open_auction_capture_0927`（强制复核） | 23:20 | 严格重查晚到修订；相同内容保留早盘首次可见时间 |

回测的逐 tick 数据视图按真实落库任务的约定完成时间放行数据；纯审计任务不落新数据，也不能成为可见性节点。完整门禁语义见 §3.3。

runner 使用 `.runtime/tushare/locks/tushare_update.lock` 防止并发写 raw/PIT，下载子进程继承同一 flock（经 `TUSHARE_UPDATE_LOCK_HELD` 标记避免重复加锁），避免 runner 异常退出后残留写进程失锁。手工 `tushare_download.py` 对生产 `data/raw` 的写命令同样非阻塞获取该独占锁，锁忙时直接失败而不是与 cron 或发布竞态。任一 raw 或 PIT 落库任务在第一条写命令前把 `data/raw/.raw_generation.json` 标为 `updating`，全部成功后发布新的 `committed` 湖世代；失败为 `dirty`，只能由同 job、区间和命令精确重跑恢复。纯审计任务不改变世代。

盘前两融任务（`cn_preopen_margin_secs_backfill_0903`/`_retry_0913`、`cn_preopen_margin_backfill_0905`/`_retry_0915`）带 `--zero-rows-not-ready`：源端尚未发布（必需数据集返回空响应）是常规事件而非失败——若本次调用没有任何写入，进程以退出码 75 结束（与竞价捕获相同的"无变更可重试"契约），cron 恢复先前 `committed` 世代并记 `not_ready`，由独立命名的重试任务或夜间全量同步补齐；若部分数据集已写入，则正常提交世代并把空分区留给重试任务。历史回补不带该旗标，必需数据集的空响应仍 fail-fast。详细运行日志写入 `logs/tushare/cron/tushare_cron_<job>_<end_date>_<timestamp>.log`，保留 14 天（当前 state 引用的每个 job 最后一份日志不删除）。cron 汇总流写入 `logs/tushare/dispatch.log`，单文件上限 5 MiB、保留两个轮转副本；进程锁保证并发追加与轮转不互相覆盖。运行状态写入 `.runtime/tushare/cron_state.json`：只有本次真实日志已成功创建才记录新 `log_path`，跳过、休市或锁失败只能沿用仍存在的上一条路径，否则省略该字段。

实验不直接跟随 live 目录变化：首次启动在锁空闲且 generation committed 时按需发布 `data/research_releases/<generation_id>/`；更新锁忙或 generation 为 `updating` / `dirty` 时立即复用最近完整版本。raw Parquet、配对 sidecar 和 PIT Parquet 使用硬链接，其余小文件与质量状态使用副本，实时目录 `rt_min_live` 不进入版本。发布后所有 Fold、交易日历和恢复运行都读取同一个实验 pin，因此数据更新无需中断实验。部署后须先在 committed 世代完成一次 bootstrap；此前若更新锁正忙会立即失败而非等待。

`stk_auction` 的定时写入只走专用严格捕获（稳定读取、完整性和历史行数下限）；晚间通用更新显式排除该数据集，不能覆盖早盘分区。

当前 crontab 必须通过专用安装器合并更新，并在安装后复核现有任务；不得直接用静态 cron 文件替换当前用户的整份 crontab。

**限频和分页**

- 每类接口遵守独立频次和单页上限；当前数值集中维护在 [参数参考](parameters_reference.md#7-数据层任务参数)。
- 脚本间隔必须比官方上限保守；全市场循环使用更低频率。
- 命中官方行数上限时，必须缩小日期、按股票代码、按来源或按 offset 分页。
- `TUSHARE_TOKEN` 只允许存在于环境变量或 ignored `.env`。
- 长任务必须有断点续跑、限频、重试和本地日志。

**实现边界**

- 命令行脚本只负责参数解析和调用。
- 下载、更新、审计、修正监控、分页和读写逻辑集中在数据源包。
- 正式 live raw/PIT 源写入必须取得 updater 独占锁，并使用临时文件加原子替换；禁止原地改写已发布文件。
- 定时配置是任务时间、回看窗口和限频参数的操作事实源；设计正文不依赖内部模块或私有变量名。

## 3. 状态文件、审计与可见时间

本章定义数据质量状态、审计规则、可见时间和 Timeview 刷新节点。

### 3.1 状态文件与审计规则

`results/data_quality/` 顶层维护以下六个 raw 数据域状态文件；财务事件索引另有独立状态文件：

| 文件 | 覆盖范围 |
|---|---|
| `base_research_status.json` | 基础维表、日频行情与约束、财务基本面 |
| `macro_context_status.json` | 宏观、政策、全球事件和跨市场上下文 |
| `intraday_minutes_status.json` | 历史分钟线最终按日层 |
| `event_flow_status.json` | 事件/资金数据和 `share_float_complete` |
| `board_trading_status.json` | 打板专题数据 |
| `text_evidence_status.json` | 文本原始层 |

临时排查产物写入 `results/data_quality/process/`；处理后移到根目录 `archive/` 或删除。

**补充状态文件**

- 修正账本 `revision_events.jsonl` / `revision_summary.json`，见第 4 章。
- 财务事件索引另有独立质量报告。启用财务域时，报告缺失或为 error 会阻断快照构造；构建时先按决策窗口选择分区，再按行级可见时间过滤。

正式质量报告由定时任务维护。Snapshot 对报告采用分级门禁：

- **硬门禁**：日频执行数据、分钟回放数据和财务事件索引的报告缺失、无法解析或状态为 error 时，快照构建失败。
- **告警门禁**：事件/资金、宏观和文本报告的同类问题写入快照告警，实验可以继续。
- **打板专题**：Snapshot 单独读取打板专题报告；异常只写入数据质量告警，不阻断实验。
- 门禁检查报告存在性、可解析性和状态；报告早于当前数据代时记新鲜度告警（执行域缺失/错误仍硬失败）。报告摘要与本次快照数据内容的绑定仍未实现，旧报告在告警下仍可能放行，这是需要人工审计的限制。

| 状态文件 | 合格条件 | 常见 warning |
|---|---|---|
| `base_research_status.json` | 无 error finding，基础维表、日频行情、约束和财务分区可读 | 单位口径、覆盖差异、重复业务键 |
| `macro_context_status.json` | 无 error finding，宏观、政策和跨市场上下文分区可读 | 发布时间保守假设、异构事件值 |
| `intraday_minutes_status.json` | `status=ok`，交易日分钟按日层覆盖可用 | 不应有常态 warning |
| `event_flow_status.json` | 无 error finding，交易日事件/资金分区覆盖到最近应可见交易日 | 稀疏事件、重复事件键、PIT 语义提示 |
| `board_trading_status.json` | 无 error finding，打板专题分区覆盖到最近应可见交易日 | 龙虎榜/榜单口径差异 |
| `text_evidence_status.json` | 无 error finding，文本源覆盖到自然日窗口 | 新闻重复、文本时间语义提示 |

**通用审计规则**

**正式状态文件检查项**

- 文件是否存在、Parquet 是否可读。
- 旁路元数据是否存在，是否和 parquet 对齐。
- 是否有空文件、半成品、孤儿旁路元数据。
- 预期分区是否齐全。
- 关键字段、业务键、日期字段是否可用。
- 是否命中分页行数上限。
- 单位和可见时间规则是否写入报告。

**报告结构**

- 顶层保留报告（六个 raw 数据域、财务事件索引和 revision summary）统一采用 schema v2 的九字段 envelope：`schema_version`、`report_type`、`created_at`、`status`、`scope`、`finding_counts`、`datasets`、`findings`、`metadata`。schema v1 已被旧 revision summary 占用，因此生产端和消费端一律拒绝 v1、无版本及未知版本文件，修复方式是重新生成 v2，不做推测性兼容。
- `scope` 固定包含 `data_root`、`start_date`、`end_date`、`datasets`，各报告可补充筛选条件；`datasets` 由同一个 builder 从 findings 与 `scope.datasets` 派生，其键集合必须与 `scope.datasets` 精确相等。即使某数据集没有 finding，也保留 `status=ok`、全零 `finding_counts` 和空 `checks`。`datasets.<name>` 固定只包含 `status`、`finding_counts`、`checks`。
- 每条 finding 只包含 `severity`、`check`、`message`、`details`。`check` 是记录判别符，只有 `details` 允许保留该检查真正需要的领域字段；消费方不得跨不同 `check` 猜测同名扩展字段语义。
- `metadata` 保存单位、PIT 规则、结论、行数、抽样参数等报告专属内容，避免把大量可空扩展字段铺到统一 envelope 上。
- producer 写入前和 snapshot/research-release consumer 读取后都校验完整 envelope、类型、状态/计数、finding、dataset summary 与报告类型。发布使用同目录唯一临时文件后 `os.replace()`，并发手工审计与 cron 发布最多产生“最后一个完整报告获胜”，不会共享或覆盖同一临时文件。

存在 `error` 时脚本返回非 0；只有 `warning` 时返回 0，但下游必须显式处理 warning 指向的语义风险。

**各数据域审计**

| 数据域 | 入口 | 输出 | 核心检查 | 特殊风险 |
|---|---|---|---|---|
| 基础研究 | `scripts/data/tushare_audit.py base --include-limit-list` | `base_research_status.json` | 基础维表、交易日、日频分区、财务多版本、跨表股票覆盖、单位 | `bak_basic` 起始较晚；日频表覆盖口径不同；财务重复键是原始语义 |
| 宏观与全球 | `scripts/data/tushare_audit.py macro` | `macro_context_status.json` | 月/季/年/代码/货币分区、字段、重复事件键、单位和可见时间 | 月度/季度发布时间滞后；`eco_cal` 异构事件不能直接数值化 |
| 历史分钟线 | `scripts/data/tushare_audit.py intraday-by-date` | `intraday_minutes_status.json` | 按日文件、必需字段、重复 `(ts_code, trade_time)`、时间解析、09:30/15:00 条 | 正式状态文件用本地分钟覆盖口径；严格 daily 覆盖只做专项排查 |
| 事件/资金 | `scripts/data/tushare_audit.py event-flow` | `event_flow_status.json` | 日频/公告分区、资金和事件单位、重复业务键、`share_float_complete` 合并结果 | 解禁触顶风险；融资融券标的资格不等于券商券源 |
| 打板专题 | `scripts/data/tushare_audit.py board-trading` | `board_trading_status.json` | tag/type/market 分区、榜单字段、可见时间、重复键 | 日终标签不能用于盘中；同花顺和 TuShare 涨跌停口径不同 |
| 文本数据 | `scripts/data/tushare_audit.py text` | `text_evidence_status.json` | 月/日期/source 分区、时间字段、重复文本键、触顶风险 | 只审计文本域，不再复制 base audit；重复推送和转载是 warning，快照层生成本次快照内唯一标识和正文引用 |

分钟线竞价口径专项检查只用于过程排查，不写入顶层状态文件。

### 3.2 原始数据时间可见性合同

**原始层原则**

- 原始层尽量保留 TuShare 原始字段，不派生 alpha 列。
- 原始层不静默删除多版本财报、重复公告、稀疏事件和源端重复推送。
- 每个 parquet 都应有旁路元数据，记录来源、请求、抓取时间和最终数据文件摘要。
- 数据文件和元数据文件各自通过临时文件原子替换，但两者不是跨文件事务。异常中断可能留下不匹配组合；质量审计只抽样验证近期文件，未通过审计前不得把配对关系视为可信。
- 原始审计只说明数据是否足以支持按时点可见，不声明某个策略无泄漏。

**可见时间速查**

下表定义行级最早可见时间。回放还叠加落库任务完成时间；两者都到达后，该行才进入滚动视图。

| 数据 | 可见时间规则 |
|---|---|
| `daily` / `daily_basic` | 行级时间在当日收盘后；再经晚间落库门禁，交易日内通常只到 D-1；历史回放无真实完成账本时保守按次日 03:05 放行，09:25 不得使用当日日频 |
| 分钟线 | `available_at=trade_time`，视为该分钟 bar close 后可见；历史分钟随 `cn_evening_full` 晚间滚动落库，当日实时 bar 由引擎 `ctx.bars` 提供、不走持久化视图 |
| 财务 | 优先 `f_ann_date`，否则 `ann_date`；多版本按决策时点选择；`fundamental_events` 由 `cn_nightly_pit_event_build`（约 03:50）落库后可查 |
| 业绩预告/快报 | 每个版本按其自身 `ann_date` 可见；`first_ann_date` 只是序列属性，绝不作为可见性下界（PIT 审计强制 available_at ≥ 本行 ann_date，违反记 error） |
| 分红 | Agent 可见性只用 `imp_ann_date/ann_date` 判断，`ex_date/record_date/pay_date` 是未来事件属性；Broker 侧另按 `ex_date` 消费已实施分红作为除权日市场事实（Environment 输入、非 Agent 输入，不构成 PIT 泄漏） |
| 宏观 | 使用源表明确发布时间；否则按月末、季末或日期采用保守延后。发布日程当前未接入历史关联；Timeview 再叠加晚间落库时间 |
| 全球事件 | 有具体 `time` 时使用 `date+time`，否则当日收盘后可见；Timeview 随 `cn_evening_full` 落库 |
| 文本 | 优先 `rec_time/pub_time/pubtime/datetime/create_time`；有日期基准的字段须通过 -1~+3 天合理性检查，否则按日期保守回退（见 §1.7）；`cctv_news/news` 盘前另由 `cn_preopen_text_backfill_0855` 回补 |
| 两融 | `margin/margin_detail` 行级 `available_at` 为下一日 09:00，Timeview 经盘前 `cn_preopen_margin_backfill_0905`/`_retry_0915` 落库；`margin_secs` 为当日盘前 09:00，经 `cn_preopen_margin_secs_backfill_0903`/`_retry_0913` 落库 |
| 资金/大宗 | `moneyflow` 当日 19:00、`block_trade` 当日 21:00 为行级 `available_at`；Timeview 随 `cn_evening_full`（历史保守边界次日 03:05）落库，故当日盘中不可见 |
| 其他扩展域 | 精确开盘竞价自2025-01-16起按实际完整落地时间可见（通常09:27–09:29；旧历史保守记09:29）；事件面板族行级通常为19:00，盘前静态表当日09:00；宏观市场级数据保守按日终可见；互动问答按实际发布时间；参考静态表定期强刷维护 |

### 3.3 Timeview 刷新节点与环境层交接

回测的逐 tick 数据视图复刻本地库的刷新节奏。一行数据需要同时满足：

1. 行级可见时间已经到达。
2. 负责落库的刷新任务已按计划完成。

竞价使用分区观测到的实际可见时间；其他历史节点缺少逐次完成账本时按“任务启动时间 + 保守时长”建模。刷新节点必须对应真实落库任务；只读审计任务不能让新数据变为可见。完整回放语义见 Environment 文档。

**默认刷新节点**

| 节点 | 启动 → 就绪 | 让什么变可见 |
|---|---|---|
| `cn_evening_full` | 23:35 → 次日 03:05（210 分钟保守边界） | A 股日频核心（`daily` / `daily_basic` / `adj_factor` / `stk_limit` / `suspend_d`）、分钟历史、`moneyflow`、`block_trade`、股东/回购/解禁/龙虎榜、全部宏观、文本主语料 |
| `cn_nightly_pit_event_build` | 03:35 → 约 03:50（约 15 分钟） | 财务 PIT 事件（`fundamental_events`）变为可查询 |
| `cn_preopen_board_backfill_0850` | 08:50 → 约 08:55 | 前一日打板关键榜单（`kpl_list` 等） |
| `cn_preopen_text_backfill_0855` | 08:55 → 约 09:00 | 短新闻 `cctv_news` / `news` 盘前回补 |
| `cn_preopen_margin_secs_backfill_0903` / `_retry_0913` | 09:03 / 09:13 → 约 09:05 / 09:15 | 当日 `margin_secs` 近似标的池（同一集合临时门控担保品买入、融资买入和融券卖出） |
| `cn_preopen_margin_backfill_0905` / `_retry_0915` | 09:05 / 09:15 → 约 09:07 / 09:17 | 前一交易日 `margin` / `margin_detail` |
| `stk_auction` 行级边界 | 实际完整落地时间（通常 09:27–09:29） | 当日开盘竞价研究视图；Broker 清算真值不提前暴露 |

晚间全量任务的历史保守边界为次日 03:05，因此：

- 交易日内持久日频视图通常只到 D-1。
- 当日日频与分钟历史要到次日任务完成后才滚动进入。
- 当日实时分钟 Bar 由回放引擎单独提供，不走持久视图。

纯审计 job（`cn_nightly_full_audit`、`cn_daily_revision_sentinel`、09:20 的 `cn_preopen_event_flow_audit_0920`）不落新数据，刻意不作为节点。

**环境层交接信息**

- 来源：接口名、请求参数、分区路径、旁路元数据。
- 业务键：例如 `(trade_date, ts_code)` 或财报多版本键。
- 时间键：交易日、公告日、发布时间、生效日和 `available_at` 候选。
- 单位：价格、成交量、成交额、股本、市值、财报金额、宏观口径。
- 风险标记：分页触顶、空分区、源端缺失、重复推送和 revision 事件。

## 4. 数据风险、修正账本与官方索引

本章汇总已知数据风险、修正账本的记录规则和官方来源索引。

| 风险项 | 影响 | 当前处理 |
|---|---|---|
| 深圳09:30分钟条与最终开盘竞价量额口径不一致 | 2025-01-16以前的竞价代理 | 原始数据不改写；环境层生成带规则标记的校正列，覆盖期内直接使用 `stk_auction` |
| 日线和分钟线单位不同 | 横向校验和 snapshot 拼接 | `daily.vol=手`、`daily.amount=千元`；分钟 `vol=股`、`amount=元` |
| `share_float_complete` 可能仍有触顶风险 | 解禁供给压力 | 专用入口补全并生成 union；exact-6000 标记 `source_cap_risk`；canonical 行按声明业务键去重（数值修订不再产生重复事件行），重建以业务键覆盖不缩水为门禁 |
| 历史分钟线与日线股票池不完全一致 | 早期 NEEQ/BSE 迁移、停牌退市 | 正式分钟审计用本地分钟覆盖口径；daily 覆盖对比只做专项 |
| 日频表覆盖口径不同 | `daily`、`daily_basic`、`stk_limit` 等 join | Environment snapshot 显式处理缺失，不默认全集一致 |
| 当前公司简介缺少历史可见时间 | 历史文本 Prompt 可能泄露未来业务描述 | 历史回测不直接使用 `stock_company.introduction`；公司上下文由历史名称、行业、主营业务构成和 as-of 文本生成 |
| TuShare 可能回写历史数据 | 近期和部分历史分区 | 定时任务强刷滚动窗口并写修正账本；旧非空、新空默认不覆盖 |
| `limit_list_d.limit_amount` 历史不稳定 | 打板和涨停强度字段 | raw 保留，默认不进入冻结交易输入；sentinel 发现源端会把历史数值回写为空 |
| 结构性重复业务键 | `block_trade`、`top_list` 等 | raw 保留，审计 warning；进入 snapshot 前必须扩展键、聚合或去重 |
| 同一标的集合近似三类信用资格 | 担保品、融资和融券可执行性与成本 | 回放临时共用成交日标的集合并采用假设费率；真实名单、券源、费率和风控数据到位后再拆分 |
| 财务多版本和公告日缺失 | 财务按时点可见 | 原始数据保留多版本；环境层构造 `fundamental_events` 后选择可见版本 |
| 宏观发布时间不精确 | 月度/季度数据 | 当前按源时间或保守延后；发布日程尚未接入历史关联，可能过度延迟或不够精确 |
| 宏观源端修订以原发布时间覆盖历史值 | range 文件整体替换、旧值不保留，修订会以历史 `available_at` 进入历史 PIT 快照 | 已知风险：修正账本仅记录发生过修订；旧值 vintage 保留机制待按实测修订频率评估 |
| 文本重复推送和转载 | 大模型证据 | raw 保留；快照层按截断文本载荷和可见时间去重，前缀相同的不同载荷可能被合并 |
| 质量报告未绑定新鲜度和数据摘要 | 旧报告可能为新快照放行 | 门禁只检查存在、解析和状态；运行前需人工确认报告覆盖当前数据，后续应增加绑定校验 |
| `anns_d.rec_time` / `report_rc.create_time` 对回填历史是 TuShare 采集时间（如 2025），不是发布时间 | 若直接使用会让历史公告/盈利预测在时间墙下不可见 | 入库按 -1~+3 天合理性检查回退（见 §1.7）；存量分区必须满足该规则 |
| 打板日终字段有盘中前视风险 | 打板策略 | 日终汇总字段不得用于盘中决策；真实盘中策略需分钟或盘口数据 |
| 2026-07-06 起日线量额含盘后定价成交 | 日线包含 15:05–15:30 成交，分钟线无对应 Bar | raw 不改写；依赖“分钟量和等于日线量”的检查或特征必须处理该断点 |
| 2026-07-06 起主板 ST/*ST 涨跌幅 5%→10% | 用 `pct_chg ≈ ±5%` 推断 ST 涨跌停的研究特征在该日后失效 | 交易约束层不受影响（`stk_limit` 为绝对价、逐日数据驱动，已实证核验切换正确）；研究特征应使用 `stk_limit` 而非比例启发式 |

**Revision ledger 路径**

```text
results/data_quality/revision_events.jsonl
results/data_quality/revision_summary.json
```

它记录源端修正，不等于顶层状态文件。默认 `downstream_status=pending_review`，表示下游 snapshot、缓存或实验结果是否需要重建尚未确认。

**当前账本规则**

- 正式 raw 根目录 `data/raw` 的源端修正写入 `results/data_quality/revision_events.jsonl`。
- 单元测试、临时 raw 目录和过程排查目录只写本地 `revision_events.jsonl`，不得污染正式账本。
- 正式账本不得出现 `/tmp` 路径；测试污染记录属于无效账本输入。
- `event_id` 是修正内容的稳定语义身份（不含观测时间）；正式账本只保留同一 `event_id` 的首次观测，重复探测仍可告警但不重复落账。告警行是仅含 `event_id`/dataset/partition/severity/issue 的紧凑单行，完整记录只存账本，不在运行日志重复完整 JSON。
- 每行统一为 `schema_version=2` 的固定字段集合（v2 移除恒定的 `record_type`、`downstream_status` 与 `dataset` 的重复字段 `api_name`）；只观察、不写 raw 的 sentinel 事件以 `null` 保留 `old_source_hash`、`new_source_hash`、`write_action` 和 `allow_empty_revision_overwrite`，不再通过缺字段表达状态。
- 业务键重复导致无法按键 diff 的分区，先做全表内容等价比较：内容一致不构成修正、不落账；仅在内容确实变化时记 `duplicate_key_rows` 事件。
- 账本保持单文件追加 + 按 `event_id` 尾部增量去重（首次扫描 O(文件)，实测数十 MB 级 <1s）。这是当前规模下的成本边界；活跃文件接近约 256MB 时再评估按事件数据年分片。
- 分页接口若连续返回重复的非空满页，会 fail fast，避免死循环或重复写入。
- `stock_basic` 代码加载只接受合法 A 股代码模式 `\d{6}.(SH|SZ|BJ)`。
- `bak_basic` 审计的预期交易日上限必须截到审计 `end_date`，不能把 `trade_cal` 的未来 lookahead 误报为缺失。

修正哨兵抽样监控日线、复权、每日指标、价格限制、停牌和涨跌停标签的全字段源端差异：

- 数据集范围与抽样规模只在定时配置中维护。
- 字段回写、空值回写或行键变化必须进入修正账本。
- 易回写的涨跌停金额字段不得进入冻结交易输入；只能保留为审计字段，或等待字段级版本化后再使用。

**官方文档索引**

- 权限说明：https://tushare.pro/document/1?doc_id=290
- 权限表：https://tushare.pro/document/2?doc_id=108
- 日线行情：https://tushare.pro/document/2?doc_id=27
- 复权因子：https://tushare.pro/document/2?doc_id=28
- 每日指标：https://tushare.pro/document/2?doc_id=32
- 历史分钟：https://tushare.pro/document/2?doc_id=370
- 开盘集合竞价：https://tushare.pro/document/2?doc_id=369
- 开盘啦榜单：https://tushare.pro/document/2?doc_id=347
- 连板天梯/最强板块：https://tushare.pro/document/1?doc_id=356 / https://tushare.pro/document/2?doc_id=357
- 龙虎榜/游资/热榜：https://tushare.pro/document/2?doc_id=106 / https://tushare.pro/document/2?doc_id=107 / https://tushare.pro/document/2?doc_id=311 / https://tushare.pro/document/2?doc_id=312 / https://tushare.pro/document/2?doc_id=320 / https://tushare.pro/document/2?doc_id=321
- 两融：https://tushare.pro/document/2?doc_id=58 / https://tushare.pro/document/2?doc_id=59 / https://tushare.pro/document/2?doc_id=326
- 上市公司公告：https://tushare.pro/document/2?doc_id=176
- 中国经济数据发布日程：https://tushare.pro/document/2?doc_id=461
- GDP：https://tushare.pro/document/2?doc_id=227
- 上交所融资融券交易实施细则解读（维保比例/保证金可用余额公式）：https://www.sse.com.cn/services/tradingservice/margin/edu/c/10074042/files/a1f1c4833302451fb9130dbb94116c56.pdf
- 国金证券融资融券业务页（利率、担保证券、维保比例公示）：https://www.gjzq.com.cn/main/a/rzrq/index.html
- CPI/PPI/PMI/货币供应/社融：https://tushare.pro/document/2?doc_id=228 / https://tushare.pro/document/2?doc_id=229 / https://tushare.pro/document/2?doc_id=325 / https://tushare.pro/document/2?doc_id=242 / https://tushare.pro/document/2?doc_id=310
- 利率与全球事件：https://tushare.pro/document/2?doc_id=202 / https://tushare.pro/document/2?doc_id=204 / https://tushare.pro/document/2?doc_id=205 / https://tushare.pro/document/2?doc_id=206 / https://tushare.pro/document/2?doc_id=233
- 全球指数/外汇/美国利率：https://tushare.pro/document/2?doc_id=211 / https://tushare.pro/document/2?doc_id=179 / https://tushare.pro/document/2?doc_id=218
- 央行货币政策执行报告：https://tushare.pro/document/2?doc_id=465
