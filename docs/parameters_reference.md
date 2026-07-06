# 参数速查

本文档汇总五份 living docs 引用的全部参数、超参数与关键常量：默认值、定义位置和作用。代码是唯一事实源（默认值已逐项对照当前代码核验）；本文档是派生速查，修改任何旋钮时应在同一工作项内同步本表和相应权威文档章节。运行期实际生效值以 run manifest / snapshot manifest 记录为准。

**相关边界**

- 快照窗口、执行/回放与 Broker 语义的权威定义见 `docs/environment_design.md`。
- Fold/Epoch 排程、验收与冻结语义见 `docs/pipeline_design.md`。
- Agent 合同中各预算的使用方式见 `docs/agent_design.md`。
- 数据层任务与限频的权威定义见 `docs/data_documentation.md`。
- QMT 实盘草案参数见 `docs/QMT_documentation.md`。

**导航**

- [1. 快照窗口（SnapshotConfig）](#1-快照窗口snapshotconfig)
- [2. 实验编排与验收（ExperimentConfig / AcceptanceRules / ModificationConstraints）](#2-实验编排与验收experimentconfig--acceptancerules--modificationconstraints)
- [3. 回放执行与预算](#3-回放执行与预算)
- [4. Broker profile（账户、成本与信用）](#4-broker-profile账户成本与信用)
- [5. Agent 会话与上下文管理](#5-agent-会话与上下文管理)
- [6. Sandbox 资源与工具预算](#6-sandbox-资源与工具预算)
- [7. 数据层任务参数](#7-数据层任务参数)
- [8. 报告与其他常量](#8-报告与其他常量)

## 1. 快照窗口（SnapshotConfig）

定义：`src/autotrade/environment/snapshot.py`；权威文档 `environment_design.md` §1.1。CLI 覆盖：`--window-months`、`--daily-window-months` 等（未传时各域回退基础窗口）。

| 参数 | 默认 | 作用 |
|---|---:|---|
| `window_months` | 21 | 各数据域月窗口的基础回退值 |
| `daily_window_months` | None（回退基础） | `daily` 域月窗口 |
| `fundamentals_window_months` | None（回退基础） | `fundamentals` 域月窗口（可见披露口径） |
| `events_window_months` | None（回退基础） | `events` 域月窗口 |
| `macro_window_months` | None（回退基础） | `macro` 域月窗口 |
| `text_window_months` | None（回退基础） | `text` 域月窗口 |
| `intraday_trade_days` | 21 | 决策输入快照的分钟线交易日窗口（回放槽分钟窗口由 Fold 周期决定，与此无关） |

`universe` 域不使用月窗口，按决策日在市口径生成（`environment_design.md` §1.1）。

## 2. 实验编排与验收（ExperimentConfig / AcceptanceRules / ModificationConstraints）

定义：`src/autotrade/pipelines/config.py`、`src/autotrade/environment/artifacts.py`、`src/autotrade/pipelines/folds.py`；权威文档 `pipeline_design.md` §1–§2。

**排程与循环**

| 参数 | 默认 | 作用 |
|---|---:|---|
| `fold_period` | `quarter` | Fold 决策/测试周期，`week`/`month`/`quarter`/`year` 四选一（CLI `--fold-period`） |
| `epochs` | 1 | Epoch 数；每个 Epoch 前运行一次元学习 |
| `first_test_period` / `last_test_period` | 实验必填 | development 首/末测试周期 |
| `heldout_first_period` / `heldout_last_period` | 实验必填 | held-out 起止周期；实验开始前冻结、不得与 development 重叠 |
| `folds.MIN_REGION_TRADE_DAYS`（常量） | 2 | valid/test/held-out 区间最小交易日数（末日保留强制清仓），排程构建时校验 |
| `folds.RESEARCH_ANCHOR_TIME`（常量） | `23:59:59` | 每个区间决策快照锚点 = 区间前最后一交易日收盘（北京时间） |
| `convergence_start_epoch` | 3 | 从该 Epoch 起 Fold 进入收敛阶段提示词 |
| `meta_memory_max_epochs` | 3 | 元学习原始记忆拼接的最近 Epoch 数（`0` 关闭） |
| `meta_learning_directive` | 空 | 实验级元学习研究方向（CLI `--meta-learning-directive[-file]`） |
| `step_tree_enabled` | True | 跨 Fold Step 产物树 |
| `record_failed_attempts` | True | Step 树记录 `[failed]` 轻量节点 |
| `use_docker` | True | 正式实验固定 Docker Sandbox（`--local-dev` 仅开发） |
| `meta_sandbox_rebuild_enabled` | True | 元学习 `sandbox_environment.json` 触发派生镜像构建 |
| `meta_sandbox_rebuild_timeout_seconds` | 1800 | 派生镜像 `docker build` 超时 |
| `meta_sandbox_image_keep` | 3 | 本实验保留的派生镜像数（尽力 GC 更旧镜像） |

**验收（AcceptanceRules；CLI `--min-return`/`--min-sharpe`/`--max-drawdown`）**

| 参数 | 默认 | 作用 |
|---|---:|---|
| `min_return` | 0.0 | 冻结所需最低验证总收益 |
| `min_sharpe` | 0.0 | 冻结所需最低验证 Sharpe |
| `max_drawdown` | 0.25 | 冻结允许的最大验证回撤 |
| `require_complete_validation` | True（恒定） | 冻结候选只取完整验证回测；CLI 不提供放宽入口 |

**修改约束（ModificationConstraints；`pipeline_design.md` §2.1、`agent_design.md` §4.1）**

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

## 3. 回放执行与预算

定义：`src/autotrade/pipelines/config.py`（逐项写入 run manifest）；回放限时语义见 `environment_design.md` §3.5。

| 参数 | 默认 | 约束对象 |
|---|---:|---|
| `max_fold_minutes` / `fold_deadline_at` | 60 min | Fold 推理墙钟（回测耗时回补排除） |
| `finalize_before_deadline_seconds` | 300 s | deadline 前的收尾提示窗口（最多一次 wrap-up 提示） |
| `per_call_timeout_seconds` | 300 s | Agent 主 LLM 调用与 contract_check 单次超时 |
| `max_steps_per_fold` | 10 | 单 Fold 完整验证回测驱动的 Step 数上限 |
| `max_backtests_per_fold` | 30 | 单 Fold 回测次数上限（独立计时豁免的上限） |
| `auction_enabled` | True | 盘前/收盘集合竞价决策 tick |
| `auction_preopen_time` | `09:15` | 盲信息 tick（成交于 09:30 开盘竞价）；`None` 关闭 |
| `auction_decision_time` | `09:25` | 撮合开盘 tick（成交于首根连续 bar） |
| `auction_close_time` | `14:57` | 收盘竞价决策 tick（成交于 15:00 bar 收盘）；`None` 关闭 |
| `offsession_tick_minutes` | 15 min | 盘外研究 tick 间距（`0` 关闭；盘外不下单） |
| `execution_lag_bars` | 2 | 决策 bar 到撮合 bar 的固定滞后（按当日 bar 数收敛 `max(1, min(lag, n-1))`） |
| `decision_max_sim_minutes` | 60 min | `ctx.substep` 声明预算 `B` 的上限（超过在初始化即拒） |
| substep `budget_minutes`（Agent 声明） | `B>0`，tick 内唯一 | 实测墙钟 fail-fast + `state_dir` 写可见性 + broker action 提交时点（`B<1` 当分钟、`B>=1` 到 `ready_at`） |
| `backtest_max_seconds_per_decision` | 300 s | 单 `main(ctx)` tick（含 NL）真实墙钟硬上限，超限杀驱动（仅 `mode="valid"`） |
| `backtest_max_seconds_per_trading_day` | 900 s | 单交易日累计 `main(ctx)` 计算硬上限（仅 `mode="valid"`） |
| `backtest_final_eval_max_seconds_per_decision` | 900 s | 最终评估（frozen_eval）单决策防挂死兜底，非接受门槛 |
| `backtest_final_eval_max_seconds_per_trading_day` | 3000 s | 最终评估单交易日防挂死兜底 |
| `timeview_enabled` | True | 逐 tick 滚动 `ctx.asof_dir` 视图开关 |
| `nl_max_calls_per_decision_day` | 10 | 每回测 NL 配额 = 该值 × 决策天数 |
| `nl_max_calls_per_backtest` | None | 可选进一步收紧（取 min） |
| `nl_failure_policy` | `return_error_with_audit` | NL 失败时对策略的返回策略 |
| NL 单次调用超时（派生） | `0.8 ×` 单决策上限 | 为决策 tick 的其余计算留余量（`tools/backtest.py`） |

有意不设固定回测总上限：总耗时上界 = 交易日数 × 单日上限（`environment_design.md` §3.5）。

## 4. Broker profile（账户、成本与信用）

定义：`src/autotrade/environment/broker.py`（`BrokerProfile`，默认 `gjzq_dual`；每次实验固定运行普通 + 信用双账户）与 `broker_core.py` 常量；权威文档 `environment_design.md` §3.2。全部字段经 `to_record()` 写入 run manifest 并回读重建。

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
| `slippage_bps` | 5.0 | 市价 taker 滑点（限价/竞价成交不计滑点） |
| `broker_core.LOT_SIZE`（常量） | 100 | 普通 A 股一手股数；科创板为 200 股起、之后 1 股递增 |
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
| `fin_rate_annual` | 0.0835 | 融资利率（年化，研究假设，按自然日 /360 计入合约） |
| `slo_rate_annual` | 0.085 | 融券费率（年化，研究假设，按自然日 /360 计入合约） |
| `debt_contract_term_days` | 180 | 融资/融券负债合约期限（自然日） |
| `debt_contract_auto_extend` | True | 合约到期时自动展期并记录审计事件 |
| `assure_ratio` | 0.70 | 平坦担保品折算率近似（交易所上限：指数成份 ≤70%、其他 ≤65%） |
| `fin_max_quota` / `slo_max_quota` | None | 融资/融券授信额度（None = 不设额度上限） |
| `maintenance_closeout_ratio` | 1.30 | 维持担保比例平仓线（触发时只强平信用账户；普通账户不作担保） |
| `maintenance_warning_ratio` | 1.40 | 警戒参考线，仅审计记录 |
| `maintenance_withdraw_ratio` | 3.00 | 提取线：信用账户有负债时，现金划出（`transfer`）后维保比例不得低于该线 |
| `short_corporate_actions` | `disabled` | 空头分红/配股暂不建模 |

## 5. Agent 会话与上下文管理

定义：`src/autotrade/agent/runner.py`（`AgentSessionConfig`）、`src/autotrade/agent/compact.py`（`CompactionConfig`）；权威文档 `environment_design.md` §2.2。

| 参数 | 默认 | 作用 |
|---|---:|---|
| `max_llm_calls` | 200 | 主对话行动轮次上限（当前无 CLI 覆盖入口） |
| `max_history_messages` | 150 | 确定性 trim 的消息条数高位上限 |
| `trim_token_threshold` | 60,000 | 确定性 trim 的估算 token 阈值 |
| `tool_result_keep_recent` | 8 | 原地清理时保留的最近 tool 结果条数 |
| `tool_result_clear_min_chars` | 4,000 | 只清理超过该长度的旧 tool 结果 |
| `tool_result_clear_token_threshold` | 24,000 | 触发原地清理的估算 token 阈值 |
| compact `token_threshold` | 200,000 | 语义压缩触发阈值（估算 prompt token） |
| compact `min_messages` / `keep_recent_messages` | 20 / 12 | 压缩最小消息数 / 保留的最近原始消息 |
| compact `max_calls` / `max_failures` | 8 / 3 | 单会话压缩调用上限 / 连续失败熔断 |
| compact `min_remaining_seconds` | 60 s | 为后续主 LLM 调用保留的最小剩余时间 |

主对话与 NL 默认使用 provider 深度推理配置（DeepSeek 映射为 thinking + `reasoning_effort=max`）；compact 默认低成本无 thinking 模型（`environment_design.md` §2.4；CLI `--reasoning-effort`/`--no-thinking` 可覆盖）。

## 6. Sandbox 资源与工具预算

定义：`src/autotrade/environment/sandbox.py`（`SandboxSpec`）、`src/autotrade/environment/tools/shell.py`；权威文档 `environment_design.md` §2.1–§2.2。

| 参数 | 默认 | 作用 |
|---|---:|---|
| `image` | `autotrade-sandbox:latest` | 基础镜像（元学习可派生扩展镜像） |
| `user` | `agent` | 容器内非 root 执行用户 |
| `network` | `none` | 普通 Fold 断网；元学习 spec 默认 `bridge` |
| `cpus` / `memory` | 4.0 / 8g | 容器资源限制（正式实验按 host fraction 构造） |
| `pids_limit` | 512 | 容器进程数上限 |
| `gpu` / `gpu_count` / `gpu_name_filter` | `auto` / 1 / `L20` | 按空闲显存自动分配 GPU |
| shell `timeout_seconds` | 默认 120，上限 1800 | 单条 shell 命令超时（容器内 `timeout` 整组杀） |
| shell `max_output_chars` | 20,000 | 单次内联输出预算，超出落盘返回路径 |

## 7. 数据层任务参数

定义：`configs/tushare_update_schedule.json`、`src/autotrade/data_sources/tushare/`；权威文档 `data_documentation.md` §2。

**更新与刷新**

| 参数 | 默认 | 作用 |
|---|---:|---|
| cron 回看窗口 | 30 天 | `update` 从 start 扫到 end，非只更新当天 |
| 财务强刷窗口 | 最近 6 个报告期 / 3 个公告月 | 财务与基本面日常刷新范围 |
| 分钟线强刷 | 最近 1 个自然日 | 每晚补最近窗口 |
| 交易日历前瞻 | `end_date + 7` 天 | 供次日盘前判断 |
| 宏观/全球窗口下界 | `20200101` | range 型数据保留窗口起点 |
| `revision_monitor.sentinel_sample_size` | 12 | 修正哨兵每日抽样分区数 |
| `revision_monitor.sentinel_datasets` | daily / adj_factor / daily_basic / stk_limit / suspend_d / limit_list_d | 哨兵监控数据集（单一配置来源） |

**限频与分页（TuShare 10000 积分档）**

| 参数 | 默认 | 作用 |
|---|---:|---|
| 常规/特色接口频次 | 500 / 300 次每分钟 | 官方限频 |
| 文本接口频次 | 新闻 400、公告 500、政策 500 次每分钟 | 独立文本权限 |
| 请求最小间隔 | 分钟线与混合文本 ≥0.22 s；`namechange` 0.50 s | 脚本保守间隔 |
| 文本单页上限 | `anns_d` 2000、`major_news` 400、`npr` 500、`research_report` 1000、`report_rc` 3000、`news` 1500 | clamp 值 |
| `stk_mins` 单页上限 | 8000 | 分钟线分页 |
| 文本时间合理性窗口 | -1 ~ +3 天 | `rec_time`/`create_time` 相对日期基准的可信窗口，超出即保守回退（`data_documentation.md` §1.7） |

**刷新节点（`REFRESH_NODES`，Timeview 可见性门禁；`data_documentation.md` §3.3）**

| 节点 | 启动 → 就绪 |
|---|---|
| `cn_evening_full` | 23:35 → 次日约 02:05 |
| `cn_nightly_pit_event_build` | 03:35 → 约 03:50 |
| `cn_preopen_board_backfill_0850` | 08:50 → 约 08:55 |
| `cn_preopen_text_backfill_0855` | 08:55 → 约 09:00 |
| `cn_preopen_margin_secs_backfill_0903` / `_retry_0913` | 09:03 / 09:13 → 约 09:05 / 09:15 |
| `cn_preopen_margin_backfill_0905` / `_retry_0915` | 09:05 / 09:15 → 约 09:07 / 09:17 |

## 8. 报告与其他常量

| 参数 | 默认 | 定义位置与作用 |
|---|---:|---|
| 报告 benchmark | `000300.SH` | `pipelines/reporting.py`；缺基准数据时 summary 标 warning（`pipeline_design.md` §4.2） |
| 深圳开盘竞价校正倍率 | `00*.SZ` ×0.76、`30*.SZ` ×0.58 | `environment/features/`（auction 校正）；仅开盘竞价近似输入（`environment_design.md` §1.4） |
| 财务事件可见时点 | 公告日 18:00 | `features/fundamental_events.py`；公告日优先 `f_ann_date` 再 `ann_date`（`environment_design.md` §1.3） |
| 元学习 `web_search` 引擎 | Tavily + Semantic Scholar | run manifest `web_search_engines`；三视角非空检索后才可 `done`（`pipeline_design.md` §3.1） |
| QMT 执行器轮询间隔（草案） | `3nSecond` | `QMT_documentation.md` §2.2；实盘上线前随执行器实现冻结 |
| QMT 本金上限（草案） | `CQ_MAX_PRINCIPAL` 环境变量 | 未设置时用账户总资产口径（`QMT_documentation.md` §6.4） |
