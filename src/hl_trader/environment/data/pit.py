from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Iterable

import pandas as pd

from .contracts import CN_TZ, DatasetContract


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


def _normalize_decision_time(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=CN_TZ)
    return value.astimezone(CN_TZ)


@dataclass(frozen=True)
class PITDataStore:
    raw_dir: Path
    contracts: dict[str, DatasetContract]

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
                frames.append(self.read_trade_date(dataset, key, columns=columns))
        if frames:
            return pd.concat(frames, ignore_index=True)
        return pd.DataFrame(columns=columns) if columns else pd.DataFrame()

    def assert_visible(self, dataset: str, partition_date: date | str, decision_time: datetime) -> None:
        contract = self.contracts[dataset]
        partition_dt = datetime.strptime(yyyymmdd(partition_date), "%Y%m%d").date()
        decision_dt = _normalize_decision_time(decision_time)
        if contract.available_at(partition_dt) > decision_dt:
            raise ValueError(
                f"{dataset} partition {partition_dt.isoformat()} is not visible at {decision_dt.isoformat()}"
            )

    def latest_visible_trade_date(self, dataset: str, decision_time: datetime) -> str | None:
        contract = self.contracts[dataset]
        decision_dt = _normalize_decision_time(decision_time)
        visible = []
        for key in self.trade_dates(dataset):
            partition_date = datetime.strptime(key, "%Y%m%d").date()
            if contract.available_at(partition_date) <= decision_dt:
                visible.append(key)
        return visible[-1] if visible else None

    def iter_visible_trade_dates(self, dataset: str, decision_time: datetime) -> Iterable[str]:
        contract = self.contracts[dataset]
        decision_dt = _normalize_decision_time(decision_time)
        for key in self.trade_dates(dataset):
            partition_date = datetime.strptime(key, "%Y%m%d").date()
            if contract.available_at(partition_date) <= decision_dt:
                yield key
