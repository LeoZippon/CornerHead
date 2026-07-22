"""Column-level unit registry and snapshot unit normalization.

``FIELD_RULES`` is the single source of truth for the unit and semantic type
of every column that can appear in a snapshot file. A rule targets explicit
column names or fnmatch globs under an exact ``(file, dataset)`` key — never a
free-text field family — so coverage and overlap are machine-checkable.

Resolution order for one ``(file, dataset, column)``:

1. the dataset's (or, for single-schema files, the file's) explicit rules —
   at most one may match, otherwise the registry is broken;
2. ``COMMON_FIELD_SEMANTICS`` — shared identifier/date/text/categorical
   classifiers that mean the same thing in every dataset;
3. the dataset's default rule (``columns=("*",)``), allowed only where the
   vendor contract is uniform (financial-statement amounts);
4. otherwise ``UnresolvedUnitError`` — an unclassified column fails the
   snapshot build instead of shipping without unit metadata.

Projections (never maintained by hand elsewhere): the snapshot conversion
tables and ``DatasetContract.unit_rules`` derive from factor rules; audit
report metadata selects by each domain's dataset list; ``unit_reference.json``
(written next to data_summary.json) enumerates every column of the live
snapshot; ``docs/units_reference.md`` renders the registry for humans.

``status`` discipline: ``verified`` = reconciled against another dataset or a
known external truth (named in ``evidence``); ``official`` = vendor field
contract; ``inferred`` = local-magnitude plausibility only; ``unknown`` =
honestly unresolved — such columns must not enter thresholds or cross-dataset
arithmetic. Observed value ranges validate a rule but never define one.
"""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase
from functools import lru_cache
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq


class UnresolvedUnitError(ValueError):
    """A snapshot column has no unit/semantic classification."""


@dataclass(frozen=True)
class FieldRule:
    file: str  # snapshot parquet name, or "raw_only" for never-snapshotted lake datasets
    dataset: str | None  # exact snapshot dataset id; None for single-schema files
    columns: tuple[str, ...]  # exact names or fnmatch globs; ("*",) = dataset default tier
    semantic: str = "numeric"  # numeric | identifier | datetime | text | categorical
    source_unit: str | None = None  # required for numeric ("unknown" when unresolved)
    factor: float | None = None  # snapshot-load multiplier (exact column names only)
    normalized_unit: str | None = None
    status: str = "official"  # verified | official | inferred | unknown
    evidence: str = ""
    note: str = ""

    def key(self) -> str:
        return f"{self.file}:{self.dataset or ''}:{'/'.join(self.columns)}"

    def to_record(self) -> dict[str, object]:
        record: dict[str, object] = {
            "file": self.file,
            "dataset": self.dataset,
            "columns": "/".join(self.columns),
            "semantic_type": self.semantic,
            "source_unit": self.source_unit,
            "status": self.status,
        }
        for field in ("factor", "normalized_unit", "evidence", "note"):
            value = getattr(self, field)
            if value not in (None, ""):
                record[field] = value
        return record


# Columns that carry the same meaning in every dataset. Ordered; first match
# wins. Dataset rules always take precedence over these.
COMMON_FIELD_SEMANTICS: tuple[tuple[str, str], ...] = (
    ("available_at_rule", "categorical"),
    ("available_at", "datetime"),
    ("available_month", "datetime"),
    ("dataset", "identifier"),
    ("ts_code", "identifier"),
    ("ts_codes", "identifier"),
    ("con_code", "identifier"),
    ("sub_code", "identifier"),
    ("cb_code", "identifier"),
    ("stk_code", "identifier"),
    ("opt_code", "identifier"),
    ("mapping_ts_code", "identifier"),
    ("leading_code", "identifier"),
    ("bz_code", "identifier"),
    ("l1_code", "identifier"),
    ("pcode", "identifier"),
    ("symbol", "identifier"),
    ("text_id", "identifier"),
    ("library_file", "identifier"),
    ("source_hash", "identifier"),
    ("source_path", "identifier"),
    ("source_row_id", "identifier"),
    ("business_key", "identifier"),
    ("download_path", "identifier"),
    ("source_file", "identifier"),
    ("source_cap_risk", "categorical"),
    ("url", "identifier"),
    ("pdf_url", "identifier"),
    ("update_flag", "categorical"),
    ("curr_type", "categorical"),
    ("session", "categorical"),
    ("type", "categorical"),
    ("*_type", "categorical"),
    ("call_put", "categorical"),
    ("fut_code", "identifier"),
    ("?_month", "categorical"),  # futures/options delivery month codes
    ("*_date", "datetime"),
    ("*_ddate", "datetime"),
    ("*_edate", "datetime"),
    ("div_listdate", "datetime"),
    ("date", "datetime"),
    ("datetime", "datetime"),
    ("month", "datetime"),
    ("quarter", "datetime"),
    ("*_time", "datetime"),
    ("time", "datetime"),
    ("maturity_date", "datetime"),
    ("*_name", "text"),
    ("name", "text"),
    ("ts_name", "text"),
    ("*_title", "text"),
    ("title", "text"),
    ("content*", "text"),
    ("*_desc", "text"),
    ("desc", "text"),
    ("*_reason", "text"),
    ("reason", "text"),
    ("*_summary", "text"),
    ("summary", "text"),
    ("exchange", "categorical"),
    ("exchange_id", "categorical"),
    ("market", "categorical"),
    ("side", "categorical"),
    ("status", "categorical"),
    ("proc", "categorical"),
    ("div_proc", "categorical"),
    ("in_de", "categorical"),
    ("is_*", "categorical"),
    ("up_stat", "text"),
    ("tag", "text"),
    ("theme", "text"),
    ("concept", "text"),
    ("industry", "text"),
    ("area", "text"),
    ("lead_stock", "text"),
    ("exalter", "text"),
    ("orgs", "text"),
    ("hm_orgs", "text"),
    ("pledgor", "text"),
    ("rece_*", "text"),
    ("comp_rece", "text"),
    ("bank", "text"),
    ("broker", "text"),
    ("country", "categorical"),
    ("currency", "categorical"),
    ("event", "text"),
    ("buyer", "text"),
    ("seller", "text"),
    ("*_clause", "text"),
    ("guarantor", "text"),
    ("*_rating", "text"),
    ("rating*", "text"),
    ("audit_result", "text"),
    ("audit_agency", "text"),
    ("audit_sign", "text"),
    ("bz_item", "text"),
    ("author*", "text"),
    ("classify", "text"),
    ("imp_dg", "text"),
    ("inst_csname", "text"),
    ("channels", "text"),
    ("src", "categorical"),
    ("puborg", "text"),
    ("q", "text"),
    ("a", "text"),
    ("trade_unit", "text"),
    ("quote_unit", "text"),
    ("leading", "text"),
)


FIELD_RULES: tuple[FieldRule, ...] = (
    # ======================= daily.parquet (normalized) =====================
    FieldRule("daily.parquet", None,
              ("open", "high", "low", "close", "pre_close", "change", "close_basic",
               "pre_close_limit", "up_limit", "down_limit"),
              source_unit="CNY_per_share",
              status="verified", evidence="matches auction and minute price scales"),
    FieldRule("daily.parquet", None, ("vol",), source_unit="hands", factor=100.0,
              normalized_unit="shares",
              status="verified", evidence="cross-checked against stk_mins share volume"),
    FieldRule("daily.parquet", None, ("amount",), source_unit="thousand_CNY", factor=1000.0,
              normalized_unit="CNY",
              status="verified", evidence="price*volume reconciliation"),
    FieldRule("daily.parquet", None,
              ("pct_chg", "turnover_rate", "turnover_rate_f", "dv_ratio", "dv_ttm"),
              source_unit="percent", factor=0.01, normalized_unit="decimal",
              note="5% arrives as 5.0; snapshot stores 0.05"),
    FieldRule("daily.parquet", None, ("total_share", "float_share", "free_share"),
              source_unit="10k_shares", factor=10_000.0, normalized_unit="shares",
              status="verified", evidence="back-calculated from share_float unlock ratios"),
    FieldRule("daily.parquet", None, ("total_mv", "circ_mv"),
              source_unit="10k_CNY", factor=10_000.0, normalized_unit="CNY",
              status="verified", evidence="price*shares reconciliation"),
    FieldRule("daily.parquet", None, ("volume_ratio", "pe", "pe_ttm", "pb", "ps", "ps_ttm"),
              source_unit="multiple", note="dimensionless valuation/liquidity multiples"),
    FieldRule("daily.parquet", None, ("adj_factor",), source_unit="dimensionless_ratio"),
    FieldRule("daily.parquet", None, ("is_suspended",), semantic="categorical"),
    # ================== intraday_1min.parquet (source==normalized) ==========
    FieldRule("intraday_1min.parquet", None, ("open", "high", "low", "close"),
              source_unit="CNY_per_share"),
    FieldRule("intraday_1min.parquet", None, ("vol", "vol_pit"), source_unit="shares"),
    FieldRule("intraday_1min.parquet", None, ("amount", "amount_pit"), source_unit="CNY"),
    FieldRule("intraday_1min.parquet", None,
              ("auction_vol_correction_factor", "auction_amount_correction_factor"),
              source_unit="dimensionless_ratio"),
    FieldRule("intraday_1min.parquet", None,
              ("auction_market_bucket", "auction_open_bar", "auction_correction_rule"),
              semantic="categorical"),
    # ======================= auction.parquet (normalized) ===================
    FieldRule("auction.parquet", None, ("price", "pre_close"), source_unit="CNY_per_share"),
    FieldRule("auction.parquet", None, ("vol",), source_unit="shares"),
    FieldRule("auction.parquet", None, ("amount",), source_unit="CNY"),
    FieldRule("auction.parquet", None, ("turnover_rate",), source_unit="percent",
              factor=0.01, normalized_unit="decimal",
              status="verified", evidence="single-day smoke check vs daily turnover"),
    FieldRule("auction.parquet", None, ("volume_ratio",), source_unit="multiple",
              note="1.2 = 1.2x"),
    FieldRule("auction.parquet", None, ("float_share",), source_unit="10k_shares",
              factor=10_000.0, normalized_unit="shares",
              status="verified", evidence="same source scale as daily_basic.float_share"),
    # ================ corporate_actions.parquet (replay slots) ==============
    FieldRule("corporate_actions.parquet", None, ("cash_per_share",), source_unit="CNY_per_share"),
    FieldRule("corporate_actions.parquet", None, ("stock_per_share",),
              source_unit="shares_per_share", note="bonus/transfer shares per held share"),
    # ======================= events.parquet, by dataset =====================
    FieldRule("events.parquet", "margin", ("rzye", "rzmre", "rzche", "rqye", "rzrqye"),
              source_unit="CNY"),
    FieldRule("events.parquet", "margin", ("rqyl", "rqmcl"), source_unit="shares",
              note="securities-lending quantities"),
    FieldRule("events.parquet", "margin_detail", ("rzye", "rzmre", "rzche", "rqye", "rzrqye"),
              source_unit="CNY"),
    FieldRule("events.parquet", "margin_detail", ("rqyl", "rqmcl", "rqchl"),
              source_unit="shares"),
    # margin_secs is an eligibility table: identifiers only, no numeric fields.
    FieldRule("events.parquet", "moneyflow",
              ("buy_*_vol", "sell_*_vol", "net_mf_vol"), source_unit="hands"),
    FieldRule("events.parquet", "moneyflow",
              ("buy_*_amount", "sell_*_amount", "net_mf_amount"), source_unit="10k_CNY",
              note="500 means CNY 5m; normalize before mixing with daily/stk_mins"),
    FieldRule("events.parquet", "moneyflow_dc", ("close",), source_unit="CNY_per_share",
              status="verified", evidence="median 11.7 at stock price scale"),
    FieldRule("events.parquet", "moneyflow_dc", ("pct_change", "*_rate"), source_unit="percent"),
    FieldRule("events.parquet", "moneyflow_dc", ("net_amount", "buy_*_amount"),
              source_unit="10k_CNY",
              status="inferred", evidence="per-stock medians at 10k-CNY scale only"),
    FieldRule("events.parquet", "moneyflow_ths", ("latest",), source_unit="CNY_per_share",
              status="verified", evidence="median 12.8 at stock price scale"),
    FieldRule("events.parquet", "moneyflow_ths", ("pct_change", "*_rate"), source_unit="percent"),
    FieldRule("events.parquet", "moneyflow_ths",
              ("net_amount", "net_d5_amount", "buy_*_amount"), source_unit="10k_CNY",
              status="inferred", evidence="per-stock medians at 10k-CNY scale only"),
    FieldRule("events.parquet", "moneyflow_ind_dc", ("close",), source_unit="index_points",
              status="inferred", evidence="median 1941 at board index scale"),
    FieldRule("events.parquet", "moneyflow_ind_dc", ("pct_change", "*_rate"),
              source_unit="percent"),
    FieldRule("events.parquet", "moneyflow_ind_dc", ("net_amount", "buy_*_amount"),
              source_unit="CNY",
              status="verified", evidence="industry medians only plausible in CNY (1.5e7 ~ 15m)",
              note="industry-level DC flows are CNY, unlike stock-level moneyflow_dc in 10k CNY"),
    FieldRule("events.parquet", "moneyflow_ind_dc", ("rank",), source_unit="rank"),
    FieldRule("events.parquet", "moneyflow_ind_dc", ("buy_sm_amount_stock",), semantic="text",
              note="name of the top small-order-inflow stock"),
    FieldRule("events.parquet", "moneyflow_ind_ths", ("close",), source_unit="index_points",
              status="inferred"),
    FieldRule("events.parquet", "moneyflow_ind_ths", ("pct_change", "pct_change_stock"),
              source_unit="percent"),
    FieldRule("events.parquet", "moneyflow_ind_ths", ("close_price",),
              source_unit="CNY_per_share", note="leading stock price"),
    FieldRule("events.parquet", "moneyflow_ind_ths", ("company_num",), source_unit="count"),
    FieldRule("events.parquet", "moneyflow_ind_ths",
              ("net_buy_amount", "net_sell_amount", "net_amount"), source_unit="100m_CNY",
              status="verified", evidence="industry medians ~49.5 only plausible as 100m CNY"),
    FieldRule("events.parquet", "moneyflow_cnt_ths", ("industry_index",),
              source_unit="index_points",
              status="inferred", evidence="values ~3000 at concept index scale"),
    FieldRule("events.parquet", "moneyflow_cnt_ths", ("pct_change", "pct_change_stock"),
              source_unit="percent"),
    FieldRule("events.parquet", "moneyflow_cnt_ths", ("close_price",),
              source_unit="CNY_per_share", note="leading stock price"),
    FieldRule("events.parquet", "moneyflow_cnt_ths", ("company_num",), source_unit="count"),
    FieldRule("events.parquet", "moneyflow_cnt_ths",
              ("net_buy_amount", "net_sell_amount", "net_amount"), source_unit="100m_CNY",
              status="verified", evidence="concept medians ~162 only plausible as 100m CNY"),
    FieldRule("events.parquet", "cyq_perf",
              ("his_low", "his_high", "cost_5pct", "cost_15pct", "cost_50pct",
               "cost_85pct", "cost_95pct", "weight_avg"),
              source_unit="CNY_per_share",
              status="verified", evidence="cost percentiles sit at price scale",
              note="cost_5pct is the 5th-percentile holder cost PRICE, not a percent"),
    FieldRule("events.parquet", "cyq_perf", ("winner_rate",), source_unit="percent"),
    FieldRule("events.parquet", "bak_daily",
              ("open", "high", "low", "close", "pre_close", "change", "avg_price"),
              source_unit="CNY_per_share"),
    FieldRule("events.parquet", "bak_daily", ("pct_change", "turn_over", "swing"),
              source_unit="percent"),
    FieldRule("events.parquet", "bak_daily", ("vol",), source_unit="hands",
              status="verified", evidence="ratio to daily.vol == 1.0"),
    FieldRule("events.parquet", "bak_daily", ("amount",), source_unit="10k_CNY",
              status="verified", evidence="x10 matches daily.amount (thousand CNY)"),
    FieldRule("events.parquet", "bak_daily", ("selling", "buying"), source_unit="hands",
              status="inferred", evidence="selling+buying ~ vol"),
    FieldRule("events.parquet", "bak_daily", ("total_share", "float_share"),
              source_unit="100m_shares",
              status="verified", evidence="x10^4 matches daily_basic share fields",
              note="multiply by 10000 before comparing with daily_basic"),
    FieldRule("events.parquet", "bak_daily", ("total_mv", "float_mv"),
              source_unit="100m_CNY",
              status="verified", evidence="x10^4 matches daily_basic market values"),
    FieldRule("events.parquet", "bak_daily", ("pe", "vol_ratio"), source_unit="multiple"),
    FieldRule("events.parquet", "bak_daily", ("strength", "activity", "attack"),
              source_unit="vendor_score", status="inferred",
              note="opaque vendor composite indicators"),
    FieldRule("events.parquet", "bak_daily", ("avg_turnover", "interval_3", "interval_6"),
              source_unit="unknown", status="unknown",
              note="all-NA locally; resolve before use"),
    FieldRule("events.parquet", "stk_premarket", ("total_share", "float_share"),
              source_unit="10k_shares",
              status="verified", evidence="same scale as daily_basic share fields"),
    FieldRule("events.parquet", "stk_premarket", ("pre_close", "up_limit", "down_limit"),
              source_unit="CNY_per_share"),
    FieldRule("events.parquet", "slb_len",
              ("ob", "auc_amount", "repo_amount", "repay_amount", "cb"),
              source_unit="unknown", status="unknown",
              note="no local rows yet; verify on first landed partition"),
    FieldRule("events.parquet", "slb_len_mm", ("ope_inv", "lent_qnt", "cls_inv", "end_bal"),
              source_unit="unknown", status="unknown",
              note="no local rows yet; verify on first landed partition"),
    FieldRule("events.parquet", "block_trade", ("price",), source_unit="CNY_per_share",
              status="verified", evidence="price*vol == amount"),
    FieldRule("events.parquet", "block_trade", ("vol",), source_unit="10k_shares",
              status="verified", evidence="price*vol == amount"),
    FieldRule("events.parquet", "block_trade", ("amount",), source_unit="10k_CNY",
              status="verified", evidence="price*vol == amount",
              note="sparse; zero-row dates expected"),
    FieldRule("events.parquet", "stk_holdernumber", ("holder_num",), source_unit="count"),
    FieldRule("events.parquet", "top10_holders", ("hold_amount", "hold_change"),
              source_unit="shares",
              status="verified", evidence="hold_amount/(hold_ratio%) matches total share capital"),
    FieldRule("events.parquet", "top10_holders", ("hold_ratio", "hold_float_ratio"),
              source_unit="percent"),
    FieldRule("events.parquet", "top10_floatholders", ("hold_amount", "hold_change"),
              source_unit="shares",
              status="verified", evidence="same reconciliation as top10_holders"),
    FieldRule("events.parquet", "top10_floatholders", ("hold_ratio", "hold_float_ratio"),
              source_unit="percent"),
    FieldRule("events.parquet", "pledge_detail",
              ("pledge_amount", "holding_amount", "pledged_amount"),
              source_unit="10k_shares",
              status="verified", evidence="ratios reconcile at 10k-share scale"),
    FieldRule("events.parquet", "pledge_detail", ("p_total_ratio", "h_total_ratio"),
              source_unit="percent"),
    FieldRule("events.parquet", "stk_surv", ("fund_visitors",), source_unit="count",
              note="participating institutions"),
    FieldRule("events.parquet", "new_share", ("price",), source_unit="CNY_per_share"),
    FieldRule("events.parquet", "new_share", ("pe",), source_unit="multiple",
              status="verified", evidence="median 15 at issue-PE scale"),
    FieldRule("events.parquet", "new_share", ("amount", "market_amount", "limit_amount"),
              source_unit="10k_shares"),
    FieldRule("events.parquet", "new_share", ("funds",), source_unit="100m_CNY",
              status="inferred", evidence="median 4.1 at IPO-proceeds scale only"),
    FieldRule("events.parquet", "new_share", ("ballot",), source_unit="percent",
              note="0.03 means 0.03%"),
    FieldRule("events.parquet", "stk_holdertrade",
              ("change_vol", "after_share", "total_share"), source_unit="shares",
              status="verified", evidence="after_share/total_share at holder position scale",
              note="total_share is the holder's post-trade position, not company capital"),
    FieldRule("events.parquet", "stk_holdertrade", ("change_ratio", "after_ratio"),
              source_unit="percent"),
    FieldRule("events.parquet", "stk_holdertrade", ("avg_price",), source_unit="CNY_per_share",
              status="verified", evidence="median 20.6 at price scale"),
    FieldRule("events.parquet", "repurchase", ("vol",), source_unit="shares",
              status="verified", evidence="amount/vol sits at price scale"),
    FieldRule("events.parquet", "repurchase", ("amount",), source_unit="CNY"),
    FieldRule("events.parquet", "repurchase", ("high_limit", "low_limit"),
              source_unit="CNY_per_share",
              status="verified", evidence="medians 15.0/11.45 at price scale",
              note="repurchase price band, not amounts"),
    FieldRule("events.parquet", "share_float_complete", ("float_share",),
              source_unit="shares", status="verified",
              evidence="float_share/(float_ratio%) matches daily_basic total share capital",
              note="NOT 10k shares: 386-share unlock rows exist and reconcile only as shares"),
    FieldRule("events.parquet", "share_float_complete", ("float_ratio",),
              source_unit="percent"),
    FieldRule("events.parquet", "top_list", ("close",), source_unit="CNY_per_share"),
    FieldRule("events.parquet", "top_list",
              ("pct_change", "turnover_rate", "net_rate", "amount_rate"),
              source_unit="percent"),
    FieldRule("events.parquet", "top_list",
              ("amount", "l_sell", "l_buy", "l_amount", "net_amount"), source_unit="CNY"),
    FieldRule("events.parquet", "top_list", ("float_values",), source_unit="CNY",
              status="verified", evidence="median 6e9 at float-market-value scale"),
    FieldRule("events.parquet", "top_inst", ("buy", "sell", "net_buy"), source_unit="CNY"),
    FieldRule("events.parquet", "top_inst", ("buy_rate", "sell_rate"), source_unit="percent"),
    FieldRule("events.parquet", "kpl_list",
              ("pct_chg", "bid_pct_chg", "rt_pct_chg", "turnover_rate"),
              source_unit="percent"),
    FieldRule("events.parquet", "kpl_list",
              ("amount", "net_change", "free_float", "limit_order", "lu_limit_order"),
              source_unit="CNY",
              status="inferred", evidence="medians (7.4e8 amount, 6.1e9 free float) at CNY scale"),
    FieldRule("events.parquet", "kpl_list",
              ("bid_amount", "bid_change", "bid_turnover", "lu_bid_vol"),
              source_unit="unknown", status="unknown",
              note="all-NA locally; resolve before use"),
    FieldRule("events.parquet", "kpl_concept_cons", ("hot_num",), source_unit="vendor_score"),
    FieldRule("events.parquet", "dc_index",
              ("pct_change", "leading_pct", "turnover_rate"), source_unit="percent"),
    FieldRule("events.parquet", "dc_index", ("total_mv",), source_unit="10k_CNY",
              status="inferred", evidence="concept aggregates only plausible at 10k-CNY scale"),
    FieldRule("events.parquet", "dc_index", ("up_num", "down_num"), source_unit="count"),
    FieldRule("events.parquet", "dc_index", ("level",), semantic="categorical"),
    # dc_member is a membership mapping: identifiers only.
    FieldRule("events.parquet", "limit_step", ("nums",), source_unit="count",
              note="consecutive-limit count"),
    FieldRule("events.parquet", "limit_cpt_list", ("days", "cons_nums", "up_nums"),
              source_unit="count"),
    FieldRule("events.parquet", "limit_cpt_list", ("pct_chg",), source_unit="percent"),
    FieldRule("events.parquet", "limit_cpt_list", ("rank",), source_unit="rank"),
    FieldRule("events.parquet", "limit_list_ths", ("price",), source_unit="CNY_per_share",
              status="verified", evidence="median 24 at price scale"),
    FieldRule("events.parquet", "limit_list_ths",
              ("pct_chg", "turnover_rate", "limit_up_suc_rate"), source_unit="percent"),
    FieldRule("events.parquet", "limit_list_ths", ("open_num",), source_unit="count"),
    FieldRule("events.parquet", "limit_list_ths", ("free_float",), source_unit="CNY",
              status="inferred", evidence="median 7.1e9 at float-market-value scale"),
    FieldRule("events.parquet", "limit_list_ths",
              ("limit_order", "limit_amount", "turnover", "rise_rate", "sum_float",
               "lu_limit_order"),
              source_unit="unknown", status="unknown",
              note="all-NA locally; resolve before use"),
    FieldRule("events.parquet", "ths_hot", ("rank",), source_unit="rank"),
    FieldRule("events.parquet", "ths_hot", ("pct_change",), source_unit="percent"),
    FieldRule("events.parquet", "ths_hot", ("current_price",), source_unit="CNY_per_share",
              note="for A-share rows"),
    FieldRule("events.parquet", "ths_hot", ("hot",), source_unit="vendor_score"),
    FieldRule("events.parquet", "dc_hot", ("rank",), source_unit="rank"),
    FieldRule("events.parquet", "dc_hot", ("pct_change",), source_unit="percent"),
    FieldRule("events.parquet", "dc_hot", ("current_price",), source_unit="CNY_per_share",
              note="for A-share rows"),
    FieldRule("events.parquet", "dc_hot", ("hot",), source_unit="vendor_score"),
    FieldRule("events.parquet", "hm_detail",
              ("buy_amount", "sell_amount", "net_amount"), source_unit="CNY"),
    # hm_list is a static reference list: name/desc/orgs only.
    # ======================= macro.parquet, by dataset ======================
    FieldRule("macro.parquet", "cn_gdp", ("gdp", "pi", "si", "ti"), source_unit="100m_CNY"),
    FieldRule("macro.parquet", "cn_gdp", ("*_yoy",), source_unit="percent"),
    FieldRule("macro.parquet", "cn_cpi", ("nt_val", "town_val", "cnt_val"),
              source_unit="official_index"),
    FieldRule("macro.parquet", "cn_cpi", ("*_yoy", "*_mom", "*_accu"), source_unit="percent"),
    FieldRule("macro.parquet", "cn_ppi", ("ppi*",), source_unit="percent",
              note="yoy/mom/accumulated change rates; no index level columns"),
    FieldRule("macro.parquet", "cn_pmi", ("pmi*",), source_unit="diffusion_index"),
    FieldRule("macro.parquet", "cn_m", ("m0", "m1", "m2"), source_unit="100m_CNY"),
    FieldRule("macro.parquet", "cn_m", ("*_yoy", "*_mom"), source_unit="percent"),
    FieldRule("macro.parquet", "sf_month", ("inc_month", "inc_cumval", "stk_endval"),
              source_unit="100m_CNY"),
    FieldRule("macro.parquet", "shibor",
              ("on", "1w", "2w", "1m", "3m", "6m", "9m", "1y"), source_unit="percent"),
    FieldRule("macro.parquet", "shibor_quote", ("*_b", "*_a"), source_unit="percent",
              note="bid/ask quotes per tenor"),
    FieldRule("macro.parquet", "shibor_lpr", ("1y", "5y"), source_unit="percent"),
    # monetary_policy is text evidence: dates/title/urls/content only.
    FieldRule("macro.parquet", "eco_cal", ("value", "pre_value", "fore_value"),
              source_unit="unknown", status="unknown",
              note="heterogeneous by event; must not be pooled without event-specific parsing"),
    FieldRule("macro.parquet", "index_global",
              ("open", "close", "high", "low", "pre_close", "change"),
              source_unit="index_points"),
    FieldRule("macro.parquet", "index_global", ("pct_chg", "swing"), source_unit="percent"),
    FieldRule("macro.parquet", "index_global", ("vol", "amount"),
              source_unit="unknown", status="unknown",
              note="unit varies by market and source; sparse"),
    FieldRule("macro.parquet", "index_daily",
              ("open", "close", "high", "low", "pre_close", "change"),
              source_unit="index_points"),
    FieldRule("macro.parquet", "index_daily", ("pct_chg",), source_unit="percent",
              note="5%=5.0 — do not multiply by 100 again"),
    FieldRule("macro.parquet", "index_daily", ("vol",), source_unit="hands"),
    FieldRule("macro.parquet", "index_daily", ("amount",), source_unit="thousand_CNY"),
    FieldRule("macro.parquet", "index_dailybasic", ("total_mv", "float_mv"),
              source_unit="CNY",
              status="verified", evidence="CSI300 total_mv ~1e13 only plausible in CNY",
              note="CNY here vs 10k CNY in daily_basic — do not mix scales"),
    FieldRule("macro.parquet", "index_dailybasic",
              ("total_share", "float_share", "free_share"), source_unit="shares",
              status="verified", evidence="~1e11 share scale",
              note="shares here vs 10k shares in daily_basic"),
    FieldRule("macro.parquet", "index_dailybasic", ("turnover_rate", "turnover_rate_f"),
              source_unit="percent",
              status="verified", evidence="median 2.4 at percent scale"),
    FieldRule("macro.parquet", "index_dailybasic", ("pe", "pe_ttm", "pb"),
              source_unit="multiple"),
    FieldRule("macro.parquet", "sw_daily",
              ("open", "low", "high", "close", "change"), source_unit="index_points"),
    FieldRule("macro.parquet", "sw_daily", ("pct_change",), source_unit="percent"),
    FieldRule("macro.parquet", "sw_daily", ("vol",), source_unit="10k_shares"),
    FieldRule("macro.parquet", "sw_daily", ("amount",), source_unit="10k_CNY",
              status="verified", evidence="industry turnover median 6.1e5 == 6.1b CNY"),
    FieldRule("macro.parquet", "sw_daily", ("pe", "pb"), source_unit="multiple"),
    FieldRule("macro.parquet", "sw_daily", ("float_mv", "total_mv"), source_unit="10k_CNY",
              status="verified", evidence="industry total_mv median 2.5e7 == 250b CNY"),
    FieldRule("macro.parquet", "ci_daily",
              ("open", "low", "high", "close", "pre_close", "change"),
              source_unit="index_points"),
    FieldRule("macro.parquet", "ci_daily", ("pct_change",), source_unit="percent"),
    FieldRule("macro.parquet", "ci_daily", ("vol",), source_unit="10k_shares"),
    FieldRule("macro.parquet", "ci_daily", ("amount",), source_unit="10k_CNY"),
    FieldRule("macro.parquet", "daily_info", ("com_count",), source_unit="count"),
    FieldRule("macro.parquet", "daily_info", ("trans_count",),
              source_unit="unknown", status="unknown",
              note="all-NA locally; count basis (笔 vs 万笔) unverified"),
    FieldRule("macro.parquet", "daily_info", ("total_share", "float_share", "vol"),
              source_unit="100m_shares",
              status="verified", evidence="exchange-level medians only plausible in 100m units"),
    FieldRule("macro.parquet", "daily_info", ("total_mv", "float_mv", "amount"),
              source_unit="100m_CNY",
              status="verified", evidence="exchange turnover ~3.9e3 == 390b CNY",
              note="SSE stats in 100m units vs sz_daily_info in CNY — do not mix"),
    FieldRule("macro.parquet", "daily_info", ("pe",), source_unit="multiple"),
    FieldRule("macro.parquet", "daily_info", ("tr",), source_unit="percent"),
    FieldRule("macro.parquet", "sz_daily_info", ("count",), source_unit="count"),
    FieldRule("macro.parquet", "sz_daily_info",
              ("amount", "total_mv", "float_mv"), source_unit="CNY",
              status="verified", evidence="SZSE daily turnover ~6.9e10 == 69b CNY"),
    FieldRule("macro.parquet", "sz_daily_info", ("vol",), source_unit="shares",
              status="inferred", note="sparse locally"),
    FieldRule("macro.parquet", "sz_daily_info", ("total_share", "float_share"),
              source_unit="shares", status="inferred", note="sparse locally"),
    FieldRule("macro.parquet", "moneyflow_mkt_dc", ("close_sh", "close_sz"),
              source_unit="index_points"),
    FieldRule("macro.parquet", "moneyflow_mkt_dc",
              ("pct_change_sh", "pct_change_sz", "*_rate"), source_unit="percent"),
    FieldRule("macro.parquet", "moneyflow_mkt_dc", ("net_amount", "buy_*_amount"),
              source_unit="CNY",
              status="verified", evidence="market-wide flows ~ -4.5e10 plausible only in CNY"),
    # broker_recommend is a monthly text list: identifiers only.
    FieldRule("macro.parquet", "ths_daily",
              ("open", "high", "low", "close", "pre_close", "change"),
              source_unit="index_points"),
    FieldRule("macro.parquet", "ths_daily", ("avg_price",), source_unit="CNY_per_share"),
    FieldRule("macro.parquet", "ths_daily", ("pct_change", "turnover_rate"),
              source_unit="percent"),
    FieldRule("macro.parquet", "ths_daily", ("vol",), source_unit="hands",
              status="inferred", evidence="magnitude between share and 10k-share scales",
              note="use comparatively; do not mix with stock volumes"),
    FieldRule("macro.parquet", "fx_daily", ("bid_*", "ask_*"), source_unit="quote_price"),
    FieldRule("macro.parquet", "fx_daily", ("tick_qty",), source_unit="count",
              note="quote/tick count, not traded volume"),
    FieldRule("macro.parquet", "repo_daily", ("repo_maturity",), semantic="categorical"),
    FieldRule("macro.parquet", "repo_daily",
              ("pre_close", "open", "high", "low", "close", "weight", "weight_r"),
              source_unit="percent",
              status="verified", evidence="repo quotes are annualized rates (~1.5)"),
    FieldRule("macro.parquet", "repo_daily", ("amount",), source_unit="10k_CNY",
              status="verified", evidence="GC001 daily ~2.2e8 == ~2.2 trillion CNY"),
    FieldRule("macro.parquet", "repo_daily", ("num",), source_unit="count"),
    FieldRule("macro.parquet", "us_tycr", ("m*", "y*"), source_unit="percent"),
    FieldRule("macro.parquet", "us_trycr", ("y*",), source_unit="percent"),
    FieldRule("macro.parquet", "fut_basic", ("multiplier", "per_unit"),
              source_unit="units_per_lot"),
    # fut_mapping is a contract mapping: identifiers only.
    FieldRule("macro.parquet", "fut_daily",
              ("pre_close", "pre_settle", "open", "high", "low", "close", "settle",
               "delv_settle", "change1", "change2"),
              source_unit="contract_quote_units",
              note="index points for CFFEX; fut_basic multiplier converts to notional"),
    FieldRule("macro.parquet", "fut_daily", ("vol", "oi", "oi_chg"), source_unit="lots"),
    FieldRule("macro.parquet", "fut_daily", ("amount",), source_unit="10k_CNY"),
    FieldRule("macro.parquet", "opt_basic", ("per_unit",), source_unit="units_per_lot"),
    FieldRule("macro.parquet", "opt_basic", ("exercise_price",),
              source_unit="underlying_quote_units"),
    FieldRule("macro.parquet", "opt_basic", ("list_price",),
              source_unit="premium_quote_units"),
    FieldRule("macro.parquet", "opt_basic", ("min_price_chg",), semantic="text",
              note="vendor tick-size field; format varies"),
    FieldRule("macro.parquet", "opt_daily",
              ("pre_settle", "pre_close", "open", "high", "low", "close", "settle"),
              source_unit="premium_quote_units"),
    FieldRule("macro.parquet", "opt_daily", ("vol", "oi"), source_unit="contracts"),
    FieldRule("macro.parquet", "opt_daily", ("amount",), source_unit="10k_CNY",
              status="verified", evidence="premium*per_unit*vol reconciles at 10k-CNY scale"),
    FieldRule("macro.parquet", "cb_basic", ("par", "issue_price", "maturity_call_price"),
              source_unit="CNY_per_100_par"),
    FieldRule("macro.parquet", "cb_basic", ("issue_size", "remain_size"), source_unit="CNY",
              status="verified", evidence="issue_size ~7.5e8 at bond-issue scale"),
    FieldRule("macro.parquet", "cb_basic", ("maturity",), source_unit="years"),
    FieldRule("macro.parquet", "cb_basic", ("coupon_rate",), source_unit="percent"),
    FieldRule("macro.parquet", "cb_basic", ("pay_per_year",), source_unit="count"),
    FieldRule("macro.parquet", "cb_basic", ("first_conv_price", "conv_price"),
              source_unit="CNY_per_share",
              status="verified", evidence="medians 12-16 at stock price scale",
              note="nightly CURRENT-STATE refresh; never feed historical backtests"),
    FieldRule("macro.parquet", "cb_daily",
              ("pre_close", "open", "high", "low", "close", "change", "bond_value",
               "cb_value"),
              source_unit="CNY_per_100_par"),
    FieldRule("macro.parquet", "cb_daily", ("pct_chg", "bond_over_rate", "cb_over_rate"),
              source_unit="percent"),
    FieldRule("macro.parquet", "cb_daily", ("vol",), source_unit="lots"),
    FieldRule("macro.parquet", "cb_daily", ("amount",), source_unit="10k_CNY"),
    FieldRule("macro.parquet", "cb_call", ("call_price", "call_price_tax"),
              source_unit="CNY_per_100_par"),
    FieldRule("macro.parquet", "cb_call", ("call_vol",), source_unit="bonds",
              status="verified", evidence="call_vol*call_price reconciles with call_amount"),
    FieldRule("macro.parquet", "cb_call", ("call_amount",), source_unit="10k_CNY",
              status="verified", evidence="reconciles with call_vol at per-100-par prices"),
    FieldRule("macro.parquet", "yc_cb", ("curve_term",), source_unit="years"),
    FieldRule("macro.parquet", "yc_cb", ("yield",), source_unit="percent",
              note="curve_type 0=YTM, 1=spot"),
    # ==================== fundamentals.parquet, by dataset ==================
    FieldRule("fundamentals.parquet", "income_vip", ("basic_eps", "diluted_eps"),
              source_unit="CNY_per_share"),
    FieldRule("fundamentals.parquet", "income_vip", ("*",), source_unit="CNY",
              note="income statement amounts (vendor contract is uniform)"),
    FieldRule("fundamentals.parquet", "balancesheet_vip", ("total_share",),
              source_unit="shares",
              status="verified", evidence="median 3.9e8 at share-capital scale",
              note="period-end total shares, not the CNY paid-in-capital line"),
    FieldRule("fundamentals.parquet", "balancesheet_vip", ("*",), source_unit="CNY",
              note="balance sheet amounts (vendor contract is uniform)"),
    FieldRule("fundamentals.parquet", "cashflow_vip", ("*",), source_unit="CNY",
              note="cash flow statement amounts (vendor contract is uniform)"),
    FieldRule("fundamentals.parquet", "fina_indicator_vip",
              ("eps", "dt_eps", "total_revenue_ps", "revenue_ps", "capital_rese_ps",
               "surplus_rese_ps", "undist_profit_ps", "diluted2_eps", "bps", "ocfps",
               "retainedps", "cfps", "ebit_ps", "fcff_ps", "fcfe_ps"),
              source_unit="CNY_per_share"),
    FieldRule("fundamentals.parquet", "fina_indicator_vip",
              ("extra_item", "profit_dedt", "gross_margin", "op_income", "ebit", "ebitda",
               "fcff", "fcfe", "current_exint", "noncurrent_exint", "interestdebt",
               "netdebt", "tangible_asset", "working_capital", "networking_capital",
               "invest_capital", "retained_earnings", "fixed_assets"),
              source_unit="CNY",
              status="verified", evidence="gross_margin median 2.4e8 is a CNY amount",
              note="gross_margin is gross PROFIT in CNY; grossprofit_margin is the percent"),
    FieldRule("fundamentals.parquet", "fina_indicator_vip",
              ("current_ratio", "quick_ratio", "cash_ratio", "assets_to_eqt",
               "dp_assets_to_eqt", "debt_to_eqt", "eqt_to_debt", "eqt_to_interestdebt",
               "tangibleasset_to_debt", "tangasset_to_intdebt", "tangibleasset_to_netdebt",
               "ocf_to_debt", "ocf_to_shortdebt"),
              source_unit="multiple",
              status="verified", evidence="current_ratio median 1.68 = 1.68x, not percent"),
    FieldRule("fundamentals.parquet", "fina_indicator_vip",
              ("ar_turn", "ca_turn", "fa_turn", "assets_turn"),
              source_unit="times_per_period",
              status="verified", evidence="assets_turn median 0.35 at turnover-frequency scale"),
    FieldRule("fundamentals.parquet", "fina_indicator_vip", ("turn_days",),
              source_unit="days",
              status="verified", evidence="median 180 at days scale"),
    FieldRule("fundamentals.parquet", "fina_indicator_vip",
              ("netprofit_margin", "grossprofit_margin", "cogs_of_sales", "expense_of_sales",
               "profit_to_gr", "saleexp_to_gr", "adminexp_of_gr", "finaexp_of_gr",
               "gc_of_gr", "op_of_gr", "ebit_of_gr", "roe", "roe_waa", "roe_dt", "roa",
               "npta", "roic", "roe_yearly", "roa2_yearly", "roa_yearly", "roa_dp",
               "debt_to_assets", "ca_to_assets", "nca_to_assets", "tbassets_to_totalassets",
               "int_to_talcap", "eqt_to_talcapital", "currentdebt_to_debt",
               "longdeb_to_debt", "profit_to_op", "q_*", "*_yoy", "q_op_qoq"),
              source_unit="percent",
              status="verified", evidence="roe median 10.6, debt_to_assets 42.8 at percent scale"),
    FieldRule("fundamentals.parquet", "fina_indicator_vip", ("impai_ttm",),
              source_unit="unknown", status="unknown",
              note="median -0.63 inconsistent with a CNY amount; resolve before use"),
    FieldRule("fundamentals.parquet", "forecast_vip", ("p_change_min", "p_change_max"),
              source_unit="percent"),
    FieldRule("fundamentals.parquet", "forecast_vip",
              ("net_profit_min", "net_profit_max", "last_parent_net"),
              source_unit="10k_CNY",
              status="verified", evidence="last_parent_net matches forecast bounds at 10k-CNY scale",
              note="must not be mixed directly with statement net profit in CNY"),
    FieldRule("fundamentals.parquet", "express_vip",
              ("revenue", "operate_profit", "total_profit", "n_income", "total_assets",
               "total_hldr_eqy_exc_min_int", "open_net_assets"),
              source_unit="CNY",
              status="verified", evidence="revenue median 4.9e9 at CNY scale"),
    FieldRule("fundamentals.parquet", "express_vip",
              ("diluted_eps", "bps", "open_bps"), source_unit="CNY_per_share"),
    FieldRule("fundamentals.parquet", "express_vip", ("diluted_roe", "yoy_net_profit"),
              source_unit="percent"),
    FieldRule("fundamentals.parquet", "dividend", ("cash_div", "cash_div_tax"),
              source_unit="CNY_per_share",
              status="verified", evidence="median 0.095 per share"),
    FieldRule("fundamentals.parquet", "dividend",
              ("stk_div", "stk_bo_rate", "stk_co_rate"), source_unit="shares_per_share",
              note="bonus/transfer proportions per held share"),
    FieldRule("fundamentals.parquet", "fina_audit", ("audit_fees",), source_unit="CNY",
              status="verified", evidence="median 4e5 at audit-fee scale"),
    FieldRule("fundamentals.parquet", "fina_mainbz_vip",
              ("bz_sales", "bz_profit", "bz_cost"), source_unit="CNY",
              status="verified", evidence="segment revenue median 1.3e7 at CNY scale"),
    # disclosure_date carries dates only.
    # ============== raw-lake datasets never included in snapshots ===========
    FieldRule("raw_only", "bak_basic", ("total_share", "float_share"),
              source_unit="100m_shares", note="no volume or amount fields"),
    FieldRule("raw_only", "bak_basic",
              ("total_assets", "liquid_assets", "fixed_assets"), source_unit="100m_CNY",
              note="coarse company snapshot fields; supplemental use only"),
    FieldRule("raw_only", "cn_schedule", ("*",), semantic="text",
              note="release schedule; not in the default snapshot macro set"),
    FieldRule("raw_only", "hibor", ("*",), source_unit="percent",
              note="not in the default snapshot macro set"),
    FieldRule("raw_only", "libor", ("*",), source_unit="percent",
              note="not in the default snapshot macro set"),
    FieldRule("raw_only", "us_tbr", ("*",), source_unit="percent",
              note="not in the default snapshot macro set"),
    FieldRule("raw_only", "us_tltr", ("*",), source_unit="percent",
              note="not in the default snapshot macro set"),
)


@lru_cache(maxsize=1)
def _rules_index() -> dict[tuple[str, str | None], tuple[tuple[FieldRule, ...], FieldRule | None]]:
    index: dict[tuple[str, str | None], tuple[list[FieldRule], list[FieldRule]]] = {}
    for rule in FIELD_RULES:
        explicit, defaults = index.setdefault((rule.file, rule.dataset), ([], []))
        (defaults if rule.columns == ("*",) else explicit).append(rule)
    out: dict[tuple[str, str | None], tuple[tuple[FieldRule, ...], FieldRule | None]] = {}
    for key, (explicit, defaults) in index.items():
        if len(defaults) > 1:
            raise ValueError(f"multiple default unit rules for {key}")
        out[key] = (tuple(explicit), defaults[0] if defaults else None)
    return out


def resolve_field(file: str, dataset: str | None, column: str) -> dict[str, object]:
    """Classify one snapshot column; raises UnresolvedUnitError if unregistered."""
    explicit, default = _rules_index().get((file, dataset), ((), None))
    matches = [
        rule for rule in explicit
        if any(fnmatchcase(column, pattern) for pattern in rule.columns)
    ]
    if len(matches) > 1:
        raise ValueError(
            f"unit registry overlap for {file}:{dataset}:{column}: "
            f"{[rule.key() for rule in matches]}"
        )
    rule = matches[0] if matches else None
    semantic = source_unit = factor = normalized = status = note = None
    if rule is None:
        for pattern, common_semantic in COMMON_FIELD_SEMANTICS:
            if fnmatchcase(column, pattern):
                semantic, status = common_semantic, "official"
                break
        else:
            rule = default
    if rule is not None:
        semantic, source_unit, status = rule.semantic, rule.source_unit, rule.status
        factor, normalized, note = rule.factor, rule.normalized_unit, rule.note or None
    if semantic is None:
        raise UnresolvedUnitError(
            f"no unit rule or common classifier for {file}:{dataset}:{column}"
        )
    record: dict[str, object] = {
        "file": file,
        "dataset": dataset,
        "column": column,
        "semantic_type": semantic,
        "source_unit": source_unit,
        "status": status,
    }
    if factor is not None:
        record["factor"] = factor
        record["normalized_unit"] = normalized
    if note:
        record["note"] = note
    return record


def build_unit_reference(
    column_map: dict[tuple[str, str | None], list[str]],
) -> list[dict[str, object]]:
    """Per-column records for every (file, dataset, column) in the map.

    Raises UnresolvedUnitError listing ALL unclassified columns at once.
    """
    records: list[dict[str, object]] = []
    problems: list[str] = []
    for (file, dataset), columns in sorted(
        column_map.items(), key=lambda item: (item[0][0], item[0][1] or "")
    ):
        for column in columns:
            try:
                records.append(resolve_field(file, dataset, column))
            except UnresolvedUnitError as exc:
                problems.append(str(exc))
    if problems:
        raise UnresolvedUnitError(
            "unclassified snapshot columns:\n" + "\n".join(sorted(problems))
        )
    return records


# Union files whose per-dataset column attribution lives in the snapshot
# manifest (captured at build time); all other snapshot parquets are
# single-schema and classified from their footer directly.
UNION_DOMAIN_BY_FILE: dict[str, str] = {
    "events.parquet": "events",
    "macro.parquet": "macro",
    "fundamentals.parquet": "fundamentals",
}


def snapshot_column_map(view_dir, manifest: dict[str, object]) -> dict[tuple[str, str | None], list[str]]:
    """(file, dataset) -> columns for every parquet in one snapshot view.

    Union files require ``dataset_columns`` in the manifest domain metadata
    (written by the snapshot builder); a manifest without it is from an
    incompatible snapshot format and fails fast.
    """
    view_dir = Path(view_dir)
    domains = manifest.get("domains", {})
    column_map: dict[tuple[str, str | None], list[str]] = {}
    for path in sorted(view_dir.glob("*.parquet")):
        domain = UNION_DOMAIN_BY_FILE.get(path.name)
        if domain is None:
            try:
                column_map[(path.name, None)] = list(pq.read_schema(path).names)
            except Exception:
                # An unreadable footer means the Agent cannot read the file
                # either; the summary records its metadata_error and the file
                # contributes no unit records (absent columns stay forbidden
                # under the unknown-unit policy).
                pass
            continue
        meta = domains.get(domain)
        if not isinstance(meta, dict) or "dataset_columns" not in meta:
            raise ValueError(
                f"snapshot manifest domain '{domain}' lacks dataset_columns for {path.name}; "
                "incompatible snapshot format — rebuild the snapshot"
            )
        for dataset, columns in meta["dataset_columns"].items():
            column_map[(path.name, dataset)] = list(columns)
    return column_map


def validate_snapshot_units(view_dir, manifest: dict[str, object]) -> None:
    """Fail-fast: every column of every snapshot file must classify."""
    build_unit_reference(snapshot_column_map(view_dir, manifest))


def rules_for(file: str | None = None, datasets: tuple[str, ...] | None = None) -> tuple[FieldRule, ...]:
    """Registry selection by file and/or exact dataset ids."""
    selected = FIELD_RULES
    if file is not None:
        selected = tuple(rule for rule in selected if rule.file == file)
    if datasets is not None:
        wanted = set(datasets)
        selected = tuple(rule for rule in selected if rule.dataset in wanted)
    return selected


# Datasets whose every column is an identifier/date/text resolved by the
# common classifiers — they legitimately carry no unit rules.
NO_NUMERIC_DATASETS = frozenset(
    {"margin_secs", "dc_member", "hm_list", "monetary_policy", "broker_recommend",
     "fut_mapping", "disclosure_date"}
)


def dataset_rules_records(datasets: tuple[str, ...]) -> dict[str, list[dict[str, object]]]:
    """Audit projection: registry records grouped by dataset, fail-fast on ids
    that neither carry a rule nor are known no-numeric datasets (typo guard)."""
    ruled = {rule.dataset for rule in FIELD_RULES if rule.dataset}
    unknown = sorted(set(datasets) - ruled - NO_NUMERIC_DATASETS)
    if unknown:
        raise KeyError(f"unit registry has no rules for datasets: {unknown}")
    by_dataset: dict[str, list[dict[str, object]]] = {dataset: [] for dataset in datasets}
    for rule in rules_for(datasets=datasets):
        by_dataset[rule.dataset].append(rule.to_record())
    return by_dataset


def registry_file_datasets() -> set[tuple[str, str]]:
    return {(rule.file, rule.dataset) for rule in FIELD_RULES if rule.dataset}


def column_source_units(file: str) -> dict[str, str]:
    """Exact column -> source unit for a single-schema file's numeric rules."""
    out: dict[str, str] = {}
    for rule in rules_for(file):
        if rule.dataset is not None or rule.source_unit is None:
            continue
        for column in rule.columns:
            if not any(ch in column for ch in "*?["):
                out[column] = rule.source_unit
    return out


def _conversions(file: str) -> tuple[tuple[str, float, str], ...]:
    conversions: list[tuple[str, float, str]] = []
    for rule in rules_for(file):
        if rule.factor is None:
            continue
        for column in rule.columns:
            if any(ch in column for ch in "*?["):
                raise ValueError(f"conversion rule {rule.key()} must use exact column names")
            conversions.append((column, rule.factor, f"{rule.source_unit}->{rule.normalized_unit}"))
    return tuple(conversions)


# Derived byte-conversion tables (single-sourced from the registry).
DAILY_UNIT_CONVERSIONS: tuple[tuple[str, float, str], ...] = _conversions("daily.parquet")
AUCTION_UNIT_CONVERSIONS: tuple[tuple[str, float, str], ...] = _conversions("auction.parquet")

# Minimal Agent-facing contract inside data_summary.json: the full per-column
# table ships as its own artifact next to it, loaded by the Agent on demand.
AGENT_UNIT_CONTRACT: dict[str, str] = {
    "identity_rule": "interpret units by file + dataset + column; never by column name alone",
    "unit_reference": "/mnt/artifacts/unit_reference.json",
    "normalized_files": (
        "daily/intraday_1min/auction/corporate_actions files store normalized values; "
        "records carrying a factor show the applied source->normalized conversion"
    ),
    "unknown_unit_policy": (
        "columns whose unit_reference entry has status 'unknown' must not be used for "
        "absolute thresholds or cross-dataset arithmetic until explicitly resolved"
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
