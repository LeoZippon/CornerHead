from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

import numpy as np
import pandas as pd


SZ_MAIN_BOARD_AUCTION_FACTOR = 0.76
SZ_GEM_AUCTION_FACTOR = 0.58


@dataclass(frozen=True)
class AuctionCorrectionConfig:
    """PIT-layer correction for TuShare minute 09:30 auction bars.

    Raw ``stk_mins`` files are immutable. This transform creates corrected
    PIT columns for historical auction columns that need to align with the
    live ``stk_auction`` source.
    """

    enabled: bool = True
    code_column: str = "ts_code"
    time_column: str = "trade_time"
    volume_column: str = "vol"
    amount_column: str = "amount"
    corrected_suffix: str = "_pit"
    volume_factors: Mapping[str, float] = field(
        default_factory=lambda: {
            "sz_main_00": SZ_MAIN_BOARD_AUCTION_FACTOR,
            "sz_gem_30": SZ_GEM_AUCTION_FACTOR,
        }
    )
    amount_factors: Mapping[str, float] = field(
        default_factory=lambda: {
            "sz_main_00": SZ_MAIN_BOARD_AUCTION_FACTOR,
            "sz_gem_30": SZ_GEM_AUCTION_FACTOR,
        }
    )


def market_bucket(ts_code: object) -> str:
    text = str(ts_code or "").strip().upper()
    if text.endswith(".SZ") and text.startswith("00"):
        return "sz_main_00"
    if text.endswith(".SZ") and text.startswith("30"):
        return "sz_gem_30"
    if text.endswith(".SH") and text.startswith("60"):
        return "sh_main_60"
    if text.endswith(".SH") and text.startswith("68"):
        return "sh_star_68"
    if text.endswith(".BJ"):
        return "bj"
    return "other"


def is_open_auction_time(value: object) -> bool:
    text = str(value or "").strip()
    return "09:30" in text[:19]


def apply_open_auction_correction(
    frame: pd.DataFrame,
    config: AuctionCorrectionConfig | None = None,
) -> pd.DataFrame:
    """Return a copy with corrected PIT volume/amount columns.

    The correction is intentionally limited to 09:30 minute bars. Closing
    auction bars and non-Shenzhen buckets keep factor 1.0.
    """

    config = config or AuctionCorrectionConfig()
    required = [config.code_column, config.time_column]
    missing = [column for column in required if column not in frame.columns]
    if missing:
        raise ValueError(f"auction correction missing required columns: {missing}")

    out = frame.copy()

    # Replay slots contain tens of millions of rows but only a few thousand
    # distinct codes and a few hundred distinct minute stamps per partition.
    # Factorize first, evaluate the scalar contract once per distinct value,
    # then expand through integer indexing.  This avoids both per-row Python
    # calls and full-column string normalization/allocation.
    code_ids, unique_codes = pd.factorize(out[config.code_column], sort=False)
    unique_buckets = np.asarray([market_bucket(value) for value in unique_codes], dtype=object)
    bucket_values = np.full(len(out), "other", dtype=object)
    valid_codes = code_ids >= 0
    bucket_values[valid_codes] = unique_buckets[code_ids[valid_codes]]
    buckets = pd.Series(bucket_values, index=out.index, dtype="object")

    time_ids, unique_times = pd.factorize(out[config.time_column], sort=False)
    unique_open = np.asarray([is_open_auction_time(value) for value in unique_times], dtype=bool)
    open_values = np.zeros(len(out), dtype=bool)
    valid_times = time_ids >= 0
    open_values[valid_times] = unique_open[time_ids[valid_times]]
    open_mask = pd.Series(open_values, index=out.index, dtype=bool)

    out["auction_market_bucket"] = buckets
    out["auction_open_bar"] = open_values
    volume_factors = np.ones(len(out), dtype=float)
    amount_factors = np.ones(len(out), dtype=float)

    if config.volume_column in out.columns:
        corrected_volume = f"{config.volume_column}{config.corrected_suffix}"
        out[corrected_volume] = pd.to_numeric(out[config.volume_column], errors="coerce")
        if config.enabled:
            unique_factors = np.asarray(
                [float(config.volume_factors.get(bucket, 1.0)) for bucket in unique_buckets], dtype=float
            )
            volume_factors.fill(float(config.volume_factors.get("other", 1.0)))
            volume_factors[valid_codes] = unique_factors[code_ids[valid_codes]]
            volume_factors[~open_values] = 1.0
            out[corrected_volume] = out[corrected_volume] * volume_factors

    if config.amount_column in out.columns:
        corrected_amount = f"{config.amount_column}{config.corrected_suffix}"
        out[corrected_amount] = pd.to_numeric(out[config.amount_column], errors="coerce")
        if config.enabled:
            unique_factors = np.asarray(
                [float(config.amount_factors.get(bucket, 1.0)) for bucket in unique_buckets], dtype=float
            )
            amount_factors.fill(float(config.amount_factors.get("other", 1.0)))
            amount_factors[valid_codes] = unique_factors[code_ids[valid_codes]]
            amount_factors[~open_values] = 1.0
            out[corrected_amount] = out[corrected_amount] * amount_factors

    out["auction_vol_correction_factor"] = volume_factors
    out["auction_amount_correction_factor"] = amount_factors
    corrected = config.enabled & open_values & ((volume_factors != 1.0) | (amount_factors != 1.0))
    rules = np.full(len(out), "none", dtype=object)
    rules[corrected] = "minute_0930_to_live_stk_auction_by_market_bucket"
    out["auction_correction_rule"] = rules
    return out
