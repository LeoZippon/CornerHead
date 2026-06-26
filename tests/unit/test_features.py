# Consolidated unit tests: test_environment.py


# Source: test_auction_correction.py
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from autotrade.environment.data import PITDataStore, default_tushare_contracts
from autotrade.environment.features import (
    FundamentalEventsBuilder,
    FundamentalEventsConfig,
    audit_fundamental_events,
    complete_months_for_date_window,
)
from autotrade.environment.features.auction import apply_open_auction_correction, market_bucket


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



# Source: test_pit_store.py
class PITDataStoreTest(unittest.TestCase):
    def test_pit_store_handles_reversed_ranges_without_partition_reads(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = PITDataStore(Path(tmp), default_tushare_contracts())
            frame = store.read_trade_range("daily", "20200103", "20200101", columns=["trade_date", "ts_code"])
            self.assertTrue(frame.empty)
            self.assertEqual(list(frame.columns), ["trade_date", "ts_code"])


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

            output = Path(tmp) / "pit" / "fundamental_events"
            written = builder.write_partitioned(events, output)
            self.assertEqual(len(written), 3)
            report = audit_fundamental_events(
                output,
                FundamentalEventsConfig(start_date="20200101", end_date="20200131", datasets=("income_vip", "dividend", "fina_mainbz_vip")),
            )
            self.assertEqual(report["status"], "warning")

    def test_fundamental_events_merge_partial_month_and_replace_complete_month(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "pit"
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
