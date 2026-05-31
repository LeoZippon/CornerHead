from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
import calendar


@dataclass(frozen=True)
class Fold:
    fold_id: str
    train_start: date
    train_end: date
    test_start: date
    test_end: date

    def contains_test_date(self, value: date) -> bool:
        return self.test_start <= value <= self.test_end


def month_add(value: date, months: int) -> date:
    year = value.year + (value.month - 1 + months) // 12
    month = (value.month - 1 + months) % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def day_before(value: date) -> date:
    return value - timedelta(days=1)


def generate_rolling_folds(
    *,
    start_date: date,
    end_date: date,
    train_length_months: int,
    test_length_months: int,
    step_months: int,
) -> list[Fold]:
    if start_date >= end_date:
        raise ValueError("start_date must be before end_date")
    if min(train_length_months, test_length_months, step_months) <= 0:
        raise ValueError("fold month lengths must be positive")
    folds: list[Fold] = []
    train_start = start_date
    index = 1
    while True:
        train_end = day_before(month_add(train_start, train_length_months))
        test_start = train_end + timedelta(days=1)
        test_end = day_before(month_add(test_start, test_length_months))
        if test_start > end_date:
            break
        if test_end > end_date:
            test_end = end_date
        folds.append(Fold(f"fold_{index:03d}", train_start, train_end, test_start, test_end))
        if test_end >= end_date:
            break
        train_start = month_add(train_start, step_months)
        index += 1
    return folds
