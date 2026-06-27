"""Simulated Broker and the default CITIC replay/Broker profile.

The Broker exposes only fundamental, strategy-agnostic primitives
(``buy``/``sell``/``short``/``cover``/``close`` by share amount, plus account,
position, and per-stock trade-history queries). It owns no trading-strategy
logic; trading strategies live in the Agent's ``output`` and drive these
primitives during minute-by-minute replay.

The Broker still enforces every A-share market rule (docs/environment_design.md
chapter 7): cash, short margin, T+1 sellable balance, lot size, limit
up/down, suspension, the configured short-inventory mode (default
``proxy_margin_secs``), optional run-config concentration limits, commission,
stamp duty, slippage, borrow fee, and forced close. Every order/reject is
recorded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from autotrade.environment.runtime import new_id

LOT_SIZE = 100
SHORT_INVENTORY_MODES = ("proxy_margin_secs", "broker_inventory", "theoretical_short")


STAMP_DUTY_CUTOVER = "20230828"  # sell-side stamp duty halved to 0.05% from this date


@dataclass(frozen=True)
class BrokerProfile:
    """Default CITIC replay/Broker profile (docs/environment_design.md 7.3).

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
    max_total_holdings: int | None = None
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
    max_single_name_weight: float | None = None
    profile_id: str = "citic_default_v3"
    source: str = "docs/environment_design.md#73-回放profile"
    maintenance_source: str = "https://pb.citics.com/trading/xxgs/wcdbbl/"

    def __post_init__(self) -> None:
        if self.short_inventory_mode not in SHORT_INVENTORY_MODES:
            raise ValueError(f"unsupported short_inventory_mode={self.short_inventory_mode}")
        if self.max_total_holdings is not None and self.max_total_holdings <= 0:
            raise ValueError("max_total_holdings must be positive")
        if self.max_single_name_weight is not None and self.max_single_name_weight <= 0:
            raise ValueError("max_single_name_weight must be positive")

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
    def limit_up_blocked_at_price(bar: pd.Series, price: object) -> bool:
        limit = bar.get("up_limit")
        return pd.notna(limit) and pd.notna(price) and float(price) >= float(limit)

    @staticmethod
    def limit_down_blocked_at_price(bar: pd.Series, price: object) -> bool:
        limit = bar.get("down_limit")
        return pd.notna(limit) and pd.notna(price) and float(price) <= float(limit)


@dataclass
class Order:
    """Audited record of a single broker primitive call (filled or rejected)."""

    ts_code: str
    action: str  # "buy" | "sell" | "short" | "cover" | "close"
    side: str  # "long" | "short"
    requested_amount: int
    trade_date: str
    decision_time: str = ""
    price: float | None = None
    filled_quantity: int = 0
    status: str = "submitted"
    reject_reason: str | None = None
    reason: str = ""
    source_artifacts: list[str] = field(default_factory=list)
    price_label: str = "price"
    order_id: str = field(default_factory=lambda: new_id("ord"))

    def to_record(self) -> dict[str, object]:
        return {
            "order_id": self.order_id,
            "ts_code": self.ts_code,
            "action": self.action,
            "side": self.side,
            "requested_amount": self.requested_amount,
            "filled_quantity": self.filled_quantity,
            "price": self.price,
            "price_label": self.price_label,
            "status": self.status,
            "reject_reason": self.reject_reason,
            "decision_time": self.decision_time,
            "trade_date": self.trade_date,
            "reason": self.reason,
            "source_artifacts": list(self.source_artifacts),
        }


@dataclass
class Position:
    """An aggregate long or short position with average cost and T+1 lock.

    ``locked_today`` is the share count acquired on ``locked_date`` that the
    T+1 rule forbids selling/covering until a later trade date.
    """

    ts_code: str
    side: str
    quantity: int
    entry_price: float
    entry_date: str
    entry_cost: float
    last_price: float
    locked_today: int = 0
    locked_date: str = ""

    @property
    def market_value(self) -> float:
        return self.quantity * self.last_price

    @property
    def short_liability(self) -> float:
        return self.market_value if self.side == "short" else 0.0

    @property
    def sellable_quantity(self) -> int:
        """Shares that may be sold (long) or covered (short) right now."""
        return max(self.quantity - self.locked_today, 0)


class SimBroker:
    """Order/fill/position accounting driven only by structured primitives.

    The Agent strategy never writes fills, positions, or returns; it calls
    ``buy``/``sell``/``short``/``cover``/``close`` (by share amount) and the
    Broker applies every market constraint and records the outcome.
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
        self.trade_ledger: dict[str, list[dict[str, object]]] = {}
        self.fees_paid = 0.0
        self.stamp_duty_paid = 0.0
        self.borrow_fees = 0.0
        self.traded_notional = 0.0
        self.reject_counts: dict[str, int] = {}
        self.current_date = ""

    # ---- broker queries (docs/environment_design.md 7.1) ----

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
                "sellable_quantity": pos.sellable_quantity,
                "entry_price": pos.entry_price,
                "entry_date": pos.entry_date,
                "last_price": pos.last_price,
                "market_value": pos.market_value,
            }
            for pos in self.positions.values()
        ]

    def query_orders(self) -> list[dict[str, object]]:
        return [order.to_record() for order in self.orders]

    def trades_for(self, ts_code: str) -> list[dict[str, object]]:
        """Per-code executed-trade history from the broker ledger."""
        return list(self.trade_ledger.get(str(ts_code), []))

    def position_quantity(self, ts_code: str) -> int:
        """Signed share count: long positive, short negative, flat zero."""
        pos = self.positions.get(str(ts_code))
        if pos is None:
            return 0
        return pos.quantity if pos.side == "long" else -pos.quantity

    def record_event(self, event_type: str, **payload: object) -> None:
        """Append an audited replay event from trusted Environment code."""
        self._event(event_type, **payload)

    # ---- fundamental primitives ----

    def execute(
        self,
        ts_code: str,
        action: str,
        *,
        trade_date: str,
        raw_price: float | None,
        amount: int | None = None,
        weight: float | None = None,
        time: str = "",
        reason: str = "",
        source_artifacts: list[str] | None = None,
        price_label: str = "price",
    ) -> Order:
        """Apply one strategy primitive at the current bar with full constraints.

        ``action`` is ``buy``/``sell``/``short``/``cover``/``close``. ``amount``
        is a share count (lot-aligned); ``weight`` is a notional-fraction
        convenience used when ``amount`` is absent.
        """
        self._advance_date(trade_date)
        action = str(action).lower().strip()
        side = "short" if action in {"short", "cover"} else "long"
        order = Order(
            ts_code=str(ts_code),
            action=action,
            side=side,
            requested_amount=int(amount) if amount is not None else 0,
            trade_date=str(trade_date),
            decision_time=str(time),
            reason=str(reason or action),
            source_artifacts=list(source_artifacts or []),
            price_label=price_label,
        )
        self.orders.append(order)

        bar = self.market.bar(trade_date, ts_code)
        if MarketData.is_suspended(bar):
            return self._reject(order, "suspended")
        if raw_price is None or pd.isna(raw_price):
            return self._reject(order, "missing_price")
        raw_price = float(raw_price)

        if action in {"buy", "short"}:
            return self._open(order, bar, raw_price, amount=amount, weight=weight)
        if action in {"sell", "cover", "close"}:
            return self._reduce(order, bar, raw_price, amount=amount)
        return self._reject(order, f"unsupported_action:{action}")

    def _open(self, order: Order, bar: pd.Series, raw_price: float, *, amount, weight) -> Order:
        shares = self._resolve_amount(amount, weight, raw_price)
        if shares <= 0:
            return self._reject(order, "amount_below_lot_size")
        pos = self.positions.get(order.ts_code)
        if pos is not None and pos.side != order.side:
            return self._reject(order, "opposite_side_position_open")
        if (
            self.profile.max_total_holdings is not None
            and pos is None
            and len(self.positions) >= self.profile.max_total_holdings
        ):
            return self._reject(order, "max_holdings_reached")
        shares = self._cap_single_name(pos, shares, raw_price)
        if shares <= 0:
            return self._reject(order, "single_name_weight_cap")
        if order.action == "short":
            inventory_reject = self._short_inventory_reject(order.ts_code)
            if inventory_reject is not None:
                return self._reject(order, inventory_reject)
            if MarketData.limit_down_blocked_at_price(bar, raw_price):
                return self._reject(order, "limit_down_blocked_short")
            return self._fill_short_open(order, raw_price, shares)
        if MarketData.limit_up_blocked_at_price(bar, raw_price):
            return self._reject(order, "limit_up_blocked_buy")
        return self._fill_long_open(order, raw_price, shares)

    def _fill_long_open(self, order: Order, raw_price: float, shares: int) -> Order:
        price = self.profile.slipped_price(raw_price, is_buy=True)
        notional = shares * price
        fee = self.profile.commission(notional)
        if notional + fee > self.cash + 1e-6:
            return self._reject(order, "insufficient_cash")
        self.cash -= notional + fee
        self.fees_paid += fee
        self._add_to_position(order.ts_code, "long", shares, price, notional + fee, order.trade_date)
        return self._fill(order, price, shares, "open")

    def _fill_short_open(self, order: Order, raw_price: float, shares: int) -> Order:
        price = self.profile.slipped_price(raw_price, is_buy=False)
        notional = shares * price
        fee = self.profile.commission(notional)
        duty = self.profile.stamp_duty_on_sale(notional, order.trade_date)
        margin = notional * self.profile.effective_short_margin_ratio
        if margin + fee + duty > self.cash - self._short_margin_occupied() + 1e-6:
            return self._reject(order, "insufficient_short_margin")
        self.cash += notional - fee - duty
        self.fees_paid += fee
        self.stamp_duty_paid += duty
        # entry_cost for a short is the net sale proceeds released proportionally on cover.
        self._add_to_position(order.ts_code, "short", shares, price, notional - fee - duty, order.trade_date)
        return self._fill(order, price, shares, "open")

    def _reduce(self, order: Order, bar: pd.Series, raw_price: float, *, amount) -> Order:
        pos = self.positions.get(order.ts_code)
        want_side = "short" if order.action == "cover" else ("long" if order.action == "sell" else None)
        if pos is None:
            return self._reject(order, "no_position")
        if want_side is not None and pos.side != want_side:
            return self._reject(order, f"side_mismatch:{order.action}:{pos.side}")
        order.side = pos.side
        sellable = pos.sellable_quantity
        if sellable <= 0:
            return self._reject(order, "t_plus_one_no_sellable")
        if order.action == "close" or amount is None:
            shares = sellable
        else:
            shares = min(self._lot_floor(amount), sellable)
        if shares <= 0:
            return self._reject(order, "amount_below_lot_size")
        is_buy = pos.side == "short"  # covering a short is a buy
        if pos.side == "long" and MarketData.limit_down_blocked_at_price(bar, raw_price):
            return self._reject(order, "limit_down_blocked_sell")
        if pos.side == "short" and MarketData.limit_up_blocked_at_price(bar, raw_price):
            return self._reject(order, "limit_up_blocked_cover")
        price = self.profile.slipped_price(raw_price, is_buy=is_buy)
        self._reduce_position(pos, shares, price, order.trade_date)
        return self._fill(order, price, shares, "close" if order.ts_code not in self.positions else "reduce")

    # ---- replay lifecycle ----

    def mark_to_market(self, trade_date: str) -> float:
        self._advance_date(trade_date)
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
        """Liquidate all sellable shares at the day's close (mandatory exit)."""
        for ts_code in list(self.positions):
            self.close_position(ts_code, trade_date, forced=forced)

    def close_position(self, ts_code: str, trade_date: str, *, forced: bool = False) -> bool:
        """Close one position's sellable shares at the day's close price."""
        self._advance_date(trade_date)
        pos = self.positions.get(ts_code)
        if pos is None:
            return False
        sellable = pos.sellable_quantity
        if sellable <= 0:
            self._event("exit_blocked_t_plus_one", ts_code=ts_code, side=pos.side, trade_date=trade_date, forced=forced)
            return False
        bar = self.market.bar(trade_date, ts_code)
        if MarketData.is_suspended(bar):
            self._event("exit_blocked_suspended", ts_code=ts_code, side=pos.side, trade_date=trade_date, forced=forced)
            return False
        raw_price = bar.get("close")
        if pd.isna(raw_price):
            self._event("exit_blocked_missing_price", ts_code=ts_code, side=pos.side, trade_date=trade_date, forced=forced)
            return False
        raw_price = float(raw_price)
        if pos.side == "long" and MarketData.limit_down_blocked_at_price(bar, raw_price):
            self._event("exit_blocked_limit_down", ts_code=ts_code, side=pos.side, trade_date=trade_date, forced=forced)
            return False
        if pos.side == "short" and MarketData.limit_up_blocked_at_price(bar, raw_price):
            self._event("exit_blocked_limit_up", ts_code=ts_code, side=pos.side, trade_date=trade_date, forced=forced)
            return False
        price = self.profile.slipped_price(raw_price, is_buy=pos.side == "short")
        self._reduce_position(pos, sellable, price, trade_date, forced=forced, price_label="close")
        return True

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

    def _advance_date(self, trade_date: str) -> None:
        trade_date = str(trade_date)
        for pos in self.positions.values():
            if pos.locked_date and trade_date > pos.locked_date:
                pos.locked_today = 0
                pos.locked_date = trade_date
        if trade_date > self.current_date:
            self.current_date = trade_date

    def _add_to_position(
        self,
        ts_code: str,
        side: str,
        shares: int,
        price: float,
        cash_basis: float,
        trade_date: str,
    ) -> Position:
        self.traded_notional += shares * price
        pos = self.positions.get(ts_code)
        if pos is None:
            pos = Position(
                ts_code=ts_code,
                side=side,
                quantity=shares,
                entry_price=price,
                entry_date=trade_date,
                entry_cost=cash_basis,
                last_price=price,
                locked_today=shares,
                locked_date=trade_date,
            )
            self.positions[ts_code] = pos
            return pos
        pos.entry_price = (pos.entry_price * pos.quantity + price * shares) / (pos.quantity + shares)
        pos.quantity += shares
        pos.entry_cost += cash_basis
        pos.locked_today += shares
        pos.locked_date = trade_date
        pos.last_price = price
        return pos

    def _reduce_position(
        self,
        pos: Position,
        shares: int,
        price: float,
        trade_date: str,
        *,
        forced: bool = False,
        price_label: str = "price",
    ) -> None:
        shares = min(shares, pos.sellable_quantity)
        if shares <= 0:
            return
        self.traded_notional += shares * price
        notional = shares * price
        fee = self.profile.commission(notional)
        basis_released = pos.entry_cost * shares / pos.quantity
        if pos.side == "long":
            duty = self.profile.stamp_duty_on_sale(notional, trade_date)
            self.cash += notional - fee - duty
            self.stamp_duty_paid += duty
            realized = (notional - fee - duty) - basis_released
        else:
            # covering a short: pay to buy back; basis_released is the net proceeds banked at open
            duty = 0.0
            self.cash -= notional + fee
            realized = basis_released - (notional + fee)
        self.fees_paid += fee
        pos.quantity -= shares
        pos.entry_cost -= basis_released
        pos.last_price = price
        pos.locked_today = min(pos.locked_today, pos.quantity)
        self._ledger(
            pos.ts_code,
            side=pos.side,
            kind="reduce" if pos.quantity > 0 else "close",
            price=price,
            quantity=shares,
            trade_date=trade_date,
            realized_pnl=realized,
        )
        if pos.quantity <= 0:
            self._event(
                "position_closed",
                ts_code=pos.ts_code,
                trade_date=trade_date,
                side=pos.side,
                price=price,
                quantity=shares,
                realized_pnl=realized,
                forced=forced,
                price_label=price_label,
            )
            del self.positions[pos.ts_code]
        else:
            self._event(
                "position_reduced",
                ts_code=pos.ts_code,
                trade_date=trade_date,
                side=pos.side,
                price=price,
                quantity=shares,
                realized_pnl=realized,
                forced=forced,
                price_label=price_label,
            )

    def _fill(self, order: Order, price: float, shares: int, kind: str) -> Order:
        order.status = "filled"
        order.price = price
        order.filled_quantity = shares
        if kind == "open":
            self._ledger(order.ts_code, side=order.side, kind="open", price=price, quantity=shares, trade_date=order.trade_date)
        self._event(
            "order_filled",
            order_id=order.order_id,
            ts_code=order.ts_code,
            action=order.action,
            side=order.side,
            price=price,
            quantity=shares,
            price_label=order.price_label,
        )
        return order

    def _reject(self, order: Order, reason: str) -> Order:
        order.status = "rejected"
        order.reject_reason = reason
        self.reject_counts[reason] = self.reject_counts.get(reason, 0) + 1
        self._event("order_rejected", order_id=order.order_id, ts_code=order.ts_code, action=order.action, reason=reason)
        return order

    def _short_inventory_reject(self, ts_code: str) -> str | None:
        mode = self.profile.short_inventory_mode
        if mode == "proxy_margin_secs":
            return None if ts_code in self.shortable_codes else "margin_secs_not_shortable"
        if mode == "broker_inventory":
            # Real CITIC inventory/fee files are not wired yet; without them the
            # mode must reject rather than silently assume borrow availability.
            return "broker_inventory_unavailable"
        return None  # theoretical_short: explicit research mode, no inventory gate

    def _cap_single_name(self, pos: Position | None, shares: int, raw_price: float) -> int:
        """Clamp an opening order so the position's notional stays within the
        single-name weight cap (rounded down to whole lots)."""
        if self.profile.max_single_name_weight is None:
            return shares
        if raw_price <= 0:
            return 0
        cap_notional = self.profile.max_single_name_weight * self.initial_equity
        held_notional = (pos.quantity * raw_price) if pos is not None else 0.0
        budget_shares = self._lot_floor((cap_notional - held_notional) / raw_price)
        return min(shares, budget_shares)

    def _resolve_amount(self, amount: int | None, weight: float | None, raw_price: float) -> int:
        if amount is not None and str(amount).strip() != "":
            return self._lot_floor(amount)
        if weight is not None and str(weight).strip() != "" and raw_price > 0:
            return self._lot_floor(abs(float(weight)) * self.initial_equity / raw_price)
        return 0

    @staticmethod
    def _lot_floor(amount: object) -> int:
        try:
            shares = int(float(amount))
        except (TypeError, ValueError):
            return 0
        return (shares // LOT_SIZE) * LOT_SIZE

    def _short_margin_occupied(self) -> float:
        return sum(
            pos.quantity * pos.entry_price * self.profile.effective_short_margin_ratio
            for pos in self.positions.values()
            if pos.side == "short"
        )

    def _ledger(self, ts_code: str, *, side: str, kind: str, price: float, quantity: int, trade_date: str, realized_pnl: float | None = None) -> None:
        record = {
            "ts_code": ts_code,
            "side": side,
            "kind": kind,
            "price": float(price),
            "quantity": int(quantity),
            "amount": int(quantity),
            "notional": float(price) * int(quantity),
            "trade_date": str(trade_date),
            "date": str(trade_date),
        }
        if realized_pnl is not None:
            record["realized_pnl"] = float(realized_pnl)
        self.trade_ledger.setdefault(str(ts_code), []).append(record)

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
