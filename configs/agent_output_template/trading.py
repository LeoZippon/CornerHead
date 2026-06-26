"""Trade-strategy functions and the candidate-to-strategy mapping.

``main.py`` maps each candidate stock to one strategy *function name* in a
``trade_intents`` row, for example::

    {"code": "600000.SH", "trade_strategy": "example_swing_t", "amount": 2000, "percent": 0.03}

During minute-by-minute replay the Environment calls the matched function once
per bar with a single ``ctx`` argument. The function reads the bar and drives
the Broker's fundamental primitives. The Broker enforces cash, short margin,
T+1 sellable balance, lot size, price limits, suspension, and shortability.
Holdings count, single-name sizing, and concentration are strategy decisions by
default, so implement them here or in candidate selection when needed.
Strategies never write fills, cash, or positions directly.

``ctx`` attributes:

* ``ctx.broker``  — ``.money``/``.cash``; ``.buy(amount=None, weight=None)``,
  ``.sell(amount=None)``, ``.short(amount=None, weight=None)``,
  ``.cover(amount=None)``, ``.close()``; ``.account``, ``.positions``.
* ``ctx.stock``   — ``.code``, ``.price``, ``.position`` (signed shares),
  ``.trades`` (this stock's executed-trade history; each trade exposes
  ``.price``/``.side``/``.quantity``/``.date``).
* ``ctx.cur_price``, ``ctx.cur_time`` ("HH:MM"), ``ctx.cur_date`` ("YYYYMMDD").
* ``ctx.params`` — the intent row's params dict (``amount``/``price``/``percent``/...).
* ``ctx.bar`` — current minute bar dict; ``ctx.account`` and ``ctx.positions``
  are read-only account snapshots.

The replay ``ctx`` intentionally does not expose model directories, workspace,
or NL tools. Use models and ``at_tools.nl`` while building ``trade_intents`` in
the decision stage, then pass the resulting values through ``ctx.params``.
``amount`` is a share count (lot-aligned to 100); ``weight`` is a notional
fraction of initial equity. Functions are pure trading logic with no network
access or unbounded loops.
"""

from __future__ import annotations

import pandas as pd

INTENT_COLUMNS = (
    "ts_code",
    "trade_strategy",
    "params",
    "start_date",
    "end_date",
    "reason",
    "source_artifacts",
)


def build_trades(context: dict[str, object], candidates: pd.DataFrame) -> pd.DataFrame:
    """Map candidates to strategy functions.

    The template maps nothing; a strategy fills this in, e.g.::

        rows = [intent(code, "example_swing_t", amount=2000, percent=0.03) for code in codes]
        return pd.DataFrame(rows, columns=list(INTENT_COLUMNS))
    """
    return empty_trades()


def intent(
    ts_code: str,
    trade_strategy: str,
    *,
    reason: str = "",
    source_artifacts=None,
    start_date: str = "",
    end_date: str = "",
    **params: object,
) -> dict[str, object]:
    """Build one candidate-to-strategy mapping row; extra kwargs become params."""
    return {
        "ts_code": ts_code,
        "trade_strategy": trade_strategy,
        "params": dict(params),
        "start_date": start_date,
        "end_date": end_date,
        "reason": reason,
        "source_artifacts": list(source_artifacts or []),
    }


def empty_trades() -> pd.DataFrame:
    return pd.DataFrame(columns=list(INTENT_COLUMNS))


# --- Optional examples ------------------------------------------------------
#
# These names are not known by the Environment or Broker. They are ordinary
# Agent-owned Python functions, provided only as short patterns to edit,
# delete, or rename. A strategy function exists only if this file or main.py
# defines it and trade_intents references it by name.


def example_build_once(ctx) -> None:
    """Build once to a target notional weight, then hold to liquidation."""
    if ctx.stock.position == 0:
        ctx.broker.buy(weight=float(ctx.params.get("weight", 0.1)))


def example_price_dip_buy(ctx) -> None:
    """Buy a fixed share amount when the price dips to/below ``price``."""
    if ctx.stock.position == 0 and ctx.cur_price is not None and ctx.cur_price <= float(ctx.params["price"]):
        ctx.broker.buy(ctx.params.get("amount"))


def example_time_entry(ctx) -> None:
    """Buy near the close, at/after ``time`` (default 14:57)."""
    if ctx.stock.position == 0 and ctx.cur_time >= str(ctx.params.get("time", "14:57")):
        ctx.broker.buy(ctx.params.get("amount"))


def example_price_break_short(ctx) -> None:
    """Short a fixed share amount when the price rises to/above ``price``."""
    if ctx.stock.position == 0 and ctx.cur_price is not None and ctx.cur_price >= float(ctx.params["price"]):
        ctx.broker.short(ctx.params.get("amount"))


def example_swing_t(ctx) -> None:
    """Intraday swing (做T) around the last executed price.

    Assumes a base position is built first (e.g. map the stock with
    ``example_build_once`` on an earlier date, or buy on the first call). Buys
    ``amount`` shares on a dip and sells ``amount`` of the *sellable* (T+1
    eligible) balance on a rally, both before the close.
    """
    amount = ctx.params.get("amount")
    band = float(ctx.params.get("percent", 0.05))
    if ctx.cur_price is None or ctx.cur_time >= "14:57":
        return
    last = ctx.stock.trades[-1].price if ctx.stock.trades else ctx.cur_price
    if ctx.broker.money >= ctx.cur_price * float(amount or 0) and ctx.cur_price < last * (1 - band):
        ctx.broker.buy(amount)
    elif ctx.stock.position > float(amount or 0) and ctx.cur_price > last * (1 + band):
        ctx.broker.sell(amount)
