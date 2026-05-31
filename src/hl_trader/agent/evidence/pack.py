from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
import json
from pathlib import Path
from typing import Any

import pandas as pd

from hl_trader.environment.storage.ledger import stable_hash, to_jsonable, utc_now_iso


PIT_METADATA_COLUMNS = ("feature_date", "source_trade_date", "tradable_date", "available_at")
KNOWN_UNITS = {
    "pct_chg": "percent",
    "ret_1d": "ratio",
    "ret_5d": "ratio",
    "ret_20d": "ratio",
    "ret_60d": "ratio",
    "volatility_20d": "ratio",
    "amount": "thousand_cny",
    "amount_ma20": "thousand_cny",
    "vol": "lot",
    "turnover_rate": "percent",
    "turnover_rate_f": "percent",
    "total_mv": "ten_thousand_cny",
    "circ_mv": "ten_thousand_cny",
}


def hash_payload(payload: Any) -> str:
    return stable_hash(payload)


@dataclass(frozen=True)
class EvidenceItem:
    name: str
    payload: dict[str, Any]
    source: str
    as_of: str | date | datetime
    payload_hash: str = ""

    def to_record(self) -> dict[str, Any]:
        record = {
            "name": self.name,
            "payload": self.payload,
            "source": self.source,
            "as_of": self.as_of,
        }
        expected_hash = hash_payload(record["payload"])
        if self.payload_hash and self.payload_hash != expected_hash:
            raise ValueError(f"payload_hash mismatch for evidence item {self.name}")
        record["payload_hash"] = expected_hash
        return to_jsonable(record)


@dataclass(frozen=True)
class EvidencePack:
    pack_id: str
    decision_date: str
    tradable_date: str
    ts_codes: tuple[str, ...]
    items: tuple[EvidenceItem, ...]
    created_at: str = field(default_factory=utc_now_iso)
    schema_version: str = "evidence_pack.v1"

    def to_record(self) -> dict[str, Any]:
        items = [item.to_record() for item in self.items]
        record = {
            "schema_version": self.schema_version,
            "pack_id": self.pack_id,
            "decision_date": self.decision_date,
            "tradable_date": self.tradable_date,
            "ts_codes": list(self.ts_codes),
            "items": items,
            "created_at": self.created_at,
        }
        record["pack_hash"] = pack_content_hash(record)
        return to_jsonable(record)


def pack_content_hash(record: dict[str, Any]) -> str:
    return hash_payload({
        key: value
        for key, value in record.items()
        if key not in {"pack_hash", "pack_id", "created_at"}
    })


def verify_pack_record(record: dict[str, Any]) -> None:
    for item in record.get("items", []):
        expected = hash_payload(item.get("payload", {}))
        if item.get("payload_hash") != expected:
            raise ValueError(f"evidence item payload_hash verification failed: {item.get('name')}")
    expected_pack_hash = pack_content_hash(record)
    if record.get("pack_hash") != expected_pack_hash:
        raise ValueError("evidence pack_hash verification failed")
    if record.get("pack_id") != expected_pack_hash:
        raise ValueError("evidence pack_id verification failed")


class EvidencePackBuilder:
    def __init__(self, *, source_system: str = "offline_feature_store") -> None:
        self.source_system = source_system

    def from_feature_cross_section(
        self,
        frame: pd.DataFrame,
        *,
        decision_date: str,
        tradable_date: str,
        ts_codes: list[str] | tuple[str, ...],
        feature_columns: list[str],
    ) -> EvidencePack:
        decision_key = _date_key(decision_date, "decision_date")
        tradable_key = _date_key(tradable_date, "tradable_date")
        codes = _unique_codes(ts_codes)
        if not feature_columns:
            raise ValueError("feature_columns cannot be empty")
        required = {"ts_code", *feature_columns}
        missing = required - set(frame.columns)
        if missing:
            raise ValueError(f"feature cross section missing columns: {sorted(missing)}")

        working = frame.copy()
        working["ts_code"] = working["ts_code"].astype(str)
        subset = working[working["ts_code"].isin(codes)].copy()
        if subset.empty:
            raise ValueError("feature cross section contains none of the requested ts_codes")
        duplicated = subset.duplicated("ts_code", keep=False)
        if duplicated.any():
            sample = subset.loc[duplicated, ["ts_code"]].head(5).to_dict("records")
            raise ValueError(f"feature cross section has duplicate ts_code rows: {sample}")
        missing_codes = sorted(set(codes) - set(subset["ts_code"].astype(str)))
        if missing_codes:
            raise ValueError(f"feature cross section missing requested ts_codes: {missing_codes}")

        _validate_no_future_date(subset, "feature_date", decision_key)
        _validate_no_future_date(subset, "source_trade_date", decision_key)
        _validate_available_at(subset, decision_key)
        if "tradable_date" in subset.columns:
            tradable_values = _date_values(subset["tradable_date"], "tradable_date")
            if tradable_values != {tradable_key}:
                raise ValueError(f"feature cross section tradable_date mismatch: {sorted(tradable_values)} != {tradable_key}")

        metadata_columns = [col for col in PIT_METADATA_COLUMNS if col in subset.columns and col not in feature_columns]
        output_columns = ["ts_code", *metadata_columns, *feature_columns]
        subset = subset[output_columns].sort_values("ts_code")
        units = {col: KNOWN_UNITS[col] for col in output_columns if col in KNOWN_UNITS}
        payload = {
            "rows": subset.to_dict(orient="records"),
            "feature_columns": list(feature_columns),
            "metadata_columns": metadata_columns,
            "row_count": int(len(subset)),
            "units": units,
            "pit": {
                "decision_date": decision_key,
                "tradable_date": tradable_key,
                "feature_date_max": _max_date_value(subset, "feature_date"),
                "source_trade_date_max": _max_date_value(subset, "source_trade_date"),
                "available_at_max": _max_datetime_value(subset, "available_at"),
            },
        }
        item = EvidenceItem(
            name="feature_snapshot",
            payload=payload,
            source=self.source_system,
            as_of=decision_key,
        )
        item_record = item.to_record()
        pack_id = hash_payload({
            "schema_version": "evidence_pack.v1",
            "decision_date": decision_key,
            "tradable_date": tradable_key,
            "ts_codes": list(codes),
            "items": [item_record],
        })
        return EvidencePack(pack_id, decision_key, tradable_key, codes, (item,))

    @staticmethod
    def append_jsonl(path: str | Path, pack: EvidencePack) -> None:
        output = Path(path)
        output.parent.mkdir(parents=True, exist_ok=True)
        record = pack.to_record()
        verify_pack_record(record)
        with output.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    @staticmethod
    def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
        input_path = Path(path)
        if not input_path.exists():
            return []
        records: list[dict[str, Any]] = []
        with input_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    record = json.loads(line)
                    verify_pack_record(record)
                    records.append(record)
        return records


def _unique_codes(ts_codes: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    codes = tuple(str(code) for code in ts_codes)
    if not codes:
        raise ValueError("ts_codes cannot be empty")
    if len(codes) != len(set(codes)):
        raise ValueError("ts_codes must be unique")
    return codes


def _date_key(value: Any, label: str) -> str:
    if pd.isna(value):
        raise ValueError(f"{label} cannot be missing")
    try:
        return pd.Timestamp(value).strftime("%Y%m%d")
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"invalid {label}: {value}") from exc


def _date_values(values: pd.Series, label: str) -> set[str]:
    return {_date_key(value, label) for value in values.dropna()}


def _validate_no_future_date(frame: pd.DataFrame, column: str, decision_key: str) -> None:
    if column not in frame.columns:
        return
    future_values = sorted(value for value in _date_values(frame[column], column) if value > decision_key)
    if future_values:
        raise ValueError(f"{column} contains values after decision_date {decision_key}: {future_values[:3]}")


def _validate_available_at(frame: pd.DataFrame, decision_key: str) -> None:
    if "available_at" not in frame.columns:
        return
    future_values = sorted(
        value for value in _date_values(frame["available_at"], "available_at")
        if value > decision_key
    )
    if future_values:
        raise ValueError(f"available_at contains values after decision_date {decision_key}: {future_values[:3]}")


def _max_date_value(frame: pd.DataFrame, column: str) -> str | None:
    if column not in frame.columns:
        return None
    values = _date_values(frame[column], column)
    return max(values) if values else None


def _max_datetime_value(frame: pd.DataFrame, column: str) -> str | None:
    if column not in frame.columns:
        return None
    values = [value for value in frame[column].dropna()]
    if not values:
        return None
    timestamps = [pd.Timestamp(value).tz_convert("UTC") if pd.Timestamp(value).tzinfo else pd.Timestamp(value).tz_localize("UTC") for value in values]
    return to_jsonable(max(timestamps).to_pydatetime())
