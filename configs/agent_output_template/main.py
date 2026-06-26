"""Formal strategy entrypoint.

The Environment calls ``main(ctx)`` once per replay minute. ``main`` owns all
timing: it manages open positions every minute and screens/opens new positions
on the ticks you choose. It drives the Broker primitives by ``ts_code`` through
``ctx.broker``; the Broker enforces cash, short margin, T+1, lot size, price
limits, suspension, and shortability, and records every fill.

``ctx`` (market-level, rebuilt each minute):

* ``ctx.cur_date`` ("YYYYMMDD"), ``ctx.cur_time`` ("HH:MM").
* ``ctx.account`` / ``ctx.positions`` — read-only snapshots; ``ctx.cash``.
* ``ctx.price(ts_code)`` / ``ctx.bar(ts_code)`` / ``ctx.bars`` — this minute only.
* ``ctx.broker`` — ``.buy/sell/short/cover/close(ts_code, amount=None, weight=None)``,
  ``.cash``/``.money``, ``.position(ts_code)``.
* ``ctx.nl(ts_code, prompt=...)`` — PIT NL sub-agent; use it in the decision stage
  and keep its frequency low (it is the main API cost).
* ``ctx.snapshot_dir`` — point-in-time data for screening; ``ctx.model_dir`` —
  persisted parameters; ``ctx.state_dir`` — run-scoped scratch (e.g. holdings.json);
  ``ctx.params``.

Keep cross-minute bookkeeping in ``ctx.state_dir``; the Broker stays the source
of truth for actual positions. ``amount`` is a lot-aligned share count (100);
``weight`` is a notional fraction of initial equity.
"""

from __future__ import annotations

from candidate import screen_and_open
from trading import manage_positions


def main(ctx) -> None:
    # 1) Manage existing positions every minute (exits, 做T, stops).
    manage_positions(ctx)
    # 2) Open new positions on chosen ticks. You control the cadence here; run
    #    heavy work (screening, nl()) on a few ticks, not on every minute.
    if ctx.cur_time == "09:30":
        screen_and_open(ctx)
