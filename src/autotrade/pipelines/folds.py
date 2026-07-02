"""Rolling Fold schedules (docs/pipeline_design.md chapter 2).

A fold is named after its test period. The previous period at the configured
cadence is its validation period, and the months before validation are the input
window. Each segment's decision-input snapshot is anchored at 23:59:59 of the last
trading day BEFORE the period begins: the agent's frozen research baseline then
holds everything published through that prior day's close but nothing from the
period's first day, whose intraday/pre-open data rolls in only later as the replay
sim-clock crosses each row's available_at (the per-tick Timeview). The intraday
auction decision ticks (09:15/09:25/14:57) are a separate replay concern defined in
environment_design.md, not this schedule.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path

import pandas as pd

from autotrade.environment.data.contracts import CN_TZ

QUARTER_PATTERN = re.compile(r"^(\d{4})Q([1-4])$")
# Research-snapshot anchor: end of the prior trading day (close of business),
# not an intraday moment. The decision-input view is frozen as of this time.
RESEARCH_ANCHOR_TIME = time(23, 59, 59)
DEFAULT_WINDOW_MONTHS = 21
PERIOD_UNITS = ("week", "month", "quarter", "year")
# The replay engine reserves the final trade date of a region for forced
# liquidation, so every validation/test/held-out region needs at least two
# trading days to be backtestable at all. Guarded here at schedule build time
# so a fold can never reach the (expensive) sandbox + LLM session doomed.
MIN_REGION_TRADE_DAYS = 2


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


def period_range(first: str, last: str, *, period: str = "quarter") -> list[str]:
    period = _normalize_period(period)
    first_start, _ = period_bounds(first, period=period)
    last_start, _ = period_bounds(last, period=period)
    labels: list[str] = []
    current = pd.Timestamp(first_start)
    last_ts = pd.Timestamp(last_start)
    while current <= last_ts:
        labels.append(_period_label(current, period))
        current = _advance_period(current, period, 1)
        if len(labels) > 5000:
            raise ValueError(f"period range too large or inverted: {first}..{last} ({period})")
    if not labels:
        raise ValueError(f"period range is inverted: {first}..{last} ({period})")
    return labels


def period_bounds(label: str, *, period: str = "quarter") -> tuple[str, str]:
    period = _normalize_period(period)
    if ".." in str(label):
        start, end = [yyyymmdd(part) for part in str(label).split("..", maxsplit=1)]
        if end < start:
            raise ValueError(f"period range end precedes start: {label!r}")
        return start, end
    if period == "quarter":
        return quarter_bounds(str(label))
    start = _period_start(str(label), period)
    end = _advance_period(start, period, 1) - pd.Timedelta(days=1)
    return start.strftime("%Y%m%d"), end.strftime("%Y%m%d")


def previous_period(label: str, *, period: str = "quarter") -> str:
    period = _normalize_period(period)
    start, _ = period_bounds(label, period=period)
    previous = _advance_period(pd.Timestamp(start), period, -1)
    return _period_label(previous, period)


def first_trading_day(start: str, end: str, trading_days: list[str]) -> str:
    for day in trading_days:
        if start <= day <= end:
            return day
    raise ValueError(f"no trading day inside {start}..{end}")


def build_fold_schedule(
    first_test_period: str,
    last_test_period: str,
    trading_days: list[str],
    *,
    window_months: int = DEFAULT_WINDOW_MONTHS,
    period: str = "quarter",
) -> list[FoldSpec]:
    folds: list[FoldSpec] = []
    period = _normalize_period(period)
    test_labels = period_range(first_test_period, last_test_period, period=period)
    for test_label in test_labels:
        validation_label = previous_period(test_label, period=period)
        validation_start, validation_end = period_bounds(validation_label, period=period)
        test_start, test_end = period_bounds(test_label, period=period)
        _require_min_trade_days(f"fold_{test_label} validation", validation_start, validation_end, trading_days)
        _require_min_trade_days(f"fold_{test_label} test", test_start, test_end, trading_days)
        window_start = pd.Timestamp(validation_start) - pd.DateOffset(months=window_months)
        window_end = pd.Timestamp(validation_start) - pd.Timedelta(days=1)
        folds.append(
            FoldSpec(
                fold_id=f"fold_{test_label}",
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


def heldout_periods(
    first_period: str,
    last_period: str,
    trading_days: list[str],
    *,
    period: str = "quarter",
) -> list[dict[str, object]]:
    """Held-out replay periods at the configured cadence."""
    periods = []
    period = _normalize_period(period)
    for label in period_range(first_period, last_period, period=period):
        start, end = period_bounds(label, period=period)
        _require_min_trade_days(f"held-out {label}", start, end, trading_days)
        periods.append(
            {
                "label": label,
                "start": start,
                "end": end,
                "decision_time": _decision_time(start, end, trading_days),
            }
        )
    return periods


def assert_no_overlap(development_last_test_period: str, heldout_first_period: str, *, period: str = "quarter") -> None:
    """Held-out must be configured upfront and not overlap development."""
    dev_end = period_bounds(development_last_test_period, period=period)[1]
    heldout_start = period_bounds(heldout_first_period, period=period)[0]
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


def _require_min_trade_days(region: str, start: str, end: str, trading_days: list[str]) -> None:
    count = sum(1 for day in trading_days if start <= day <= end)
    if count < MIN_REGION_TRADE_DAYS:
        raise ValueError(
            f"{region} region {start}..{end} has {count} trading day(s); replay needs at least "
            f"{MIN_REGION_TRADE_DAYS} (final day is reserved for forced liquidation)"
        )


def _decision_time(start: str, end: str, trading_days: list[str]) -> datetime:
    """Research-snapshot anchor: 23:59:59 of the trading day before the period.

    Freezing the decision-input snapshot at the close of the prior trading day keeps
    the agent's research baseline strictly pre-period; the period's own data becomes
    visible only as the replay sim-clock crosses each row's available_at.
    """
    first_day = first_trading_day(start, end, trading_days)
    anchor_day = _prior_trading_day(first_day, trading_days)
    return datetime.strptime(anchor_day, "%Y%m%d").replace(
        hour=RESEARCH_ANCHOR_TIME.hour,
        minute=RESEARCH_ANCHOR_TIME.minute,
        second=RESEARCH_ANCHOR_TIME.second,
        tzinfo=CN_TZ,
    )


def _prior_trading_day(day: str, trading_days: list[str]) -> str:
    earlier = [d for d in trading_days if d < day]
    if not earlier:
        raise ValueError(f"no trading day before {day}; cannot anchor the research snapshot")
    return max(earlier)


def _normalize_period(period: str) -> str:
    value = str(period or "quarter").lower().strip()
    aliases = {"weekly": "week", "monthly": "month", "quarterly": "quarter", "yearly": "year", "annual": "year"}
    value = aliases.get(value, value)
    if value not in PERIOD_UNITS:
        raise ValueError(f"unsupported fold period: {period!r}; expected one of {PERIOD_UNITS}")
    return value


def _period_start(label: str, period: str) -> pd.Timestamp:
    text = str(label).strip()
    if period == "year" and re.fullmatch(r"\d{4}", text):
        return pd.Timestamp(year=int(text), month=1, day=1)
    if period == "month" and re.fullmatch(r"\d{6}", text):
        return pd.Timestamp(year=int(text[:4]), month=int(text[4:6]), day=1)
    return pd.Timestamp(yyyymmdd(text))


def _advance_period(value: pd.Timestamp, period: str, step: int) -> pd.Timestamp:
    if period == "week":
        return value + pd.Timedelta(weeks=step)
    if period == "month":
        return value + pd.DateOffset(months=step)
    if period == "quarter":
        return value + pd.DateOffset(months=3 * step)
    if period == "year":
        return value + pd.DateOffset(years=step)
    raise ValueError(f"unsupported fold period: {period}")


def _period_label(start: pd.Timestamp, period: str) -> str:
    if period == "month":
        return start.strftime("%Y%m")
    if period == "quarter":
        quarter = (start.month - 1) // 3 + 1
        return f"{start.year}Q{quarter}"
    if period == "year":
        return start.strftime("%Y")
    return start.strftime("%Y%m%d")


def yyyymmdd(value: str) -> str:
    text = str(value).strip()
    parsed = pd.Timestamp(text)
    return parsed.strftime("%Y%m%d")
