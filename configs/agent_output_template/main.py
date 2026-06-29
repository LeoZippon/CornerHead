"""Formal strategy entrypoint — minimal working default.

The Environment calls ``main(ctx)`` once per replay tick (a market-level ``ctx``).
This default is intentionally small but complete: while flat it buys an
equal-weight basket from the visible cross-section (screened once per trading day)
and holds it to the mandatory final-day liquidation. Replace the placeholder
screen in ``_screen`` with your own signal.

Key ``ctx`` surface (advanced helpers in ``candidate.py`` / ``trading.py`` + ``README.md``):
  ``ctx.positions`` / ``ctx.account`` / ``ctx.cash``   current holdings and account state
  ``ctx.price(code)`` / ``ctx.bar(code)``              this tick's price/bar (None pre-auction)
  ``ctx.broker.buy/sell/short/cover/close(code, weight=...|amount=...)``  place orders
  ``ctx.broker.pending(code)``                         still-working orders for a code
  ``ctx.asof_dir``                                     rolling daily history for screening (rolls each day)
  ``ctx.snapshot_dir`` / ``ctx.model_dir``             frozen decision snapshot / model artifacts
  ``ctx.substep(name, budget_minutes=B)``              declare a heavy block's latency budget
  ``ctx.nl(code, prompt=...)``                         optional LLM text read

Orders fill a LATER bar (``execution_lag_bars`` ahead), never within the decision
bar, so re-submitting before a fill double-buys — ``ctx.broker.pending(code)``
guards against that until the order lands. ``ctx.asof_dir`` is a per-day rolling
view, so read it ONCE per day (cache it, as below) instead of on every tick; the
full frozen cross-section lives under ``ctx.snapshot_dir``. None of the latency /
``nl()`` helpers are required — this file uses only the core primitives.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

TOP_N = 10

# ``ctx.asof_dir`` rolls to a new directory each trading day, so caching the screen
# by that path reads daily.parquet once per day rather than once per per-tick call.
# The driver imports this module once and calls ``main`` every tick, so module-level
# state persists across the replay (and resets between backtests).
_SCREEN_CACHE: dict[str, list[str]] = {}


def _screen(ctx) -> list[str]:
    """The day's target basket. Cached per ``ctx.asof_dir`` to avoid re-reading the
    full table on every tick. Replace the body with your own cross-sectional signal."""
    asof_dir = str(ctx.asof_dir)
    cached = _SCREEN_CACHE.get(asof_dir)
    if cached is not None:
        return cached
    daily = pd.read_parquet(Path(asof_dir) / "daily.parquet")
    codes = sorted(daily["ts_code"].astype(str).unique())[:TOP_N]
    _SCREEN_CACHE[asof_dir] = codes
    return codes


def main(ctx) -> None:
    if ctx.positions:  # already holding the basket — hold to final-day liquidation
        return
    for code in _screen(ctx):
        # Skip a code with a price unavailable this tick or an order still in flight
        # (the fill lands a later bar, so re-submitting before then would double-buy).
        if ctx.price(code) is not None and not ctx.broker.pending(code):
            ctx.broker.buy(code, weight=1.0 / TOP_N, reason="equal_weight_basket")
