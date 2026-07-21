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
    "events.parquet": {
        "mode": "source_by_dataset",
        "datasets": {
            "moneyflow": {
                "*_vol": "hands",
                "*_amount": "10k_CNY; 500 means CNY 5m",
            }
        },
    },
    "macro.parquet": {
        "mode": "source_by_dataset",
        "datasets": {
            "index_daily": {
                "pct_chg": "percent_number; 5%=5.0; do not multiply by 100 again",
            }
        },
    },
    "fundamentals.parquet": {"mode": "source_by_dataset"},
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
