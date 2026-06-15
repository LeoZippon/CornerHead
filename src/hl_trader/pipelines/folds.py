"""Quarterly rolling Fold schedule (docs/pipeline_design.md chapter 2).

A fold is named after its test quarter. The previous quarter is its validation
period, the 21 months before the validation period are the input window, and
decision times are the first trading day of each period at 09:25 Beijing time
(pre-open, after the pre-open data gates).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path

import pandas as pd

from hl_trader.environment.data.contracts import CN_TZ

QUARTER_PATTERN = re.compile(r"^(\d{4})Q([1-4])$")
DECISION_TIME = time(9, 25)
DEFAULT_WINDOW_MONTHS = 21


@dataclass(frozen=True)
class FoldSpec:
    fold_id: str
    input_window_start: str
    input_window_end: str
    validation_start: str
    validation_end: str
    test_start: str
    test_end: str
    valid_decision_time: datetime
    test_decision_time: datetime

    def to_record(self) -> dict[str, object]:
        return {
            "fold_id": self.fold_id,
            "input_window": f"{self.input_window_start}..{self.input_window_end}",
            "validation_period": f"{self.validation_start}..{self.validation_end}",
            "test_period": f"{self.test_start}..{self.test_end}",
            "valid_decision_time": self.valid_decision_time.isoformat(),
            "test_decision_time": self.test_decision_time.isoformat(),
        }


def parse_quarter(label: str) -> tuple[int, int]:
    match = QUARTER_PATTERN.match(label.strip())
    if not match:
        raise ValueError(f"invalid quarter label: {label!r} (expected e.g. 2022Q1)")
    return int(match.group(1)), int(match.group(2))


def quarter_bounds(label: str) -> tuple[str, str]:
    year, quarter = parse_quarter(label)
    start = pd.Timestamp(year=year, month=3 * (quarter - 1) + 1, day=1)
    end = start + pd.DateOffset(months=3) - pd.Timedelta(days=1)
    return start.strftime("%Y%m%d"), end.strftime("%Y%m%d")


def previous_quarter(label: str) -> str:
    year, quarter = parse_quarter(label)
    return f"{year - 1}Q4" if quarter == 1 else f"{year}Q{quarter - 1}"


def next_quarter(label: str) -> str:
    year, quarter = parse_quarter(label)
    return f"{year + 1}Q1" if quarter == 4 else f"{year}Q{quarter + 1}"


def quarter_range(first: str, last: str) -> list[str]:
    parse_quarter(first), parse_quarter(last)
    labels = [first]
    while labels[-1] != last:
        labels.append(next_quarter(labels[-1]))
        if len(labels) > 200:
            raise ValueError(f"quarter range too large or inverted: {first}..{last}")
    return labels


def first_trading_day(start: str, end: str, trading_days: list[str]) -> str:
    for day in trading_days:
        if start <= day <= end:
            return day
    raise ValueError(f"no trading day inside {start}..{end}")


def build_fold_schedule(
    first_test_quarter: str,
    last_test_quarter: str,
    trading_days: list[str],
    *,
    window_months: int = DEFAULT_WINDOW_MONTHS,
) -> list[FoldSpec]:
    folds: list[FoldSpec] = []
    for test_quarter in quarter_range(first_test_quarter, last_test_quarter):
        validation_quarter = previous_quarter(test_quarter)
        validation_start, validation_end = quarter_bounds(validation_quarter)
        test_start, test_end = quarter_bounds(test_quarter)
        window_start = pd.Timestamp(validation_start) - pd.DateOffset(months=window_months)
        window_end = pd.Timestamp(validation_start) - pd.Timedelta(days=1)
        folds.append(
            FoldSpec(
                fold_id=f"fold_{test_quarter}",
                input_window_start=window_start.strftime("%Y%m%d"),
                input_window_end=window_end.strftime("%Y%m%d"),
                validation_start=validation_start,
                validation_end=validation_end,
                test_start=test_start,
                test_end=test_end,
                valid_decision_time=_decision_time(validation_start, validation_end, trading_days),
                test_decision_time=_decision_time(test_start, test_end, trading_days),
            )
        )
    return folds


def heldout_periods(first_quarter: str, last_quarter: str, trading_days: list[str]) -> list[dict[str, object]]:
    """Per-quarter held-out replay periods with frozen decision times."""
    periods = []
    for label in quarter_range(first_quarter, last_quarter):
        start, end = quarter_bounds(label)
        periods.append(
            {
                "label": label,
                "start": start,
                "end": end,
                "decision_time": _decision_time(start, end, trading_days),
            }
        )
    return periods


def assert_no_overlap(development_last_test_quarter: str, heldout_first_quarter: str) -> None:
    """Held-out must be configured upfront and not overlap development."""
    dev_end = quarter_bounds(development_last_test_quarter)[1]
    heldout_start = quarter_bounds(heldout_first_quarter)[0]
    if heldout_start <= dev_end:
        raise ValueError(
            f"held-out starts {heldout_start} but development runs through {dev_end}; periods must not overlap"
        )


def load_sse_trading_days(raw_dir: str | Path) -> list[str]:
    calendar_dir = Path(raw_dir) / "trade_cal" / "exchange=SSE"
    if not calendar_dir.exists():
        raise FileNotFoundError(f"missing SSE trade calendar: {calendar_dir}")
    frames = [pd.read_parquet(path, columns=["cal_date", "is_open"]) for path in sorted(calendar_dir.glob("year=*.parquet"))]
    if not frames:
        raise FileNotFoundError(f"no trade calendar partitions under {calendar_dir}")
    calendar = pd.concat(frames, ignore_index=True)
    open_days = calendar[calendar["is_open"].astype(str) == "1"]["cal_date"].astype(str)
    return sorted(set(open_days))


def _decision_time(start: str, end: str, trading_days: list[str]) -> datetime:
    day = first_trading_day(start, end, trading_days)
    return datetime.strptime(day, "%Y%m%d").replace(
        hour=DECISION_TIME.hour, minute=DECISION_TIME.minute, tzinfo=CN_TZ
    )
