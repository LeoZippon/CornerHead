# Data Documentation

整理日期：2026-05-30

本文档只记录 MacroQuant 当前需要保留的 TuShare 下载边界、顺序、限频、raw PIT 数据合同、单位约束和审计入口。历史阶段性审计细节以 `LOGBOOK.md` 和 `docs/logbook/DETAILED_LOGBOOK.md` 为准。

不要把 TuShare token 写入已跟踪仓库文件、命令日志或运行日志。下载脚本只应从环境变量或 ignored local `.env` 读取：

```bash
export TUSHARE_TOKEN="..."
```

## 数据下载

TuShare 实现位于 `src/hl_trader/data_sources/tushare/`。其中 `download.py` 负责下载、更新、分钟线整理和 `share_float_complete` union，`audit.py` 负责当前 raw 数据审计，`common.py` 负责共享常量、接口合同、TuShare client、路径/日期/sidecar/PIT helper，`cron_update.py` 负责定时更新 runner。

命令入口位于 `scripts/tushare/`，用于手工命令和 cron 调度；数据源业务逻辑统一由 `src/hl_trader/data_sources/tushare/` 提供。

### 初始下载与整理

第一次建库或大窗口重建时，先按数据依赖顺序下载和整理，再启用日常更新：

1. 基础研究数据：先 `reference`，再 `daily`，最后 `fundamental`。
2. 宏观与全球上下文：`macro` 与 `global` 可在基础研究数据完成后补充。
3. 历史分钟线：先批量下载 `intraday` 源层，再整理为按日最终层。
4. 事件/资金数据：下载 `event_flow`，并用 `download-share-float-complete` 生成解禁最终 union。
5. 打板专题数据：下载 `board_trading`，覆盖开盘啦榜单、同花顺涨跌停榜单、连板天梯、最强板块、龙虎榜、游资和热榜。
6. 文本 evidence：下载公告、新闻、政策、研报、盈利预测文本源；只作为 evidence raw tier，不进入默认日频特征链路。

正式下载、整理入口如下：

```bash
~/miniconda3/bin/conda run -n stock python scripts/tushare/download.py download --tier reference
~/miniconda3/bin/conda run -n stock python scripts/tushare/download.py download --tier daily --include-limit-list
~/miniconda3/bin/conda run -n stock python scripts/tushare/download.py download --tier fundamental --start-date 20100101 --end-date <YYYYMMDD>
~/miniconda3/bin/conda run -n stock python scripts/tushare/download.py download --tier macro --start-date 20200101 --end-date <YYYYMMDD>
~/miniconda3/bin/conda run -n stock python scripts/tushare/download.py download --tier global --start-date 20200101 --end-date <YYYYMMDD>
~/miniconda3/bin/conda run -n stock python scripts/tushare/download.py download --tier intraday --datasets stk_mins --start-date 20200101 --end-date 20260525
~/miniconda3/bin/conda run -n stock python scripts/tushare/download.py compact-intraday-by-date --start-date 20200101 --end-date 20260525
~/miniconda3/bin/conda run -n stock python scripts/tushare/download.py download --tier event_flow --start-date 20200101 --end-date <YYYYMMDD>
~/miniconda3/bin/conda run -n stock python scripts/tushare/download.py download-share-float-complete --ann-start-date 20100101 --ann-end-date 20260525 --float-start-date 20200101 --float-end-date 20260525 --rescue-ann-limit-hits --write-union
~/miniconda3/bin/conda run -n stock python scripts/tushare/download.py download --tier board_trading --start-date 20200101 --end-date <YYYYMMDD>
~/miniconda3/bin/conda run -n stock python scripts/tushare/download.py download --tier text_evidence --start-date 20200101 --end-date <YYYYMMDD>
```

### 日常增量更新

日常增量更新入口：

```bash
~/miniconda3/bin/conda run -n stock python scripts/tushare/download.py update --start-date 20200101 --end-date <YYYYMMDD>
```

按日研究或实盘决策固定运行一个 `update` 命令。`--start-date` 是当前研究窗口的数据下界，`--end-date` 默认当天；脚本会按基础维表、日频行情、财务、宏观、全球、事件/资金、打板专题、按日分钟线、解禁 union 和文本 evidence 的顺序补齐全维度数据。默认语义是 skip-existing：已经存在且 sidecar 覆盖当前请求范围的分区直接跳过，缺失分区自动补充；当前月、当前年这类文件名已存在但 sidecar 覆盖不到新 `end_date` 的聚合分区会重新拉取该分区，避免月度/年度文件导致隐性断层。sidecar 覆盖比较会把 `YYYYMMDD`、`YYYYMMDDHHMMSS` 和 `YYYY-MM-DD HH:MM:SS` 归一到同一时间边界，日期型 `end_date` 按当日结束处理，避免午夜时间戳误判覆盖整天。只有显式 `--force` 才强制重拉已有分区。临时跳过重型数据可加 `--no-include-intraday`、`--no-include-share-float-complete` 或 `--no-include-board-trading`；跳过 `bak_basic` 可加 `--skip-bak-basic`。`share_float_complete` 每次随 update 重建完整历史 union，重建结果若少于既有 union 行数会 fail fast，只有人工确认后才用 `--allow-union-shrink` 覆盖。当日源端尚未发布时，必需的日频和交易日事件接口若返回 0 行，更新脚本只打印 `skipped_write`，不写半成品分区；按日分钟线若预期股票池非空也拒绝写 0 行文件。

### 定时更新

TuShare 接口更新时间目录维护在 `configs/tushare_update_schedule.json`。该文件逐项记录当前脚本使用的全部接口、数据域、官方更新时间或更新频率、cron 覆盖策略和官方文档链接；官方文档没有写具体时刻的接口统一标为“官方未标具体入库时刻”，但仍纳入保守日更。当前不把更新时间散落写入代码，`scripts/tushare/cron_update.py` 只读取该配置并调用统一 `update` 入口。

当前 cron 使用北京时间：

```cron
35 23 * * * cd /Data/lzp/MacroQuant && mkdir -p logs && /home/lzp/miniconda3/envs/stock/bin/python scripts/tushare/cron_update.py --job cn_evening_full >> logs/tushare_cron_dispatch.log 2>&1
5 9 * * * cd /Data/lzp/MacroQuant && mkdir -p logs && /home/lzp/miniconda3/envs/stock/bin/python scripts/tushare/cron_update.py --job cn_preopen_margin_backfill_0905 >> logs/tushare_cron_dispatch.log 2>&1
15 9 * * * cd /Data/lzp/MacroQuant && mkdir -p logs && /home/lzp/miniconda3/envs/stock/bin/python scripts/tushare/cron_update.py --job cn_preopen_margin_retry_0915 >> logs/tushare_cron_dispatch.log 2>&1
```

`cn_evening_full` 更新当天，覆盖 A 股日线 15:00-16:00、每日指标 15:00-17:00、分钟线 17:00-21:00、资金流 19:00、大宗交易 21:00、打板专题榜单/龙虎榜/热榜、研报/盈利预测晚间、宏观/全球和文本 evidence 的常规落库窗口。`cn_preopen_margin_backfill_0905` 和 `cn_preopen_margin_retry_0915` 更新前一自然日，只回补 TuShare 标注每日 09:00 更新的 `margin` 和 `margin_detail`，避免盘前再跑分钟线、`share_float_complete` 或全文本这类重任务，给 09:25 前的快速审计、特征冻结和 Agent 决策留出时间。09:05 和 09:15 使用不同 job 名：如果 09:05 因源端未准备好而没有写入数据但进程返回成功，09:15 仍会再次尝试；如果 09:05 已写入完整分区，09:15 会被下载器的 skip-existing/sidecar 逻辑快速跳过。runner 使用全局 `.runtime/tushare/locks/tushare_update.lock`，因此 cron job 不会并发写同一套 raw 数据和状态文件。每次运行都会把资源检查、完整命令和返回码写入 `logs/tushare_cron_<job>_<end_date>_<timestamp>.log`，状态写入 ignored 的 `.runtime/tushare/cron_state.json`。

安装或刷新 cron 使用：

```bash
/home/lzp/miniconda3/envs/stock/bin/python ops/cron/install_tushare_cron.py
crontab -l
```

不要直接用 `crontab ops/cron/tushare_update.cron` 安装；该形式会替换当前用户整份 crontab。安装脚本会只替换 `# BEGIN MacroQuant TuShare update` 与 `# END MacroQuant TuShare update` 之间的托管块，保留其他项目任务。

### 限频和分页

- 10000 积分基础频次：常规数据 500 次/分钟，特色数据 300 次/分钟。
- 独立文本权限频次：新闻资讯 400 次/分钟，公告信息 500 次/分钟，政策法规库 500 次/分钟。
- 脚本默认用保守间隔：日频下载曾因 `stk_limit` 触发 400 次/分钟限制，当前常规下载以 `0.18s` 或更慢为宜；分钟线和混合文本下载使用 `0.22s` 可落在 300 次/分钟内；单独 `news` 可用 `0.16s`，仍低于 400 次/分钟。
- 任一接口返回行数触及官方上限时，不假设全量完整，必须缩小日期窗口、按股票代码、按来源或按 offset 继续分页。
- 当前脚本会 clamp 文本接口单次上限：`anns_d=2000`、`major_news=400`、`npr=500`、`research_report=1000`、`report_rc=3000`、`news=1500`；`stk_mins` 单页上限按 `8000` 处理。
- 宏观/全球上下文使用 `0.22s` 默认间隔；`eco_cal`、`index_global`、`fx_daily`、`libor` 等按月、年份、代码或货币分区分页，避免单次窗口过大。
- 已定向重拉并分页验证：`daily/trade_date=20221118`、`adj_factor/trade_date=20220808`、`stk_limit/trade_date=20201027`、`stk_limit/trade_date=20220705`、历史 exact-7000 的 `balancesheet_vip` period 分区、`moneyflow/trade_date=20230704`、`stk_holdernumber/month=202511`。当前仍需保守标记的是 `share_float` 源端 6000 行 cap 风险、分钟线零行 stock-year、宏观/全球日历源覆盖稀疏或重复等语义风险。

### Raw 可见性速查

本节只保留 raw 数据进入 Environment 或 evidence 前的可见性速查。PIT feature/observation 构造、selector 和泄漏检查见 `docs/environment_design.md`。

- 行情：`daily`、`daily_basic` 只能用于当日收盘后或下一交易日决策；09:25 信号不得使用当日 `daily`/`daily_basic`。
- 分钟：`available_at=trade_time`，回测中视为该分钟 bar close 后可见。
- 财务：优先用 `f_ann_date`，没有时保守使用 `ann_date`；同一股票同一报告期多版本必须按决策时点选择当时可见版本。
- 宏观：只有月度或季度字段时，raw 层先按保守规则写 `available_at`（月末+31天、季末+45天）；后续特征层应优先用 `cn_schedule.publish_date` 或更精确发布时间修正。
- 全球事件：`eco_cal` 有可解析 `time` 时使用 `date+time`，否则按当天收盘后可见处理。
- 央行货币政策执行报告：`monetary_policy.pub_date` 作为保守可见日期；`content_html`/PDF 进入 text evidence 前必须做 hash、截断和来源记录。
- 文本：优先用 `rec_time`、`pub_time`、`pubtime`、`datetime`、`create_time` 构造 `available_at`；只有日期时按收盘后或次日可见处理。
- 事件/资金：`margin`/`margin_detail` 按下一日 09:00 可见，`moneyflow` 按当日 19:00，`block_trade` 按当日 21:00；公告类事件按 `ann_date` 保守可见。

### 单位口径

- `daily.vol` 是手，`daily.amount` 是千元。
- `stk_mins.vol` 是股，`stk_mins.amount` 是元。
- `daily_basic.total_share/float_share/free_share` 是万股，`daily_basic.total_mv/circ_mv` 是万元。
- `bak_basic` 不含 `vol` / `amount`，不能用于成交量或成交额口径对齐；其股本/资产字段是亿口径粗快照。
- `bak_daily.vol` 可与 `daily.vol` 对比；`bak_daily.amount` 是万元，和 `daily.amount` 千元比较时需乘以 10。
- 财报主表金额字段按元处理；`forecast_vip` 利润预测字段是万元；`fina_indicator_vip` 是混合表，必须按字段族处理。
- 宏观金额字段保持 TuShare 官方原始单位：`cn_gdp`、`cn_m`、`sf_month` 主要是亿元口径，PMI 是扩散指数，利率字段通常是百分比，`eco_cal` 数值按事件异构处理。
- 事件/资金保留 TuShare 原始单位：`moneyflow` 量为手、金额为万元；`margin` 两融金额为元；`block_trade.vol` 为万股；其余字段进入特征层前按接口单独归一。

### 基础维表

| 数据 | 接口 | 范围/拉取方式 | 当前边界 |
|---|---|---|---|
| 股票列表 | `stock_basic` | `list_status=L/D/P` | 股票池基表，不能用 `stock_company` 替代 |
| 上市公司信息 | `stock_company` | `exchange=SSE/SZSE/BSE` | 公司属性补充；覆盖不等于全股票池 |
| 历史每日股票列表 | `bak_basic` | 按交易日循环，2016 起 | 补充每日行业、估值、股本快照；首个非空日为 `20160809` |
| 交易日历 | `trade_cal` | `SSE/SZSE/BSE`，2010 至今 | WFO、调仓和交易日判断；BSE 本地返回为空，先以 SSE/SZSE 为主 |
| 曾用名/ST 历史 | `namechange` | 全量或按股票代码 | 使用 `ann_date`/保守 `available_at`，不要用未来 `start_date` 泄漏 |
| 行业分类 | `index_classify` | `src=SW2021` | 申万行业层级 |
| 行业成分 | `index_member_all` | 按一级行业循环 | 历史行业暴露 |

基础维表当前已落到扁平化 `data/raw/*`。源端覆盖口径差异在审计章节处理；当前没有 broad redownload 需求。

### 日频行情与交易约束

| 数据 | 接口 | 范围/拉取方式 | 用途 |
|---|---|---|---|
| 日线行情 | `daily` | 按 `trade_date`，`20100104-20260528` 已完成 | OHLCV、成交额 |
| 复权因子 | `adj_factor` | 按 `trade_date` | 复权收益 |
| 每日指标 | `daily_basic` | 按 `trade_date` | PE/PB/PS、股息率、市值、换手率、股本 |
| 涨跌停价格 | `stk_limit` | 按 `trade_date` | 涨跌停执行约束 |
| 停复牌 | `suspend_d` | 按 `trade_date` 或日期区间 | 停牌/复牌 |
| 涨跌停/炸板列表 | `limit_list_d` | 默认保留，`20200102-20260529` 已有分区 | 打板标签、炸板/回封事件和次日事件特征 |

日频行情结构完整。已知语义边界是 `daily`、`daily_basic`、`stk_limit` 覆盖口径不同，特征层必须显式处理缺失或使用内连接。

### 打板策略数据准备

当前 raw 边界已经保留打板研究的基础数据，可以先支撑“日终标签 + 分钟回放”的策略验证：

| 需求 | 当前数据 | 用法边界 |
|---|---|---|
| 涨停/跌停价格 | `stk_limit` | 盘前交易约束和涨跌停价判断；TuShare 标注交易日早间更新，但历史 PIT 中仍要按可见时点使用 |
| 日终打板标签 | `limit_list_d` | 识别涨停、跌停、炸板、回封次数、首次/最后封板时间、封单额等；适合作为标签、次日特征或审计，不得在盘中提前使用日终汇总字段 |
| 分钟级触板/开板回放 | `stk_mins_1min_by_date` + `stk_limit` | 用分钟 OHLC 与涨停价推导首次触板、封板后开板、尾盘状态等；分钟粒度无法还原逐笔排队和盘口撤单 |
| 流动性和可交易过滤 | `daily`、`daily_basic`、`moneyflow`、`suspend_d`、`namechange` | 过滤停牌、ST/曾用名、成交额、市值、换手、资金流等基础约束 |
| 开/收盘竞价近似 | `stk_mins_1min_by_date` 的 `09:30`/`15:00` 分钟条 | 作为历史竞价近似；暂不全量下载 `stk_auction`/`stk_auction_c` |

TuShare 官方数据索引还单列了“打板专题数据”。这些接口不替代现有日线、分钟线和涨跌停约束，而是补充更贴近题材、情绪、连板、龙虎榜和热榜的 raw evidence/event 层。当前已作为独立 `board_trading` 数据域接入下载、日常更新和审计。

当前数据发现和保留边界：

- `board_trading` 从 `20200101` 作为全域下载下界；每个接口仍按自己的官方历史边界生成预期分区。`limit_list_ths` 官方历史从 `20231101` 开始，`hm_detail` 从 `20220801` 开始，早于接口边界的日期不视为缺失。
- `limit_list_ths` 和 `limit_list_d` 都可作为打板标签/情绪 evidence，但口径不同：`limit_list_d` 是每日涨跌停/炸板统计，`limit_list_ths` 是同花顺榜单池子。二者进入特征层前必须按 `available_at` 过滤，并保留来源字段，不能互相覆盖。
- 2026-05 抽样交叉检验确认，历史分钟线 `09:30` 深圳开盘竞价条与实盘口径 `stk_auction` 存在系统性偏差；该偏差只在 Environment 特征层修正，raw 分钟线不改写。全天分钟汇总与 `daily` 的单位换算正常，没有发现全日量额单位错配。

| 优先级 | 接口 | 主要价值 | 下载与 PIT 边界 |
|---|---|---|---|
| 当前接入 | `kpl_list` | 开盘啦涨停、炸板、跌停、自然涨停、竞价榜单；包含题材、状态、竞价成交额、封单、换手等打板语义字段 | 官方标注次日 08:30 更新，主要作为次日情绪特征、日终标签和 evidence，不得在当日盘中提前使用 |
| 当前接入 | `limit_step`、`limit_cpt_list` | 连板天梯、最强涨停板块、连板高度、涨停家数和题材强度 | 按交易日下载；默认收盘后或次日可见，适合构造市场高度、板块轮动和打板情绪指标 |
| 当前接入 | `limit_list_ths` | 同花顺涨停池、连扳池、冲刺涨停、炸板池、跌停池；比 `limit_list_d` 更接近榜单语义 | 历史从 20231101 起，增量约 16:00 更新；2023-11-01 以前不能作为标签源，进入特征层时只按可见窗口使用 |
| 当前接入 | `top_list`、`top_inst`、`hm_list`、`hm_detail` | 龙虎榜、机构席位和游资交易明细，可刻画高情绪个股的席位结构和次日承接 | 龙虎榜按 20:00 可见，游资明细按收盘后保守处理；只能作为次日或更晚特征/evidence |
| 当前接入 | `ths_hot`、`dc_hot` | 同花顺/东方财富热榜，覆盖热股、概念、行业、人气和飙升榜 | 使用 `rank_time` 做 `available_at`；若做盘中 Agent，需要按采集批次落盘，不能用 22:30 最新榜回看盘中 |
| 条件补充 | `stk_auction`、`stk_auction_c` | 官方开盘/收盘集合竞价成交数据，可校验或替代分钟线竞价近似 | 历史分钟线已含 09:30/15:00 分钟条，所以不默认全量重复下载；若做 09:25 前后实盘决策，应从现在起定时采集 `stk_auction` |
| 条件补充 | `kpl_concept`、`kpl_concept_cons`、`ths_index`、`ths_daily`、`ths_member`、`dc_concept`、`dc_concept_cons`、`tdx_daily`、`tdx_member`、`moneyflow_cnt_ths`、`moneyflow_ind_dc` | 题材库、题材成分、板块行情和板块资金流，用于定义题材归因、板块强度和跨源一致性 | 不建议一次性把所有题材体系都设为核心特征；先选一个主体系，其他源作为交叉验证。部分新接口历史较短或不再新增，需在小窗口验证中记录 |

`board_trading` 默认下载 `kpl_list`、`limit_step`、`limit_cpt_list`、`limit_list_ths`、`top_list/top_inst`、`hm_list/hm_detail`、`ths_hot/dc_hot`。当前默认参数保留开盘啦 `涨停/炸板/跌停/自然涨停/竞价`，同花顺涨跌停榜单 `涨停池/连扳池/冲刺涨停/炸板池/跌停池`，同花顺热榜 `热股/行业板块/概念板块`，东方财富热榜 `A股市场` 的 `人气榜/飙升榜`，并使用 `is_new=N` 保留带 `rank_time` 的 PIT 快照。审计检查 sidecar 覆盖、行数触顶、重复业务键、`rank_time`/官方更新时间可解析性、金额/成交量单位，以及这些专题接口和 `limit_list_d`、分钟线推导标签之间的冲突样本。

如果未来要从“日终打板事件研究”升级到“真实盘中打板执行”，还需要在 Environment 层补充专门的 PIT 特征构造和执行约束：用当时已走完的分钟 bar 判断是否触板、是否开板、是否可下单，不使用 `limit_list_d.first_time/open_times/fd_amount` 这类日终汇总字段做盘中决策。更精确的排队成交、封单变化和撤单行为需要 QMT 实时盘口或更高频 Level-2 数据；当前 TuShare raw 主要支撑标签、回放和粗粒度策略验证。

### 财务与基本面

| 数据 | 接口 | 范围/拉取方式 | PIT 注意 |
|---|---|---|---|
| 利润表 | `income_vip` | `period=20100331-20260331` | 保留 `f_ann_date/report_type/comp_type` |
| 资产负债表 | `balancesheet_vip` | 同上 | 单次大窗口可能触顶 |
| 现金流量表 | `cashflow_vip` | 同上 | 单次大窗口可能触顶 |
| 财务指标 | `fina_indicator_vip` | 同上 | 无 `f_ann_date`，按 `ann_date` 更保守 |
| 业绩预告 | `forecast_vip` | `ann_month=201001-202605` | 事件和预期修正 |
| 业绩快报 | `express_vip` | `ann_month=201001-202605` | 财报前置可用信息 |
| 分红送股 | `dividend` | 全 `stock_basic` 代码 | `ann_date` 可为空，结合 `imp_ann_date/ex_date/record_date/pay_date` |
| 审计意见 | `fina_audit` | 全 `stock_basic` 代码 | 需按 `ts_code` 拉取 |
| 主营业务构成 | `fina_mainbz_vip` | 全 `stock_basic` 代码 | period 查询易触顶，优先按股票代码 |
| 披露计划 | `disclosure_date` | `period=20100331-20260331` | 披露计划/实际披露日期，不是数值表 |

财务基本面原始层保留多版本记录、重复业务键、少量空公告日和稀疏事件分区；进入 PIT 特征层时再按可见时间和业务键选择。

### 宏观与全球上下文

宏观/全球数据先作为 regime context 和 LLM evidence，不直接替代日频股票特征。落盘仍使用扁平化 `data/raw/<dataset>/...`。

| 数据 | 接口 | 拉取方式 | PIT/用途 |
|---|---|---|---|
| 经济数据发布日程 | `cn_schedule` | 按月 `m=YYYYMM` | 用 `publish_date` 修正 CPI/PPI/PMI/货币供应等宏观数据可见时间 |
| GDP | `cn_gdp` | `start_q/end_q` 一次拉取 | 季度宏观 regime，默认季末+45天保守可见 |
| CPI/PPI/PMI | `cn_cpi` / `cn_ppi` / `cn_pmi` | `start_m/end_m` 一次拉取 | 通胀和景气度；默认月末+31天保守可见 |
| 货币供应与社融 | `cn_m` / `sf_month` | `start_m/end_m` 一次拉取 | 流动性 regime；金额字段保持官方亿元口径 |
| 利率与回购 | `shibor` / `shibor_quote` / `shibor_lpr` / `repo_daily` | 按年 `start_date/end_date` | 资金价格；date-only 数据不得用于同日开盘决策 |
| 港/外币拆借利率 | `hibor` / `libor` | `hibor` 按年，`libor` 按货币+年份 | 离岸/外币流动性；默认 LIBOR 货币为 USD/EUR/JPY/GBP/CHF |
| 美国利率 | `us_tycr` / `us_trycr` / `us_tbr` / `us_tltr` | 按年 | 全球利率环境；date-only 保守晚间可见 |
| 全球财经日历 | `eco_cal` | 按月，可选 `country/currency/event` | 事件值异构，必须按事件解析；默认全国家/全货币/全事件 |
| 全球指数 | `index_global` | 默认主要指数代码+年份，可用 `--index-code` 扩展 | 跨市场风险偏好；OHLC 为指数点位 |
| 外汇日线 | `fx_daily` | 默认 `USDCNH.FXCM`+年份，可用 `--fx-code` 扩展 | 人民币汇率上下文；bid/ask quote，不是股票成交量 |
| 央行货币政策执行报告 | `monetary_policy` | 按发布年份，包含 `content_html`/PDF 链接 | 已购买独立权限；作为政策文本 evidence，先不直接影响下单 |

常用命令：

```bash
~/miniconda3/bin/conda run -n stock python scripts/tushare/download.py download --tier macro --start-date 20200101 --end-date <YYYYMMDD>
~/miniconda3/bin/conda run -n stock python scripts/tushare/download.py download --tier global --start-date 20200101 --end-date <YYYYMMDD>
```

### 历史分钟

| 数据 | 接口 | 范围/拉取方式 | 当前决策 |
|---|---|---|---|
| 历史 1 分钟源 | `stk_mins` | 全 A 股票池，按 `ts_code + year`，`freq=1min` | 批量下载和可追溯源层；只下载 1min，其他频率从 1min 重采样 |
| 历史 1 分钟按日文件 | 本地整理 | 从 `stk_mins` 源层整理为每交易日全市场文件 | PIT 回放、日内特征和后续每日增量更新优先读取该层 |
| 实盘/实时分钟 | `rt_min` / `rt_min_daily` | 仅实盘阶段使用 | 不并入历史 raw 下载 |
| 开/收盘竞价 | `stk_auction` / `stk_auction_c` | 不做历史全量下载 | 历史竞价由 `stk_mins` 的 `09:30` 和 `15:00` 分钟条承载；`stk_auction` 用于实盘开盘竞价和历史校验，历史 09:30 深圳分钟条进入特征层前需按校验系数修正 |

批量下载源路径为 `data/raw/stk_mins_1min/ts_code=<TS_CODE>/year=<YYYY>.parquet`；完整按日整理通过后，该源层可移动到 `archive/` 作为追溯备份。活跃按日最终路径为 `data/raw/stk_mins_1min_by_date/trade_date=<YYYYMMDD>.parquet`，字段同样包含 `ts_code, trade_time, open, high, low, close, vol, amount, trade_date, available_at, available_at_rule`。整理过程不保留额外中间结果，只在最终按日文件通过 schema、重复键、日期、时间、可见性和可选股票池覆盖校验后落盘。当前全 A `20200101-20260525` 按日层已落盘；使用分钟特征前应按有效股票池过滤。raw 分钟线不覆盖写入校正值；如果历史 09:30 分钟条被用作实盘 `stk_auction` 的替代特征，Environment 层会生成 `vol_pit/amount_pit` 并只对 09:30 的深圳股票应用校正：`00*.SZ` 使用 `0.76`，`30*.SZ` 使用 `0.58`，沪市、北交所和 15:00 收盘竞价保持 `1.0`。这些系数来自本项目对 `stk_mins_1min_by_date`、TuShare `stk_auction` 和日线单位的交叉检验；后续应定期用 `audit.py auction-alignment` 复核。

常用命令：

```bash
PYTHONUNBUFFERED=1 ~/miniconda3/bin/conda run -n stock python scripts/tushare/download.py download --tier intraday --raw-dir data/raw --datasets stk_mins --start-date 20200101 --end-date 20260525 --min-interval-seconds 0.22 --timeout-seconds 120
PYTHONUNBUFFERED=1 ~/miniconda3/bin/conda run -n stock python scripts/tushare/download.py compact-intraday-by-date --raw-dir data/raw --start-date 20200101 --end-date 20260525 --expected-codes-source none --min-rows-per-day 1
PYTHONUNBUFFERED=1 ~/miniconda3/bin/conda run -n stock python scripts/tushare/download.py update-intraday-by-date --raw-dir data/raw --start-date 20260526 --end-date 20260526 --expected-codes-source daily --max-retries 3
```

`update-intraday-by-date` 会按交易日和股票池请求 `stk_mins`，请求失败的股票只在内存中重试；超过允许缺失数时该交易日文件不写入，避免保留半成品。该逻辑已纳入每日 `update` 默认流程；单独入口主要用于补跑、窗口测试或排查。已有按日文件默认允许少量历史源端缺口不触发全市场重拉，参数为 `--existing-allow-missing-codes`，默认 50；新写入文件仍使用 `--allow-missing-codes` 控制，默认 0。

### 事件与资金数据

| 数据 | 接口 | 用途 |
|---|---|---|
| 两融汇总 | `margin` | 杠杆与市场情绪 |
| 两融明细 | `margin_detail` | 个股融资融券压力 |
| 个股资金流 | `moneyflow` | 资金行为因子 |
| 股东人数 | `stk_holdernumber` | 筹码集中度 |
| 股东增减持 | `stk_holdertrade` | 公司治理/事件 |
| 回购 | `repurchase` | 资本配置与安全边际 |
| 解禁 | `share_float_complete` | 供给压力；由 `share_float` 多路径补全后形成最终 union |
| 大宗交易 | `block_trade` | 特殊交易行为 |
| 卖方盈利预测 | `report_rc` | 已归入文本 evidence，不在事件/资金默认下载 |

事件/资金通用下载入口负责两融、资金流、股东、回购和大宗交易。`share_float` 使用专用 `download-share-float-complete` 入口生成 `share_float_complete` union。日频资金表按交易日分区，股东/回购等稀疏公告表按月份分区；解禁的活跃保留边界是 `share_float_complete` union，`float_date` 日分区、`ann_date` 主路径和 candidate 级 `ann_date+ts_code` 补充文件属于补全过程产物，可归档。若请求结束日超过本地 SSE 交易日历覆盖范围，交易日类接口会自动截到本地交易日历最后一天，自然日事件表仍按请求结束日下载：

```bash
PYTHONUNBUFFERED=1 ~/miniconda3/bin/conda run -n stock python scripts/tushare/download.py download --tier event_flow --raw-dir data/raw --start-date 20200101 --end-date <YYYYMMDD> --min-interval-seconds 0.22 --timeout-seconds 90
```

事件/资金 raw 层以预期分区和 `share_float_complete` union 作为保留边界。主要 raw 业务语义风险包括：`share_float` 源端存在 6000 行上限风险，股东/回购/大宗等事件表存在重复业务键或可为空日期字段，特征层必须按 PIT 和业务键重新选择。

每日 `update` 默认运行 `share_float_complete`：近期 `ann_date`/`float_date` 下载窗口使用本次 `--start-date` 到 `--end-date`；union 重建窗口固定覆盖 `ann_date=20100101-<end_date>` 和 `float_date=20200101-<end_date>`，所以不会丢失历史 union。默认会对触及 6000 行上限的近期 `ann_date` 分区执行 candidate 级补充，受 `--max-ann-rescue-days` 和 `--max-rescue-calls` 保护。由于历史 `share_float` 过程目录已经归档，union 重建会同时扫描 `data/raw` 和 `archive/data_raw/*` 中保留的 `share_float_ann_date`、`share_float_ann_date_ts_code`、`share_float_float_date`、`share_float_float_date_ts_code` 等过程目录；如果扫描结果会让既有 `share_float_complete` 行数缩小，脚本默认报错而不是覆盖。

`share_float` 补全入口：

```bash
PYTHONUNBUFFERED=1 ~/miniconda3/bin/conda run -n stock python scripts/tushare/download.py download-share-float-complete \
  --raw-dir data/raw \
  --ann-start-date 20100101 \
  --ann-end-date 20260525 \
  --float-start-date 20200101 \
  --float-end-date 20260525 \
  --rescue-ann-limit-hits \
  --write-union
```

`share_float` 当前使用 `ann_date` 作为 PIT 主路径，并对触顶 `ann_date` 分区执行 candidate 级 `ann_date+ts_code` 补充。补全过程默认不写独立状态文件；如需保留过程细节，可显式传 `--output` 写入临时路径。合并备用文件为：

```text
data/raw/share_float_complete/share_float_complete.parquet
```

该备用文件由 `ann_date` 主路径、`ann_date+ts_code` candidate 补充、原 `float_date` 路径和已有 `float_date+ts_code` 文件合并去重得到。注意：candidate 补充后仍存在最细 `ann_date+ts_code` 文件正好 6000 行，说明 TuShare 源端对单股单公告日也可能继续截断；这些记录只能标记 `source_cap_risk`，不能声称数学意义上完全无截断。

候选股票救援顺序：

1. 触顶分区自身已经出现的 `ts_code`。
2. 另一条 `share_float` 路径交叉出现的 `ts_code`，例如 `float_date` 触顶时扫描 `ann_date` 路径里 `float_date=目标日` 的记录。
3. `anns_d` 中标题包含限售、解禁、上市流通等关键词的公告 `ts_code`。
4. 显式传入的 `--rescue-code` 或 `--rescue-codes-file`。
5. 只有显式 `--rescue-universe all_a` 时才全 A。

触顶救援必须使用 `stock` 环境，并显式设置范围和预算：

```bash
PYTHONUNBUFFERED=1 ~/miniconda3/bin/conda run -n stock python scripts/tushare/download.py download-share-float-complete \
  --raw-dir data/raw \
  --skip-ann-date \
  --rescue-ann-date 20200103 \
  --float-rescue-date 20200106 \
  --rescue-code 002973.SZ \
  --max-rescue-calls 1000
```

救援默认使用 `--rescue-universe candidate`，不会全 A 扫描。`--max-rescue-calls` 默认 50000，超过预算会 fail fast，不会发起 API 下载。`--rescue-universe all_a` 是完整扫描备用入口，不作为默认路线。

### 文本 Evidence

文本 Evidence 是独立 raw tier，不并入基础/日频/财务默认链路。当前保留以下 raw 数据源：

| 数据 | 接口 | 分区与 PIT 规则 |
|---|---|---|
| 上市公司全量公告 | `anns_d` | 月度分区；优先 `rec_time`，缺失时保守视为收盘后/次日可见 |
| 长新闻 | `major_news` | 月度分区；`pub_time` 为 `available_at` |
| 新闻联播 | `cctv_news` | 日分区；只有日期时保守设为当日晚间可见 |
| 政策法规库 | `npr` | 月度分区；`pubtime` 为 `available_at`，HTML 保留 raw/hash |
| 券商研究报告 | `research_report` | 月度分区；`trade_date` 只有日期，不能给盘中策略使用 |
| 卖方盈利预测 | `report_rc` | 月度分区；优先 `create_time`，否则按晚间更新保守可见 |
| 新闻快讯 | `news` | 自动展开 9 个官方 `src`，按来源+日期分区；`datetime` 为 `available_at` |

当前文本 raw 层已完成 2020 年以来主要来源下载。进入模型前必须生成 `evidence_id`、`document_hash`、`available_at`、`source_quality`，并做正文长度限制和公司/行业实体映射。

### 下载前检查

- 确认 `TUSHARE_TOKEN` 只存在于环境变量或 ignored local `.env`。
- 运行 `nvidia-smi` 和 `free -h`，记录资源状态。
- 先做 1-3 个交易日或少量股票 dry-run，验证字段、分区、唯一键、sidecar 和触顶告警。
- 长任务必须使用断点续跑、限频、重试和本地日志；`logs/`、`data/`、`results/`、`wandb/` 不提交 Git。

### 官方文档

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
- 上市公司公告：https://tushare.pro/document/2?doc_id=176
- 中国经济数据发布日程：https://tushare.pro/document/2?doc_id=461
- GDP：https://tushare.pro/document/2?doc_id=227
- CPI/PPI/PMI/货币供应/社融：https://tushare.pro/document/2?doc_id=228 / https://tushare.pro/document/2?doc_id=229 / https://tushare.pro/document/2?doc_id=325 / https://tushare.pro/document/2?doc_id=242 / https://tushare.pro/document/2?doc_id=310
- 利率与全球事件：https://tushare.pro/document/2?doc_id=202 / https://tushare.pro/document/2?doc_id=204 / https://tushare.pro/document/2?doc_id=205 / https://tushare.pro/document/2?doc_id=206 / https://tushare.pro/document/2?doc_id=233
- 全球指数/外汇/美国利率：https://tushare.pro/document/2?doc_id=211 / https://tushare.pro/document/2?doc_id=179 / https://tushare.pro/document/2?doc_id=218
- 央行货币政策执行报告：https://tushare.pro/document/2?doc_id=465

## 审计口径

命令、状态文件和文档都按数据域命名。`results/data_quality/` 顶层只保留当前状态文件：

| 文件 | 覆盖范围 |
|---|---|
| `base_research_status.json` | 基础维表、日频行情与约束、财务基本面 |
| `macro_context_status.json` | 国内宏观、央行货币政策、全球事件、跨市场上下文 |
| `intraday_minutes_status.json` | 历史分钟线 |
| `event_flow_status.json` | 事件/资金数据；当前为 warning，无缺失分区 |
| `board_trading_status.json` | 打板专题数据 |
| `text_evidence_status.json` | 文本 evidence raw tier |

跨域合并审计不作为顶层当前状态文件维护。除下表定义的正式 status 入口外，凡是临时把多个顶层数据域合并到一次报告中，都必须显式传 `--output`，把报告写到临时路径；不能默认覆盖 6 个正式 status 中的任意一个。临时排查产物可先写入 `results/data_quality/process/`；处理完成后必须从该目录移出：需要留痕的移动到根目录 `archive/`，不再需要的直接删除。`share_float` 补全下载默认不写状态文件，关键结果由 `event_flow_status.json` 统一审计。

当前数据域按 6 类维护，下载、审计和顶层 status 采用同一套语义边界。`reference`、`daily`、`fundamental`、`macro`、`global` 等只是脚本执行子步骤，用来控制依赖、限频和体量，不作为额外的人读数据域。

| 数据域 | 下载子步骤 | 当前 status | 审计入口 |
|---|---|---|---|
| 基础研究数据 | `reference`、`daily`、`fundamental` | `base_research_status.json` | `scripts/tushare/audit.py base --include-limit-list` |
| 宏观与全球上下文 | `macro`、`global` | `macro_context_status.json` | `scripts/tushare/audit.py macro` |
| 历史分钟线 | `intraday`、`compact-intraday-by-date` | `intraday_minutes_status.json` | `scripts/tushare/audit.py intraday-by-date` |
| 事件/资金数据 | `event_flow`、`download-share-float-complete` | `event_flow_status.json` | `scripts/tushare/audit.py event-flow` |
| 打板专题数据 | `board_trading` | `board_trading_status.json` | `scripts/tushare/audit.py board-trading` |
| 文本 evidence | `text_evidence` | `text_evidence_status.json` | `scripts/tushare/audit.py base --include-text` |

主要审计逻辑按数据域展开如下。

#### 基础研究数据

合并检查基础维表、日频行情与交易约束、财务基本面。核心逻辑包括 Parquet/sidecar 一致性、股票池和交易日覆盖、日频分区完整性、分页/触顶风险、跨表股票覆盖差异、财务多版本/重复业务键、`ann_date`/`f_ann_date` PIT 可见性、单位口径和已知源端稀疏问题。

#### 宏观与全球上下文

检查国内宏观、利率、社融、发布日程、全球财经日历、跨市场指数/外汇/美债利率、央行货币政策执行报告。按接口策略检查季度/月度/年份/代码/货币分区、sidecar、空分区、重复事件键、保守 `available_at` 和单位规则。

#### 历史分钟线

以最终按日文件为准，检查交易日文件数、sidecar、必需字段、重复 `(ts_code, trade_time)`、错误 `trade_date`、时间解析、`available_at` 解析、09:30/15:00 竞价分钟条和零行文件。股票池覆盖差异属于专项排查，只能在处理期间临时写入 `results/data_quality/process/`。竞价口径专项校验使用 `scripts/tushare/audit.py auction-alignment`，对比本地 09:30 分钟条、TuShare `stk_auction` 和日线全天单位；该报告只作为过程审计，不写入顶层 status。

#### 事件/资金数据

检查两融、资金流、股东人数、股东增减持、回购、大宗交易分区和 sidecar。`share_float` 以 `share_float_complete` union 为保留边界；审计重复业务键、空日期、PIT 可见性、单位规则和源端 6000 行 cap 风险。

#### 打板专题数据

检查开盘啦榜单、同花顺涨跌停榜单、连板天梯、最强板块、龙虎榜、机构席位、游资名录/明细、同花顺热榜和东方财富热榜。按交易日、tag、limit_type、market、hot_type 和 `is_new` 分区检查 sidecar、分页触顶、重复业务键、`available_at` 可解析性和单位规则。`kpl_list` 按次日 08:30 可见，`limit_list_ths` 按 16:00 左右可见，龙虎榜按 20:00 可见，热榜优先使用 `rank_time`，没有精确时间时保守按日终或官方最新榜时点处理。

已确认的 raw 源端现象：`top_list` 历史数据中会出现少量 exact duplicate，也会出现同一 `ts_code` 同日同原因但名称不同的 ST/name alias 行。审计业务键保留 `name` 来避免把名称别名误判为重复；进入 PIT 特征或 evidence 前仍要按业务键、来源和可见时间做 deterministic 去重。

#### 文本 evidence

检查公告、新闻、政策法规、研报、盈利预测文本源分区。按来源/月份/日期策略检查文件完整性、sidecar、分页触顶、重复业务键、`available_at` 解析和文本 evidence raw 边界。

因此，下载阶段也按 6 个数据域理解，只是在执行时拆成更细子命令：

1. 基础研究数据拆成 `reference`、`daily`、`fundamental` 下载，是因为后两者依赖股票池和交易日历，且调用频率、窗口和体量不同；审计时必须合并判断。
2. 宏观与全球上下文拆成 `macro`、`global` 下载，是为了便于按研究用途和接口参数组织；审计状态合并为一个宏观上下文边界。
3. 历史分钟线下载源层和按日整理是两个执行动作，但最终保留和审计边界只有按日分钟层。
4. 事件/资金中 `share_float` 补全有多条过程路径，但最终保留和审计边界是 `share_float_complete` 与其他事件/资金表。
5. 打板专题数据独立维护，因为榜单、热榜、龙虎榜、游资明细的 PIT 时点和业务键不同于普通资金事件，也不同于纯文本 evidence。
6. 文本 evidence 独立维护，因为来源、分页、去重和 prompt evidence 边界与结构化数据不同。

`results/data_quality/process/` 是临时处理区，不是状态归档区。约束如下：

- 只允许存放仍在处理中的专项排查结果，例如分钟按日层对 `daily` 股票池的覆盖差异。
- 这类文件不代表新的顶层数据域，也不需要和 6 个当前 status 一起手动维护。
- 排查完成后，保留价值明确的文件移到根目录 `archive/`，否则删除。
- `results/data_quality/` 顶层不得重新积累历史状态文件。

PIT 特征构建和泄漏测试不是 raw 下载数据域，不进入 `results/data_quality/` 顶层 status；它们由 `scripts/hl.py build-features`、单元测试和实验 ledger 检查。

### Status 文件结构

6 个顶层 status 都由审计脚本直接覆盖写入，不需要手动修改。文件结构保持一致：

- `created_at`：审计报告生成时间，UTC。
- `raw_dir`：本次审计读取的数据根目录。
- `scope`：命令参数和数据范围，例如起止日期、数据项、指数代码、外汇代码、分钟股票池来源。
- `status`：由 finding 最高严重级别决定，`error > warning > ok`。
- `finding_counts`：`error`、`warning`、`info` 计数。
- `datasets`：按数据项聚合的状态、finding 计数和检查名。
- `findings`：逐条审计结果，包含 `severity`、`check`、`message`、`details`。
- `unit_rules` / `pit_rules`：该数据域当前认可的单位和可见时间规则。
- `doc_refs`：对应 TuShare 官方文档链接。
- `conclusions`：审计结论，只描述当前可操作状态。

脚本返回码也遵循同一规则：存在 `error` 返回非 0；只有 `warning` 时返回 0，但下游特征或实验必须显式处理 warning 指向的语义风险。

### 1. 基础研究数据审计

入口：

```bash
~/miniconda3/bin/conda run -n stock python scripts/tushare/audit.py base --include-limit-list --end-date <YYYYMMDD> --fundamental-end-date <YYYYMMDD>
```

默认输出：`results/data_quality/base_research_status.json`。

代码逻辑：

1. 解析 `raw_dir` 和日期范围；如果未传 `--end-date`，使用本地 SSE 交易日历最后一个开市日。
2. 根据参数选择数据项：基础维表固定包含股票池、公司信息、交易日历、曾用名、行业分类和行业成分；日频默认包含 `daily`、`adj_factor`、`daily_basic`、`stk_limit`、`suspend_d` 和 `limit_list_d`；财务包含财报主表、指标、预告、快报、分红、审计意见、主营构成和披露计划。
3. `audit_integrated_filesystem` 做文件系统级检查：数据目录是否存在、Parquet 是否可读、`.meta.json` sidecar 是否存在、空文件或缺少必需字段是否出现。
4. 基础维表专项检查：
   - `stock_basic` 检查 `L/D/P` 文件、必需字段空值、状态分布和股票代码唯一性。
   - `stock_company` 检查公司信息覆盖，但不要求覆盖等于股票池。
   - `trade_cal` 提取 SSE 开市日，作为日频、分钟和 WFO 日期基准。
   - `bak_basic` 检查交易日覆盖和首个非空日；它只作为补充快照，不替代主行情。
   - `namechange` 检查曾用名/ST 变更日期字段和重复键。
   - `index_classify`、`index_member_all` 检查申万行业层级和成分覆盖。
5. 日频专项检查：
   - 以 SSE 开市日生成预期 `trade_date` 分区。
   - 对 `daily`、`daily_basic`、`adj_factor`、`stk_limit`、`suspend_d`、`limit_list_d` 检查缺失分区、sidecar、schema、空分区和重复业务键。
   - 做跨表股票覆盖差异，区分源端口径差异和疑似缺失。
   - `audit_unit_schema` 固化单位口径：`daily.vol=手`、`daily.amount=千元`、`daily_basic` 股本为万股/市值为万元。
   - 可选 API case study 用 `bak_daily` 与本地日频样本对比覆盖和单位，结果只作为审计案例，不把 `bak_daily` 纳入主干。
6. PIT 和股票池语义检查：
   - `audit_stock_universe_semantics` 检查上市、退市、暂停上市股票与行情表覆盖的关系。
   - `audit_pit_availability` 检查 `ann_date`、`f_ann_date`、披露日等可见时间字段是否可用于 PIT 选择。
7. 财务专项检查：
   - 按 period、ann_month 或 ts_code 策略生成预期文件。
   - 检查缺失文件、空分区、sidecar、必需字段、重复业务键和单次请求触顶风险。
   - `audit_fundamental_unit_and_pit_semantics` 固化财报金额、预测金额、`f_ann_date`/`ann_date` 优先级、多版本保留等规则。
8. 写入 case studies：日频覆盖、财务 PIT、多版本重复键、分红日期异常等样本，供后续特征层实现验证。

基础研究数据的 `warning` 通常代表源端口径差异、多版本原始记录或可接受稀疏性；`error` 才代表结构不可用或预期文件缺失。

### 2. 宏观与全球上下文审计

入口：

```bash
~/miniconda3/bin/conda run -n stock python scripts/tushare/audit.py macro --start-date <YYYYMMDD> --end-date <YYYYMMDD>
```

默认输出：`results/data_quality/macro_context_status.json`。

代码逻辑：

1. 根据命令参数选择宏观、政策和全球上下文数据项，包括 `cn_schedule`、GDP、CPI/PPI/PMI、货币供应、社融、资金利率、HIBOR/LIBOR、美国利率、全球财经日历、全球指数、外汇和央行货币政策执行报告。
2. `audit_integrated_filesystem` 检查目录、Parquet、sidecar 和 schema。
3. `expected_macro_paths` 按接口策略生成预期分区：
   - 月度表按 `YYYYMM`。
   - 季度表按 `YYYYQn` 或起止季度。
   - 年份表按年份窗口。
   - 全球指数、外汇、LIBOR 按代码/货币加年份。
   - `eco_cal` 按月份和可选国家、货币、事件过滤。
4. `audit_macro_dataset` 检查每个数据项的缺失分区、空分区、sidecar、字段、重复键和触顶风险。
5. `audit_macro_keys` 对事件类和时间序列表做重复业务键统计；`eco_cal` 允许同日多事件，但不能把异构事件值直接当作统一数值因子。
6. 报告写入 `macro_unit_rules` 和 `macro_pit_rules`：月度/季度宏观在 raw 层使用保守可见时间，进入特征层前优先用 `cn_schedule.publish_date` 或更精确发布时间修正。

宏观上下文目前先用于 regime context 和 evidence，不直接进入默认日频公式化特征。

### 3. 历史分钟线审计

当前顶层 status 只审计最终按日分钟层：

```bash
~/miniconda3/bin/conda run -n stock python scripts/tushare/audit.py intraday-by-date --start-date <YYYYMMDD> --end-date <YYYYMMDD> --full-scan
```

默认输出：`results/data_quality/intraday_minutes_status.json`。

代码逻辑：

1. 使用本地 SSE 开市日历生成预期交易日。
2. 对 `data/raw/stk_mins_1min_by_date/trade_date=<YYYYMMDD>.parquet` 建立文件清单。
3. 库存检查：
   - 每个预期交易日是否有文件。
   - 每个文件是否有 `.meta.json` sidecar。
   - 必需字段是否完整。
   - 零行文件数量和总行数；最终按日分钟文件出现 0 行是 error。
4. 深度检查：
   - 默认可抽样，`--full-scan` 检查全部日期。
   - `validate_stk_mins_by_date_frame` 检查 `trade_date` 是否与分区一致、`trade_time` 是否可解析、`available_at` 是否可解析、`(ts_code, trade_time)` 是否重复、行数是否低于阈值。
   - 可用 `--expected-codes-source daily` 将当日分钟股票覆盖与日频股票池对比；该覆盖差异属于专项排查，可临时写入 `results/data_quality/process/`，处理完成后归档到根目录 `archive/` 或删除，不改变顶层 6 域。
5. 单位规则写入报告：`vol=股`、`amount=元`、`available_at=trade_time`，历史 09:30 和 15:00 分钟条承载开盘/收盘竞价。

`scripts/tushare/audit.py intraday` 审计按股票+年份保存的源层，用于下载追溯或源层排查；当前研究、PIT 和增量更新以按日层为准。

### 4. 事件/资金数据审计

入口：

```bash
~/miniconda3/bin/conda run -n stock python scripts/tushare/audit.py event-flow --start-date <YYYYMMDD> --end-date <YYYYMMDD>
```

默认输出：`results/data_quality/event_flow_status.json`。

代码逻辑：

1. 选择事件/资金数据项：两融汇总、两融明细、个股资金流、股东人数、股东增减持、回购、解禁、大宗交易。
2. 如果 `share_float_complete/share_float_complete.parquet` 存在，则文件系统审计不再要求保留 `share_float` 原始过程目录；解禁以 complete union 为保留边界。
3. `audit_integrated_filesystem` 检查 retained raw 文件、Parquet、sidecar 和 schema。
4. `expected_event_paths` 按数据项策略生成预期路径：
   - 日频资金表按交易日。
   - 稀疏公告/事件表按月份。
   - 解禁最终 union 只检查保留文件。
5. `audit_event_dataset` 检查缺失分区、空分区、sidecar、字段、重复业务键、空日期和触顶风险。
6. `audit_share_float_complete_union` 检查解禁 union：
   - 文件是否存在、是否可读、是否有关键字段。
   - 是否覆盖 `ann_date` 主路径、candidate 补充、`float_date` 路径和已有补充路径。
   - 是否存在 exact-6000 或 candidate 级 exact-6000 风险。
   - union 去重后的业务键和源路径统计。
7. 报告写入 `event_unit_rules` 与 `event_pit_rules`，明确资金流、两融、大宗、公告事件的可见时间和原始单位。

事件/资金的空月份或空日期不一定是错误；只有缺失预期文件、结构不可读、关键字段缺失才应阻断下游。

### 5. 打板专题数据审计

入口：

```bash
~/miniconda3/bin/conda run -n stock python scripts/tushare/audit.py board-trading --start-date <YYYYMMDD> --end-date <YYYYMMDD>
```

默认输出：`results/data_quality/board_trading_status.json`。

代码逻辑：

1. 选择打板专题数据项：`kpl_list`、`limit_step`、`limit_cpt_list`、`limit_list_ths`、`top_list`、`top_inst`、`hm_list`、`hm_detail`、`ths_hot`、`dc_hot`。
2. `expected_board_paths` 按接口策略生成预期路径：
   - 普通交易日表按 `trade_date=<YYYYMMDD>`。
   - `kpl_list` 按 `tag=<TAG>/trade_date=<YYYYMMDD>`。
   - `limit_list_ths` 按 `limit_type=<TYPE>/trade_date=<YYYYMMDD>`，从 `20231101` 起生成预期路径。
   - `ths_hot` 按 `market=<MARKET>/is_new=<Y|N>/trade_date=<YYYYMMDD>`。
   - `dc_hot` 按 `market=<MARKET>/hot_type=<TYPE>/is_new=<Y|N>/trade_date=<YYYYMMDD>`。
   - `hm_list` 是静态参考表，路径为 `hm_list/hm_list.parquet`。
3. `audit_integrated_filesystem` 检查目录、Parquet、sidecar 和 schema。
4. `audit_board_dataset` 检查缺失分区、空分区、sidecar、字段、分页触顶和重复业务键。
5. `audit_board_keys` 检查 `available_at` 是否存在并可解析；静态 `hm_list` 不强制要求历史 PIT 时间。
6. 报告写入 `board_unit_rules` 和 `board_pit_rules`：
   - `kpl_list` 以次日 08:30 可见。
   - `limit_list_ths` 以当日 16:00 左右可见。
   - `top_list/top_inst` 以当日 20:00 可见。
   - `limit_step/limit_cpt_list/hm_detail` 保守按当日日终可见。
   - `ths_hot/dc_hot` 优先用 `rank_time`，`is_new=Y` 没有精确时间时按 22:30 可见。

打板专题数据的 `warning` 通常代表源端重复键、分页触顶或某些历史阶段接口稀疏；这些不应直接阻断 raw 层，但进入 PIT 特征和 Agent evidence 前必须按 `available_at` 过滤，并与 `limit_list_d`、分钟线推导涨停标签做冲突样本检查。

### 6. 文本 Evidence 审计

入口：

```bash
~/miniconda3/bin/conda run -n stock python scripts/tushare/audit.py base --include-text --text-start-date <YYYYMMDD> --text-end-date <YYYYMMDD>
```

默认输出：`results/data_quality/text_evidence_status.json`。

代码逻辑：

1. `audit_unified` 在基础研究审计基线之外追加文本 evidence 数据项，并由默认输出路由到文本 status；因此文本 status 中可能包含基础维表或日频/财务依赖检查。
2. `selected_integrated_text_datasets` 选择公告、长新闻、新闻联播、政策法规、券商研报、盈利预测、新闻快讯。
3. `expected_text_paths` 按接口策略生成预期分区：
   - 公告、长新闻、政策法规、研报、盈利预测按月份。
   - 新闻联播按日期。
   - 新闻快讯按官方 `src` 加日期。
4. `audit_text_dataset` 检查文件、sidecar、schema、空分区、重复业务键、分页触顶和时间字段。
5. `audit_text_keys` 对每类文本建立保守业务键：公告/新闻用标题、来源、发布时间和股票代码组合；研报/预测用报告或机构相关字段组合。
6. 文本只到 raw evidence 边界。进入 LLM 前必须再次生成 `evidence_id`、`document_hash`、`available_at`、来源质量、正文截断结果和公司/行业实体映射；这些属于 evidence pack 或 Agent 层，不由 raw status 直接表示。

文本重复业务键通常保留为 warning，因为上游可能重复推送或多来源转载；LLM evidence 层必须按 hash 和可见时间去重。

## Raw PIT 数据合同

Data 层只定义 raw 数据能否支持 PIT，不负责生成 feature、observation 或 evidence pack。具体 PIT feature/observation 构造、selector、泄漏检查和回放时点可见性由 Environment 负责，见 `docs/environment_design.md`；LLM evidence pack 的输入边界见 `docs/agent_design.md`。

### 原始层元数据

所有 raw 文件必须带 `.meta.json` sidecar，至少记录接口名、请求参数、抓取时间和源数据 hash。数据行本身尽量保留 TuShare 原始字段；`available_at` 只在能够保守推断时写入或在特征层派生。多版本财报、重复公告、稀疏事件和源端重复推送不在 raw 层强行删除。

### Raw 可见性原则

- raw 层不得把未来事件生效日伪装成当前可见信息，例如解禁 `float_date`、分红 `ex_date`、业绩报告期 `period` 都不能替代公告可见时间。
- 只含日期、不含时间的数据默认不能用于同日开盘决策；日频行情和日频指标默认下一交易日可交易。
- 财务、公告、研报、宏观发布等异步数据必须保留公告日、实际发布时间或可保守推断发布时间，使 Environment 能构造 `available_at <= decision_time` 的选择器。
- 同一业务键多版本数据在 raw 层全部保留；Environment 或 Agent evidence 层按决策时点选择当时最新可见版本。
- raw 审计只判断字段、单位、分区、sidecar、触顶风险和可见时间字段是否足以支撑 PIT；不声明某个特征在回测中无泄漏。

### 交给 Environment 的最小合同

每个进入特征或回放的数据域至少要能提供：

- 数据来源：TuShare 接口名、请求参数、分区路径和 sidecar。
- 业务键：例如 `(trade_date, ts_code)`、`(ts_code, period, report_type, comp_type)`、公告标题/发布时间/source 组合。
- 时间键：原始交易日、公告日、发布时间、生效日和保守 `available_at` 候选。
- 单位规则：价格、成交量、成交额、股本、市值、财报金额、宏观数值和事件数量口径。
- 触顶和稀疏风险：分页上限、exact-limit、空分区、源端缺失或重复推送标记。

### 跨域 PIT 要求

- 财务：raw 层保留 `f_ann_date`、`ann_date`、`period`、`report_type`、`comp_type` 和多版本记录；Environment 选择 `available_at <= decision_time` 的最新可见版本。
- 分红、解禁、回购、股东事件：raw 层同时保留公告日期和事件生效日期；Environment 做 PIT 时只能用公告可见性决定是否暴露未来事件属性。
- 资金流、两融、大宗：raw 层记录交易日和保守可见时间；日频策略默认只能影响下一交易日及以后。
- 宏观：raw 层保留原始月份、季度、发布日期、发布日程和保守可见时间；Environment 后续可用 `cn_schedule.publish_date` 或更精确发布时间替换保守规则。
- 文本：raw 层保留来源、URL/标题、发布时间、正文或 HTML hash；Agent evidence 层再生成 `evidence_id`、`document_hash`、截断正文和实体映射。
