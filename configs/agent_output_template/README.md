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

once per tick, with a **market-level** `ctx`. `main` owns all timing: it manages
open positions every tick and screens/opens new positions on the ticks it
chooses. It drives the Broker primitives by `ts_code` through `ctx.broker`; there
is no `trade_intents` mapping — positions can open or change at any tick.

Orders are **market orders** (parity with the live QMT controller — no
broker-side conditional/stop orders): an order placed on a tick fills at a **later
bar's open**, `execution_lag_bars` ahead (default 2, modelling submit latency),
never within the bar you decided on. Query `ctx.broker.pending(code)` to skip
codes with an order still in flight. The recommended daily cadence splits the
decision from the order, because the 09:15 pre-open info tick has no price yet:

```python
from candidate import open_targets, screen_targets
from trading import manage_positions


def main(ctx):
    manage_positions(ctx)             # exits / 做T every tick (fills next bar)
    if ctx.cur_time == "09:15":       # no price yet: screen + nl(), persist targets
        screen_targets(ctx)
    elif ctx.cur_time == "09:25":     # matched open known: submit; fills at 09:31
        open_targets(ctx)
```

`ctx` exposes (rebuilt each tick):

- `ctx.cur_date` (`"YYYYMMDD"`), `ctx.cur_time` (`"HH:MM"`).
- `ctx.account`, `ctx.positions` (read-only snapshots), `ctx.cash`.
- `ctx.price(ts_code)`, `ctx.bar(ts_code)`, `ctx.bars` — the current tick only
  (`None` at the 09:15 info tick; future bars are never visible).
- `ctx.broker`: `.buy/sell/short/cover/close(ts_code, amount=None, weight=None)`,
  `.money`/`.cash`, `.position(ts_code)`, `.pending(ts_code)` (working orders).
- `ctx.nl(ts_code, prompt="...")` — point-in-time NL Sub Agent (decision stage).
- `ctx.asof_dir` (rolling daily as-of view) and `ctx.snapshot_dir` (point-in-time
  data for screening), `ctx.model_dir` (persisted parameters), `ctx.state_dir`
  (run-scoped scratch, e.g. the day's targets), `ctx.params`.

`amount` is a share count (lot-aligned to 100); `weight` is a notional fraction
of initial equity. The Broker enforces cash, short margin, T+1 sellable balance,
lot size, price limits, suspension, and shortability, and reserves the final
replay date for mandatory liquidation. The Broker is the source of truth for
positions and reflects **filled** positions only; `ctx.broker.pending(code)`
exposes orders still working between decision and fill, so gate re-entry/exit on
both. `ctx.state_dir` is for your own scratch (e.g. the day's targets), not a
position or order ledger.

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
