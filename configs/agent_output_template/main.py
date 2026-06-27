"""Formal strategy entrypoint.

The Environment calls ``main(ctx)`` once per replay tick. Orders use NEXT-BAR
execution: an order placed on a tick fills at the NEXT bar's open, so you decide
on the data you can see and the fill happens one bar later, never within the same
bar. The Broker enforces cash, short margin, T+1, lot size, price limits,
suspension, and shortability, and records every fill.

Recommended daily cadence (illustrated below):

* 09:15 pre-open info tick — ``ctx.price`` is None (the auction has not matched).
  Do the expensive work here: screen the cross-section, call ``ctx.nl(...)``, and
  write the chosen targets to ``ctx.state_dir``.
* 09:25 pre-open tick — the matched open is visible via ``ctx.price``. Read the
  targets back and submit the orders; ``weight`` sizing works because the price is
  known, and submitting from the persisted list keeps the order set deduplicated.
  These orders fill at the 09:31 open (the first continuous bar).
* every tick — manage open positions (exits / 做T / stops); those orders fill on
  the next bar too.

``ctx`` (market-level, rebuilt each tick):

* ``ctx.cur_date`` ("YYYYMMDD"), ``ctx.cur_time`` ("HH:MM").
* ``ctx.account`` / ``ctx.positions`` — read-only snapshots; ``ctx.cash``.
* ``ctx.price(ts_code)`` / ``ctx.bar(ts_code)`` / ``ctx.bars`` — this tick only
  (None at the 09:15 info tick).
* ``ctx.broker`` — ``.buy/sell/short/cover/close(ts_code, amount=None, weight=None)``,
  ``.cash``/``.money``, ``.position(ts_code)``.
* ``ctx.nl(ts_code, prompt=...)`` — PIT NL sub-agent; use it only in the decision
  stage and keep its frequency low (it is the main API cost).
* ``ctx.asof_dir`` / ``ctx.snapshot_dir`` — point-in-time data for screening;
  ``ctx.model_dir`` — persisted parameters; ``ctx.state_dir`` — run-scoped scratch
  (e.g. the day's targets); ``ctx.params``.

The position view reflects FILLED positions only (orders fill next bar), so place
each entry once on a decision tick and track your own intent in ``ctx.state_dir``
rather than re-deciding every tick. ``amount`` is a lot-aligned share count (100);
``weight`` is a notional fraction of initial equity.
"""

from __future__ import annotations

from candidate import open_targets, screen_targets
from trading import manage_positions


def main(ctx) -> None:
    # Manage existing positions every tick (exits, 做T, stops) — fills next bar.
    manage_positions(ctx)
    # Decide once pre-open, then submit once the matched open price is known.
    if ctx.cur_time == "09:15":
        screen_targets(ctx)  # heavy work: screen + nl(); write targets to state_dir
    elif ctx.cur_time == "09:25":
        open_targets(ctx)  # read targets; submit orders that fill at the 09:31 open
