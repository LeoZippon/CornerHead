"""Minute replay market data and the daily-synthesized bar fallback.

The per-minute ``main(ctx)`` engine (``main_ctx_engine.py``) replays real
minute bars when the slot carries them and falls back to two synthetic bars
per day (09:30 open, 15:00 close) built from the daily row. Synthetic bars are
flagged so the Broker restricts them to reference-price fills.
"""

from __future__ import annotations

import concurrent.futures
import math
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq


_MARKET_VALUE_COLUMNS = ("open", "high", "low", "close", "vol", "amount")


@dataclass(frozen=True)
class MinuteReplayPartition:
    """One replay day's raw Timeview rows plus the normalized Broker rows."""

    trade_date: str
    market_rows: pd.DataFrame
    timeview_rows: pd.DataFrame | None


class ParquetMinuteReplaySource:
    """Read and normalize one Parquet trade-date partition ahead of replay.

    Replay files are written with one or more row groups per trade date. Keeping
    only the current day and one prefetched day avoids retaining and re-sorting a
    whole quarter in pandas while preserving the existing single-file contract.
    """

    def __init__(
        self,
        path: Path,
        *,
        trade_dates: tuple[str, ...] | None = None,
        include_timeview_rows: bool = True,
    ) -> None:
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(f"missing replay minute data: {self.path}")
        footer = pq.ParquetFile(self.path)
        self.source_rows = int(footer.metadata.num_rows)
        if self.source_rows:
            missing = [
                name for name in MinuteMarketData.REQUIRED
                if name not in footer.schema_arrow.names
            ]
            if missing:
                raise ValueError(f"replay minute data missing columns: {missing}")
        self._include_timeview_rows = bool(include_timeview_rows)
        self._allowed_dates = (
            frozenset(map(str, trade_dates)) if trade_dates is not None else None
        )
        self.selected_rows = _selected_footer_rows(footer, self._allowed_dates)
        self._time_column = next(
            (name for name in MinuteMarketData.TIME_COLUMNS if name in footer.schema_arrow.names),
            None,
        )
        if self.source_rows and self._time_column is None:
            raise ValueError(
                f"replay minute data missing one of time columns: {list(MinuteMarketData.TIME_COLUMNS)}"
            )
        if self._include_timeview_rows:
            self._columns: list[str] | None = None
        else:
            wanted = {"trade_date", "ts_code", *_MARKET_VALUE_COLUMNS, self._time_column}
            self._columns = [name for name in footer.schema_arrow.names if name in wanted]
        self._pool: concurrent.futures.ThreadPoolExecutor | None = None
        self._future: concurrent.futures.Future[MinuteReplayPartition] | None = None
        self._future_date: str | None = None
        self._closed = False
        self._lock = threading.Lock()
        self._read_seconds = 0.0
        self._normalize_seconds = 0.0
        self._wait_seconds = 0.0
        self._rows_loaded = 0
        self._partitions_loaded = 0

    def prefetch(self, trade_date: str) -> None:
        """Start exactly one future day; duplicate requests are harmless."""
        date = str(trade_date)
        if self._closed:
            raise RuntimeError("minute replay source is closed")
        if self._allowed_dates is not None and date not in self._allowed_dates:
            return
        if self._future is not None:
            if self._future_date == date:
                return
            raise RuntimeError(
                f"minute replay prefetch already holds {self._future_date}; cannot queue {date}"
            )
        if self._pool is None:
            self._pool = concurrent.futures.ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="replay-minute-prefetch",
            )
        self._future_date = date
        self._future = self._pool.submit(self._load, date)

    def rows_for_date(
        self,
        trade_date: str,
        *,
        next_trade_date: str | None = None,
    ) -> MinuteReplayPartition:
        date = str(trade_date)
        if self._allowed_dates is not None and date not in self._allowed_dates:
            partition = MinuteReplayPartition(date, empty_minute_rows(), None)
        else:
            self.prefetch(date)
            future = self._future
            if future is None:  # pragma: no cover - prefetch(date) always installs it
                raise RuntimeError(f"minute replay prefetch did not start for {date}")
            wait_started = time.monotonic()
            try:
                partition = future.result()
            finally:
                with self._lock:
                    self._wait_seconds += time.monotonic() - wait_started
                self._future = None
                self._future_date = None
        if next_trade_date is not None:
            self.prefetch(str(next_trade_date))
        return partition

    def stats(self) -> dict[str, object]:
        with self._lock:
            return {
                "minutes_seconds": round(self._read_seconds, 6),
                "minute_normalize_seconds": round(self._normalize_seconds, 6),
                "minute_prefetch_wait_seconds": round(self._wait_seconds, 6),
                "minute_partitions_loaded": int(self._partitions_loaded),
                "minute_rows_loaded": int(self._rows_loaded),
            }

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._pool is not None:
            self._pool.shutdown(wait=True, cancel_futures=True)
            self._pool = None
        self._future = None
        self._future_date = None

    def __enter__(self) -> "ParquetMinuteReplaySource":
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.close()

    def _load(self, trade_date: str) -> MinuteReplayPartition:
        read_started = time.monotonic()
        raw = pd.read_parquet(
            self.path,
            columns=self._columns,
            filters=[("trade_date", "==", str(trade_date))],
        )
        read_seconds = time.monotonic() - read_started
        normalize_started = time.monotonic()
        market = normalize_minute_rows(raw) if not raw.empty else empty_minute_rows()
        normalize_seconds = time.monotonic() - normalize_started
        with self._lock:
            self._read_seconds += read_seconds
            self._normalize_seconds += normalize_seconds
            self._rows_loaded += len(raw)
            self._partitions_loaded += 1
        return MinuteReplayPartition(
            str(trade_date),
            market,
            raw if self._include_timeview_rows else None,
        )


def _selected_footer_rows(footer: pq.ParquetFile, allowed_dates: frozenset[str] | None) -> int:
    if allowed_dates is None:
        return int(footer.metadata.num_rows)
    try:
        date_index = footer.schema_arrow.names.index("trade_date")
    except ValueError:
        return 0
    selected = 0
    for index in range(footer.metadata.num_row_groups):
        group = footer.metadata.row_group(index)
        statistics = group.column(date_index).statistics
        if statistics is None or not statistics.has_min_max:
            # The builder writes date-homogeneous row groups with statistics. A
            # legacy file without them remains readable; count it conservatively.
            selected += group.num_rows
            continue
        minimum, maximum = str(statistics.min), str(statistics.max)
        if any(minimum <= date <= maximum for date in allowed_dates):
            selected += group.num_rows
    return int(selected)


class MinuteMarketData:
    """Minute replay bars indexed by trade date, minute, and code."""

    REQUIRED = ("trade_date", "ts_code", "close")
    TIME_COLUMNS = ("trade_time", "datetime", "timestamp", "time")

    def __init__(self, minutes: pd.DataFrame) -> None:
        if minutes.empty:
            raise ValueError("minute replay data is empty")
        frame = normalize_minute_rows(minutes)
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


def normalize_minute_rows(minutes: pd.DataFrame) -> pd.DataFrame:
    """Project and normalize Broker-visible minute columns with vectorized time parsing."""
    if minutes.empty:
        return empty_minute_rows()
    missing = [col for col in MinuteMarketData.REQUIRED if col not in minutes.columns]
    if missing:
        raise ValueError(f"replay minute data missing columns: {missing}")
    time_column = next((col for col in MinuteMarketData.TIME_COLUMNS if col in minutes.columns), None)
    if time_column is None:
        raise ValueError(
            f"replay minute data missing one of time columns: {list(MinuteMarketData.TIME_COLUMNS)}"
        )
    columns = ["trade_date", "ts_code", time_column]
    columns.extend(column for column in _MARKET_VALUE_COLUMNS if column in minutes.columns)
    frame = minutes.loc[:, list(dict.fromkeys(columns))].copy()
    frame["trade_date"] = frame["trade_date"].astype(str)
    frame["ts_code"] = frame["ts_code"].astype(str)
    frame["minute_key"], frame["minute_sort"] = _minute_columns(frame[time_column])
    if frame["minute_key"].isna().any():
        bad = frame.loc[frame["minute_key"].isna(), time_column].head(5).tolist()
        raise ValueError(f"replay minute data has invalid trade_time values: {bad}")
    frame["minute_sort"] = frame["minute_sort"].astype("int64")
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
    return frame.sort_values(
        ["trade_date", "minute_sort", "ts_code"],
        kind="stable",
    ).reset_index(drop=True)


def _minute_columns(values: pd.Series) -> tuple[pd.Series, pd.Series]:
    """Parse each distinct timestamp once, then expand by integer dictionary codes.

    A daily A-share partition has roughly 240 distinct minute strings repeated
    across thousands of codes. Factorization keeps the exact legacy parser for
    every supported format while replacing millions of Python calls with a
    vectorized integer take.
    """
    codes, unique_values = pd.factorize(values, sort=False, use_na_sentinel=True)
    unique_keys = np.asarray([_minute_key(value) for value in unique_values], dtype=object)
    unique_sorts = np.asarray(
        [minute_sort(key) if key is not None else -1 for key in unique_keys],
        dtype=np.int64,
    )
    keys = np.full(len(values), None, dtype=object)
    sorts = np.full(len(values), -1, dtype=np.int64)
    present = codes >= 0
    keys[present] = unique_keys[codes[present]]
    sorts[present] = unique_sorts[codes[present]]
    nullable_sorts = pd.array(sorts, dtype="Int64")
    nullable_sorts[sorts < 0] = pd.NA
    return (
        pd.Series(keys, index=values.index, dtype=object),
        pd.Series(nullable_sorts, index=values.index),
    )


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
        # Every close_fallback candidate has minute_key == "15:00", so only the
        # day's 15:00 rows can collide — keying the full ~700k-row day frame
        # cost ~0.2s/day of pure waste in host_replay_overhead.
        at_close = minute_rows[minute_rows["minute_key"].astype(str) == "15:00"]
        existing_keys = set(at_close["ts_code"].astype(str))
        close_fallback = close_fallback[
            [str(row.ts_code) not in existing_keys for row in close_fallback.itertuples()]
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
