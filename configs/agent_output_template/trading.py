"""Optional advanced helper — the minimal default ``main.py`` does not import this.

Per-tick position management, for a ``main(ctx)`` that opts into the cadence.
Call these helpers from inside a light ``ctx.substep``; broker actions are rejected
outside substeps so runtime and submit latency are accounted uniformly.

``manage_positions(ctx)`` first cancels stale working orders, then iterates the
current holdings and applies exit / 做T (intraday swing) logic by driving
``ctx.broker`` primitives keyed by ``ts_code``.
These are ordinary Agent-owned functions — edit, delete, or rename them freely;
the Environment knows none of these names. Functions are pure trading logic with
no network access or unbounded loops. The Broker enforces cash, short margin,
T+1 sellable balance, lot size, price limits, suspension, and shortability, and
reserves the final replay day for mandatory liquidation.

Orders fill a few bars later (``execution_lag_bars``), and ``ctx.positions``
reflects FILLED positions only, so a just-submitted exit is not visible there until
it fills. Gate management on ``ctx.broker.pending(ts_code)`` (the working-order
query) — and/or band/threshold guards — so a rule does not re-fire the same order
on consecutive ticks before it fills. Use ``ctx.broker.pending()`` with no code to
scan all working orders; returned records include ``order_id`` and ``age_minutes``
for cancellation.
"""

from __future__ import annotations


def manage_positions(ctx) -> None:
    """Template: apply a simple 做T rule to each holding (edit to taste)."""
    cancel_stale_pending(ctx, max_age_minutes=1.0)
    for pos in ctx.positions:
        example_swing_t(ctx, str(pos.get("ts_code", "")))


def cancel_stale_pending(ctx, *, max_age_minutes: float = 1.0) -> None:
    """Every tick, cancel pending orders older than ``max_age_minutes``.

    This is intentionally light per-tick bookkeeping, so call it from a small-budget
    management substep. The Broker remains the source of truth: only cancel orders visible through
    ``pending()``, and let filled positions be reconciled through ``ctx.positions``.
    """
    for order in ctx.broker.pending():
        order_id = order.get("order_id")
        age = float(order.get("age_minutes") or 0.0)
        if order_id and age > max_age_minutes:
            ctx.broker.cancel(order_id, reason="stale_pending_gt_1m")


def example_swing_t(ctx, ts_code: str) -> None:
    """Intraday swing (做T) around the entry price for one holding.

    Buys ``100`` shares on a dip and sells ``100`` of the *sellable* (T+1
    eligible) balance on a rally, both before the close. ``ctx.params`` may carry
    a ``percent`` band.
    """
    price = ctx.price(ts_code)
    if price is None or ctx.cur_time >= "14:57":
        return
    if ctx.broker.pending(ts_code):
        return  # an order is already working for this code; don't re-fire before it fills
    pos = next((p for p in ctx.positions if str(p.get("ts_code")) == ts_code), None)
    if pos is None:
        return
    band = float(ctx.params.get("percent", 0.05))
    entry = float(pos.get("entry_price") or price)
    sellable = int(pos.get("sellable_quantity", 0) or 0)
    if price <= entry * (1 - band) and ctx.broker.cash >= price * 100:
        ctx.broker.buy(ts_code, amount=100, reason="swing_dip")
    elif sellable >= 100 and price >= entry * (1 + band):
        ctx.broker.sell(ts_code, amount=100, reason="swing_rally")
