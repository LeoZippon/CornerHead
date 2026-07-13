"""Minute replay market data and the daily-synthesized bar fallback.

The per-minute ``main(ctx)`` engine (``main_ctx_engine.py``) replays real
minute bars when the slot carries them and falls back to two synthetic bars
per day (09:30 open, 15:00 close) built from the daily row. Synthetic bars are
flagged so the Broker restricts them to reference-price fills.
"""

from __future__ import annotations

import math
import re

import pandas as pd


class MinuteMarketData:
    """Minute replay bars indexed by trade date, minute, and code."""

    REQUIRED = ("trade_date", "ts_code", "close")
    TIME_COLUMNS = ("trade_time", "datetime", "timestamp", "time")

    def __init__(self, minutes: pd.DataFrame) -> None:
        if minutes.empty:
            raise ValueError("minute replay data is empty")
        missing = [col for col in self.REQUIRED if col not in minutes.columns]
        if missing:
            raise ValueError(f"replay minute data missing columns: {missing}")
        time_column = next((col for col in self.TIME_COLUMNS if col in minutes.columns), None)
        if time_column is None:
            raise ValueError(f"replay minute data missing one of time columns: {list(self.TIME_COLUMNS)}")
        frame = minutes.copy()
        frame["trade_date"] = frame["trade_date"].astype(str)
        frame["ts_code"] = frame["ts_code"].astype(str)
        frame["minute_key"] = frame[time_column].map(_minute_key)
        if frame["minute_key"].isna().any():
            bad = frame.loc[frame["minute_key"].isna(), time_column].head(5).tolist()
            raise ValueError(f"replay minute data has invalid trade_time values: {bad}")
        frame["minute_sort"] = frame["minute_key"].map(minute_sort)
        frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
        if "open" not in frame.columns:
            frame["open"] = frame["close"]
        else:
            frame["open"] = pd.to_numeric(frame["open"], errors="coerce")
        if "high" not in frame.columns:
            frame["high"] = frame[["open", "close"]].max(axis=1)
        else:
            frame["high"] = pd.to_numeric(frame["high"], errors="coerce")
        if "low" not in frame.columns:
            frame["low"] = frame[["open", "close"]].min(axis=1)
        else:
            frame["low"] = pd.to_numeric(frame["low"], errors="coerce")
        frame = frame.sort_values(["trade_date", "minute_sort", "ts_code"], kind="stable").reset_index(drop=True)
        self._frame = frame
        self._date_bounds: dict[str, tuple[int, int]] = {}
        start = 0
        for trade_date, count in frame.groupby("trade_date", sort=False).size().items():
            end = start + int(count)
            self._date_bounds[str(trade_date)] = (start, end)
            start = end

    def rows_for_date(self, trade_date: str) -> pd.DataFrame:
        bounds = self._date_bounds.get(str(trade_date))
        if bounds is None:
            return self._frame.iloc[0:0].copy()
        start, end = bounds
        return self._frame.iloc[start:end].copy()


def _minute_key(value: object) -> str | None:
    if value is None or pd.isna(value):
        return None
    text = str(value).strip()
    if not text:
        return None
    match = re.search(r"(\d{1,2}):(\d{2})", text)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        return f"{hour:02d}:{minute:02d}" if 0 <= hour <= 23 and 0 <= minute <= 59 else None
    digits = re.sub(r"\D", "", text)
    if len(digits) >= 12:
        hour, minute = int(digits[8:10]), int(digits[10:12])
        return f"{hour:02d}:{minute:02d}" if 0 <= hour <= 23 and 0 <= minute <= 59 else None
    if len(digits) in {4, 6}:
        hour, minute = int(digits[:2]), int(digits[2:4])
        return f"{hour:02d}:{minute:02d}" if 0 <= hour <= 23 and 0 <= minute <= 59 else None
    parsed = pd.to_datetime(text, errors="coerce")
    if pd.isna(parsed):
        return None
    return parsed.strftime("%H:%M")


def minute_sort(minute_key: str) -> int:
    hour, minute = str(minute_key).split(":", maxsplit=1)
    return int(hour) * 60 + int(minute)


def empty_minute_rows() -> pd.DataFrame:
    return pd.DataFrame(columns=["trade_date", "ts_code", "open", "close", "high", "low", "minute_key", "minute_sort"])


def _synthetic_daily_minutes(replay_daily: pd.DataFrame, trade_date: str) -> pd.DataFrame:
    """Fallback minute bars (09:30 open, 15:00 close) for daily-only dates.

    Both bars carry ``synthetic=True``: the 15:00 bar's high/low span the whole
    session, so the Broker restricts synthetic bars to reference-price fills
    (no range trade-through — see ``broker._limit_fill_price``). The day range
    stays visible at the 15:00 tick, where it is legitimately known.
    """
    rows = replay_daily[replay_daily["trade_date"].astype(str) == str(trade_date)].copy()
    if rows.empty:
        return empty_minute_rows()
    lows = rows.apply(_daily_low, axis=1)
    highs = rows.apply(_daily_high, axis=1)
    open_rows = rows.copy()
    open_rows["close"] = open_rows["open"]
    # The 09:30 open bar must expose only the opening price: day high/low and the
    # full-day vol/amount are post-open information and would leak look-ahead.
    open_rows["high"] = open_rows["open"]
    open_rows["low"] = open_rows["open"]
    for column in ("vol", "amount"):
        if column in open_rows.columns:
            open_rows[column] = math.nan
    open_rows["minute_key"] = "09:30"
    close_rows = rows.copy()
    close_rows["open"] = close_rows["close"]
    close_rows["high"] = highs
    close_rows["low"] = lows
    close_rows["minute_key"] = "15:00"
    frame = pd.concat([open_rows, close_rows], ignore_index=True)
    frame["synthetic"] = True
    frame["minute_sort"] = frame["minute_key"].map(minute_sort)
    return frame.sort_values(["minute_sort", "ts_code"], kind="stable").reset_index(drop=True)


def minute_rows_with_daily_fallback(
    replay_daily: pd.DataFrame,
    trade_date: str,
    minute_rows: pd.DataFrame,
) -> pd.DataFrame:
    fallback = _synthetic_daily_minutes(replay_daily, trade_date)
    if minute_rows.empty:
        return fallback
    present_codes = set(minute_rows["ts_code"].astype(str))
    missing_rows = fallback[~fallback["ts_code"].astype(str).isin(present_codes)]
    close_fallback = fallback[
        (fallback["minute_key"] == "15:00")
        & fallback["ts_code"].astype(str).isin(present_codes)
    ].copy()
    if not close_fallback.empty:
        existing_keys = set(zip(minute_rows["ts_code"].astype(str), minute_rows["minute_key"].astype(str)))
        close_fallback = close_fallback[
            [
                (str(row.ts_code), str(row.minute_key)) not in existing_keys
                for row in close_fallback.itertuples()
            ]
        ]
    if missing_rows.empty and close_fallback.empty:
        return minute_rows
    return pd.concat([minute_rows, missing_rows, close_fallback], ignore_index=True).sort_values(
        ["minute_sort", "ts_code"],
        kind="stable",
    ).reset_index(drop=True)


def _daily_low(bar: pd.Series) -> float:
    values = [bar.get("low"), bar.get("open"), bar.get("close")]
    numeric = [float(value) for value in values if pd.notna(value)]
    return min(numeric) if numeric else math.nan


def _daily_high(bar: pd.Series) -> float:
    values = [bar.get("high"), bar.get("open"), bar.get("close")]
    numeric = [float(value) for value in values if pd.notna(value)]
    return max(numeric) if numeric else math.nan
