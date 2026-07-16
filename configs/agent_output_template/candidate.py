"""Optional advanced helper — the minimal default ``main.py`` does not import this.

Shows the recommended cadence for a heavier strategy under the 24h tick model:

* ``research(ctx)`` wraps the expensive cross-sectional screen in
  ``ctx.substep("research", budget_minutes=B)`` and writes the day's order plan to
  ``ctx.state_dir``. Writes inside a sub-step are STAGED: they become visible in
  ``ctx.state_dir`` only after the block's declared duration elapses
  (ready_at = this tick + B), modelling the latency before a heavy computation's
  output is usable. So research and execution are separate ticks — a later tick
  reads the plan once it has landed. Read the data domains from ``ctx.asof_dir``
  (rolling, point-in-time) and the frozen history from ``ctx.snapshot_dir``;
  ``ctx.nl(code?, prompt=..., event_filter?=..., response_format?=...)`` is optional
  and the main API cost — gate it on explicit PIT events and keep it rare.
  Broker actions issued inside a sub-step are also delayed until ready_at, but this
  template still writes an explicit plan first so execution and reconciliation are
  easy to audit.
* ``manage(ctx)`` runs every tick on the resident plan (no re-screening) and must be
  called from inside a light ``ctx.substep`` by ``main``. It submits still-pending
  entries, marks entries filled against the real broker position, cancels plan
  entries or stale Broker pending orders, and persists the updated plan. Status
  updates are staged like any other ``ctx.state_dir`` write and become visible after
  that management substep's ``ready_at``.
  Ordinary off-session ticks must not submit broker orders; use them to update the
  plan, then submit from an explicit orderable tick or a tick with a live bar. This
  template's ``manage`` keeps a ``ctx.price(code) is None`` guard plus the
  09:25-09:30 no-submission gap, so it waits until continuous trading. If a strategy intentionally uses blind
  09:15 auction orders, adapt that guard deliberately and ensure sizing is valid
  without a current price.
  Account views stay fixed for one tick, so ``manage`` reads cash once, keeps a local
  remaining budget, and leaves a fee/slippage buffer across the batch.

Each plan entry carries a status (``pending`` / ``filled`` / ``cancelled``); only
unfinished entries are acted on, reconciled against ``ctx.broker`` truth, mirroring
how a live executor tracks its own working orders.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd  # noqa: F401 - available for screening reads

_PLAN = "plan.json"
TOP_N = 5


def _plan_path(ctx) -> Path:
    return Path(str(ctx.state_dir), _PLAN)


def research(ctx, *, budget_minutes: float = 5.0, screen_time: str = "08:00") -> None:
    """Screen the cross-section in a sub-step and stage the day's plan to state_dir.

    Anchored to a FIXED daily pre-open time (a real trader's routine): the
    ``ctx.cur_time`` gate runs the heavy screen once per day in the pre-open window,
    not every tick. The template selects nothing; a strategy ranks ``ctx.asof_dir`` and
    writes a ``{ts_code: {"status": "pending", "cash_fraction": f}}`` map. The write
    is staged and surfaces in ``ctx.state_dir`` only after ``budget_minutes`` elapse,
    so a later orderable tick (09:15 / 09:25) executes it."""
    if ctx.cur_time < screen_time or ctx.cur_time >= "09:15":
        return  # only screen in the fixed pre-open window [screen_time, 09:15)
    with ctx.substep("research", budget_minutes=budget_minutes):
        path = _plan_path(ctx)
        if path.exists():
            resident = json.loads(path.read_text(encoding="utf-8"))
            if resident.get("plan_date") == ctx.cur_date:
                return  # today's plan is already staged/live; manage() acts on it
            # A plan carries its period key and EXPIRES with it: a stale plan must
            # be regenerated, not silently trusted forever (see the prompt's
            # cross-period lifecycle contract).
        daily = pd.read_parquet(Path(str(ctx.asof_dir)) / "daily", columns=["ts_code"])
        codes = sorted(daily["ts_code"].astype(str).unique())[:TOP_N]
        plan = {
            "plan_date": ctx.cur_date,
            "entries": {code: {"status": "pending", "cash_fraction": 1.0 / TOP_N} for code in codes},
        }
        # Inside the sub-step ctx.state_dir is the staging dir, so this write is held
        # back until ready_at; a later tick's manage() sees it once it has merged.
        path.write_text(json.dumps(plan), encoding="utf-8")


def manage(ctx) -> None:
    """Act on the resident plan: submit pending entries, reconcile fills, persist.

    Caller contract: run this inside ``ctx.substep(...)``.
    """
    path = _plan_path(ctx)
    if not path.exists():  # the staged plan has not become visible yet
        return
    plan = json.loads(path.read_text(encoding="utf-8"))
    entries = plan.get("entries", {})
    changed = False
    cash_budget = float(ctx.broker.stock["available_cash"]) * 0.95
    remaining_budget = cash_budget
    for code, entry in entries.items():
        if entry.get("status") != "pending":
            continue
        if ctx.broker.position(code) != 0:  # the broker confirms the fill
            entry["status"] = "filled"
            changed = True
            continue
        if "09:25" < ctx.cur_time < "09:30":
            continue  # an observed auction result is research-only in this gap
        price = ctx.price(code)
        if ctx.broker.pending(code) or price is None:
            continue  # an order is already in flight, or no price to size/submit yet
        cash_fraction = float(entry.get("cash_fraction") or 0.0)
        target_budget = min(remaining_budget, cash_budget * cash_fraction)
        amount = int(target_budget / float(price) // 100 * 100)
        if amount <= 0:
            continue
        ctx.broker.buy(code, amount=amount, reason="plan_entry")
        remaining_budget -= amount * float(price)
    if changed:
        path.write_text(json.dumps(plan), encoding="utf-8")
