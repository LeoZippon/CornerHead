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

Formal backtests execute:

```python
def run_strategy(context: dict[str, object]) -> dict[str, object]:
    ...
```

The return value should include `trade_intents` or `trades`: a list or DataFrame
that **maps each candidate stock to one trading-strategy function**. Required
fields per row: `code`/`ts_code` and `trade_strategy` (the function name).
Optional: `params` (a dict, or inline keyword columns), `start_date`/`end_date`
(active window, `YYYYMMDD`), `reason`, and `source_artifacts`. Example row:

```python
{
    "code": "600000.SH",
    "trade_strategy": "example_swing_t",
    "amount": 2000,
    "percent": 0.03,
}
```

`at_tools.nl(ts_code, prompt="...")` is available during the decision stage
inside `main.py`, `candidate.py`, or helper modules before `trade_intents` are
returned. It starts a host-side NL Sub Agent with a point-in-time
`text_retrieve` tool and returns a result dict. The final `content` is
unconstrained; decision code should parse whatever score, label, or decision
field it needs and pass the result through `trade_intents.params`. Request,
retrieval, evidence, result, and provider-call logs are written under the
backtest result directory.

`context["model_dir"]` and `AT_MODEL_DIR` point to
`/mnt/agent/models/`. Decision code can load frozen parameters from `models`,
or train inside `main.py` from the current PIT snapshot. If the trained
parameters should be inherited by later folds, write them to `models` and run
`modification_check_tool` again after training/backtesting. If the model is
intentionally retrained every backtest, keep transient intermediate files in
memory so frozen evaluation remains deterministic.

## Trading strategies are Agent-defined functions

There are **no built-in strategy names**. Each `trade_strategy` must resolve to
a function defined in `trading.py` (or `main.py`). The template's `example_*`
functions are ordinary editable Agent code, not Environment-supported keywords.
During minute-by-minute replay the Environment calls the matched function once
per bar with a single `ctx` argument:

```python
def example_swing_t(ctx):
    amount = ctx.params["amount"]
    if ctx.cur_price is None or ctx.cur_time >= "14:57":
        return
    last_price = ctx.stock.trades[-1].price if ctx.stock.trades else ctx.cur_price
    cash_needed = ctx.cur_price * amount
    dip_triggered = ctx.cur_price < last_price * 0.95
    rally_triggered = ctx.cur_price > last_price * 1.05
    if ctx.broker.money >= cash_needed and dip_triggered:
        ctx.broker.buy(amount)
    elif ctx.stock.position > amount and rally_triggered:
        ctx.broker.sell(amount)
```

`ctx` exposes:

- `ctx.broker`: `.money`/`.cash`; `.buy(amount=None, weight=None)`,
  `.sell(amount=None)`, `.short(amount=None, weight=None)`, `.cover(amount=None)`,
  `.close()`; `.account`, `.positions`.
- `ctx.stock`: `.code`, `.price`, `.position` (signed shares), and `.trades`.
  Each trade in `.trades` exposes `.price`, `.side`, `.quantity`, and `.date`.
- `ctx.cur_price`, `ctx.cur_time` (`"HH:MM"`), `ctx.cur_date` (`"YYYYMMDD"`),
  `ctx.bar`, `ctx.params`, `ctx.account`, `ctx.positions`.

`ctx` is a pure trading replay context. It does not expose `model_dir`,
`workspace_dir`, or `nl`; use models and NL only while building
`trade_intents`, then pass the decision through `params`.

`amount` is a share count (lot-aligned to 100); `weight` is a notional fraction
of initial equity. The Broker enforces cash, short margin, T+1 sellable balance,
lot size, price limits, suspension, and shortability. Holdings count,
single-name sizing, and concentration are strategy decisions by default; implement
them in candidate selection, sizing, or trading functions when needed. The final
replay date is reserved for mandatory liquidation of any remaining holdings.
