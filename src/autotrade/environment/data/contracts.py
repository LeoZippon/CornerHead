from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

CN_TZ = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class DatasetContract:
    dataset: str
    partition_key: str
    available_time: time
    lag_days: int = 0
    tradable_lag_days: int = 1
    unit_rules: dict[str, str] | None = None
    pit_notes: str = ""

    def available_at(self, partition_date: date) -> datetime:
        return datetime.combine(partition_date + timedelta(days=self.lag_days), self.available_time, tzinfo=CN_TZ)

    def tradable_from(self, partition_date: date) -> date:
        return partition_date + timedelta(days=self.tradable_lag_days)


def default_tushare_contracts() -> dict[str, DatasetContract]:
    return {
        "daily": DatasetContract(
            dataset="daily",
            partition_key="trade_date",
            available_time=time(17, 30),
            tradable_lag_days=1,
            unit_rules={"vol": "hands", "amount": "thousand_cny"},
            pit_notes="Use for close-to-close research or next-trade-date decisions, not same-day 09:25 decisions.",
        ),
        "daily_basic": DatasetContract(
            dataset="daily_basic",
            partition_key="trade_date",
            available_time=time(18, 0),
            tradable_lag_days=1,
            unit_rules={"total_share": "ten_thousand_shares", "total_mv": "ten_thousand_cny"},
            pit_notes="Valuation and share fields are available after market close; use next trade date for decisions.",
        ),
        "adj_factor": DatasetContract(
            dataset="adj_factor",
            partition_key="trade_date",
            available_time=time(9, 30),
            tradable_lag_days=0,
            unit_rules={"adj_factor": "ratio"},
            pit_notes="Raw trade_date alone is not enough for intraday PIT; conservative daily replay should use prior close factors.",
        ),
        "stk_limit": DatasetContract(
            dataset="stk_limit",
            partition_key="trade_date",
            available_time=time(8, 45),
            tradable_lag_days=0,
            unit_rules={"up_limit": "cny_per_share", "down_limit": "cny_per_share"},
            pit_notes="Can be used before the trading session if the source timestamp is trusted.",
        ),
        "suspend_d": DatasetContract(
            dataset="suspend_d",
            partition_key="trade_date",
            available_time=time(8, 45),
            tradable_lag_days=0,
            pit_notes="Use as a trading constraint; zero rows mean no suspended names for that partition.",
        ),
        "limit_list_d": DatasetContract(
            dataset="limit_list_d",
            partition_key="trade_date",
            available_time=time(17, 30),
            tradable_lag_days=1,
            pit_notes="Event table starts in 2020 locally; use as next-day event evidence unless source timing is proven earlier.",
        ),
    }


def sim_datetime(trade_date: str, minute_key: str) -> datetime:
    """Beijing-time simulation clock for one replay tick.

    ``trade_date`` is ``YYYYMMDD`` and ``minute_key`` is ``HH:MM`` (24h). Every
    replay tick -- intraday bar, pre-open/close auction, and off-session -- binds to
    this clock. It is the single basis for off-session grid spacing, auction/fill
    mapping, ``available_at`` visibility in the Timeview, staged-write ``ready_at``,
    and the daily post-close refresh. The live loop reuses ``main(ctx)`` against the
    real Asia/Shanghai system clock, so the semantics carry over unchanged.
    """
    hour_text, _, minute_text = str(minute_key).partition(":")
    return datetime.strptime(str(trade_date), "%Y%m%d").replace(
        hour=int(hour_text or 0), minute=int(minute_text or 0), tzinfo=CN_TZ
    )
