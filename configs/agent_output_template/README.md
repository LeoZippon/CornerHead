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
explicit orderable ticks (09:15/09:25/14:57, plus the after-hours fixed-price
tick when enabled — see the `afterhours_decision_time` fact) or ticks with live
bars; ordinary off-session ticks are for research, state updates, and plan handoff.

Orders map to live QMT `order_stock` types (no broker-side stop/conditional
order). An order reaches the book a **later bar**, `execution_lag_bars` ahead
(default 2, modelling submit latency), never within the bar you decided on: a
plain call is a **market order** (fills at its bar's open + slippage; if the code
prints no bar that minute it keeps working and fills at the day's next traded
bar, else the day-end sweep cancels it); `limit=P` is a **limit order**
(FIX_PRICE) that fills without slippage: at a favorable open if the bar opens
through P, otherwise at P only when the bar trades STRICTLY through it — a bare
touch (low == P on a buy, high == P on a sell) counts as queued, unfilled. It
rests until filled, explicitly cancelled, or swept at day end.
Query `ctx.broker.pending(code)` to skip codes with an order still in flight, or
`ctx.broker.pending()` to scan all pending orders and `ctx.broker.cancel(order_id)`
to cancel stale unfilled orders. Cross-minute `ctx.substep` actions are not broker
orders until `ready_at`, so they do not appear in `pending()` before submission.
After submission they enter the normal submit-lag / working-order flow.

`ctx.account`, `ctx.positions`, `ctx.broker.stock`, and `ctx.broker.credit` are
snapshots from tick entry. A same-tick action enters the action queue (submitted
light actions also appear in `pending()`) but does not rewrite those snapshots. For
a batch, read the budget once, decrement it locally, and leave room for costs.
Within one bar the Broker matches FIFO: filled/rejected predecessors release their
reservation immediately; only earlier orders that remain working keep resources
frozen.

The Environment calls `main(ctx)` across the WHOLE day on a 24h tick grid (intraday
bars at 1-minute granularity plus a configurable off-session grid for research/state
only), so the same loop also drives live trading. Do not submit new broker orders
from ordinary off-session ticks. To prepare a pre-open order, write the plan to
`ctx.state_dir` inside an off-session `ctx.substep`, then submit from the blind 09:15/09:25
ticks or a later live-bar tick. An observed auction-result tick between 09:25 and 09:30 is research-only. A 14:57 close-auction tick fills at the
15:00 close. The after-hours fixed-price tick (default 15:05, when enabled) shows the
confirmed close in `ctx.bars` and settles orders **immediately at that close** (no
slippage, no lag; a limit worse than the close is an invalid submission) — only for
codes whose board has after-hours trading on that date (STAR from 2019-07, ChiNext
from 2020-08, all remaining A-shares from 2026-07-06; earlier dates reject
`afterhours_not_available`), with `short`/`fin_buy` unsupported there and all the
usual limit/suspension/T+1/cash constraints still enforced.

The default `main.py` is a deliberately minimal **working** baseline — while flat,
buy an equal-weight basket and hold to the final-day liquidation:

```python
def main(ctx):
    with ctx.substep("main_tick", budget_minutes=0.5):
        if ctx.positions:             # already holding — hold to final-day liquidation
            return
        # snapshot_dir is frozen for this replay: put this read/rank behind a
        # module-level cache in real code (see the shipped main.py).
        daily = pd.read_parquet(Path(str(ctx.snapshot_dir)) / "daily.parquet", columns=["ts_code"])
        codes = sorted(daily["ts_code"].astype(str).unique())[:10]  # placeholder signal
        remaining_budget = float(ctx.broker.stock["available_cash"]) * 0.95
        for index, code in enumerate(codes):
            price = ctx.price(code)
            if price is not None and not ctx.broker.pending(code):
                target_budget = remaining_budget / (len(codes) - index)
                amount = int(target_budget / price // 100 * 100)
                if amount > 0:
                    ctx.broker.buy(code, amount=amount)
                    remaining_budget -= amount * price
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

The sample `candidate.manage()` skips ticks where `ctx.price(code)` is `None` and the
09:25–09:30 no-submission gap, so it waits for a continuous live-bar tick. If you intentionally
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
| `buy(ts_code, amount, limit=None, reason=None)` | Stock account cash buy |
| `sell(ts_code, amount, limit=None, reason=None)` | Stock account sell of T+1 sellable long shares |
| `credit_buy(ts_code, amount, limit=None, reason=None)` | Credit account collateral buy |
| `credit_sell(ts_code, amount, limit=None, reason=None)` | Credit account collateral sell; financed shares must use `sell_repay` |
| `fin_buy(ts_code, amount, limit=None, reason=None)` | Margin buy; creates /360 daily interest-accruing financing debt |
| `short(ts_code, amount, *, limit, reason=None)` | Short sale; pass a finite positive `limit=` and satisfy the uptick rule |
| `cover(ts_code, amount, limit=None, reason=None)` | Buy to cover short debt, oldest contract first; same-day short cover is T+1-blocked |
| `sell_repay(ts_code, amount, limit=None, reason=None)` | Sell credit-account shares and repay financing debt interest-first |
| `direct_repay(amount, reason=None)` | Repay financing debt from credit-account cash; strict reject if cash/debt is insufficient |
| `transfer(amount, from_account, to_account, reason=None)` | Pre-09:14 cash transfer request between `stock` and `credit` accounts |
| `close(ts_code, account=None, reason=None)` | Market exit; pass `account=` if both accounts hold the code |
| `cancel(order_id, reason=None)` | Cancel an order returned by `pending()` |
| `pending(ts_code=None)` | Submitted, unfilled, cancellable orders; no argument returns all |
| `position(ts_code, account=None)` | Filled position only; default nets across accounts |
| `stock` / `credit` / `account` / `positions` | Tick-entry account and position snapshots; `stock`/`credit` are dict properties (no parentheses); same-tick actions do not rewrite them, while submitted earlier pending orders are already reserved |
| `debt_contracts(ts_code=None)` | Open financing/short debt contracts and accrued interest |

Common mistakes: broker actions outside `ctx.substep` are rejected; `short`
without `limit=` is rejected; cross-minute substep actions are not orders until
`ready_at`, must still be ready inside an exchange order-submission window, and
therefore do not appear in `pending()` before submission.

`ctx` exposes (rebuilt each tick):

| ctx surface | Environment contract |
|---|---|
| `ctx.cur_datetime` | Authoritative Asia/Shanghai ISO-8601 string, for example `"2025-01-02T09:25:00+08:00"`; use it directly, or parse it with `datetime.fromisoformat()` when an object is needed |
| `ctx.cur_date` | Current trade date derived from `cur_datetime`, `YYYYMMDD`; use for daily logic, cache keys, and state filenames |
| `ctx.cur_time` | Current intraday minute derived from `cur_datetime`, `HH:MM`; use for scheduled actions such as 09:25 and 14:57 |
| `ctx.account` | Read-only dual-account snapshot: `stock`, `credit`, `total_assets`, `risk_limits` |
| `ctx.positions` | Read-only per-symbol position rows. Exact row keys: `account`, `ts_code`, `side`, `quantity`, `sellable_quantity`, `entry_price`, `entry_date`, `entry_cost`, `last_price`, `market_value`. There is no `qty`/`volume`/`cost_basis`/`avg_price` key — `row.get("volume", 0)` silently returns 0 and kills every exit path. Detect holdings via `quantity`; size sells by `sellable_quantity` (T+1 sellable, net of pending sells), never by `quantity` |
| `ctx.price(ts_code)` | Current tick-visible price for one symbol; future prices are never visible, and 09:15 plus ordinary off-session ticks usually return `None` |
| `ctx.bar(ts_code)` | Current tick-visible bar for one symbol; returns `None` when no bar is visible |
| `ctx.bars` | Current tick-visible market bar list; contains only this tick, never future bars |
| `ctx.broker` | Broker queries, order/cancel verbs, and margin primitives; order/cancel calls must be inside `ctx.substep`; see the broker quick reference above |
| `ctx.substep(name, budget_minutes=B)` | Strategy-step budget context; declares compute time, state `ready_at`, and broker action submit timing |
| `ctx.nl(ts_code?, prompt="...", event_filter?=..., response_format?=...)` | Point-in-time NL Sub Agent; stock calls may declare a rolling event gate and enum output contract; must run inside `ctx.substep` and follows sim-clock text visibility |
| `ctx.asof_dir` | Path string for the per-tick PIT view: dataset directories such as `daily`, plus the single file `universe.parquet` and `text_library`; wrap with `Path(str(...))` before `/` joins |
| `ctx.asof_version` | Global version that changes when any Timeview domain rolls, including minute data; use a narrower key for heavy single-domain features |
| `ctx.snapshot_dir` | Path string for the frozen research baseline snapshot; does not roll during replay |
| `ctx.state_dir` | Path string for the managed cross-tick state directory (wrap with `Path(str(...))` before `/` joins); only available inside `ctx.substep`; first access copies visible state, then writes stage until `ready_at` |
| `ctx.model_dir` | Path string for the read-only persisted model artifact directory; data that must persist across backtests belongs in `models/` before replay |

For direct text processing, read `pd.read_parquet(Path(str(ctx.asof_dir)) / "text_index")`
and join rows to `Path(str(ctx.asof_dir)) / "text_library"` via each row's `library_file` and `text_id`.

`amount` is a share count (SH/SZ main board and ChiNext: multiples of 100; STAR:
200 minimum then 1-share steps; BSE: 100 minimum then 1-share steps). The Broker
enforces cash and
保证金可用余额, T+1 sellable balance, lot size, price limits, suspension,
margin-target eligibility and credit
quotas, per-calendar-day debt interest, the maintenance-ratio forced close, and
reserves the final replay date for mandatory liquidation. Ex-date corporate
actions apply automatically before the ex-date's first tick: longs are credited
cash dividends and 送转 bonus shares (cost basis stays continuous), 融券 shorts
compensate the lender in cash and owe the post-conversion share count — holding
through an ex-date is no longer booked as a raw price-gap loss. The Broker is the source
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
the start of that sub-step; the copy is created lazily on first access, so a
broker-only sub-step creates no state staging tree. Writes are STAGED and become
visible only after `B`
minutes (`ready_at = tick + B`), so write a plan in one tick and read it in a later
one. Broker actions issued inside `0 < B < 1` light sub-steps are submitted in the
current decision minute; actions inside `B>=1` sub-steps are delayed to `ready_at`
and are submitted only if they have not crossed out of the exchange's accepted
order-submission windows. It resets per backtest — durable parameters belong in
`models/`, written before `backtest`; `ctx.model_dir` is read-only during
formal replay.

## Cost discipline

`main(ctx)` runs every tick, but heavy work — cross-sectional screening, model
inference, and `ctx.nl()` — should run only on the few ticks you choose (e.g.
pre-open or near close), never every tick, or API cost and wall-clock blow up.
Load or cache model parameters from `ctx.model_dir` once; do not write or
retrain into `ctx.model_dir` during replay.

Use the narrowest cache key that matches the data dependency: frozen
`ctx.snapshot_dir` features are computed once per backtest; rolling daily/event
features run at one fixed research time and cache by their effective date or another
strategy-owned key. Do not invalidate heavy daily features on every
`ctx.asof_version` change because that global version also tracks minute data. Always
project required columns and filter large `events`/minute domains before converting
them to pandas.
For repeated factor work on large PIT tables, take the exact per-symbol tail needed
by the longest window before rolling/group operations and joins. Compare factors,
ranks, candidates, and orders against the full-history implementation at `1e-12`
tolerance; do not substitute sampling or approximate windows for speed.

Formal strategy processes use a fixed Python hash seed for repeatable unordered
container iteration across runs. Still sort candidates explicitly whenever order
expresses investment priority; reproducibility is not a substitute for intent.

`ctx.nl(ts_code?, prompt="...", event_filter?=..., response_format?=...)`
(equivalently `from at_tools import nl`) starts a
host-side NL Sub Agent. Passing `ts_code` requests single-stock PIT text analysis;
`ctx.nl(prompt="...")`
uses the same service for event, theme, sector, macro, or market-wide PIT text
retrieval. `ts_code` strictly bounds title and body retrieval to evidence linked
by that code or company name; omit it when broad context is required. For a stock,
declare `event_filter={"patterns": [...], "lookback_days": N}` to run only when
matching evidence exists inside the rolling PIT window. No match is the successful
state `no_matching_evidence` with empty content and no provider call; matching
revisions also let completed results remain reusable until evidence enters or exits
the window. Narrow labels should declare
`response_format={"type": "enum", "values": [...]}` and use the returned canonical
value directly. Without it, `content` remains free-form. Request, retrieval,
evidence, result, and provider-call logs are written under the backtest result
directory.
NL carries publish/ingest-time, recall, model-prior, free-text-parsing, and
look-ahead risks: down-weight or drop low-evidence conclusions, and never let NL
override cash, tradability, cost, or replay constraints.
