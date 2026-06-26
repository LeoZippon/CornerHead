"""Per-minute position management, called by ``main(ctx)`` every minute.

``manage_positions(ctx)`` iterates the current holdings and applies exit / 做T
(intraday swing) logic by driving ``ctx.broker`` primitives keyed by ``ts_code``.
These are ordinary Agent-owned functions — edit, delete, or rename them freely;
the Environment knows none of these names. Functions are pure trading logic with
no network access or unbounded loops. The Broker enforces cash, short margin,
T+1 sellable balance, lot size, price limits, suspension, and shortability, and
reserves the final replay day for mandatory liquidation.
"""

from __future__ import annotations


def manage_positions(ctx) -> None:
    """Template: apply a simple 做T rule to each holding (edit to taste)."""
    for pos in ctx.positions:
        example_swing_t(ctx, str(pos.get("ts_code", "")))


def example_swing_t(ctx, ts_code: str) -> None:
    """Intraday swing (做T) around the entry price for one holding.

    Buys ``100`` shares on a dip and sells ``100`` of the *sellable* (T+1
    eligible) balance on a rally, both before the close. ``ctx.params`` may carry
    a ``percent`` band.
    """
    price = ctx.price(ts_code)
    if price is None or ctx.cur_time >= "14:57":
        return
    pos = next((p for p in ctx.positions if str(p.get("ts_code")) == ts_code), None)
    if pos is None:
        return
    band = float(ctx.params.get("percent", 0.05))
    entry = float(pos.get("entry_price") or price)
    sellable = int(pos.get("sellable_quantity", 0) or 0)
    if price <= entry * (1 - band) and ctx.broker.money >= price * 100:
        ctx.broker.buy(ts_code, amount=100, reason="swing_dip")
    elif sellable >= 100 and price >= entry * (1 + band):
        ctx.broker.sell(ts_code, amount=100, reason="swing_rally")
