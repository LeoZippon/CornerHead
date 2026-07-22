from __future__ import annotations

import json
import warnings
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from autotrade.environment.data.contracts import CN_TZ


def concat_rows(frames: list[pd.DataFrame], **kwargs) -> pd.DataFrame:
    """Row-union concat for parquet-sourced frames.

    Empty inputs are dropped (their inclusion in dtype inference is deprecated
    and warns per call). Real frames may still carry all-NA columns — genuine
    sparse schema, not removable — for which pandas' future behavior (typed
    all-NA columns participate in inference) equals today's outcome because
    the columns are parquet-typed; that residual FutureWarning is accepted
    noise and suppressed at this single boundary."""
    non_empty = [frame for frame in frames if not frame.empty]
    if not non_empty:
        # Preserve the column union and dtypes of the empty inputs instead of
        # collapsing to a columnless frame (writers persist the schema; readers
        # select columns on the result).
        columns: dict[str, object] = {}
        for frame in frames:
            for column in frame.columns:
                columns.setdefault(column, frame[column].dtype)
        return pd.DataFrame({column: pd.Series(dtype=dtype) for column, dtype in columns.items()})
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=FutureWarning, message=".*empty or all-NA entries.*")
        merged = pd.concat(non_empty, **kwargs)
    # Empty inputs still contribute schema: columns only they carry are added
    # as typed all-NA via reindex (integer dtypes promote to float, exactly as
    # a plain concat would have produced).
    for frame in frames:
        if frame.empty:
            for column in frame.columns:
                if column not in merged.columns:
                    merged[column] = pd.Series(dtype=frame[column].dtype).reindex(merged.index)
    return merged


def parquet_meta(path: Path) -> dict[str, Any]:
    """Read a raw-lake parquet's ``<file>.meta.json`` sidecar (empty if absent).

    The sidecar scheme is part of the raw-lake contract: the ingest adapter
    writes it, PIT consumers read it through this single helper."""
    meta_path = path.with_suffix(path.suffix + ".meta.json")
    if not meta_path.exists():
        return {}
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def to_cn_timestamps(series: pd.Series) -> pd.Series:
    """Parse available_at values to Asia/Shanghai timestamps.

    Raw datasets mix tz-aware ISO strings (e.g. margin) and tz-naive Beijing
    wall-clock strings (e.g. anns_d rec_time); naive values must be localized
    to CN, never treated as UTC.

    Fast path first: replay/snapshot columns are usually uniform
    "YYYY-MM-DDTHH:MM:SS+08:00" strings, and pandas' generic tz-aware parser
    costs ~5µs/row — a quarter of minute bars is tens of millions of rows.
    The fixed-format naive parse plus one localize is ~10x faster and applies
    only when a vectorized suffix check proves the offset is uniform CN.
    """
    if series.dtype == object and len(series):
        text = series.astype(str)
        if text.str.endswith("+08:00").all():
            fast = pd.to_datetime(text.str.slice(0, 19), format="%Y-%m-%dT%H:%M:%S", errors="coerce")
            if not fast.isna().any():
                # Localize via a UTC shift: tz_localize(CN_TZ) with a ZoneInfo
                # walks rows one by one, the UTC route is metadata-only.
                return (fast - pd.Timedelta(hours=8)).dt.tz_localize("UTC").dt.tz_convert(CN_TZ)
    try:
        parsed = pd.to_datetime(series, errors="coerce")
    except (ValueError, TypeError):
        parsed = None
    if parsed is not None and getattr(parsed.dtype, "tz", None) is not None:
        return parsed.dt.tz_convert(CN_TZ)
    if parsed is not None and parsed.dtype != object:
        return parsed.dt.tz_localize(CN_TZ)
    # Mixed aware/naive values: normalize element-wise.
    fallback = pd.to_datetime(series, errors="coerce", utc=False)
    if fallback.dtype != object:
        if getattr(fallback.dtype, "tz", None) is not None:
            return fallback.dt.tz_convert(CN_TZ)
        return fallback.dt.tz_localize(CN_TZ)
    return fallback.map(
        lambda value: (value.tz_localize(CN_TZ) if value.tzinfo is None else value.tz_convert(CN_TZ))
        if pd.notna(value)
        else pd.NaT
    )


def yyyymmdd(value: date | datetime | str) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y%m%d")
    if isinstance(value, date):
        return value.strftime("%Y%m%d")
    text = str(value).strip()
    if len(text) == 8 and text.isdigit():
        return text
    try:
        return pd.Timestamp(text).strftime("%Y%m%d")
    except Exception as exc:
        raise ValueError(f"cannot parse date value as YYYYMMDD: {value!r}") from exc


def parse_partition_date(path: Path) -> str:
    stem = path.stem
    return stem.split("=", 1)[1] if "=" in stem else ""


@dataclass(frozen=True)
class PITDataStore:
    raw_dir: Path

    def dataset_dir(self, dataset: str) -> Path:
        path = self.raw_dir / dataset
        if not path.exists():
            raise FileNotFoundError(f"missing dataset directory: {path}")
        return path

    def trade_dates(self, dataset: str) -> list[str]:
        return sorted(
            key
            for key in (parse_partition_date(path) for path in self.dataset_dir(dataset).glob("trade_date=*.parquet"))
            if key
        )

    def read_trade_date(self, dataset: str, trade_date: date | str, columns: list[str] | None = None) -> pd.DataFrame:
        key = yyyymmdd(trade_date)
        path = self.dataset_dir(dataset) / f"trade_date={key}.parquet"
        if not path.exists():
            raise FileNotFoundError(f"missing partition: {path}")
        return pd.read_parquet(path, columns=columns)

    def read_trade_range(self, dataset: str, start: date | str, end: date | str, columns: list[str] | None = None) -> pd.DataFrame:
        start_key = yyyymmdd(start)
        end_key = yyyymmdd(end)
        if start_key > end_key:
            return pd.DataFrame(columns=columns) if columns else pd.DataFrame()
        frames = []
        for key in self.trade_dates(dataset):
            if start_key <= key <= end_key:
                frame = self.read_trade_date(dataset, key, columns=columns)
                # Empty partitions are excluded so concat dtype inference never
                # sees empty entries (deprecated in pandas, warning per call).
                if not frame.empty:
                    frames.append(frame)
        if frames:
            return pd.concat(frames, ignore_index=True)
        return pd.DataFrame(columns=columns) if columns else pd.DataFrame()
