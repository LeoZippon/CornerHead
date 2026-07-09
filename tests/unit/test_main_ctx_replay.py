"""Unified per-minute main(ctx) engine: mid-replay entry via ts_code broker."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pandas as pd

from autotrade.environment.backtest_engine import BacktestError, MinuteMarketData, compute_return_stats
from autotrade.environment.broker import BrokerProfile
from autotrade.environment.executor import LocalExecutor
from autotrade.environment.main_ctx_engine import MainPolicyRunner, _day_tick_plan, run_main_ctx_replay
from autotrade.environment.sandbox import LocalSandbox


def _all_orders(broker):
    return broker.get_trade_detail_data(
        account_type="STOCK", data_type="ORDER"
    ) + broker.get_trade_detail_data(account_type="CREDIT", data_type="ORDER")


TS_CODE = "000001.SZ"

# Open a brand-new long mid-replay (day 2, pre-open), not at the fold decision time.
MAIN_PY = '''
def main(ctx):
    with ctx.substep("main_tick", budget_minutes=0.5):
        code = "000001.SZ"
        if ctx.cur_date == "20220105" and ctx.cur_time == "09:25" and ctx.broker.position(code) == 0:
            ctx.broker.buy(code, amount=1000, reason="mid_replay_entry")
'''

# Raises if the synthetic 09:30 open bar leaks end-of-day high/low/vol/amount.
LEAK_GUARD_MAIN = '''
def main(ctx):
    with ctx.substep("main_tick", budget_minutes=0.5):
        code = "000001.SZ"
        if ctx.cur_time != "09:30":
            return
        bar = ctx.bar(code) or {}
        op = bar.get("open")
        if bar.get("high") != op or bar.get("low") != op:
            raise RuntimeError("open bar leaks intraday high/low")
        if bar.get("vol") is not None or bar.get("amount") is not None:
            raise RuntimeError("open bar leaks day volume/amount")
        if ctx.broker.position(code) == 0 and ctx.price(code) is not None:
            ctx.broker.buy(code, amount=1000, reason="clean_open")
'''

# Enters at the 09:25 call-auction tick and fills at the first continuous bar.
AUCTION_MAIN = '''
def main(ctx):
    with ctx.substep("main_tick", budget_minutes=0.5):
        code = "000001.SZ"
        if ctx.cur_time == "09:25" and ctx.broker.position(code) == 0 and ctx.price(code) is not None:
            ctx.broker.buy(code, amount=1000, reason="auction_entry")
'''

# Places a fixed-price (FIX_PRICE) limit buy; the engine rests it until a bar's
# low reaches the limit, then fills at the limit price (no maker slippage).
LIMIT_FILL_MAIN = '''
def main(ctx):
    with ctx.substep("main_tick", budget_minutes=0.5):
        code = "000001.SZ"
        if ctx.cur_time == "09:30" and ctx.broker.position(code) == 0 and not ctx.broker.pending(code):
            ctx.broker.buy(code, amount=1000, limit=9.80, reason="limit_entry")
'''

# Limit price the market never reaches -> day-end auto-cancelled.
LIMIT_CANCEL_MAIN = '''
def main(ctx):
    with ctx.substep("main_tick", budget_minutes=0.5):
        code = "000001.SZ"
        if ctx.cur_time == "09:30" and ctx.broker.position(code) == 0 and not ctx.broker.pending(code):
            ctx.broker.buy(code, amount=1000, limit=7.00, reason="limit_miss")
'''

# Two buys in one tick; records the agent-visible cash/buying-power before and after
# each so the test can confirm substep actions do NOT project the account view intra-tick.
PARITY_MAIN = '''
import json, os
def main(ctx):
    with ctx.substep("main_tick", budget_minutes=0.5):
        c1, c2 = "000001.SZ", "000002.SZ"
        sd = ctx.state_dir
        if os.path.exists(os.path.join(sd, "obs.json")):
            return
        p1, p2 = ctx.price(c1), ctx.price(c2)
        if p1 is None or p2 is None or ctx.broker.position(c1) != 0:
            return
        obs = {"cash0": ctx.broker.stock["cash"], "avail0": ctx.broker.stock["available_cash"], "p1": p1, "p2": p2}
        ctx.broker.buy(c1, amount=1000, reason="buy1")
        obs["cash1"] = ctx.broker.stock["cash"]
        obs["avail1"] = ctx.broker.stock["available_cash"]
        obs["pos1"] = ctx.broker.position(c1)
        shares2 = int((float(ctx.broker.stock["available_cash"]) * 0.1) / p2 // 100 * 100)
        ctx.broker.buy(c2, amount=shares2, reason="buy2")
        obs["pos2"] = ctx.broker.position(c2)
        os.makedirs(sd, exist_ok=True)
        with open(os.path.join(sd, "obs.json"), "w") as handle:
            json.dump(obs, handle)
'''

PENDING_RESERVES_CASH_MAIN = '''
import json, os
def main(ctx):
    with ctx.substep("main_tick", budget_minutes=0.5):
        code = "000001.SZ"
        sd = ctx.state_dir
        if ctx.cur_time == "09:30" and not ctx.broker.pending(code):
            ctx.broker.buy(code, amount=10000, reason="reserve_cash")
        if ctx.cur_time == "09:31" and not os.path.exists(os.path.join(sd, "reserve.json")):
            obs = {
                "cash": ctx.broker.stock["cash"],
                "available_cash": ctx.broker.stock["available_cash"],
                "pending": len(ctx.broker.pending(code)),
                "position": ctx.broker.position(code),
            }
            os.makedirs(sd, exist_ok=True)
            with open(os.path.join(sd, "reserve.json"), "w") as handle:
                json.dump(obs, handle)
'''

# Opens a short and records buying power before/after so the test can confirm the
# delayed-submit short does NOT project available_cash inside the substep.
SHORT_PARITY_MAIN = '''
import json, os
def main(ctx):
    with ctx.substep("main_tick", budget_minutes=0.5):
        c = "000002.SZ"
        sd = ctx.state_dir
        if os.path.exists(os.path.join(sd, "sobs.json")):
            return
        p = ctx.price(c)
        if p is None or ctx.broker.position(c) != 0:
            return
        obs = {"avail0": ctx.broker.credit["available_cash"], "cash0": ctx.broker.credit["cash"], "p": p}
        # 融券卖出 must be a limit order; 19.8 == the activation bar's reference
        # price in this fixture, so the uptick rule passes and the order fills.
        ctx.broker.short(c, amount=1000, limit=19.8, reason="short1")
        obs["avail1"] = ctx.broker.credit["available_cash"]
        obs["cash1"] = ctx.broker.credit["cash"]
        obs["pos1"] = ctx.broker.position(c)
        os.makedirs(sd, exist_ok=True)
        with open(os.path.join(sd, "sobs.json"), "w") as handle:
            json.dump(obs, handle)
'''

# Shorts a code then calls sell() on it (wrong action for a short); neither is projected
# intra-tick — the host Broker accepts the short and rejects the side-mismatched sell.
SIDE_MISMATCH_MAIN = '''
import json, os
def main(ctx):
    with ctx.substep("main_tick", budget_minutes=0.5):
        c = "000002.SZ"
        sd = ctx.state_dir
        if os.path.exists(os.path.join(sd, "mobs.json")):
            return
        if ctx.price(c) is None or ctx.broker.position(c) != 0:
            return
        ctx.broker.short(c, amount=1000, limit=19.8, reason="short")
        pos_after_short = ctx.broker.position(c)
        avail_after_short = ctx.broker.credit["available_cash"]
        ctx.broker.credit_sell(c, amount=1000, reason="wrong_sell_on_short")  # side mismatch
        obs = {
            "pos_after_short": pos_after_short, "avail_after_short": avail_after_short,
            "pos_after_sell": ctx.broker.position(c), "avail_after_sell": ctx.broker.credit["available_cash"],
        }
        os.makedirs(sd, exist_ok=True)
        with open(os.path.join(sd, "mobs.json"), "w") as handle:
            json.dump(obs, handle)
'''

# Limit price the market never reaches, but its validity extends to the close.
LIMIT_DAY_END_CANCEL_MAIN = '''
def main(ctx):
    with ctx.substep("main_tick", budget_minutes=0.5):
        code = "000001.SZ"
        if ctx.cur_time == "09:30" and ctx.broker.position(code) == 0 and not ctx.broker.pending(code):
            ctx.broker.buy(code, amount=1000, limit=7.00, reason="limit_day_end_miss")
'''

# Re-evaluates every continuous tick but skips codes with a working order, so the
# multi-bar execution lag does not produce duplicate entries (live order-query parity).
PENDING_DEDUP_MAIN = '''
def main(ctx):
    with ctx.substep("main_tick", budget_minutes=0.5):
        code = "000001.SZ"
        if ctx.cur_time < "09:30" or ctx.price(code) is None:
            return  # continuous session only
        if ctx.broker.position(code) == 0 and not ctx.broker.pending(code):
            ctx.broker.buy(code, amount=1000, reason="dedup_entry")
'''

# Cancels every pending order older than one minute. With execution_lag_bars=3 the
# 09:30 order is still in the submit-lag queue at 09:32, so cancel prevents activation.
STALE_QUEUED_CANCEL_MAIN = '''
def main(ctx):
    with ctx.substep("main_tick", budget_minutes=0.5):
        code = "000001.SZ"
        if ctx.cur_time == "09:30" and ctx.price(code) is not None and not ctx.broker.pending(code):
            ctx.broker.buy(code, amount=1000, reason="queued_cancel_target")
        for order in ctx.broker.pending():
            if float(order.get("age_minutes") or 0.0) > 1.0:
                ctx.broker.cancel(order["order_id"], reason="stale_gt_1m")
'''

# Cancels a limit order after it has entered the Broker's working book and missed a bar.
WORKING_CANCEL_MAIN = '''
def main(ctx):
    with ctx.substep("main_tick", budget_minutes=0.5):
        code = "000001.SZ"
        if ctx.cur_time == "09:30" and ctx.price(code) is not None and not ctx.broker.pending(code):
            ctx.broker.buy(code, amount=1000, limit=9.50, reason="working_cancel_target")
        for order in ctx.broker.pending():
            if order.get("status") == "working":
                ctx.broker.cancel(order["order_id"], reason="working_cancel")
'''

# The same-tick pending view must expose the documented fields, not the raw action dict.
SAME_TICK_PENDING_FIELDS_MAIN = '''
def main(ctx):
    with ctx.substep("main_tick", budget_minutes=0.5):
        code = "000001.SZ"
        if ctx.cur_time == "09:30" and ctx.price(code) is not None and not ctx.broker.pending(code):
            oid = ctx.broker.buy(code, amount=1000, reason="same_tick_pending_fields")
            pending = ctx.broker.pending()
            assert len(pending) == 1, pending
            order = pending[0]
            assert order["order_id"] == oid, order
            assert order["status"] == "pending", order
            assert order["age_minutes"] == 0.0, order
            assert order["account"] == "stock", order
            assert order["op_type"] == 23, order
            assert "_substep" not in order, order
'''

INVALID_AMOUNT_MAIN = '''
def main(ctx):
    with ctx.substep("main_tick", budget_minutes=0.5):
        code = "000001.SZ"
        if ctx.cur_time == "09:25" and ctx.broker.position(code) == 0:
            ctx.broker.buy(code, amount=100.5, reason="fractional_amount")
'''

NON_POSITIVE_LIMIT_MAIN = '''
def main(ctx):
    with ctx.substep("main_tick", budget_minutes=0.5):
        code = "000001.SZ"
        if ctx.cur_time == "09:25" and ctx.broker.position(code) == 0:
            ctx.broker.buy(code, amount=1000, limit=0, reason="bad_limit")
'''

SHORT_MISSING_LIMIT_MAIN = '''
def main(ctx):
    with ctx.substep("main_tick", budget_minutes=0.5):
        code = "000002.SZ"
        if ctx.cur_time == "09:25" and ctx.broker.position(code) == 0:
            ctx.broker.short(code, amount=1000, reason="missing_limit")
'''

# Submits and immediately cancels; no net main_actions order should remain for audit.
BUY_THEN_CANCEL_SAME_TICK_MAIN = '''
def main(ctx):
    with ctx.substep("main_tick", budget_minutes=0.5):
        code = "000001.SZ"
        if ctx.cur_time == "09:30" and ctx.price(code) is not None and not ctx.broker.pending(code):
            oid = ctx.broker.buy(code, amount=1000, reason="cancel_same_tick")
            ctx.broker.cancel(oid, reason="cancel_same_tick")
'''

# A long-valid limit missed the final real bar. Post-close off-session hygiene must
# not override the day-end auto-cancel reason.
POSTCLOSE_STALE_CANCEL_MAIN = '''
def main(ctx):
    with ctx.substep("main_tick", budget_minutes=0.5):
        code = "000001.SZ"
        if ctx.cur_time == "09:30" and ctx.price(code) is not None and not ctx.broker.pending(code):
            ctx.broker.buy(code, amount=1000, limit=7.00, reason="postclose_target")
        if ctx.cur_time > "09:34":
            for order in ctx.broker.pending():
                if order.get("order_id"):
                    ctx.broker.cancel(order["order_id"], reason="postclose_stale_cancel")
'''

# Submits at 09:15 with no matched price yet (ctx.price is None); fills at the open.
PREOPEN_MAIN = '''
def main(ctx):
    with ctx.substep("main_tick", budget_minutes=0.5):
        code = "000001.SZ"
        if ctx.cur_time == "09:15" and ctx.broker.position(code) == 0 and ctx.price(code) is None:
            ctx.broker.buy(code, amount=1000, reason="preopen_blind")
'''

# Asserts the per-tick Timeview daily view at 20220105 09:15 holds the frozen
# snapshot history + the prior replay day (visible once that night's evening node
# completed ~02:05) but never today or the future; raises (failing the run) on a
# leak. The agent reads the domain as a directory of parquet parts.
ASOF_GUARD_MAIN = '''
from pathlib import Path

import pandas as pd


def main(ctx):
    with ctx.substep("main_tick", budget_minutes=0.5):
        code = "000001.SZ"
        if ctx.cur_date != "20220105" or ctx.cur_time != "09:15":
            return
        dates = set(pd.read_parquet(Path(str(ctx.asof_dir)) / "daily")["trade_date"].astype(str))
        assert "20211230" in dates, dates           # frozen snapshot history
        assert "20220104" in dates, dates           # prior replay day (evening node done)
        assert "20220105" not in dates, "today leaked"
        assert "20220331" not in dates, "future leaked"
        assert ctx.asof_version, "asof_version not exposed"
        if ctx.broker.position(code) == 0:
            ctx.broker.buy(code, amount=1000, reason="asof_ok")
'''

# Declares a screening sub-step (budget_minutes=3): broker actions inside the block
# are submitted only once ready_at is reached, then they use the normal bar lag.
SUBSTEP_LATENCY_MAIN = '''
def main(ctx):
    code = "000001.SZ"
    if ctx.cur_time == "09:30" and ctx.broker.position(code) == 0 and not ctx.broker.pending(code):
        with ctx.substep("screen", budget_minutes=3):
            ctx.broker.buy(code, amount=1000, reason="slow_entry")
'''

# A sub-step that declares a small positive budget (0.001 min = 0.06s) but really
# sleeps past it: the real wall-time exceeds the declared budget, so the replay
# aborts (fail-fast, non-exploitable).
SUBSTEP_OVERRUN_MAIN = '''
import time

def main(ctx):
    code = "000001.SZ"
    if ctx.cur_time == "09:30":
        with ctx.substep("slow", budget_minutes=0.001):
            time.sleep(0.3)
            ctx.broker.buy(code, amount=1000, reason="overrun")
'''

# Wrapping a block with budget_minutes=0 is identical to not wrapping (no delay,
# no ceiling), so ctx.substep rejects it: the agent must declare a positive budget
# or leave the block unwrapped.
SUBSTEP_ZERO_BUDGET_MAIN = '''
def main(ctx):
    code = "000001.SZ"
    if ctx.cur_time == "09:30":
        with ctx.substep("light", budget_minutes=0):
            ctx.broker.buy(code, amount=1000, reason="zero_budget")
'''

BROKER_OUTSIDE_SUBSTEP_MAIN = '''
def main(ctx):
    code = "000001.SZ"
    if ctx.cur_time == "09:30" and ctx.price(code) is not None:
        ctx.broker.buy(code, amount=1000, reason="outside_substep")
'''

STATE_OUTSIDE_SUBSTEP_MAIN = '''
def main(ctx):
    if ctx.cur_time == "09:30":
        _ = ctx.state_dir
'''

STATE_ENV_BYPASS_MAIN = '''
import os

def main(ctx):
    if ctx.cur_time == "09:30":
        _ = os.environ["AT_STATE_DIR"]
'''

IMPORT_NL_MAIN = '''
from at_tools import nl

nl("000001.SZ", prompt="import-time call")

def main(ctx):
    return
'''

UNTRACKED_HEAVY_MAIN = '''
import time

def main(ctx):
    if ctx.cur_time == "09:30":
        time.sleep(0.1)
'''

# A small positive budget submits at the first tick at/after ready_at, then fills
# at the normal execution_lag_bars from that submit tick.
SUBSTEP_LIGHT_BUDGET_MAIN = '''
def main(ctx):
    code = "000001.SZ"
    if ctx.cur_time == "09:30" and ctx.broker.position(code) == 0 and not ctx.broker.pending(code):
        with ctx.substep("light", budget_minutes=1):
            ctx.broker.buy(code, amount=1000, reason="light_entry")
'''

SUBSTEP_HALF_BUDGET_MAIN = '''
def main(ctx):
    code = "000001.SZ"
    if ctx.cur_time == "09:30" and ctx.broker.position(code) == 0 and not ctx.broker.pending(code):
        with ctx.substep("light", budget_minutes=0.5):
            ctx.broker.buy(code, amount=1000, reason="half_minute_entry")
'''

NOOP_MAIN = '''
def main(ctx):
    return
'''

# A positive substep budget on a 09:25 auction decision misses the accepted
# open-auction submission window; it is not auto-scheduled into 09:30.
SUBSTEP_AUCTION_SMALL_MAIN = '''
def main(ctx):
    code = "000001.SZ"
    if ctx.cur_time == "09:25" and ctx.broker.position(code) == 0 and not ctx.broker.pending(code):
        with ctx.substep("screen", budget_minutes=1):
            ctx.broker.buy(code, amount=1000, reason="auction_small_budget")
'''

SUBSTEP_AUCTION_LARGE_MAIN = '''
def main(ctx):
    code = "000001.SZ"
    if ctx.cur_time == "09:25" and ctx.broker.position(code) == 0 and not ctx.broker.pending(code):
        with ctx.substep("screen", budget_minutes=4):
            ctx.broker.buy(code, amount=1000, reason="auction_large_budget")
'''

SUBSTEP_PENDING_DELAY_MAIN = '''
def main(ctx):
    code = "000001.SZ"
    if ctx.cur_time == "09:30" and not ctx.broker.pending(code):
        with ctx.substep("screen", budget_minutes=3):
            ctx.broker.buy(code, amount=1000, reason="delayed_pending")
    if ctx.cur_time == "09:31":
        pending = ctx.broker.pending(code)
        assert pending == [], pending
'''

SUBSTEP_SAME_TICK_PENDING_MAIN = '''
def main(ctx):
    code = "000001.SZ"
    if ctx.cur_time == "09:30" and not ctx.broker.pending(code):
        cash_before = ctx.broker.stock["cash"]
        pos_before = ctx.broker.position(code)
        with ctx.substep("screen", budget_minutes=3):
            oid = ctx.broker.buy(code, amount=1000, reason="same_tick_delayed_pending")
            pending = ctx.broker.pending(code)
            assert pending == [], (oid, pending)
            assert ctx.broker.stock["cash"] == cash_before, (ctx.broker.stock["cash"], cash_before)
            assert ctx.broker.position(code) == pos_before, (ctx.broker.position(code), pos_before)
        pending = ctx.broker.pending(code)
        assert pending == [], pending
'''

SUBSTEP_CANCEL_DELAYED_MAIN = '''
def main(ctx):
    code = "000001.SZ"
    if ctx.cur_time == "09:30" and not ctx.broker.pending(code):
        with ctx.substep("screen", budget_minutes=3):
            ctx.broker.buy(code, amount=1000, reason="cancel_delayed_target")
    if ctx.cur_time == "09:31":
        pending = ctx.broker.pending(code)
        assert pending == [], pending
'''

SUBSTEP_LATE_NO_FILL_MAIN = '''
def main(ctx):
    code = "000001.SZ"
    if ctx.cur_time == "14:59" and not ctx.broker.pending(code):
        with ctx.substep("late", budget_minutes=1):
            ctx.broker.buy(code, amount=1000, reason="late_delayed_no_fill")
'''

SUBSTEP_DUPLICATE_NAME_MAIN = '''
def main(ctx):
    code = "000001.SZ"
    if ctx.cur_time == "09:30":
        with ctx.substep("screen", budget_minutes=1):
            ctx.broker.buy(code, amount=1000, reason="first")
        with ctx.substep("screen", budget_minutes=3):
            ctx.broker.buy(code, amount=1000, reason="second")
'''

REPLAY_ROWS = [
    ("20220104", 10.0, 10.2),
    ("20220105", 10.3, 11.0),
    ("20220331", 12.0, 12.5),
]


def _limit_up_open_replay() -> pd.DataFrame:
    # Day 1 opens exactly at the upper limit (one-sided limit-up auction).
    return pd.DataFrame(
        [
            {"trade_date": "20220104", "ts_code": TS_CODE, "open": 13.0, "high": 13.0, "low": 13.0,
             "close": 13.0, "up_limit": 13.0, "down_limit": 8.0, "is_suspended": False},
            {"trade_date": "20220331", "ts_code": TS_CODE, "open": 12.0, "high": 13.0, "low": 11.0,
             "close": 12.5, "up_limit": 14.0, "down_limit": 9.0, "is_suspended": False},
        ]
    )


def _ohlc_replay() -> pd.DataFrame:
    # Distinct high/low/vol so any open-bar look-ahead is unambiguous.
    return pd.DataFrame(
        [
            {"trade_date": "20220104", "ts_code": TS_CODE, "open": 10.0, "high": 12.0, "low": 9.0,
             "close": 11.0, "vol": 5000.0, "amount": 55000.0, "up_limit": 13.0, "down_limit": 8.0, "is_suspended": False},
            {"trade_date": "20220331", "ts_code": TS_CODE, "open": 11.0, "high": 13.0, "low": 10.5,
             "close": 12.0, "vol": 6000.0, "amount": 72000.0, "up_limit": 14.0, "down_limit": 9.0, "is_suspended": False},
        ]
    )


def _dense_minutes() -> pd.DataFrame:
    # Several continuous bars so a decision has bars both before and after its
    # execution_lag_bars=2 fill bar (used to exercise the in-flight order window).
    return pd.DataFrame(
        [
            {"trade_date": "20220104", "ts_code": TS_CODE, "trade_time": t, "open": 10.0, "high": 10.1, "low": 9.95, "close": 10.05}
            for t in ("09:30", "09:31", "09:32", "09:33", "14:57")
        ]
    )


def _substep_delay_minutes() -> pd.DataFrame:
    # Enough continuous bars to observe ready_at submission plus execution_lag_bars
    # without falling through to the synthetic 15:00 close fallback.
    return pd.DataFrame(
        [
            {"trade_date": "20220104", "ts_code": TS_CODE, "trade_time": t, "open": 10.0, "high": 10.1, "low": 9.95, "close": 10.05}
            for t in ("09:30", "09:31", "09:32", "09:33", "09:34", "09:35", "09:36", "14:57")
        ]
    )


def _late_no_fill_minutes() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"trade_date": "20220104", "ts_code": TS_CODE, "trade_time": "14:59", "open": 10.0, "high": 10.1, "low": 9.95, "close": 10.05},
            {"trade_date": "20220104", "ts_code": TS_CODE, "trade_time": "15:00", "open": 10.1, "high": 10.2, "low": 10.0, "close": 10.15},
        ]
    )


def _limit_minutes() -> pd.DataFrame:
    # 09:33 dips to 9.78 (reaches a 9.80 limit) but never to 9.50.
    rows = [
        ("09:30", 10.00, 10.05, 9.98, 10.02),
        ("09:31", 10.02, 10.06, 9.99, 10.03),
        ("09:32", 10.03, 10.05, 9.95, 10.00),
        ("09:33", 10.00, 10.02, 9.78, 9.85),
        ("09:34", 9.85, 9.90, 9.80, 9.88),
    ]
    return pd.DataFrame(
        [
            {"trade_date": "20220104", "ts_code": TS_CODE, "trade_time": t, "open": o, "high": h, "low": low, "close": c}
            for t, o, h, low, c in rows
        ]
    )


def _auction_minutes() -> pd.DataFrame:
    # Day 1 has a 09:30 opening-auction print and a 09:31 first-continuous bar, so
    # next-bar execution fills a 09:15 order at 09:30 and a 09:25 order at 09:31.
    return pd.DataFrame(
        [
            {"trade_date": "20220104", "ts_code": TS_CODE, "trade_time": "09:30", "open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0},
            {"trade_date": "20220104", "ts_code": TS_CODE, "trade_time": "09:31", "open": 10.1, "high": 10.3, "low": 10.0, "close": 10.2},
            {"trade_date": "20220104", "ts_code": TS_CODE, "trade_time": "14:57", "open": 10.5, "high": 10.6, "low": 10.4, "close": 10.5},
        ]
    )


def _replay_daily() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "trade_date": d,
                "ts_code": TS_CODE,
                "open": o,
                "close": c,
                "up_limit": o * 1.2,
                "down_limit": o * 0.8,
                "is_suspended": False,
            }
            for d, o, c in REPLAY_ROWS
        ]
    )


class MainCtxReplayTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.sandbox = LocalSandbox(Path(self._tmp.name) / "run")
        self.sandbox.prepare_layout()
        self.sandbox.paths.agent_output.mkdir(parents=True, exist_ok=True)
        (self.sandbox.paths.agent_output / "main.py").write_text(MAIN_PY, encoding="utf-8")
        self.executor = LocalExecutor(self.sandbox.paths)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self) -> object:
        profile = BrokerProfile()
        with MainPolicyRunner(
            self.executor,
            self.sandbox.paths,
            timeout_seconds=30.0,
            decision_time="2022-01-04T09:30:00+08:00",
            replay_granularity="daily",
        ) as policy:
            policy.validate_main()
            return run_main_ctx_replay(
                _replay_daily(),
                profile,
                shortable_codes=frozenset(),
                main_policy=policy,
            )

    def test_opens_new_position_mid_replay(self) -> None:
        result = self._run()
        orders = _all_orders(result.broker)
        buys = [o for o in orders if o["action"] == "buy" and o["status"] == "filled"]
        self.assertEqual(len(buys), 1, orders)
        # The entry happens on day 2, proving a position can open after the decision time.
        self.assertEqual(buys[0]["ts_code"], TS_CODE)
        self.assertEqual(buys[0]["trade_date"], "20220105")
        self.assertGreater(buys[0]["filled_quantity"], 0)

    def test_open_bar_has_no_intraday_lookahead(self) -> None:
        # Daily-only replay: the synthetic 09:30 bar must show open==high==low and
        # no vol/amount; LEAK_GUARD_MAIN raises (failing the backtest) otherwise.
        (self.sandbox.paths.agent_output / "main.py").write_text(LEAK_GUARD_MAIN, encoding="utf-8")
        with MainPolicyRunner(
            self.executor,
            self.sandbox.paths,
            timeout_seconds=30.0,
            decision_time="2022-01-04T09:30:00+08:00",
            replay_granularity="daily",
        ) as policy:
            policy.validate_main()
            result = run_main_ctx_replay(
                _ohlc_replay(),
                BrokerProfile(),
                shortable_codes=frozenset(),
                main_policy=policy,
                execution_lag_bars=1,  # this test exercises the open-bar synthesis, not the lag
            )
        buys = [o for o in _all_orders(result.broker) if o["action"] == "buy" and o["status"] == "filled"]
        self.assertEqual(len(buys), 1)

    def test_substep_broker_actions_do_not_project_intra_tick(self) -> None:
        # Substep broker actions are delayed-submit plans. Inside the substep the
        # account/position view does not project them; the host Broker applies the
        # real cash and position constraints when the action is submitted/fills.
        import json

        (self.sandbox.paths.agent_output / "main.py").write_text(PARITY_MAIN, encoding="utf-8")
        replay = pd.DataFrame(
            [
                {"trade_date": d, "ts_code": code, "open": o, "close": c,
                 "up_limit": o * 1.2, "down_limit": o * 0.8, "is_suspended": False}
                for d, code, o, c in [
                    ("20220104", "000001.SZ", 10.0, 9.9), ("20220105", "000001.SZ", 9.9, 9.8),
                    ("20220104", "000002.SZ", 20.0, 19.8), ("20220105", "000002.SZ", 19.8, 19.6),
                ]
            ]
        )
        profile = BrokerProfile()
        with MainPolicyRunner(
            self.executor, self.sandbox.paths, timeout_seconds=30.0,
            decision_time="2022-01-04T09:30:00+08:00", replay_granularity="daily",
        ) as policy:
            policy.validate_main()
            result = run_main_ctx_replay(
                replay, profile, shortable_codes=frozenset(), main_policy=policy,
                auction_enabled=False, offsession_tick_minutes=0,
            )
        obs = json.loads((self.sandbox.paths.workspace / ".state" / "obs.json").read_text(encoding="utf-8"))
        self.assertEqual(obs["cash1"], obs["cash0"])
        self.assertEqual(obs["avail1"], obs["avail0"])
        self.assertEqual(obs["pos1"], 0)
        self.assertEqual(obs["pos2"], 0)
        # Both delayed buys are still submitted on this light substep and filled by the broker.
        filled = {
            o["ts_code"]
            for o in _all_orders(result.broker)
            if o["action"] == "buy" and o["status"] == "filled"
        }
        self.assertEqual(filled, {"000001.SZ", "000002.SZ"})

    def test_submitted_pending_order_reserves_available_cash_on_next_tick(self) -> None:
        import json

        (self.sandbox.paths.agent_output / "main.py").write_text(PENDING_RESERVES_CASH_MAIN, encoding="utf-8")
        result = self._run_with(_ohlc_replay(), _dense_minutes(), execution_lag_bars=2)
        obs = json.loads((self.sandbox.paths.workspace / ".state" / "reserve.json").read_text(encoding="utf-8"))
        self.assertEqual(obs["cash"], 500_000.0)
        self.assertLess(obs["available_cash"], 400_000.0)
        self.assertEqual(obs["pending"], 1)
        self.assertEqual(obs["position"], 0)
        filled = [o for o in _all_orders(result.broker) if o["action"] == "buy" and o["status"] == "filled"]
        self.assertEqual(len(filled), 1)

    def test_substep_short_open_does_not_project_available_cash(self) -> None:
        # Substep short opens are delayed-submit plans; no cash, available_cash, or
        # position projection is visible inside the substep.
        import json

        (self.sandbox.paths.agent_output / "main.py").write_text(SHORT_PARITY_MAIN, encoding="utf-8")
        replay = pd.DataFrame(
            [
                {"trade_date": d, "ts_code": code, "open": o, "close": c,
                 "up_limit": o * 1.2, "down_limit": o * 0.8, "is_suspended": False}
                for d, code, o, c in [
                    ("20220104", "000002.SZ", 20.0, 19.8), ("20220105", "000002.SZ", 19.8, 19.6),
                ]
            ]
        )
        profile = BrokerProfile()
        with MainPolicyRunner(
            self.executor, self.sandbox.paths, timeout_seconds=30.0,
            decision_time="2022-01-04T09:30:00+08:00", replay_granularity="daily",
        ) as policy:
            policy.validate_main()
            result = run_main_ctx_replay(
                replay, profile, shortable_codes=frozenset({"000002.SZ"}), main_policy=policy,
                auction_enabled=False, offsession_tick_minutes=0,
            )
        obs = json.loads((self.sandbox.paths.workspace / ".state" / "sobs.json").read_text(encoding="utf-8"))
        self.assertEqual(obs["pos1"], 0)
        self.assertEqual(obs["avail1"], obs["avail0"])
        self.assertEqual(obs["cash1"], obs["cash0"])
        shorts = [o for o in _all_orders(result.broker) if o["action"] == "short" and o["status"] == "filled"]
        self.assertEqual(len(shorts), 1)

    def test_projection_rejects_side_mismatched_reduce(self) -> None:
        # Calling sell() after a same-substep short does not see a projected short;
        # both actions are submitted to the host Broker, which accepts the short and
        # rejects the side-mismatched sell.
        import json

        (self.sandbox.paths.agent_output / "main.py").write_text(SIDE_MISMATCH_MAIN, encoding="utf-8")
        replay = pd.DataFrame(
            [
                {"trade_date": d, "ts_code": "000002.SZ", "open": o, "close": c,
                 "up_limit": o * 1.2, "down_limit": o * 0.8, "is_suspended": False}
                for d, o, c in [("20220104", 20.0, 19.8), ("20220105", 19.8, 19.6)]
            ]
        )
        with MainPolicyRunner(
            self.executor, self.sandbox.paths, timeout_seconds=30.0,
            decision_time="2022-01-04T09:30:00+08:00", replay_granularity="daily",
        ) as policy:
            policy.validate_main()
            run_main_ctx_replay(
                replay, BrokerProfile(),
                shortable_codes=frozenset({"000002.SZ"}), main_policy=policy,
                auction_enabled=False, offsession_tick_minutes=0,
            )
        obs = json.loads((self.sandbox.paths.workspace / ".state" / "mobs.json").read_text(encoding="utf-8"))
        self.assertEqual(obs["pos_after_short"], 0)
        self.assertEqual(obs["pos_after_sell"], 0)
        self.assertAlmostEqual(obs["avail_after_sell"], obs["avail_after_short"])

    def _run_with(self, replay: pd.DataFrame, minutes: pd.DataFrame | None = None, **replay_kwargs) -> object:
        with MainPolicyRunner(
            self.executor, self.sandbox.paths, timeout_seconds=30.0,
            decision_time="2022-01-04T09:25:00+08:00", replay_granularity="daily",
        ) as policy:
            policy.validate_main()
            return run_main_ctx_replay(
                replay, BrokerProfile(),
                shortable_codes=frozenset(), main_policy=policy,
                replay_intraday_1min=minutes,
                **replay_kwargs,
            )

    def test_auction_entry_fills_at_first_continuous_bar(self) -> None:
        # A 09:25 order is decided on the matched open but, under next-bar
        # execution, fills at the 09:31 open (the first continuous bar) — never
        # within the bar it was decided on.
        (self.sandbox.paths.agent_output / "main.py").write_text(AUCTION_MAIN, encoding="utf-8")
        result = self._run_with(_ohlc_replay(), _auction_minutes())
        buys = [o for o in _all_orders(result.broker) if o["action"] == "buy" and o["status"] == "filled"]
        self.assertEqual(len(buys), 1)
        # The 09:25 decision fills at the first CONTINUOUS bar (09:31), so it is a taker
        # fill: continuous price label and slippage apply (only the open/close call
        # auctions are slippage-free).
        self.assertEqual(buys[0]["price_label"], "minute:09:31")
        self.assertEqual(buys[0]["trade_date"], "20220104")
        self.assertAlmostEqual(buys[0]["price"], BrokerProfile().slipped_price(10.1, is_buy=True))

    def test_substep_state_write_is_delayed_then_merged(self) -> None:
        # A plan written to ctx.state_dir inside a sub-step (B=2 min) is NOT visible
        # at the generating tick; it surfaces in the visible state dir only once
        # ready_at = tick + B has elapsed, and the audit records the merge.
        stage_main = '''
from pathlib import Path

_SEEN = {}


def main(ctx):
    code = "000001.SZ"
    if ctx.cur_date == "20220104" and ctx.cur_time == "09:25":
        with ctx.substep("screen", budget_minutes=2):
            (Path(str(ctx.state_dir)) / "plan.txt").write_text("go")   # staged, not yet visible
    with ctx.substep("manage", budget_minutes=0.5):
        visible = Path(str(ctx.state_dir)) / "plan.txt"
        if visible.exists() and "buy" not in _SEEN:
            _SEEN["buy"] = ctx.cur_datetime
            ctx.broker.buy(code, amount=1000, reason="plan_visible")
'''
        (self.sandbox.paths.agent_output / "main.py").write_text(stage_main, encoding="utf-8")
        replay = pd.DataFrame(
            [
                {"trade_date": "20220104", "ts_code": TS_CODE, "open": 10.0, "close": 10.2,
                 "up_limit": 12.0, "down_limit": 8.0, "is_suspended": False},
                {"trade_date": "20220105", "ts_code": TS_CODE, "open": 10.3, "close": 11.0,
                 "up_limit": 12.0, "down_limit": 8.0, "is_suspended": False},
                {"trade_date": "20220331", "ts_code": TS_CODE, "open": 12.0, "close": 12.5,
                 "up_limit": 14.0, "down_limit": 9.0, "is_suspended": False},
            ]
        )
        with MainPolicyRunner(
            self.executor, self.sandbox.paths, timeout_seconds=30.0,
            decision_time="2022-01-04T09:25:00+08:00", replay_granularity="daily",
        ) as policy:
            policy.validate_main()
            result = run_main_ctx_replay(
                replay, BrokerProfile(),
                shortable_codes=frozenset(), main_policy=policy,
            )
        # main(ctx) asserted the staged write was hidden at 09:25, then bought once it
        # merged; one staged write is recorded and merged.
        buys = [o for o in _all_orders(result.broker) if o["action"] == "buy" and o["status"] == "filled"]
        self.assertEqual(len(buys), 1)
        audit = result.state_staging_audit or []
        self.assertEqual(len(audit), 1)
        self.assertTrue(audit[0]["merged"])
        self.assertEqual(audit[0]["substep"], "screen")

    def test_substep_read_sees_old_visible_state(self) -> None:
        # R10: inside a sub-step, reading ctx.state_dir must return the OLD visible
        # value, not the empty staging dir. The strategy writes "seed" to the visible
        # state dir at 09:15 (outside a sub-step), then at 09:25 reads it from inside
        # a sub-step (must be "seed") and stages "update" (must NOT leak to visible this
        # tick). It buys only when both hold; without the fix the in-substep read
        # raises FileNotFoundError and the replay aborts.
        stage_main = '''
from pathlib import Path


def main(ctx):
    if ctx.cur_date == "20220104" and ctx.cur_time == "09:15":
        with ctx.substep("seed", budget_minutes=0.5):
            Path(str(ctx.state_dir), "state.txt").write_text("seed")
    if ctx.cur_date == "20220104" and ctx.cur_time == "09:25":
        with ctx.substep("read_old", budget_minutes=2):
            staged = Path(str(ctx.state_dir)) / "state.txt"   # staging dir, seeded from visible
            old = staged.read_text()
            staged.write_text("update")   # staged, delayed
        if old == "seed":
            with ctx.substep("trade", budget_minutes=0.5):
                ctx.broker.buy("000001.SZ", amount=1000, reason="read_old_ok")
'''
        (self.sandbox.paths.agent_output / "main.py").write_text(stage_main, encoding="utf-8")
        replay = pd.DataFrame(
            [
                {"trade_date": "20220104", "ts_code": TS_CODE, "open": 10.0, "close": 10.2,
                 "up_limit": 12.0, "down_limit": 8.0, "is_suspended": False},
                {"trade_date": "20220105", "ts_code": TS_CODE, "open": 10.3, "close": 11.0,
                 "up_limit": 12.0, "down_limit": 8.0, "is_suspended": False},
                {"trade_date": "20220331", "ts_code": TS_CODE, "open": 12.0, "close": 12.5,
                 "up_limit": 14.0, "down_limit": 9.0, "is_suspended": False},
            ]
        )
        with MainPolicyRunner(
            self.executor, self.sandbox.paths, timeout_seconds=30.0,
            decision_time="2022-01-04T09:25:00+08:00", replay_granularity="daily",
        ) as policy:
            policy.validate_main()
            result = run_main_ctx_replay(
                replay, BrokerProfile(),
                shortable_codes=frozenset(), main_policy=policy,
            )
        buys = [o for o in _all_orders(result.broker) if o["action"] == "buy" and o["status"] == "filled"]
        self.assertEqual(len(buys), 1)
        staged = [a for a in (result.state_staging_audit or []) if a["substep"] == "read_old"]
        self.assertEqual(len(staged), 1)   # only the changed "update", not the seeded copy
        self.assertTrue(staged[0]["merged"])

    def test_rolling_asof_view_excludes_today_and_future(self) -> None:
        snap = self.sandbox.paths.snapshot
        snap.mkdir(parents=True, exist_ok=True)
        pd.DataFrame([{"trade_date": "20211230", "ts_code": TS_CODE, "open": 9.0, "close": 9.5}]).to_parquet(
            snap / "daily.parquet", index=False
        )
        pd.DataFrame([{"ts_code": TS_CODE, "name": "x"}]).to_parquet(snap / "universe.parquet", index=False)
        (self.sandbox.paths.agent_output / "main.py").write_text(ASOF_GUARD_MAIN, encoding="utf-8")
        replay = pd.DataFrame(
            [
                {"trade_date": "20220104", "ts_code": TS_CODE, "open": 10.0, "close": 10.2,
                 "up_limit": 12.0, "down_limit": 8.0, "is_suspended": False, "available_at": "2022-01-04T17:30:00+08:00"},
                {"trade_date": "20220105", "ts_code": TS_CODE, "open": 10.3, "close": 11.0,
                 "up_limit": 12.0, "down_limit": 8.0, "is_suspended": False, "available_at": "2022-01-05T17:30:00+08:00"},
                {"trade_date": "20220331", "ts_code": TS_CODE, "open": 12.0, "close": 12.5,
                 "up_limit": 14.0, "down_limit": 9.0, "is_suspended": False, "available_at": "2022-03-31T17:30:00+08:00"},
            ]
        )
        with MainPolicyRunner(
            self.executor, self.sandbox.paths, timeout_seconds=30.0,
            decision_time="2022-01-04T09:25:00+08:00", replay_granularity="daily",
        ) as policy:
            policy.validate_main()
            result = run_main_ctx_replay(
                replay, BrokerProfile(),
                shortable_codes=frozenset(),
                main_policy=policy, timeview_enabled=True, snapshot_dir=snap,
            )
        buys = [o for o in _all_orders(result.broker) if o["action"] == "buy" and o["status"] == "filled"]
        self.assertEqual(len(buys), 1)  # main asserted the as-of view, then bought

    def test_limit_order_fills_at_limit_when_bar_reaches_it(self) -> None:
        (self.sandbox.paths.agent_output / "main.py").write_text(LIMIT_FILL_MAIN, encoding="utf-8")
        result = self._run_with(_ohlc_replay(), _limit_minutes())
        buys = [o for o in _all_orders(result.broker) if o["action"] == "buy" and o["status"] == "filled"]
        self.assertEqual(len(buys), 1)
        self.assertEqual(buys[0]["price_label"], "minute:09:33")
        # Maker fill at exactly the limit price — no taker slippage.
        self.assertAlmostEqual(buys[0]["price"], 9.80)

    def test_limit_order_day_end_cancels_when_unfilled(self) -> None:
        (self.sandbox.paths.agent_output / "main.py").write_text(LIMIT_CANCEL_MAIN, encoding="utf-8")
        result = self._run_with(_ohlc_replay(), _limit_minutes())
        buys = [o for o in _all_orders(result.broker) if o["action"] == "buy" and o["status"] == "filled"]
        self.assertEqual(len(buys), 0)  # 7.00 never reached
        cancels = [
            e for e in result.broker.events
            if e["event_type"] == "order_cancelled" and e.get("reason") == "day_end_unfilled"
        ]
        self.assertTrue(cancels)

    def test_fractional_amount_rejects_without_closing_position(self) -> None:
        (self.sandbox.paths.agent_output / "main.py").write_text(INVALID_AMOUNT_MAIN, encoding="utf-8")
        result = self._run_with(_ohlc_replay(), _limit_minutes())
        rejects = [o for o in _all_orders(result.broker) if o["reject_reason"] == "invalid_amount"]
        self.assertEqual(len(rejects), 1)
        self.assertEqual(result.broker.position_quantity("000001.SZ"), 0)

    def test_non_positive_limit_rejects_instead_of_market_order(self) -> None:
        (self.sandbox.paths.agent_output / "main.py").write_text(NON_POSITIVE_LIMIT_MAIN, encoding="utf-8")
        result = self._run_with(_ohlc_replay(), _limit_minutes())
        rejects = [o for o in _all_orders(result.broker) if o["reject_reason"] == "invalid_limit_price"]
        self.assertEqual(len(rejects), 1)
        self.assertEqual(result.broker.position_quantity("000001.SZ"), 0)

    def test_short_requires_explicit_limit_at_strategy_interface(self) -> None:
        (self.sandbox.paths.agent_output / "main.py").write_text(SHORT_MISSING_LIMIT_MAIN, encoding="utf-8")
        with self.assertRaisesRegex(BacktestError, "required keyword-only argument: 'limit'"):
            self._run_with(_ohlc_replay(), _limit_minutes())

    def test_day_end_limit_cancel_records_current_trade_date(self) -> None:
        (self.sandbox.paths.agent_output / "main.py").write_text(LIMIT_DAY_END_CANCEL_MAIN, encoding="utf-8")
        result = self._run_with(_ohlc_replay(), _limit_minutes())
        cancels = [
            e for e in result.broker.events
            if e["event_type"] == "order_cancelled" and e.get("reason") == "day_end_unfilled"
        ]
        self.assertEqual(len(cancels), 1)
        self.assertEqual(cancels[0]["trade_date"], "20220104")

    def test_postclose_hygiene_does_not_override_day_end_cancel(self) -> None:
        (self.sandbox.paths.agent_output / "main.py").write_text(POSTCLOSE_STALE_CANCEL_MAIN, encoding="utf-8")
        result = self._run_with(_ohlc_replay(), _limit_minutes())
        day_end = [
            e for e in result.broker.events
            if e["event_type"] == "order_cancelled" and e.get("reason") == "day_end_unfilled"
        ]
        postclose = [
            e for e in result.broker.events
            if e["event_type"] == "order_cancelled" and e.get("reason") == "postclose_stale_cancel"
        ]
        self.assertEqual(len(day_end), 1)
        self.assertEqual(postclose, [])

    def test_pending_query_dedups_in_flight_orders(self) -> None:
        (self.sandbox.paths.agent_output / "main.py").write_text(PENDING_DEDUP_MAIN, encoding="utf-8")
        result = self._run_with(_ohlc_replay(), _dense_minutes())
        buys = [o for o in _all_orders(result.broker) if o["action"] == "buy" and o["status"] == "filled"]
        # The 09:30 order fills at 09:32 (execution_lag_bars=2); at 09:31 the real
        # position is still flat, so without ctx.broker.pending() the strategy would
        # submit a duplicate. The working-order query collapses it to a single buy.
        self.assertEqual(len(buys), 1)

    def test_cancel_removes_submit_lag_pending_order(self) -> None:
        (self.sandbox.paths.agent_output / "main.py").write_text(STALE_QUEUED_CANCEL_MAIN, encoding="utf-8")
        with MainPolicyRunner(
            self.executor, self.sandbox.paths, timeout_seconds=30.0,
            decision_time="2022-01-04T09:25:00+08:00", replay_granularity="minute",
        ) as policy:
            policy.validate_main()
            result = run_main_ctx_replay(
                _ohlc_replay(), BrokerProfile(),
                shortable_codes=frozenset(), main_policy=policy,
                replay_intraday_1min=_dense_minutes(), execution_lag_bars=3,
            )
        buys = [o for o in _all_orders(result.broker) if o["action"] == "buy" and o["status"] == "filled"]
        self.assertEqual(buys, [])
        cancels = [
            e for e in result.broker.events
            if e["event_type"] == "order_cancelled"
            and e.get("reason") == "stale_gt_1m"
            and e.get("pending_stage") == "submit_lag"
        ]
        self.assertEqual(len(cancels), 1)

    def test_cancel_removes_working_limit_order(self) -> None:
        (self.sandbox.paths.agent_output / "main.py").write_text(WORKING_CANCEL_MAIN, encoding="utf-8")
        with MainPolicyRunner(
            self.executor, self.sandbox.paths, timeout_seconds=30.0,
            decision_time="2022-01-04T09:25:00+08:00", replay_granularity="minute",
        ) as policy:
            policy.validate_main()
            result = run_main_ctx_replay(
                _ohlc_replay(), BrokerProfile(),
                shortable_codes=frozenset(), main_policy=policy,
                replay_intraday_1min=_limit_minutes(), execution_lag_bars=1,
            )
        buys = [o for o in _all_orders(result.broker) if o["action"] == "buy" and o["status"] == "filled"]
        self.assertEqual(buys, [])
        cancels = [
            e for e in result.broker.events
            if e["event_type"] == "order_cancelled" and e.get("reason") == "working_cancel"
        ]
        self.assertEqual(len(cancels), 1)

    def test_same_tick_pending_records_have_documented_fields(self) -> None:
        (self.sandbox.paths.agent_output / "main.py").write_text(SAME_TICK_PENDING_FIELDS_MAIN, encoding="utf-8")
        result = self._run_with(_ohlc_replay(), _dense_minutes())
        buys = [o for o in _all_orders(result.broker) if o["action"] == "buy" and o["status"] == "filled"]
        self.assertEqual(len(buys), 1)  # main(ctx) assertions passed, then the order filled later

    def test_same_tick_buy_then_cancel_has_no_net_main_action(self) -> None:
        (self.sandbox.paths.agent_output / "main.py").write_text(BUY_THEN_CANCEL_SAME_TICK_MAIN, encoding="utf-8")
        result = self._run_with(_ohlc_replay(), _dense_minutes())
        buys = [o for o in _all_orders(result.broker) if o["action"] == "buy" and o["status"] == "filled"]
        self.assertEqual(buys, [])
        main_actions = [e for e in result.broker.events if e["event_type"] == "main_actions"]
        self.assertEqual(main_actions, [])
        cancels = [
            e for e in result.broker.events
            if e["event_type"] == "order_cancelled" and e.get("reason") == "cancel_same_tick"
        ]
        self.assertEqual(len(cancels), 1)

    def test_substep_budget_delays_order_submission(self) -> None:
        # A budget_minutes=3 decision at 09:30 is not submitted until 09:33; then
        # execution_lag_bars=2 fills it at 09:35.
        (self.sandbox.paths.agent_output / "main.py").write_text(SUBSTEP_LATENCY_MAIN, encoding="utf-8")
        result = self._run_with(_ohlc_replay(), _substep_delay_minutes())
        buys = [o for o in _all_orders(result.broker) if o["action"] == "buy" and o["status"] == "filled"]
        self.assertEqual(len(buys), 1)
        self.assertEqual(buys[0]["price_label"], "minute:09:35")
        events = [e for e in result.broker.events if e.get("event_type") == "main_actions" and e.get("delayed_from_substep")]
        self.assertTrue(str(events[0]["actions"][0].get("submitted_at") or "").endswith("09:33:00+08:00"))

    def test_substep_overrun_aborts_replay(self) -> None:
        # The sub-step's real wall-time exceeds its small positive declared budget,
        # so the replay fails fast with an agent-facing error (under-declaring is
        # not exploitable). Without ctx.substep the run would have completed.
        (self.sandbox.paths.agent_output / "main.py").write_text(SUBSTEP_OVERRUN_MAIN, encoding="utf-8")
        with self.assertRaises(BacktestError):
            self._run_with(_ohlc_replay(), _dense_minutes())

    def test_substep_overrun_does_not_abort_under_final_eval(self) -> None:
        # R7: the final/frozen (held-out) eval skips the per-substep wall fail-fast so
        # a transient overrun cannot abort an already-accepted strategy's reproducible
        # eval. The same SUBSTEP_OVERRUN_MAIN that aborts under valid completes here,
        # and the overrunning sub-step's runtime is still aggregated.
        (self.sandbox.paths.agent_output / "main.py").write_text(SUBSTEP_OVERRUN_MAIN, encoding="utf-8")
        with MainPolicyRunner(
            self.executor, self.sandbox.paths, timeout_seconds=30.0,
            decision_time="2022-01-04T09:25:00+08:00", replay_granularity="daily",
        ) as policy:
            policy.validate_main()
            result = run_main_ctx_replay(
                _ohlc_replay(), BrokerProfile(),
                shortable_codes=frozenset(), main_policy=policy,
                replay_intraday_1min=_dense_minutes(),
                enforce_substep_timeout=False,
            )
        self.assertIn("slow", result.substep_runtime)
        self.assertEqual(result.substep_runtime["slow"]["count"], 1.0)

    def test_substep_zero_budget_is_rejected(self) -> None:
        # Wrapping with budget_minutes=0 is a no-op identical to not wrapping, so
        # ctx.substep rejects it (surfaced to the agent as a BacktestError).
        (self.sandbox.paths.agent_output / "main.py").write_text(SUBSTEP_ZERO_BUDGET_MAIN, encoding="utf-8")
        with self.assertRaises(BacktestError):
            self._run_with(_ohlc_replay(), _dense_minutes())

    def test_broker_action_outside_substep_is_rejected(self) -> None:
        (self.sandbox.paths.agent_output / "main.py").write_text(BROKER_OUTSIDE_SUBSTEP_MAIN, encoding="utf-8")
        with self.assertRaisesRegex(BacktestError, "must be called inside ctx.substep"):
            self._run_with(_ohlc_replay(), _dense_minutes())

    def test_state_dir_outside_substep_is_rejected(self) -> None:
        (self.sandbox.paths.agent_output / "main.py").write_text(STATE_OUTSIDE_SUBSTEP_MAIN, encoding="utf-8")
        with self.assertRaisesRegex(BacktestError, "state_dir is only available inside ctx.substep"):
            self._run_with(_ohlc_replay(), _dense_minutes())

    def test_state_env_is_hidden_from_strategy(self) -> None:
        (self.sandbox.paths.agent_output / "main.py").write_text(STATE_ENV_BYPASS_MAIN, encoding="utf-8")
        with self.assertRaisesRegex(BacktestError, "AT_STATE_DIR"):
            self._run_with(_ohlc_replay(), _dense_minutes())

    def test_import_time_nl_is_rejected(self) -> None:
        (self.sandbox.paths.agent_output / "main.py").write_text(IMPORT_NL_MAIN, encoding="utf-8")
        with self.assertRaisesRegex(BacktestError, "must be called inside ctx.substep"):
            self._run_with(_ohlc_replay(), _dense_minutes())

    def test_unwrapped_strategy_time_is_rejected(self) -> None:
        (self.sandbox.paths.agent_output / "main.py").write_text(UNTRACKED_HEAVY_MAIN, encoding="utf-8")
        with self.assertRaisesRegex(BacktestError, "spent .* outside ctx.substep"):
            self._run_with(_ohlc_replay(), _dense_minutes())

    def test_subminute_budget_submits_on_current_tick(self) -> None:
        # A 0.5-minute substep is still accounted/limited, but completes inside
        # the current decision minute and fills at the normal lag from 09:30.
        (self.sandbox.paths.agent_output / "main.py").write_text(SUBSTEP_HALF_BUDGET_MAIN, encoding="utf-8")
        result = self._run_with(_ohlc_replay(), _substep_delay_minutes())
        buys = [o for o in _all_orders(result.broker) if o["action"] == "buy" and o["status"] == "filled"]
        self.assertEqual(len(buys), 1)
        self.assertEqual(buys[0]["price_label"], "minute:09:32")

    def test_substep_light_positive_budget_delays_from_ready_tick(self) -> None:
        # A 1-minute decision at 09:30 crosses a minute boundary: it submits at
        # 09:31, then fills at 09:33.
        (self.sandbox.paths.agent_output / "main.py").write_text(SUBSTEP_LIGHT_BUDGET_MAIN, encoding="utf-8")
        result = self._run_with(_ohlc_replay(), _substep_delay_minutes())
        buys = [o for o in _all_orders(result.broker) if o["action"] == "buy" and o["status"] == "filled"]
        self.assertEqual(len(buys), 1)
        self.assertEqual(buys[0]["price_label"], "minute:09:33")
        events = [e for e in result.broker.events if e.get("event_type") == "main_actions" and e.get("delayed_from_substep")]
        self.assertTrue(str(events[0]["actions"][0].get("submitted_at") or "").endswith("09:31:00+08:00"))

    def test_substep_small_budget_missing_auction_window_is_unfilled(self) -> None:
        # A 09:25 budget_minutes=1 decision is ready at 09:26, outside the accepted
        # open-auction submission window; the host must not auto-send it at 09:30.
        (self.sandbox.paths.agent_output / "main.py").write_text(SUBSTEP_AUCTION_SMALL_MAIN, encoding="utf-8")
        result = self._run_with(_ohlc_replay(), _substep_delay_minutes())
        buys = [o for o in _all_orders(result.broker) if o["action"] == "buy" and o["status"] == "filled"]
        self.assertEqual(buys, [])
        unfilled = [
            e for e in result.broker.events
            if e["event_type"] == "main_actions_unfilled"
            and e.get("reason") == "substep_ready_at_not_orderable"
            and str(e.get("ready_at") or "").endswith("09:26:00+08:00")
        ]
        self.assertEqual(len(unfilled), 1)

    def test_substep_large_budget_missing_auction_window_is_unfilled(self) -> None:
        # A 09:25 budget_minutes=4 decision is ready at 09:29, still outside the
        # exchange's accepted submission windows; it must not roll into 09:30.
        (self.sandbox.paths.agent_output / "main.py").write_text(SUBSTEP_AUCTION_LARGE_MAIN, encoding="utf-8")
        result = self._run_with(_ohlc_replay(), _substep_delay_minutes())
        buys = [o for o in _all_orders(result.broker) if o["action"] == "buy" and o["status"] == "filled"]
        self.assertEqual(buys, [])
        unfilled = [
            e for e in result.broker.events
            if e["event_type"] == "main_actions_unfilled"
            and e.get("reason") == "substep_ready_at_not_orderable"
            and str(e.get("ready_at") or "").endswith("09:29:00+08:00")
        ]
        self.assertEqual(len(unfilled), 1)

    def test_substep_delayed_action_is_hidden_from_pending_until_release(self) -> None:
        # While waiting for ready_at, a cross-minute substep broker action is not a
        # broker order yet and does not appear in pending(); it is submitted at ready.
        (self.sandbox.paths.agent_output / "main.py").write_text(SUBSTEP_PENDING_DELAY_MAIN, encoding="utf-8")
        result = self._run_with(_ohlc_replay(), _substep_delay_minutes())
        buys = [o for o in _all_orders(result.broker) if o["action"] == "buy" and o["status"] == "filled"]
        self.assertEqual(len(buys), 1)

    def test_cross_minute_substep_pending_is_hidden_within_same_tick(self) -> None:
        # The same main(ctx) invocation does not see cross-minute delayed actions
        # through pending(); they are future plans until their declared ready_at.
        (self.sandbox.paths.agent_output / "main.py").write_text(SUBSTEP_SAME_TICK_PENDING_MAIN, encoding="utf-8")
        result = self._run_with(_ohlc_replay(), _substep_delay_minutes())
        buys = [o for o in _all_orders(result.broker) if o["action"] == "buy" and o["status"] == "filled"]
        self.assertEqual(len(buys), 1)
        self.assertFalse(
            any(o.get("reason") == "duplicate_due_to_missing_pending" for o in _all_orders(result.broker))
        )

    def test_cancel_does_not_see_unready_substep_action(self) -> None:
        (self.sandbox.paths.agent_output / "main.py").write_text(SUBSTEP_CANCEL_DELAYED_MAIN, encoding="utf-8")
        result = self._run_with(_ohlc_replay(), _substep_delay_minutes())
        buys = [o for o in _all_orders(result.broker) if o["action"] == "buy" and o["status"] == "filled"]
        self.assertEqual(len(buys), 1)
        self.assertFalse(any(e.get("pending_stage") == "substep_delay" for e in result.broker.events))

    def test_substep_ready_on_real_tick_without_fill_bar_records_unfilled(self) -> None:
        # A ready delayed action should submit on the first real/orderable tick. If
        # that tick has no later fill bar, it is recorded as no-fill rather than being
        # silently rolled into a later off-session or next-day tick.
        (self.sandbox.paths.agent_output / "main.py").write_text(SUBSTEP_LATE_NO_FILL_MAIN, encoding="utf-8")
        result = self._run_with(_ohlc_replay(), _late_no_fill_minutes())
        buys = [o for o in _all_orders(result.broker) if o["action"] == "buy" and o["status"] == "filled"]
        self.assertEqual(buys, [])
        unfilled = [
            e for e in result.broker.events
            if e["event_type"] == "main_actions_unfilled"
            and e.get("minute_key") == "15:00"
            and e.get("reason") == "no_fill_bar_ahead"
        ]
        self.assertEqual(len(unfilled), 1)
        self.assertFalse(
            any(e["event_type"] == "main_actions_unfilled" and e.get("reason") == "substep_delayed_action_not_released"
                for e in result.broker.events)
        )

    def test_substep_runtime_and_replay_metrics_in_result(self) -> None:
        # The replay aggregates per-sub-step wall-time and reports total runtime +
        # the replayed-day count so the Agent can read where backtest time went.
        (self.sandbox.paths.agent_output / "main.py").write_text(SUBSTEP_LATENCY_MAIN, encoding="utf-8")
        result = self._run_with(_ohlc_replay(), _dense_minutes())
        self.assertIn("screen", result.substep_runtime)
        self.assertEqual(result.substep_runtime["screen"]["count"], 1.0)
        self.assertGreaterEqual(result.substep_runtime["screen"]["budget_minutes"], 3.0)
        self.assertIsInstance(result.replay_wall_seconds, float)
        self.assertEqual(result.replayed_trade_days, 2)  # _ohlc_replay has two trade dates
        # Per-phase wall-time is reported for the 24h replay's cost breakdown (W9).
        self.assertEqual(
            set(result.phase_seconds),
            {"strategy_compute", "nl_service", "timeview_build", "state_merge", "broker_match"},
        )
        self.assertGreaterEqual(result.phase_seconds["strategy_compute"], 0.0)

    def test_on_progress_heartbeat_fires_for_long_replay(self) -> None:
        # The throttled heartbeat fires by the day-count threshold (>=30 days) so a
        # long replay is auditable even when each tick is fast.
        (self.sandbox.paths.agent_output / "main.py").write_text(NOOP_MAIN, encoding="utf-8")
        dates = [d.strftime("%Y%m%d") for d in pd.bdate_range("2022-01-03", periods=35)]
        replay = pd.DataFrame(
            [
                {"trade_date": d, "ts_code": TS_CODE, "open": 10.0, "close": 10.0,
                 "up_limit": 12.0, "down_limit": 8.0, "is_suspended": False}
                for d in dates
            ]
        )
        pings: list[tuple] = []
        with MainPolicyRunner(
            self.executor, self.sandbox.paths, timeout_seconds=30.0,
            decision_time="2022-01-03T09:25:00+08:00", replay_granularity="daily",
        ) as policy:
            policy.validate_main()
            run_main_ctx_replay(
                replay, BrokerProfile(),
                shortable_codes=frozenset(), main_policy=policy,
                on_progress=lambda *a: pings.append(a),
            )
        self.assertTrue(pings)  # at least one heartbeat over 35 days
        _date, idx, total, _elapsed, _orders = pings[0]
        self.assertEqual(total, 35)
        self.assertGreaterEqual(idx, 30)

    def test_substep_duplicate_name_in_same_tick_is_rejected(self) -> None:
        # Same-tick sub-step names are used as the order->budget key, so reusing a
        # name would make latency ambiguous. The engine rejects it instead.
        (self.sandbox.paths.agent_output / "main.py").write_text(SUBSTEP_DUPLICATE_NAME_MAIN, encoding="utf-8")
        with self.assertRaisesRegex(BacktestError, "already used in this tick"):
            self._run_with(_ohlc_replay(), _dense_minutes())

    def test_per_trading_day_compute_cap_aborts(self) -> None:
        # A trade day whose cumulative main(ctx) compute exceeds the per-day cap aborts.
        (self.sandbox.paths.agent_output / "main.py").write_text(NOOP_MAIN, encoding="utf-8")
        with MainPolicyRunner(
            self.executor, self.sandbox.paths, timeout_seconds=30.0,
            decision_time="2022-01-04T09:25:00+08:00", replay_granularity="daily",
        ) as policy:
            policy.validate_main()
            with self.assertRaisesRegex(BacktestError, "compute budget"):
                run_main_ctx_replay(
                    _ohlc_replay(), BrokerProfile(),
                    shortable_codes=frozenset(), main_policy=policy,
                    replay_intraday_1min=_dense_minutes(),
                    max_seconds_per_trading_day=0.0001,  # any real tick exceeds this
                )

    def test_per_decision_wall_cap_kills_slow_tick(self) -> None:
        # A single main(ctx) tick over the per-decision wall cap is killed immediately.
        slow = (
            "import time\n"
            "def main(ctx):\n"
            "    if ctx.cur_time == '09:30':\n"
            "        time.sleep(3)\n"
        )
        (self.sandbox.paths.agent_output / "main.py").write_text(slow, encoding="utf-8")
        with MainPolicyRunner(
            self.executor, self.sandbox.paths, timeout_seconds=1.0,  # the per-decision cap
            decision_time="2022-01-04T09:25:00+08:00", replay_granularity="daily",
        ) as policy:
            policy.validate_main()
            with self.assertRaisesRegex(BacktestError, "decision exceeded"):
                run_main_ctx_replay(
                    _ohlc_replay(), BrokerProfile(),
                    shortable_codes=frozenset(), main_policy=policy,
                    replay_intraday_1min=_dense_minutes(),
                )

    def test_preopen_0915_tick_has_no_price_but_fills_at_open(self) -> None:
        (self.sandbox.paths.agent_output / "main.py").write_text(PREOPEN_MAIN, encoding="utf-8")
        result = self._run_with(_ohlc_replay(), _auction_minutes())
        buys = [o for o in _all_orders(result.broker) if o["action"] == "buy" and o["status"] == "filled"]
        self.assertEqual(len(buys), 1)  # the 09:15 guard requires ctx.price is None
        self.assertEqual(buys[0]["price_label"], "auction")
        self.assertEqual(buys[0]["trade_date"], "20220104")
        # The blind 09:15 order fills at the 09:30 opening-auction print (10.0, no slippage).
        self.assertAlmostEqual(buys[0]["price"], 10.0)

    def test_auction_buy_rejected_at_one_sided_limit_up_open(self) -> None:
        (self.sandbox.paths.agent_output / "main.py").write_text(AUCTION_MAIN, encoding="utf-8")
        result = self._run_with(_limit_up_open_replay())
        rejects = [o for o in _all_orders(result.broker) if o["status"] == "rejected"]
        self.assertTrue(any(o["reject_reason"] == "limit_up_blocked_buy" for o in rejects))

    def test_forced_liquidation_and_profit(self) -> None:
        result = self._run()
        # No positions remain after the mandatory final-day liquidation.
        self.assertEqual(result.broker.stock.positions, {})
        self.assertEqual(result.broker.credit.positions, {})
        stats = compute_return_stats(result)
        # Decided day2 09:30, filled next bar (~day2 close 11.0), liquidated day3
        # close (12.5): net positive.
        self.assertGreater(stats["total_return"], 0.0)
        self.assertGreaterEqual(stats["trade_count"], 1)

    def test_substep_budget_over_decision_cap_is_rejected(self) -> None:
        # decision_max_sim_minutes caps the declared budget at substep init: a larger
        # B is rejected in-sandbox, failing the run (the agent must split the work).
        main = (
            "def main(ctx):\n"
            "    if ctx.cur_time == '09:30':\n"
            "        with ctx.substep('screen', budget_minutes=50):\n"
            "            ctx.broker.buy('000001.SZ', amount=1000)\n"
        )
        (self.sandbox.paths.agent_output / "main.py").write_text(main, encoding="utf-8")
        with MainPolicyRunner(
            self.executor, self.sandbox.paths, timeout_seconds=30.0,
            decision_time="2022-01-04T09:25:00+08:00", replay_granularity="daily",
            decision_max_sim_minutes=10.0,
        ) as policy:
            policy.validate_main()
            with self.assertRaisesRegex(BacktestError, "decision_max_sim_minutes"):
                run_main_ctx_replay(
                    _ohlc_replay(), BrokerProfile(),
                    shortable_codes=frozenset(), main_policy=policy,
                    replay_intraday_1min=_dense_minutes(),
                )

    def test_offsession_grid_adds_research_only_ticks(self) -> None:
        # An off-session grid wakes main(ctx) outside the session for research/state;
        # those ticks never fill orders. A 21:00 (post-close) buy is recorded unfilled.
        main = (
            "def main(ctx):\n"
            "    if ctx.cur_time == '21:00' and ctx.broker.position('000001.SZ') == 0:\n"
            "        with ctx.substep('main_tick', budget_minutes=0.5):\n"
            "            ctx.broker.buy('000001.SZ', amount=1000, reason='offsession')\n"
        )
        (self.sandbox.paths.agent_output / "main.py").write_text(main, encoding="utf-8")
        with MainPolicyRunner(
            self.executor, self.sandbox.paths, timeout_seconds=30.0,
            decision_time="2022-01-04T09:25:00+08:00", replay_granularity="daily",
        ) as policy:
            policy.validate_main()
            result = run_main_ctx_replay(
                _ohlc_replay(), BrokerProfile(),
                shortable_codes=frozenset(), main_policy=policy,
                replay_intraday_1min=_dense_minutes(), offsession_tick_minutes=180,
            )
        self.assertGreater(result.offsession_ticks, 0)
        self.assertGreater(result.intraday_ticks, 0)
        self.assertEqual(result.total_ticks, result.intraday_ticks + result.offsession_ticks)
        buys = [o for o in _all_orders(result.broker) if o["action"] == "buy" and o["status"] == "filled"]
        self.assertEqual(len(buys), 0)  # the post-close 21:00 order never fills
        self.assertTrue(any(e["event_type"] == "main_actions_unfilled" for e in result.broker.events))

    def test_preopen_offsession_tick_does_not_fill_at_open(self) -> None:
        # Off-session ticks before the explicit 09:15 auction tick are research/state
        # only; only the auction ticks themselves can route orders to the open.
        main = (
            "def main(ctx):\n"
            "    if ctx.cur_time == '06:00' and ctx.broker.position('000001.SZ') == 0:\n"
            "        with ctx.substep('main_tick', budget_minutes=0.5):\n"
            "            ctx.broker.buy('000001.SZ', amount=1000, reason='preopen_offsession')\n"
        )
        (self.sandbox.paths.agent_output / "main.py").write_text(main, encoding="utf-8")
        with MainPolicyRunner(
            self.executor, self.sandbox.paths, timeout_seconds=30.0,
            decision_time="2022-01-04T09:25:00+08:00", replay_granularity="daily",
        ) as policy:
            policy.validate_main()
            result = run_main_ctx_replay(
                _ohlc_replay(), BrokerProfile(),
                shortable_codes=frozenset(), main_policy=policy,
                replay_intraday_1min=_dense_minutes(), offsession_tick_minutes=180,
            )
        buys = [
            o for o in _all_orders(result.broker)
            if o["action"] == "buy" and o["status"] == "filled"
        ]
        self.assertEqual(len(buys), 0)
        self.assertTrue(
            any(
                e["event_type"] == "main_actions_unfilled"
                and e.get("minute_key") == "06:00"
                for e in result.broker.events
            )
        )

    def test_cross_minute_offsession_broker_action_is_not_scheduled_to_open(self) -> None:
        # A broker action generated in ordinary off-session is not a live broker order
        # and is not a host-side scheduler request for 09:15.
        main = (
            "def main(ctx):\n"
            "    if ctx.cur_time == '06:00' and ctx.broker.position('000001.SZ') == 0:\n"
            "        with ctx.substep('research', budget_minutes=20):\n"
            "            ctx.broker.buy('000001.SZ', amount=1000, reason='delayed_offsession')\n"
        )
        (self.sandbox.paths.agent_output / "main.py").write_text(main, encoding="utf-8")
        with MainPolicyRunner(
            self.executor, self.sandbox.paths, timeout_seconds=30.0,
            decision_time="2022-01-04T09:25:00+08:00", replay_granularity="daily",
        ) as policy:
            policy.validate_main()
            result = run_main_ctx_replay(
                _ohlc_replay(), BrokerProfile(),
                shortable_codes=frozenset(), main_policy=policy,
                replay_intraday_1min=_dense_minutes(), offsession_tick_minutes=180,
            )
        buys = [o for o in _all_orders(result.broker) if o["action"] == "buy" and o["status"] == "filled"]
        self.assertEqual(buys, [])
        self.assertTrue(
            any(
                e["event_type"] == "main_actions_unfilled"
                and e.get("reason") == "substep_generated_at_not_orderable"
                for e in result.broker.events
            )
        )

    def test_close_auction_fills_decision_at_final_bar(self) -> None:
        # R6: with auction_close_time=14:57, the 14:57 bar's decision fills at the
        # day's final 15:00 bar's CLOSE (the close auction), labelled "auction". The
        # final bar's open (10.5) and close (10.6) differ so the test pins the close.
        main = (
            "def main(ctx):\n"
            "    if ctx.cur_time == '14:57' and ctx.broker.position('000001.SZ') == 0 "
            "and not ctx.broker.pending('000001.SZ'):\n"
            "        with ctx.substep('main_tick', budget_minutes=0.5):\n"
            "            ctx.broker.buy('000001.SZ', amount=1000, reason='close_auction')\n"
        )
        (self.sandbox.paths.agent_output / "main.py").write_text(main, encoding="utf-8")
        minutes = pd.DataFrame(
            [
                {"trade_date": "20220104", "ts_code": TS_CODE, "trade_time": "09:31", "open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0},
                {"trade_date": "20220104", "ts_code": TS_CODE, "trade_time": "14:57", "open": 10.4, "high": 10.4, "low": 10.4, "close": 10.4},
                {"trade_date": "20220104", "ts_code": TS_CODE, "trade_time": "15:00", "open": 10.5, "high": 10.7, "low": 10.4, "close": 10.6},
            ]
        )
        with MainPolicyRunner(
            self.executor, self.sandbox.paths, timeout_seconds=30.0,
            decision_time="2022-01-04T09:25:00+08:00", replay_granularity="minute",
        ) as policy:
            policy.validate_main()
            result = run_main_ctx_replay(
                _ohlc_replay(), BrokerProfile(),
                shortable_codes=frozenset(), main_policy=policy,
                replay_intraday_1min=minutes,
            )
        buys = [o for o in _all_orders(result.broker) if o["action"] == "buy" and o["status"] == "filled"]
        self.assertEqual(len(buys), 1)
        self.assertEqual(buys[0]["price_label"], "auction")
        # Fills at the 15:00 close (10.6), not the 15:00 open (10.5); auction, no slippage.
        self.assertAlmostEqual(buys[0]["price"], 10.6)

    def test_cur_datetime_is_exposed(self) -> None:
        # ctx.cur_datetime carries the Beijing-time sim clock for the tick.
        main = (
            "def main(ctx):\n"
            "    if ctx.cur_time == '09:25':\n"
            "        assert ctx.cur_datetime == '2022-01-04T09:25:00+08:00', ctx.cur_datetime\n"
            "        if ctx.broker.position('000001.SZ') == 0 and ctx.price('000001.SZ') is not None:\n"
            "            with ctx.substep('main_tick', budget_minutes=0.5):\n"
            "                ctx.broker.buy('000001.SZ', amount=1000)\n"
        )
        (self.sandbox.paths.agent_output / "main.py").write_text(main, encoding="utf-8")
        result = self._run_with(_ohlc_replay(), _auction_minutes())
        buys = [o for o in _all_orders(result.broker) if o["action"] == "buy" and o["status"] == "filled"]
        self.assertEqual(len(buys), 1)  # the cur_datetime assert passed, then it bought


if __name__ == "__main__":
    unittest.main()


class DayTickPlanTest(unittest.TestCase):
    def test_0925_view_excludes_codes_without_opening_print(self) -> None:
        # A code whose first bar of the day arrives mid-session (intraday
        # resumption, late first trade) has no matched open: exposing its later
        # first price at the 09:25 auction tick would be look-ahead.
        minutes = pd.DataFrame(
            [
                {"trade_date": "20220104", "ts_code": "000001.SZ", "trade_time": "09:30", "open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0},
                {"trade_date": "20220104", "ts_code": "000001.SZ", "trade_time": "10:00", "open": 10.2, "high": 10.3, "low": 10.1, "close": 10.2},
                {"trade_date": "20220104", "ts_code": "000009.SZ", "trade_time": "10:00", "open": 50.0, "high": 50.0, "low": 50.0, "close": 50.0},
            ]
        )
        rows = MinuteMarketData(minutes).rows_for_date("20220104")
        plan = _day_tick_plan(rows, True, "09:15", "09:25", 2)
        tick = next(t for t in plan if t.minute_key == "09:25")
        self.assertEqual(set(tick.group["ts_code"]), {"000001.SZ"})
        # The opening code exposes exactly its 09:30 print (collapsed to the open).
        row = tick.group.iloc[0]
        self.assertEqual((row["open"], row["high"], row["low"], row["close"]), (10.0, 10.0, 10.0, 10.0))


class DecisionGridTest(unittest.TestCase):
    def test_is_decision_tick_grid_and_always_on_ticks(self) -> None:
        from types import SimpleNamespace

        from autotrade.environment.main_ctx_engine import _is_decision_tick

        def tick(minute_key, **flags):
            base = {"is_offsession": False, "is_auction": False, "is_close_auction": False, "is_afterhours": False}
            base.update(flags)
            return SimpleNamespace(minute_key=minute_key, **base)

        # Grid 1 = every bar (exact legacy behavior).
        self.assertTrue(_is_decision_tick(tick("09:31"), 1))
        # Coarser grid: only wall minutes divisible by N decide.
        self.assertTrue(_is_decision_tick(tick("09:35"), 5))
        self.assertFalse(_is_decision_tick(tick("09:36"), 5))
        self.assertFalse(_is_decision_tick(tick("09:31"), 5))
        # Auction, off-session and after-hours ticks always decide regardless of the grid.
        self.assertTrue(_is_decision_tick(tick("09:25", is_auction=True), 5))
        self.assertTrue(_is_decision_tick(tick("14:57", is_close_auction=True), 5))
        self.assertTrue(_is_decision_tick(tick("08:00", is_offsession=True), 5))
        self.assertTrue(_is_decision_tick(tick("15:06", is_afterhours=True), 5))


class LazyBarsTest(unittest.TestCase):
    def _bars(self):
        import importlib.util
        from pathlib import Path as _P

        driver_path = _P(__file__).resolve().parents[2] / "src" / "autotrade" / "environment" / "main_ctx_driver.py"
        spec = importlib.util.spec_from_file_location("_lazybars_driver", driver_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module._LazyBars(
            {"ts_code": ["000001.SZ", "600000.SH"], "open": [10.0, 5.0], "close": [10.5, None]}
        )

    def test_dict_semantics_and_lazy_materialization(self) -> None:
        bars = self._bars()
        self.assertEqual(len(bars), 2)
        self.assertIn("600000.SH", bars)
        self.assertNotIn("999999.SZ", bars)
        self.assertEqual(bars["000001.SZ"], {"ts_code": "000001.SZ", "open": 10.0, "close": 10.5})
        self.assertIsNone(bars.get("999999.SZ"))
        self.assertEqual(sorted(bars), ["000001.SZ", "600000.SH"])
        # Full-dict idioms materialize everything.
        materialized = dict(bars)
        self.assertEqual(set(materialized), {"000001.SZ", "600000.SH"})
        self.assertEqual(materialized["600000.SH"]["open"], 5.0)
        self.assertEqual(len(list(bars.items())), 2)
        copied = bars.copy()
        self.assertEqual(copied["600000.SH"]["close"], None)
        self.assertIsInstance(copied, dict)

    def test_missing_code_raises_keyerror(self) -> None:
        bars = self._bars()
        with self.assertRaises(KeyError):
            bars["999999.SZ"]
