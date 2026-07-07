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
to cancel stale unfilled orders. Cross-minute `ctx.substep` actions are not broker
orders until `ready_at`, so they do not appear in `pending()` before submission.
After submission they enter the normal submit-lag / working-order flow.

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
            price = ctx.price(code)
            if price is not None and not ctx.broker.pending(code):
                amount = int((ctx.broker.stock["available_cash"] / 10) / price // 100 * 100)
                if amount > 0:
                    ctx.broker.buy(code, amount=amount)
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

## `ctx.broker` quick reference

Use these only inside `ctx.substep(name, budget_minutes=B)`. `amount` is shares:
ordinary A-shares use positive 100-share lots, STAR Market uses 200 shares minimum
then 1-share increments, and size is never inferred from `weight`.

| Interface | Use |
|---|---|
| `buy(ts_code, amount, limit=None, valid_bars=1, reason=None)` | Stock account cash buy |
| `sell(ts_code, amount, limit=None, valid_bars=1, reason=None)` | Stock account sell of T+1 sellable long shares |
| `credit_buy(ts_code, amount, limit=None, valid_bars=1, reason=None)` | Credit account collateral buy |
| `credit_sell(ts_code, amount, limit=None, valid_bars=1, reason=None)` | Credit account collateral sell; financed shares must use `sell_repay` |
| `fin_buy(ts_code, amount, limit=None, valid_bars=1, reason=None)` | Margin buy; creates /360 daily interest-accruing financing debt |
| `short(ts_code, amount, limit, valid_bars=1, reason=None)` | Short sale; `limit` is required and must satisfy the uptick rule |
| `cover(ts_code, amount, limit=None, valid_bars=1, reason=None)` | Buy to cover short debt, oldest contract first; same-day short cover is T+1-blocked |
| `sell_repay(ts_code, amount, limit=None, valid_bars=1, reason=None)` | Sell credit-account shares and repay financing debt interest-first |
| `direct_repay(amount, reason=None)` | Repay financing debt from credit-account cash; strict reject if cash/debt is insufficient |
| `transfer(amount, from_account, to_account, reason=None)` | Pre-09:14 cash transfer request between `stock` and `credit` accounts |
| `close(ts_code, account=None, reason=None)` | Market exit; pass `account=` if both accounts hold the code |
| `cancel(order_id, reason=None)` | Cancel an order returned by `pending()` |
| `pending(ts_code=None)` | Submitted, unfilled, cancellable orders; no argument returns all |
| `position(ts_code, account=None)` | Filled position only; default nets across accounts |
| `stock` / `credit` / `account` / `positions` | Account and position snapshots; `cash`/`quantity` are filled truth, while available cash/bail/sellable shares reserve submitted pending orders |
| `debt_contracts(ts_code=None)` | Open financing/short debt contracts and accrued interest |

Common mistakes: broker actions outside `ctx.substep` are rejected; `short`
without `limit=` is rejected; cross-minute substep actions are not orders until
`ready_at` and therefore do not appear in `pending()` before submission.

`ctx` exposes (rebuilt each tick):

| ctx surface | Environment contract |
|---|---|
| `ctx.cur_datetime` | Authoritative sim timestamp in Asia/Shanghai ISO format; drives Timeview, substep `ready_at`, delayed submission, and matching |
| `ctx.cur_date` | Current trade date derived from `cur_datetime`, `YYYYMMDD`; use for daily logic, cache keys, and state filenames |
| `ctx.cur_time` | Current intraday minute derived from `cur_datetime`, `HH:MM`; use for scheduled actions such as 09:25 and 14:57 |
| `ctx.account` | Read-only dual-account snapshot: `stock`, `credit`, `total_assets`, `risk_limits` |
| `ctx.positions` | Read-only per-symbol position rows; each row includes `account` to distinguish stock and credit holdings |
| `ctx.price(ts_code)` | Current tick-visible price for one symbol; future prices are never visible, and 09:15 plus ordinary off-session ticks usually return `None` |
| `ctx.bar(ts_code)` | Current tick-visible bar for one symbol; returns `None` when no bar is visible |
| `ctx.bars` | Current tick-visible market bar list; contains only this tick, never future bars |
| `ctx.broker` | Broker queries, order/cancel verbs, and margin primitives; order/cancel calls must be inside `ctx.substep`; see the broker quick reference above |
| `ctx.substep(name, budget_minutes=B)` | Strategy-step budget context; declares compute time, state `ready_at`, and broker action submit timing |
| `ctx.nl(ts_code?, prompt="...")` | Point-in-time NL Sub Agent for single-stock or event/theme/sector/macro text analysis; must run inside `ctx.substep` and follows sim-clock text visibility |
| `ctx.asof_dir` | Per-tick rolling, refresh-node-gated parquet PIT view: `daily`, `events`, `macro`, `fundamentals`, `intraday_1min` |
| `ctx.asof_version` | Changes only when Timeview actually rolls; cache as-of reads by this value |
| `ctx.snapshot_dir` | Frozen research baseline snapshot; does not roll during replay |
| `ctx.state_dir` | Managed cross-tick state directory; only available inside `ctx.substep`, with writes staged until `ready_at` |
| `ctx.model_dir` | Read-only persisted model artifact directory; data that must persist across backtests belongs in `models/` before replay |
| `ctx.params` | Read-only run parameters |

`amount` is a share count (lot-aligned to 100). The Broker enforces cash and
保证金可用余额, T+1 sellable balance, lot size, price limits, suspension,
margin-target eligibility and credit
quotas, per-calendar-day debt interest, the maintenance-ratio forced close, and
reserves the final replay date for mandatory liquidation. The Broker is the source
of truth for positions and reflects **filled** positions only;
`ctx.broker.pending(code)` exposes orders still working between decision and fill,
so gate re-entry/exit on both. For light order hygiene, run this every tick inside
a small-budget sub-step:

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
before submission. It resets per backtest — durable parameters belong in
`models/`, written before `backtest`; `ctx.model_dir` is read-only during
formal replay.

## Cost discipline

`main(ctx)` runs every tick, but heavy work — cross-sectional screening, model
inference, and `ctx.nl()` — should run only on the few ticks you choose (e.g.
pre-open or near close), never every tick, or API cost and wall-clock blow up.
Load or cache model parameters from `ctx.model_dir` once; do not write or
retrain into `ctx.model_dir` during replay.

`ctx.nl(ts_code?, prompt="...")` (equivalently `from at_tools import nl`) starts a
host-side NL Sub Agent. Passing `ts_code` requests single-stock PIT text analysis;
`ctx.nl(prompt="...")`
uses the same service for event, theme, sector, macro, or market-wide PIT text
retrieval. `ts_code` is a context/ranking hint, not a hard filter. The final
`content` is unconstrained; parse whatever score, label, or decision you need in
`main`/`candidate`/helpers. Request, retrieval, evidence, result, and
provider-call logs are written under the backtest result directory.
NL carries publish/ingest-time, recall, model-prior, free-text-parsing, and
look-ahead risks: down-weight or drop low-evidence conclusions, and never let NL
override cash, tradability, cost, or replay constraints.
