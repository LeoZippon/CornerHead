# 单位参考表

本文档由 `scripts/dev/export_units.py` 从 `src/autotrade/environment/data/units.py` 的
`UNIT_RULES` 生成，禁止手工编辑；回归测试会重新生成并与本文件比对。
单位口径的分层边界与使用纪律见 `docs/data_documentation.md` §1.2。

状态含义：`verified` 已经真实数据比对核验；`official` 依据供应商官方字段合同；
`inferred` 仅由局部证据推断，使用前应补核验。`snapshot factor` 为快照载入时的乘数
（空表示保留源单位）。`agent` 为否的条目不进入 Agent 合同（非默认快照数据集）。

## daily.parquet（日频归一化文件）

| dataset | 字段族 | 源单位 | snapshot factor | 状态 | agent | 依据/说明 |
|---|---|---|---|---|---|---|
| — | open/high/low/close/pre_close | CNY_per_share | — | verified | 是 | matches auction and minute price scales |
| — | vol | hands | ×100 → shares | verified | 是 | cross-checked against stk_mins share volume |
| — | amount | thousand_CNY | ×1000 → CNY | verified | 是 | price*volume reconciliation |
| — | pct_chg/turnover_rate/turnover_rate_f/dv_ratio/dv_ttm | percent | ×0.01 → decimal | official | 是 | 5% arrives as 5.0; snapshot stores 0.05 |
| — | total_share/float_share/free_share | 10k_shares | ×10000 → shares | verified | 是 | back-calculated from share_float unlock ratios |
| — | total_mv/circ_mv | 10k_CNY | ×10000 → CNY | verified | 是 | price*shares reconciliation |
| — | adj_factor | dimensionless_ratio | — | official | 是 | — |
| — | up_limit/down_limit | CNY_per_share | — | official | 是 | — |

## intraday_1min.parquet（历史分钟线）

| dataset | 字段族 | 源单位 | snapshot factor | 状态 | agent | 依据/说明 |
|---|---|---|---|---|---|---|
| — | open/high/low/close | CNY_per_share | — | official | 是 | — |
| — | vol | shares | — | official | 是 | — |
| — | amount | CNY | — | official | 是 | — |

## auction.parquet（开盘竞价）

| dataset | 字段族 | 源单位 | snapshot factor | 状态 | agent | 依据/说明 |
|---|---|---|---|---|---|---|
| — | price/pre_close | CNY_per_share | — | official | 是 | — |
| — | vol | shares | — | official | 是 | — |
| — | amount | CNY | — | official | 是 | — |
| — | turnover_rate | percent | ×0.01 → decimal | verified | 是 | single-day smoke check vs daily turnover |
| — | volume_ratio | dimensionless_ratio | — | official | 是 | 1.2 = 1.2x |
| — | float_share | 10k_shares | ×10000 → shares | verified | 是 | same source scale as daily_basic.float_share |

## events.parquet（事件/资金/打板 source union）

| dataset | 字段族 | 源单位 | snapshot factor | 状态 | agent | 依据/说明 |
|---|---|---|---|---|---|---|
| `margin` | rzye/rzmre/rqye and other balances | CNY | — | official | 是 | rqyl is securities-lending quantity in shares |
| `margin_detail` | balance/amount fields | CNY | — | official | 是 | rqyl/rqmcl are share quantities |
| `margin_secs` | no numeric market amount | none | — | official | 是 | eligibility table; exchange is SSE/SZSE/BSE, not broker inventory |
| `moneyflow` | buy_*_vol/sell_*_vol | hands | — | official | 是 | — |
| `moneyflow` | buy_*_amount/sell_*_amount/net_mf_amount | 10k_CNY | — | official | 是 | 500 means CNY 5m; normalize before mixing with daily/stk_mins |
| `moneyflow_dc` | net_amount/buy_*_amount | 10k_CNY | — | official | 是 | per-stock medians at 10k-CNY scale |
| `moneyflow_ths` | net_amount/net_d5_amount/buy_*_amount | 10k_CNY | — | official | 是 | latest is CNY per share price |
| `moneyflow_ind_dc` | net_amount/buy_*_amount | CNY | — | verified | 是 | industry medians only plausible in CNY (1.5e7 ~ 15m)；industry-level DC flows are CNY, unlike stock-level moneyflow_dc in 10k CNY |
| `moneyflow_ind_ths` | net_buy_amount/net_sell_amount/net_amount | 100m_CNY | — | verified | 是 | industry medians ~49.5 only plausible as 100m CNY；close_price/pct_change_stock refer to the leading stock (CNY / percent) |
| `moneyflow_cnt_ths` | net_buy_amount/net_sell_amount/net_amount | 100m_CNY | — | verified | 是 | concept medians ~162 only plausible as 100m CNY |
| `cyq_perf` | his_low/his_high/cost_*pct/weight_avg | CNY_per_share | — | verified | 是 | cost percentiles sit at price scale |
| `cyq_perf` | winner_rate/cost_*pct percentile labels | percent | — | official | 是 | — |
| `bak_daily` | vol | hands | — | verified | 是 | ratio to daily.vol == 1.0 |
| `bak_daily` | amount | 10k_CNY | — | verified | 是 | x10 matches daily.amount (thousand CNY) |
| `bak_daily` | total_share/float_share | 100m_shares | — | verified | 是 | x10^4 matches daily_basic share fields；multiply by 10000 before comparing with daily_basic |
| `bak_daily` | total_mv/float_mv | 100m_CNY | — | verified | 是 | x10^4 matches daily_basic market values |
| `stk_premarket` | total_share/float_share | 10k_shares | — | verified | 是 | same scale as daily_basic share fields |
| `stk_premarket` | pre_close/up_limit/down_limit | CNY_per_share | — | official | 是 | — |
| `slb_len` | balances | CNY_and_shares_by_field | — | official | 是 | no local rows yet; verify on first landed partition |
| `slb_len_mm` | balances | CNY_and_shares_by_field | — | official | 是 | no local rows yet; verify on first landed partition |
| `block_trade` | price | CNY_per_share | — | verified | 是 | price*vol == amount |
| `block_trade` | vol | 10k_shares | — | verified | 是 | price*vol == amount |
| `block_trade` | amount | 10k_CNY | — | verified | 是 | price*vol == amount；sparse; zero-row dates expected |
| `stk_holdernumber` | holder_num | account_count | — | official | 是 | — |
| `top10_holders` | hold_amount/hold_change | shares | — | verified | 是 | hold_amount/(hold_ratio%) matches total share capital |
| `top10_holders` | hold_ratio/hold_float_ratio | percent | — | official | 是 | — |
| `top10_floatholders` | hold_amount/hold_change | shares | — | verified | 是 | same reconciliation as top10_holders |
| `top10_floatholders` | hold_ratio/hold_float_ratio | percent | — | official | 是 | — |
| `pledge_detail` | pledge_amount/holding_amount/pledged_amount | 10k_shares | — | official | 是 | ratios reconcile at 10k-share scale |
| `pledge_detail` | p_total_ratio/h_total_ratio | percent | — | official | 是 | — |
| `stk_surv` | survey metadata | text | — | official | 是 | no numeric unit |
| `new_share` | price | CNY_per_share | — | official | 是 | — |
| `new_share` | amount/market_amount/limit_amount | 10k_shares | — | official | 是 | — |
| `new_share` | funds | 100m_CNY | — | official | 是 | median 7.18 at IPO-proceeds scale |
| `new_share` | ballot | percent | — | official | 是 | 0.03 means 0.03% |
| `stk_holdertrade` | change_vol/after_share | shares | — | verified | 是 | after_share at share-capital scale |
| `stk_holdertrade` | change_ratio and other ratios | percent | — | official | 是 | — |
| `repurchase` | vol | shares | — | verified | 是 | amount/vol sits at price scale |
| `repurchase` | amount | CNY | — | official | 是 | — |
| `repurchase` | high_limit/low_limit | CNY_per_share | — | verified | 是 | medians 15.0/11.45 at price scale；repurchase price band, not amounts |
| `share_float_complete` | float_share | shares | — | verified | 是 | float_share/(float_ratio%) matches daily_basic total share capital；NOT 10k shares: 386-share unlock rows exist and reconcile only as shares |
| `share_float_complete` | float_ratio | percent | — | official | 是 | — |
| `top_list` | amount/l_buy/l_sell/net_amount and buy/sell fields | CNY | — | official | 是 | — |
| `top_list` | net_rate/amount_rate/turnover_rate | percent | — | official | 是 | — |
| `top_inst` | buy/sell/net_buy | CNY | — | official | 是 | — |
| `top_inst` | buy_rate/sell_rate | percent | — | official | 是 | — |
| `kpl_list` | amount/free_float/limit_order/lu_limit_order | CNY | — | official | 是 | mostly CNY-level amounts from source |
| `kpl_concept_cons` | hot_num | source_rank_score | — | official | 是 | — |
| `dc_index` | total_mv | 10k_CNY | — | inferred | 是 | concept aggregates only plausible at 10k-CNY scale |
| `dc_index` | pct_change/leading_pct/turnover_rate | percent | — | official | 是 | — |
| `dc_index` | up_num/down_num | count | — | official | 是 | — |
| `dc_member` | membership mapping | text | — | official | 是 | no numeric unit |
| `limit_step` | nums | count | — | official | 是 | consecutive-limit count label |
| `limit_cpt_list` | up_nums/cons_nums | count | — | official | 是 | — |
| `limit_cpt_list` | pct_chg | percent | — | official | 是 | — |
| `limit_list_ths` | price/current monetary fields | CNY | — | official | 是 | pct_chg/turnover/rise_rate style fields are percent |
| `ths_hot` | rank/hot | source_rank_score | — | official | 是 | pct_change is percent; current_price is CNY for A-share rows |
| `dc_hot` | rank/hot | source_rank_score | — | official | 是 | pct_change is percent; current_price is CNY for A-share rows |
| `hm_detail` | buy_amount/sell_amount/net_amount | CNY | — | official | 是 | — |
| `hm_list` | reference metadata | text | — | official | 是 | no numeric unit |

## macro.parquet（宏观与跨资产 source union）

| dataset | 字段族 | 源单位 | snapshot factor | 状态 | agent | 依据/说明 |
|---|---|---|---|---|---|---|
| `cn_gdp` | gdp and industry value fields | 100m_CNY | — | official | 是 | *_yoy fields are percent |
| `cn_cpi` | index levels | official_index | — | official | 是 | mom/yoy/accumulated percent fields by column suffix |
| `cn_ppi` | index levels | official_index | — | official | 是 | mom/yoy/accumulated percent fields by column suffix |
| `cn_pmi` | pmi fields | diffusion_index | — | official | 是 | — |
| `cn_m` | m0/m1/m2 | 100m_CNY | — | official | 是 | *_yoy and *_mom are percent |
| `sf_month` | social financing flows/stocks | 100m_CNY | — | official | 是 | — |
| `shibor` | rate columns | percent | — | official | 是 | — |
| `shibor_quote` | bid/ask columns | percent | — | official | 是 | — |
| `shibor_lpr` | rate columns | percent | — | official | 是 | — |
| `monetary_policy` | text/PDF evidence | text | — | official | 是 | — |
| `eco_cal` | actual/previous/forecast | heterogeneous_by_event | — | official | 是 | must not be pooled without event-specific parsing |
| `index_global` | OHLC | index_points | — | official | 是 | vol/amount availability varies by market and source |
| `index_daily` | OHLC | index_points | — | official | 是 | — |
| `index_daily` | pct_chg | percent | — | official | 是 | 5%=5.0 — do not multiply by 100 again |
| `index_daily` | vol | hands | — | official | 是 | — |
| `index_daily` | amount | thousand_CNY | — | official | 是 | — |
| `index_dailybasic` | total_mv/float_mv | CNY | — | verified | 是 | CSI300 total_mv ~1e13 only plausible in CNY；CNY here vs 10k CNY in daily_basic — do not mix scales |
| `index_dailybasic` | total_share/float_share/free_share | shares | — | verified | 是 | ~1e11 share scale；shares here vs 10k shares in daily_basic |
| `index_dailybasic` | turnover_rate(_f)/pe(_ttm)/pb | percent_or_ratio | — | official | 是 | — |
| `sw_daily` | OHLC | index_points | — | official | 是 | — |
| `sw_daily` | vol | 10k_shares | — | official | 是 | — |
| `sw_daily` | amount | 10k_CNY | — | official | 是 | — |
| `ci_daily` | OHLC | index_points | — | official | 是 | vol/amount follow the sw_daily 10k shares / 10k CNY convention |
| `daily_info` | total_share/float_share/vol | 100m_shares | — | verified | 是 | exchange-level medians only plausible in 100m units |
| `daily_info` | total_mv/float_mv/amount | 100m_CNY | — | verified | 是 | exchange turnover ~3.9e3 == 390b CNY；SSE stats in 100m units vs sz_daily_info in CNY — do not mix |
| `sz_daily_info` | amount/total_mv/float_mv | CNY | — | verified | 是 | SZSE daily turnover ~6.9e10 == 69b CNY |
| `moneyflow_mkt_dc` | net_amount/buy_*_amount | CNY | — | verified | 是 | market-wide flows ~ -4.5e10 plausible only in CNY |
| `broker_recommend` | monthly broker lists | text | — | official | 是 | — |
| `ths_daily` | OHLC | index_points | — | official | 是 | avg_price is CNY per share; turnover_rate/pct_change are percent |
| `ths_daily` | vol | source_volume_units | — | inferred | 是 | use comparatively; do not mix with stock volumes |
| `fx_daily` | bid/ask quotes | quote_price | — | official | 是 | tick_qty is quote/tick count, not stock volume |
| `repo_daily` | price/rate/amount | official_raw | — | official | 是 | normalize before cross-asset factor use |
| `us_tycr` | yields | percent | — | official | 是 | — |
| `us_trycr` | yields | percent | — | official | 是 | — |
| `fut_basic` | per_unit | units_per_lot_multiplier | — | official | 是 | — |
| `fut_mapping` | contract mapping | text | — | official | 是 | — |
| `fut_daily` | prices | contract_quote_units | — | official | 是 | index points for CFFEX; multiplier from fut_basic converts to notional |
| `fut_daily` | vol/oi | lots | — | official | 是 | — |
| `fut_daily` | amount | 10k_CNY | — | official | 是 | — |
| `opt_basic` | per_unit/min_price_chg/exercise_price | contract_units | — | official | 是 | exercise_price shares the underlying quote unit |
| `opt_daily` | prices | premium_quote_units | — | official | 是 | — |
| `opt_daily` | vol/oi | contracts | — | official | 是 | — |
| `opt_daily` | amount | 10k_CNY | — | official | 是 | — |
| `cb_basic` | par/issue_price | CNY_per_100_par | — | official | 是 | — |
| `cb_basic` | issue_size/remain_size | CNY | — | verified | 是 | issue_size ~7.5e8 at bond-issue scale |
| `cb_basic` | coupon_rate | percent | — | official | 是 | — |
| `cb_daily` | prices | CNY_per_100_par | — | official | 是 | — |
| `cb_daily` | vol | lots | — | official | 是 | — |
| `cb_daily` | amount | 10k_CNY | — | official | 是 | — |
| `cb_daily` | bond_over_rate/cb_over_rate | percent | — | official | 是 | — |
| `cb_call` | call_price/call_price_tax | CNY_per_100_par | — | official | 是 | — |
| `cb_call` | call_vol | bonds | — | verified | 是 | call_vol*call_price reconciles with call_amount in 10k CNY |
| `cb_call` | call_amount | 10k_CNY | — | verified | 是 | reconciles with call_vol at per-100-par prices |
| `yc_cb` | yield | percent | — | official | 是 | curve_term is years; curve_type 0=YTM, 1=spot |
| `cn_schedule` | release schedule | text | — | official | 否 | not in the default snapshot macro set |
| `hibor` | rate columns | percent | — | official | 否 | not in the default snapshot macro set |
| `libor` | rate columns | percent | — | official | 否 | not in the default snapshot macro set |
| `us_tbr` | rate columns | percent | — | official | 否 | not in the default snapshot macro set |
| `us_tltr` | rate columns | percent | — | official | 否 | not in the default snapshot macro set |

## fundamentals.parquet（财务 source union）

| dataset | 字段族 | 源单位 | snapshot factor | 状态 | agent | 依据/说明 |
|---|---|---|---|---|---|---|
| `income_vip` | amount fields | CNY | — | official | 是 | unless the field is explicitly per-share or ratio |
| `balancesheet_vip` | amount fields | CNY | — | official | 是 | — |
| `cashflow_vip` | amount fields | CNY | — | official | 是 | — |
| `fina_indicator_vip` | eps/bps and per-share fields | CNY_per_share | — | official | 是 | — |
| `fina_indicator_vip` | roe and ratio fields | percent | — | official | 是 | mixed table; handle by field family, some fields are CNY amounts |
| `forecast_vip` | net_profit_min/net_profit_max | 10k_CNY | — | official | 是 | must not be mixed directly with statement net profit in CNY |
| `express_vip` | revenue/profit/asset fields | CNY | — | verified | 是 | revenue median 4.9e9 at CNY scale |
| `dividend` | cash_div/cash_div_tax | CNY_per_share | — | verified | 是 | median 0.095 per share |
| `dividend` | base_share | 10k_shares | — | official | 是 | present only on some records |
| `fina_audit` | audit_fees | CNY | — | verified | 是 | median 4e5 at audit-fee scale |
| `fina_mainbz_vip` | bz_sales/bz_profit/bz_cost | CNY | — | verified | 是 | segment revenue median 1.3e7 at CNY scale |
| `disclosure_date` | dates only | text | — | official | 是 | — |

## 仅原始湖数据（不进入快照）

| dataset | 字段族 | 源单位 | snapshot factor | 状态 | agent | 依据/说明 |
|---|---|---|---|---|---|---|
| `bak_basic` | float_share/total_share | 100m_shares | — | official | 否 | no volume or amount fields |
| `bak_basic` | total_assets/liquid_assets/fixed_assets | 100m_CNY | — | official | 否 | coarse company snapshot fields; supplemental use only |
