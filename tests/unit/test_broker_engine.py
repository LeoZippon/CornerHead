import stat
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from hl_trader.environment.backtest_engine import (
    BacktestError,
    build_order_plan,
    compose_final_scores,
    compute_return_stats,
    cross_section_normalize,
    hide_snapshot_slots_from_agent,
    run_fixed_holding_replay,
    truncate_candidates,
    validate_candidates,
    validate_order_plan,
)
from hl_trader.environment.broker import BrokerProfile, MarketData, Order, SimBroker
from hl_trader.environment.runtime import SandboxPaths


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


class BrokerTest(unittest.TestCase):
    def make_broker(self, *, shortable=("000002.SZ",), mode="proxy_margin_secs"):
        profile = BrokerProfile(short_inventory_mode=mode)
        return SimBroker(profile, MarketData(REPLAY), shortable_codes=frozenset(shortable))

    def order(self, code, side, weight):
        return Order(ts_code=code, side=side, order_type="target_weight", target_weight=weight, reason="t", source_artifacts=[])

    def test_long_fill_hold_and_close(self):
        broker = self.make_broker()
        broker.submit_order(self.order("000001.SZ", "long", 0.1), decision_time="2022-01-04T09:25:00+08:00", fill_date="20220104")
        broker.fill_open("20220104")
        self.assertEqual(broker.orders[0].status, "filled")
        broker.mark_to_market("20220105")
        broker.mark_to_market("20220106")
        broker.close_all("20220106")
        self.assertEqual(broker.positions, {})
        self.assertGreater(broker.equity(), broker.initial_equity)

    def test_t1_violation_raises(self):
        broker = self.make_broker()
        broker.submit_order(self.order("000001.SZ", "long", 0.1), decision_time="t", fill_date="20220104")
        broker.fill_open("20220104")
        with self.assertRaisesRegex(ValueError, "T\\+1"):
            broker.close_all("20220104")

    def test_short_requires_margin_secs_membership(self):
        broker = self.make_broker(shortable=())
        order = broker.submit_order(self.order("000002.SZ", "short", -0.1), decision_time="t", fill_date="20220104")
        self.assertEqual(order.status, "rejected")
        self.assertEqual(order.reject_reason, "margin_secs_not_shortable")
        self.assertEqual(broker.reject_counts["margin_secs_not_shortable"], 1)

    def test_short_profit_and_borrow_fee(self):
        broker = self.make_broker()
        broker.submit_order(self.order("000002.SZ", "short", -0.1), decision_time="t", fill_date="20220104")
        broker.fill_open("20220104")
        broker.mark_to_market("20220105")
        broker.close_all("20220106")
        self.assertGreater(broker.equity(), broker.initial_equity)
        self.assertGreater(broker.borrow_fees, 0.0)
        # Stamp duty applies to the opening short sale (10 bps before 20230828).
        self.assertGreater(broker.stamp_duty_paid, 0.0)

    def test_costs_use_slippage_min_commission_and_dated_stamp_duty(self):
        profile = BrokerProfile()
        self.assertAlmostEqual(profile.slipped_price(10.0, is_buy=True), 10.005)
        self.assertAlmostEqual(profile.slipped_price(10.0, is_buy=False), 9.995)
        self.assertEqual(profile.commission(1_000.0), 5.0)  # minimum ¥5 floor
        self.assertAlmostEqual(profile.commission(100_000.0), 10.0)
        self.assertAlmostEqual(profile.stamp_duty_on_sale(100_000.0, "20230827"), 100.0)
        self.assertAlmostEqual(profile.stamp_duty_on_sale(100_000.0, "20230828"), 50.0)

    def test_broker_inventory_mode_rejects_without_files(self):
        broker = self.make_broker(mode="broker_inventory")
        order = broker.submit_order(self.order("000002.SZ", "short", -0.1), decision_time="t", fill_date="20220104")
        self.assertEqual(order.reject_reason, "broker_inventory_unavailable")

    def test_limit_up_blocks_buy_and_suspension_blocks_fill(self):
        daily = make_daily(
            [
                ("20220104", "000001.SZ", 11.0, 11.0, 11.0, 9.0, False),
                ("20220104", "000003.SZ", 5.0, 5.0, 5.5, 4.5, True),
                ("20220105", "000001.SZ", 11.0, 11.0, 12.0, 10.0, False),
                ("20220105", "000003.SZ", 5.0, 5.0, 5.5, 4.5, False),
            ]
        )
        broker = SimBroker(BrokerProfile(), MarketData(daily), shortable_codes=frozenset())
        broker.submit_order(self.order("000001.SZ", "long", 0.1), decision_time="t", fill_date="20220104")
        broker.submit_order(self.order("000003.SZ", "long", 0.1), decision_time="t", fill_date="20220104")
        broker.fill_open("20220104")
        reasons = {o.ts_code: o.reject_reason for o in broker.orders}
        self.assertEqual(reasons["000001.SZ"], "limit_up_open_blocked_buy")
        self.assertEqual(reasons["000003.SZ"], "suspended_on_fill_date")


class ScoreAndPlanTest(unittest.TestCase):
    def candidates(self):
        return pd.DataFrame(
            [
                {"ts_code": "000001.SZ", "factor_score": 2.0, "reason": "a", "source_artifacts": []},
                {"ts_code": "000002.SZ", "factor_score": -2.0, "reason": "b", "source_artifacts": []},
                {"ts_code": "000003.SZ", "factor_score": 0.5, "reason": "c", "source_artifacts": []},
            ]
        )

    def test_validate_candidates_checks_universe_and_duplicates(self):
        universe = {"000001.SZ", "000002.SZ", "000003.SZ"}
        frame = validate_candidates(self.candidates(), universe=universe)
        self.assertEqual(len(frame), 3)
        with self.assertRaisesRegex(BacktestError, "outside the visible universe"):
            validate_candidates(self.candidates(), universe={"000001.SZ"})
        duplicated = pd.concat([self.candidates(), self.candidates()])
        with self.assertRaisesRegex(BacktestError, "duplicate"):
            validate_candidates(duplicated, universe=universe)

    def test_truncates_to_top_candidates_by_abs_score(self):
        kept, truncated = truncate_candidates(self.candidates(), max_candidates=2)
        self.assertEqual(truncated, 1)
        self.assertEqual(sorted(kept["ts_code"]), ["000001.SZ", "000002.SZ"])  # |2.0| beats |0.5|
        same, none_truncated = truncate_candidates(self.candidates(), max_candidates=10)
        self.assertEqual(none_truncated, 0)
        self.assertEqual(len(same), 3)

    def test_normalize_and_compose(self):
        self.assertEqual(list(cross_section_normalize(pd.Series([2.0, -1.0]))), [1.0, -0.5])
        nl = {"000001.SZ": {"nl_score": 1.0, "confidence": 0.9, "risk_tags": []}}
        scored = compose_final_scores(self.candidates(), nl, nl_mode="on")
        row = scored.set_index("ts_code").loc["000001.SZ"]
        self.assertAlmostEqual(row["final_score"], 0.7 * 1.0 + 0.3 * 1.0)
        off = compose_final_scores(self.candidates(), {}, nl_mode="off")
        self.assertAlmostEqual(off.set_index("ts_code").loc["000001.SZ", "final_score"], 1.0)

    def test_sample_mode_gives_unsampled_candidates_the_average_nl_score(self):
        nl = {
            "000001.SZ": {"nl_score": 0.6, "confidence": 0.9, "risk_tags": []},
            "000002.SZ": {"nl_score": 0.2, "confidence": 0.9, "risk_tags": []},
        }
        scored = compose_final_scores(self.candidates(), nl, nl_mode="sample")
        indexed = scored.set_index("ts_code")
        self.assertAlmostEqual(indexed.loc["000003.SZ", "nl_score"], 0.4)  # mean of sampled
        self.assertFalse(indexed.loc["000003.SZ", "nl_scored"])
        self.assertAlmostEqual(
            indexed.loc["000003.SZ", "final_score"], 0.7 * 0.25 + 0.3 * 0.4
        )

    def test_order_plan_thresholds_cap_and_hard_exclude(self):
        nl = {
            "000001.SZ": {"nl_score": 1.0, "confidence": 0.9, "risk_tags": []},
            "000002.SZ": {"nl_score": -1.0, "confidence": 0.9, "risk_tags": []},
            "000003.SZ": {"nl_score": 1.0, "confidence": 0.9, "risk_tags": ["hard_exclude"]},
        }
        scored = compose_final_scores(self.candidates(), nl, nl_mode="on")
        plan = build_order_plan(scored, long_threshold=0.7, short_threshold=-0.7, max_total_holdings=10, max_single_name_weight=0.2)
        sides = dict(zip(plan["ts_code"], plan["side"]))
        self.assertEqual(sides, {"000001.SZ": "long", "000002.SZ": "short"})
        self.assertAlmostEqual(abs(plan["target_weight"]).iloc[0], 0.1)
        validate_order_plan(plan, universe={"000001.SZ", "000002.SZ"}, max_total_holdings=10, max_single_name_weight=0.2)

    def test_order_plan_weights_are_proportional_to_abs_final_score(self):
        scored = pd.DataFrame(
            [
                {"ts_code": "000001.SZ", "final_score": 0.9, "factor_score": 1.0, "nl_score": 0.5, "reason": "a", "source_artifacts": [], "hard_excluded": False},
                {"ts_code": "000002.SZ", "final_score": -0.75, "factor_score": -1.0, "nl_score": -0.5, "reason": "b", "source_artifacts": [], "hard_excluded": False},
                {"ts_code": "000003.SZ", "final_score": 0.75, "factor_score": 0.5, "nl_score": 0.5, "reason": "c", "source_artifacts": [], "hard_excluded": False},
            ]
        )
        plan = build_order_plan(scored, long_threshold=0.7, short_threshold=-0.7, max_total_holdings=10, max_single_name_weight=0.2)
        weights = dict(zip(plan["ts_code"], plan["target_weight"]))
        gross = 3 / 10
        total = 0.9 + 0.75 + 0.75
        self.assertAlmostEqual(weights["000001.SZ"], gross * 0.9 / total)
        self.assertAlmostEqual(weights["000002.SZ"], -gross * 0.75 / total)
        self.assertAlmostEqual(weights["000003.SZ"], gross * 0.75 / total)
        self.assertGreater(weights["000001.SZ"], weights["000003.SZ"])

    def test_short_order_plan_rolls_down_to_next_shortable_candidate(self):
        scored = pd.DataFrame(
            [
                {"ts_code": "000001.SZ", "final_score": 0.95, "factor_score": 1.0, "nl_score": 0.3, "reason": "long", "source_artifacts": [], "hard_excluded": False},
                {"ts_code": "000002.SZ", "final_score": -0.98, "factor_score": -1.0, "nl_score": -0.4, "reason": "not shortable", "source_artifacts": [], "hard_excluded": False},
                {"ts_code": "000003.SZ", "final_score": -0.90, "factor_score": -0.9, "nl_score": -0.3, "reason": "shortable", "source_artifacts": [], "hard_excluded": False},
                {"ts_code": "000004.SZ", "final_score": -0.80, "factor_score": -0.8, "nl_score": -0.2, "reason": "backup", "source_artifacts": [], "hard_excluded": False},
            ]
        )
        plan = build_order_plan(
            scored,
            long_threshold=0.7,
            short_threshold=-0.7,
            max_total_holdings=2,
            max_single_name_weight=0.5,
            shortable_codes=frozenset({"000003.SZ", "000004.SZ"}),
        )
        self.assertEqual(list(plan["ts_code"]), ["000001.SZ", "000003.SZ"])
        self.assertNotIn("000002.SZ", set(plan["ts_code"]))
        self.assertEqual(dict(zip(plan["ts_code"], plan["side"]))["000003.SZ"], "short")

    def test_replay_and_stats(self):
        plan = pd.DataFrame(
            [
                {"ts_code": "000001.SZ", "side": "long", "target_weight": 0.1, "final_score": 0.9, "factor_score": 1.0, "nl_score": 0.5, "reason": "a", "source_artifacts": []},
                {"ts_code": "000002.SZ", "side": "short", "target_weight": -0.1, "final_score": -0.9, "factor_score": -1.0, "nl_score": -0.5, "reason": "b", "source_artifacts": []},
            ]
        )
        result = run_fixed_holding_replay(
            plan, REPLAY, BrokerProfile(), decision_time_iso="2022-01-04T09:25:00+08:00", shortable_codes=frozenset({"000002.SZ"})
        )
        stats = compute_return_stats(result)
        self.assertGreater(stats["total_return"], 0.0)
        self.assertGreater(stats["long_return"], 0.0)
        self.assertGreater(stats["short_return"], 0.0)
        self.assertEqual(stats["holdings_count"], 2)
        self.assertEqual(stats["order_status_counts"], {"filled": 2})
        self.assertIn("equity_curve", stats)


class CandidateIsolationTest(unittest.TestCase):
    def test_snapshot_slots_are_hidden_and_restored_during_candidate_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = SandboxPaths(Path(tmp) / "mnt")
            for slot in (paths.train, paths.valid, paths.test):
                slot.mkdir(parents=True)
                slot.chmod(0o755)

            with hide_snapshot_slots_from_agent(paths):
                self.assertEqual(stat.S_IMODE(paths.train.stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE(paths.valid.stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE(paths.test.stat().st_mode), 0o700)

            self.assertEqual(stat.S_IMODE(paths.train.stat().st_mode), 0o755)
            self.assertEqual(stat.S_IMODE(paths.valid.stat().st_mode), 0o755)
            self.assertEqual(stat.S_IMODE(paths.test.stat().st_mode), 0o755)


if __name__ == "__main__":
    unittest.main()
