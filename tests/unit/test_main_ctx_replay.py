"""Unified per-minute main(ctx) engine: mid-replay entry via ts_code broker."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pandas as pd

from autotrade.environment.backtest_engine import BacktestError, compute_return_stats
from autotrade.environment.broker import BrokerProfile
from autotrade.environment.executor import LocalExecutor
from autotrade.environment.main_ctx_engine import MainPolicyRunner, run_main_ctx_replay
from autotrade.environment.sandbox import LocalSandbox

TS_CODE = "000001.SZ"

# Open a brand-new long mid-replay (day 2, pre-open), not at the fold decision time.
MAIN_PY = '''
def main(ctx):
    code = "000001.SZ"
    if ctx.cur_date == "20220105" and ctx.cur_time == "09:25" and ctx.broker.position(code) == 0:
        ctx.broker.buy(code, weight=0.3, reason="mid_replay_entry")
'''

# Raises if the synthetic 09:30 open bar leaks end-of-day high/low/vol/amount.
LEAK_GUARD_MAIN = '''
def main(ctx):
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
        ctx.broker.buy(code, weight=0.1, reason="clean_open")
'''

# Enters at the 09:25 call-auction tick and fills at the first continuous bar.
AUCTION_MAIN = '''
def main(ctx):
    code = "000001.SZ"
    if ctx.cur_time == "09:25" and ctx.broker.position(code) == 0 and ctx.price(code) is not None:
        ctx.broker.buy(code, weight=0.2, reason="auction_entry")
'''

# Places a fixed-price (FIX_PRICE) limit buy; the engine rests it until a bar's
# low reaches the limit, then fills at the limit price (no maker slippage).
LIMIT_FILL_MAIN = '''
def main(ctx):
    code = "000001.SZ"
    if ctx.cur_time == "09:30" and ctx.broker.position(code) == 0 and not ctx.broker.pending(code):
        ctx.broker.buy(code, weight=0.2, limit=9.80, valid_bars=5, reason="limit_entry")
'''

# Limit price the market never reaches inside its validity window -> auto-cancelled.
LIMIT_CANCEL_MAIN = '''
def main(ctx):
    code = "000001.SZ"
    if ctx.cur_time == "09:30" and ctx.broker.position(code) == 0 and not ctx.broker.pending(code):
        ctx.broker.buy(code, weight=0.2, limit=9.50, valid_bars=2, reason="limit_miss")
'''

# Limit price the market never reaches, but its validity extends to the close.
LIMIT_DAY_END_CANCEL_MAIN = '''
def main(ctx):
    code = "000001.SZ"
    if ctx.cur_time == "09:30" and ctx.broker.position(code) == 0 and not ctx.broker.pending(code):
        ctx.broker.buy(code, weight=0.2, limit=7.00, valid_bars=99, reason="limit_day_end_miss")
'''

# Re-evaluates every continuous tick but skips codes with a working order, so the
# multi-bar execution lag does not produce duplicate entries (live order-query parity).
PENDING_DEDUP_MAIN = '''
def main(ctx):
    code = "000001.SZ"
    if ctx.cur_time < "09:30" or ctx.price(code) is None:
        return  # continuous session only
    if ctx.broker.position(code) == 0 and not ctx.broker.pending(code):
        ctx.broker.buy(code, weight=0.2, reason="dedup_entry")
'''

# Submits at 09:15 with no matched price yet (ctx.price is None); fills at the open.
PREOPEN_MAIN = '''
def main(ctx):
    code = "000001.SZ"
    if ctx.cur_time == "09:15" and ctx.broker.position(code) == 0 and ctx.price(code) is None:
        ctx.broker.buy(code, weight=0.2, reason="preopen_blind")
'''

# Asserts the per-tick Timeview daily view at 20220105 09:15 holds the frozen
# snapshot history + the prior replay day (visible once that night's evening node
# completed ~02:05) but never today or the future; raises (failing the run) on a
# leak. The agent reads the domain as a directory of parquet parts.
ASOF_GUARD_MAIN = '''
from pathlib import Path

import pandas as pd


def main(ctx):
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
        ctx.broker.buy(code, weight=0.1, reason="asof_ok")
'''

# Declares a heavy screening sub-step (budget_minutes=3): the budget is a real-time
# ceiling and state-staging gate, not a fill delay, so the order fills at the default 09:32.
SUBSTEP_LATENCY_MAIN = '''
def main(ctx):
    code = "000001.SZ"
    if ctx.cur_time == "09:30" and ctx.broker.position(code) == 0 and not ctx.broker.pending(code):
        with ctx.substep("screen", budget_minutes=3):
            ctx.broker.buy(code, weight=0.2, reason="slow_entry")
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
            ctx.broker.buy(code, weight=0.2, reason="overrun")
'''

# Wrapping a block with budget_minutes=0 is identical to not wrapping (no delay,
# no ceiling), so ctx.substep rejects it: the agent must declare a positive budget
# or leave the block unwrapped.
SUBSTEP_ZERO_BUDGET_MAIN = '''
def main(ctx):
    code = "000001.SZ"
    if ctx.cur_time == "09:30":
        with ctx.substep("light", budget_minutes=0):
            ctx.broker.buy(code, weight=0.2, reason="zero_budget")
'''

# A small positive budget (<= execution_lag_bars) fills at the SAME default bar as
# unwrapped, but still gets a real-time ceiling — the intended way to wrap light work.
SUBSTEP_LIGHT_BUDGET_MAIN = '''
def main(ctx):
    code = "000001.SZ"
    if ctx.cur_time == "09:30" and ctx.broker.position(code) == 0 and not ctx.broker.pending(code):
        with ctx.substep("light", budget_minutes=1):
            ctx.broker.buy(code, weight=0.2, reason="light_entry")
'''

NOOP_MAIN = '''
def main(ctx):
    return
'''

# A substep budget on a 09:25 auction order does NOT change its auction fill: the
# budget never moves the fill bar, whether small or large.
SUBSTEP_AUCTION_SMALL_MAIN = '''
def main(ctx):
    code = "000001.SZ"
    if ctx.cur_time == "09:25" and ctx.broker.position(code) == 0 and not ctx.broker.pending(code):
        with ctx.substep("screen", budget_minutes=1):
            ctx.broker.buy(code, weight=0.2, reason="auction_small_budget")
'''

SUBSTEP_AUCTION_LARGE_MAIN = '''
def main(ctx):
    code = "000001.SZ"
    if ctx.cur_time == "09:25" and ctx.broker.position(code) == 0 and not ctx.broker.pending(code):
        with ctx.substep("screen", budget_minutes=4):
            ctx.broker.buy(code, weight=0.2, reason="auction_large_budget")
'''

SUBSTEP_DUPLICATE_NAME_MAIN = '''
def main(ctx):
    code = "000001.SZ"
    if ctx.cur_time == "09:30":
        with ctx.substep("screen", budget_minutes=1):
            ctx.broker.buy(code, weight=0.1, reason="first")
        with ctx.substep("screen", budget_minutes=3):
            ctx.broker.buy(code, weight=0.1, reason="second")
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
        profile = BrokerProfile(initial_cash=1_000_000.0)
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
        orders = result.broker.query_stock_orders()
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
                BrokerProfile(initial_cash=1_000_000.0),
                shortable_codes=frozenset(),
                main_policy=policy,
                execution_lag_bars=1,  # this test exercises the open-bar synthesis, not the lag
            )
        buys = [o for o in result.broker.query_stock_orders() if o["action"] == "buy" and o["status"] == "filled"]
        self.assertEqual(len(buys), 1)

    def _run_with(self, replay: pd.DataFrame, minutes: pd.DataFrame | None = None) -> object:
        with MainPolicyRunner(
            self.executor, self.sandbox.paths, timeout_seconds=30.0,
            decision_time="2022-01-04T09:25:00+08:00", replay_granularity="daily",
        ) as policy:
            policy.validate_main()
            return run_main_ctx_replay(
                replay, BrokerProfile(initial_cash=1_000_000.0),
                shortable_codes=frozenset(), main_policy=policy,
                replay_intraday_1min=minutes,
            )

    def test_auction_entry_fills_at_first_continuous_bar(self) -> None:
        # A 09:25 order is decided on the matched open but, under next-bar
        # execution, fills at the 09:31 open (the first continuous bar) — never
        # within the bar it was decided on.
        (self.sandbox.paths.agent_output / "main.py").write_text(AUCTION_MAIN, encoding="utf-8")
        result = self._run_with(_ohlc_replay(), _auction_minutes())
        buys = [o for o in result.broker.query_stock_orders() if o["action"] == "buy" and o["status"] == "filled"]
        self.assertEqual(len(buys), 1)
        self.assertEqual(buys[0]["price_label"], "auction")
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
    visible = Path(str(ctx.state_dir)) / "plan.txt"   # outside a sub-step: the visible dir
    if ctx.cur_date == "20220104" and ctx.cur_time == "09:25":
        with ctx.substep("screen", budget_minutes=2):
            (Path(str(ctx.state_dir)) / "plan.txt").write_text("go")   # staged, not yet visible
        assert not visible.exists(), "staged write leaked at the generating tick"
    if visible.exists() and "buy" not in _SEEN:
        _SEEN["buy"] = ctx.cur_datetime
        ctx.broker.buy(code, weight=0.1, reason="plan_visible")
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
                replay, BrokerProfile(initial_cash=1_000_000.0),
                shortable_codes=frozenset(), main_policy=policy,
            )
        # main(ctx) asserted the staged write was hidden at 09:25, then bought once it
        # merged; one staged write is recorded and merged.
        buys = [o for o in result.broker.query_stock_orders() if o["action"] == "buy" and o["status"] == "filled"]
        self.assertEqual(len(buys), 1)
        audit = result.state_staging_audit or []
        self.assertEqual(len(audit), 1)
        self.assertTrue(audit[0]["merged"])
        self.assertEqual(audit[0]["substep"], "screen")

    def test_substep_read_sees_old_visible_state(self) -> None:
        # R10: inside a sub-step, reading ctx.state_dir must return the OLD visible
        # value, not the empty staging dir. The strategy writes "v1" to the visible
        # state dir at 09:15 (outside a sub-step), then at 09:25 reads it from inside
        # a sub-step (must be "v1") and stages "v2" (must NOT leak to visible this
        # tick). It buys only when both hold; without the fix the in-substep read
        # raises FileNotFoundError and the replay aborts.
        stage_main = '''
from pathlib import Path


def main(ctx):
    visible = Path(str(ctx.state_dir)) / "state.txt"   # outside a sub-step: visible dir
    if ctx.cur_date == "20220104" and ctx.cur_time == "09:15":
        visible.write_text("v1")
    if ctx.cur_date == "20220104" and ctx.cur_time == "09:25":
        with ctx.substep("read_old", budget_minutes=2):
            staged = Path(str(ctx.state_dir)) / "state.txt"   # staging dir, seeded from visible
            old = staged.read_text()
            staged.write_text("v2")   # staged, delayed
        if old == "v1" and visible.read_text() == "v1":
            ctx.broker.buy("000001.SZ", weight=0.1, reason="read_old_ok")
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
                replay, BrokerProfile(initial_cash=1_000_000.0),
                shortable_codes=frozenset(), main_policy=policy,
            )
        buys = [o for o in result.broker.query_stock_orders() if o["action"] == "buy" and o["status"] == "filled"]
        self.assertEqual(len(buys), 1)
        staged = [a for a in (result.state_staging_audit or []) if a["substep"] == "read_old"]
        self.assertEqual(len(staged), 1)   # only the changed "v2", not the seeded copy
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
                replay, BrokerProfile(initial_cash=1_000_000.0),
                shortable_codes=frozenset(),
                main_policy=policy, timeview_enabled=True, snapshot_dir=snap,
            )
        buys = [o for o in result.broker.query_stock_orders() if o["action"] == "buy" and o["status"] == "filled"]
        self.assertEqual(len(buys), 1)  # main asserted the as-of view, then bought

    def test_limit_order_fills_at_limit_when_bar_reaches_it(self) -> None:
        (self.sandbox.paths.agent_output / "main.py").write_text(LIMIT_FILL_MAIN, encoding="utf-8")
        result = self._run_with(_ohlc_replay(), _limit_minutes())
        buys = [o for o in result.broker.query_stock_orders() if o["action"] == "buy" and o["status"] == "filled"]
        self.assertEqual(len(buys), 1)
        self.assertEqual(buys[0]["price_label"], "minute:09:33")
        # Maker fill at exactly the limit price — no taker slippage.
        self.assertAlmostEqual(buys[0]["price"], 9.80)

    def test_limit_order_auto_cancels_when_unfilled(self) -> None:
        (self.sandbox.paths.agent_output / "main.py").write_text(LIMIT_CANCEL_MAIN, encoding="utf-8")
        result = self._run_with(_ohlc_replay(), _limit_minutes())
        buys = [o for o in result.broker.query_stock_orders() if o["action"] == "buy" and o["status"] == "filled"]
        self.assertEqual(len(buys), 0)  # 9.50 never reached
        cancels = [
            e for e in result.broker.events
            if e["event_type"] == "order_cancelled" and e.get("reason") == "expired_unfilled"
        ]
        self.assertTrue(cancels)

    def test_day_end_limit_cancel_records_current_trade_date(self) -> None:
        (self.sandbox.paths.agent_output / "main.py").write_text(LIMIT_DAY_END_CANCEL_MAIN, encoding="utf-8")
        result = self._run_with(_ohlc_replay(), _limit_minutes())
        cancels = [
            e for e in result.broker.events
            if e["event_type"] == "order_cancelled" and e.get("reason") == "day_end_unfilled"
        ]
        self.assertEqual(len(cancels), 1)
        self.assertEqual(cancels[0]["trade_date"], "20220104")

    def test_pending_query_dedups_in_flight_orders(self) -> None:
        (self.sandbox.paths.agent_output / "main.py").write_text(PENDING_DEDUP_MAIN, encoding="utf-8")
        result = self._run_with(_ohlc_replay(), _dense_minutes())
        buys = [o for o in result.broker.query_stock_orders() if o["action"] == "buy" and o["status"] == "filled"]
        # The 09:30 order fills at 09:32 (execution_lag_bars=2); at 09:31 the real
        # position is still flat, so without ctx.broker.pending() the strategy would
        # submit a duplicate. The working-order query collapses it to a single buy.
        self.assertEqual(len(buys), 1)

    def test_substep_budget_does_not_move_fill_bar(self) -> None:
        # The sub-step budget is a real-time ceiling and state-staging gate, not a
        # fill delay: a budget_minutes=3 decision at 09:30 still fills at the default
        # 09:32 (execution_lag_bars=2), exactly like an unwrapped decision.
        (self.sandbox.paths.agent_output / "main.py").write_text(SUBSTEP_LATENCY_MAIN, encoding="utf-8")
        result = self._run_with(_ohlc_replay(), _dense_minutes())
        buys = [o for o in result.broker.query_stock_orders() if o["action"] == "buy" and o["status"] == "filled"]
        self.assertEqual(len(buys), 1)
        self.assertEqual(buys[0]["price_label"], "minute:09:32")

    def test_substep_overrun_aborts_replay(self) -> None:
        # The sub-step's real wall-time exceeds its small positive declared budget,
        # so the replay fails fast with an agent-facing error (under-declaring is
        # not exploitable). Without ctx.substep the run would have completed.
        (self.sandbox.paths.agent_output / "main.py").write_text(SUBSTEP_OVERRUN_MAIN, encoding="utf-8")
        with self.assertRaises(BacktestError):
            self._run_with(_ohlc_replay(), _dense_minutes())

    def test_substep_zero_budget_is_rejected(self) -> None:
        # Wrapping with budget_minutes=0 is a no-op identical to not wrapping, so
        # ctx.substep rejects it (surfaced to the agent as a BacktestError).
        (self.sandbox.paths.agent_output / "main.py").write_text(SUBSTEP_ZERO_BUDGET_MAIN, encoding="utf-8")
        with self.assertRaises(BacktestError):
            self._run_with(_ohlc_replay(), _dense_minutes())

    def test_substep_light_positive_budget_fills_at_default_bar(self) -> None:
        # A small positive budget (1 <= execution_lag_bars=2) leaves the fill bar at
        # the default 09:32 while still declaring a real-time ceiling.
        (self.sandbox.paths.agent_output / "main.py").write_text(SUBSTEP_LIGHT_BUDGET_MAIN, encoding="utf-8")
        result = self._run_with(_ohlc_replay(), _dense_minutes())
        buys = [o for o in result.broker.query_stock_orders() if o["action"] == "buy" and o["status"] == "filled"]
        self.assertEqual(len(buys), 1)
        self.assertEqual(buys[0]["price_label"], "minute:09:32")

    def test_substep_small_budget_keeps_auction_fill(self) -> None:
        # A 09:25 order in a small-budget substep (ceil(1) <= lag_floor=2) still fills
        # at the auction (09:31, price_label "auction") — the budget adds no delay.
        (self.sandbox.paths.agent_output / "main.py").write_text(SUBSTEP_AUCTION_SMALL_MAIN, encoding="utf-8")
        result = self._run_with(_ohlc_replay(), _auction_minutes())
        buys = [o for o in result.broker.query_stock_orders() if o["action"] == "buy" and o["status"] == "filled"]
        self.assertEqual(len(buys), 1)
        self.assertEqual(buys[0]["price_label"], "auction")

    def test_substep_large_budget_keeps_auction_fill(self) -> None:
        # A large budget no longer delays the fill bar: a 09:25 order in a
        # budget_minutes=4 substep still fills at the 09:31 auction print (~10.1,
        # price_label "auction"), identical to a small-budget or unwrapped order.
        (self.sandbox.paths.agent_output / "main.py").write_text(SUBSTEP_AUCTION_LARGE_MAIN, encoding="utf-8")
        result = self._run_with(_ohlc_replay(), _auction_minutes())
        buys = [o for o in result.broker.query_stock_orders() if o["action"] == "buy" and o["status"] == "filled"]
        self.assertEqual(len(buys), 1)
        self.assertEqual(buys[0]["price_label"], "auction")
        self.assertAlmostEqual(buys[0]["price"], BrokerProfile().slipped_price(10.1, is_buy=True))

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
                replay, BrokerProfile(initial_cash=1_000_000.0),
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
                    _ohlc_replay(), BrokerProfile(initial_cash=1_000_000.0),
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
                    _ohlc_replay(), BrokerProfile(initial_cash=1_000_000.0),
                    shortable_codes=frozenset(), main_policy=policy,
                    replay_intraday_1min=_dense_minutes(),
                )

    def test_preopen_0915_tick_has_no_price_but_fills_at_open(self) -> None:
        (self.sandbox.paths.agent_output / "main.py").write_text(PREOPEN_MAIN, encoding="utf-8")
        result = self._run_with(_ohlc_replay(), _auction_minutes())
        buys = [o for o in result.broker.query_stock_orders() if o["action"] == "buy" and o["status"] == "filled"]
        self.assertEqual(len(buys), 1)  # the 09:15 guard requires ctx.price is None
        self.assertEqual(buys[0]["price_label"], "auction")
        self.assertEqual(buys[0]["trade_date"], "20220104")
        # The blind 09:15 order fills at the 09:30 opening-auction print (10.0).
        self.assertAlmostEqual(buys[0]["price"], BrokerProfile().slipped_price(10.0, is_buy=True))

    def test_auction_buy_rejected_at_one_sided_limit_up_open(self) -> None:
        (self.sandbox.paths.agent_output / "main.py").write_text(AUCTION_MAIN, encoding="utf-8")
        result = self._run_with(_limit_up_open_replay())
        rejects = [o for o in result.broker.query_stock_orders() if o["status"] == "rejected"]
        self.assertTrue(any(o["reject_reason"] == "limit_up_blocked_buy" for o in rejects))

    def test_forced_liquidation_and_profit(self) -> None:
        result = self._run()
        # No positions remain after the mandatory final-day liquidation.
        self.assertEqual(result.broker.positions, {})
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
            "            ctx.broker.buy('000001.SZ', weight=0.1)\n"
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
                    _ohlc_replay(), BrokerProfile(initial_cash=1_000_000.0),
                    shortable_codes=frozenset(), main_policy=policy,
                    replay_intraday_1min=_dense_minutes(),
                )

    def test_offsession_grid_adds_research_only_ticks(self) -> None:
        # An off-session grid wakes main(ctx) outside the session for research/state;
        # those ticks never fill orders. A 21:00 (post-close) buy is recorded unfilled.
        main = (
            "def main(ctx):\n"
            "    if ctx.cur_time == '21:00' and ctx.broker.position('000001.SZ') == 0:\n"
            "        ctx.broker.buy('000001.SZ', weight=0.1, reason='offsession')\n"
        )
        (self.sandbox.paths.agent_output / "main.py").write_text(main, encoding="utf-8")
        with MainPolicyRunner(
            self.executor, self.sandbox.paths, timeout_seconds=30.0,
            decision_time="2022-01-04T09:25:00+08:00", replay_granularity="daily",
        ) as policy:
            policy.validate_main()
            result = run_main_ctx_replay(
                _ohlc_replay(), BrokerProfile(initial_cash=1_000_000.0),
                shortable_codes=frozenset(), main_policy=policy,
                replay_intraday_1min=_dense_minutes(), offsession_tick_minutes=180,
            )
        self.assertGreater(result.offsession_ticks, 0)
        self.assertGreater(result.intraday_ticks, 0)
        self.assertEqual(result.total_ticks, result.intraday_ticks + result.offsession_ticks)
        buys = [o for o in result.broker.query_stock_orders() if o["action"] == "buy" and o["status"] == "filled"]
        self.assertEqual(len(buys), 0)  # the post-close 21:00 order never fills
        self.assertTrue(any(e["event_type"] == "main_actions_unfilled" for e in result.broker.events))

    def test_close_auction_fills_decision_at_final_bar(self) -> None:
        # With auction_close_time=14:57, the 14:57 bar's decision fills at the day's
        # final 15:00 bar (the close auction), labelled "auction".
        main = (
            "def main(ctx):\n"
            "    if ctx.cur_time == '14:57' and ctx.broker.position('000001.SZ') == 0 "
            "and not ctx.broker.pending('000001.SZ'):\n"
            "        ctx.broker.buy('000001.SZ', weight=0.2, reason='close_auction')\n"
        )
        (self.sandbox.paths.agent_output / "main.py").write_text(main, encoding="utf-8")
        minutes = pd.DataFrame(
            [
                {"trade_date": "20220104", "ts_code": TS_CODE, "trade_time": t, "open": o, "high": o, "low": o, "close": o}
                for t, o in (("09:31", 10.0), ("14:57", 10.4), ("15:00", 10.6))
            ]
        )
        with MainPolicyRunner(
            self.executor, self.sandbox.paths, timeout_seconds=30.0,
            decision_time="2022-01-04T09:25:00+08:00", replay_granularity="minute",
        ) as policy:
            policy.validate_main()
            result = run_main_ctx_replay(
                _ohlc_replay(), BrokerProfile(initial_cash=1_000_000.0),
                shortable_codes=frozenset(), main_policy=policy,
                replay_intraday_1min=minutes, auction_close_time="14:57",
            )
        buys = [o for o in result.broker.query_stock_orders() if o["action"] == "buy" and o["status"] == "filled"]
        self.assertEqual(len(buys), 1)
        self.assertEqual(buys[0]["price_label"], "auction")
        self.assertAlmostEqual(buys[0]["price"], BrokerProfile().slipped_price(10.6, is_buy=True))

    def test_cur_datetime_is_exposed(self) -> None:
        # ctx.cur_datetime carries the Beijing-time sim clock for the tick.
        main = (
            "def main(ctx):\n"
            "    if ctx.cur_time == '09:25':\n"
            "        assert ctx.cur_datetime == '2022-01-04T09:25:00+08:00', ctx.cur_datetime\n"
            "        if ctx.broker.position('000001.SZ') == 0 and ctx.price('000001.SZ') is not None:\n"
            "            ctx.broker.buy('000001.SZ', weight=0.1)\n"
        )
        (self.sandbox.paths.agent_output / "main.py").write_text(main, encoding="utf-8")
        result = self._run_with(_ohlc_replay(), _auction_minutes())
        buys = [o for o in result.broker.query_stock_orders() if o["action"] == "buy" and o["status"] == "filled"]
        self.assertEqual(len(buys), 1)  # the cur_datetime assert passed, then it bought


if __name__ == "__main__":
    unittest.main()
