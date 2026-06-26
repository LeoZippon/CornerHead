import stat
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from autotrade.environment.backtest_engine import (
    BacktestError,
    compute_return_stats,
    hide_snapshot_slots_from_agent,
)
from autotrade.environment.broker import BrokerProfile, MarketData, SimBroker
from autotrade.environment.main_ctx_engine import MainPolicyRunner, run_main_ctx_replay
from autotrade.environment.runtime import SandboxPaths
from autotrade.environment.tools.backtest import _profile_kwargs


def _held(state, code):
    return next((p for p in state["positions"] if str(p["ts_code"]) == code), None)

DECISION = "2022-01-04T09:25:00+08:00"


def make_daily(rows):
    return pd.DataFrame(rows, columns=["trade_date", "ts_code", "open", "close", "up_limit", "down_limit", "is_suspended"])


REPLAY = make_daily(
    [
        ("20220104", "000001.SZ", 10.0, 10.5, 11.0, 9.0, False),
        ("20220105", "000001.SZ", 10.6, 11.0, 11.6, 9.5, False),
        ("20220106", "000001.SZ", 11.1, 11.5, 12.1, 10.0, False),
        ("20220104", "000002.SZ", 20.0, 19.5, 22.0, 18.0, False),
        ("20220105", "000002.SZ", 19.4, 19.0, 21.5, 17.6, False),
        ("20220106", "000002.SZ", 18.9, 18.0, 21.0, 17.2, False),
    ]
)

MINUTE_REPLAY = pd.DataFrame(
    [
        {"trade_date": "20220104", "ts_code": "000001.SZ", "trade_time": "09:31", "open": 10.0, "high": 10.20, "low": 10.05, "close": 10.20},
        {"trade_date": "20220104", "ts_code": "000001.SZ", "trade_time": "14:57", "open": 10.20, "high": 10.30, "low": 10.15, "close": 10.25},
        {"trade_date": "20220105", "ts_code": "000001.SZ", "trade_time": "09:31", "open": 10.50, "high": 10.60, "low": 10.40, "close": 10.60},
        {"trade_date": "20220105", "ts_code": "000001.SZ", "trade_time": "10:30", "open": 10.60, "high": 10.90, "low": 10.55, "close": 10.85},
        {"trade_date": "20220106", "ts_code": "000001.SZ", "trade_time": "09:31", "open": 11.20, "high": 11.30, "low": 11.10, "close": 11.20},
    ]
)


class FakeMainPolicy:
    """Drives the replay engine without a sandbox: a plain
    ``func(state) -> list[action]`` stands in for main(ctx) so we can exercise
    the Broker primitives directly."""

    def __init__(self, fn) -> None:
        self.fn = fn

    def validate_main(self) -> None:
        return None

    def step(self, state: dict[str, object]) -> list[dict[str, object]]:
        return list(self.fn(state) or [])


class BrokerPrimitiveTest(unittest.TestCase):
    def make_broker(self, *, shortable=("000002.SZ",), mode="proxy_margin_secs", daily=REPLAY, **profile_kw):
        profile = BrokerProfile(short_inventory_mode=mode, **profile_kw)
        return SimBroker(profile, MarketData(daily), shortable_codes=frozenset(shortable))

    def test_long_buy_hold_and_close(self):
        broker = self.make_broker()
        order = broker.execute("000001.SZ", "buy", trade_date="20220104", raw_price=10.0, amount=1000)
        self.assertEqual(order.status, "filled")
        self.assertEqual(broker.position_quantity("000001.SZ"), 1000)
        broker.mark_to_market("20220105")
        broker.mark_to_market("20220106")
        broker.close_all("20220106")
        self.assertEqual(broker.positions, {})
        self.assertGreater(broker.equity(), broker.initial_equity)

    def test_t1_blocks_same_day_close(self):
        broker = self.make_broker()
        broker.execute("000001.SZ", "buy", trade_date="20220104", raw_price=10.0, amount=1000)
        broker.close_all("20220104")
        self.assertIn("000001.SZ", broker.positions)
        self.assertTrue(any(e["event_type"] == "exit_blocked_t_plus_one" for e in broker.events))

    def test_partial_sell_clamps_to_sellable_balance(self):
        broker = self.make_broker()
        broker.execute("000001.SZ", "buy", trade_date="20220104", raw_price=10.0, amount=1000)
        broker.execute("000001.SZ", "buy", trade_date="20220105", raw_price=10.6, amount=500)
        self.assertEqual(broker.position_quantity("000001.SZ"), 1500)
        order = broker.execute("000001.SZ", "sell", trade_date="20220105", raw_price=11.0, amount=1500)
        # Only the 1000 shares bought on 0104 are sellable on 0105 (T+1).
        self.assertEqual(order.filled_quantity, 1000)
        self.assertEqual(broker.position_quantity("000001.SZ"), 500)

    def test_short_requires_margin_secs_membership(self):
        broker = self.make_broker(shortable=())
        order = broker.execute("000002.SZ", "short", trade_date="20220104", raw_price=20.0, amount=500)
        self.assertEqual(order.status, "rejected")
        self.assertEqual(order.reject_reason, "margin_secs_not_shortable")

    def test_short_profit_and_borrow_fee(self):
        broker = self.make_broker()
        broker.execute("000002.SZ", "short", trade_date="20220104", raw_price=20.0, amount=500)
        broker.mark_to_market("20220105")
        broker.close_all("20220106")
        closed = [event for event in broker.events if event["event_type"] == "position_closed"][0]
        self.assertGreater(broker.equity(), broker.initial_equity)
        self.assertGreater(broker.borrow_fees, 0.0)
        self.assertGreater(broker.stamp_duty_paid, 0.0)
        self.assertEqual(closed["side"], "short")

    def test_broker_inventory_mode_rejects_without_files(self):
        broker = self.make_broker(mode="broker_inventory")
        order = broker.execute("000002.SZ", "short", trade_date="20220104", raw_price=20.0, amount=500)
        self.assertEqual(order.reject_reason, "broker_inventory_unavailable")

    def test_max_total_holdings_rejects_new_code(self):
        broker = self.make_broker(shortable=(), max_total_holdings=1)
        broker.execute("000001.SZ", "buy", trade_date="20220104", raw_price=10.0, amount=100)
        order = broker.execute("000002.SZ", "buy", trade_date="20220104", raw_price=20.0, amount=100)
        self.assertEqual(order.reject_reason, "max_holdings_reached")

    def test_single_name_weight_cap_clamps_shares(self):
        broker = self.make_broker(max_single_name_weight=0.2)  # 20% of 1,000,000 = 200,000
        order = broker.execute("000001.SZ", "buy", trade_date="20220104", raw_price=10.0, weight=0.5)
        self.assertEqual(order.status, "filled")
        self.assertEqual(order.filled_quantity, 20000)  # 200,000 / 10 = 20,000 shares

    def test_default_profile_does_not_force_holdings_or_single_name_caps(self):
        broker = self.make_broker(shortable=())
        first = broker.execute("000001.SZ", "buy", trade_date="20220104", raw_price=10.0, weight=0.5)
        second = broker.execute("000002.SZ", "buy", trade_date="20220104", raw_price=20.0, amount=100)
        self.assertEqual(first.status, "filled")
        self.assertGreater(first.filled_quantity, 20000)
        self.assertEqual(second.status, "filled")

    def test_limit_up_blocks_buy_and_suspension_blocks_fill(self):
        daily = make_daily(
            [
                ("20220104", "000001.SZ", 11.0, 11.0, 11.0, 9.0, False),
                ("20220104", "000003.SZ", 5.0, 5.0, 5.5, 4.5, True),
            ]
        )
        broker = SimBroker(BrokerProfile(), MarketData(daily), shortable_codes=frozenset())
        blocked = broker.execute("000001.SZ", "buy", trade_date="20220104", raw_price=11.0, amount=100)
        suspended = broker.execute("000003.SZ", "buy", trade_date="20220104", raw_price=5.0, amount=100)
        self.assertEqual(blocked.reject_reason, "limit_up_blocked_buy")
        self.assertEqual(suspended.reject_reason, "suspended")

    def test_trades_for_records_open_and_reduce_history(self):
        broker = self.make_broker()
        broker.execute("000001.SZ", "buy", trade_date="20220104", raw_price=10.0, amount=1000)
        broker.mark_to_market("20220105")
        broker.execute("000001.SZ", "sell", trade_date="20220105", raw_price=11.0, amount=500)
        trades = broker.trades_for("000001.SZ")
        self.assertEqual([t["kind"] for t in trades], ["open", "reduce"])
        self.assertAlmostEqual(trades[0]["price"], BrokerProfile().slipped_price(10.0, is_buy=True))

    def test_costs_use_slippage_min_commission_and_dated_stamp_duty(self):
        profile = BrokerProfile()
        self.assertAlmostEqual(profile.slipped_price(10.0, is_buy=True), 10.005)
        self.assertAlmostEqual(profile.slipped_price(10.0, is_buy=False), 9.995)
        self.assertEqual(profile.commission(1_000.0), 5.0)  # minimum ¥5 floor
        self.assertAlmostEqual(profile.commission(100_000.0), 10.0)
        self.assertAlmostEqual(profile.stamp_duty_on_sale(100_000.0, "20230827"), 100.0)
        self.assertAlmostEqual(profile.stamp_duty_on_sale(100_000.0, "20230828"), 50.0)

    def test_profile_record_round_trips_all_constructor_fields(self):
        profile = BrokerProfile(
            commission_bps=2.5,
            min_commission_cny=1.25,
            stamp_duty_sell_bps_before_cutover=12.0,
            stamp_duty_sell_bps_from_cutover=6.0,
            slippage_bps=7.0,
            short_borrow_fee_annual=0.03,
            maintenance_source="broker-doc",
        )
        restored = BrokerProfile(**_profile_kwargs(profile.to_record()))
        self.assertEqual(restored.min_commission_cny, 1.25)
        self.assertEqual(restored.stamp_duty_sell_bps_before_cutover, 12.0)
        self.assertEqual(restored.slippage_bps, 7.0)
        self.assertEqual(restored.maintenance_source, "broker-doc")
        private_restored = BrokerProfile(**_profile_kwargs(BrokerProfile(is_private_fund=True).to_record()))
        self.assertEqual(private_restored.effective_short_margin_ratio, 1.2)


class ReplayIntegrationTest(unittest.TestCase):
    def test_main_runs_each_bar_and_liquidates_at_exit(self):
        def buy_hold(state):
            return [] if _held(state, "000001.SZ") else [{"action": "buy", "ts_code": "000001.SZ", "weight": 0.1}]

        replay = run_main_ctx_replay(
            REPLAY,
            BrokerProfile(),
            decision_time_iso=DECISION,
            shortable_codes=frozenset(),
            main_policy=FakeMainPolicy(buy_hold),
        )
        stats = compute_return_stats(replay)
        self.assertEqual(stats["replay_granularity"], "daily")
        self.assertEqual(stats["order_status_counts"].get("filled"), 1)
        self.assertGreater(stats["total_return"], 0.0)
        self.assertTrue(any(e["event_type"] == "position_closed" for e in replay.broker.events))

    def test_minute_replay_uses_minute_bars(self):
        def close_entry(state):
            if not _held(state, "000001.SZ") and state["cur_time"] >= "14:57":
                return [{"action": "buy", "ts_code": "000001.SZ", "weight": 0.1}]
            return []

        replay = run_main_ctx_replay(
            REPLAY,
            BrokerProfile(),
            decision_time_iso=DECISION,
            shortable_codes=frozenset(),
            replay_intraday_1min=MINUTE_REPLAY,
            main_policy=FakeMainPolicy(close_entry),
        )
        self.assertEqual(replay.granularity, "minute")
        fill = [event for event in replay.broker.events if event["event_type"] == "order_filled"][0]
        self.assertEqual(fill["price_label"], "minute:14:57")
        self.assertAlmostEqual(fill["price"], BrokerProfile().slipped_price(10.25, is_buy=True))

    def test_swing_t_buys_dip_and_sells_rally_next_day(self):
        def swing(state):
            if state["cur_time"] >= "14:57":
                return []
            bar = next((b for b in state["bars"] if str(b["ts_code"]) == "000001.SZ"), None)
            if bar is None or bar.get("close") is None:
                return []
            price = float(bar["close"])
            pos = _held(state, "000001.SZ")
            amount = 500
            if pos is None:
                return [{"action": "buy", "ts_code": "000001.SZ", "amount": amount}]
            if int(pos["sellable_quantity"]) >= amount and price > float(pos["entry_price"]) * 1.01:
                return [{"action": "sell", "ts_code": "000001.SZ", "amount": amount}]
            return []

        replay = run_main_ctx_replay(
            REPLAY,
            BrokerProfile(),
            decision_time_iso=DECISION,
            shortable_codes=frozenset(),
            replay_intraday_1min=MINUTE_REPLAY,
            main_policy=FakeMainPolicy(swing),
        )
        reduced = [e for e in replay.broker.events if e["event_type"] in {"position_reduced", "position_closed"}]
        self.assertTrue(reduced)  # the swing sold on a later-day rally

    def test_unshortable_code_is_rejected_during_replay(self):
        def go_short(state):
            return [] if _held(state, "000002.SZ") else [{"action": "short", "ts_code": "000002.SZ", "weight": 0.1}]

        replay = run_main_ctx_replay(
            REPLAY,
            BrokerProfile(),
            decision_time_iso=DECISION,
            shortable_codes=frozenset(),
            main_policy=FakeMainPolicy(go_short),
        )
        self.assertTrue(any(o.reject_reason == "margin_secs_not_shortable" for o in replay.broker.orders))


class CandidateIsolationTest(unittest.TestCase):
    def test_snapshot_slots_are_hidden_and_restored_during_candidate_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = SandboxPaths(Path(tmp) / "mnt")
            for slot in (paths.train, paths.valid, paths.test, paths.artifacts):
                slot.mkdir(parents=True)
                slot.chmod(0o755)

            with hide_snapshot_slots_from_agent(paths):
                self.assertEqual(stat.S_IMODE(paths.train.stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE(paths.valid.stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE(paths.test.stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE(paths.artifacts.stat().st_mode), 0o700)

            self.assertEqual(stat.S_IMODE(paths.train.stat().st_mode), 0o755)
            self.assertEqual(stat.S_IMODE(paths.valid.stat().st_mode), 0o755)
            self.assertEqual(stat.S_IMODE(paths.test.stat().st_mode), 0o755)
            self.assertEqual(stat.S_IMODE(paths.artifacts.stat().st_mode), 0o755)

    def test_main_policy_runner_restores_slots_on_enter_error(self):
        class FailingPopenExecutor:
            python = "python"

            def map_path(self, path):
                return str(path)

            def popen(self, *_args, **_kwargs):
                raise RuntimeError("popen failed")

        with tempfile.TemporaryDirectory() as tmp:
            paths = SandboxPaths(Path(tmp) / "mnt")
            paths.workspace.mkdir(parents=True)
            paths.agent_output.mkdir(parents=True)
            paths.artifacts.mkdir(parents=True)
            paths.artifacts.chmod(0o755)

            runner = MainPolicyRunner(
                FailingPopenExecutor(),
                paths,
                timeout_seconds=1.0,
                decision_time=DECISION,
                replay_granularity="minute",
            )
            with self.assertRaisesRegex(RuntimeError, "popen failed"):
                runner.__enter__()

            # The hidden snapshot/artifact slots are restored even when startup fails.
            self.assertEqual(stat.S_IMODE(paths.artifacts.stat().st_mode), 0o755)


if __name__ == "__main__":
    unittest.main()
