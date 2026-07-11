"""TuShare realtime minute feed (rt_min) for the live market-data path.

Advance integration for the live loop: bars normalize to the SAME schema as
the historical minute store (``STK_MINS_REQUIRED_COLUMNS``), so the unified
tick loop, Timeview and any by-date persistence consume live bars exactly like
replay bars. ``rt_min`` needs a paid subscription; the trial tier answers a
single latest bar per code, which is enough for --probe validation.

Empirical contract (probed 2026-07-11 against api.tushare.pro):
  rt_min(ts_code, freq="1MIN") -> ts_code, freq, time("YYYY-MM-DD HH:MM:SS",
  Asia/Shanghai bar close), open, close, high, low, vol(shares), amount(CNY).
"""

from __future__ import annotations

import os
from pathlib import Path

import pandas as pd

from .common import STK_MINS_REQUIRED_COLUMNS, TuShareClient

RT_MIN_FREQ = "1MIN"
RT_MIN_AVAILABLE_AT_RULE = "source:trade_time_bar_close"
LIVE_MINUTE_DIRNAME = "rt_min_live"


def normalize_rt_minutes(frame: pd.DataFrame) -> pd.DataFrame:
    """rt_min rows -> historical minute-store schema (adds trade_date/available_at)."""
    if frame.empty:
        return pd.DataFrame(columns=STK_MINS_REQUIRED_COLUMNS)
    out = frame.rename(columns={"time": "trade_time"}).copy()
    missing = [c for c in ("ts_code", "trade_time", "open", "high", "low", "close", "vol", "amount") if c not in out.columns]
    if missing:
        raise ValueError(f"rt_min response missing columns: {missing}")
    stamps = pd.to_datetime(out["trade_time"], format="%Y-%m-%d %H:%M:%S")
    out["trade_date"] = stamps.dt.strftime("%Y%m%d")
    # Same stamping rule as the historical layer: a bar is visible at its close.
    out["available_at"] = stamps.dt.tz_localize("Asia/Shanghai").map(lambda ts: ts.isoformat())
    out["available_at_rule"] = RT_MIN_AVAILABLE_AT_RULE
    return out[STK_MINS_REQUIRED_COLUMNS]


class RealtimeMinuteFeed:
    """Poll rt_min for a watchlist and yield only bars not seen before.

    The client's serial throttle bounds the request rate; a watchlist poll is
    len(watchlist) requests, so keep watchlists to held positions + candidates.
    """

    def __init__(self, client: TuShareClient, watchlist: list[str]) -> None:
        if not watchlist:
            raise ValueError("realtime feed requires a non-empty watchlist")
        self.client = client
        self.watchlist = list(dict.fromkeys(watchlist))
        self._seen: set[tuple[str, str]] = set()

    def poll(self) -> pd.DataFrame:
        parts: list[pd.DataFrame] = []
        for ts_code in self.watchlist:
            result = self.client.query("rt_min", {"ts_code": ts_code, "freq": RT_MIN_FREQ})
            if result.items:
                parts.append(pd.DataFrame(result.items, columns=result.fields))
        merged = normalize_rt_minutes(pd.concat(parts, ignore_index=True)) if parts else normalize_rt_minutes(pd.DataFrame())
        fresh_mask = [
            (row.ts_code, row.trade_time) not in self._seen for row in merged.itertuples(index=False)
        ]
        fresh = merged.loc[fresh_mask].reset_index(drop=True)
        self._seen.update((row.ts_code, row.trade_time) for row in fresh.itertuples(index=False))
        return fresh


class RealtimeMinuteStore:
    """Per-trade-date parquet store of live bars, replay-schema identical.

    Append is dedup-by-(ts_code, trade_time) with an atomic replace, so the
    partition can be handed to MinuteMarketData / the live tick loop at any
    moment mid-session.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def partition_path(self, trade_date: str) -> Path:
        return self.root / f"trade_date={trade_date}.parquet"

    def append(self, bars: pd.DataFrame) -> dict[str, int]:
        appended: dict[str, int] = {}
        if bars.empty:
            return appended
        self.root.mkdir(parents=True, exist_ok=True)
        for trade_date, group in bars.groupby("trade_date"):
            path = self.partition_path(str(trade_date))
            merged = group
            if path.exists():
                merged = pd.concat([pd.read_parquet(path), group], ignore_index=True)
            merged = (
                merged.drop_duplicates(["ts_code", "trade_time"], keep="last")
                .sort_values(["trade_time", "ts_code"])
                .reset_index(drop=True)
            )
            tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
            merged.to_parquet(tmp, index=False)
            tmp.replace(path)
            appended[str(trade_date)] = len(group)
        return appended

    def bars(self, trade_date: str) -> pd.DataFrame:
        path = self.partition_path(trade_date)
        if not path.exists():
            return pd.DataFrame(columns=STK_MINS_REQUIRED_COLUMNS)
        return pd.read_parquet(path)
