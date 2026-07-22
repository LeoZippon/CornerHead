# 单位参考表

本文档由 `scripts/dev/export_units.py` 从 `src/autotrade/environment/data/units.py` 的
`FIELD_RULES` 生成，禁止手工编辑；回归测试会重新生成并与本文件比对。
单位口径的查找规则与使用纪律见 `docs/data_documentation.md` §1.2。

本表是注册表的**规则视图**（按列名/通配符定位的规则行）。逐列展开后的完整字段级
单位表随每次快照生成为 `/mnt/artifacts/unit_reference.json`，只包含当前快照实际可见
的 file/dataset/column；完备性由两道校验保证——快照构建对每一列强制解析（缺规则即
失败），回归测试对 `configs/data/snapshot_columns.json` 的全量供应商列清单强制解析。

状态含义：`verified` 已与另一数据源或已知外部事实对账（依据见 evidence 列）；
`official` 依据供应商官方字段合同；`inferred` 仅由本地量级合理性推断；`unknown`
诚实未解决——此类字段不得进入绝对阈值或跨数据集算术。`factor` 为快照载入时的乘数
（归一化文件存换算后的值）。

## daily.parquet（日频归一化文件）

| dataset | 列（名/通配） | 语义 | 源单位 | factor | 状态 | 依据/说明 |
|---|---|---|---|---|---|---|
| — | open/high/low/close/pre_close/change/close_basic/pre_close_limit/up_limit/down_limit | numeric | CNY_per_share | — | verified | matches auction and minute price scales |
| — | vol | numeric | hands | ×100 → shares | verified | cross-checked against stk_mins share volume |
| — | amount | numeric | thousand_CNY | ×1000 → CNY | verified | price*volume reconciliation |
| — | pct_chg/turnover_rate/turnover_rate_f/dv_ratio/dv_ttm | numeric | percent | ×0.01 → decimal | official | 5% arrives as 5.0; snapshot stores 0.05 |
| — | total_share/float_share/free_share | numeric | 10k_shares | ×10000 → shares | verified | back-calculated from share_float unlock ratios |
| — | total_mv/circ_mv | numeric | 10k_CNY | ×10000 → CNY | verified | price*shares reconciliation |
| — | volume_ratio/pe/pe_ttm/pb/ps/ps_ttm | numeric | multiple | — | official | dimensionless valuation/liquidity multiples |
| — | adj_factor | numeric | dimensionless_ratio | — | official | — |
| — | is_suspended | categorical | — | — | official | — |

## intraday_1min.parquet（历史分钟线）

| dataset | 列（名/通配） | 语义 | 源单位 | factor | 状态 | 依据/说明 |
|---|---|---|---|---|---|---|
| — | open/high/low/close | numeric | CNY_per_share | — | official | — |
| — | vol/vol_pit | numeric | shares | — | official | — |
| — | amount/amount_pit | numeric | CNY | — | official | — |
| — | auction_vol_correction_factor/auction_amount_correction_factor | numeric | dimensionless_ratio | — | official | — |
| — | auction_market_bucket/auction_open_bar/auction_correction_rule | categorical | — | — | official | — |

## auction.parquet（开盘竞价）

| dataset | 列（名/通配） | 语义 | 源单位 | factor | 状态 | 依据/说明 |
|---|---|---|---|---|---|---|
| — | price/pre_close | numeric | CNY_per_share | — | official | — |
| — | vol | numeric | shares | — | official | — |
| — | amount | numeric | CNY | — | official | — |
| — | turnover_rate | numeric | percent | ×0.01 → decimal | verified | single-day smoke check vs daily turnover |
| — | volume_ratio | numeric | multiple | — | official | 1.2 = 1.2x |
| — | float_share | numeric | 10k_shares | ×10000 → shares | verified | same source scale as daily_basic.float_share |

## corporate_actions.parquet（回放分红送转）

| dataset | 列（名/通配） | 语义 | 源单位 | factor | 状态 | 依据/说明 |
|---|---|---|---|---|---|---|
| — | cash_per_share | numeric | CNY_per_share | — | official | — |
| — | stock_per_share | numeric | shares_per_share | — | official | bonus/transfer shares per held share |

## events.parquet（事件/资金/打板 source union）

| dataset | 列（名/通配） | 语义 | 源单位 | factor | 状态 | 依据/说明 |
|---|---|---|---|---|---|---|
| `margin` | rzye/rzmre/rzche/rqye/rzrqye | numeric | CNY | — | official | — |
| `margin` | rqyl/rqmcl | numeric | shares | — | official | securities-lending quantities |
| `margin_detail` | rzye/rzmre/rzche/rqye/rzrqye | numeric | CNY | — | official | — |
| `margin_detail` | rqyl/rqmcl/rqchl | numeric | shares | — | official | — |
| `moneyflow` | buy_*_vol/sell_*_vol/net_mf_vol | numeric | hands | — | official | — |
| `moneyflow` | buy_*_amount/sell_*_amount/net_mf_amount | numeric | 10k_CNY | — | official | 500 means CNY 5m; normalize before mixing with daily/stk_mins |
| `moneyflow_dc` | close | numeric | CNY_per_share | — | verified | median 11.7 at stock price scale |
| `moneyflow_dc` | pct_change/*_rate | numeric | percent | — | official | — |
| `moneyflow_dc` | net_amount/buy_*_amount | numeric | 10k_CNY | — | inferred | per-stock medians at 10k-CNY scale only |
| `moneyflow_ths` | latest | numeric | CNY_per_share | — | verified | median 12.8 at stock price scale |
| `moneyflow_ths` | pct_change/*_rate | numeric | percent | — | official | — |
| `moneyflow_ths` | net_amount/net_d5_amount/buy_*_amount | numeric | 10k_CNY | — | inferred | per-stock medians at 10k-CNY scale only |
| `moneyflow_ind_dc` | close | numeric | index_points | — | inferred | median 1941 at board index scale |
| `moneyflow_ind_dc` | pct_change/*_rate | numeric | percent | — | official | — |
| `moneyflow_ind_dc` | net_amount/buy_*_amount | numeric | CNY | — | verified | industry medians only plausible in CNY (1.5e7 ~ 15m)；industry-level DC flows are CNY, unlike stock-level moneyflow_dc in 10k CNY |
| `moneyflow_ind_dc` | rank | numeric | rank | — | official | — |
| `moneyflow_ind_dc` | buy_sm_amount_stock | text | — | — | official | name of the top small-order-inflow stock |
| `moneyflow_ind_ths` | close | numeric | index_points | — | inferred | — |
| `moneyflow_ind_ths` | pct_change/pct_change_stock | numeric | percent | — | official | — |
| `moneyflow_ind_ths` | close_price | numeric | CNY_per_share | — | official | leading stock price |
| `moneyflow_ind_ths` | company_num | numeric | count | — | official | — |
| `moneyflow_ind_ths` | net_buy_amount/net_sell_amount/net_amount | numeric | 100m_CNY | — | verified | industry medians ~49.5 only plausible as 100m CNY |
| `moneyflow_cnt_ths` | industry_index | numeric | index_points | — | inferred | values ~3000 at concept index scale |
| `moneyflow_cnt_ths` | pct_change/pct_change_stock | numeric | percent | — | official | — |
| `moneyflow_cnt_ths` | close_price | numeric | CNY_per_share | — | official | leading stock price |
| `moneyflow_cnt_ths` | company_num | numeric | count | — | official | — |
| `moneyflow_cnt_ths` | net_buy_amount/net_sell_amount/net_amount | numeric | 100m_CNY | — | verified | concept medians ~162 only plausible as 100m CNY |
| `cyq_perf` | his_low/his_high/cost_5pct/cost_15pct/cost_50pct/cost_85pct/cost_95pct/weight_avg | numeric | CNY_per_share | — | verified | cost percentiles sit at price scale；cost_5pct is the 5th-percentile holder cost PRICE, not a percent |
| `cyq_perf` | winner_rate | numeric | percent | — | official | — |
| `bak_daily` | open/high/low/close/pre_close/change/avg_price | numeric | CNY_per_share | — | official | — |
| `bak_daily` | pct_change/turn_over/swing | numeric | percent | — | official | — |
| `bak_daily` | vol | numeric | hands | — | verified | ratio to daily.vol == 1.0 |
| `bak_daily` | amount | numeric | 10k_CNY | — | verified | x10 matches daily.amount (thousand CNY) |
| `bak_daily` | selling/buying | numeric | hands | — | inferred | selling+buying ~ vol |
| `bak_daily` | total_share/float_share | numeric | 100m_shares | — | verified | x10^4 matches daily_basic share fields；multiply by 10000 before comparing with daily_basic |
| `bak_daily` | total_mv/float_mv | numeric | 100m_CNY | — | verified | x10^4 matches daily_basic market values |
| `bak_daily` | pe/vol_ratio | numeric | multiple | — | official | — |
| `bak_daily` | strength/activity/attack | numeric | vendor_score | — | inferred | opaque vendor composite indicators |
| `bak_daily` | avg_turnover/interval_3/interval_6 | numeric | unknown | — | unknown | all-NA locally; resolve before use |
| `stk_premarket` | total_share/float_share | numeric | 10k_shares | — | verified | same scale as daily_basic share fields |
| `stk_premarket` | pre_close/up_limit/down_limit | numeric | CNY_per_share | — | official | — |
| `slb_len` | ob/auc_amount/repo_amount/repay_amount/cb | numeric | unknown | — | unknown | no local rows yet; verify on first landed partition |
| `slb_len_mm` | ope_inv/lent_qnt/cls_inv/end_bal | numeric | unknown | — | unknown | no local rows yet; verify on first landed partition |
| `block_trade` | price | numeric | CNY_per_share | — | verified | price*vol == amount |
| `block_trade` | vol | numeric | 10k_shares | — | verified | price*vol == amount |
| `block_trade` | amount | numeric | 10k_CNY | — | verified | price*vol == amount；sparse; zero-row dates expected |
| `stk_holdernumber` | holder_num | numeric | count | — | official | — |
| `top10_holders` | hold_amount/hold_change | numeric | shares | — | verified | hold_amount/(hold_ratio%) matches total share capital |
| `top10_holders` | hold_ratio/hold_float_ratio | numeric | percent | — | official | — |
| `top10_floatholders` | hold_amount/hold_change | numeric | shares | — | verified | same reconciliation as top10_holders |
| `top10_floatholders` | hold_ratio/hold_float_ratio | numeric | percent | — | official | — |
| `pledge_detail` | pledge_amount/holding_amount/pledged_amount | numeric | 10k_shares | — | verified | ratios reconcile at 10k-share scale |
| `pledge_detail` | p_total_ratio/h_total_ratio | numeric | percent | — | official | — |
| `stk_surv` | fund_visitors | numeric | count | — | official | participating institutions |
| `new_share` | price | numeric | CNY_per_share | — | official | — |
| `new_share` | pe | numeric | multiple | — | verified | median 15 at issue-PE scale |
| `new_share` | amount/market_amount/limit_amount | numeric | 10k_shares | — | official | — |
| `new_share` | funds | numeric | 100m_CNY | — | inferred | median 4.1 at IPO-proceeds scale only |
| `new_share` | ballot | numeric | percent | — | official | 0.03 means 0.03% |
| `stk_holdertrade` | change_vol/after_share/total_share | numeric | shares | — | verified | after_share/total_share at holder position scale；total_share is the holder's post-trade position, not company capital |
| `stk_holdertrade` | change_ratio/after_ratio | numeric | percent | — | official | — |
| `stk_holdertrade` | avg_price | numeric | CNY_per_share | — | verified | median 20.6 at price scale |
| `repurchase` | vol | numeric | shares | — | verified | amount/vol sits at price scale |
| `repurchase` | amount | numeric | CNY | — | official | — |
| `repurchase` | high_limit/low_limit | numeric | CNY_per_share | — | verified | medians 15.0/11.45 at price scale；repurchase price band, not amounts |
| `share_float_complete` | float_share | numeric | shares | — | verified | float_share/(float_ratio%) matches daily_basic total share capital；NOT 10k shares: 386-share unlock rows exist and reconcile only as shares |
| `share_float_complete` | float_ratio | numeric | percent | — | official | — |
| `top_list` | close | numeric | CNY_per_share | — | official | — |
| `top_list` | pct_change/turnover_rate/net_rate/amount_rate | numeric | percent | — | official | — |
| `top_list` | amount/l_sell/l_buy/l_amount/net_amount | numeric | CNY | — | official | — |
| `top_list` | float_values | numeric | CNY | — | verified | median 6e9 at float-market-value scale |
| `top_inst` | buy/sell/net_buy | numeric | CNY | — | official | — |
| `top_inst` | buy_rate/sell_rate | numeric | percent | — | official | — |
| `kpl_list` | pct_chg/bid_pct_chg/rt_pct_chg/turnover_rate | numeric | percent | — | official | — |
| `kpl_list` | amount/net_change/free_float/limit_order/lu_limit_order | numeric | CNY | — | inferred | medians (7.4e8 amount, 6.1e9 free float) at CNY scale |
| `kpl_list` | bid_amount/bid_change/bid_turnover/lu_bid_vol | numeric | unknown | — | unknown | all-NA locally; resolve before use |
| `kpl_concept_cons` | hot_num | numeric | vendor_score | — | official | — |
| `dc_index` | pct_change/leading_pct/turnover_rate | numeric | percent | — | official | — |
| `dc_index` | total_mv | numeric | 10k_CNY | — | inferred | concept aggregates only plausible at 10k-CNY scale |
| `dc_index` | up_num/down_num | numeric | count | — | official | — |
| `dc_index` | level | categorical | — | — | official | — |
| `limit_step` | nums | numeric | count | — | official | consecutive-limit count |
| `limit_cpt_list` | days/cons_nums/up_nums | numeric | count | — | official | — |
| `limit_cpt_list` | pct_chg | numeric | percent | — | official | — |
| `limit_cpt_list` | rank | numeric | rank | — | official | — |
| `limit_list_ths` | price | numeric | CNY_per_share | — | verified | median 24 at price scale |
| `limit_list_ths` | pct_chg/turnover_rate/limit_up_suc_rate | numeric | percent | — | official | — |
| `limit_list_ths` | open_num | numeric | count | — | official | — |
| `limit_list_ths` | free_float | numeric | CNY | — | inferred | median 7.1e9 at float-market-value scale |
| `limit_list_ths` | limit_order/limit_amount/turnover/rise_rate/sum_float/lu_limit_order | numeric | unknown | — | unknown | all-NA locally; resolve before use |
| `ths_hot` | rank | numeric | rank | — | official | — |
| `ths_hot` | pct_change | numeric | percent | — | official | — |
| `ths_hot` | current_price | numeric | CNY_per_share | — | official | for A-share rows |
| `ths_hot` | hot | numeric | vendor_score | — | official | — |
| `dc_hot` | rank | numeric | rank | — | official | — |
| `dc_hot` | pct_change | numeric | percent | — | official | — |
| `dc_hot` | current_price | numeric | CNY_per_share | — | official | for A-share rows |
| `dc_hot` | hot | numeric | vendor_score | — | official | — |
| `hm_detail` | buy_amount/sell_amount/net_amount | numeric | CNY | — | official | — |

## macro.parquet（宏观与跨资产 source union）

| dataset | 列（名/通配） | 语义 | 源单位 | factor | 状态 | 依据/说明 |
|---|---|---|---|---|---|---|
| `cn_gdp` | gdp/pi/si/ti | numeric | 100m_CNY | — | official | — |
| `cn_gdp` | *_yoy | numeric | percent | — | official | — |
| `cn_cpi` | nt_val/town_val/cnt_val | numeric | official_index | — | official | — |
| `cn_cpi` | *_yoy/*_mom/*_accu | numeric | percent | — | official | — |
| `cn_ppi` | ppi* | numeric | percent | — | official | yoy/mom/accumulated change rates; no index level columns |
| `cn_pmi` | pmi* | numeric | diffusion_index | — | official | — |
| `cn_m` | m0/m1/m2 | numeric | 100m_CNY | — | official | — |
| `cn_m` | *_yoy/*_mom | numeric | percent | — | official | — |
| `sf_month` | inc_month/inc_cumval/stk_endval | numeric | 100m_CNY | — | official | — |
| `shibor` | on/1w/2w/1m/3m/6m/9m/1y | numeric | percent | — | official | — |
| `shibor_quote` | *_b/*_a | numeric | percent | — | official | bid/ask quotes per tenor |
| `shibor_lpr` | 1y/5y | numeric | percent | — | official | — |
| `eco_cal` | value/pre_value/fore_value | numeric | unknown | — | unknown | heterogeneous by event; must not be pooled without event-specific parsing |
| `index_global` | open/close/high/low/pre_close/change | numeric | index_points | — | official | — |
| `index_global` | pct_chg/swing | numeric | percent | — | official | — |
| `index_global` | vol/amount | numeric | unknown | — | unknown | unit varies by market and source; sparse |
| `index_daily` | open/close/high/low/pre_close/change | numeric | index_points | — | official | — |
| `index_daily` | pct_chg | numeric | percent | — | official | 5%=5.0 — do not multiply by 100 again |
| `index_daily` | vol | numeric | hands | — | official | — |
| `index_daily` | amount | numeric | thousand_CNY | — | official | — |
| `index_dailybasic` | total_mv/float_mv | numeric | CNY | — | verified | CSI300 total_mv ~1e13 only plausible in CNY；CNY here vs 10k CNY in daily_basic — do not mix scales |
| `index_dailybasic` | total_share/float_share/free_share | numeric | shares | — | verified | ~1e11 share scale；shares here vs 10k shares in daily_basic |
| `index_dailybasic` | turnover_rate/turnover_rate_f | numeric | percent | — | verified | median 2.4 at percent scale |
| `index_dailybasic` | pe/pe_ttm/pb | numeric | multiple | — | official | — |
| `sw_daily` | open/low/high/close/change | numeric | index_points | — | official | — |
| `sw_daily` | pct_change | numeric | percent | — | official | — |
| `sw_daily` | vol | numeric | 10k_shares | — | official | — |
| `sw_daily` | amount | numeric | 10k_CNY | — | verified | industry turnover median 6.1e5 == 6.1b CNY |
| `sw_daily` | pe/pb | numeric | multiple | — | official | — |
| `sw_daily` | float_mv/total_mv | numeric | 10k_CNY | — | verified | industry total_mv median 2.5e7 == 250b CNY |
| `ci_daily` | open/low/high/close/pre_close/change | numeric | index_points | — | official | — |
| `ci_daily` | pct_change | numeric | percent | — | official | — |
| `ci_daily` | vol | numeric | 10k_shares | — | official | — |
| `ci_daily` | amount | numeric | 10k_CNY | — | official | — |
| `daily_info` | com_count | numeric | count | — | official | — |
| `daily_info` | trans_count | numeric | unknown | — | unknown | all-NA locally; count basis (笔 vs 万笔) unverified |
| `daily_info` | total_share/float_share/vol | numeric | 100m_shares | — | verified | exchange-level medians only plausible in 100m units |
| `daily_info` | total_mv/float_mv/amount | numeric | 100m_CNY | — | verified | exchange turnover ~3.9e3 == 390b CNY；SSE stats in 100m units vs sz_daily_info in CNY — do not mix |
| `daily_info` | pe | numeric | multiple | — | official | — |
| `daily_info` | tr | numeric | percent | — | official | — |
| `sz_daily_info` | count | numeric | count | — | official | — |
| `sz_daily_info` | amount/total_mv/float_mv | numeric | CNY | — | verified | SZSE daily turnover ~6.9e10 == 69b CNY |
| `sz_daily_info` | vol | numeric | shares | — | inferred | sparse locally |
| `sz_daily_info` | total_share/float_share | numeric | shares | — | inferred | sparse locally |
| `moneyflow_mkt_dc` | close_sh/close_sz | numeric | index_points | — | official | — |
| `moneyflow_mkt_dc` | pct_change_sh/pct_change_sz/*_rate | numeric | percent | — | official | — |
| `moneyflow_mkt_dc` | net_amount/buy_*_amount | numeric | CNY | — | verified | market-wide flows ~ -4.5e10 plausible only in CNY |
| `ths_daily` | open/high/low/close/pre_close/change | numeric | index_points | — | official | — |
| `ths_daily` | avg_price | numeric | CNY_per_share | — | official | — |
| `ths_daily` | pct_change/turnover_rate | numeric | percent | — | official | — |
| `ths_daily` | vol | numeric | hands | — | inferred | magnitude between share and 10k-share scales；use comparatively; do not mix with stock volumes |
| `fx_daily` | bid_*/ask_* | numeric | quote_price | — | official | — |
| `fx_daily` | tick_qty | numeric | count | — | official | quote/tick count, not traded volume |
| `repo_daily` | repo_maturity | categorical | — | — | official | — |
| `repo_daily` | pre_close/open/high/low/close/weight/weight_r | numeric | percent | — | verified | repo quotes are annualized rates (~1.5) |
| `repo_daily` | amount | numeric | 10k_CNY | — | verified | GC001 daily ~2.2e8 == ~2.2 trillion CNY |
| `repo_daily` | num | numeric | count | — | official | — |
| `us_tycr` | m*/y* | numeric | percent | — | official | — |
| `us_trycr` | y* | numeric | percent | — | official | — |
| `fut_basic` | multiplier/per_unit | numeric | units_per_lot | — | official | — |
| `fut_daily` | pre_close/pre_settle/open/high/low/close/settle/delv_settle/change1/change2 | numeric | contract_quote_units | — | official | index points for CFFEX; fut_basic multiplier converts to notional |
| `fut_daily` | vol/oi/oi_chg | numeric | lots | — | official | — |
| `fut_daily` | amount | numeric | 10k_CNY | — | official | — |
| `opt_basic` | per_unit | numeric | units_per_lot | — | official | — |
| `opt_basic` | exercise_price | numeric | underlying_quote_units | — | official | — |
| `opt_basic` | list_price | numeric | premium_quote_units | — | official | — |
| `opt_basic` | min_price_chg | text | — | — | official | vendor tick-size field; format varies |
| `opt_daily` | pre_settle/pre_close/open/high/low/close/settle | numeric | premium_quote_units | — | official | — |
| `opt_daily` | vol/oi | numeric | contracts | — | official | — |
| `opt_daily` | amount | numeric | 10k_CNY | — | verified | premium*per_unit*vol reconciles at 10k-CNY scale |
| `cb_basic` | par/issue_price/maturity_call_price | numeric | CNY_per_100_par | — | official | — |
| `cb_basic` | issue_size/remain_size | numeric | CNY | — | verified | issue_size ~7.5e8 at bond-issue scale |
| `cb_basic` | maturity | numeric | years | — | official | — |
| `cb_basic` | coupon_rate | numeric | percent | — | official | — |
| `cb_basic` | pay_per_year | numeric | count | — | official | — |
| `cb_basic` | first_conv_price/conv_price | numeric | CNY_per_share | — | verified | medians 12-16 at stock price scale；nightly CURRENT-STATE refresh; never feed historical backtests |
| `cb_daily` | pre_close/open/high/low/close/change/bond_value/cb_value | numeric | CNY_per_100_par | — | official | — |
| `cb_daily` | pct_chg/bond_over_rate/cb_over_rate | numeric | percent | — | official | — |
| `cb_daily` | vol | numeric | lots | — | official | — |
| `cb_daily` | amount | numeric | 10k_CNY | — | official | — |
| `cb_call` | call_price/call_price_tax | numeric | CNY_per_100_par | — | official | — |
| `cb_call` | call_vol | numeric | bonds | — | verified | call_vol*call_price reconciles with call_amount |
| `cb_call` | call_amount | numeric | 10k_CNY | — | verified | reconciles with call_vol at per-100-par prices |
| `yc_cb` | curve_term | numeric | years | — | official | — |
| `yc_cb` | yield | numeric | percent | — | official | curve_type 0=YTM, 1=spot |

## fundamentals.parquet（财务 source union）

| dataset | 列（名/通配） | 语义 | 源单位 | factor | 状态 | 依据/说明 |
|---|---|---|---|---|---|---|
| `income_vip` | basic_eps/diluted_eps | numeric | CNY_per_share | — | official | — |
| `income_vip` | * | numeric | CNY | — | official | income statement amounts (vendor contract is uniform) |
| `balancesheet_vip` | total_share | numeric | shares | — | verified | median 3.9e8 at share-capital scale；period-end total shares, not the CNY paid-in-capital line |
| `balancesheet_vip` | * | numeric | CNY | — | official | balance sheet amounts (vendor contract is uniform) |
| `cashflow_vip` | * | numeric | CNY | — | official | cash flow statement amounts (vendor contract is uniform) |
| `fina_indicator_vip` | eps/dt_eps/total_revenue_ps/revenue_ps/capital_rese_ps/surplus_rese_ps/undist_profit_ps/diluted2_eps/bps/ocfps/retainedps/cfps/ebit_ps/fcff_ps/fcfe_ps | numeric | CNY_per_share | — | official | — |
| `fina_indicator_vip` | extra_item/profit_dedt/gross_margin/op_income/ebit/ebitda/fcff/fcfe/current_exint/noncurrent_exint/interestdebt/netdebt/tangible_asset/working_capital/networking_capital/invest_capital/retained_earnings/fixed_assets | numeric | CNY | — | verified | gross_margin median 2.4e8 is a CNY amount；gross_margin is gross PROFIT in CNY; grossprofit_margin is the percent |
| `fina_indicator_vip` | current_ratio/quick_ratio/cash_ratio/assets_to_eqt/dp_assets_to_eqt/debt_to_eqt/eqt_to_debt/eqt_to_interestdebt/tangibleasset_to_debt/tangasset_to_intdebt/tangibleasset_to_netdebt/ocf_to_debt/ocf_to_shortdebt | numeric | multiple | — | verified | current_ratio median 1.68 = 1.68x, not percent |
| `fina_indicator_vip` | ar_turn/ca_turn/fa_turn/assets_turn | numeric | times_per_period | — | verified | assets_turn median 0.35 at turnover-frequency scale |
| `fina_indicator_vip` | turn_days | numeric | days | — | verified | median 180 at days scale |
| `fina_indicator_vip` | netprofit_margin/grossprofit_margin/cogs_of_sales/expense_of_sales/profit_to_gr/saleexp_to_gr/adminexp_of_gr/finaexp_of_gr/gc_of_gr/op_of_gr/ebit_of_gr/roe/roe_waa/roe_dt/roa/npta/roic/roe_yearly/roa2_yearly/roa_yearly/roa_dp/debt_to_assets/ca_to_assets/nca_to_assets/tbassets_to_totalassets/int_to_talcap/eqt_to_talcapital/currentdebt_to_debt/longdeb_to_debt/profit_to_op/q_*/*_yoy/q_op_qoq | numeric | percent | — | verified | roe median 10.6, debt_to_assets 42.8 at percent scale |
| `fina_indicator_vip` | impai_ttm | numeric | unknown | — | unknown | median -0.63 inconsistent with a CNY amount; resolve before use |
| `forecast_vip` | p_change_min/p_change_max | numeric | percent | — | official | — |
| `forecast_vip` | net_profit_min/net_profit_max/last_parent_net | numeric | 10k_CNY | — | verified | last_parent_net matches forecast bounds at 10k-CNY scale；must not be mixed directly with statement net profit in CNY |
| `express_vip` | revenue/operate_profit/total_profit/n_income/total_assets/total_hldr_eqy_exc_min_int/open_net_assets | numeric | CNY | — | verified | revenue median 4.9e9 at CNY scale |
| `express_vip` | diluted_eps/bps/open_bps | numeric | CNY_per_share | — | official | — |
| `express_vip` | diluted_roe/yoy_net_profit | numeric | percent | — | official | — |
| `dividend` | cash_div/cash_div_tax | numeric | CNY_per_share | — | verified | median 0.095 per share |
| `dividend` | stk_div/stk_bo_rate/stk_co_rate | numeric | shares_per_share | — | official | bonus/transfer proportions per held share |
| `fina_audit` | audit_fees | numeric | CNY | — | verified | median 4e5 at audit-fee scale |
| `fina_mainbz_vip` | bz_sales/bz_profit/bz_cost | numeric | CNY | — | verified | segment revenue median 1.3e7 at CNY scale |

## 仅原始湖数据（不进入快照）

| dataset | 列（名/通配） | 语义 | 源单位 | factor | 状态 | 依据/说明 |
|---|---|---|---|---|---|---|
| `bak_basic` | total_share/float_share | numeric | 100m_shares | — | official | no volume or amount fields |
| `bak_basic` | total_assets/liquid_assets/fixed_assets | numeric | 100m_CNY | — | official | coarse company snapshot fields; supplemental use only |
| `cn_schedule` | * | text | — | — | official | release schedule; not in the default snapshot macro set |
| `hibor` | * | numeric | percent | — | official | not in the default snapshot macro set |
| `libor` | * | numeric | percent | — | official | not in the default snapshot macro set |
| `us_tbr` | * | numeric | percent | — | official | not in the default snapshot macro set |
| `us_tltr` | * | numeric | percent | — | official | not in the default snapshot macro set |

## 无数值字段的数据集

以下数据集全部字段均为标识/日期/文本，由通用分类器解析，不携带单位规则：
`broker_recommend`、`dc_member`、`disclosure_date`、`fut_mapping`、`hm_list`、`margin_secs`、`monetary_policy`。

## 通用列分类器（按序首个匹配；数据集规则优先）

| 模式 | 语义 |
|---|---|
| `available_at_rule` | categorical |
| `available_at` | datetime |
| `available_month` | datetime |
| `dataset` | identifier |
| `ts_code` | identifier |
| `ts_codes` | identifier |
| `con_code` | identifier |
| `sub_code` | identifier |
| `cb_code` | identifier |
| `stk_code` | identifier |
| `opt_code` | identifier |
| `mapping_ts_code` | identifier |
| `leading_code` | identifier |
| `bz_code` | identifier |
| `l1_code` | identifier |
| `pcode` | identifier |
| `symbol` | identifier |
| `text_id` | identifier |
| `library_file` | identifier |
| `source_hash` | identifier |
| `source_path` | identifier |
| `source_row_id` | identifier |
| `business_key` | identifier |
| `download_path` | identifier |
| `source_file` | identifier |
| `source_cap_risk` | categorical |
| `url` | identifier |
| `pdf_url` | identifier |
| `update_flag` | categorical |
| `curr_type` | categorical |
| `session` | categorical |
| `type` | categorical |
| `*_type` | categorical |
| `call_put` | categorical |
| `fut_code` | identifier |
| `?_month` | categorical |
| `*_date` | datetime |
| `*_ddate` | datetime |
| `*_edate` | datetime |
| `div_listdate` | datetime |
| `date` | datetime |
| `datetime` | datetime |
| `month` | datetime |
| `quarter` | datetime |
| `*_time` | datetime |
| `time` | datetime |
| `maturity_date` | datetime |
| `*_name` | text |
| `name` | text |
| `ts_name` | text |
| `*_title` | text |
| `title` | text |
| `content*` | text |
| `*_desc` | text |
| `desc` | text |
| `*_reason` | text |
| `reason` | text |
| `*_summary` | text |
| `summary` | text |
| `exchange` | categorical |
| `exchange_id` | categorical |
| `market` | categorical |
| `side` | categorical |
| `status` | categorical |
| `proc` | categorical |
| `div_proc` | categorical |
| `in_de` | categorical |
| `is_*` | categorical |
| `up_stat` | text |
| `tag` | text |
| `theme` | text |
| `concept` | text |
| `industry` | text |
| `area` | text |
| `lead_stock` | text |
| `exalter` | text |
| `orgs` | text |
| `hm_orgs` | text |
| `pledgor` | text |
| `rece_*` | text |
| `comp_rece` | text |
| `bank` | text |
| `broker` | text |
| `country` | categorical |
| `currency` | categorical |
| `event` | text |
| `buyer` | text |
| `seller` | text |
| `*_clause` | text |
| `guarantor` | text |
| `*_rating` | text |
| `rating*` | text |
| `audit_result` | text |
| `audit_agency` | text |
| `audit_sign` | text |
| `bz_item` | text |
| `author*` | text |
| `classify` | text |
| `imp_dg` | text |
| `inst_csname` | text |
| `channels` | text |
| `src` | categorical |
| `puborg` | text |
| `q` | text |
| `a` | text |
| `trade_unit` | text |
| `quote_unit` | text |
| `leading` | text |
