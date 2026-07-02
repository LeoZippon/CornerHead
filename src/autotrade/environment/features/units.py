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
    ("total_share", 10_000.0, "ten_thousand_shares->shares"),
    ("float_share", 10_000.0, "ten_thousand_shares->shares"),
    ("free_share", 10_000.0, "ten_thousand_shares->shares"),
    ("total_mv", 10_000.0, "ten_thousand_cny->cny"),
    ("circ_mv", 10_000.0, "ten_thousand_cny->cny"),
)


def normalize_daily_units(frame: pd.DataFrame) -> tuple[pd.DataFrame, list[dict[str, object]]]:
    """Apply the daily unit contract and return conversion metadata."""
    frame = frame.copy()
    conversions: list[dict[str, object]] = []
    for column, factor, rule in DAILY_UNIT_CONVERSIONS:
        if column in frame.columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce") * factor
            conversions.append({"column": column, "factor": factor, "rule": rule})
    return frame, conversions
