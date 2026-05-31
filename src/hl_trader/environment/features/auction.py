from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

import pandas as pd


SZ_MAIN_BOARD_AUCTION_FACTOR = 0.76
SZ_GEM_AUCTION_FACTOR = 0.58


@dataclass(frozen=True)
class AuctionCorrectionConfig:
    """PIT-layer correction for TuShare minute 09:30 auction bars.

    Raw ``stk_mins`` files are immutable. This transform creates corrected
    PIT columns for historical auction features that need to align with the
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
    buckets = out[config.code_column].map(market_bucket)
    open_mask = out[config.time_column].map(is_open_auction_time)

    out["auction_market_bucket"] = buckets
    out["auction_open_bar"] = open_mask.astype(bool)
    out["auction_vol_correction_factor"] = 1.0
    out["auction_amount_correction_factor"] = 1.0
    out["auction_correction_rule"] = "none"

    if config.volume_column in out.columns:
        corrected_volume = f"{config.volume_column}{config.corrected_suffix}"
        out[corrected_volume] = pd.to_numeric(out[config.volume_column], errors="coerce")
        if config.enabled:
            for bucket, factor in config.volume_factors.items():
                mask = open_mask & buckets.eq(bucket)
                out.loc[mask, corrected_volume] = out.loc[mask, corrected_volume] * float(factor)
                out.loc[mask, "auction_vol_correction_factor"] = float(factor)

    if config.amount_column in out.columns:
        corrected_amount = f"{config.amount_column}{config.corrected_suffix}"
        out[corrected_amount] = pd.to_numeric(out[config.amount_column], errors="coerce")
        if config.enabled:
            for bucket, factor in config.amount_factors.items():
                mask = open_mask & buckets.eq(bucket)
                out.loc[mask, corrected_amount] = out.loc[mask, corrected_amount] * float(factor)
                out.loc[mask, "auction_amount_correction_factor"] = float(factor)

    corrected = (
        config.enabled
        & open_mask
        & (
            out["auction_vol_correction_factor"].ne(1.0)
            | out["auction_amount_correction_factor"].ne(1.0)
        )
    )
    out.loc[corrected, "auction_correction_rule"] = "minute_0930_to_live_stk_auction_by_market_bucket"
    return out
