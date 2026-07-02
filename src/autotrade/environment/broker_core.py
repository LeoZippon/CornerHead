"""Dependency-light deterministic fill-projection core (docs/environment_design.md 7).

Pure stdlib (no ``autotrade``/``pandas`` import), but host-only: this module is NOT
shipped into the Agent sandbox image. Its single consumer is the authoritative host
:class:`~autotrade.environment.broker.SimBroker`, which projects every order's
money/share outcome from the functions here (commission, stamp duty, slippage, lot
sizing, the open/reduce cash deltas, and the short margin / locked-proceeds
buying-power model). Only this deterministic math lives here; bar-level gates
(suspension, price limits, shortable inventory, T+1 sellable) and position
bookkeeping stay with the broker, which holds the market data and position state.

The sandbox driver (``main_ctx_driver.py`` — the only module baked into the image) is
deliberately stdlib-only and does NO intra-tick fill projection: within a tick the
agent sees the cash/positions filled as of the ENTERING tick, and the host settles
the resulting orders on the next tick. There is no in-sandbox broker view projecting
from this core, so nothing here needs to stay in sync with sandbox-visible state.
"""

from __future__ import annotations

from dataclasses import dataclass

LOT_SIZE = 100
STAMP_DUTY_CUTOVER = "20230828"  # sell-side stamp duty halved to 0.05% from this date


@dataclass(frozen=True)
class CostModel:
    """The deterministic cost parameters that turn a raw price + share count into a
    fill price, commission, stamp duty, and short margin (a flattened view of the
    profile's economics, with ``short_margin_ratio`` already resolved)."""

    commission_bps: float = 1.0
    min_commission_cny: float = 5.0
    stamp_duty_sell_bps_before_cutover: float = 10.0
    stamp_duty_sell_bps_from_cutover: float = 5.0
    slippage_bps: float = 5.0
    short_margin_ratio: float = 1.0

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


def lot_floor(amount: object) -> int:
    """Round a desired share count down to a whole board lot."""
    try:
        shares = int(float(amount))
    except (TypeError, ValueError):
        return 0
    return (shares // LOT_SIZE) * LOT_SIZE


def resolve_shares(amount: object, weight: object, raw_price: object, initial_equity: float) -> int:
    """Share count from an explicit ``amount`` or, failing that, a ``weight`` fraction
    of ``initial_equity`` at ``raw_price`` — lot-aligned. 0 when neither is usable."""
    if amount is not None and str(amount).strip() != "":
        return lot_floor(amount)
    if weight is not None and str(weight).strip() != "" and raw_price not in (None, "") and float(raw_price) > 0:
        return lot_floor(abs(float(weight)) * initial_equity / float(raw_price))
    return 0


@dataclass(frozen=True)
class OpenFill:
    """Deterministic projection of opening a position (``buy`` long / ``short``)."""

    side: str  # "long" | "short"
    price: float
    shares: int
    fee: float
    duty: float
    margin: float
    notional: float
    # Signed change applied to literal cash on fill.
    cash_delta: float
    # Deployable cash that must be available to accept the order.
    required_cash: float
    # entry cost basis recorded on the position: notional+fee for a long buy; for a
    # short, the net proceeds banked and locked as collateral (== entry_cost).
    cost_basis: float


def project_open(
    cost: CostModel,
    *,
    side: str,
    raw_price: float,
    shares: int,
    trade_date: str,
    apply_slippage: bool = True,
) -> OpenFill:
    """Project a long buy or short open at ``raw_price`` for ``shares`` lots.

    A long buy may only deploy available cash (``required_cash = notional + fee``). A
    short banks its net proceeds into cash but locks them plus margin, so its buying
    power requirement is ``margin + fee + duty`` and its locked collateral is the net
    proceeds. ``apply_slippage`` is True for marketable fills, False for limit/auction
    fills (where ``raw_price`` is already the fill price)."""
    is_buy = side == "long"
    price = cost.slipped_price(raw_price, is_buy=is_buy) if apply_slippage else float(raw_price)
    notional = shares * price
    fee = cost.commission(notional)
    if side == "long":
        return OpenFill(
            side="long", price=price, shares=shares, fee=fee, duty=0.0, margin=0.0,
            notional=notional, cash_delta=-(notional + fee), required_cash=notional + fee,
            cost_basis=notional + fee,
        )
    duty = cost.stamp_duty_on_sale(notional, trade_date)
    margin = notional * cost.short_margin_ratio
    proceeds = notional - fee - duty
    return OpenFill(
        side="short", price=price, shares=shares, fee=fee, duty=duty, margin=margin,
        notional=notional, cash_delta=proceeds, required_cash=margin + fee + duty,
        cost_basis=proceeds,
    )


@dataclass(frozen=True)
class ReduceFill:
    """Deterministic projection of reducing a position (``sell`` long / ``cover`` short)."""

    side: str  # the position side being reduced
    price: float
    shares: int
    fee: float
    duty: float
    notional: float
    # Signed change applied to literal cash on fill.
    cash_delta: float


def project_reduce(
    cost: CostModel,
    *,
    side: str,
    raw_price: float,
    shares: int,
    trade_date: str,
    apply_slippage: bool = True,
) -> ReduceFill:
    """Project reducing a ``side`` position by ``shares`` lots at ``raw_price``.

    Selling a long banks ``notional - fee - duty``; covering a short pays
    ``notional + fee`` (a buy, so slippage is on the buy side)."""
    is_buy = side == "short"  # covering a short is a buy
    price = cost.slipped_price(raw_price, is_buy=is_buy) if apply_slippage else float(raw_price)
    notional = shares * price
    fee = cost.commission(notional)
    if side == "long":
        duty = cost.stamp_duty_on_sale(notional, trade_date)
        cash_delta = notional - fee - duty
    else:
        duty = 0.0
        cash_delta = -(notional + fee)
    return ReduceFill(side=side, price=price, shares=shares, fee=fee, duty=duty, notional=notional, cash_delta=cash_delta)
