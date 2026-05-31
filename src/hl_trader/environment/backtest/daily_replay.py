from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Callable, Iterable

from hl_trader.environment.execution import BrokerSimulator, Fill, Order, PortfolioState
from hl_trader.environment.storage import TrialLedger


@dataclass(frozen=True)
class ReplayEvent:
    trade_date: date
    event_type: str
    details: dict


DecisionFn = Callable[[date, PortfolioState], Iterable[Order]]
PriceFn = Callable[[date, str], float | None]
ConstraintFn = Callable[[date, str], dict]


class DailyReplayEngine:
    def __init__(self, broker: BrokerSimulator, ledger: TrialLedger | None = None) -> None:
        self.broker = broker
        self.ledger = ledger

    def run(
        self,
        *,
        trade_dates: Iterable[date],
        initial_state: PortfolioState,
        decide: DecisionFn,
        price_for: PriceFn,
        constraints_for: ConstraintFn | None = None,
    ) -> tuple[PortfolioState, list[Fill]]:
        dates = _strictly_increasing_dates(trade_dates)
        state = initial_state
        fills: list[Fill] = []
        for trade_date in dates:
            self.broker.settle_t_plus_1(state)
            orders = list(decide(trade_date, state))
            for order in orders:
                if order.trade_date != trade_date:
                    raise ValueError("order.trade_date must match the current replay trade_date")
                price = price_for(trade_date, order.ts_code)
                if price is None:
                    continue
                constraints = constraints_for(trade_date, order.ts_code) if constraints_for else {}
                fill = self.broker.execute_order(state, order, price, **constraints)
                if fill:
                    fills.append(fill)
                    if self.ledger:
                        self.ledger.append({"event_type": "fill", "fill": fill})
            if self.ledger:
                marks = _marks_for_positions(state, trade_date, price_for)
                self.ledger.append({
                    "event_type": "daily_close",
                    "trade_date": trade_date,
                    "cash": state.cash,
                    "gross_exposure": state.gross_exposure(marks),
                    "equity": state.equity(marks),
                    "positions": len(state.positions),
                })
        return state, fills


def _strictly_increasing_dates(trade_dates: Iterable[date]) -> list[date]:
    dates = list(trade_dates)
    for previous, current in zip(dates, dates[1:]):
        if current <= previous:
            raise ValueError("trade_dates must be strictly increasing")
    return dates


def _marks_for_positions(state: PortfolioState, trade_date: date, price_for: PriceFn) -> dict[str, float]:
    marks: dict[str, float] = {}
    for ts_code in state.positions:
        price = price_for(trade_date, ts_code)
        if price is not None:
            marks[ts_code] = price
    return marks
