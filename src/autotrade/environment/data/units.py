"""Dataset-qualified unit registry and snapshot unit normalization.

``UNIT_RULES`` is the single structured source of truth for units across the
data lake, projected to (1) the Agent contract (``AGENT_UNIT_CONTRACT``,
shipped via data_summary.json so offline Fold Agents resolve source units
without host access), (2) audit report metadata (tushare ``audit.py`` selects
by its domain dataset lists), (3) the generated ``docs/units_reference.md``
(``scripts/dev/export_units.py``; a freshness test pins it), and (4) the
actual snapshot byte conversions (``DAILY_UNIT_CONVERSIONS`` /
``AUCTION_UNIT_CONVERSIONS`` are derived from rules carrying a factor).

Registry policy: exact snapshot ``dataset`` identifiers only (no composite or
alias keys); family-level rules, not a per-column encyclopedia; ``status``
records how each rule was established — "verified" means checked against the
live lake with the check named in ``evidence``; observed value ranges may
validate a rule but never define one.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

import pandas as pd


@dataclass(frozen=True)
class UnitRule:
    file: str  # snapshot parquet the dataset lands in; "raw_only" if never snapshotted
    dataset: str | None  # exact snapshot `dataset` id; None for single-schema files
    fields: str  # column family the rule covers
    source_unit: str
    factor: float | None = None  # snapshot-load multiplier (None: kept as source)
    normalized_unit: str | None = None
    columns: tuple[str, ...] = ()  # exact columns the factor applies to
    status: str = "official"  # verified | official | inferred
    evidence: str = ""
    agent_visible: bool = True
    note: str = ""

    def key(self) -> str:
        return f"{self.dataset or self.file}:{self.fields}"

    def to_record(self) -> dict[str, object]:
        record = {k: v for k, v in asdict(self).items() if v not in (None, "", ())}
        record.pop("agent_visible", None)
        return record


UNIT_RULES: tuple[UnitRule, ...] = (
    # ------------------------- daily.parquet (normalized) -------------------
    UnitRule("daily.parquet", None, "open/high/low/close/pre_close", "CNY_per_share",
             status="verified", evidence="matches auction and minute price scales"),
    UnitRule("daily.parquet", None, "vol", "hands", 100.0, "shares", ("vol",),
             status="verified", evidence="cross-checked against stk_mins share volume"),
    UnitRule("daily.parquet", None, "amount", "thousand_CNY", 1000.0, "CNY", ("amount",),
             status="verified", evidence="price*volume reconciliation"),
    UnitRule("daily.parquet", None, "pct_chg/turnover_rate/turnover_rate_f/dv_ratio/dv_ttm",
             "percent", 0.01, "decimal",
             ("pct_chg", "turnover_rate", "turnover_rate_f", "dv_ratio", "dv_ttm"),
             status="official", note="5% arrives as 5.0; snapshot stores 0.05"),
    UnitRule("daily.parquet", None, "total_share/float_share/free_share", "10k_shares",
             10_000.0, "shares", ("total_share", "float_share", "free_share"),
             status="verified", evidence="back-calculated from share_float unlock ratios"),
    UnitRule("daily.parquet", None, "total_mv/circ_mv", "10k_CNY", 10_000.0, "CNY",
             ("total_mv", "circ_mv"), status="verified", evidence="price*shares reconciliation"),
    UnitRule("daily.parquet", None, "adj_factor", "dimensionless_ratio", columns=("adj_factor",)),
    UnitRule("daily.parquet", None, "up_limit/down_limit", "CNY_per_share",
             columns=("up_limit", "down_limit")),
    # -------------------- intraday_1min.parquet (source==normalized) --------
    UnitRule("intraday_1min.parquet", None, "open/high/low/close", "CNY_per_share"),
    UnitRule("intraday_1min.parquet", None, "vol", "shares", columns=("vol",)),
    UnitRule("intraday_1min.parquet", None, "amount", "CNY", columns=("amount",)),
    # ------------------------- auction.parquet (normalized) -----------------
    UnitRule("auction.parquet", None, "price/pre_close", "CNY_per_share"),
    UnitRule("auction.parquet", None, "vol", "shares"),
    UnitRule("auction.parquet", None, "amount", "CNY"),
    UnitRule("auction.parquet", None, "turnover_rate", "percent", 0.01, "decimal", ("turnover_rate",),
             status="verified", evidence="single-day smoke check vs daily turnover"),
    UnitRule("auction.parquet", None, "volume_ratio", "dimensionless_ratio", note="1.2 = 1.2x"),
    UnitRule("auction.parquet", None, "float_share", "10k_shares", 10_000.0, "shares", ("float_share",),
             status="verified", evidence="same source scale as daily_basic.float_share"),
    # ------------------------- events.parquet, by dataset -------------------
    UnitRule("events.parquet", "margin", "rzye/rzmre/rqye and other balances", "CNY",
             note="rqyl is securities-lending quantity in shares"),
    UnitRule("events.parquet", "margin_detail", "balance/amount fields", "CNY",
             note="rqyl/rqmcl are share quantities"),
    UnitRule("events.parquet", "margin_secs", "no numeric market amount", "none",
             note="eligibility table; exchange is SSE/SZSE/BSE, not broker inventory"),
    UnitRule("events.parquet", "moneyflow", "buy_*_vol/sell_*_vol", "hands"),
    UnitRule("events.parquet", "moneyflow", "buy_*_amount/sell_*_amount/net_mf_amount", "10k_CNY",
             note="500 means CNY 5m; normalize before mixing with daily/stk_mins"),
    UnitRule("events.parquet", "moneyflow_dc", "net_amount/buy_*_amount", "10k_CNY",
             status="official", evidence="per-stock medians at 10k-CNY scale"),
    UnitRule("events.parquet", "moneyflow_ths", "net_amount/net_d5_amount/buy_*_amount", "10k_CNY",
             status="official", note="latest is CNY per share price"),
    UnitRule("events.parquet", "moneyflow_ind_dc", "net_amount/buy_*_amount", "CNY",
             status="verified", evidence="industry medians only plausible in CNY (1.5e7 ~ 15m)",
             note="industry-level DC flows are CNY, unlike stock-level moneyflow_dc in 10k CNY"),
    UnitRule("events.parquet", "moneyflow_ind_ths", "net_buy_amount/net_sell_amount/net_amount", "100m_CNY",
             status="verified", evidence="industry medians ~49.5 only plausible as 100m CNY",
             note="close_price/pct_change_stock refer to the leading stock (CNY / percent)"),
    UnitRule("events.parquet", "moneyflow_cnt_ths", "net_buy_amount/net_sell_amount/net_amount", "100m_CNY",
             status="verified", evidence="concept medians ~162 only plausible as 100m CNY"),
    UnitRule("events.parquet", "cyq_perf", "his_low/his_high/cost_*pct/weight_avg", "CNY_per_share",
             status="verified", evidence="cost percentiles sit at price scale"),
    UnitRule("events.parquet", "cyq_perf", "winner_rate/cost_*pct percentile labels", "percent"),
    UnitRule("events.parquet", "bak_daily", "vol", "hands",
             status="verified", evidence="ratio to daily.vol == 1.0"),
    UnitRule("events.parquet", "bak_daily", "amount", "10k_CNY",
             status="verified", evidence="x10 matches daily.amount (thousand CNY)"),
    UnitRule("events.parquet", "bak_daily", "total_share/float_share", "100m_shares",
             status="verified", evidence="x10^4 matches daily_basic share fields",
             note="multiply by 10000 before comparing with daily_basic"),
    UnitRule("events.parquet", "bak_daily", "total_mv/float_mv", "100m_CNY",
             status="verified", evidence="x10^4 matches daily_basic market values"),
    UnitRule("events.parquet", "stk_premarket", "total_share/float_share", "10k_shares",
             status="verified", evidence="same scale as daily_basic share fields"),
    UnitRule("events.parquet", "stk_premarket", "pre_close/up_limit/down_limit", "CNY_per_share"),
    UnitRule("events.parquet", "slb_len", "balances", "CNY_and_shares_by_field",
             status="official", note="no local rows yet; verify on first landed partition"),
    UnitRule("events.parquet", "slb_len_mm", "balances", "CNY_and_shares_by_field",
             status="official", note="no local rows yet; verify on first landed partition"),
    UnitRule("events.parquet", "block_trade", "price", "CNY_per_share",
             status="verified", evidence="price*vol == amount"),
    UnitRule("events.parquet", "block_trade", "vol", "10k_shares",
             status="verified", evidence="price*vol == amount"),
    UnitRule("events.parquet", "block_trade", "amount", "10k_CNY",
             status="verified", evidence="price*vol == amount", note="sparse; zero-row dates expected"),
    UnitRule("events.parquet", "stk_holdernumber", "holder_num", "account_count"),
    UnitRule("events.parquet", "top10_holders", "hold_amount/hold_change", "shares",
             status="verified", evidence="hold_amount/(hold_ratio%) matches total share capital"),
    UnitRule("events.parquet", "top10_holders", "hold_ratio/hold_float_ratio", "percent"),
    UnitRule("events.parquet", "top10_floatholders", "hold_amount/hold_change", "shares",
             status="verified", evidence="same reconciliation as top10_holders"),
    UnitRule("events.parquet", "top10_floatholders", "hold_ratio/hold_float_ratio", "percent"),
    UnitRule("events.parquet", "pledge_detail", "pledge_amount/holding_amount/pledged_amount", "10k_shares",
             status="official", evidence="ratios reconcile at 10k-share scale"),
    UnitRule("events.parquet", "pledge_detail", "p_total_ratio/h_total_ratio", "percent"),
    UnitRule("events.parquet", "stk_surv", "survey metadata", "text", note="no numeric unit"),
    UnitRule("events.parquet", "new_share", "price", "CNY_per_share"),
    UnitRule("events.parquet", "new_share", "amount/market_amount/limit_amount", "10k_shares"),
    UnitRule("events.parquet", "new_share", "funds", "100m_CNY",
             status="official", evidence="median 7.18 at IPO-proceeds scale"),
    UnitRule("events.parquet", "new_share", "ballot", "percent", note="0.03 means 0.03%"),
    UnitRule("events.parquet", "stk_holdertrade", "change_vol/after_share", "shares",
             status="verified", evidence="after_share at share-capital scale"),
    UnitRule("events.parquet", "stk_holdertrade", "change_ratio and other ratios", "percent"),
    UnitRule("events.parquet", "repurchase", "vol", "shares",
             status="verified", evidence="amount/vol sits at price scale"),
    UnitRule("events.parquet", "repurchase", "amount", "CNY"),
    UnitRule("events.parquet", "repurchase", "high_limit/low_limit", "CNY_per_share",
             status="verified", evidence="medians 15.0/11.45 at price scale",
             note="repurchase price band, not amounts"),
    UnitRule("events.parquet", "share_float_complete", "float_share", "shares",
             status="verified",
             evidence="float_share/(float_ratio%) matches daily_basic total share capital",
             note="NOT 10k shares: 386-share unlock rows exist and reconcile only as shares"),
    UnitRule("events.parquet", "share_float_complete", "float_ratio", "percent"),
    UnitRule("events.parquet", "top_list", "amount/l_buy/l_sell/net_amount and buy/sell fields", "CNY"),
    UnitRule("events.parquet", "top_list", "net_rate/amount_rate/turnover_rate", "percent"),
    UnitRule("events.parquet", "top_inst", "buy/sell/net_buy", "CNY"),
    UnitRule("events.parquet", "top_inst", "buy_rate/sell_rate", "percent"),
    UnitRule("events.parquet", "kpl_list", "amount/free_float/limit_order/lu_limit_order", "CNY",
             status="official", note="mostly CNY-level amounts from source"),
    UnitRule("events.parquet", "kpl_concept_cons", "hot_num", "source_rank_score"),
    UnitRule("events.parquet", "dc_index", "total_mv", "10k_CNY",
             status="inferred", evidence="concept aggregates only plausible at 10k-CNY scale"),
    UnitRule("events.parquet", "dc_index", "pct_change/leading_pct/turnover_rate", "percent"),
    UnitRule("events.parquet", "dc_index", "up_num/down_num", "count"),
    UnitRule("events.parquet", "dc_member", "membership mapping", "text", note="no numeric unit"),
    UnitRule("events.parquet", "limit_step", "nums", "count", note="consecutive-limit count label"),
    UnitRule("events.parquet", "limit_cpt_list", "up_nums/cons_nums", "count"),
    UnitRule("events.parquet", "limit_cpt_list", "pct_chg", "percent"),
    UnitRule("events.parquet", "limit_list_ths", "price/current monetary fields", "CNY",
             status="official", note="pct_chg/turnover/rise_rate style fields are percent"),
    UnitRule("events.parquet", "ths_hot", "rank/hot", "source_rank_score",
             note="pct_change is percent; current_price is CNY for A-share rows"),
    UnitRule("events.parquet", "dc_hot", "rank/hot", "source_rank_score",
             note="pct_change is percent; current_price is CNY for A-share rows"),
    UnitRule("events.parquet", "hm_detail", "buy_amount/sell_amount/net_amount", "CNY"),
    UnitRule("events.parquet", "hm_list", "reference metadata", "text", note="no numeric unit"),
    # ------------------------- macro.parquet, by dataset --------------------
    UnitRule("macro.parquet", "cn_gdp", "gdp and industry value fields", "100m_CNY",
             note="*_yoy fields are percent"),
    UnitRule("macro.parquet", "cn_cpi", "index levels", "official_index",
             note="mom/yoy/accumulated percent fields by column suffix"),
    UnitRule("macro.parquet", "cn_ppi", "index levels", "official_index",
             note="mom/yoy/accumulated percent fields by column suffix"),
    UnitRule("macro.parquet", "cn_pmi", "pmi fields", "diffusion_index"),
    UnitRule("macro.parquet", "cn_m", "m0/m1/m2", "100m_CNY", note="*_yoy and *_mom are percent"),
    UnitRule("macro.parquet", "sf_month", "social financing flows/stocks", "100m_CNY"),
    UnitRule("macro.parquet", "shibor", "rate columns", "percent"),
    UnitRule("macro.parquet", "shibor_quote", "bid/ask columns", "percent"),
    UnitRule("macro.parquet", "shibor_lpr", "rate columns", "percent"),
    UnitRule("macro.parquet", "monetary_policy", "text/PDF evidence", "text"),
    UnitRule("macro.parquet", "eco_cal", "actual/previous/forecast", "heterogeneous_by_event",
             note="must not be pooled without event-specific parsing"),
    UnitRule("macro.parquet", "index_global", "OHLC", "index_points",
             note="vol/amount availability varies by market and source"),
    UnitRule("macro.parquet", "index_daily", "OHLC", "index_points"),
    UnitRule("macro.parquet", "index_daily", "pct_chg", "percent",
             note="5%=5.0 — do not multiply by 100 again"),
    UnitRule("macro.parquet", "index_daily", "vol", "hands"),
    UnitRule("macro.parquet", "index_daily", "amount", "thousand_CNY"),
    UnitRule("macro.parquet", "index_dailybasic", "total_mv/float_mv", "CNY",
             status="verified", evidence="CSI300 total_mv ~1e13 only plausible in CNY",
             note="CNY here vs 10k CNY in daily_basic — do not mix scales"),
    UnitRule("macro.parquet", "index_dailybasic", "total_share/float_share/free_share", "shares",
             status="verified", evidence="~1e11 share scale",
             note="shares here vs 10k shares in daily_basic"),
    UnitRule("macro.parquet", "index_dailybasic", "turnover_rate(_f)/pe(_ttm)/pb", "percent_or_ratio"),
    UnitRule("macro.parquet", "sw_daily", "OHLC", "index_points"),
    UnitRule("macro.parquet", "sw_daily", "vol", "10k_shares", status="official"),
    UnitRule("macro.parquet", "sw_daily", "amount", "10k_CNY", status="official"),
    UnitRule("macro.parquet", "ci_daily", "OHLC", "index_points",
             note="vol/amount follow the sw_daily 10k shares / 10k CNY convention"),
    UnitRule("macro.parquet", "daily_info", "total_share/float_share/vol", "100m_shares",
             status="verified", evidence="exchange-level medians only plausible in 100m units"),
    UnitRule("macro.parquet", "daily_info", "total_mv/float_mv/amount", "100m_CNY",
             status="verified", evidence="exchange turnover ~3.9e3 == 390b CNY",
             note="SSE stats in 100m units vs sz_daily_info in CNY — do not mix"),
    UnitRule("macro.parquet", "sz_daily_info", "amount/total_mv/float_mv", "CNY",
             status="verified", evidence="SZSE daily turnover ~6.9e10 == 69b CNY"),
    UnitRule("macro.parquet", "moneyflow_mkt_dc", "net_amount/buy_*_amount", "CNY",
             status="verified", evidence="market-wide flows ~ -4.5e10 plausible only in CNY"),
    UnitRule("macro.parquet", "broker_recommend", "monthly broker lists", "text"),
    UnitRule("macro.parquet", "ths_daily", "OHLC", "index_points",
             note="avg_price is CNY per share; turnover_rate/pct_change are percent"),
    UnitRule("macro.parquet", "ths_daily", "vol", "source_volume_units",
             status="inferred", note="use comparatively; do not mix with stock volumes"),
    UnitRule("macro.parquet", "fx_daily", "bid/ask quotes", "quote_price",
             note="tick_qty is quote/tick count, not stock volume"),
    UnitRule("macro.parquet", "repo_daily", "price/rate/amount", "official_raw",
             note="normalize before cross-asset factor use"),
    UnitRule("macro.parquet", "us_tycr", "yields", "percent"),
    UnitRule("macro.parquet", "us_trycr", "yields", "percent"),
    UnitRule("macro.parquet", "fut_basic", "per_unit", "units_per_lot_multiplier"),
    UnitRule("macro.parquet", "fut_mapping", "contract mapping", "text"),
    UnitRule("macro.parquet", "fut_daily", "prices", "contract_quote_units",
             note="index points for CFFEX; multiplier from fut_basic converts to notional"),
    UnitRule("macro.parquet", "fut_daily", "vol/oi", "lots"),
    UnitRule("macro.parquet", "fut_daily", "amount", "10k_CNY"),
    UnitRule("macro.parquet", "opt_basic", "per_unit/min_price_chg/exercise_price",
             "contract_units", note="exercise_price shares the underlying quote unit"),
    UnitRule("macro.parquet", "opt_daily", "prices", "premium_quote_units"),
    UnitRule("macro.parquet", "opt_daily", "vol/oi", "contracts"),
    UnitRule("macro.parquet", "opt_daily", "amount", "10k_CNY"),
    UnitRule("macro.parquet", "cb_basic", "par/issue_price", "CNY_per_100_par"),
    UnitRule("macro.parquet", "cb_basic", "issue_size/remain_size", "CNY",
             status="verified", evidence="issue_size ~7.5e8 at bond-issue scale"),
    UnitRule("macro.parquet", "cb_basic", "coupon_rate", "percent"),
    UnitRule("macro.parquet", "cb_daily", "prices", "CNY_per_100_par"),
    UnitRule("macro.parquet", "cb_daily", "vol", "lots"),
    UnitRule("macro.parquet", "cb_daily", "amount", "10k_CNY"),
    UnitRule("macro.parquet", "cb_daily", "bond_over_rate/cb_over_rate", "percent"),
    UnitRule("macro.parquet", "cb_call", "call_price/call_price_tax", "CNY_per_100_par"),
    UnitRule("macro.parquet", "cb_call", "call_vol", "bonds",
             status="verified", evidence="call_vol*call_price reconciles with call_amount in 10k CNY"),
    UnitRule("macro.parquet", "cb_call", "call_amount", "10k_CNY",
             status="verified", evidence="reconciles with call_vol at per-100-par prices"),
    UnitRule("macro.parquet", "yc_cb", "yield", "percent",
             note="curve_term is years; curve_type 0=YTM, 1=spot"),
    # --------- macro specs outside the default snapshot set (raw lake) ------
    UnitRule("macro.parquet", "cn_schedule", "release schedule", "text", agent_visible=False,
             note="not in the default snapshot macro set"),
    UnitRule("macro.parquet", "hibor", "rate columns", "percent", agent_visible=False,
             note="not in the default snapshot macro set"),
    UnitRule("macro.parquet", "libor", "rate columns", "percent", agent_visible=False,
             note="not in the default snapshot macro set"),
    UnitRule("macro.parquet", "us_tbr", "rate columns", "percent", agent_visible=False,
             note="not in the default snapshot macro set"),
    UnitRule("macro.parquet", "us_tltr", "rate columns", "percent", agent_visible=False,
             note="not in the default snapshot macro set"),
    # ---------------------- fundamentals.parquet, by dataset ----------------
    UnitRule("fundamentals.parquet", "income_vip", "amount fields", "CNY",
             note="unless the field is explicitly per-share or ratio"),
    UnitRule("fundamentals.parquet", "balancesheet_vip", "amount fields", "CNY"),
    UnitRule("fundamentals.parquet", "cashflow_vip", "amount fields", "CNY"),
    UnitRule("fundamentals.parquet", "fina_indicator_vip", "eps/bps and per-share fields", "CNY_per_share"),
    UnitRule("fundamentals.parquet", "fina_indicator_vip", "roe and ratio fields", "percent",
             note="mixed table; handle by field family, some fields are CNY amounts"),
    UnitRule("fundamentals.parquet", "forecast_vip", "net_profit_min/net_profit_max", "10k_CNY",
             note="must not be mixed directly with statement net profit in CNY"),
    UnitRule("fundamentals.parquet", "express_vip", "revenue/profit/asset fields", "CNY",
             status="verified", evidence="revenue median 4.9e9 at CNY scale"),
    UnitRule("fundamentals.parquet", "dividend", "cash_div/cash_div_tax", "CNY_per_share",
             status="verified", evidence="median 0.095 per share"),
    UnitRule("fundamentals.parquet", "dividend", "base_share", "10k_shares",
             status="official", note="present only on some records"),
    UnitRule("fundamentals.parquet", "fina_audit", "audit_fees", "CNY",
             status="verified", evidence="median 4e5 at audit-fee scale"),
    UnitRule("fundamentals.parquet", "fina_mainbz_vip", "bz_sales/bz_profit/bz_cost", "CNY",
             status="verified", evidence="segment revenue median 1.3e7 at CNY scale"),
    UnitRule("fundamentals.parquet", "disclosure_date", "dates only", "text"),
    # ---------------- raw-lake datasets never included in snapshots ---------
    UnitRule("raw_only", "bak_basic", "float_share/total_share", "100m_shares",
             agent_visible=False, note="no volume or amount fields"),
    UnitRule("raw_only", "bak_basic", "total_assets/liquid_assets/fixed_assets", "100m_CNY",
             agent_visible=False, note="coarse company snapshot fields; supplemental use only"),
)


def rules_for(file: str | None = None, datasets: tuple[str, ...] | None = None) -> tuple[UnitRule, ...]:
    """Registry selection by file and/or exact dataset ids."""
    selected = UNIT_RULES
    if file is not None:
        selected = tuple(rule for rule in selected if rule.file == file)
    if datasets is not None:
        wanted = set(datasets)
        selected = tuple(rule for rule in selected if rule.dataset in wanted)
    return selected


def rules_text(rules: tuple[UnitRule, ...]) -> dict[str, str]:
    """Human/report-facing projection: qualified key -> one-line rule."""
    rendered: dict[str, str] = {}
    for rule in rules:
        text = rule.source_unit
        if rule.factor is not None:
            text += f"; snapshot multiplies by {rule.factor:g} -> {rule.normalized_unit}"
        if rule.note:
            text += f" — {rule.note}"
        text += f" [{rule.status}]"
        rendered[rule.key()] = text
    return rendered


def dataset_rules_text(file: str, datasets: tuple[str, ...]) -> dict[str, str]:
    """Fail-fast text projection: every requested dataset must carry a rule."""
    rules = rules_for(file=file, datasets=datasets)
    missing = sorted(set(datasets) - {rule.dataset for rule in rules})
    if missing:
        raise KeyError(f"unit registry has no {file} rules for datasets: {missing}")
    return rules_text(rules)


def column_source_units(file: str) -> dict[str, str]:
    """Exact column -> source unit for rules that enumerate their columns."""
    return {column: rule.source_unit for rule in rules_for(file) for column in rule.columns}


def registry_datasets() -> set[str]:
    return {rule.dataset for rule in UNIT_RULES if rule.dataset}


def _conversions(file: str) -> tuple[tuple[str, float, str], ...]:
    conversions: list[tuple[str, float, str]] = []
    for rule in rules_for(file):
        if rule.factor is None:
            continue
        for column in rule.columns:
            conversions.append((column, rule.factor, f"{rule.source_unit}->{rule.normalized_unit}"))
    return tuple(conversions)


# Derived byte-conversion tables (single-sourced from the registry).
DAILY_UNIT_CONVERSIONS: tuple[tuple[str, float, str], ...] = _conversions("daily.parquet")
AUCTION_UNIT_CONVERSIONS: tuple[tuple[str, float, str], ...] = _conversions("auction.parquet")

def _normalized_units(file: str) -> dict[str, str]:
    """Post-conversion unit per field family, as the Agent sees the file."""
    return {
        rule.fields: rule.normalized_unit if rule.factor is not None else rule.source_unit
        for rule in rules_for(file)
    }


# Compact Agent-facing contract shipped via data_summary.json. Heterogeneous
# union fields are identified by (file, dataset, column pattern), never by
# column name alone.
AGENT_UNIT_CONTRACT: dict[str, object] = {
    "identity_rule": "interpret units by file + dataset + column; never by column name alone",
    "coverage": {
        "normalized_files": "complete file-level unit families and conversion factors",
        "source_unions": "family-level registry below; not an exhaustive copy of every upstream field",
    },
    "daily.parquet": {
        "mode": "normalized",
        "units": _normalized_units("daily.parquet"),
        "percent_family_note": "decimal after load; 5%=0.05; -9.5%=-0.095",
        "conversion_factors": {column: factor for column, factor, _ in DAILY_UNIT_CONVERSIONS},
    },
    "auction.parquet": {
        "mode": "normalized",
        "units": _normalized_units("auction.parquet"),
        "conversion_factors": {column: factor for column, factor, _ in AUCTION_UNIT_CONVERSIONS},
    },
    "intraday_1min.parquet": {
        "mode": "source_equals_normalized",
        "units": _normalized_units("intraday_1min.parquet"),
    },
    "events.parquet": {"mode": "source_by_dataset", "rules": "see source_unit_rules"},
    "macro.parquet": {"mode": "source_by_dataset", "rules": "see source_unit_rules"},
    "fundamentals.parquet": {"mode": "source_by_dataset", "rules": "see source_unit_rules"},
    # Structured registry records (dataset-qualified); offline Fold Agents
    # resolve source units from here without host access.
    "source_unit_rules": [rule.to_record() for rule in UNIT_RULES if rule.agent_visible],
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
