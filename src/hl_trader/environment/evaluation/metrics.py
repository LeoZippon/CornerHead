from __future__ import annotations

import math

import pandas as pd


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
