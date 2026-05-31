from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timezone
import hashlib
import json
import math
from numbers import Integral, Real
from pathlib import Path
from typing import Any


UTC = timezone.utc


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def to_jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return to_jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, set):
        converted = [to_jsonable(item) for item in value]
        return sorted(converted, key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True, allow_nan=False))
    if isinstance(value, (list, tuple)):
        return [to_jsonable(item) for item in value]
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        else:
            value = value.astimezone(UTC)
        return value.isoformat(timespec="seconds").replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if value.__class__.__name__ == "NAType":
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, Integral):
        return int(value)
    if isinstance(value, Real):
        numeric = float(value)
        return numeric if math.isfinite(numeric) else None
    return value


def stable_hash(value: Any) -> str:
    encoded = json.dumps(
        to_jsonable(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _record_payload(record: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in record.items() if key != "record_hash"}


class TrialLedger:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, event: dict[str, Any]) -> None:
        record = dict(event)
        record.setdefault("recorded_at", utc_now_iso())
        expected_hash = stable_hash(_record_payload(record))
        if "record_hash" in record and record["record_hash"] != expected_hash:
            raise ValueError("ledger record_hash verification failed before append")
        record["record_hash"] = expected_hash
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(to_jsonable(record), ensure_ascii=False, sort_keys=True) + "\n")

    def read_all(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        records = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    record = json.loads(line)
                    expected_hash = record.get("record_hash")
                    if expected_hash is not None and expected_hash != stable_hash(_record_payload(record)):
                        raise ValueError("ledger record_hash verification failed")
                    records.append(record)
        return records
