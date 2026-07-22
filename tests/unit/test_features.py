# Consolidated unit tests: test_environment.py


# Source: test_auction_correction.py
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from autotrade.environment.data import PITDataStore
from autotrade.environment.data import (
    FundamentalEventsBuilder,
    FundamentalEventsConfig,
    audit_fundamental_events,
    month_aligned_replace_window,
)
from autotrade.environment.data.auction import (
    AuctionCorrectionConfig,
    apply_open_auction_correction,
    is_open_auction_time,
    market_bucket,
)


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

    def test_vectorized_auction_correction_matches_scalar_contract(self):
        frame = pd.DataFrame(
            {
                "ts_code": [" 000001.sz ", "300001.SZ", "600000.SH", "688001.SH", "430001.BJ", None, 0],
                "trade_time": [
                    " 2026-05-29 09:30:00 ",
                    "2026-05-29 09:31:00",
                    "09:30",
                    None,
                    "2026-05-29 09:30:59",
                    "2026-05-29 09:30:00",
                    930,
                ],
                "vol": ["100", "bad", 300, None, 500, 600, 700],
                "amount": [200, 400, "600", 800, None, 1200, 1400],
            },
            index=[9, 7, 5, 3, 1, -1, -3],
        )
        original = frame.copy(deep=True)
        config = AuctionCorrectionConfig(
            volume_factors={"sz_main_00": 0.5, "sh_main_60": 0.25, "other": 0.9},
            amount_factors={"sz_main_00": 0.75, "bj": 0.1, "other": 0.8},
        )

        out = apply_open_auction_correction(frame, config)

        expected_buckets = frame["ts_code"].map(market_bucket)
        expected_open = frame["trade_time"].map(is_open_auction_time)
        expected_vol_factors = pd.Series(
            [config.volume_factors.get(bucket, 1.0) if opened else 1.0 for bucket, opened in zip(expected_buckets, expected_open)],
            index=frame.index,
        )
        expected_amount_factors = pd.Series(
            [config.amount_factors.get(bucket, 1.0) if opened else 1.0 for bucket, opened in zip(expected_buckets, expected_open)],
            index=frame.index,
        )
        pd.testing.assert_frame_equal(frame, original)
        pd.testing.assert_series_equal(out["auction_market_bucket"], expected_buckets, check_names=False)
        pd.testing.assert_series_equal(out["auction_open_bar"], expected_open.astype(bool), check_names=False)
        pd.testing.assert_series_equal(out["auction_vol_correction_factor"], expected_vol_factors, check_names=False)
        pd.testing.assert_series_equal(out["auction_amount_correction_factor"], expected_amount_factors, check_names=False)
        pd.testing.assert_series_equal(
            out["vol_pit"], pd.to_numeric(frame["vol"], errors="coerce") * expected_vol_factors, check_names=False
        )
        pd.testing.assert_series_equal(
            out["amount_pit"], pd.to_numeric(frame["amount"], errors="coerce") * expected_amount_factors, check_names=False
        )



# Source: test_pit_store.py
class PITDataStoreTest(unittest.TestCase):
    def test_pit_store_handles_reversed_ranges_without_partition_reads(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = PITDataStore(Path(tmp))
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

    def test_forecast_revision_visible_only_from_its_own_ann_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "raw"
            forecast_dir = raw / "forecast_vip"
            forecast_dir.mkdir(parents=True)
            pd.DataFrame([
                {
                    "ts_code": "600157.SH",
                    "ann_date": "20200110",
                    "first_ann_date": "20200110",
                    "end_date": "20191231",
                    "type": "预增",
                    "update_flag": "0",
                },
                {
                    "ts_code": "600157.SH",
                    "ann_date": "20200125",
                    "first_ann_date": "20200110",
                    "end_date": "20191231",
                    "type": "预增",
                    "update_flag": "1",
                },
            ]).to_parquet(forecast_dir / "ann_month=202001.parquet", index=False)

            builder = FundamentalEventsBuilder(raw)
            config = FundamentalEventsConfig(start_date="20200101", end_date="20200131", datasets=("forecast_vip",))
            events = builder.build(config).sort_values("available_at").reset_index(drop=True)

            self.assertEqual(list(events["available_at_rule"]), ["source:ann_date", "source:ann_date"])
            self.assertEqual(
                list(events["available_at"]),
                ["2020-01-10T18:00:00+08:00", "2020-01-25T18:00:00+08:00"],
            )

            output = Path(tmp) / "pit"
            builder.write_partitioned(events, output)
            # blank source_hash (fixture has no .meta.json sidecars) keeps this at warning
            self.assertEqual(audit_fundamental_events(output, config)["status"], "warning")

            # Backdating a revision to first_ann_date is determinate lookahead
            # and must hard-fail the audit.
            path = output / "forecast_vip" / "available_month=202001.parquet"
            tampered = pd.read_parquet(path)
            tampered.loc[tampered["update_flag"] == "1", "available_at"] = "2020-01-10T18:00:00+08:00"
            tampered.to_parquet(path, index=False)
            report = audit_fundamental_events(output, config)
            self.assertEqual(report["status"], "error")
            self.assertEqual(report["findings"][-1]["details"]["backdated_available_at_rows"], 1)

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

            aligned_start, replace_months = month_aligned_replace_window("20200115", "20200131")
            self.assertEqual(aligned_start, "20200101")
            self.assertEqual(replace_months, {"202001"})
            builder.write_partitioned(update, output, replace_months=replace_months)
            replaced = pd.read_parquet(output / "dividend" / "available_month=202001.parquet")
            self.assertEqual(set(replaced["business_key"]), {"new"})

            builder.write_partitioned(
                pd.DataFrame(),
                output,
                replace_months=replace_months,
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
            details = report["findings"][-1]["details"]
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
            self.assertIn(
                "fundamental_events_partitions",
                [finding["check"] for finding in required_report["findings"]],
            )


class UnitRegistryProjectionTest(unittest.TestCase):
    def test_source_unit_rules_is_the_single_projection_source(self):
        from autotrade.data_sources.tushare.audit import (
            board_unit_rules,
            event_unit_rules,
            integrated_unit_rules,
            macro_unit_rules,
        )
        from autotrade.environment.data.units import AGENT_UNIT_CONTRACT, SOURCE_UNIT_RULES

        # Every registry rule is a non-empty description.
        for key, rule in SOURCE_UNIT_RULES.items():
            self.assertTrue(isinstance(rule, str) and rule, key)
        # The Agent contract ships the registry itself (data_summary carries it
        # into the sandbox, so offline Fold Agents can resolve source units).
        self.assertIs(AGENT_UNIT_CONTRACT["source_unit_rules"], SOURCE_UNIT_RULES)
        # Audit report metadata is a projection of the same registry.
        for rules in (macro_unit_rules(), event_unit_rules(), board_unit_rules(), integrated_unit_rules()):
            for key, rule in rules.items():
                self.assertEqual(rule, SOURCE_UNIT_RULES[key], key)
