"""Formal strategy entrypoint — minimal working default.

The Environment calls ``main(ctx)`` once per replay tick (a market-level ``ctx``).
This default is intentionally small but complete: while flat it buys an
equal-weight basket from the frozen research baseline (screened once per
backtest) and holds it to the mandatory final-day liquidation. Replace the
placeholder screen in ``_screen`` with your own signal.

Key ``ctx`` surface (advanced helpers in ``candidate.py`` / ``trading.py`` + ``README.md``):
  ``ctx.account`` / ``ctx.positions``                 dual-account snapshots / per-symbol holdings (rows carry ``account``)
  ``ctx.broker.stock`` / ``ctx.broker.credit``         per-account dict properties (do not call them)
  ``ctx.price(code)`` / ``ctx.bar(code)``              this tick's price/bar (None pre-auction)
  ``ctx.broker.buy/sell(...)``                         stock account (long-only cash)
  ``ctx.broker.credit_buy/credit_sell/fin_buy/short/cover/sell_repay/direct_repay``  credit account (两融)
  ``ctx.broker.transfer(amount, from_account, to_account)``  pre-09:14 account transfer request
  ``ctx.broker.close(code, account=...)``              market exit; account= required if both hold the code
  ``ctx.broker.pending(code=None)`` / ``ctx.broker.cancel(order_id)``      query/cancel working orders
  ``ctx.asof_dir`` / ``ctx.asof_version``              rolling point-in-time data view + its version
  ``ctx.snapshot_dir`` / ``ctx.model_dir``             frozen research snapshot / model artifacts
  ``ctx.state_dir``                                    managed cross-tick state (only available inside substep)
  ``ctx.substep(name, budget_minutes=B)``              required wrapper for broker/state actions; B<1 submits this tick, B>=1 after ready_at
  ``ctx.nl(code?, prompt=..., event_filter?=..., response_format?=...)``  optional PIT text analysis

``ctx.asof_dir`` holds directories for ``daily``, ``events``, ``macro``,
``fundamentals``, ``intraday_1min`` and ``text_index``, plus the ``universe.parquet``
file and ``text_library`` body shards.
The context directories are path strings. Read parquet domains with
``pd.read_parquet(Path(str(ctx.asof_dir)) / "daily")``.
The view rolls forward as each dataset's real refresh job completes.
``ctx.asof_version`` covers the whole view, so the minute domain can change it every
minute; do not use that global version to invalidate unrelated heavy daily features.
Run rolling research at a fixed time and cache by the date/data node it actually
depends on. ``ctx.snapshot_dir`` never rolls, so frozen features should be computed
once per backtest (the live bar is always ``ctx.bars`` / ``ctx.price``).

Orders enter the Broker a LATER fixed session minute (``execution_lag_bars`` ahead),
never within the decision minute; without a market event they remain working.
Broker actions and ``ctx.state_dir`` access must run inside a positive-budget
``ctx.substep``; even light per-tick management should use a small budget such as
0.5 minutes so runtime and submit latency are accounted uniformly.

Account/position views are snapshots from tick entry: same-tick actions do not
reduce those views (submitted light actions also appear in ``pending()``). Batch
orders therefore keep a local remaining budget and leave a fee/slippage buffer.
Same-event matching is FIFO; only earlier orders still working reserve resources.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

TOP_N = 10

# The driver imports this module once and calls ``main`` every tick, so module-level
# state persists across the replay and resets for the next backtest. ``snapshot_dir``
# is immutable within one replay: cache its heavy features by path and never reread
# the same wide table on every tick/day.
_SCREEN_CACHE: dict[str, list[str]] = {}


def _screen(ctx) -> list[str]:
    """Frozen target basket, computed once per backtest."""
    snapshot = str(ctx.snapshot_dir)
    cached = _SCREEN_CACHE.get(snapshot)
    if cached is not None:
        return cached
    daily = pd.read_parquet(Path(snapshot) / "daily.parquet", columns=["ts_code"])
    codes = sorted(daily["ts_code"].astype(str).unique())[:TOP_N]
    _SCREEN_CACHE[snapshot] = codes
    return codes


def main(ctx) -> None:
    with ctx.substep("main_tick", budget_minutes=0.5):
        if ctx.positions:  # already holding the basket — hold to final-day liquidation
            return
        codes = _screen(ctx)
        remaining_budget = float(ctx.broker.stock["available_cash"]) * 0.95
        for index, code in enumerate(codes):
            # Skip a code with a price unavailable this tick or an order still in flight
            # (the fill lands a later bar, so re-submitting before then would double-buy).
            price = ctx.price(code)
            if price is not None and not ctx.broker.pending(code):
                target_budget = remaining_budget / (len(codes) - index)
                amount = int(target_budget / float(price) // 100 * 100)
                if amount > 0:
                    ctx.broker.buy(code, amount=amount, reason="equal_amount_basket")
                    remaining_budget -= amount * float(price)
