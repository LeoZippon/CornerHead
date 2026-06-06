# Consolidated unit tests: test_environment.py


# Source: test_auction_correction.py
import unittest

import pandas as pd

from hl_trader.environment.features.auction import apply_open_auction_correction, market_bucket


class AuctionCorrectionTest(unittest.TestCase):
    def test_market_bucket(self):
        self.assertEqual(market_bucket("000001.SZ"), "sz_main_00")
        self.assertEqual(market_bucket("300001.SZ"), "sz_gem_30")
        self.assertEqual(market_bucket("600000.SH"), "sh_main_60")
        self.assertEqual(market_bucket("688001.SH"), "sh_star_68")
        self.assertEqual(market_bucket("430001.BJ"), "bj")

    def test_apply_open_auction_correction_only_adjusts_sz_0930(self):
        frame = pd.DataFrame(
            [
                {"ts_code": "000001.SZ", "trade_time": "2026-05-29 09:30:00", "vol": 1000.0, "amount": 2000.0},
                {"ts_code": "300001.SZ", "trade_time": "2026-05-29 09:30:00", "vol": 1000.0, "amount": 2000.0},
                {"ts_code": "600000.SH", "trade_time": "2026-05-29 09:30:00", "vol": 1000.0, "amount": 2000.0},
                {"ts_code": "000001.SZ", "trade_time": "2026-05-29 15:00:00", "vol": 1000.0, "amount": 2000.0},
            ]
        )

        out = apply_open_auction_correction(frame)

        self.assertAlmostEqual(out.loc[0, "vol_pit"], 760.0)
        self.assertAlmostEqual(out.loc[0, "amount_pit"], 1520.0)
        self.assertAlmostEqual(out.loc[1, "vol_pit"], 580.0)
        self.assertAlmostEqual(out.loc[1, "amount_pit"], 1160.0)
        self.assertAlmostEqual(out.loc[2, "vol_pit"], 1000.0)
        self.assertAlmostEqual(out.loc[3, "vol_pit"], 1000.0)
        self.assertEqual(out.loc[0, "auction_correction_rule"], "minute_0930_to_live_stk_auction_by_market_bucket")
        self.assertEqual(out.loc[2, "auction_correction_rule"], "none")


# Source: test_broker.py
import unittest
from datetime import date

from hl_trader.environment.backtest import DailyReplayEngine
from hl_trader.environment.execution import BrokerSimulator, Order, PortfolioState, Position
from hl_trader.environment.schemas import TradeStrategyPolicy


class ListLedger:
    def __init__(self):
        self.events = []

    def append(self, event):
        self.events.append(event)


class BrokerSimulatorTest(unittest.TestCase):
    def test_broker_respects_lot_cash_and_t1_settlement(self):
        broker = BrokerSimulator(TradeStrategyPolicy(policy_id="p"))
        state = PortfolioState(cash=10_000.0)
        fill = broker.execute_order(state, Order(date(2020, 1, 2), "000001.SZ", "buy", 1000), 10.0)
        self.assertIsNotNone(fill)
        self.assertEqual(fill.shares, 900)
        self.assertEqual(state.positions["000001.SZ"].available_shares, 0)
        blocked = broker.execute_order(state, Order(date(2020, 1, 2), "000001.SZ", "sell", 100), 10.0)
        self.assertIsNone(blocked)
        broker.settle_t_plus_1(state)
        sold = broker.execute_order(state, Order(date(2020, 1, 3), "000001.SZ", "sell", 100), 10.0)
        self.assertIsNotNone(sold)
        self.assertEqual(sold.shares, 100)

    def test_broker_blocks_limit_and_suspended(self):
        broker = BrokerSimulator(TradeStrategyPolicy(policy_id="p"))
        state = PortfolioState(cash=100_000.0)
        self.assertIsNone(broker.execute_order(state, Order(date(2020, 1, 2), "000001.SZ", "buy", 100), 10.0, up_limit=10.0))
        self.assertIsNone(broker.execute_order(state, Order(date(2020, 1, 2), "000001.SZ", "buy", 100), 9.9, suspended=True))
        state.positions["000001.SZ"] = Position("000001.SZ", shares=100, available_shares=100, cost_basis=10.0)
        self.assertIsNone(broker.execute_order(state, Order(date(2020, 1, 2), "000001.SZ", "sell", 100), 9.0, down_limit=9.0))

    def test_broker_honors_limit_price_and_rejects_bad_price(self):
        broker = BrokerSimulator(TradeStrategyPolicy(policy_id="p"))
        state = PortfolioState(cash=100_000.0)
        blocked = broker.execute_order(state, Order(date(2020, 1, 2), "000001.SZ", "buy", 100, limit_price=9.9), 10.0)
        self.assertIsNone(blocked)
        fill = broker.execute_order(state, Order(date(2020, 1, 2), "000001.SZ", "buy", 100, limit_price=10.0), 10.0)
        self.assertIsNotNone(fill)
        broker.settle_t_plus_1(state)
        blocked_sell = broker.execute_order(state, Order(date(2020, 1, 3), "000001.SZ", "sell", 100, limit_price=10.1), 10.0)
        self.assertIsNone(blocked_sell)
        with self.assertRaisesRegex(ValueError, "execution price"):
            broker.execute_order(state, Order(date(2020, 1, 3), "000001.SZ", "buy", 100), 0.0)

    def test_daily_replay_requires_chronological_dates_and_matching_orders(self):
        engine = DailyReplayEngine(BrokerSimulator(TradeStrategyPolicy(policy_id="p")))
        with self.assertRaisesRegex(ValueError, "strictly increasing"):
            engine.run(
                trade_dates=[date(2020, 1, 3), date(2020, 1, 2)],
                initial_state=PortfolioState(cash=10_000.0),
                decide=lambda trade_date, state: [],
                price_for=lambda trade_date, ts_code: 10.0,
            )
        with self.assertRaisesRegex(ValueError, "order.trade_date"):
            engine.run(
                trade_dates=[date(2020, 1, 2)],
                initial_state=PortfolioState(cash=10_000.0),
                decide=lambda trade_date, state: [Order(date(2020, 1, 3), "000001.SZ", "buy", 100)],
                price_for=lambda trade_date, ts_code: 10.0,
            )

    def test_daily_replay_logs_cash_exposure_and_equity(self):
        ledger = ListLedger()
        engine = DailyReplayEngine(BrokerSimulator(TradeStrategyPolicy(policy_id="p")), ledger=ledger)
        state = PortfolioState(
            cash=1_000.0,
            positions={"000001.SZ": Position("000001.SZ", shares=100, available_shares=100, cost_basis=10.0)},
        )
        engine.run(
            trade_dates=[date(2020, 1, 2)],
            initial_state=state,
            decide=lambda trade_date, state: [],
            price_for=lambda trade_date, ts_code: 10.0,
        )
        close_events = [event for event in ledger.events if event["event_type"] == "daily_close"]
        self.assertEqual(close_events[-1]["cash"], 1_000.0)
        self.assertEqual(close_events[-1]["gross_exposure"], 1_000.0)
        self.assertEqual(close_events[-1]["equity"], 2_000.0)
        self.assertEqual(close_events[-1]["positions"], 1)


# Source: test_contracts_and_config.py
import unittest
from datetime import date
from pathlib import Path

from hl_trader.environment.data import default_tushare_contracts
from hl_trader.environment.schemas import load_experiment_config


class ContractsAndConfigTest(unittest.TestCase):
    def test_daily_contract_is_next_day_tradable(self):
        daily = default_tushare_contracts()["daily"]
        self.assertEqual(daily.tradable_from(date(2020, 1, 2)), date(2020, 1, 3))
        self.assertEqual(daily.available_at(date(2020, 1, 2)).hour, 17)

    def test_load_pilot_config(self):
        cfg = load_experiment_config(Path("configs/experiments/pilot_2020_daily.yaml"))
        self.assertEqual(cfg.experiment_id, "pilot_2020_daily_value_quality")
        self.assertEqual(cfg.protocol.start_date.isoformat(), "2020-01-02")
        self.assertEqual(cfg.protocol.nl_weight, 0.0)
        self.assertTrue(cfg.trade_policy.allows("rebalance"))


# Source: test_daily_pit_features.py
import math
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from hl_trader.environment.data import PITDataStore, default_tushare_contracts
from hl_trader.environment.features import (
    DailyPITFeatureBuilder,
    FeatureBuildConfig,
    FundamentalEventsBuilder,
    FundamentalEventsConfig,
    audit_fundamental_events,
    complete_months_for_date_window,
)
from hl_trader.environment.leakage import assert_no_feature_leakage


class DailyPITFeatureBuilderTest(unittest.TestCase):
    def test_builds_next_day_tradable_features(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp)
            dates = pd.date_range("2020-01-01", periods=30, freq="B").strftime("%Y%m%d").tolist()
            self._write_p1_fixture(raw, dates)

            builder = DailyPITFeatureBuilder(raw)
            features = builder.build(FeatureBuildConfig(start_date=dates[20], end_date=dates[25], lookback_days=25))

            self.assertFalse(features.empty)
            self.assertIn("ret_20d", features.columns)
            self.assertIn("source_trade_date", features.columns)
            self.assertIn("tradable_date", features.columns)
            self.assertIn("result_available_time", features.columns)
            self.assertNotIn("adj_close", features.columns)
            self.assertEqual(features["feature_date"].min(), dates[20])
            self.assertGreater(features["tradable_date"].min(), features["feature_date"].min())
            self.assertTrue((features["source_trade_date"] == features["feature_date"]).all())
            self.assertTrue((features["result_available_time"] == features["available_at"]).all())
            assert_no_feature_leakage(features)

    def test_limit_list_d_raw_only_fields_are_quarantined_from_daily_alpha(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp)
            dates = pd.date_range("2020-01-01", periods=6, freq="B").strftime("%Y%m%d").tolist()
            self._write_p1_fixture(raw, dates)
            pd.DataFrame(
                [
                    {
                        "trade_date": dates[2],
                        "ts_code": "000001.SZ",
                        "limit": "U",
                        "limit_amount": 1_650_376_910,
                        "fd_amount": 980_000_000,
                        "first_time": "09:31:00",
                        "last_time": "14:56:00",
                        "open_times": 3,
                        "strth": "strong",
                        "limit_order": 12_300,
                    }
                ]
            ).to_parquet(raw / "limit_list_d" / f"trade_date={dates[2]}.parquet", index=False)

            features = DailyPITFeatureBuilder(raw).build(
                FeatureBuildConfig(start_date=dates[2], end_date=dates[3], lookback_days=0)
            )

            self.assertIn("limit", features.columns)
            for raw_only_column in DailyPITFeatureBuilder.LIMIT_LIST_D_RAW_ONLY_COLUMNS:
                self.assertNotIn(raw_only_column, features.columns)
                self.assertNotIn(f"limit_list_d_{raw_only_column}", features.columns)
            row = features[(features["feature_date"] == dates[2]) & (features["ts_code"] == "000001.SZ")].iloc[0]
            self.assertEqual(row["limit"], "U")

    def test_last_daily_feature_uses_trade_cal_for_next_tradable_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp)
            dates = ["20200601", "20200602", "20200603"]
            self._write_p1_fixture(raw, dates)
            calendar = raw / "trade_cal" / "exchange=SSE" / "year=2020.parquet"
            calendar.parent.mkdir(parents=True, exist_ok=True)
            pd.DataFrame(
                [
                    {"cal_date": "20200601", "is_open": "1"},
                    {"cal_date": "20200602", "is_open": "1"},
                    {"cal_date": "20200603", "is_open": "1"},
                    {"cal_date": "20200604", "is_open": "1"},
                ]
            ).to_parquet(calendar, index=False)

            features = DailyPITFeatureBuilder(raw).build(
                FeatureBuildConfig(start_date="20200603", end_date="20200603", lookback_days=2)
            )

            self.assertFalse(features.empty)
            self.assertEqual(set(features["feature_date"]), {"20200603"})
            self.assertEqual(set(features["tradable_date"]), {"20200604"})

    def test_return_features_use_published_pct_change_not_snapshot_adjustment(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp)
            dates = pd.date_range("2020-01-01", periods=30, freq="B").strftime("%Y%m%d").tolist()
            self._write_p1_fixture(raw, dates, pct_chg_by_code={"000001.SZ": 5.0, "000002.SZ": 2.5})

            features = DailyPITFeatureBuilder(raw).build(
                FeatureBuildConfig(start_date=dates[20], end_date=dates[21], lookback_days=25)
            )

            row = features[(features["feature_date"] == dates[20]) & (features["ts_code"] == "000001.SZ")].iloc[0]
            self.assertAlmostEqual(row["ret_1d"], 0.05, places=12)
            self.assertAlmostEqual(row["ret_5d"], (1.05 ** 5) - 1.0, places=12)

    def test_rolling_volatility_does_not_bleed_across_symbols(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp)
            dates = pd.date_range("2020-01-01", periods=8, freq="B").strftime("%Y%m%d").tolist()
            self._write_p1_fixture(raw, dates, pct_chg_by_code={"000001.SZ": 8.0, "000002.SZ": 1.0})

            features = DailyPITFeatureBuilder(raw).build(
                FeatureBuildConfig(start_date=dates[0], end_date=dates[6], lookback_days=0)
            )

            early_second_symbol = features[
                (features["feature_date"] == dates[1]) & (features["ts_code"] == "000002.SZ")
            ].iloc[0]
            self.assertTrue(math.isnan(early_second_symbol["volatility_20d"]))

    def test_duplicate_core_keys_fail_fast(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp)
            dates = pd.date_range("2020-01-01", periods=6, freq="B").strftime("%Y%m%d").tolist()
            self._write_p1_fixture(raw, dates, duplicate_daily_basic=True)

            with self.assertRaisesRegex(ValueError, "daily_basic duplicate"):
                DailyPITFeatureBuilder(raw).build(
                    FeatureBuildConfig(start_date=dates[0], end_date=dates[4], lookback_days=0)
                )

    def test_joins_fundamental_event_features_when_provided(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = root / "raw"
            events_root = root / "features" / "fundamental_events"
            dates = pd.date_range("2020-01-01", periods=6, freq="B").strftime("%Y%m%d").tolist()
            self._write_p1_fixture(raw, dates)
            event_path = events_root / "fina_indicator_vip" / "available_month=202001.parquet"
            event_path.parent.mkdir(parents=True, exist_ok=True)
            pd.DataFrame([
                {
                    "dataset": "fina_indicator_vip",
                    "ts_code": "000001.SZ",
                    "end_date": "20191231",
                    "ann_date": "20200102",
                    "available_at": "2020-01-02T18:00:00+08:00",
                    "available_month": "202001",
                    "business_key": "k1",
                    "roe": 12.5,
                }
            ]).to_parquet(event_path, index=False)

            features = DailyPITFeatureBuilder(raw).build(
                FeatureBuildConfig(
                    start_date=dates[1],
                    end_date=dates[2],
                    lookback_days=0,
                    fundamental_events_dir=events_root,
                )
            )

            row = features[(features["feature_date"] == dates[2]) & (features["ts_code"] == "000001.SZ")].iloc[0]
            self.assertEqual(row["fund_latest_end_date"], "20191231")
            self.assertAlmostEqual(row["fund_roe"], 12.5)

    def test_pit_store_handles_reversed_ranges_without_partition_reads(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp)
            dates = pd.date_range("2020-01-01", periods=3, freq="B").strftime("%Y%m%d").tolist()
            self._write_p1_fixture(raw, dates)

            store = PITDataStore(raw, default_tushare_contracts())
            frame = store.read_trade_range("daily", dates[-1], dates[0], columns=["trade_date", "ts_code"])
            self.assertTrue(frame.empty)
            self.assertEqual(list(frame.columns), ["trade_date", "ts_code"])

    def _write_p1_fixture(
        self,
        raw: Path,
        dates: list[str],
        pct_chg_by_code: dict[str, float] | None = None,
        duplicate_daily_basic: bool = False,
    ) -> None:
        pct_chg_by_code = pct_chg_by_code or {"000001.SZ": 5.0, "000002.SZ": 2.5}
        for ds in ["daily", "daily_basic", "stk_limit", "suspend_d", "limit_list_d"]:
            (raw / ds).mkdir(parents=True)
        for i, trade_date in enumerate(dates):
            pd.DataFrame({
                "ts_code": ["000001.SZ", "000002.SZ"],
                "trade_date": [trade_date, trade_date],
                "open": [10 + i, 20 + i],
                "high": [11 + i, 21 + i],
                "low": [9 + i, 19 + i],
                "close": [10.5 + i, 20.5 + i],
                "pre_close": [10 + i, 20 + i],
                "change": [0.5, 0.5],
                "pct_chg": [pct_chg_by_code["000001.SZ"], pct_chg_by_code["000002.SZ"]],
                "vol": [1000, 2000],
                "amount": [10000 + i, 20000 + i],
            }).to_parquet(raw / "daily" / f"trade_date={trade_date}.parquet", index=False)

            daily_basic = pd.DataFrame({
                "ts_code": ["000001.SZ", "000002.SZ"],
                "trade_date": [trade_date, trade_date],
                "turnover_rate": [1.0, 2.0],
                "turnover_rate_f": [1.1, 2.1],
                "pe": [8.0, 12.0],
                "pe_ttm": [9.0, 13.0],
                "pb": [0.8, 1.2],
                "ps_ttm": [1.0, 2.0],
                "dv_ratio": [3.0, 1.0],
                "total_share": [100000, 200000],
                "float_share": [90000, 190000],
                "free_share": [80000, 180000],
                "total_mv": [1000000, 2000000],
                "circ_mv": [900000, 1900000],
            })
            if duplicate_daily_basic and trade_date == dates[0]:
                daily_basic = pd.concat([daily_basic, daily_basic.iloc[[0]]], ignore_index=True)
            daily_basic.to_parquet(raw / "daily_basic" / f"trade_date={trade_date}.parquet", index=False)

            pd.DataFrame({
                "trade_date": [trade_date, trade_date],
                "ts_code": ["000001.SZ", "000002.SZ"],
                "pre_close": [10 + i, 20 + i],
                "up_limit": [11 + i, 22 + i],
                "down_limit": [9 + i, 18 + i],
            }).to_parquet(raw / "stk_limit" / f"trade_date={trade_date}.parquet", index=False)
            pd.DataFrame({"trade_date": [], "ts_code": []}).to_parquet(raw / "suspend_d" / f"trade_date={trade_date}.parquet", index=False)
            pd.DataFrame({"trade_date": [], "ts_code": [], "limit": []}).to_parquet(raw / "limit_list_d" / f"trade_date={trade_date}.parquet", index=False)


class FundamentalEventsBuilderTest(unittest.TestCase):
    def test_builds_available_month_events_and_audit_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "raw"
            raw.mkdir()
            income_dir = raw / "income_vip"
            dividend_dir = raw / "dividend"
            mainbz_dir = raw / "fina_mainbz_vip"
            income_dir.mkdir()
            dividend_dir.mkdir()
            mainbz_dir.mkdir()
            pd.DataFrame([
                {
                    "ts_code": "000001.SZ",
                    "ann_date": "20200103",
                    "f_ann_date": "20200102",
                    "end_date": "20191231",
                    "report_type": "1",
                    "comp_type": "1",
                    "end_type": "4",
                }
            ]).to_parquet(income_dir / "period=20191231.parquet", index=False)
            pd.DataFrame([
                {
                    "ts_code": "000001.SZ",
                    "end_date": "20191231",
                    "ann_date": "",
                    "imp_ann_date": "20200104",
                    "ex_date": "20200110",
                    "record_date": "20200109",
                    "pay_date": "20200111",
                    "div_proc": "实施",
                    "cash_div_tax": 0.1,
                },
                {
                    "ts_code": "000002.SZ",
                    "end_date": "20191231",
                    "ann_date": "",
                    "imp_ann_date": "",
                    "ex_date": "20200110",
                    "record_date": "20200109",
                    "pay_date": "20200111",
                    "div_proc": "实施",
                    "cash_div_tax": 0.2,
                },
            ]).to_parquet(dividend_dir / "ts_code=000001.SZ.parquet", index=False)
            pd.DataFrame([
                {
                    "ts_code": "000001.SZ",
                    "end_date": "20191231",
                    "bz_item": "产品A",
                    "bz_code": "A",
                    "curr_type": "CNY",
                }
            ]).to_parquet(mainbz_dir / "ts_code=000001.SZ.parquet", index=False)

            builder = FundamentalEventsBuilder(raw)
            events = builder.build(FundamentalEventsConfig(
                start_date="20200101",
                end_date="20200131",
                datasets=("income_vip", "dividend", "fina_mainbz_vip"),
            ))

            self.assertEqual(set(events["dataset"]), {"income_vip", "dividend", "fina_mainbz_vip"})
            self.assertNotIn("000002.SZ", set(events["ts_code"]))
            self.assertTrue((events["available_month"] == "202001").all())
            mainbz = events[events["dataset"] == "fina_mainbz_vip"].iloc[0]
            self.assertEqual(mainbz["available_at_rule"], "fallback_joined_statement_available_at")

            output = Path(tmp) / "features" / "fundamental_events"
            written = builder.write_partitioned(events, output)
            self.assertEqual(len(written), 3)
            report = audit_fundamental_events(
                output,
                FundamentalEventsConfig(start_date="20200101", end_date="20200131", datasets=("income_vip", "dividend", "fina_mainbz_vip")),
            )
            self.assertEqual(report["status"], "warning")

    def test_fundamental_events_merge_partial_month_and_replace_complete_month(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "features"
            builder = FundamentalEventsBuilder(Path(tmp) / "raw")
            original = pd.DataFrame([
                {
                    "dataset": "dividend",
                    "ts_code": "000001.SZ",
                    "available_at": "2020-01-02T18:00:00+08:00",
                    "available_at_rule": "source:imp_ann_date_or_ann_date",
                    "available_month": "202001",
                    "business_key": "old",
                    "source_path": "/raw/dividend/ts_code=000001.SZ.parquet",
                    "source_hash": "old",
                    "source_row_id": 0,
                }
            ])
            update = original.assign(ts_code="000002.SZ", business_key="new", source_hash="new")

            builder.write_partitioned(original, output)
            builder.write_partitioned(update, output)
            merged = pd.read_parquet(output / "dividend" / "available_month=202001.parquet")
            self.assertEqual(set(merged["business_key"]), {"old", "new"})

            builder.write_partitioned(update, output, replace_months=complete_months_for_date_window("20200101", "20200131"))
            replaced = pd.read_parquet(output / "dividend" / "available_month=202001.parquet")
            self.assertEqual(set(replaced["business_key"]), {"new"})

            builder.write_partitioned(
                pd.DataFrame(),
                output,
                replace_months=complete_months_for_date_window("20200101", "20200131"),
                replace_datasets=("dividend",),
            )
            self.assertFalse((output / "dividend" / "available_month=202001.parquet").exists())

    def test_fundamental_event_audit_rejects_dangerous_rules_and_wrong_source_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "events"
            path = root / "dividend" / "available_month=202001.parquet"
            path.parent.mkdir(parents=True)
            pd.DataFrame([
                {
                    "dataset": "dividend",
                    "ts_code": "000001.SZ",
                    "available_at": "2020-01-02T18:00:00+08:00",
                    "available_at_rule": "source:imp_ann_date_or_ann_date:ex_date",
                    "available_month": "202001",
                    "business_key": "bad",
                    "source_path": "/raw/fina_indicator_vip/period=20191231.parquet",
                    "source_hash": "hash",
                    "source_row_id": 0,
                }
            ]).to_parquet(path, index=False)

            report = audit_fundamental_events(
                root,
                FundamentalEventsConfig(start_date="20200101", end_date="20200131", datasets=("dividend",)),
            )

            self.assertEqual(report["status"], "error")
            details = report["checks"][-1]["details"]
            self.assertEqual(details["disallowed_available_at_rule_rows"], 1)
            self.assertEqual(details["wrong_source_path_rows"], 1)

    def test_fundamental_event_audit_can_require_partitions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "events"
            config = FundamentalEventsConfig(start_date="20200101", end_date="20200131", datasets=("dividend",))

            warning_report = audit_fundamental_events(root, config)
            required_report = audit_fundamental_events(root, config, require_partitions=True)

            self.assertEqual(warning_report["status"], "warning")
            self.assertEqual(required_report["status"], "error")
            self.assertIn("fundamental_events_partitions", [check["check"] for check in required_report["checks"]])


# Source: test_leakage_checks.py
import unittest

import pandas as pd

from hl_trader.environment.leakage import assert_no_feature_leakage, find_feature_leakage


class LeakageChecksTest(unittest.TestCase):
    def test_detects_same_day_tradable_date(self):
        frame = pd.DataFrame({
            "feature_date": ["20200102"],
            "tradable_date": ["20200102"],
            "available_at": ["2020-01-02T18:00:00+08:00"],
            "ts_code": ["000001.SZ"],
        })
        self.assertTrue(find_feature_leakage(frame))
        with self.assertRaises(AssertionError):
            assert_no_feature_leakage(frame)

    def test_accepts_next_day_feature(self):
        frame = pd.DataFrame({
            "feature_date": ["20200102"],
            "tradable_date": ["20200103"],
            "available_at": ["2020-01-02T18:00:00+08:00"],
            "ts_code": ["000001.SZ"],
        })
        assert_no_feature_leakage(frame)

    def test_accepts_timezone_aware_utc_available_at(self):
        frame = pd.DataFrame({
            "feature_date": ["2020-01-02"],
            "tradable_date": ["2020-01-03"],
            "available_at": ["2020-01-02T10:00:00+00:00"],
            "ts_code": ["000001.SZ"],
        })
        assert_no_feature_leakage(frame)

    def test_detects_available_at_before_feature_close(self):
        frame = pd.DataFrame({
            "feature_date": ["20200102"],
            "tradable_date": ["20200103"],
            "available_at": ["2020-01-02T09:30:00+08:00"],
            "ts_code": ["000001.SZ"],
        })
        violations = find_feature_leakage(frame)
        self.assertIn("available_at", {violation.check for violation in violations})

    def test_detects_future_source_trade_date(self):
        frame = pd.DataFrame({
            "feature_date": ["20200102"],
            "source_trade_date": ["20200103"],
            "tradable_date": ["20200106"],
            "available_at": ["2020-01-02T18:00:00+08:00"],
            "ts_code": ["000001.SZ"],
        })
        violations = find_feature_leakage(frame)
        self.assertIn("source_trade_date", {violation.check for violation in violations})

    def test_detects_duplicate_feature_key(self):
        frame = pd.DataFrame({
            "feature_date": ["20200102", "20200102"],
            "tradable_date": ["20200103", "20200103"],
            "available_at": ["2020-01-02T18:00:00+08:00", "2020-01-02T18:00:00+08:00"],
            "ts_code": ["000001.SZ", "000001.SZ"],
        })
        violations = find_feature_leakage(frame)
        self.assertIn("duplicate_key", {violation.check for violation in violations})
