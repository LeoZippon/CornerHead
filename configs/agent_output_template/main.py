"""Formal strategy entrypoint — minimal working default.

The Environment calls ``main(ctx)`` once per replay tick (a market-level ``ctx``).
This default is intentionally small but complete: while flat it buys an
equal-weight basket from the visible cross-section (screened once per as-of
version) and holds it to the mandatory final-day liquidation. Replace the
placeholder screen in ``_screen`` with your own signal.

Key ``ctx`` surface (advanced helpers in ``candidate.py`` / ``trading.py`` + ``README.md``):
  ``ctx.account`` / ``ctx.positions``                 account snapshot / per-symbol holdings (cash: ``ctx.broker.cash``)
  ``ctx.price(code)`` / ``ctx.bar(code)``              this tick's price/bar (None pre-auction)
  ``ctx.broker.buy/sell/short/cover/close(code, weight=...|amount=...)``  place orders; returns ``order_id``
  ``ctx.broker.pending(code=None)`` / ``ctx.broker.cancel(order_id)``      query/cancel working orders
  ``ctx.asof_dir`` / ``ctx.asof_version``              rolling point-in-time data view + its version
  ``ctx.snapshot_dir`` / ``ctx.model_dir``             frozen research snapshot / model artifacts
  ``ctx.state_dir``                                    managed cross-tick state (only available inside substep)
  ``ctx.substep(name, budget_minutes=B)``              required wrapper for broker/state actions; B<1 submits this tick, B>=1 after ready_at
  ``ctx.nl(code?, prompt=...)``                        optional PIT text analysis

``ctx.asof_dir`` holds one directory per data domain (``daily``, ``events``, ``macro``,
``fundamentals``, ``intraday_1min``), each read with ``pd.read_parquet(ctx.asof_dir / "daily")``.
The view rolls forward as each dataset's real refresh job completes, so during a
trading session it is frozen and ``ctx.asof_version`` is stable — cache a read by
that version and recompute only when it changes (the daily cross-section is only
through the prior trading day intraday; the live bar is ``ctx.bars`` / ``ctx.price``).

Orders fill a LATER bar (``execution_lag_bars`` ahead), never within the decision
bar. Broker actions and ``ctx.state_dir`` access must run inside a positive-budget
``ctx.substep``; even light per-tick management should use a small budget such as
0.5 minutes so runtime and submit latency are accounted uniformly.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

TOP_N = 10

# ``ctx.asof_version`` changes only when the rolling view actually rolls (a dataset's
# refresh node is crossed), so caching the screen by it reads daily once per version
# rather than once per tick. The driver imports this module once and calls ``main``
# every tick, so module-level state persists across the replay (and resets per backtest).
_SCREEN_CACHE: dict[str, list[str]] = {}


def _screen(ctx) -> list[str]:
    """The day's target basket. Cached by ``ctx.asof_version`` to avoid re-reading
    the table every tick. Replace the body with your own cross-sectional signal."""
    cached = _SCREEN_CACHE.get(ctx.asof_version)
    if cached is not None:
        return cached
    daily = pd.read_parquet(Path(str(ctx.asof_dir)) / "daily")
    codes = sorted(daily["ts_code"].astype(str).unique())[:TOP_N]
    _SCREEN_CACHE[ctx.asof_version] = codes
    return codes


def main(ctx) -> None:
    with ctx.substep("main_tick", budget_minutes=0.5):
        if ctx.positions:  # already holding the basket — hold to final-day liquidation
            return
        for code in _screen(ctx):
            # Skip a code with a price unavailable this tick or an order still in flight
            # (the fill lands a later bar, so re-submitting before then would double-buy).
            if ctx.price(code) is not None and not ctx.broker.pending(code):
                ctx.broker.buy(code, weight=1.0 / TOP_N, reason="equal_weight_basket")
