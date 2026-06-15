"""Simulated Broker and the default CITIC replay/Broker profile.

Implements docs/environment_design.md chapter 5: structured orders only, every
reject/fill is recorded, short selling follows the configured inventory mode
(default ``proxy_margin_secs``), and profile values are written to the run
manifest by the caller.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from hl_trader.environment.runtime import new_id

LOT_SIZE = 100
SHORT_INVENTORY_MODES = ("proxy_margin_secs", "broker_inventory", "theoretical_short")


STAMP_DUTY_CUTOVER = "20230828"  # sell-side stamp duty halved to 0.05% from this date


@dataclass(frozen=True)
class BrokerProfile:
    """Default CITIC replay/Broker profile (docs/environment_design.md 5.3).

    Maintenance lines follow the published CITIC base case (T-day 16:00
    维持担保比例 > 200%): closeout 130%, safety 140%, withdrawal 300%.
    Concentration-dependent variants below 200% are not modeled.
    """

    initial_cash: float = 1_000_000.0
    commission_bps: float = 1.0
    min_commission_cny: float = 5.0
    stamp_duty_sell_bps_before_cutover: float = 10.0
    stamp_duty_sell_bps_from_cutover: float = 5.0
    slippage_bps: float = 5.0
    long_score_threshold: float = 0.7
    short_score_threshold: float = -0.7
    max_total_holdings: int = 10
    short_inventory_mode: str = "proxy_margin_secs"
    short_margin_ratio: float = 1.0
    short_margin_ratio_private_fund: float = 1.2
    is_private_fund: bool = False
    # Annualized assumed borrow fee; flagged as a research assumption in the
    # profile record until per-security broker fee files are wired in.
    short_borrow_fee_annual: float = 0.085
    # Dividends/rights against short positions are intentionally not modeled yet.
    short_corporate_actions: str = "disabled"
    maintenance_closeout_ratio: float = 1.30
    maintenance_warning_ratio: float = 1.40
    maintenance_withdraw_ratio: float = 3.00
    max_single_name_weight: float = 0.20
    profile_id: str = "citic_default_v2"
    source: str = "docs/environment_design.md#53-回放配置"
    maintenance_source: str = "https://pb.citics.com/trading/xxgs/wcdbbl/"

    def __post_init__(self) -> None:
        if self.short_inventory_mode not in SHORT_INVENTORY_MODES:
            raise ValueError(f"unsupported short_inventory_mode={self.short_inventory_mode}")
        if self.long_score_threshold <= 0 or self.short_score_threshold >= 0:
            raise ValueError("long threshold must be positive and short threshold negative")
        if self.max_total_holdings <= 0:
            raise ValueError("max_total_holdings must be positive")

    @property
    def effective_short_margin_ratio(self) -> float:
        return self.short_margin_ratio_private_fund if self.is_private_fund else self.short_margin_ratio

    def commission(self, notional: float) -> float:
        return max(notional * self.commission_bps / 10_000.0, self.min_commission_cny)

    def stamp_duty_on_sale(self, notional: float, trade_date: str) -> float:
        bps = (
            self.stamp_duty_sell_bps_from_cutover
            if str(trade_date) >= STAMP_DUTY_CUTOVER
            else self.stamp_duty_sell_bps_before_cutover
        )
        return notional * bps / 10_000.0

    def slipped_price(self, price: float, *, is_buy: bool) -> float:
        slip = self.slippage_bps / 10_000.0
        return price * (1.0 + slip) if is_buy else price * (1.0 - slip)

    def to_record(self) -> dict[str, object]:
        return {
            "profile_id": self.profile_id,
            "source": self.source,
            "maintenance_source": self.maintenance_source,
            "initial_cash": self.initial_cash,
            "commission_bps": self.commission_bps,
            "min_commission_cny": self.min_commission_cny,
            "stamp_duty_sell_bps_before_cutover": self.stamp_duty_sell_bps_before_cutover,
            "stamp_duty_sell_bps_from_cutover": self.stamp_duty_sell_bps_from_cutover,
            "stamp_duty_cutover_date": STAMP_DUTY_CUTOVER,
            "slippage_bps": self.slippage_bps,
            "long_score_threshold": self.long_score_threshold,
            "short_score_threshold": self.short_score_threshold,
            "max_total_holdings": self.max_total_holdings,
            "short_inventory_mode": self.short_inventory_mode,
            "short_margin_ratio": self.effective_short_margin_ratio,
            "short_borrow_fee_annual": self.short_borrow_fee_annual,
            "short_borrow_fee_is_assumed": True,
            "short_corporate_actions": self.short_corporate_actions,
            "maintenance_closeout_ratio": self.maintenance_closeout_ratio,
            "maintenance_warning_ratio": self.maintenance_warning_ratio,
            "maintenance_withdraw_ratio": self.maintenance_withdraw_ratio,
            "max_single_name_weight": self.max_single_name_weight,
        }


class MarketData:
    """Daily replay bars indexed by (trade_date, ts_code).

    Expects normalized snapshot units (CNY prices) with columns: open, close,
    up_limit, down_limit, is_suspended.
    """

    REQUIRED = ("trade_date", "ts_code", "open", "close")

    def __init__(self, daily: pd.DataFrame) -> None:
        missing = [col for col in self.REQUIRED if col not in daily.columns]
        if missing:
            raise ValueError(f"replay daily data missing columns: {missing}")
        frame = daily.copy()
        frame["trade_date"] = frame["trade_date"].astype(str)
        frame["ts_code"] = frame["ts_code"].astype(str)
        self._bars = frame.set_index(["trade_date", "ts_code"]).sort_index()
        self.trade_dates = sorted(frame["trade_date"].unique())

    def bar(self, trade_date: str, ts_code: str) -> pd.Series | None:
        try:
            row = self._bars.loc[(str(trade_date), str(ts_code))]
        except KeyError:
            return None
        return row.iloc[0] if isinstance(row, pd.DataFrame) else row

    @staticmethod
    def is_suspended(bar: pd.Series | None) -> bool:
        return bar is None or bool(bar.get("is_suspended", False))

    @staticmethod
    def limit_up_blocked(bar: pd.Series) -> bool:
        limit = bar.get("up_limit")
        return pd.notna(limit) and float(bar["open"]) >= float(limit)

    @staticmethod
    def limit_down_blocked(bar: pd.Series) -> bool:
        limit = bar.get("down_limit")
        return pd.notna(limit) and float(bar["open"]) <= float(limit)


@dataclass
class Order:
    ts_code: str
    side: str  # "long" | "short"
    order_type: str
    target_weight: float
    reason: str
    source_artifacts: list[str]
    limit_price: float | None = None
    order_id: str = field(default_factory=lambda: new_id("ord"))
    status: str = "submitted"
    reject_reason: str | None = None
    submitted_at: str = ""
    fillable_from: str = ""

    def to_record(self) -> dict[str, object]:
        return {
            "order_id": self.order_id,
            "ts_code": self.ts_code,
            "side": self.side,
            "order_type": self.order_type,
            "target_weight": self.target_weight,
            "limit_price": self.limit_price,
            "status": self.status,
            "reject_reason": self.reject_reason,
            "submitted_at": self.submitted_at,
            "fillable_from": self.fillable_from,
            "reason": self.reason,
            "source_artifacts": list(self.source_artifacts),
        }


@dataclass
class Position:
    ts_code: str
    side: str
    quantity: int
    entry_price: float
    entry_date: str
    entry_cost: float
    last_price: float

    @property
    def market_value(self) -> float:
        return self.quantity * self.last_price

    @property
    def short_liability(self) -> float:
        return self.market_value if self.side == "short" else 0.0


class SimBroker:
    """Order/fill/position accounting for daily fixed-holding replay.

    The Broker only accepts structured orders; the Agent or frozen strategy
    never writes fills, positions, or returns directly.
    """

    def __init__(
        self,
        profile: BrokerProfile,
        market: MarketData,
        *,
        shortable_codes: frozenset[str],
        initial_cash: float | None = None,
    ) -> None:
        self.profile = profile
        self.market = market
        self.shortable_codes = shortable_codes
        self.cash = float(initial_cash if initial_cash is not None else profile.initial_cash)
        self.initial_equity = self.cash
        self.positions: dict[str, Position] = {}
        self.orders: list[Order] = []
        self.events: list[dict[str, object]] = []
        self.fees_paid = 0.0
        self.stamp_duty_paid = 0.0
        self.borrow_fees = 0.0
        self.reject_counts: dict[str, int] = {}

    # ---- broker interface (docs/environment_design.md 5.1) ----

    def get_account(self) -> dict[str, object]:
        return {
            "cash": self.cash,
            "total_assets": self.equity(),
            "available_cash": self.cash - self._short_margin_occupied(),
            "short_margin_occupied": self._short_margin_occupied(),
            "maintenance_ratio": self.maintenance_ratio(),
            "risk_limits": {
                "max_total_holdings": self.profile.max_total_holdings,
                "max_single_name_weight": self.profile.max_single_name_weight,
                "maintenance_closeout_ratio": self.profile.maintenance_closeout_ratio,
            },
        }

    def get_positions(self) -> list[dict[str, object]]:
        return [
            {
                "ts_code": pos.ts_code,
                "side": pos.side,
                "quantity": pos.quantity,
                "sellable_quantity": pos.quantity,
                "entry_price": pos.entry_price,
                "entry_date": pos.entry_date,
                "last_price": pos.last_price,
                "market_value": pos.market_value,
            }
            for pos in self.positions.values()
        ]

    def submit_order(self, order: Order, *, decision_time: str, fill_date: str) -> Order:
        order.submitted_at = decision_time
        order.fillable_from = fill_date
        self.orders.append(order)
        reject = self._eligibility_reject(order)
        if reject is not None:
            self._reject(order, reject)
        else:
            order.status = "accepted"
        return order

    def cancel_order(self, order_id: str) -> Order:
        order = self._find_order(order_id)
        if order.status not in {"submitted", "accepted"}:
            raise ValueError(f"order {order_id} cannot be cancelled from status {order.status}")
        order.status = "cancelled"
        self._event("order_cancelled", order_id=order_id, ts_code=order.ts_code)
        return order

    def query_orders(self) -> list[dict[str, object]]:
        return [order.to_record() for order in self.orders]

    # ---- replay mechanics ----

    def fill_open(self, trade_date: str) -> None:
        """Fill accepted orders at the day's open with A-share constraints."""
        for order in self.orders:
            if order.status != "accepted" or order.fillable_from != trade_date:
                continue
            bar = self.market.bar(trade_date, order.ts_code)
            if MarketData.is_suspended(bar):
                self._reject(order, "suspended_on_fill_date")
                continue
            if order.side == "long" and MarketData.limit_up_blocked(bar):
                self._reject(order, "limit_up_open_blocked_buy")
                continue
            if order.side == "short" and MarketData.limit_down_blocked(bar):
                self._reject(order, "limit_down_open_blocked_short")
                continue
            is_buy = order.side == "long"
            price = self.profile.slipped_price(float(bar["open"]), is_buy=is_buy)
            target_notional = abs(order.target_weight) * self.initial_equity
            quantity = int(target_notional / price / LOT_SIZE) * LOT_SIZE
            if quantity <= 0:
                self._reject(order, "target_notional_below_lot_size")
                continue
            notional = quantity * price
            fee = self.profile.commission(notional)
            if is_buy:
                if notional + fee > self.cash:
                    self._reject(order, "insufficient_cash")
                    continue
                self.cash -= notional + fee
            else:
                # Short opening is a sale: stamp duty applies to the seller.
                duty = self.profile.stamp_duty_on_sale(notional, trade_date)
                margin = notional * self.profile.effective_short_margin_ratio
                if margin + fee + duty > self.cash - self._short_margin_occupied():
                    self._reject(order, "insufficient_short_margin")
                    continue
                self.cash += notional - fee - duty
                self.stamp_duty_paid += duty
            self.fees_paid += fee
            order.status = "filled"
            self.positions[order.ts_code] = Position(
                ts_code=order.ts_code,
                side=order.side,
                quantity=quantity,
                entry_price=price,
                entry_date=trade_date,
                entry_cost=notional + fee if order.side == "long" else notional,
                last_price=price,
            )
            self._event("order_filled", order_id=order.order_id, ts_code=order.ts_code, price=price, quantity=quantity)

    def mark_to_market(self, trade_date: str) -> float:
        for pos in self.positions.values():
            bar = self.market.bar(trade_date, pos.ts_code)
            if bar is not None and pd.notna(bar.get("close")):
                pos.last_price = float(bar["close"])
            if pos.side == "short":
                daily_fee = pos.quantity * pos.entry_price * self.profile.short_borrow_fee_annual / 365.0
                self.cash -= daily_fee
                self.borrow_fees += daily_fee
        ratio = self.maintenance_ratio()
        if ratio is not None and ratio < self.profile.maintenance_closeout_ratio:
            self._event("forced_close_triggered", trade_date=trade_date, maintenance_ratio=ratio)
            self.close_all(trade_date, forced=True)
        return self.equity()

    def close_all(self, trade_date: str, *, forced: bool = False) -> None:
        """Close longs and buy-to-cover shorts at the day's close.

        A+1 rule: positions opened the same day cannot be closed (T+1).
        """
        for ts_code in list(self.positions):
            pos = self.positions[ts_code]
            if trade_date <= pos.entry_date:
                raise ValueError(f"T+1 violation: cannot close {ts_code} on entry date {pos.entry_date}")
            bar = self.market.bar(trade_date, ts_code)
            if MarketData.is_suspended(bar):
                self._event("exit_blocked_suspended", ts_code=ts_code, trade_date=trade_date, forced=forced)
                bar = None
            is_buy = pos.side == "short"  # closing a short buys to cover
            raw_price = float(bar["close"]) if bar is not None else pos.last_price
            price = self.profile.slipped_price(raw_price, is_buy=is_buy)
            notional = pos.quantity * price
            fee = self.profile.commission(notional)
            duty = 0.0
            if pos.side == "long":
                duty = self.profile.stamp_duty_on_sale(notional, trade_date)
                self.cash += notional - fee - duty
                self.stamp_duty_paid += duty
            else:
                self.cash -= notional + fee
            self.fees_paid += fee
            pos.last_price = price
            realized = (notional - pos.entry_cost) if pos.side == "long" else (pos.entry_cost - notional)
            self._event(
                "position_closed",
                ts_code=ts_code,
                trade_date=trade_date,
                side=pos.side,
                price=price,
                realized_pnl=realized - fee - duty,
                forced=forced,
            )
            del self.positions[ts_code]

    def equity(self) -> float:
        long_value = sum(pos.market_value for pos in self.positions.values() if pos.side == "long")
        short_liability = sum(pos.short_liability for pos in self.positions.values())
        return self.cash + long_value - short_liability

    def maintenance_ratio(self) -> float | None:
        short_liability = sum(pos.short_liability for pos in self.positions.values())
        if short_liability <= 0:
            return None
        long_value = sum(pos.market_value for pos in self.positions.values() if pos.side == "long")
        return (self.cash + long_value) / short_liability

    # ---- internals ----

    def _eligibility_reject(self, order: Order) -> str | None:
        if order.side not in {"long", "short"}:
            return f"unsupported_side:{order.side}"
        if order.side == "long":
            return None
        mode = self.profile.short_inventory_mode
        if mode == "proxy_margin_secs":
            if order.ts_code not in self.shortable_codes:
                return "margin_secs_not_shortable"
            return None
        if mode == "broker_inventory":
            # Real CITIC inventory/fee files are not wired yet; without them the
            # mode must reject rather than silently assume borrow availability.
            return "broker_inventory_unavailable"
        return None  # theoretical_short: explicit research mode, no inventory gate

    def _reject(self, order: Order, reason: str) -> None:
        order.status = "rejected"
        order.reject_reason = reason
        self.reject_counts[reason] = self.reject_counts.get(reason, 0) + 1
        self._event("order_rejected", order_id=order.order_id, ts_code=order.ts_code, reason=reason)

    def _short_margin_occupied(self) -> float:
        return sum(
            pos.quantity * pos.entry_price * self.profile.effective_short_margin_ratio
            for pos in self.positions.values()
            if pos.side == "short"
        )

    def _find_order(self, order_id: str) -> Order:
        for order in self.orders:
            if order.order_id == order_id:
                return order
        raise KeyError(f"unknown order_id: {order_id}")

    def _event(self, event_type: str, **payload: object) -> None:
        self.events.append({"event_type": event_type, **payload})


def load_shortable_codes(snapshot_dir: str | Path, decision_date: str) -> frozenset[str]:
    """Decision-date margin_secs membership from the events domain (proxy mode)."""
    events_path = Path(snapshot_dir) / "events.parquet"
    if not events_path.exists():
        return frozenset()
    events = pd.read_parquet(events_path)
    if events.empty or "dataset" not in events.columns:
        return frozenset()
    rows = events[(events["dataset"] == "margin_secs")]
    if "trade_date" in rows.columns:
        rows = rows[rows["trade_date"].astype(str) == str(decision_date)]
    return frozenset(rows["ts_code"].astype(str)) if "ts_code" in rows.columns else frozenset()
