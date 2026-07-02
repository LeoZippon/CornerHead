# Agent Output Contract

This directory is copied to `/mnt/agent/output/`.

`main.py` is the only required formal entrypoint. The Agent may edit `main.py`,
`candidate.py`, `trading.py`, `nl_prompt.md`, and helper modules or packages
with supported text/code suffixes. Organize code as needed, but do not write
caches, logs, data dumps, model weights, notebooks, hidden files, or secrets
here.

Persisted model parameters belong in the sibling directory
`/mnt/agent/models/`, not in `output/`. It may contain subdirectories for
reproducible model parameters such as `.json`, `.joblib`, `.pkl`, `.npy`,
`.npz`, `.pt`, `.pth`, `.onnx`, `.safetensors`, `.cbm`, `.ubj`, or `.model`
files. Temporary training files stay in `/mnt/agent/workspace/`.

## `main(ctx)` is called every replay tick

Formal backtests replay the region tick by tick and call:

```python
def main(ctx) -> None:
    ...
```

once per tick, with a **market-level** `ctx`. `main` owns all timing: it reconciles
positions/orders and maintains plans every tick, then screens/opens new positions
on the ticks it chooses. It drives the Broker primitives by `ts_code` through
`ctx.broker`; there is no `trade_intents` mapping. Submit broker orders only on
explicit orderable ticks (09:15/09:25/14:57) or ticks with live bars; ordinary
off-session ticks are for research, state updates, and plan handoff.

Orders map to live QMT `order_stock` types (no broker-side stop/conditional
order). An order reaches the book a **later bar**, `execution_lag_bars` ahead
(default 2, modelling submit latency), never within the bar you decided on: a
plain call is a **market order** (fills at that bar's open + slippage); `limit=P`
is a **limit order** (FIX_PRICE) that rests until a bar's `[low, high]` reaches P
and fills without slippage: at a favorable open if the bar opens through P,
otherwise at P after an intrabar touch. It auto-cancels after `valid_bars` bars.
Query `ctx.broker.pending(code)` to skip codes with an order still in flight, or
`ctx.broker.pending()` to scan all pending orders and `ctx.broker.cancel(order_id)`
to cancel stale unfilled orders. Orders generated inside `ctx.substep` appear in
`pending()` immediately with `pending_stage="substep_delay"` until their `ready_at`,
then submit on the first orderable tick and enter the normal submit-lag /
working-order flow.

The Environment calls `main(ctx)` across the WHOLE day on a 24h tick grid (intraday
bars at 1-minute granularity plus a configurable off-session grid for research/state
only), so the same loop also drives live trading. Do not submit new broker orders
from ordinary off-session ticks. To prepare a pre-open order, write the plan to
`ctx.state_dir` inside an off-session `ctx.substep`, then submit from the 09:15 info tick, the 09:25
matched-open tick, or a later live-bar tick. A 14:57 close-auction tick fills at the
15:00 close.

The default `main.py` is a deliberately minimal **working** baseline — while flat,
buy an equal-weight basket and hold to the final-day liquidation:

```python
def main(ctx):
    with ctx.substep("main_tick", budget_minutes=0.5):
        if ctx.positions:             # already holding — hold to final-day liquidation
            return
        daily = pd.read_parquet(Path(str(ctx.asof_dir)) / "daily")  # a domain is a directory
        codes = sorted(daily["ts_code"].astype(str).unique())[:10]  # placeholder signal
        for code in codes:
            if ctx.price(code) is not None and not ctx.broker.pending(code):
                ctx.broker.buy(code, weight=0.1)
```

For finer control, the **optional** recommended cadence (in `candidate.py` /
`trading.py`) screens in a sub-step that STAGES the plan to `ctx.state_dir` (visible
after the block's declared duration), and later ticks execute and reconcile it:

```python
from candidate import manage, research
from trading import manage_positions


def main(ctx):
    with ctx.substep("manage_tick", budget_minutes=0.5):
        manage_positions(ctx)         # exits / 做T on holdings at orderable ticks
        manage(ctx)                   # reconcile every tick; submit only at orderable ticks
    research(ctx)                     # self-wraps when it decides to screen;
                                      # the plan lands in ctx.state_dir after ready_at
```

The sample `candidate.manage()` skips ticks where `ctx.price(code)` is `None`, so it
will naturally wait for 09:25 or a continuous live-bar tick. If you intentionally
want blind 09:15 auction orders, adapt the guard deliberately and make sure sizing is
valid without a current price.

Anchor heavy work on a **fixed daily schedule**, like a real trader's routine: the
sample `research()` gates on `ctx.cur_time` to run its screen once per day in a
pre-open window (e.g. `08:00`), then submits at `09:15`/`09:25`, manages intraday on a
fixed cadence, and wraps up by `14:57` — rather than screening on every tick.

`ctx` exposes (rebuilt each tick):

- `ctx.cur_date` (`"YYYYMMDD"`), `ctx.cur_time` (`"HH:MM"`).
- `ctx.account`, `ctx.positions` (read-only snapshots); available cash via `ctx.broker.cash`.
- `ctx.cur_datetime` — ISO Beijing timestamp (`+08:00`) for the tick.
- `ctx.price(ts_code)`, `ctx.bar(ts_code)`, `ctx.bars` — the current tick only
  (`None` at the 09:15 info tick and off-session ticks; future bars never visible).
- `ctx.broker`: `.buy/sell/short/cover/close(ts_code, amount=None, weight=None,
  limit=None, valid_bars=1, reason=None)` returning `order_id`,
  `.cancel(order_id, reason=None)`, `.money`/`.cash`, `.position(ts_code)`,
  `.pending(ts_code=None)` (working orders; no argument returns all). `limit=P`
  makes it a limit order; the optional `reason=` is an audit annotation the driver
  records without affecting matching.
- `ctx.nl(ts_code, prompt="...")` — point-in-time NL Sub Agent (its text corpus
  also rolls on the refresh nodes; frozen research corpus always visible).
- `ctx.asof_dir` — per-tick rolling point-in-time view; one directory per data
  domain (`daily`, `events`, `macro`, `fundamentals`, `intraday_1min`), read with
  `pd.read_parquet(ctx.asof_dir / "daily")`. A row appears only once its real
  refresh job has finished by the sim clock, so the daily cross-section is through
  the prior trading day intraday (today's bar is `ctx.bars`/`ctx.price`).
- `ctx.asof_version` — changes only when the view actually rolls; cache an asof
  read by it and recompute only when it changes.
- `ctx.snapshot_dir` (frozen research baseline), `ctx.model_dir` (persisted
  parameters), `ctx.state_dir` (managed cross-tick state, only available inside
  `ctx.substep`), `ctx.params`.

`amount` is a share count (lot-aligned to 100); `weight` is a notional fraction
of initial equity. The Broker enforces cash, short margin, T+1 sellable balance,
lot size, price limits, suspension, and shortability, and reserves the final
replay date for mandatory liquidation. The Broker is the source of truth for
positions and reflects **filled** positions only; `ctx.broker.pending(code)`
exposes orders still working between decision and fill, so gate re-entry/exit on
both. For light order hygiene, run this every tick inside a small-budget sub-step:

```python
with ctx.substep("cancel_stale_pending", budget_minutes=0.5):
    for order in ctx.broker.pending():
        if order.get("order_id") and float(order.get("age_minutes") or 0.0) > 1.0:
            ctx.broker.cancel(order["order_id"], reason="stale_pending_gt_1m")
```

`ctx.state_dir` holds your own cross-tick state, not a position/order ledger;
it is only available inside `ctx.substep(name, B)`. Reads see the visible state from
the start of that sub-step; writes are STAGED and become visible only after `B`
minutes (`ready_at = tick + B`), so write a plan in one tick and read it in a later
one. Broker actions issued inside `0 < B < 1` light sub-steps are submitted in the
current decision minute; actions inside `B>=1` sub-steps are delayed to `ready_at`
before submission. It resets per backtest — durable parameters belong in `ctx.model_dir`.

## Cost discipline

`main(ctx)` runs every tick, but heavy work — cross-sectional screening, model
inference, and `ctx.nl()` — should run only on the few ticks you choose (e.g.
pre-open or near close), never every tick, or API cost and wall-clock blow up.
Load or cache model parameters from `ctx.model_dir` once; do not retrain every
minute.

`ctx.nl(ts_code, prompt="...")` (equivalently `from at_tools import nl`) starts a
host-side NL Sub Agent with a point-in-time `text_retrieve` tool and returns a
result dict. The final `content` is unconstrained; parse whatever score, label,
or decision you need in `main`/`candidate`/helpers. Request, retrieval, evidence,
result, and provider-call logs are written under the backtest result directory.
NL carries publish/ingest-time, recall, model-prior, free-text-parsing, and
look-ahead risks: down-weight or drop low-evidence conclusions, and never let NL
override cash, tradability, cost, or replay constraints.
