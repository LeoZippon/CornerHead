# 参数参考

本文档集中汇总研究、回放、数据任务、Agent、Sandbox 和控制台的参数、超参数与关键常量。代码与运行记录是事实源；本文档是派生速查，修改旋钮时应在同一工作项内同步本表和相应权威文档。

实际生效值按作用域留痕：实验和回放参数进入 run manifest，快照参数进入 snapshot manifest，交互式创建参数进入 `params.json`，数据任务参数进入调度配置和任务状态，浏览器本地设置由浏览器保存。各节同时说明控制台、CLI、API/参数文件、代码级配置和运行时派生值之间的入口差异。

**相关边界**

- 数据源、源单位、刷新和审计合同见 [数据文档](data_documentation.md)。
- 快照、执行/回放与 Broker 语义见 [Environment 设计](environment_design.md)。
- Agent 合同及预算使用方式见 [Agent 设计](agent_design.md)。
- Fold、Epoch、验收和冻结语义见 [Pipeline 设计](pipeline_design.md)。
- 控制台与 QMT 实盘边界见 [部署文档](deployment_documentation.md)。

**职责边界**

参数参考负责集中汇总配置入口、默认值、范围、派生规则和运行留痕位置。参数参考不定义数据、回放、Agent、Pipeline 或部署语义；发生冲突时以代码、运行记录和对应权威文档为准。

**导航**

- [1. 快照窗口（SnapshotConfig）](#1-快照窗口snapshotconfig)
- [2. 实验编排与验收（ExperimentConfig / AcceptanceRules / ModificationConstraints）](#2-实验编排与验收experimentconfig--acceptancerules--modificationconstraints)
- [3. 回放执行与预算](#3-回放执行与预算)
- [4. Broker profile（账户、成本与信用）](#4-broker-profile账户成本与信用)
- [5. Agent 会话与上下文管理](#5-agent-会话与上下文管理)
- [6. Sandbox 资源与工具预算](#6-sandbox-资源与工具预算)
- [7. 数据层任务参数](#7-数据层任务参数)
- [8. 报告与其他常量](#8-报告与其他常量)
- [9. HITL、模型、联网与控制台](#9-hitl模型联网与控制台)
- [10. 构造期校验与失败](#10-构造期校验与失败)

## 1. 快照窗口（SnapshotConfig）

本章汇总各数据域 Snapshot 窗口、数据集成员和构造开关。

窗口参数可由实验 CLI、控制台或参数文件覆盖；未单独指定的数据域回退基础窗口。数据集清单和构造开关当前属于代码级配置。全部配置（含 `news_sources` 与 `news_window_months`）进入快照记录。

| 参数 | 默认 | 作用 |
|---|---:|---|
| `window_months` | 21 | 各数据域月窗口的基础回退值 |
| `daily_window_months` | None（回退基础） | `daily` 域月窗口 |
| `fundamentals_window_months` | None（回退基础） | `fundamentals` 域月窗口（可见披露口径） |
| `events_window_months` | None（回退基础） | `events` 域月窗口 |
| `macro_window_months` | None（回退基础） | `macro` 域月窗口 |
| `text_window_months` | None（回退基础） | `text` 域月窗口 |
| `intraday_trade_days` | 21 | 决策输入快照的分钟线交易日窗口（回放槽分钟窗口由 Fold 周期决定，与此无关） |
| `include_events` / `include_macro` / `include_text` / `include_fundamentals` / `include_intraday` | True | 数据域开关：关闭 = 决策快照与回放槽均不加载该域（分钟域关闭后回放退化为日线粒度） |
| `events_datasets` / `macro_datasets` / `text_datasets` / `fundamental_datasets` | ()（=该域全部默认数据集） | 数据项子集：只加载所选数据集；未知名称在配置构造时 fail-fast |
| `news_sources` | ()（=磁盘上全部来源） | `news` 快讯来源子集；空元组表示自动发现全部 `src=` 分区，显式列表逐源 fail-fast 校验；跨源文本载荷 hash 去重恒开启 |
| `news_window_months` | None（跟随 text 窗口） | `news` 独立滚动窗口钳制；None 表示不额外钳制 |
| `screen_exclude_st` | False | 股票筛选：按决策锚点在市名称剔除含 ST 的股票 |
| `screen_exclude_new_listed_days` | 0 | 股票筛选：剔除锚点前 N 天内上市的新股（0=关） |
| `screen_min_circ_mv_yi` / `screen_max_circ_mv_yi` | None | 股票筛选：锚点流通市值带（亿元）；属性缺失 fail-closed |
| `screen_min_price` / `screen_max_price` | None | 股票筛选：锚点收盘价带（元） |
| `screen_boards` | ()（全部板块） | 股票筛选：板块子集 main/gem/star/bj；筛选集合整区间冻结，限制全部逐股域（决策快照+回放槽），空集在快照构建时显式报错 |

`universe` 不使用月窗口，按决策日在市口径生成。

**数据集清单与构造开关（代码级配置）**

| 参数 | 默认 | 作用 |
|---|---|---|
| `events_datasets` | margin、资金流（含东财/同花顺个股与板块）、筹码、备用行情、盘前、转融通、股东（含前十大）、质押、调研、IPO、回购、解禁、龙虎榜、打板（含开盘啦概念/东财板块）、热榜和游资等 37 个数据集 | 决策快照的事件域成员；精确清单写入 snapshot manifest |
| `macro_datasets` | 国内宏观、利率、政策、全球指数、A 股指数（含估值/申万/中信/同花顺行业与市场统计）、外汇、回购利率、美债曲线、全市场资金流和券商金股等 25 个数据集 | 决策快照的宏观域成员 |
| `text_datasets` | `anns_d`、`major_news`、`cctv_news`、`npr`、`research_report`、`report_rc`、`irm_qa_sh`、`irm_qa_sz`、`news` | 文本域成员 |
| `fundamental_datasets` | 财务三表、指标、预告、快报、分红、审计、主营构成和披露计划等 10 个数据集 | 财务 PIT 事件成员 |
| `include_intraday` / `include_industry` | True / True | 是否构造分钟样本和历史行业归属 |
| `text_body_chars` | 4000 | 每条文本载荷进入快照前的字符上限 |
| `replay_include_events` / `replay_include_text` / `replay_include_minutes` | True / True / True | 回放槽是否包含事件、文本和分钟域 |
| `replay_include_macro` / `replay_include_fundamentals` | True / True | 回放槽是否包含宏观和财务域 |
| `SNAPSHOT_DOMAIN_WORKERS`（常量） | 2 | 单次 Snapshot/Replay 构建的独立数据域并行上限；分钟域本身不拆成并发任务 |

## 2. 实验编排与验收（ExperimentConfig / AcceptanceRules / ModificationConstraints）

本章汇总实验周期、Epoch、验收规则、修改约束和 held-out 配置。

核心排程参数可由实验 CLI、控制台或参数文件设置；修改约束主要是代码级策略，生效值写入运行记录。

**排程与循环**

| 参数 | 默认 | 作用 |
|---|---:|---|
| `fold_period` | `quarter` | Fold 决策/测试周期，`week`/`month`/`quarter`/`year` 四选一（CLI `--fold-period`） |
| `epochs` | 1 | Epoch 数；每个 Epoch 前运行一次元学习 |
| `experiment_id` | 必填 | 实验唯一标识；只接受字母、数字、下划线和连字符 |
| `experiments_root` / `work_root` | `experiments` / `.runtime/sandboxes` | 持久实验目录与临时运行目录；控制台服务端管理，CLI 可覆盖 |
| `template_dir` | `configs/agent_output_template` | 新实验的初始策略模板 |
| `raw_dir` | `data/raw` | 原始数据根目录 |
| `fundamental_events_root` / `fundamental_events_status` | `data/pit/fundamental_events` / `results/data_quality/fundamental_events_status.json` | 财务 PIT 索引及其质量门禁 |
| `first_test_period` / `last_test_period` | HITL 必填并给推荐值 | development 首/末测试周期；普通 CLI 的季度入口保留 `2022Q1` 至 `2025Q4` 默认，其他周期必须显式给出 |
| `heldout_first_period` / `heldout_last_period` | 实验配置必填 | held-out 起止周期；实验开始前冻结且不得与 development 重叠 |
| `folds.MIN_REGION_TRADE_DAYS`（常量） | 2 | valid/test/held-out 区间最小交易日数（末日保留强制清仓），排程构建时校验 |
| `folds.RESEARCH_ANCHOR_TIME`（常量） | `23:59:59` | 每个区间决策快照锚点 = 区间前最后一交易日收盘（北京时间） |
| `convergence_start_epoch` | 3 | 从该 Epoch 起 Fold 进入收敛阶段提示词 |
| `meta_memory_max_epochs` | 3 | 元学习原始记忆拼接的最近 Epoch 数（`0` 关闭） |
| `meta_learning_directive` | 空 | 实验级元学习研究方向（CLI `--meta-learning-directive[-file]`；HITL 可按 Epoch 经 `directive_override` 覆盖） |
| `fold_directive`（run_fold 参数） | 空 | 研究者本 Fold 指令，注入 Fold 系统提示词并记录于 manifest/账本（HITL 控制台或审计 CLI `--fold-directive-file`） |
| `step_tree_enabled` / `disable_step_tree` | True / False | 域配置正向开关 / CLI 与参数文件反向开关；控制跨 Fold Step 产物树 |
| `record_failed_attempts` | True | Step 树记录 `[failed]` 轻量节点 |
| `use_docker` | True | 正式实验固定 Docker Sandbox（`--local-dev` 仅开发） |
| Agent Python hash seed | `0` | Local、开发 Docker 与 Formal 策略进程默认固定，保证未排序容器跨进程可复现 |
| `meta_sandbox_rebuild_enabled` / `disable_meta_sandbox_rebuild` | True / False | 域配置正向开关 / 用户入口反向开关；控制元学习派生镜像构建 |
| `meta_sandbox_rebuild_timeout_seconds` | 1800 | 派生镜像 `docker build` 超时 |
| `meta_sandbox_image_keep` | 3 | 本实验保留的派生镜像数（尽力 GC 更旧镜像） |

**验收（AcceptanceRules；CLI `--min-return`/`--min-sharpe`/`--max-drawdown`）**

| 参数 | 默认 | 作用 |
|---|---:|---|
| `min_return` | 0.0 | 验证总收益目标：低于时仍冻结但账本记录 `accept_warnings`（警告不重置 Fold） |
| `min_sharpe` | 0.0 | 验证 Sharpe 目标：低于时仍冻结但记录警告 |
| `max_drawdown` | 0.25 | 冻结允许的最大验证回撤（构造时校验有限且在 [0,1]） |
| `require_complete_validation` | True（恒定） | 冻结候选只取完整验证回测；CLI 不提供放宽入口 |

非有限指标（NaN/inf）是硬拒绝。收益统计口径属于回放合同，见 Environment 设计。

**修改约束（ModificationConstraints；具体使用方式见 Pipeline 的 Step 执行和 Agent 的正式策略产物章节）**

| 参数 | 默认 | 作用 |
|---|---:|---|
| `max_changed_files` | 8 | 相对父产物允许的变更文件数 |
| `max_diff_lines` | 600 | 总 diff 行数上限 |
| `max_code_diff_lines` | 500 | Python 代码 diff 行数上限 |
| `max_strategy_files` | 64 | `output/` 总文件数上限 |
| `max_strategy_bytes` | 1,000,000 | `output/` 总字节数上限 |
| `max_model_artifact_files` | 64 | `models/` 文件数上限 |
| `max_model_artifact_bytes` | 1 GiB | `models/` 总字节数上限 |
| `early_epoch_count` | 2 | 前 N 个 Epoch 使用宽松额度 |
| `early_max_changed_files` / `early_max_diff_lines` / `early_max_code_diff_lines` | 12 / 1200 / 1000 | 早期 Epoch 的宽松额度 |
| `is_initial_artifact` | False | 初始模板检查时设为 True，跳过相对父产物的 diff 数量限制，但仍执行总文件数、大小和只读约束 |

**组合配置字段**

| 字段 | 默认 | 作用 |
|---|---|---|
| `snapshot_config` | None（构造时由 `window_months` 生成） | 完整快照配置 |
| `step_constraints` / `regularization_constraints` | 默认修改约束 | 普通 Step 与元学习正则化产物的独立修改门禁 |
| `acceptance` / `broker_profile` | 默认验收规则 / `gjzq_dual` | 验收和 Broker 子配置 |
| `sandbox_spec` | 正式入口按宿主比例派生 | 普通 Fold 的 Sandbox 配置 |
| `meta_learning_sandbox_spec` | None（入口通常从普通 Sandbox 派生） | 元学习专用网络、环境变量和资源配置 |
| `meta_learning_managed_proxy` | 默认禁用的托管代理规格 | 元学习会话级代理生命周期配置 |

## 3. 回放执行与预算

本章汇总回放模式、决策频率、竞价行为、墙钟限制和结果预算。

以下值写入 run manifest。控制台暴露主要预算和回放旋钮；竞价总开关、各竞价时间、Timeview 和最终评估显式上限当前属于代码级配置，不在普通 CLI、HITL 表单或参数 API 中开放。

| 参数 | 默认 | 约束对象 |
|---|---:|---|
| `max_fold_minutes` | 20 min | Fold/元学习推理墙钟；回测与研究者等待分别回补 |
| `fold_deadline_at` | 运行时派生 | 会话启动时间加有效推理预算得到的绝对截止时间，无固定默认 |
| `finalize_before_deadline_seconds` | 300 s | deadline 前的收尾提示窗口（最多一次 wrap-up 提示） |
| `per_call_timeout_seconds` | 300 s | Agent 主 LLM 调用与 contract_check 单次超时 |
| `max_steps_per_fold` | 10 | 单 Fold 完整验证回测驱动的 Step 数上限 |
| `max_backtests_per_fold` | 30 | 单 Fold 回测次数上限（独立计时豁免的上限） |
| `auction_enabled` | True | 盘前/收盘集合竞价决策 tick |
| `auction_preopen_time` | `09:15` | 盲信息 tick（成交于 09:30 开盘竞价）；`None` 关闭 |
| `auction_decision_time` | `09:25` | 盲提交 tick（不暴露竞价结果，成交于首根连续 bar） |
| `auction_close_time` | `14:57` | 收盘竞价决策 tick（成交于 15:00 bar 收盘）；`None` 关闭 |
| `afterhours_decision_time` | `15:05` | 盘后固定价格 tick：可见已确认收盘价，合资格订单立即按收盘价结算；`short`/`fin_buy` 不支持；`None` 关闭 |
| `offsession_tick_minutes` | 30 min | 盘外研究 tick 间距（`0` 关闭；盘外不下单） |
| `intraday_decision_minutes` | 1 | 普通盘中 bar 上 `main(ctx)` 决策间距（分钟）；Broker 仍逐 bar 撮合，竞价/盘外 tick 恒为决策 tick |
| `execution_lag_bars` | 2 | 决策 bar 到撮合 bar 的固定滞后（按当日 bar 数收敛 `max(1, min(lag, n-1))`） |
| `decision_max_sim_minutes` | 30 min | `ctx.substep` 声明预算 `B` 的上限（超过在初始化即拒） |
| substep `budget_minutes`（Agent 声明） | `B>0`，tick 内唯一 | 实测墙钟 fail-fast + `state_dir` 写可见性 + broker action 提交时点（`B<1` 当分钟、`B>=1` 到 `ready_at`） |
| `ctx.state_dir` 单个暂存文件 | 64 MiB | substep 首次访问才复制可见状态；合并前按普通文件和大小门禁，拒绝父目录/符号链接/FIFO 换链 |
| `backtest_max_seconds_per_decision` | 1800 s | 单 `main(ctx)` tick（含 NL）真实墙钟硬上限，超限杀驱动（仅 `mode="valid"`） |
| `backtest_max_seconds_per_trading_day` | 3600 s | 单交易日累计 `main(ctx)` 计算硬上限（仅 `mode="valid"`） |
| `backtest_final_eval_max_seconds_per_decision` | 配置值 None；有效值默认 5400 s | None 时按验证单决策上限的 3 倍派生；显式正值优先，仅作 frozen_eval 防挂死兜底 |
| `backtest_final_eval_max_seconds_per_trading_day` | 配置值 None；有效值默认 10800 s | None 时按验证单日上限的 3 倍派生；显式正值优先 |
| `timeview_enabled` | True | 逐 tick 滚动 `ctx.asof_dir` 视图开关 |
| `nl_max_calls_per_decision_day` | 10 | 每回测 NL 配额 = 该值 × 决策天数 |
| `nl_max_calls_per_backtest` | None | 可选进一步收紧（取 min） |
| `nl_failure_policy` | `return_error_with_audit` | `return_error_with_audit` 返回可审计错误（结果带 `feedback`：失败原因+退化建议，策略按 status 分支降级）；`fail` 使回测失败；其他值在配置构造时拒绝 |
| NL 单次调用超时（派生） | `0.8 ×` 单决策上限 | 为决策 tick 的其余计算留余量；每轮再钳制到本决策剩余墙钟，被钳制时禁用 provider 重试 |
| NL 检索 pattern 上限（常量） | 256 字符 | RE2/grep 语义；不支持反向引用和环视，越界或不支持时返回可修复工具错误 |

有意不设固定回测总上限：总耗时上界 = 交易日数 × 单日上限（`environment_design.md` §3.7）。

## 4. Broker profile（账户、成本与信用）

本章汇总模拟 Broker 的账户、交易成本、滑点、信用和维保参数。

`BrokerProfile` 默认使用 `gjzq_dual`，每次实验同时运行普通账户和信用账户。全部字段写入 run manifest 并可据此重建；`profile_id` 标识配置档，`source`、`formula_source` 和 `maintenance_source` 保存设计文档及规则依据，不是交易旋钮。

**账户与成本**

| 参数 | 默认 | 作用 |
|---|---:|---|
| `stock_initial_cash` | 500,000 | 普通账户（long-only 现金）初始资金（元） |
| `credit_initial_cash` | 500,000 | 信用账户（担保品买卖 + 融资融券）初始资金（元）；组合权益 = 两者之和，运行中可经 `transfer` 划转 |
| `commission_bps` | 1.0 | 佣金（万一），受最低佣金约束 |
| `transfer_fee_bps` | 0.1 | 过户费（0.01‰，买卖双边） |
| `min_commission_cny` | 5.0 | 最低佣金（元/笔） |
| `stamp_duty_sell_bps_before_cutover` | 10.0 | 印花税（卖出侧，切换日前，万十） |
| `stamp_duty_sell_bps_from_cutover` | 5.0 | 印花税（切换日起，万五） |
| `broker_core.STAMP_DUTY_CUTOVER`（常量） | `20230828` | 印花税减半切换日 |
| `slippage_bps` | 5.0 | 市价 taker 滑点（限价/竞价/盘后定价成交不计滑点）；固定值、与订单规模和买卖价差无关——已记录的研究假设 |
| `LOT_SIZE`（常量） | 100 | 普通 A 股和北交所的最低买入数量；普通 A 股后续按 100 股整数倍，北交所最低 100 股后按 1 股递增 |
| `STAR_MIN_LOT_SIZE`（常量） | 200 | 科创板最低买入数量；达到最低数量后按 1 股递增 |
| `max_total_holdings` | None | 最大持仓数（默认交给 Agent 自控） |
| `max_single_name_weight` | None | 单票权重上限（默认交给 Agent 自控） |

**信用账户（融资融券）**

| 参数 | 默认 | 作用 |
|---|---:|---|
| `short_inventory_mode` | `proxy_margin_secs` | 信用账户标的池模式：`proxy_margin_secs` / `broker_inventory` / `theoretical_short`（当前同一集合近似门控担保品买入、融资买入与融券卖出） |
| `fin_margin_ratio` | 1.0 | 融资保证金比例（交易所下限 100%） |
| `slo_margin_ratio` | 1.0 | 融券保证金比例 |
| `slo_margin_ratio_private_fund` | 1.2 | 私募适用的融券保证金比例（`is_private_fund=True` 时生效） |
| `is_private_fund` | False | 选择融券保证金档位 |
| `fin_rate_annual` | 0.0835 | 融资利率（年化，研究假设，按自然日 /360 计入合约；无按月结息周期，利息在偿还/平仓时一次付清——已记录的研究假设） |
| `slo_rate_annual` | 0.085 | 融券费率（年化，研究假设，按自然日 /360 计入合约） |
| `debt_contract_term_days` | 180 | 融资/融券负债合约期限（自然日） |
| `debt_contract_auto_extend` | True | 合约到期时自动展期并记录审计事件 |
| `assure_ratio` | 0.70 | 统一担保品折算率近似（交易所上限：指数成份 ≤70%、其他 ≤65%） |
| `fin_max_quota` / `slo_max_quota` | None | 融资/融券授信额度（None = 不设额度上限） |
| `maintenance_closeout_ratio` | 1.30 | 维持担保比例平仓线（触发时只强平信用账户；普通账户不作担保） |
| `maintenance_warning_ratio` | 1.40 | 警戒参考线，仅审计记录 |
| `maintenance_withdraw_ratio` | 3.00 | 提取线：信用账户有负债时，现金划出（`transfer`）后维保比例不得低于该线 |
| `corporate_actions` | `modeled` | 除权日现金红利/送转处理（多头贷记、空头补偿；`disabled` 为研究隔离开关；配股未建模） |
| `dividend_tax_rate` | 0.0 | 多头现金红利的统一研究税率（差别化红利税未建模；空头恒按税前全额补偿） |

## 5. Agent 会话与上下文管理

本章汇总 Agent 会话、模型调用、上下文压缩和 Fold 截止时间参数。

Agent 会话的基础预算由 Pipeline 装配；控制台可覆盖压缩参数，其他字段当前为代码级配置。

| 参数 | 默认 | 作用 |
|---|---:|---|
| `max_llm_calls` | 200 | 主对话行动轮次上限（当前无 CLI 覆盖入口） |
| `max_steps` | 10 | Agent 会话内完整验证 Step 上限；由实验字段 `max_steps_per_fold` 传入 |
| `max_history_messages` | 150 | 确定性 trim 的消息条数高位上限 |
| `trim_token_threshold` | 60,000 | 确定性 trim 的估算 token 阈值 |
| `max_response_tokens` | 8,000 | 主 Agent 单次输出上限，推理 token 也计入 |
| `context_summary_max_items` / `context_summary_max_chars` | 30 / 6,000 | 确定性上下文摘要的条目数和字符上限 |
| `clear_tool_results` | True | 是否在语义压缩前原地清理较旧的大型工具结果 |
| `tool_result_keep_recent` | 8 | 原地清理时保留的最近 tool 结果条数 |
| `tool_result_clear_min_chars` | 4,000 | 只清理超过该长度的旧 tool 结果 |
| `tool_result_clear_token_threshold` | 24,000 | 触发原地清理的估算 token 阈值 |
| compact `token_threshold` | 200,000 | 语义压缩触发阈值（估算 prompt token）；控制台键为 `compact_token_threshold` |
| compact `min_messages` / `keep_recent_messages` | 20 / 12 | 压缩最小消息数 / 保留的最近原始消息 |
| compact `max_response_tokens` | 1,600 | 压缩摘要输出上限；控制台键为 `compact_max_tokens` |
| compact `max_calls` / `max_failures` | 8 / 3 | 单会话压缩调用上限 / 连续失败熔断 |
| compact `timeout_seconds` / `min_remaining_seconds` | 90 s / 60 s | 单次压缩超时 / 为后续主 LLM 调用保留的最小剩余时间 |
| `context_compaction` | 默认压缩配置 | Agent 会话持有的完整压缩子配置 |

主对话与 NL 默认使用 provider 深度推理配置（DeepSeek 映射为 thinking + `reasoning_effort=max`）；compact 默认低成本无 thinking 模型（`environment_design.md` §2.4；CLI `--reasoning-effort`/`--no-thinking` 可覆盖）。

## 6. Sandbox 资源与工具预算

本章汇总 Sandbox 的计算资源、网络、环境变量、工具和服务预算。

Sandbox 参数写入 run manifest。普通字段直接实例化时有名义默认；正式实验使用宿主资源比例派生 CPU 和内存。

| 参数 | 默认 | 作用 |
|---|---:|---|
| `image` | `autotrade-sandbox:latest` | 基础镜像逻辑标签；元学习可派生扩展镜像，最终可复现身份以 run manifest 记录的 image ID / digest 为准 |
| `user` | `agent` | 容器内非 root 执行用户 |
| `network` | `none` | 普通 Fold 断网；元学习 spec 默认 `bridge` |
| `cpus` / `memory` | 名义 4.0 / 8g；正式实验为宿主 CPU/RAM 的 10% | 容器资源限制；正式派生值各自至少 1 CPU / 1 GiB |
| `pids_limit` | 512 | 容器进程数上限 |
| `gpu` / `gpu_count` / `gpu_name_filter` | `auto` / 1 / `L20` | `gpu_count` 是创建实验时每个 Sandbox 的默认 GPU 数量，范围 1..4；表单显示当前各卡空闲显存，创建和容器启动时分别检查并自动选卡 |
| `env_passthrough` / `env_aliases` | 空 / 空 | 允许透传的环境变量名及容器别名；普通 Fold 为空，元学习按显式配置扩展 |
| `add_host_gateway` / `host_gateway_ip` | False / None | 是否向 bridge 容器注入宿主网关及运行时探测到的地址 |
| shell `timeout_seconds` | 默认 120，上限 1800 | 单条 shell 命令超时（容器内 `timeout` 整组杀） |
| shell `max_output_chars` | 20,000 | 单次内联输出预算，超出落盘返回路径 |
| shell stdout/stderr 捕获上限 | 各 200,000 字符 | 超出上限的尾部即使落盘也不再保留，并在结果中标记截断 |

**文件、检索与联网工具预算**

| 工具/参数 | 默认或上限 | 作用 |
|---|---:|---|
| `grep` / `glob` 默认结果数 | 250 / 100 | 单次默认返回条目数；请求上限分别为 1,000 / 1,000 |
| `read` 默认/最大行数 | 2,000 / 5,000 | 分页读取文本文件 |
| `rg` 宿主搜索超时 | 20 s | 受控搜索子进程的硬超时 |
| 文件工具统一内联上限 | 20,000 字符 | 读取或搜索结果超过后截断或分页 |
| `write_file` / `edit_file` 单文件上限 | 200,000 字符 | 单次写入或编辑后的文本大小上限 |
| NL 提示文件上限 | 8,000 字符 | 策略提交给 NL 的扩展提示文本上限 |
| `web_search` 结果数 | 默认 5，上限 10 | 单次元学习搜索返回条目数 |
| `web_fetch` 工具输出 | 默认 12,000，上限 30,000 字符 | 返回给 Agent 的网页文本预算 |
| `web_fetch` 宿主抓取 | 30 s、最多 5 次重定向、响应体 5 MiB、提取文本 100,000 字符 | 公开网页读取的网络与内容上限 |
| Explore | 120 s、6 个工具轮、6,000 输出 token | 单次探查委托的预算；只读是 Prompt 约定，不是独立权限层 |
| NL | 3 个工具轮、3,000 输出 token | 单次 NL 分析预算；检索默认返回 5 条，可请求 1 至 20 条 |

## 7. 数据层任务参数

本章汇总数据下载、更新、审计、限频、分页和刷新节点参数。

数据任务的有效值由调度配置和各入口参数共同决定；调度项覆盖全局默认。

**更新与刷新**

| 参数 | 默认 | 作用 |
|---|---:|---|
| `default_start_date` | `20200101` | 定时数据任务未覆盖起点时使用的历史下界 |
| `default_raw_dir` / `default_pit_root` | `data/raw` / `data/pit` | 调度层的 raw 与 PIT 默认根目录 |
| 晚间更新回看窗口 | 30 天 | `update` 从 start 扫到 end，非只更新当天 |
| 财务 PIT 构建回看窗口 | 120 天 | 夜间财务事件索引增量构建范围 |
| 盘前回补回看窗口 | 榜单 0 天、短文本 2 天 | 按任务终点向前计算回补起点；两融任务直接按目标交易日执行 |
| 财务强刷窗口 | 最近 6 个报告期 / 3 个公告月 | 财务与基本面日常刷新范围 |
| 分钟线强刷 | 最近 1 个自然日 | 每晚补最近窗口 |
| 交易日历前瞻 | `end_date + 7` 天 | 供次日盘前判断 |
| 数据任务锁 `lock_wait_seconds` | 全局 900 s | `flock` 排它锁等待上限；revision/PIT 作业覆盖为 1800 s，盘前回补为 180 s，09:20 审计为 240 s |
| 默认请求间隔 / 请求超时 | 0.22 s / 120 s | 定时更新传给数据客户端的基础调用节奏；具体接口可使用更慢间隔 |
| `skip_if_already_ok` | True（当前定时作业） | 同一作业和终点已有成功状态时跳过；`force_run` 可显式重跑 |
| 竞价捕获等待 / 轮询 | 170 s / 10 s | 09:27 起等待接口返回当日完整结果；09:31 重试，23:20 强制复核 |
| 竞价稳定与完整性 | 连续 2 次；至少 1000 行 | 两次业务内容一致才发布 |
| 竞价历史行数下限 | 前一分区的 99.5%，且最多少 10 行 | 防止非空但缩水的响应覆盖完整分区 |
| 竞价单次请求超时 | 15 s | 每次 `stk_auction` 请求上限 |
| `end_date_offset_days` / `end_date_mode` | 按作业配置 | 从北京时间当前日期推导任务终点，并可钳制到最近 SSE 交易日 |
| `cn_nightly_full_audit.event_flow_end_extra_offset_days` | 1 天 | 02:30 的事件/资金状态统一避开次晨才发布的两融数据；09:20 再审计上一交易日 |
| `revision_monitor.sentinel_sample_size` | 12 | 修正哨兵每日抽样分区数 |
| `revision_monitor.sentinel_datasets` | daily / adj_factor / daily_basic / stk_limit / suspend_d / limit_list_d | 哨兵监控数据集（单一配置来源） |

**限频与分页（TuShare 10000 积分档）**

| 参数 | 默认 | 作用 |
|---|---:|---|
| 常规/特色接口频次 | 500 / 300 次每分钟 | 官方限频 |
| 文本接口频次 | 新闻 400、公告 500、政策 500 次每分钟 | 独立文本权限 |
| 请求最小间隔 | 分钟线与混合文本 ≥0.22 s；`namechange` 0.50 s | 脚本保守间隔 |
| 文本单页上限 | `anns_d` 2000、`major_news` 400、`npr` 500、`research_report` 1000、`report_rc` 3000、`news` 1500 | 单页钳制上限 |
| `stk_mins` 单页上限 | 8000 | 分钟线分页 |
| 文本时间合理性窗口 | -1 ~ +3 天 | `rec_time`/`create_time` 相对日期基准的可信窗口，超出即保守回退（`data_documentation.md` §1.7） |

**下载、更新与审计入口**

| 参数组 | 默认/选择 | 作用 |
|---|---|---|
| 下载层级 `tier` | 必填；从受支持数据域中选择 | 选择下载域；可用 `datasets`、日期范围和代码列表进一步收窄 |
| `start_date` / `end_date` | 入口相关；历史下载常从 2010 或 2020 起，结束默认当天 | 下载、更新或审计范围；定时任务可由环境变量或调度配置覆盖起点 |
| `force` / `force_run` | False | 忽略已有分区或已成功任务状态重新执行；仍受空覆盖和质量门禁约束 |
| `page_limit` | None；文本和分钟接口再按官方上限钳制 | 请求页大小 |
| `max_retries` / `retry_delay_seconds` | 3 / 5 s | 分钟窗口和更新任务的失败重试 |
| `allow_empty_revision_overwrite` | False | 是否允许源端空响应覆盖已有非空历史分区；默认拒绝 |
| revision `sample_size` / `seed` | 12 / 结束日期 | 历史修正哨兵的确定性抽样规模和种子 |
| 分钟完整性 `min_rows_per_day` / `allow_missing_codes` | 0 / 0 | 新分区最少行数和允许缺失代码数；已有分区允许 50 个代码差异用于增量检查 |
| `allow_validation_warnings` | False | 是否允许带 warning 的分钟层发布；正式任务默认不放宽 |
| 解禁 rescue `max_ann_rescue_days` / `max_rescue_calls` | 5 / 50,000 | 公告日触顶拆分的安全上限 |

**刷新节点（`REFRESH_NODES`，Timeview 可见性门禁；`data_documentation.md` §3.3）**

| 节点 | 启动 → 就绪 |
|---|---|
| `cn_evening_full` | 23:35 → 次日 03:05（210 分钟保守边界） |
| `cn_nightly_pit_event_build` | 03:35 → 约 03:50 |
| `cn_preopen_board_backfill_0850` | 08:50 → 约 08:55 |
| `cn_preopen_text_backfill_0855` | 08:55 → 约 09:00 |
| `cn_preopen_margin_secs_backfill_0903` / `_retry_0913` | 09:03 / 09:13 → 约 09:05 / 09:15 |
| `cn_preopen_margin_backfill_0905` / `_retry_0915` | 09:05 / 09:15 → 约 09:07 / 09:17 |
| `stk_auction` | 分区实际完整落地时间（通常 09:27–09:29） |

## 8. 报告与其他常量

本章汇总报告基准、统计口径和少量跨模块固定常量。

| 参数 | 默认 | 定义位置与作用 |
|---|---:|---|
| 报告 benchmark | `000300.SH` | 使用回放时冻结的基准块，不在渲染时读取 raw；有成绩周期缺块时报告 warning |
| 深圳开盘竞价代理校正倍率 | `00*.SZ` ×0.76、`30*.SZ` ×0.58 | 仅用于2025-01-16以前或精确接口缺行时的09:30量额代理；覆盖期内使用 `stk_auction` 原值 |
| 财务事件可见时点 | 公告日 18:00 | 公告日优先最终公告日，再取普通公告日 |
| 元学习 `web_search` 引擎 | Tavily + Semantic Scholar | run manifest `web_search_engines`；三视角非空检索后才可 `done`（`pipeline_design.md` §3.2） |
| QMT 本金上限（草案） | `CQ_MAX_PRINCIPAL` 环境变量 | 未设置时用账户总资产口径（`deployment_documentation.md` §6.4） |

## 9. HITL、模型、联网与控制台

本章汇总交互式实验、模型选择、元学习联网、控制台门控和分析服务参数。

交互式创建参数持久化到 `params.json`，worker 据此重建运行配置。控制台隐藏的运维参数仍可通过 API 或参数文件设置。

**模型与上下文入口**

| 参数 | 默认 | 作用 |
|---|---:|---|
| `model` / `nl_model` / `compact_model` | `deepseek-v4-pro` / `deepseek-v4-flash` / `deepseek-v4-flash` | 主 Agent、NL 和上下文压缩模型 |
| `reasoning_effort` | `max` | Agent 与 NL 开启 thinking 时的推理强度 |
| `no_thinking` | False | 关闭 Agent 与 NL 的 provider 推理模式 |
| `disable_context_compact` | False | 禁用语义压缩；确定性裁剪仍作为上下文保护 |
| `compact_token_threshold` / `compact_keep_recent_messages` | 200,000 / 12 | 压缩触发阈值与保留最近原文数 |
| `compact_max_tokens` / `compact_max_calls` | 1,600 / 8 | 压缩摘要输出上限与单会话调用上限 |

**元学习网络、凭据与派生环境**

| 参数 | 默认 | 作用 |
|---|---:|---|
| `web_search_engines` | Tavily、Semantic Scholar | 元学习可用搜索引擎；普通 Fold 不提供联网搜索 |
| `tavily_api_key_env` / `semantic_scholar_api_key_env` | `TAVILY_API_KEY` / `SEMANTIC_SCHOLAR_API_KEY` | 从宿主读取搜索凭据的变量名；不记录变量值 |
| `meta_learning_network` | `bridge` | 元学习容器网络：`bridge` / `host` / `none` |
| `meta_learning_env` | 空 | 研究者显式追加透传的宿主环境变量名；只记录名称 |
| 默认元学习凭据候选 | `GITHUB_TOKEN`、`HF_TOKEN` | 当前代码会尝试从宿主透传这两个变量；普通 Fold 不透传 |
| `meta_learning_add_host_gateway` | False | bridge 模式下显式注入宿主网关；托管代理启用时也会自动需要网关 |
| `disable_meta_learning_host_proxy` | False | 禁用托管代理别名和相关宿主代理通路 |
| `disable_meta_learning_managed_proxy` | False | 即使检测到配置也不启动会话级 XRay |
| `meta_learning_xray_bin` | None（回退环境变量或 `xray`） | XRay 可执行文件路径 |
| `meta_learning_xray_startup_timeout` | 15 s | 托管 XRay 端口就绪超时 |
| `disable_meta_sandbox_rebuild` | False | 忽略元学习的派生镜像请求 |

| 参数 | 默认 | 作用 |
|---|---:|---|
| `initial_control_mode` | `step` | 新实验初始运行模式：`step` 逐会话并逐次验证批准 / `manual` 仅逐会话批准 / `auto` 自动连续执行 |
| `analysis_enabled` | True | Fold 完成后自动生成 LLM 策略分析（仅使用验证回放证据） |
| `analysis_model` | `deepseek-v4-pro` | 策略分析模型（`reasoning_effort` 固定 `high`，超时 900 s） |
| worker `--poll-seconds` | 2.0 | 门控等待时 control.json 轮询间隔 |
| status 心跳间隔（常量） | 3.0 s | worker 心跳 + 实时 run/trace 路径发现 |
| `MAX_RUNNING_EXPERIMENTS`（常量） | 5 | 控制台并行运行实验数上限 |
| 控制台绑定 | Unix socket（生产） | `.runtime/webui/console.sock` 位于 0700 服务目录；`--host/--port`（默认 38888）仅用于显式本地调试 |
| 控制台模型选项 | v4-pro / v4-flash | 创建表单暴露的模型集合 |
| 控制台周期选择器 | 交易日历∩数据覆盖 | 四个周期参数按 Fold 周期从 SSE 日历枚举完整可回测周期（再按 daily/分钟线分区覆盖裁剪）并给推荐默认；无日历时退化为文本输入 |
| 控制台可调参数扩展 | 见 §2/§3/§4 各默认 | 表单另暴露 Step/回测/NL 预算、回放执行旋钮、Broker 资金/费用/持仓上限、元学习记忆与派生镜像旋钮，多数收在“高级参数”折叠区 |
| 系统提示词预览 | `prompt-preview` | 批准前装配含 Taste、指令和等价占位事实的提示词；不含 Sandbox 创建后才能生成的完整运行事实 JSON，也不暴露测试排程 |
| 系统提示词覆盖 / Fold 重跑 | `set_prompt_override` / `rerun_fold` | Fold 级整体覆盖（运行时原样使用、manifest 记录）；重跑仅限最新已记录 Fold，产物带 `__r<id>` 标签、held-out 自动重放 |
| 提前收官 / 回滚 | `skip_to_heldout`（可 `cancel_`） / `rollback_fold` | 跳过剩余 Fold 直接进 held-out（需 ≥1 冻结 Fold）；回滚到任一已记录 Fold（其后记录移除、账本备份 `experiment_ledger.rollback_*.jsonl`、冻结产物归档 `_archive/`，worker 需已停止） |
| `inherit_from` | `""` | 创建时继承另一实验最新冻结 Fold 的 Agent Output（拷贝+哈希校验到 `strategy_artifacts/_inherited/`，源删除不影响） |
| 实验默认 / 逐 Fold GPU 分配 | 创建参数 `gpu_count`（默认 1，范围 1..4）/ `set_gpu_count` + `GET /api/gpus` | 创建参数作用于元学习、所有 Fold 和 held-out；创建表单展示实时 GPU 空闲显存，Fold 门控处可覆盖单个 Fold，运行时再次按空闲显存自动选卡 |
| 逐 Step 门控 | `set_step_gate` / `approve_step` | 每次正式验证回测后挂起等待批准；放行可带 Step 级指令（注入该次回测观察）；等待不消耗 Fold 预算 |
| Step 级回滚 | `set_parent_override` + `GET steps` / `steps/<node>/source.zip` | 把 Step 产物树已验证节点设为某 Fold 会话父产物起点（空值清除；仅限不晚于目标会话的节点，防未来验证信息回流）；树端点回映 `fold_ref`→真实 Fold、标注冻结节点与当前指针；节点 ZIP 含完整 output/models 源代码与该次验证明细 |
| 收益曲线展示 | `equity` / `folds/<e>/<f>/equity` | 服务端从冻结日收益计算累计收益和回撤，前端只绘制返回数组 |
| 风格验证端点 | `style?run_id&prefix` | 原样返回该 run 落盘的 `results/style_<prefix>.json` rollup（回放时由宿主从冻结数据计算，Web 层零计算）。逐窗口完整版在 `results/<窗口>/style_analysis.json`（验证窗口 Agent 可读），紧凑 `benchmark` 块（基准/超额收益、β、市值倾斜）进回测 summary 与账本 Step 摘要 |
| 策略产物下载 | 仅单一 ZIP（`strategy.zip`） | output+models 打包；不提供逐文件浏览/下载端点 |
| `analysis_max_tokens` | 6000 | 单次策略分析基础输出配额；length 停止时以 `max(16000, 2 × 基础值)` 重试一次 |
| trace 统计/下载 | `trace/stats` / `trace/download` | 按事件类型聚合的实时运行统计（含回测累计墙钟，用于倒计时回补显示）与原始 JSONL 下载 |
| 界面缩放 | 90%–150%（默认 100%） | 顶栏缩放选择器，按浏览器 localStorage 记忆（跨设备渲染差异的本地补偿） |
| `local_dev` | False | 仅测试和开发时改用本地执行器；正式实验保持 Docker |
| 控制台隐藏参数 | 路径、凭据变量名、`local_dev`、显式透传变量与代理二进制路径 | 不进普通表单；控制台 API 拒绝这些键，仅 worker 侧 `params.json`（运维通道）可设 |
| 分析内容预算（常量） | 单文件 20k / 总 60k 字符 | `fold_analysis.read_strategy_files` 的策略代码内联预算 |
| HITL work root | `.runtime/sandboxes/<experiment_id>` | 控制台创建实验的专属 sandbox 根（删除实验时一并清理） |

## 10. 构造期校验与失败

本章汇总各参数域在配置构造或运行启动前执行的校验和失败时点。

| 参数域 | 已实现校验 | 失败时点 |
|---|---|---|
| 实验预算 | Epoch、窗口、Fold/回测次数、执行滞后、盘中决策间距、验证回测墙钟和 NL 日配额必须为有限正数 | 实验配置构造时 |
| 可关闭的预算 | 收尾窗口、盘外 tick 间距和元学习记忆 Epoch 数必须为有限非负数 | 实验配置构造时 |
| 可选上限 | substep 上限、最终评估墙钟和单回测 NL 配额设置后必须为有限正数 | 实验配置构造时 |
| 验收阈值 | 三项指标阈值必须有限；最大回撤阈值在 [0,1]；实际指标出现 NaN/inf 时硬拒绝 | 配置构造或候选验收时 |
| Broker 资金与费用 | 初始资金非负且合计为正；费用、滑点和利率非负；保证金比例为正；额度非负 | Broker profile 构造时 |
| Broker 风控 | 红利税率在 [0,1)，折算率在 (0,1]，维保线满足 `0 < 平仓线 <= 警戒线 <= 提取线`；枚举值必须受支持 | Broker profile 构造时 |
| 上下文压缩 | token、消息和输出上限为正；失败数、调用数和剩余时间非负；压缩超时为正 | 压缩配置构造时 |
| Sandbox | GPU 数量为正；显式宿主网关地址不得为空字符串 | Sandbox 配置构造时 |
| HITL 参数 | 未知键直接拒绝；周期、类型、枚举和表单范围在创建阶段校验；逐 Fold GPU 表单范围为 1 至 4，但底层 Sandbox 只要求正数 | API/控制台创建或会话门控时 |

并非所有建议范围都有代码硬校验。例如收敛起始 Epoch、派生镜像保留数、镜像构建超时和债务期限仍主要依赖调用方给出合理值。运行记录应保留实际值，审计时不能把文档建议误当作已执行的强制门禁。
