from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pandas as pd

CN_TZ = ZoneInfo("Asia/Shanghai")
DAILY_CLOSE_EARLIEST_AVAILABLE = time(15, 0)
NEXT_SESSION_CHECK_TIME = time(9, 25)


@dataclass(frozen=True)
class LeakageViolation:
    row_index: int
    check: str
    message: str


def _parse_yyyymmdd(value: object) -> date:
    if isinstance(value, pd.Timestamp):
        return value.date()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if len(text) == 8 and text.isdigit():
        return datetime.strptime(text, "%Y%m%d").date()
    return pd.Timestamp(text).date()


def _parse_time(value: object) -> datetime:
    if isinstance(value, pd.Timestamp):
        parsed = value.to_pydatetime()
    elif isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=CN_TZ)
    return parsed.astimezone(CN_TZ)


def find_feature_leakage(frame: pd.DataFrame) -> list[LeakageViolation]:
    required = {"feature_date", "tradable_date", "available_at", "ts_code"}
    missing = sorted(required - set(frame.columns))
    if missing:
        return [LeakageViolation(-1, "schema", f"missing required columns: {missing}")]

    violations: list[LeakageViolation] = []
    work = frame.reset_index(drop=True)
    duplicate_mask = work.duplicated(["feature_date", "ts_code"], keep=False)
    for idx in work.index[duplicate_mask]:
        violations.append(
            LeakageViolation(int(idx), "duplicate_key", "feature_date/ts_code must identify at most one feature row")
        )

    for idx, row in work.iterrows():
        try:
            feature_date = _parse_yyyymmdd(row["feature_date"])
            tradable_date = _parse_yyyymmdd(row["tradable_date"])
            available_at = _parse_time(row["available_at"])
        except Exception as exc:
            violations.append(LeakageViolation(int(idx), "parse", str(exc)))
            continue

        if tradable_date <= feature_date:
            violations.append(LeakageViolation(int(idx), "tradable_date", "tradable_date must be after feature_date for daily close features"))

        earliest_close_availability = datetime.combine(feature_date, DAILY_CLOSE_EARLIEST_AVAILABLE, tzinfo=CN_TZ)
        if available_at < earliest_close_availability:
            violations.append(
                LeakageViolation(int(idx), "available_at", "available_at is before the feature_date market close")
            )

        tradable_session = datetime.combine(tradable_date, NEXT_SESSION_CHECK_TIME, tzinfo=CN_TZ)
        if available_at >= tradable_session:
            violations.append(LeakageViolation(int(idx), "available_at", "available_at must be before next tradable session"))

        if "source_trade_date" in work.columns and pd.notna(row.get("source_trade_date")):
            source_date = _parse_yyyymmdd(row["source_trade_date"])
            if source_date > feature_date:
                violations.append(LeakageViolation(int(idx), "source_trade_date", "source_trade_date is after feature_date"))
    return violations


def assert_no_feature_leakage(frame: pd.DataFrame) -> None:
    violations = find_feature_leakage(frame)
    if violations:
        sample = "; ".join(f"row={v.row_index} {v.check}: {v.message}" for v in violations[:5])
        raise AssertionError(f"feature leakage checks failed: {sample}")
