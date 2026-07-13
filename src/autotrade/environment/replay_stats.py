"""Replay result container and the return-statistics reducer.

``ReplayResult`` is what one full replay of ``main(ctx)`` produces;
``compute_return_stats`` reduces it to the ``detailed_return.json`` payload
(docs/environment_design.md §3.7). Pure result→dict math — no replay logic.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd

from autotrade.environment.broker import SimBroker

TRADING_DAYS_PER_YEAR = 252


@dataclass
class ReplayResult:
    equity_curve: pd.Series
    broker: SimBroker
    decision_date: str
    exit_date: str
    granularity: str = "minute"
    # Cost feedback: per-sub-step wall-time aggregates, total replay wall-clock, and
    # the number of trade days replayed (so the Agent can extrapolate a full run from
    # a small replay_window test pass).
    substep_runtime: dict[str, dict[str, float]] | None = None
    replay_wall_seconds: float | None = None
    replayed_trade_days: int | None = None
    # 24h tick-grid breakdown: total main(ctx) ticks and how many were intraday
    # (matchable session/auction bars) vs off-session (research/state only), so the
    # Agent can see the extra cost the off-session grid adds.
    total_ticks: int | None = None
    intraday_ticks: int | None = None
    offsession_ticks: int | None = None
    decision_calls: int | None = None
    strategy_action_count: int | None = None
    # Managed ctx.state_dir staging ledger: one record per sub-step-staged write with
    # its ready_at and merge status (some may stay unmerged past the region end).
    state_staging_audit: list[dict[str, object]] | None = None
    # Per-phase replay wall-time (strategy_compute / nl_service / timeview_init / timeview_roll /
    # state_merge / broker_match), so the 24h replay's added cost is auditable.
    phase_seconds: dict[str, float] | None = None
    # Peak RSS reported by the one-shot formal Agent process. Informational only;
    # it is never an acceptance or modification-check input.
    agent_peak_rss_bytes: int | None = None


def compute_return_stats(result: ReplayResult) -> dict[str, object]:
    """The minimum return statistics from docs/environment_design.md §3.7."""
    broker = result.broker
    curve = result.equity_curve
    initial = broker.initial_equity
    total_return = curve.iloc[-1] / initial - 1.0 if len(curve) else 0.0
    # Day-0 baseline: daily returns and drawdown are measured against the initial
    # equity, so the first day's return (initial -> day-1 close) and a peak below
    # the initial level are never dropped. The persisted equity_curve and the
    # trade-day count stay end-of-day-based; style_analysis.daily_returns_from_curve
    # seeds the same baseline for attribution.
    baselined = pd.concat(
        [pd.Series([float(initial)]), pd.Series(curve.to_numpy(dtype=float))],
        ignore_index=True,
    )
    daily_returns = baselined.pct_change().dropna()
    sharpe = 0.0
    if len(daily_returns) > 1 and daily_returns.std(ddof=1) > 0:
        sharpe = float(daily_returns.mean() / daily_returns.std(ddof=1) * math.sqrt(TRADING_DAYS_PER_YEAR))
    peak = baselined.cummax()
    max_drawdown = float(((peak - baselined) / peak).max()) if len(curve) else 0.0
    years = max(len(curve), 1) / TRADING_DAYS_PER_YEAR
    annualized = float((1.0 + total_return) ** (1.0 / years) - 1.0) if total_return > -1.0 else -1.0
    realized = [event for event in broker.events if event["event_type"] in {"position_closed", "position_reduced"}]
    full_closes = [event for event in broker.events if event["event_type"] == "position_closed"]
    # Ex-date cash dividends (credited to longs, debited from shorts as lender
    # compensation) belong to side attribution but are not trades: they enter
    # long/short P&L, never trade_count or win_rate.
    dividends = [event for event in broker.events if event["event_type"] == "dividend_cash"]
    long_pnl = sum(e["realized_pnl"] for e in realized if e["side"] == "long") + sum(
        e["amount"] for e in dividends if e["side"] == "long"
    )
    short_pnl = sum(e["realized_pnl"] for e in realized if e["side"] == "short") + sum(
        e["amount"] for e in dividends if e["side"] == "short"
    )
    wins = sum(1 for e in realized if e["realized_pnl"] > 0)
    orders = broker.get_trade_detail_data(account_type="STOCK", data_type="ORDER") + broker.get_trade_detail_data(
        account_type="CREDIT", data_type="ORDER"
    )
    per_stock = [
        {
            "ts_code": event["ts_code"],
            "side": event["side"],
            "exit_date": event["trade_date"],
            "exit_price": event["price"],
            "exit_price_label": event.get("price_label"),
            "quantity": event.get("quantity"),
            "realized_pnl": event["realized_pnl"],
            "kind": event["event_type"],
            "forced": event.get("forced", False),
        }
        for event in realized
    ]
    status_counts: dict[str, int] = {}
    for order in orders:
        status_counts[str(order["status"])] = status_counts.get(str(order["status"]), 0) + 1
    # Exit-date liquidation evidence: the mandatory exit leaves unsellable
    # inventory (suspension, limit lock, T+1, missing price) in the book. Final
    # equity already marks it to market; the leftovers are reported explicitly so
    # an incomplete liquidation is never mistaken for a clean close-out.
    exit_blocked = {
        (str(event.get("ts_code")), str(event.get("side"))): str(event["event_type"]).removeprefix("exit_blocked_")
        for event in broker.events
        if str(event["event_type"]).startswith("exit_blocked_")
        and str(event.get("trade_date")) == str(result.exit_date)
    }
    unliquidated = [
        {
            "account": state.name,
            "ts_code": pos.ts_code,
            "side": pos.side,
            "quantity": pos.quantity,
            "last_price": pos.last_price,
            "market_value": pos.market_value,
            "blocked_reason": exit_blocked.get((pos.ts_code, pos.side)),
        }
        for state in broker.accounts.values()
        for pos in state.positions.values()
    ]
    margin_secs_reject_count = sum(
        broker.reject_counts.get(reason, 0)
        for reason in (
            "margin_secs_not_collateral",
            "margin_secs_not_finable",
            "margin_secs_not_shortable",
            "margin_secs_data_missing",
        )
    )

    return {
        "initial_cash": initial,
        "final_equity": float(curve.iloc[-1]) if len(curve) else initial,
        "total_return": float(total_return),
        "long_return": float(long_pnl / initial),
        "short_return": float(short_pnl / initial),
        "annualized_return": annualized,
        "sharpe": sharpe,
        "max_drawdown": max_drawdown,
        "win_rate": float(wins / len(realized)) if realized else 0.0,
        "full_close_count": len(full_closes),
        "trade_count": len(realized),
        "turnover": float(broker.traded_notional / initial) if initial else 0.0,
        "order_count": len(orders),
        "order_status_counts": status_counts,
        "reject_counts": dict(broker.reject_counts),
        "margin_secs_reject_count": margin_secs_reject_count,
        "broker_inventory_reject_count": broker.reject_counts.get("broker_inventory_unavailable", 0),
        "max_holdings_reject_count": broker.reject_counts.get("max_holdings_reached", 0),
        "fees_paid": float(broker.fees_paid),
        "stamp_duty_paid": float(broker.stamp_duty_paid),
        "slippage_bps_assumed": broker.profile.slippage_bps,
        "credit_interest_accrued": float(broker.interest_accrued_total),
        "credit_interest_paid": float(broker.interest_paid_total),
        "dividend_cash_received": float(broker.dividend_cash_received),
        "dividend_compensation_paid": float(broker.dividend_compensation_paid),
        "forced_close_events": sum(1 for e in broker.events if e["event_type"] == "forced_close_triggered"),
        # Positions the HOST liquidated at region end (mandatory exit): a high
        # count means the strategy never sold on its own — it measured
        # "buy once, hold, host closes", not a sustainable rebalancing policy.
        "host_exit_liquidation_count": sum(1 for e in broker.events if e["event_type"] == "exit_liquidated_by_host"),
        "liquidation_complete": not unliquidated,
        "unliquidated_positions": unliquidated,
        "remaining_liabilities": float(broker.outstanding_liabilities()),
        "replay_granularity": result.granularity,
        "replay_wall_seconds": result.replay_wall_seconds,
        "replayed_trade_days": result.replayed_trade_days,
        "substep_runtime": result.substep_runtime or {},
        "phase_seconds": result.phase_seconds or {},
        "agent_peak_rss_bytes": result.agent_peak_rss_bytes,
        "total_ticks": result.total_ticks,
        "intraday_ticks": result.intraday_ticks,
        "offsession_ticks": result.offsession_ticks,
        "decision_calls": result.decision_calls,
        "strategy_action_count": result.strategy_action_count,
        "equity_curve": {str(k): float(v) for k, v in curve.items()},
        "decision_date": result.decision_date,
        "exit_date": result.exit_date,
        "per_stock": per_stock,
        "broker_events": broker.events,
    }
