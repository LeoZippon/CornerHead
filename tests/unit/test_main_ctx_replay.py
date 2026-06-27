"""Unified per-minute main(ctx) engine: mid-replay entry via ts_code broker."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pandas as pd

from autotrade.environment.backtest_engine import compute_return_stats
from autotrade.environment.broker import BrokerProfile
from autotrade.environment.executor import LocalExecutor
from autotrade.environment.main_ctx_engine import MainPolicyRunner, run_main_ctx_replay
from autotrade.environment.sandbox import LocalSandbox

TS_CODE = "000001.SZ"

# Open a brand-new long mid-replay (day 2, 09:30), not at the fold decision time.
MAIN_PY = '''
def main(ctx):
    code = "000001.SZ"
    if ctx.cur_date == "20220105" and ctx.cur_time == "09:30" and ctx.broker.position(code) == 0:
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

# Enters at the pre-open call-auction tick (09:25), filled at the day open.
AUCTION_MAIN = '''
def main(ctx):
    code = "000001.SZ"
    if ctx.cur_time == "09:25" and ctx.broker.position(code) == 0 and ctx.price(code) is not None:
        ctx.broker.buy(code, weight=0.2, reason="auction_entry")
'''

# Submits at 09:15 with no matched price yet (ctx.price is None); fills at the open.
PREOPEN_MAIN = '''
def main(ctx):
    code = "000001.SZ"
    if ctx.cur_time == "09:15" and ctx.broker.position(code) == 0 and ctx.price(code) is None:
        ctx.broker.buy(code, weight=0.2, reason="preopen_blind")
'''

# Asserts the rolling daily as-of view at 20220105 holds history + the prior
# replay day but never today or the future; raises (failing the run) on a leak.
ASOF_GUARD_MAIN = '''
from pathlib import Path

import pandas as pd


def main(ctx):
    code = "000001.SZ"
    if ctx.cur_date != "20220105" or ctx.cur_time != "09:15":
        return
    dates = set(pd.read_parquet(Path(str(ctx.asof_dir)) / "daily.parquet")["trade_date"].astype(str))
    assert "20211230" in dates, dates           # snapshot history
    assert "20220104" in dates, dates           # prior replay day (visible after close)
    assert "20220105" not in dates, "today leaked"
    assert "20220331" not in dates, "future leaked"
    if ctx.broker.position(code) == 0:
        ctx.broker.buy(code, weight=0.1, reason="asof_ok")
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
                decision_time_iso="2022-01-04T09:30:00+08:00",
                shortable_codes=frozenset(),
                main_policy=policy,
            )

    def test_opens_new_position_mid_replay(self) -> None:
        result = self._run()
        orders = result.broker.query_orders()
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
                decision_time_iso="2022-01-04T09:30:00+08:00",
                shortable_codes=frozenset(),
                main_policy=policy,
            )
        buys = [o for o in result.broker.query_orders() if o["action"] == "buy" and o["status"] == "filled"]
        self.assertEqual(len(buys), 1)

    def _run_with(self, replay: pd.DataFrame) -> object:
        with MainPolicyRunner(
            self.executor, self.sandbox.paths, timeout_seconds=30.0,
            decision_time="2022-01-04T09:25:00+08:00", replay_granularity="daily",
        ) as policy:
            policy.validate_main()
            return run_main_ctx_replay(
                replay, BrokerProfile(initial_cash=1_000_000.0),
                decision_time_iso="2022-01-04T09:25:00+08:00",
                shortable_codes=frozenset(), main_policy=policy,
            )

    def test_auction_entry_fills_at_open(self) -> None:
        (self.sandbox.paths.agent_output / "main.py").write_text(AUCTION_MAIN, encoding="utf-8")
        result = self._run_with(_ohlc_replay())
        buys = [o for o in result.broker.query_orders() if o["action"] == "buy" and o["status"] == "filled"]
        self.assertEqual(len(buys), 1)
        self.assertEqual(buys[0]["price_label"], "auction")
        self.assertEqual(buys[0]["trade_date"], "20220104")
        # Filled at the slipped day open (10.0), the call-auction matched price.
        self.assertAlmostEqual(buys[0]["price"], BrokerProfile().slipped_price(10.0, is_buy=True))

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
                decision_time_iso="2022-01-04T09:25:00+08:00", shortable_codes=frozenset(),
                main_policy=policy, asof_view_enabled=True, snapshot_dir=snap,
            )
        buys = [o for o in result.broker.query_orders() if o["action"] == "buy" and o["status"] == "filled"]
        self.assertEqual(len(buys), 1)  # main asserted the as-of view, then bought

    def test_preopen_0915_tick_has_no_price_but_fills_at_open(self) -> None:
        (self.sandbox.paths.agent_output / "main.py").write_text(PREOPEN_MAIN, encoding="utf-8")
        result = self._run_with(_ohlc_replay())
        buys = [o for o in result.broker.query_orders() if o["action"] == "buy" and o["status"] == "filled"]
        self.assertEqual(len(buys), 1)  # the 09:15 guard requires ctx.price is None
        self.assertEqual(buys[0]["price_label"], "auction")
        self.assertEqual(buys[0]["trade_date"], "20220104")
        self.assertAlmostEqual(buys[0]["price"], BrokerProfile().slipped_price(10.0, is_buy=True))

    def test_auction_buy_rejected_at_one_sided_limit_up_open(self) -> None:
        (self.sandbox.paths.agent_output / "main.py").write_text(AUCTION_MAIN, encoding="utf-8")
        result = self._run_with(_limit_up_open_replay())
        rejects = [o for o in result.broker.query_orders() if o["status"] == "rejected"]
        self.assertTrue(any(o["reject_reason"] == "limit_up_blocked_buy" for o in rejects))

    def test_forced_liquidation_and_profit(self) -> None:
        result = self._run()
        # No positions remain after the mandatory final-day liquidation.
        self.assertEqual(result.broker.positions, {})
        stats = compute_return_stats(result)
        # Bought ~day2 open (10.3), liquidated day3 close (12.5): net positive.
        self.assertGreater(stats["total_return"], 0.0)
        self.assertGreaterEqual(stats["trade_count"], 1)


if __name__ == "__main__":
    unittest.main()
