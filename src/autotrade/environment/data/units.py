from __future__ import annotations

import pandas as pd

# Percent -> decimal, 手 -> shares, 千元/万元 -> CNY
# (docs/environment_design.md §1.4 单位与特殊口径; data_documentation.md §1.2).
DAILY_UNIT_CONVERSIONS: tuple[tuple[str, float, str], ...] = (
    ("vol", 100.0, "hands->shares"),
    ("amount", 1000.0, "thousand_cny->cny"),
    ("pct_chg", 0.01, "percent->decimal"),
    ("turnover_rate", 0.01, "percent->decimal"),
    ("turnover_rate_f", 0.01, "percent->decimal"),
    ("dv_ratio", 0.01, "percent->decimal"),
    ("dv_ttm", 0.01, "percent->decimal"),
    ("total_share", 10_000.0, "ten_thousand_shares->shares"),
    ("float_share", 10_000.0, "ten_thousand_shares->shares"),
    ("free_share", 10_000.0, "ten_thousand_shares->shares"),
    ("total_mv", 10_000.0, "ten_thousand_cny->cny"),
    ("circ_mv", 10_000.0, "ten_thousand_cny->cny"),
)

AUCTION_UNIT_CONVERSIONS: tuple[tuple[str, float, str], ...] = (
    ("turnover_rate", 0.01, "percent->decimal"),
    ("float_share", 10_000.0, "ten_thousand_shares->shares"),
)

# Dataset-qualified source-unit registry: the single machine-readable source
# of truth for heterogeneous-union units, projected to (1) the Agent contract
# below (shipped via data_summary.json, so offline Fold Agents see every
# rule), (2) audit report metadata (tushare audit.py imports subsets), and
# (3) the table in docs/data_documentation.md §1.2. Family-level rules for
# datasets in or near trading research — deliberately not a per-column
# encyclopedia (see the docs §1.2 boundary). Empirically verified entries
# state the check; "raw" means preserved source units, convert before use.
SOURCE_UNIT_RULES: dict[str, str] = {
    # --- normalized trading files (source units BEFORE normalization) ---
    "daily.prices": "open/high/low/close/pre_close are CNY per share.",
    "daily.vol": "hands; multiply by 100 for shares",
    "daily.amount": "thousand CNY",
    "daily.percent_family": "pct_chg/turnover_rate(_f)/dv_ratio/dv_ttm are raw percent numbers; snapshots multiply by 0.01 to decimals.",
    "stk_mins": "minute-bar prices are CNY per share; vol is shares; amount is CNY.",
    "stk_auction": "price/pre_close are CNY per share; vol is shares; amount is CNY; turnover_rate raw percent, volume_ratio dimensionless, float_share raw 10k shares (snapshot normalizes rate and float_share).",
    # --- daily family cross-source traps ---
    "daily_basic.total_share/float_share/free_share": "10k shares",
    "daily_basic.total_mv/circ_mv": "10k CNY",
    "bak_daily.vol": "hands, same scale as daily.vol (verified ratio 1.0).",
    "bak_daily.amount": "10k CNY; multiply by 10 before comparing with daily.amount (verified).",
    "bak_daily.total_share/float_share": "100m shares; multiply by 10000 before comparing with daily_basic share fields (verified).",
    "bak_daily.total_mv/float_mv": "100m CNY; multiply by 10000 before comparing with daily_basic market values (verified).",
    "bak_basic.float_share/total_share": "100m shares; bak_basic has no volume or amount fields",
    "bak_basic.total_assets/liquid_assets/fixed_assets": "100m CNY style snapshot fields; use only as a supplemental coarse snapshot",
    # --- fundamentals ---
    "fundamental.statement_amount_fields": "income_vip/balancesheet_vip/cashflow_vip amount fields are CNY/yuan unless the field is explicitly per-share or ratio",
    "fundamental.forecast_profit_fields": "forecast_vip net_profit_min/net_profit_max are 10k CNY and must not be mixed directly with statement net profit in CNY",
    "fundamental.dividend_cash_fields": "dividend cash_div/cash_div_tax are per-share cash dividend in CNY; base_share is 10k shares when present",
    "fundamental.fina_indicator_vip": "mixed table: per-share CNY fields (eps/bps/...), percent fields (roe/...), dimensionless ratios, and CNY amounts; handle by field family",
    # --- event/flow datasets ---
    "margin/margin_detail": "financing/margin balance and amount fields are CNY; rqyl is securities-lending quantity in shares.",
    "margin_secs": "margin eligibility table has no numeric market amount; exchange is SSE/SZSE/BSE and does not guarantee broker-level borrow inventory.",
    "moneyflow": "volume fields are hands; amount fields are 10k CNY (500 means CNY 5m); normalize before mixing with daily/stk_mins.",
    "stk_holdernumber": "holder_num is shareholder account count.",
    "stk_holdertrade": "change_vol/after_share are shares; change_ratio and other ratios are percent.",
    "repurchase": "vol is shares; amount and high_limit/low_limit are CNY (verified amount/vol ~ price scale).",
    "share_float": "float_share is 10k shares (share_float_complete/_ann_date variants alike); float_ratio is percent; availability keys on ann_date when present.",
    "block_trade": "price is CNY per share; vol is 10k shares; amount is 10k CNY (verified price*vol==amount); sparse, zero-row trade dates expected.",
    # --- board-trading datasets ---
    "kpl_list": "amount/free_float/limit_order/lu_limit_order style fields are preserved in official raw units, mostly CNY-level amounts from source.",
    "limit_step": "nums is a consecutive-limit count label; no monetary unit.",
    "limit_cpt_list": "up_nums/cons_nums are counts; pct_chg is percent; rank is source rank.",
    "limit_list_ths": "price/current monetary fields are preserved in official raw units; pct_chg/turnover/rise_rate style fields are percent or source ratios as named.",
    "top_list": "amount and Dragon-Tiger buy/sell/net fields are CNY amounts; rates/turnover_rate are percent.",
    "top_inst": "buy/sell/net_buy fields are CNY amounts; buy_rate/sell_rate are percent.",
    "hm_detail": "buy_amount/sell_amount/net_amount are CNY amounts.",
    "ths_hot/dc_hot": "rank/hot are source popularity ranks/scores; pct_change is percent; current_price is CNY price for A-share rows.",
    "hm_list": "static text/reference metadata; no numeric unit.",
    # --- macro / cross-asset context (read-only, non-tradable) ---
    "cn_gdp": "GDP and industry value fields are 100m CNY style macro levels; *_yoy fields are percent.",
    "cn_cpi/cn_ppi": "index and inflation fields are official CPI/PPI values, month-on-month/year-on-year/accumulated percent fields by column suffix.",
    "cn_pmi": "PMI fields are diffusion-index levels.",
    "cn_m": "m0/m1/m2 are 100m CNY; *_yoy and *_mom are percent.",
    "sf_month": "social financing flow/stock fields are official 100m CNY style macro levels.",
    "shibor/shibor_lpr/libor/hibor/us_rates": "rate columns are percent levels unless a field name/document explicitly states otherwise.",
    "repo_daily": "bond repo price/rate/amount fields are preserved in official raw units; normalize before cross-asset factor use.",
    "index_daily": "index OHLC are index points; pct_chg is a percent number (5%=5.0) — do not multiply by 100 again; vol is hands, amount is thousand CNY.",
    "index_global": "global index OHLC fields are index points; vol/amount availability varies by market and source.",
    "fx_daily": "FX quote fields are bid/ask prices; tick_qty is quote/tick count, not stock volume.",
    "eco_cal": "economic-calendar actual/previous/forecast values are heterogeneous by event and must not be pooled without event-specific parsing.",
    "monetary_policy": "text/PDF evidence; no numeric unit.",
    "fut_daily": "futures prices are contract quote units (index points for CFFEX); vol/oi are lots (手); amount is 10k CNY; multiplier from fut_basic converts to notional.",
    "opt_daily": "option prices are premium quote units; vol/oi are contracts; amount is 10k CNY; exercise_price from opt_basic shares the underlying quote unit.",
    "cb_daily": "CB prices are per-100-par CNY; vol is lots, amount is 10k CNY; bond_over_rate/cb_over_rate are percent.",
    "yc_cb": "yield is percent per annum; curve_term is years; curve_type 0=YTM, 1=spot.",
}

# Compact Agent-facing labels. Heterogeneous union fields are identified by
# (file, dataset, column pattern), never by column name alone. The normalization
# functions below own byte transformations; this object only projects the rules.
AGENT_UNIT_CONTRACT: dict[str, object] = {
    "identity_rule": "interpret units by file + dataset + column; never by column name alone",
    "coverage": {
        "normalized_files": "complete file-level unit families and conversion factors",
        "source_unions": "high-risk rules only; not an exhaustive copy of every upstream field",
    },
    "daily.parquet": {
        "mode": "normalized",
        "price_fields": "CNY/share",
        "vol_and_share_fields": "shares",
        "amount_and_mv_fields": "CNY",
        "pct_chg_turnover_dv": "decimal; 5%=0.05; -9.5%=-0.095",
        "conversion_factors": {column: factor for column, factor, _ in DAILY_UNIT_CONVERSIONS},
    },
    "intraday_1min.parquet": {
        "price_fields": "CNY/share",
        "vol": "shares",
        "amount": "CNY",
    },
    "auction.parquet": {
        "mode": "normalized",
        "price_fields": "CNY/share",
        "vol": "shares",
        "amount": "CNY",
        "turnover_rate": "decimal; 0.5%=0.005",
        "volume_ratio": "dimensionless ratio; 1.2=1.2x",
        "float_share": "shares",
        "conversion_factors": {column: factor for column, factor, _ in AUCTION_UNIT_CONVERSIONS},
    },
    "events.parquet": {"mode": "source_by_dataset", "rules": "see source_unit_rules"},
    "macro.parquet": {"mode": "source_by_dataset", "rules": "see source_unit_rules"},
    "fundamentals.parquet": {"mode": "source_by_dataset", "rules": "see source_unit_rules"},
    # The full dataset-qualified registry, so offline Fold Agents can resolve
    # source units without host access; keys are dataset or dataset.family.
    "source_unit_rules": SOURCE_UNIT_RULES,
    "unknown_source_unit_policy": (
        "verify the upstream dataset contract and explicitly convert before using an unmapped field "
        "in an absolute threshold or cross-dataset calculation"
    ),
}


def normalize_daily_units(frame: pd.DataFrame) -> tuple[pd.DataFrame, list[dict[str, object]]]:
    """Apply the daily unit contract and return conversion metadata."""
    return _normalize_units(frame, DAILY_UNIT_CONVERSIONS)


def normalize_auction_units(frame: pd.DataFrame) -> tuple[pd.DataFrame, list[dict[str, object]]]:
    """Apply the opening-auction unit contract and return conversion metadata."""
    return _normalize_units(frame, AUCTION_UNIT_CONVERSIONS)


def _normalize_units(
    frame: pd.DataFrame,
    conversions_spec: tuple[tuple[str, float, str], ...],
) -> tuple[pd.DataFrame, list[dict[str, object]]]:
    frame = frame.copy()
    conversions: list[dict[str, object]] = []
    for column, factor, rule in conversions_spec:
        if column in frame.columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce") * factor
            conversions.append({"column": column, "factor": factor, "rule": rule})
    return frame, conversions
