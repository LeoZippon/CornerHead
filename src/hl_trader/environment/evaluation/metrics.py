from __future__ import annotations

from dataclasses import dataclass
import math

import pandas as pd


@dataclass(frozen=True)
class ShortSaleAssumptions:
    cash_collateral_pct: float = 1.0
    annual_borrow_fee_bps: float = 1800.0
    days_per_year: int = 365

    def __post_init__(self) -> None:
        if self.cash_collateral_pct <= 0:
            raise ValueError("cash_collateral_pct must be positive")
        if self.annual_borrow_fee_bps < 0:
            raise ValueError("annual_borrow_fee_bps cannot be negative")
        if self.days_per_year <= 0:
            raise ValueError("days_per_year must be positive")


@dataclass(frozen=True)
class LongShortReturnBreakdown:
    long_return: float
    short_return: float
    combined_return: float
    long_capital: float
    short_cash_collateral: float


def annualized_return(equity_curve: pd.Series, periods_per_year: int = 252) -> float:
    values = equity_curve.dropna()
    if len(values) < 2 or values.iloc[0] <= 0:
        return 0.0
    total_return = values.iloc[-1] / values.iloc[0] - 1.0
    years = (len(values) - 1) / periods_per_year
    return (1.0 + total_return) ** (1.0 / years) - 1.0 if years > 0 else 0.0


def max_drawdown(equity_curve: pd.Series) -> float:
    values = equity_curve.dropna()
    if values.empty:
        return 0.0
    running_max = values.cummax()
    drawdowns = values / running_max - 1.0
    return float(drawdowns.min())


def sharpe_ratio(returns: pd.Series, periods_per_year: int = 252) -> float:
    values = returns.dropna()
    if len(values) < 2:
        return 0.0
    std = float(values.std(ddof=1))
    if std == 0.0 or math.isnan(std):
        return 0.0
    return float(values.mean()) / std * math.sqrt(periods_per_year)


def theoretical_short_return(
    entry_price: float,
    exit_price: float,
    holding_days: int,
    assumptions: ShortSaleAssumptions | None = None,
) -> float:
    """Return theoretical short PnL per cash collateral unit.

    The default is a 100% cash collateral study sleeve with 18% annual borrow
    fee. It is not an executable broker model and does not assert borrow
    inventory availability.
    """
    if entry_price <= 0 or exit_price <= 0:
        raise ValueError("entry_price and exit_price must be positive")
    if holding_days < 0:
        raise ValueError("holding_days cannot be negative")
    values = assumptions or ShortSaleAssumptions()
    gross_notional_return = (entry_price - exit_price) / entry_price
    fee_drag = (values.annual_borrow_fee_bps / 10_000.0) * (holding_days / values.days_per_year)
    return (gross_notional_return - fee_drag) / values.cash_collateral_pct


def long_short_return_breakdown(
    long_return: float,
    short_return: float,
    *,
    long_capital: float = 1.0,
    short_cash_collateral: float = 0.0,
) -> LongShortReturnBreakdown:
    if long_capital < 0 or short_cash_collateral < 0:
        raise ValueError("capital values cannot be negative")
    total_capital = long_capital + short_cash_collateral
    if total_capital <= 0:
        raise ValueError("at least one capital sleeve must be positive")
    combined = (long_return * long_capital + short_return * short_cash_collateral) / total_capital
    return LongShortReturnBreakdown(
        long_return=float(long_return),
        short_return=float(short_return),
        combined_return=float(combined),
        long_capital=float(long_capital),
        short_cash_collateral=float(short_cash_collateral),
    )
