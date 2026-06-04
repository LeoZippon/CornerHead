# Data Documentation

整理日期：2026-06-04

本文档记录 MacroQuant 当前认可的数据边界、下载与更新流程、审计规则、单位口径和 Raw PIT 合同。历史执行细节和阶段性排查记录写入 `LOGBOOK.md` 与 `docs/logbook/DETAILED_LOGBOOK.md`，不放在本文档里。

不要把 TuShare token 写入已跟踪仓库文件、命令日志或运行日志。下载脚本只从环境变量或 ignored local `.env` 读取：

```bash
export TUSHARE_TOKEN="..."
```

## 导航

- [1. 文档边界与数据域](#1-文档边界与数据域)
  - [1.1 代码与命令边界](#11-代码与命令边界)
  - [1.2 六个当前数据域](#12-六个当前数据域)
  - [1.3 Raw 层原则](#13-raw-层原则)
- [2. 数据域与数据表](#2-数据域与数据表)
  - [2.1 全局单位口径](#21-全局单位口径)
  - [2.2 基础研究数据](#22-基础研究数据)
    - [2.2.1 基础维表](#221-基础维表)
    - [2.2.2 日频行情与交易约束](#222-日频行情与交易约束)
    - [2.2.3 财务与基本面](#223-财务与基本面)
  - [2.3 宏观与全球上下文](#23-宏观与全球上下文)
  - [2.4 历史分钟线](#24-历史分钟线)
  - [2.5 事件与资金数据](#25-事件与资金数据)
  - [2.6 打板专题数据](#26-打板专题数据)
  - [2.7 文本 Evidence](#27-文本-evidence)
- [3. 下载与更新](#3-下载与更新)
  - [3.1 初始下载与整理](#31-初始下载与整理)
  - [3.2 日常增量更新](#32-日常增量更新)
  - [3.3 修正监督与 Revision Ledger](#33-修正监督与-revision-ledger)
  - [3.4 share_float 完整补全](#34-share_float-完整补全)
  - [3.5 定时更新与夜间审计](#35-定时更新与夜间审计)
  - [3.6 限频、分页与下载前检查](#36-限频分页与下载前检查)
- [4. 审计与 Status](#4-审计与-status)
  - [4.1 顶层 status 文件](#41-顶层-status-文件)
  - [4.2 Status 文件结构](#42-status-文件结构)
  - [4.3 通用审计层](#43-通用审计层)
  - [4.4 基础研究数据审计](#44-基础研究数据审计)
  - [4.5 宏观与全球上下文审计](#45-宏观与全球上下文审计)
  - [4.6 历史分钟线审计](#46-历史分钟线审计)
  - [4.7 事件/资金数据审计](#47-事件资金数据审计)
  - [4.8 打板专题数据审计](#48-打板专题数据审计)
  - [4.9 文本 Evidence 审计](#49-文本-evidence-审计)
- [5. Raw PIT 数据合同](#5-raw-pit-数据合同)
  - [5.1 原始层元数据](#51-原始层元数据)
  - [5.2 Raw 可见性原则](#52-raw-可见性原则)
  - [5.3 可见性速查](#53-可见性速查)
  - [5.4 交给 Environment 的最小合同](#54-交给-environment-的最小合同)
  - [5.5 跨域 PIT 要求](#55-跨域-pit-要求)
- [6. 全文数据风险与口径修正总结](#6-全文数据风险与口径修正总结)
- [7. 官方文档索引](#7-官方文档索引)

## 1. 文档边界与数据域

### 1.1 代码与命令边界

TuShare 实现位于 `src/hl_trader/data_sources/tushare/`：

| 文件 | 职责 |
|---|---|
| `common.py` | 共享常量、接口合同、TuShare client、路径/日期/sidecar/PIT helper |
| `download.py` | 下载、日常更新、分钟线整理、`share_float_complete` union |
| `audit.py` | 当前 raw 数据审计和 status 报告 |
| `cron_update.py` | cron-safe 更新与审计 runner |

命令入口位于 `scripts/tushare/`，用于手工命令和 cron 调度；稳定业务逻辑由 `src/hl_trader/data_sources/tushare/` 提供。

### 1.2 六个当前数据域

下载、审计和顶层 status 使用同一套语义数据域。`reference`、`daily`、`fundamental`、`macro`、`global` 等只是下载子步骤，用来控制依赖、限频和体量，不作为额外的人读数据域。

| 数据域 | 下载子步骤 | 顶层 status | 审计入口 |
|---|---|---|---|
| 基础研究数据 | `reference`、`daily`、`fundamental` | `base_research_status.json` | `scripts/tushare/audit.py base --include-limit-list` |
| 宏观与全球上下文 | `macro`、`global` | `macro_context_status.json` | `scripts/tushare/audit.py macro` |
| 历史分钟线 | `intraday`、`compact-intraday-by-date`、日常按日分钟更新 | `intraday_minutes_status.json` | `scripts/tushare/audit.py intraday-by-date` |
| 事件/资金数据 | `event_flow`、`download-share-float-complete` | `event_flow_status.json` | `scripts/tushare/audit.py event-flow` |
| 打板专题数据 | `board_trading` | `board_trading_status.json` | `scripts/tushare/audit.py board-trading` |
| 文本 evidence | `text_evidence` | `text_evidence_status.json` | `scripts/tushare/audit.py base --include-text` |

### 1.3 Raw 层原则

- Raw 层尽量保留 TuShare 原始字段和原始行，不在下载阶段做特征化、回测选择或 aggressive 去重。
- Raw 层必须保留可追溯元数据：接口名、请求参数、抓取时间、source hash、sidecar。
- Raw 层只在能够保守推断时写入 `available_at`；更精确的 PIT selector 在 Environment 层实现。
- 多版本财报、重复公告、稀疏事件、源端重复推送和 source cap 风险在 raw 层标记和审计，不静默覆盖。

## 2. 数据域与数据表

### 2.1 全局单位口径

| 数据 | 单位规则 |
|---|---|
| `daily.vol` | 手 |
| `daily.amount` | 千元 |
| `stk_mins.vol` | 股 |
| `stk_mins.amount` | 元 |
| `daily_basic.total_share/float_share/free_share` | 万股 |
| `daily_basic.total_mv/circ_mv` | 万元 |
| `bak_basic` | 不含 `vol` / `amount`，不能用于成交量或成交额口径对齐；股本/资产字段是粗快照 |
| `bak_daily.vol` | 可与 `daily.vol` 对比 |
| `bak_daily.amount` | 万元；和 `daily.amount` 千元比较时需乘以 10 |
| 财报主表金额字段 | 元 |
| `forecast_vip` 利润预测字段 | 万元 |
| 宏观金额字段 | 保持 TuShare 官方原始单位；`cn_gdp`、`cn_m`、`sf_month` 主要是亿元口径 |
| 事件/资金 | `moneyflow` 量为手、金额为万元；`margin` 两融金额为元；`margin_secs` 是标的资格表不代表券商券源；`block_trade.vol` 为万股 |

### 2.2 基础研究数据

#### 2.2.1 基础维表

| 数据 | 接口 | 范围/拉取方式 | 用途与边界 |
|---|---|---|---|
| 股票列表 | `stock_basic` | `list_status=L/D/P` | 股票池基表，不能用 `stock_company` 替代 |
| 上市公司信息 | `stock_company` | `exchange=SSE/SZSE/BSE` | 公司属性补充；覆盖不等于全股票池 |
| 历史每日股票列表 | `bak_basic` | 按交易日循环，2016 起 | 补充每日行业、估值、股本快照；首个非空日为 `20160809` |
| 交易日历 | `trade_cal` | `SSE/SZSE/BSE`，2010 至今 | WFO、调仓和交易日判断；以 SSE/SZSE 为主 |
| 曾用名/ST 历史 | `namechange` | 全量或按股票代码 | 使用 `ann_date`/保守 `available_at`，不要用未来 `start_date` 泄漏 |
| 行业分类 | `index_classify` | `src=SW2021` | 申万行业层级 |
| 行业成分 | `index_member_all` | 按一级行业循环 | 历史行业暴露 |

#### 2.2.2 日频行情与交易约束

| 数据 | 接口 | 范围/拉取方式 | 用途与边界 |
|---|---|---|---|
| 日线行情 | `daily` | 按 `trade_date` | OHLCV、成交额 |
| 复权因子 | `adj_factor` | 按 `trade_date` | 复权价格构造和收益校验；默认不作为 PIT alpha 收益输入 |
| 每日指标 | `daily_basic` | 按 `trade_date` | PE/PB/PS、股息率、市值、换手率、股本 |
| 涨跌停价格 | `stk_limit` | 按 `trade_date` | 涨跌停执行约束 |
| 停复牌 | `suspend_d` | 按 `trade_date` 或日期区间 | 停牌/复牌 |
| 涨跌停/炸板列表 | `limit_list_d` | 按 `trade_date` | 默认保留；用于打板标签、炸板/回封事件和次日事件特征 |

日频行情结构完整。已知语义边界是 `daily`、`daily_basic`、`stk_limit` 覆盖口径不同，特征层必须显式处理缺失或使用内连接。

`limit_list_d` 在 raw 和审计域里属于 `daily`/基础研究数据，因为它按交易日分区并服务交易约束、日终事件标签和 cross-check；打板专题研究会复用它，但它不是 `board_trading` tier 的下载项。

#### 2.2.3 财务与基本面

| 数据 | 接口 | 范围/拉取方式 | 用途与边界 |
|---|---|---|---|
| 利润表 | `income_vip` | 按报告期 | 保留 `f_ann_date/report_type/comp_type`，用于 PIT 财报版本选择 |
| 资产负债表 | `balancesheet_vip` | 按报告期 | 单次大窗口可能触顶，保留多版本记录 |
| 现金流量表 | `cashflow_vip` | 按报告期 | 单次大窗口可能触顶，保留多版本记录 |
| 财务指标 | `fina_indicator_vip` | 按报告期 | 无 `f_ann_date` 时按 `ann_date` 更保守 |
| 业绩预告 | `forecast_vip` | 按公告月 | 事件和预期修正 |
| 业绩快报 | `express_vip` | 按公告月 | 财报前置可用信息 |
| 分红送股 | `dividend` | 全 `stock_basic` 代码 | PIT 可见性只用 `imp_ann_date/ann_date`；`ex_date/record_date/pay_date` 仅作事件属性 |
| 审计意见 | `fina_audit` | 全 `stock_basic` 代码 | 需按 `ts_code` 拉取 |
| 主营业务构成 | `fina_mainbz_vip` | 全 `stock_basic` 代码 | period 查询易触顶，优先按股票代码 |
| 披露计划 | `disclosure_date` | 按报告期 | 披露计划/实际披露日期，不是数值表 |

财务基本面原始层保留多版本记录、重复业务键、少量空公告日和稀疏事件分区。raw 层仍按 TuShare 稳妥查询方式存储：报表按报告期、预告/快报按公告月、分红/审计意见/主营业务构成按 `ts_code` 快照。Environment 会再构造 `fundamental_events` PIT-ready 事件层，按 `available_month` 输出，供 `daily_alpha` 或 Agent evidence 按可见时间选择。

### 2.3 宏观与全球上下文

宏观/全球数据先作为 regime context 和 LLM evidence，不直接替代日频股票特征。落盘路径仍使用 `data/raw/<dataset>/...`。

| 数据 | 接口 | 范围/拉取方式 | 用途与边界 |
|---|---|---|---|
| 经济数据发布日程 | `cn_schedule` | 按月 `m=YYYYMM` | 用 `publish_date` 修正 CPI/PPI/PMI/货币供应等宏观数据可见时间 |
| GDP | `cn_gdp` | `start_q/end_q` | 季度宏观 regime，默认季末+45天保守可见 |
| CPI/PPI/PMI | `cn_cpi` / `cn_ppi` / `cn_pmi` | `start_m/end_m` | 通胀和景气度；默认月末+31天保守可见 |
| 货币供应与社融 | `cn_m` / `sf_month` | `start_m/end_m` | 流动性 regime；金额字段保持官方亿元口径 |
| 利率与回购 | `shibor` / `shibor_quote` / `shibor_lpr` / `repo_daily` | 按年 | 资金价格；date-only 数据不得用于同日开盘决策 |
| 港/外币拆借利率 | `hibor` / `libor` | `hibor` 按年，`libor` 按货币+年份 | 离岸/外币流动性 |
| 美国利率 | `us_tycr` / `us_trycr` / `us_tbr` / `us_tltr` | 按年 | 全球利率环境；date-only 保守晚间可见 |
| 全球财经日历 | `eco_cal` | 按月，可选 `country/currency/event` | 事件值异构，必须按事件解析 |
| 全球指数 | `index_global` | 主要指数代码+年份 | 跨市场风险偏好；OHLC 为指数点位 |
| 外汇日线 | `fx_daily` | 主要外汇代码+年份 | 汇率上下文；bid/ask quote，不是股票成交量 |
| 央行货币政策执行报告 | `monetary_policy` | 按发布年份，含 HTML/PDF 链接 | 政策文本 evidence，先不直接影响下单 |

### 2.4 历史分钟线

| 数据 | 接口 | 范围/拉取方式 | 用途与边界 |
|---|---|---|---|
| 历史 1 分钟源 | `stk_mins` | 全 A 股票池，按 `ts_code + year`，`freq=1min` | 批量下载和可追溯源层；只下载 1min，其他频率从 1min 重采样 |
| 历史 1 分钟按日文件 | 本地整理 | 从 `stk_mins` 源层整理为每交易日全市场文件 | PIT 回放、日内特征和后续每日增量更新优先读取该层 |
| 实盘/实时分钟 | `rt_min` / `rt_min_daily` | 仅实盘阶段使用 | 不并入历史 raw 下载 |
| 开/收盘竞价 | `stk_auction` / `stk_auction_c` | 不做历史全量下载 | 历史竞价由 `stk_mins` 的 `09:30` 和 `15:00` 分钟条承载；`stk_auction` 用于实盘开盘竞价和历史校验 |

批量下载源路径为 `data/raw/stk_mins_1min/ts_code=<TS_CODE>/year=<YYYY>.parquet`。活跃按日最终路径为 `data/raw/stk_mins_1min_by_date/trade_date=<YYYYMMDD>.parquet`，字段包含 `ts_code, trade_time, open, high, low, close, vol, amount, trade_date, available_at, available_at_rule`。

完整按日整理只保留最终文件。整理过程必须通过 schema、重复键、日期、时间、可见性和可选股票池覆盖校验后落盘。使用分钟特征前应按有效股票池过滤。

历史 09:30 分钟条可作为开盘竞价近似，但深圳股票存在已知口径偏差；修正规则见 [6. 全文数据风险与口径修正总结](#6-全文数据风险与口径修正总结)。

### 2.5 事件与资金数据

| 数据 | 接口 | 范围/拉取方式 | 用途与边界 |
|---|---|---|---|
| 两融汇总 | `margin` | 按 `trade_date` | 杠杆与市场情绪 |
| 两融明细 | `margin_detail` | 按 `trade_date` | 个股融资融券压力 |
| 融资融券标的 | `margin_secs` | 按 `trade_date` | 盘前融资融券资格列表；只能表示交易所标的资格，不等于券商实际可融券源 |
| 个股资金流 | `moneyflow` | 按 `trade_date` | 资金行为因子 |
| 股东人数 | `stk_holdernumber` | 按公告月 | 筹码集中度 |
| 股东增减持 | `stk_holdertrade` | 按公告月 | 公司治理/事件 |
| 回购 | `repurchase` | 按公告月 | 资本配置与安全边际 |
| 解禁 | `share_float_complete` | `share_float` 多路径补全后生成 union | 供给压力；以 complete union 作为保留边界 |
| 大宗交易 | `block_trade` | 按 `trade_date` | 特殊交易行为 |

事件/资金通用下载入口负责两融、融资融券标的、资金流、股东、回购和大宗交易。`share_float` 使用专用 `download-share-float-complete` 入口生成 `share_float_complete` union。日频资金表按交易日分区，股东/回购等稀疏公告表按月份分区。真实融券执行仍需要券商侧券源、担保品、费率和风控线数据；停更的转融券接口不作为当前项目数据项维护。

解禁的活跃保留边界是 `share_float_complete/share_float_complete.parquet`。`float_date` 日分区、`ann_date` 主路径和 candidate 级 `ann_date+ts_code` 补充文件属于补全过程产物，可归档。`share_float` 当前使用 `ann_date` 作为 PIT 主路径，并对触顶 `ann_date` 分区执行 candidate 级 `ann_date+ts_code` 补充。candidate 补充后仍存在最细 `ann_date+ts_code` 文件正好 6000 行时，只能标记 `source_cap_risk`，不能声称数学意义上完全无截断。

候选股票救援顺序：

1. 触顶分区自身已经出现的 `ts_code`。
2. 另一条 `share_float` 路径交叉出现的 `ts_code`，例如 `float_date` 触顶时扫描 `ann_date` 路径里 `float_date=目标日` 的记录。
3. `anns_d` 中标题包含限售、解禁、上市流通等关键词的公告 `ts_code`。
4. 显式传入的 `--rescue-code` 或 `--rescue-codes-file`。
5. 只有显式 `--rescue-universe all_a` 时才全 A。

### 2.6 打板专题数据

当前 raw 边界已保留打板研究的基础数据，可以支撑“日终标签 + 分钟回放”的策略验证。下表把专题所需接口直接列出；其中 `board_trading` 是专题下载域，其他接口复用各自语义数据域，不重复定义下载归属：

| 需求 | 接口/数据项 | 主数据归属 | 范围/拉取方式 | 用途与边界 |
|---|---|---|---|---|
| 涨停/跌停价格 | `stk_limit` | 基础研究/日频行情与交易约束 | 按 `trade_date` | 盘前交易约束和涨跌停价判断；历史 PIT 中按可见时点使用 |
| 日终涨跌停/炸板标签 | `limit_list_d` | 基础研究/日频行情与交易约束 | 按 `trade_date` | 识别涨停、跌停、炸板、回封次数、首次/最后封板时间、封单额等；不得在盘中提前使用日终汇总字段 |
| 开盘啦榜单 | `kpl_list` | 打板专题 | `tag + trade_date` | 默认保留开盘啦涨停、炸板、跌停、自然涨停、竞价标签；按次日 08:30 可见处理 |
| 连板梯队和涨停概念 | `limit_step`、`limit_cpt_list` | 打板专题 | 按 `trade_date` | 连板高度、涨停梯队、涨停概念聚类和板块情绪；按日终或次日保守可见 |
| 同花顺涨跌停榜单 | `limit_list_ths` | 打板专题 | `limit_type + trade_date`，官方历史从 `20231101` 开始 | 默认保留涨停池、连扳池、冲刺涨停、炸板池、跌停池；与 `limit_list_d` 口径不同，不能互相覆盖 |
| 龙虎榜和机构席位 | `top_list`、`top_inst` | 打板专题 | 按 `trade_date` | 涨停后资金性质、机构/营业部参与和异动复盘；按当日 20:00 可见处理 |
| 游资/热钱参考 | `hm_list`、`hm_detail` | 打板专题 | `hm_list` 静态表；`hm_detail` 按 `trade_date`，官方历史从 `20220801` 开始 | 游资席位映射和热钱参与记录；静态表不强制历史 PIT 时间 |
| 热榜情绪 | `ths_hot`、`dc_hot` | 打板专题 | `market/hot_type/is_new + trade_date` | 同花顺热股、行业板块、概念板块与东方财富人气/飙升榜；优先使用 `rank_time`，保留 `is_new=N` 快照 |
| 分钟级触板/开板回放 | `stk_mins_1min_by_date`、`stk_limit` | 历史分钟线 + 基础研究/交易约束 | 按交易日分钟文件 | 用分钟 OHLC 与涨停价推导首次触板、开板和尾盘状态；分钟粒度无法还原逐笔排队 |
| 流动性和可交易过滤 | `daily_basic`、`moneyflow`、`suspend_d`、`namechange` | 基础研究 + 事件/资金 | 按交易日或公告分区 | 过滤停牌、ST/曾用名、成交额、市值、换手、资金流 |
| 开/收盘竞价近似 | `stk_mins_1min_by_date` | 历史分钟线 | 按交易日分钟文件 | 使用 09:30/15:00 分钟条作为历史竞价近似；不全量下载集合竞价接口 |

可见性边界：

- `kpl_list` 按官方次日 08:30 可见处理。
- `top_list/top_inst` 按 20:00 可见处理。
- `ths_hot/dc_hot` 优先使用 `rank_time`。
- `limit_step`、`limit_cpt_list`、`hm_detail` 等交易日榜单按收盘后或次日保守可见处理。
- `limit_list_ths` 官方历史从 `20231101` 开始；早于接口边界的日期不视为缺失。
- `hm_detail` 从 `20220801` 开始；早于接口边界的日期不视为缺失。

`limit_list_ths` 和 `limit_list_d` 都可作为打板标签/情绪 evidence，但口径不同：`limit_list_d` 是每日涨跌停/炸板统计，`limit_list_ths` 是同花顺榜单池子。二者进入特征层前必须按 `available_at` 过滤，并保留来源字段，不能互相覆盖。

如果未来从“日终打板事件研究”升级到“真实盘中打板执行”，Environment 层需要补充专门的 PIT 特征构造和执行约束：用当时已走完的分钟 bar 判断是否触板、是否开板、是否可下单，不使用 `limit_list_d.first_time/open_times/fd_amount` 这类日终汇总字段做盘中决策。更精确的排队成交、封单变化和撤单行为需要 QMT 实时盘口或更高频 Level-2 数据。

### 2.7 文本 Evidence

文本 Evidence 是独立 raw tier，不并入基础/日频/财务默认链路。

| 数据 | 接口 | 范围/拉取方式 | 用途与边界 |
|---|---|---|---|
| 上市公司全量公告 | `anns_d` | 按公告月 | 优先 `rec_time`，缺失时保守视为收盘后/次日可见 |
| 长新闻 | `major_news` | 按月份 | `pub_time` 为 `available_at` |
| 新闻联播 | `cctv_news` | 按日期 | 只有日期时保守设为当日晚间可见 |
| 政策法规库 | `npr` | 按月份 | `pubtime` 为 `available_at`，HTML 保留 raw/hash |
| 券商研究报告 | `research_report` | 按月份 | `trade_date` 只有日期，不能给盘中策略使用 |
| 卖方盈利预测 | `report_rc` | 按月份 | 优先 `create_time`，否则按晚间更新保守可见 |
| 新闻快讯 | `news` | 按来源+日期 | 自动展开 9 个官方 `src`；`datetime` 为 `available_at` |

进入模型前必须生成 `evidence_id`、`document_hash`、`available_at`、`source_quality`，并做正文长度限制和公司/行业实体映射。

## 3. 下载与更新

### 3.1 初始下载与整理

第一次建库或大窗口重建时，先按数据依赖顺序下载和整理，再启用日常更新：

1. 基础研究数据：先 `reference`，再 `daily`，最后 `fundamental`。
2. 宏观与全球上下文：`macro` 与 `global` 可在基础研究数据完成后补充。
3. 历史分钟线：先批量下载 `intraday` 源层，再整理为按日最终层。
4. 事件/资金数据：下载 `event_flow`，并用 `download-share-float-complete` 生成解禁最终 union。
5. 打板专题数据：下载 `board_trading`。
6. 文本 evidence：下载公告、新闻、政策、研报、盈利预测文本源。

正式下载、整理入口：

```bash
~/miniconda3/bin/conda run -n stock python scripts/tushare/download.py download --tier reference
~/miniconda3/bin/conda run -n stock python scripts/tushare/download.py download --tier daily --include-limit-list
~/miniconda3/bin/conda run -n stock python scripts/tushare/download.py download --tier fundamental --start-date 20100101 --end-date <YYYYMMDD>
~/miniconda3/bin/conda run -n stock python scripts/tushare/download.py download --tier macro --start-date 20200101 --end-date <YYYYMMDD>
~/miniconda3/bin/conda run -n stock python scripts/tushare/download.py download --tier global --start-date 20200101 --end-date <YYYYMMDD>
~/miniconda3/bin/conda run -n stock python scripts/tushare/download.py download --tier intraday --datasets stk_mins --start-date 20200101 --end-date <YYYYMMDD>
~/miniconda3/bin/conda run -n stock python scripts/tushare/download.py compact-intraday-by-date --start-date 20200101 --end-date <YYYYMMDD>
~/miniconda3/bin/conda run -n stock python scripts/tushare/download.py download --tier event_flow --start-date 20200101 --end-date <YYYYMMDD>
~/miniconda3/bin/conda run -n stock python scripts/tushare/download.py download-share-float-complete --ann-start-date 20100101 --ann-end-date <YYYYMMDD> --float-start-date 20200101 --float-end-date <YYYYMMDD> --rescue-ann-limit-hits --write-union
~/miniconda3/bin/conda run -n stock python scripts/tushare/download.py download --tier board_trading --start-date 20200101 --end-date <YYYYMMDD>
~/miniconda3/bin/conda run -n stock python scripts/tushare/download.py download --tier text_evidence --start-date 20200101 --end-date <YYYYMMDD>
```

### 3.2 日常增量更新

日常增量更新入口：

```bash
~/miniconda3/bin/conda run -n stock python scripts/tushare/download.py update --start-date <YYYYMMDD> --end-date <YYYYMMDD>
```

| 规则 | 当前语义 |
|---|---|
| 更新窗口 | `update` 从 `start_date` 扫到 `end_date`，不是只更新当天；日常 cron 回看 30 天，首次补历史可把 `start_date` 设为研究窗口下界 |
| 交易日历前置 | `update` 默认把 `trade_cal` 额外补到 `end_date+7`，供次日盘前更新和 `daily_alpha` 下一交易日映射使用；直连日线、事件/资金、打板和分钟入口也会在读取交易日历前补齐请求窗口；`trade_cal` 即使被 force 刷新也按年分区合并写入，不用小窗口响应覆盖整年日历 |
| 默认写入 | skip-existing：已有且 sidecar 覆盖当前请求范围的分区跳过，缺失分区自动补充；当前月/年等聚合分区覆盖不到新 `end_date` 时重拉 |
| 聚合分区强刷 | 月/年聚合分区在开放窗口强刷时只替换本次窗口内的旧行，并保留同月/同年窗口外旧行，避免 30 天滚动窗口把月初或年初数据截掉 |
| 日期覆盖比较 | 将 `YYYYMMDD`、`YYYYMMDDHHMMSS`、`YYYY-MM-DD HH:MM:SS` 归一到同一时间边界 |
| 强刷入口 | `--force` 强制重拉；cron 使用 `--refresh-reference-datasets`、`--refresh-daily-datasets`、`--refresh-open-window` 做定向开放窗口强刷 |
| 宏观保留窗口 | `update` 的日常窗口可以只回看 30 天，但 `macro/global` 的月度/季度 range 型数据默认仍从 `20200101` 保留窗口下界维护，避免生成只覆盖滚动窗口的短 range 文件 |
| 更新顺序 | 基础维表、日频行情、宏观、全球、事件/资金、打板专题、按日分钟线、解禁 union、文本 evidence、财务基本面 |
| 重型数据开关 | 可用 `--no-include-intraday`、`--no-include-share-float-complete`、`--no-include-board-trading` 临时跳过；可用 `--skip-bak-basic` 跳过 `bak_basic` |
| 空响应保护 | 本地已有非空分区而远端本次为空时，默认记录 revision 事件并跳过覆盖；只有显式 `--allow-empty-revision-overwrite` 才允许空覆盖 |
| 未发布保护 | 必需日频和交易日事件接口当日未发布返回 0 行时只 `skipped_write`，不写半成品；分钟线预期股票池非空时拒绝写 0 行文件 |
| 分钟 universe | 已有按日分钟文件默认按本地分钟 universe 校验；新交易日文件不存在时回退到 `daily` 股票池 |

| 数据域 | 数据项 | 源端节奏 | 日常刷新规则 | 风险控制 |
|---|---|---|---|---|
| 基础维表 | `stock_basic`、`stock_company`、`index_classify`、`index_member_all` | 低频或不定期 | 每日晚间强制刷新 | 远端空响应不会覆盖本地非空分区 |
| 基础维表 | `namechange` | 公告驱动，官方未标固定入库时刻 | 每日晚间全股票循环强制刷新，`0.50s` 限频 | 使用唯一最终表；空响应不覆盖非空本地分区 |
| 基础维表 | `trade_cal`、`bak_basic` | 交易日历定期维护；`bak_basic` 按交易日 | `trade_cal` 覆盖不足时补齐并按年合并；`bak_basic` 晚间滚动窗口强刷 | `bak_basic` 不作为成交量/成交额主口径；差异写 revision ledger |
| 日频行情与约束 | `daily`、`adj_factor`、`daily_basic`、`stk_limit`、`suspend_d`、`limit_list_d` | 交易日发布，时间从盘前到收盘后不等 | cron 对最近 30 天强制刷新；手工大窗口默认补缺，除非显式 `--refresh-daily-datasets` | 记录 revision ledger；`suspend_d`、`limit_list_d` 旧非空、新空默认不覆盖 |
| 财务与基本面 | 报表、指标、预告、快报、披露计划 | 财报或公告驱动 | 最近 6 个报告期、最近 3 个公告月强刷 | `dividend/fina_audit/fina_mainbz_vip` 是按 `ts_code` 返回的历史快照，不按日期全市场刷新；日常先从最近强刷的报表/预告/快报/披露计划中提取 90 日内可见事件股票，再用 `dividend` 的 `ann_date/imp_ann_date/ex_date/record_date` 探针补充分红候选股票，只对这些股票强刷 |
| 宏观与全球上下文 | 宏观、利率、全球事件、指数、外汇、政策文本 | 日、月、季、年或事件驱动 | 每日晚间刷新当前开放窗口；月度/季度 range 型数据用 `20200101` 保留窗口下限维护全窗口覆盖 | 历史封闭分区仍 skip-existing；旧非空、新空默认不覆盖并写 revision ledger |
| 历史分钟线 | `stk_mins_1min_by_date` | 历史分钟收盘后处理，通常 17:00-21:00 完成 | 每日晚间按日更新最近窗口，并只对最近 1 个自然日小窗口强刷 | 新交易日按 `daily` 股票池拉取；已有文件按本地分钟 universe 校验；不做 30 天全量强刷 |
| 事件/资金 | `margin`、`margin_detail` | 官方 09:00 更新上一交易日 | 09:05/09:15 盘前强制回补 | 非交易日前一天自动跳过，不报错 |
| 事件/资金 | `margin_secs` | 盘前更新当日融资融券标的 | 09:03/09:13 盘前强制刷新，并参与晚间滚动更新 | 只代表交易所资格，不代表券商可融券源 |
| 事件/资金 | `moneyflow`、`block_trade`、股东、回购、`share_float_complete` | 收盘后、公告或定期 | 晚间滚动窗口强刷；解禁每日强刷近期过程分区并重建 union | `share_float` 对 6000 行触顶分区做 candidate 救援；旧非空、新空默认不覆盖并写 revision ledger |
| 打板专题 | `kpl_list`、`limit_step`、`limit_cpt_list`、`limit_list_ths`、龙虎榜、热榜 | 日终或次日盘前 | 晚间滚动窗口强刷，08:50 回补关键盘前榜单 | 早于官方历史起点的日期不视为缺失；旧非空、新空默认不覆盖并写 revision ledger |
| 文本 evidence | 公告、新闻、政策、研报、盈利预测 | 事件推送或日/月分区 | 晚间滚动窗口强刷，08:55 回补 `cctv_news/news` | 按来源、月份、日期分区；旧非空、新空默认不覆盖；进入 Agent 前按 `available_at` 过滤 |

### 3.3 修正监督与 Revision Ledger

Revision ledger 是源端修正事件账本，路径为 `results/data_quality/revision_events.jsonl`。它不是顶层 status 文件，而是 append-only 事件流，用于提示哪些 raw 分区发生了源端回写或本地/远端不一致，进而触发衍生特征、PIT cache、回测缓存或实验 ledger 的人工/自动复核。

写入来源：

- `force_refresh`：日常 cron 在滚动窗口内强制刷新日频、`bak_basic`、财务开放报告期/公告月、宏观/全球开放窗口、事件/资金、打板专题和文本 evidence 分区。若本地旧分区和 TuShare 当前返回不一致，先写 `REVISION_ALERT`，再按安全规则决定是否覆盖 raw。
- `share_float_union_rebuild`：每日重建 `share_float_complete` union 时记录 union 级差异；如果 union 行数缩水且未显式允许，脚本直接阻断覆盖。
- `sentinel_probe`：`audit.py revision-sentinel` 抽样检查历史日频分区，只比较当前源端返回和本地 raw，不覆盖 raw。
- `history_sample_probe`：`audit.py revision-history-sample` 按年份分层抽样检查 active 交易日分区接口，只比较当前源端返回和本地 raw，不覆盖 raw；输出放在 `results/data_quality/process/`，用于专项统计分析，不作为顶层 status。

事件字段：

| 字段 | 含义 |
|---|---|
| `detected_at` | 发现时间，UTC |
| `source` | `force_refresh`、`share_float_union_rebuild`、`sentinel_probe` 或 `history_sample_probe` |
| `dataset` / `partition` / `path` | 发生差异的数据项、分区和本地文件 |
| `severity` | 数据项级别的影响强度，例如日线和涨跌停为 high |
| `downstream_status` | 默认 `pending_review`，表示下游缓存和实验结果尚未确认 |
| `key_columns` | 用于比较业务键的列 |
| `old_rows` / `new_rows` | 本地旧分区和源端当前响应行数 |
| `old_source_hash` / `new_source_hash` | 本地旧 sidecar 和本次响应的源数据 hash |
| `write_action` | `overwrite` 或 `skipped_empty_revision_overwrite` |
| `changed_keys` / `added_keys` / `removed_keys` | 业务键级差异计数 |
| `changed_columns` | changed key 中各字段发生变化的次数统计 |
| `changed_columns_sample` | 最多 5 个 changed key 的字段级 old/new 样本，每个 key 最多记录 12 个变化字段 |
| `added_rows_sample` / `removed_rows_sample` | 最多 5 条新增或删除业务键的规范化行值样本 |
| `missing_key_columns_*` / `duplicate_key_rows_*` / `comparison_issue` | key 缺失或重复键等比较异常 |
| `affected_ts_codes*` | 可识别股票代码的影响数量和样本 |

处理规则：

- `force_refresh` 对普通非空修正会覆盖 raw，因此 ledger 用于提示下游重建；对 zero-ok 数据集的“旧非空、新空”默认不覆盖。
- `sentinel_probe` 不覆盖 raw；若发现 revision、本地样本分区缺失或样本无有效检查，summary 为 `warning`，默认返回 0，便于 cron 持续运行；若出现 API 错误或必需数据集远端 0 行，summary 为 `error` 并返回非 0。
- `history_sample_probe` 不覆盖 raw，默认按每年 3 个 SSE 交易日抽样；它覆盖日频主表、`bak_basic`、event/flow 交易日表和 board-trading 交易日/组合分区。宏观、财务、文本月度/日期源和 `share_float` union 需要按月、报告期、公告月、代码或 union 专项另行抽样。
- `revision_summary.json` 记录最近一次 sentinel 的样本、错误、缺本地分区和事件样本；正式数据质量仍以 6 个顶层 status 文件为准。
- 字段级样本只对新产生的 revision event 生效；历史已写入的 JSONL 事件不会回填 old/new 字段值。
- `pending_review` 的关闭不在下载脚本里自动完成。后续应由 Environment/PIT 或实验流程在重建相关缓存后写入独立处理记录，或在人工确认后归档事件。

### 3.4 share_float 完整补全

每日 `update` 默认运行 `share_float_complete`。近期 `ann_date`/`float_date` 下载窗口使用本次 `--start-date` 到 `--end-date`；union 重建窗口固定覆盖 `ann_date=20100101-<end_date>` 和 `float_date=20200101-<end_date>`，所以不会丢失历史 union。

默认会对触及 6000 行上限的近期 `ann_date` 分区执行 candidate 级补充，受 `--max-ann-rescue-days` 和 `--max-rescue-calls` 保护。union 重建会同时扫描 `data/raw` 和 `archive/data_raw/*` 中保留的 `share_float_ann_date`、`share_float_ann_date_ts_code`、`share_float_float_date`、`share_float_float_date_ts_code` 等过程目录；如果扫描结果会让既有 `share_float_complete` 行数缩小，脚本默认报错而不是覆盖。

`share_float` 补全入口：

```bash
PYTHONUNBUFFERED=1 ~/miniconda3/bin/conda run -n stock python scripts/tushare/download.py download-share-float-complete \
  --raw-dir data/raw \
  --ann-start-date 20100101 \
  --ann-end-date <YYYYMMDD> \
  --float-start-date 20200101 \
  --float-end-date <YYYYMMDD> \
  --rescue-ann-limit-hits \
  --write-union
```

触顶救援示例：

```bash
PYTHONUNBUFFERED=1 ~/miniconda3/bin/conda run -n stock python scripts/tushare/download.py download-share-float-complete \
  --raw-dir data/raw \
  --skip-ann-date \
  --rescue-ann-date 20200103 \
  --float-rescue-date 20200106 \
  --rescue-code 002973.SZ \
  --max-rescue-calls 1000
```

救援默认使用 `--rescue-universe candidate`，不会全 A 扫描。`--max-rescue-calls` 默认 50000，超过预算会 fail fast。`--rescue-universe all_a` 是完整扫描备用入口，不作为默认路线。

### 3.5 定时更新与夜间审计

TuShare 接口更新时间目录维护在 `configs/tushare_update_schedule.json`。该文件逐项记录当前脚本使用的全部接口、数据域、官方更新时间或更新频率、cron 覆盖策略和官方文档链接。

当前 cron 使用北京时间：

```cron
35 23 * * * cd /Data/lzp/MacroQuant && mkdir -p logs && /home/lzp/miniconda3/envs/stock/bin/python scripts/tushare/cron_update.py --job cn_evening_full >> logs/tushare_cron_dispatch.log 2>&1
30 2 * * * cd /Data/lzp/MacroQuant && mkdir -p logs && /home/lzp/miniconda3/envs/stock/bin/python scripts/tushare/cron_update.py --job cn_nightly_full_audit >> logs/tushare_cron_dispatch.log 2>&1
35 3 * * * cd /Data/lzp/MacroQuant && mkdir -p logs && /home/lzp/miniconda3/envs/stock/bin/python scripts/tushare/cron_update.py --job cn_nightly_feature_build >> logs/tushare_cron_dispatch.log 2>&1
0 4 * * * cd /Data/lzp/MacroQuant && mkdir -p logs && /home/lzp/miniconda3/envs/stock/bin/python scripts/tushare/cron_update.py --job cn_daily_revision_sentinel >> logs/tushare_cron_dispatch.log 2>&1
50 8 * * * cd /Data/lzp/MacroQuant && mkdir -p logs && /home/lzp/miniconda3/envs/stock/bin/python scripts/tushare/cron_update.py --job cn_preopen_board_backfill_0850 >> logs/tushare_cron_dispatch.log 2>&1
55 8 * * * cd /Data/lzp/MacroQuant && mkdir -p logs && /home/lzp/miniconda3/envs/stock/bin/python scripts/tushare/cron_update.py --job cn_preopen_text_backfill_0855 >> logs/tushare_cron_dispatch.log 2>&1
3 9 * * * cd /Data/lzp/MacroQuant && mkdir -p logs && /home/lzp/miniconda3/envs/stock/bin/python scripts/tushare/cron_update.py --job cn_preopen_margin_secs_backfill_0903 >> logs/tushare_cron_dispatch.log 2>&1
5 9 * * * cd /Data/lzp/MacroQuant && mkdir -p logs && /home/lzp/miniconda3/envs/stock/bin/python scripts/tushare/cron_update.py --job cn_preopen_margin_backfill_0905 >> logs/tushare_cron_dispatch.log 2>&1
13 9 * * * cd /Data/lzp/MacroQuant && mkdir -p logs && /home/lzp/miniconda3/envs/stock/bin/python scripts/tushare/cron_update.py --job cn_preopen_margin_secs_retry_0913 >> logs/tushare_cron_dispatch.log 2>&1
15 9 * * * cd /Data/lzp/MacroQuant && mkdir -p logs && /home/lzp/miniconda3/envs/stock/bin/python scripts/tushare/cron_update.py --job cn_preopen_margin_retry_0915 >> logs/tushare_cron_dispatch.log 2>&1
20 9 * * * cd /Data/lzp/MacroQuant && mkdir -p logs && /home/lzp/miniconda3/envs/stock/bin/python scripts/tushare/cron_update.py --job cn_preopen_event_flow_audit_0920 >> logs/tushare_cron_dispatch.log 2>&1
```

任务含义与门控边界：

| 任务 | 时间 | 窗口和数据 | 当前门控与注意 |
|---|---:|---|---|
| `cn_evening_full` | 23:35 | 从 `end_date-30` 滚动补缺到 `end_date`，覆盖 A 股日线、每日指标、分钟线、资金流、大宗交易、融资融券标的、打板专题、文本 evidence、宏观/全球上下文和财务基本面；不会每天从 `20200101` 重扫历史 | job 返回码和 cron state；失败会阻断本次 update 后续步骤。财务基本面排在最后，因此财务接口异常不会阻断分钟线、事件/资金和文本 evidence 落库 |
| `cn_evening_full` 强刷细节 | 23:35 | `stock_basic`、`stock_company`、`bak_basic`、`namechange`、`index_classify`、`index_member_all` 用 `--refresh-reference-datasets`；日频主表用 `--refresh-daily-datasets`；宏观/全球开放窗口、事件/资金、打板专题、文本 evidence 和解禁过程分区用 `--refresh-open-window` | revision-aware 写入；本地非空而远端为空默认不覆盖。按日分钟线只强刷最近 1 个自然日，避免 nightly 大面积重拉全市场分钟线 |
| `cn_nightly_full_audit` | 02:30 | 刷新 6 个正式 data-quality status；基础、宏观、分钟、打板和文本默认从 `20200101` 到前一自然日；事件/资金额外后移 1 天，避免早于 09:00 两融窗口产生预期内 error | 逐域继续执行并汇总返回码；某个域 error 不阻断其他 status 刷新。分钟线使用 `minute` 覆盖口径，不默认启用逐日全量行级 `--full-scan` |
| `cn_nightly_feature_build` | 03:35 | 构造并审计 `fundamental_events` PIT 事件层，再用该事件层刷新 `daily_alpha`；首次初始化从 `default_start_date` 开始，已有分区后按最近 120 天滚动维护 | 只门控本任务内的 `audit-fundamental-events`，不自动读取 6 个 raw status；严格生产门控应在 Pipeline/QMT 层显式读取 raw status 后再冻结特征 |
| `cn_daily_revision_sentinel` | 04:00 | 抽样检查历史日频分区，只比较 TuShare 当前返回与本地 raw 是否一致 | 不覆盖 raw；差异写 revision ledger 并刷新 `results/data_quality/revision_summary.json` |
| `cn_preopen_board_backfill_0850` | 08:50 | 强制刷新前一自然日 `kpl_list`、`limit_step`、`limit_cpt_list` | 覆盖开盘啦次日 08:30 发布和其他打板专题源端迟到风险 |
| `cn_preopen_text_backfill_0855` | 08:55 | 强制刷新前一自然日及再往前 2 天的 `cctv_news`、`news` | 修复周末/夜间文本源未落库或零行占位 |
| `cn_preopen_margin_secs_backfill_0903` / `cn_preopen_margin_secs_retry_0913` | 09:03 / 09:13 | 强制刷新当日 `margin_secs`，用于 09:25 前判断融资融券标的资格 | 直连事件/资金入口会先补齐当日 `trade_cal`；该表只代表交易所资格，不代表券商实际券源。当前日 status 依赖 raw job 返回码和文件新鲜度 |
| `cn_preopen_margin_backfill_0905` / `cn_preopen_margin_retry_0915` | 09:05 / 09:15 | 强制回补前一自然日 `margin` 和 `margin_detail` | 给 09:25 前快速审计、特征冻结和 Agent 决策留出时间；若前一自然日不是 SSE 交易日，任务成功跳过 |
| `cn_preopen_event_flow_audit_0920` | 09:20 | 在 09:15 两融重试后刷新前一日 `margin/margin_detail` 状态 | 防止 09:25 前门控使用过期 T+1 两融覆盖；不覆盖当日 `margin_secs`，当日 `margin_secs` 进入晚间/夜间完整事件/资金审计 |

runner 使用全局 `.runtime/tushare/locks/tushare_update.lock`，因此 cron job 不会并发读写同一套 raw 数据和状态文件。锁冲突时 runner 会等待配置的 `lock_wait_seconds`，发现死进程或超过 `lock_stale_seconds` 的锁会清理；等待超时返回非 0 并写入 cron state，避免静默丢任务。每次运行都会把资源检查、完整命令和返回码写入 `logs/tushare_cron_<job>_<end_date>_<timestamp>.log`，状态写入 ignored 的 `.runtime/tushare/cron_state.json`。`skip_if_already_ok` 同时比较日期、命令 hash 和配置 hash；同一天修改调度参数后不会被旧 ok 状态误跳过。

安装或刷新 cron 使用：

```bash
/home/lzp/miniconda3/envs/stock/bin/python ops/cron/install_tushare_cron.py
crontab -l
```

不要直接用 `crontab ops/cron/tushare_update.cron` 安装；该形式会替换当前用户整份 crontab。安装脚本只替换 `# BEGIN MacroQuant TuShare update` 与 `# END MacroQuant TuShare update` 之间的托管块，保留其他项目任务。

### 3.6 限频、分页与下载前检查

- 10000 积分基础频次：常规数据 500 次/分钟，特色数据 300 次/分钟。
- 独立文本权限频次：新闻资讯 400 次/分钟，公告信息 500 次/分钟，政策法规库 500 次/分钟。
- 脚本默认用保守间隔：常规下载以 `0.18s` 或更慢为宜；分钟线和混合文本下载使用 `0.22s` 可落在 300 次/分钟内；单独 `news` 可用 `0.16s`，仍低于 400 次/分钟。
- 每日 cron 的 reference 步骤使用 `0.50s` 间隔，允许 `namechange` 全市场循环在夜间窗口内完成，同时降低对接口的持续压力。
- TuShare 当前约束重点是接口频率、权限和单次返回行数上限；日常更新没有按调用次数消耗积分的本地预算逻辑。
- 任一接口返回行数触及官方上限时，不假设全量完整，必须缩小日期窗口、按股票代码、按来源或按 offset 继续分页。
- 当前脚本会 clamp 文本接口单次上限：`anns_d=2000`、`major_news=400`、`npr=500`、`research_report=1000`、`report_rc=3000`、`news=1500`；`stk_mins` 单页上限按 `8000` 处理。
- 宏观/全球上下文使用 `0.22s` 默认间隔；`eco_cal`、`index_global`、`fx_daily`、`libor` 等按月、年份、代码或货币分区分页。
- 下载前确认 `TUSHARE_TOKEN` 只存在于环境变量或 ignored local `.env`。
- 长任务必须使用断点续跑、限频、重试和本地日志；`logs/`、`data/`、`results/`、`wandb/` 不提交 Git。

## 4. 审计与 Status

### 4.1 顶层 status 文件

`results/data_quality/` 顶层只保留当前状态文件：

| 文件 | 覆盖范围 |
|---|---|
| `base_research_status.json` | 基础维表、日频行情与约束、财务基本面 |
| `macro_context_status.json` | 国内宏观、央行货币政策、全球事件、跨市场上下文 |
| `intraday_minutes_status.json` | 历史分钟线 |
| `event_flow_status.json` | 事件/资金数据 |
| `board_trading_status.json` | 打板专题数据 |
| `text_evidence_status.json` | 文本 evidence raw tier |

跨域合并审计不作为顶层当前状态文件维护。临时排查产物可先写入 `results/data_quality/process/`；处理完成后必须移出：需要留痕的移动到根目录 `archive/`，不再需要的直接删除。`share_float` 补全下载默认不写状态文件，关键结果由 `event_flow_status.json` 统一审计。

### 4.2 Status 文件结构

6 个顶层 status 都由审计脚本直接覆盖写入，不需要手动修改。文件结构保持一致：

| 字段 | 含义 |
|---|---|
| `created_at` | 审计报告生成时间，UTC |
| `raw_dir` | 本次审计读取的数据根目录 |
| `scope` | 命令参数和数据范围，例如起止日期、数据项、指数代码、外汇代码、分钟股票池来源 |
| `status` | 由 finding 最高严重级别决定，`error > warning > ok` |
| `finding_counts` | `error`、`warning`、`info` 计数 |
| `datasets` | 按数据项聚合的状态、finding 计数和检查名 |
| `findings` | 逐条审计结果，包含 `severity`、`check`、`message`、`details` |
| `unit_rules` / `pit_rules` | 该数据域当前认可的单位和可见时间规则 |
| `doc_refs` | 对应 TuShare 官方文档链接 |
| `conclusions` | 当前可操作状态 |

脚本返回码：存在 `error` 返回非 0；只有 `warning` 时返回 0，但下游特征或实验必须显式处理 warning 指向的语义风险。

### 4.3 通用审计层

所有正式 status 都包含以下通用检查：

1. **文件系统检查**：目录是否存在、Parquet 是否可读、sidecar 是否存在、是否有 orphan sidecar、是否有空文件、schema 是否缺关键字段。
2. **预期分区检查**：根据交易日历、月份、年份、代码、货币、tag、market 或接口官方起始日期生成预期路径，检查缺失和额外分区。
3. **业务键检查**：按数据项定义重复键、空代码、空日期、分区日期与行内日期不一致等问题。
4. **分页触顶检查**：文件行数命中常见上限或接口 page limit 时，标记 source cap risk；不能假设已经完整。
5. **sidecar 覆盖检查**：范围型分区必须证明请求参数覆盖当前审计窗口。
6. **单位与 PIT 规则输出**：把当前认可的单位、可见时间和保守假设写入报告，供 Environment/Agent 使用。

### 4.4 基础研究数据审计

入口：

```bash
~/miniconda3/bin/conda run -n stock python scripts/tushare/audit.py base --include-limit-list --end-date <YYYYMMDD> --fundamental-end-date <YYYYMMDD>
```

默认输出：`results/data_quality/base_research_status.json`。

具体逻辑：

1. 解析 `raw_dir` 和日期范围；如果未传 `--end-date`，使用本地 SSE 交易日历最后一个开市日。
2. 选择基础维表、日频行情与财务基本面数据项。
3. 执行通用文件系统检查。
4. 基础维表专项：
   - `stock_basic` 检查 `L/D/P` 文件、必需字段空值、状态分布和股票代码唯一性。
   - `stock_company` 检查公司信息覆盖，但不要求覆盖等于股票池。
   - `trade_cal` 提取 SSE 开市日，作为日频、分钟和 WFO 日期基准。
   - `bak_basic` 检查交易日覆盖和首个非空日；它只作为补充快照，不替代主行情。
   - `namechange` 检查曾用名/ST 变更日期字段和重复键。
   - `index_classify`、`index_member_all` 检查申万行业层级和成分覆盖。
5. 日频专项：
   - 以 SSE 开市日生成预期 `trade_date` 分区。
   - 检查 `daily`、`daily_basic`、`adj_factor`、`stk_limit`、`suspend_d`、`limit_list_d` 缺失分区、sidecar、schema、空分区和重复业务键。
   - 做跨表股票覆盖差异，区分源端口径差异和疑似缺失。
   - 固化单位口径：`daily.vol=手`、`daily.amount=千元`、`daily_basic` 股本为万股/市值为万元。
6. PIT 和股票池语义：
   - 检查上市、退市、暂停上市股票与行情表覆盖关系。
   - 检查 `ann_date`、`f_ann_date`、披露日等可见时间字段是否可用于 PIT 选择。
7. 财务专项：
   - 按 period、ann_month 或 ts_code 策略生成预期文件。
   - 检查缺失文件、空分区、sidecar、必需字段、重复业务键和单次请求触顶风险。
   - 固化财报金额、预测金额、`f_ann_date`/`ann_date` 优先级、多版本保留等规则。

warning 通常代表源端口径差异、多版本原始记录或可接受稀疏性；error 代表结构不可用或预期文件缺失。

### 4.5 宏观与全球上下文审计

入口：

```bash
~/miniconda3/bin/conda run -n stock python scripts/tushare/audit.py macro --start-date <YYYYMMDD> --end-date <YYYYMMDD>
```

默认输出：`results/data_quality/macro_context_status.json`。

具体逻辑：

1. 选择宏观、政策和全球上下文数据项。
2. 执行通用文件系统检查。
3. `expected_macro_paths` 按接口策略生成预期分区：
   - 月度表按 `YYYYMM`。
   - 季度表按 `YYYYQn` 或起止季度。
   - 年份表按年份窗口。
   - 全球指数、外汇、LIBOR 按代码/货币加年份。
   - `eco_cal` 按月份和可选国家、货币、事件过滤。
4. `audit_macro_dataset` 检查每个数据项的缺失分区、空分区、sidecar、字段、重复键和触顶风险。
5. `audit_macro_keys` 对事件类和时间序列表做重复业务键统计；`eco_cal` 允许同日多事件，但不能把异构事件值直接当作统一数值因子。
6. 报告写入 `macro_unit_rules` 和 `macro_pit_rules`：月度/季度宏观在 raw 层使用保守可见时间，进入特征层前优先用 `cn_schedule.publish_date` 或更精确发布时间修正。

### 4.6 历史分钟线审计

顶层 status 只审计最终按日分钟层：

```bash
~/miniconda3/bin/conda run -n stock python scripts/tushare/audit.py intraday-by-date --start-date <YYYYMMDD> --end-date <YYYYMMDD>
```

默认输出：`results/data_quality/intraday_minutes_status.json`。

具体逻辑：

1. 使用本地 SSE 开市日历生成预期交易日。
2. 对 `data/raw/stk_mins_1min_by_date/trade_date=<YYYYMMDD>.parquet` 建立文件清单。
3. 库存检查：预期交易日文件、sidecar、必需字段、零行文件、总行数。最终按日分钟文件出现 0 行是 error。
4. 深度检查：
   - 默认抽样，`--full-scan` 检查全部日期。
   - `validate_stk_mins_by_date_frame` 检查 `trade_date` 与分区一致、`trade_time` 可解析、`available_at` 可解析、`(ts_code, trade_time)` 无重复、行数不低于阈值。
   - `--expected-codes-source minute` 是日常更新和正式 status 的默认覆盖口径：已有按日文件以本地分钟覆盖为准，新文件回退到 `daily` 股票池。
   - `--expected-codes-source daily` 是严格专项排查，可将当日分钟股票覆盖与日频股票池对比；覆盖差异不直接改变顶层 6 域结论。
5. 单位规则写入报告：`vol=股`、`amount=元`、`available_at=trade_time`。

`scripts/tushare/audit.py intraday` 审计按股票+年份保存的源层，用于下载追溯或源层排查；当前研究、PIT 和增量更新以按日层为准。

竞价口径专项校验使用：

```bash
~/miniconda3/bin/conda run -n stock python scripts/tushare/audit.py auction-alignment --start-date <YYYYMMDD> --end-date <YYYYMMDD>
```

该报告对比本地 09:30 分钟条、TuShare `stk_auction` 和日线全天单位，只作为过程审计，不写入顶层 status。

### 4.7 事件/资金数据审计

入口：

```bash
~/miniconda3/bin/conda run -n stock python scripts/tushare/audit.py event-flow --start-date <YYYYMMDD> --end-date <YYYYMMDD>
```

默认输出：`results/data_quality/event_flow_status.json`。

具体逻辑：

1. 选择两融汇总、两融明细、融资融券标的、个股资金流、股东人数、股东增减持、回购、解禁、大宗交易。
2. 如果 `share_float_complete/share_float_complete.parquet` 存在，则文件系统审计不要求保留 `share_float` 原始过程目录；解禁以 complete union 为保留边界。
3. 执行通用文件系统检查。
4. `expected_event_paths` 按数据项策略生成预期路径：日频资金表按交易日，稀疏公告/事件表按月份，解禁最终 union 只检查保留文件。
5. `audit_event_dataset` 检查缺失分区、空分区、sidecar、字段、重复业务键、空日期和触顶风险。
6. `audit_share_float_complete_union` 检查 union 文件是否存在、是否可读、是否有关键字段、是否覆盖主路径和 candidate 补充路径、是否存在 exact-6000 风险、union 去重后的业务键和源路径统计。
7. 报告写入 `event_unit_rules` 与 `event_pit_rules`，明确资金流、两融、融资融券标的、大宗、公告事件的可见时间和原始单位。

空月份或空日期不一定是错误；只有缺失预期文件、结构不可读、关键字段缺失才阻断下游。

### 4.8 打板专题数据审计

入口：

```bash
~/miniconda3/bin/conda run -n stock python scripts/tushare/audit.py board-trading --start-date <YYYYMMDD> --end-date <YYYYMMDD>
```

默认输出：`results/data_quality/board_trading_status.json`。

具体逻辑：

1. 选择 `kpl_list`、`limit_step`、`limit_cpt_list`、`limit_list_ths`、`top_list`、`top_inst`、`hm_list`、`hm_detail`、`ths_hot`、`dc_hot`。
2. `expected_board_paths` 按接口策略生成预期路径：
   - 普通交易日表按 `trade_date=<YYYYMMDD>`。
   - `kpl_list` 按 `tag=<TAG>/trade_date=<YYYYMMDD>`。
   - `limit_list_ths` 按 `limit_type=<TYPE>/trade_date=<YYYYMMDD>`，从 `20231101` 起生成预期路径。
   - `ths_hot` 按 `market=<MARKET>/is_new=<Y|N>/trade_date=<YYYYMMDD>`。
   - `dc_hot` 按 `market=<MARKET>/hot_type=<TYPE>/is_new=<Y|N>/trade_date=<YYYYMMDD>`。
   - `hm_list` 是静态参考表，路径为 `hm_list/hm_list.parquet`。
3. 执行通用文件系统检查。
4. `audit_board_dataset` 检查缺失分区、空分区、sidecar、字段、分页触顶和重复业务键。
5. `audit_board_keys` 检查 `available_at` 是否存在并可解析；静态 `hm_list` 不强制要求历史 PIT 时间。
6. 报告写入 `board_unit_rules` 和 `board_pit_rules`：
   - `kpl_list` 以次日 08:30 可见。
   - `limit_list_ths` 以当日 16:00 左右可见。
   - `top_list/top_inst` 以当日 20:00 可见。
   - `limit_step/limit_cpt_list/hm_detail` 保守按当日日终可见。
   - `ths_hot/dc_hot` 优先用 `rank_time`，`is_new=Y` 没有精确时间时按 22:30 可见。

warning 通常代表源端重复键、分页触顶或某些历史阶段接口稀疏；进入 PIT 特征和 Agent evidence 前必须按 `available_at` 过滤，并与 `limit_list_d`、分钟线推导涨停标签做冲突样本检查。

### 4.9 文本 Evidence 审计

入口：

```bash
~/miniconda3/bin/conda run -n stock python scripts/tushare/audit.py base --include-text --text-start-date <YYYYMMDD> --text-end-date <YYYYMMDD>
```

默认输出：`results/data_quality/text_evidence_status.json`。

具体逻辑：

1. `audit_unified` 在基础研究审计基线之外追加文本 evidence 数据项，并由默认输出路由到文本 status；因此文本 status 中可能包含基础维表或日频/财务依赖检查。
2. `selected_integrated_text_datasets` 选择公告、长新闻、新闻联播、政策法规、券商研报、盈利预测、新闻快讯。
3. `expected_text_paths` 按接口策略生成预期分区：公告、长新闻、政策法规、研报、盈利预测按月份；新闻联播按日期；新闻快讯按官方 `src` 加日期。
4. `audit_text_dataset` 检查文件、sidecar、schema、空分区、重复业务键、分页触顶和时间字段。
5. `audit_text_keys` 对每类文本建立保守业务键：公告/新闻用标题、来源、发布时间和股票代码组合；研报/预测用报告或机构相关字段组合。
6. 文本只到 raw evidence 边界。进入 LLM 前必须再次生成 `evidence_id`、`document_hash`、`available_at`、来源质量、正文截断结果和公司/行业实体映射。

文本重复业务键通常保留为 warning，因为上游可能重复推送或多来源转载；LLM evidence 层必须按 hash 和可见时间去重。

## 5. Raw PIT 数据合同

Data 层只定义 raw 数据能否支持 PIT，不负责生成 feature、observation 或 evidence pack。具体 PIT feature/observation 构造、selector、泄漏检查和回放时点可见性由 Environment 负责，见 `docs/environment_design.md`；LLM evidence pack 的输入边界见 `docs/agent_design.md`。

### 5.1 原始层元数据

所有 raw 文件必须带 `.meta.json` sidecar，至少记录接口名、请求参数、抓取时间和源数据 hash。数据行本身尽量保留 TuShare 原始字段；`available_at` 只在能够保守推断时写入或在特征层派生。多版本财报、重复公告、稀疏事件和源端重复推送不在 raw 层强行删除。

### 5.2 Raw 可见性原则

- raw 层不得把未来事件生效日伪装成当前可见信息，例如解禁 `float_date`、分红 `ex_date`、业绩报告期 `period` 都不能替代公告可见时间。
- 只含日期、不含时间的数据默认不能用于同日开盘决策；日频行情和日频指标默认下一交易日可交易。
- 财务、公告、研报、宏观发布等异步数据必须保留公告日、实际发布时间或可保守推断发布时间，使 Environment 能构造 `available_at <= decision_time` 的选择器。
- 同一业务键多版本数据在 raw 层全部保留；Environment 或 Agent evidence 层按决策时点选择当时最新可见版本。
- raw 审计只判断字段、单位、分区、sidecar、触顶风险和可见时间字段是否足以支撑 PIT；不声明某个特征在回测中无泄漏。

### 5.3 可见性速查

| 数据 | 可见性规则 |
|---|---|
| `daily` / `daily_basic` | 当日收盘后或下一交易日；09:25 信号不得使用当日数据 |
| 分钟线 | `available_at=trade_time`，回测中视为该分钟 bar close 后可见 |
| 财务 | 优先用 `f_ann_date`，没有时保守使用 `ann_date`；多版本按决策时点选择当时可见版本 |
| 宏观 | 只有月度或季度字段时，raw 层按保守规则写 `available_at`；特征层优先用 `cn_schedule.publish_date` 修正 |
| 全球事件 | `eco_cal` 有可解析 `time` 时使用 `date+time`，否则按当天收盘后可见 |
| 央行货币政策执行报告 | `monetary_policy.pub_date` 作为保守可见日期 |
| 文本 | 优先用 `rec_time`、`pub_time`、`pubtime`、`datetime`、`create_time` 构造 `available_at`；只有日期时按收盘后或次日可见 |
| 事件/资金 | `margin`/`margin_detail` 按下一日 09:00 可见，`margin_secs` 按当日 09:00 可见，`moneyflow` 按当日 19:00，`block_trade` 按当日 21:00；公告类事件按 `ann_date` 保守可见 |

### 5.4 交给 Environment 的最小合同

每个进入特征或回放的数据域至少要能提供：

- 数据来源：TuShare 接口名、请求参数、分区路径和 sidecar。
- 业务键：例如 `(trade_date, ts_code)`、`(ts_code, period, report_type, comp_type)`、公告标题/发布时间/source 组合。
- 时间键：原始交易日、公告日、发布时间、生效日和保守 `available_at` 候选。
- 单位规则：价格、成交量、成交额、股本、市值、财报金额、宏观数值和事件数量口径。
- 触顶和稀疏风险：分页上限、exact-limit、空分区、源端缺失或重复推送标记。

### 5.5 跨域 PIT 要求

- 财务：raw 层保留 `f_ann_date`、`ann_date`、`period`、`report_type`、`comp_type` 和多版本记录；Environment 构造 `data/features/fundamental_events/<dataset>/available_month=<YYYYMM>.parquet` 后，再选择 `available_at <= decision_time` 的最新可见版本。
- 分红、解禁、回购、股东事件：raw 层同时保留公告日期和事件生效日期；Environment 做 PIT 时只能用公告可见性决定是否暴露未来事件属性，缺少公告可见日的分红记录不进入 `fundamental_events`。
- 资金流、两融、大宗：raw 层记录交易日和保守可见时间；日频策略默认只能影响下一交易日及以后。
- 宏观：raw 层保留原始月份、季度、发布日期、发布日程和保守可见时间；Environment 后续可用 `cn_schedule.publish_date` 或更精确发布时间替换保守规则。
- 文本：raw 层保留来源、URL/标题、发布时间、正文或 HTML hash；Agent evidence 层再生成 `evidence_id`、`document_hash`、截断正文和实体映射。

## 6. 全文数据风险与口径修正总结

本章汇总全文涉及、且已经被交叉检验确认会影响特征、回放解释、更新策略或审计结论的数据口径、不完备、覆盖、源端回写和可见性问题。这些规则属于数据合同的一部分：Raw 层保留原始值，下载阶段只做防污染和补全标记，Environment 层在构造 PIT 特征时显式修正或过滤，不能静默改写历史原始值。

| 风险项 | 影响范围 | 当前处理 | 复核方式 |
|---|---|---|---|
| 深圳 09:30 分钟条与开盘集合竞价接口口径不一致 | 历史 `stk_mins_1min_by_date` 中 09:30 分钟条被用作 `stk_auction` 替代特征时 | Environment 生成 `vol_pit/amount_pit`：`00*.SZ` 乘 `0.76`，`30*.SZ` 乘 `0.58`；沪市、北交所和 15:00 收盘竞价保持 `1.0`；Raw 文件不改写 | 定期运行 `scripts/tushare/audit.py auction-alignment`，对比 09:30 分钟条、TuShare `stk_auction` 和日线单位 |
| 日线、分钟线成交量/成交额单位不同 | `daily` 与 `stk_mins_1min_by_date` 横向校验或特征拼接 | `daily.vol=手`、`daily.amount=千元`；分钟线 `vol=股`、`amount=元`；特征层统一单位后再比较 | 分钟审计中的 full-day minute sums vs daily units 检查 |
| `share_float_complete` 不等于普通 `event_flow` 下载项，且可能存在源端截断 | 解禁补全和 `share_float_complete` union | 使用专用 `download-share-float-complete` 入口生成 union；对触顶 `ann_date` 分区做 candidate 级 `ann_date+ts_code` 救援；仍触顶时标记 `source_cap_risk`，不宣称数学意义上完全无截断 | `event_flow_status.json` 中的 `share_float_complete` union 检查、exact-6000/source-cap findings、union shrink 防护 |
| 历史分钟线与日线股票池覆盖不完全一致 | 早期 NEEQ/BSE 迁移代码、停牌/退市等导致 `daily` 有记录但历史分钟线不可得 | 正式分钟审计和日常更新默认用 `--expected-codes-source minute`，避免把源端不可得代码当作每日错误；严格 `daily` 股票池覆盖检查只作为专项排查 | `scripts/tushare/audit.py intraday-by-date --expected-codes-source daily` 专项检查；正式 status 使用 `minute` 覆盖口径 |
| 日频表之间覆盖口径不同 | `daily`、`daily_basic`、`adj_factor`、`stk_limit`、`suspend_d`、`limit_list_d` 横向 join | Raw 层分别保留；特征层必须显式选择内连接、左连接或缺失填充策略，不默认认为同日同股票全集一致 | `base_research_status.json` 的跨表覆盖差异 findings |
| TuShare 近期分区可能发生源端回写或迟到修正 | 近期日频主表、`bak_basic`、财务开放报告期/公告月、宏观/全球开放窗口、事件/资金、打板专题、文本 evidence 和解禁 union | cron 对滚动窗口强制刷新并写 revision ledger；旧非空、新空默认不覆盖；`share_float_complete` union 缩水默认阻断覆盖 | `results/data_quality/revision_events.jsonl`、`revision_summary.json`、04:00 `revision-sentinel` |
| 历史交易日分区也可能与 TuShare 当前返回不一致 | 尤其是 `limit_list_d.limit_amount`，专项抽样中多次出现本地旧值有数字而当前源端为空；少量 `suspend_d` 也可能新增/移除业务键 | `revision-history-sample` 按年分层抽样统计接口稳定性、字段变化和 numeric-to-blank/blank-to-numeric 转换；`limit_amount` 在 Raw 层保留用于审计，但 Environment 特征层显式剔除，不进入 `daily_alpha` | `results/data_quality/process/revision_history_sample_status.json`、`revision_history_focus_limit_suspend_status.json` |
| 结构性重复业务键可能放大下游 join | `block_trade`、`top_list` 等事件/榜单接口在抽样中出现 old/new 同步存在的重复业务键 | Raw 层保留原始重复行，审计标 warning；进入 PIT 特征或 evidence 前必须用更完整事件键、exact duplicate 去重或按 `trade_date+ts_code` 聚合为计数/金额/标签，不能直接多行 join 到日频股票样本 | `revision-history-sample` 的 duplicate-key findings；`event_flow_status.json`、`board_trading_status.json` 的 duplicate-key checks |
| 财务基本面存在多版本、重复业务键和 PIT 可见性风险 | 报表、指标、预告、快报、分红、审计意见、主营业务构成、披露计划 | Raw 层保留多版本；Environment 构造 `fundamental_events` 后按 `available_at <= decision_time` 选择最新可见版本；分红只能用 `imp_ann_date/ann_date` 暴露未来事件属性 | `base_research_status.json` 财务 findings；`audit-fundamental-events` |
| 宏观/全球数据只有日期或月份时可见时间不精确 | 月度/季度宏观、全球事件、利率、政策文本 | Raw 层使用保守 `available_at`；特征层优先用 `cn_schedule.publish_date` 或更精确发布时间修正；不得用于同日开盘即时判断 | `macro_context_status.json` 的 PIT rules 和重复/触顶 findings |
| 文本 evidence 可能重复推送、转载或时间字段质量不一 | 公告、新闻、政策法规、研报、盈利预测、新闻快讯 | Raw 层保留来源和正文/hash；Agent evidence 层生成 `evidence_id/document_hash`、按 `available_at` 过滤并去重；只有日期的文本保守设为收盘后或次日可见 | `text_evidence_status.json` 的重复键、分页触顶和时间字段 findings |
| 打板标签存在来源口径差异、盘中前视风险和字段稳定性风险 | `limit_list_d`、`limit_list_ths`、`kpl_list`、分钟线推导涨停标签 | 保留来源字段；`limit_list_d.first_time/open_times/fd_amount` 等日终字段不得用于盘中决策；`limit_amount` 因源端历史回写不稳定不进入特征；真实盘中打板需用已走完分钟 bar 或 QMT/Level-2 数据 | `board_trading_status.json`、分钟线推导标签与榜单冲突样本检查、`revision-history-sample` |

## 7. 官方文档索引

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
- CPI/PPI/PMI/货币供应/社融：https://tushare.pro/document/2?doc_id=228 / https://tushare.pro/document/2?doc_id=229 / https://tushare.pro/document/2?doc_id=325 / https://tushare.pro/document/2?doc_id=242 / https://tushare.pro/document/2?doc_id=310
- 利率与全球事件：https://tushare.pro/document/2?doc_id=202 / https://tushare.pro/document/2?doc_id=204 / https://tushare.pro/document/2?doc_id=205 / https://tushare.pro/document/2?doc_id=206 / https://tushare.pro/document/2?doc_id=233
- 全球指数/外汇/美国利率：https://tushare.pro/document/2?doc_id=211 / https://tushare.pro/document/2?doc_id=179 / https://tushare.pro/document/2?doc_id=218
- 央行货币政策执行报告：https://tushare.pro/document/2?doc_id=465
