import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from autotrade.environment.features.fundamental_events import read_fundamental_events
from autotrade.environment.snapshot import SnapshotBuilder, SnapshotConfig, load_snapshot_manifest, verify_snapshot_hash

CN_TZ = ZoneInfo("Asia/Shanghai")
DECISION = datetime(2021, 10, 8, 9, 25, tzinfo=CN_TZ)


def write(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(path, index=False)


def build_raw(raw: Path) -> None:
    calendar = pd.DataFrame({"cal_date": ["20210930", "20211008", "20211011"], "is_open": ["1", "1", "1"]})
    write(raw / "trade_cal" / "exchange=SSE" / "year=2021.parquet", calendar)
    for trade_date in ("20210930", "20211008"):
        write(
            raw / "daily" / f"trade_date={trade_date}.parquet",
            pd.DataFrame(
                [{"trade_date": trade_date, "ts_code": "000001.SZ", "open": 10.0, "high": 11.0, "low": 9.5, "close": 10.5, "pre_close": 10.0, "pct_chg": 5.0, "vol": 1000.0, "amount": 1050.0}]
            ),
        )
        write(
            raw / "daily_basic" / f"trade_date={trade_date}.parquet",
            pd.DataFrame([{"trade_date": trade_date, "ts_code": "000001.SZ", "turnover_rate": 2.0, "pe": 10.0, "total_share": 100.0, "total_mv": 1000.0}]),
        )
        write(
            raw / "stk_limit" / f"trade_date={trade_date}.parquet",
            pd.DataFrame([{"trade_date": trade_date, "ts_code": "000001.SZ", "up_limit": 11.55, "down_limit": 9.45}]),
        )
        write(
            raw / "adj_factor" / f"trade_date={trade_date}.parquet",
            pd.DataFrame([{"trade_date": trade_date, "ts_code": "000001.SZ", "adj_factor": 1.0}]),
        )
        write(raw / "suspend_d" / f"trade_date={trade_date}.parquet", pd.DataFrame(columns=["trade_date", "ts_code"]))
    write(
        raw / "stk_mins_1min_by_date" / "trade_date=20210930.parquet",
        pd.DataFrame(
            [
                {"ts_code": "000001.SZ", "trade_time": "2021-09-30 09:30:00", "open": 10.0, "high": 10.1, "low": 9.9, "close": 10.0, "vol": 20000.0, "amount": 200000.0, "trade_date": "20210930", "available_at": "2021-09-30T09:30:00+08:00", "available_at_rule": "bar_close"},
            ]
        ),
    )
    write(
        raw / "margin_secs" / "trade_date=20211008.parquet",
        pd.DataFrame([{"trade_date": "20211008", "ts_code": "000001.SZ", "available_at": "2021-10-08T09:00:00+08:00", "available_at_rule": "same_day_preopen"}]),
    )
    write(
        raw / "moneyflow" / "trade_date=20211008.parquet",
        pd.DataFrame([{"trade_date": "20211008", "ts_code": "000001.SZ", "net_mf_amount": 1.0, "available_at": "2021-10-08T19:00:00+08:00", "available_at_rule": "same_day_evening"}]),
    )
    write(
        raw / "cn_gdp" / "range=2020Q1_2021Q4.parquet",
        pd.DataFrame(
            [
                {"quarter": "2021Q2", "gdp": 1.0, "available_at": "2021-07-15T10:00:00+08:00", "available_at_rule": "release"},
                {"quarter": "2021Q3", "gdp": 1.1, "available_at": "2021-10-18T10:00:00+08:00", "available_at_rule": "release"},
            ]
        ),
    )
    write(
        raw / "cctv_news" / "date=20211007.parquet",
        pd.DataFrame([{"date": "20211007", "title": "新闻联播标题", "content": "正文内容", "available_at": "2021-10-07T19:30:00+08:00", "available_at_rule": "evening"}]),
    )
    write(
        raw / "stock_basic" / "list_status=L.parquet",
        pd.DataFrame(
            [{"ts_code": "000001.SZ", "name": "平安银行", "exchange": "SZSE", "list_date": "19910403", "delist_date": None}]
        ),
    )
    write(
        raw / "stock_basic" / "list_status=D.parquet",
        pd.DataFrame(
            [
                # Delisted AFTER the decision day: must stay in the as-of universe.
                {"ts_code": "000005.SZ", "name": "世纪星源", "exchange": "SZSE", "list_date": "19901210", "delist_date": "20240426"},
                # Delisted BEFORE the decision day: must be excluded.
                {"ts_code": "000003.SZ", "name": "PT金田A", "exchange": "SZSE", "list_date": "19910703", "delist_date": "20020614"},
            ]
        ),
    )
    write(
        raw / "namechange" / "namechange.parquet",
        pd.DataFrame([{"ts_code": "000001.SZ", "name": "平安银行", "start_date": "20120801", "end_date": "", "ann_date": "20120730", "change_reason": "改名"}]),
    )


def build_fundamental_events(root: Path) -> None:
    write(
        root / "income_vip" / "available_month=202109.parquet",
        pd.DataFrame(
            [
                {"dataset": "income_vip", "ts_code": "000001.SZ", "available_at": "2021-09-10T18:00:00+08:00", "available_at_rule": "source:f_ann_date_or_ann_date", "available_month": "202109", "business_key": "k1", "source_path": "x", "source_hash": "h", "source_row_id": 0},
            ]
        ),
    )


def write_fundamental_status(path: Path, *, status: str = "ok", errors: int = 0) -> None:
    path.write_text(json.dumps({"status": status, "errors": errors, "warnings": 0}), encoding="utf-8")


CONFIG = SnapshotConfig(
    events_datasets=("margin_secs", "moneyflow"),
    macro_datasets=("cn_gdp",),
    text_datasets=("cctv_news",),
    fundamental_datasets=("income_vip",),
    intraday_trade_days=1,
    include_industry=False,
)


class SnapshotBuilderTest(unittest.TestCase):
    def test_decision_snapshot_is_pit_filtered_and_unit_normalized(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "raw"
            events_root = Path(tmp) / "fund_events"
            build_raw(raw)
            build_fundamental_events(events_root)
            status_path = Path(tmp) / "fundamental_events_status.json"
            write_fundamental_status(status_path)
            out = Path(tmp) / "snap"
            builder = SnapshotBuilder(raw, events_root, status_path)
            manifest = builder.build_decision_snapshot(DECISION, out, CONFIG)

            daily = pd.read_parquet(out / "daily.parquet")
            # Same-day 20211008 daily data is not visible at the 09:25 decision.
            self.assertEqual(sorted(daily["trade_date"].unique()), ["20210930"])
            self.assertEqual(daily.loc[0, "vol"], 100000.0)  # 手 -> 股
            self.assertEqual(daily.loc[0, "amount"], 1050000.0)  # 千元 -> 元
            self.assertAlmostEqual(daily.loc[0, "pct_chg"], 0.05)
            self.assertAlmostEqual(daily.loc[0, "turnover_rate"], 0.02)

            intraday = pd.read_parquet(out / "intraday_1min.parquet")
            self.assertIn("auction_correction_rule", intraday.columns)

            events = pd.read_parquet(out / "events.parquet")
            datasets = set(events["dataset"])
            self.assertIn("margin_secs", datasets)  # same-day 09:00 visible at 09:25
            self.assertNotIn("moneyflow", datasets)  # same-day 19:00 is in the future

            macro = pd.read_parquet(out / "macro.parquet")
            self.assertEqual(list(macro["quarter"]), ["2021Q2"])

            text_index = pd.read_parquet(out / "text_index.parquet")
            self.assertEqual(len(text_index), 1)
            bodies = pd.read_parquet(out / "text_library" / text_index.loc[0, "library_file"])
            body_map = dict(zip(bodies["text_id"], bodies["body"]))
            self.assertIn("正文内容", body_map[text_index.loc[0, "text_id"]])

            fundamentals = pd.read_parquet(out / "fundamentals.parquet")
            self.assertEqual(len(fundamentals), 1)

            universe = pd.read_parquet(out / "universe.parquet")
            codes = set(universe["ts_code"])
            self.assertIn("000001.SZ", codes)
            self.assertIn("000005.SZ", codes)  # delisted 2024 -> alive at the 2021 decision
            self.assertNotIn("000003.SZ", codes)  # delisted 2002 -> excluded
            named = universe.set_index("ts_code")
            self.assertEqual(named.loc["000001.SZ", "name_asof"], "平安银行")

            self.assertEqual(manifest["kind"], "decision_input")
            verify_snapshot_hash(out)
            stored = load_snapshot_manifest(out)
            self.assertEqual(stored["snapshot_id"], manifest["snapshot_id"])
            self.assertIn("build_profile", stored)
            self.assertIn("data_profile", stored)
            self.assertIn("daily.parquet", stored["data_profile"]["files"])
            self.assertEqual(stored["data_profile"]["files"]["daily.parquet"]["rows"], len(daily))
            self.assertIn("build_seconds", stored["data_profile"]["files"]["fundamentals.parquet"])

    def test_daily_join_filters_each_dataset_by_own_availability(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "raw"
            build_raw(raw)
            out = Path(tmp) / "snap_after_close"
            config = SnapshotConfig(
                events_datasets=(),
                macro_datasets=(),
                text_datasets=(),
                fundamental_datasets=(),
                include_intraday=False,
                include_industry=False,
            )
            decision = datetime(2021, 10, 8, 17, 45, tzinfo=CN_TZ)

            SnapshotBuilder(raw, Path(tmp) / "fund_events").build_decision_snapshot(decision, out, config)

            daily = pd.read_parquet(out / "daily.parquet").set_index(["trade_date", "ts_code"])
            same_day = daily.loc[("20211008", "000001.SZ")]
            self.assertAlmostEqual(same_day["pct_chg"], 0.05)
            self.assertAlmostEqual(same_day["up_limit"], 11.55)
            self.assertTrue(pd.isna(same_day["turnover_rate"]))
            self.assertTrue(pd.isna(same_day["total_share"]))
            manifest = load_snapshot_manifest(out)
            self.assertIn("20211008", manifest["domains"]["daily"]["visible_trade_dates_by_dataset"]["daily"])
            self.assertNotIn("20211008", manifest["domains"]["daily"]["visible_trade_dates_by_dataset"]["daily_basic"])

    def test_decision_windows_are_configurable_by_domain(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "raw"
            events_root = Path(tmp) / "fund_events"
            build_raw(raw)
            build_fundamental_events(events_root)
            status_path = Path(tmp) / "fundamental_events_status.json"
            write_fundamental_status(status_path)
            out = Path(tmp) / "snap"
            config = SnapshotConfig(
                window_months=21,
                events_datasets=("margin_secs", "moneyflow"),
                macro_datasets=("cn_gdp",),
                text_datasets=("cctv_news",),
                fundamental_datasets=("income_vip",),
                daily_window_months=1,
                fundamentals_window_months=1,
                events_window_months=1,
                macro_window_months=2,
                text_window_months=1,
                include_intraday=False,
                include_industry=False,
            )

            manifest = SnapshotBuilder(raw, events_root, status_path).build_decision_snapshot(DECISION, out, config)

            daily = pd.read_parquet(out / "daily.parquet")
            self.assertEqual(sorted(daily["trade_date"].unique()), ["20210930"])
            fundamentals = pd.read_parquet(out / "fundamentals.parquet")
            self.assertEqual(len(fundamentals), 1)
            events = pd.read_parquet(out / "events.parquet")
            self.assertEqual(set(events["dataset"]), {"margin_secs"})
            macro = pd.read_parquet(out / "macro.parquet")
            self.assertEqual(len(macro), 0)
            text_index = pd.read_parquet(out / "text_index.parquet")
            self.assertEqual(len(text_index), 1)
            self.assertEqual(manifest["window_config"]["daily_months"], 1)
            self.assertEqual(manifest["window_config"]["macro_months"], 2)
            self.assertEqual(manifest["domain_windows"]["macro"]["window_months"], 2)

    def test_fundamental_event_reader_filters_partitions_by_min_available_at(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "fund_events"
            write(
                root / "income_vip" / "available_month=202001.parquet",
                pd.DataFrame(
                    [
                        {
                            "dataset": "income_vip",
                            "ts_code": "000001.SZ",
                            "available_at": "2020-01-10T18:00:00+08:00",
                            "available_at_rule": "source:f_ann_date_or_ann_date",
                            "available_month": "202001",
                            "business_key": "old",
                            "source_path": "x",
                            "source_hash": "h",
                            "source_row_id": 0,
                        }
                    ]
                ),
            )
            write(
                root / "income_vip" / "available_month=202109.parquet",
                pd.DataFrame(
                    [
                        {
                            "dataset": "income_vip",
                            "ts_code": "000001.SZ",
                            "available_at": "2021-09-10T18:00:00+08:00",
                            "available_at_rule": "source:f_ann_date_or_ann_date",
                            "available_month": "202109",
                            "business_key": "new",
                            "source_path": "x",
                            "source_hash": "h",
                            "source_row_id": 1,
                        }
                    ]
                ),
            )

            events = read_fundamental_events(
                root,
                "2021-10-08T09:25:00+08:00",
                datasets=("income_vip",),
                min_available_at="2021-09-01T00:00:00+08:00",
                require_partitions=True,
            )

            self.assertEqual(events["business_key"].tolist(), ["new"])

    def test_replay_slot_includes_daily_events_text_and_minutes(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "raw"
            build_raw(raw)
            out = Path(tmp) / "replay"
            builder = SnapshotBuilder(raw, Path(tmp) / "missing_events")
            manifest = builder.build_replay_slot("20211007", "20211011", out, label="valid", config=CONFIG)
            daily = pd.read_parquet(out / "daily.parquet")
            self.assertEqual(sorted(daily["trade_date"].unique()), ["20211008"])
            # Replay region is not PIT-filtered: the same-evening moneyflow row is included.
            events = pd.read_parquet(out / "events.parquet")
            self.assertEqual(set(events["dataset"]), {"margin_secs", "moneyflow"})
            text_index = pd.read_parquet(out / "text_index.parquet")
            self.assertEqual(len(text_index), 1)
            minutes = pd.read_parquet(out / "intraday_1min.parquet")
            self.assertEqual(len(minutes), 0)  # fixture minutes are outside the period
            self.assertEqual(manifest["kind"], "replay_slot")
            stored = load_snapshot_manifest(out)
            self.assertIn("build_profile", stored)
            self.assertIn("intraday_1min.parquet", stored["data_profile"]["files"])
            verify_snapshot_hash(out)

    def test_missing_configured_dataset_fails_fast(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "raw"
            build_raw(raw)
            builder = SnapshotBuilder(raw, Path(tmp) / "fund_events_missing")
            config = SnapshotConfig(
                events_datasets=("margin_secs", "block_trade"),
                macro_datasets=(),
                text_datasets=(),
                fundamental_datasets=(),
                intraday_trade_days=1,
                include_industry=False,
            )
            with self.assertRaises(FileNotFoundError):
                builder.build_decision_snapshot(DECISION, Path(tmp) / "snap", config)

    def test_missing_fundamental_event_partitions_fails_fast(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "raw"
            build_raw(raw)
            config = SnapshotConfig(
                events_datasets=(),
                macro_datasets=(),
                text_datasets=(),
                fundamental_datasets=("income_vip",),
                include_intraday=False,
                include_industry=False,
            )
            status_path = Path(tmp) / "fundamental_events_status.json"
            write_fundamental_status(status_path)

            with self.assertRaises(FileNotFoundError):
                SnapshotBuilder(raw, Path(tmp) / "missing_events", status_path).build_decision_snapshot(
                    DECISION, Path(tmp) / "snap", config
                )

    def test_missing_one_configured_fundamental_dataset_fails_fast(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "raw"
            events_root = Path(tmp) / "fund_events"
            status_path = Path(tmp) / "fundamental_events_status.json"
            build_raw(raw)
            build_fundamental_events(events_root)
            write_fundamental_status(status_path)
            config = SnapshotConfig(
                events_datasets=(),
                macro_datasets=(),
                text_datasets=(),
                fundamental_datasets=("income_vip", "balancesheet_vip"),
                include_intraday=False,
                include_industry=False,
            )

            with self.assertRaisesRegex(FileNotFoundError, "balancesheet_vip"):
                SnapshotBuilder(raw, events_root, status_path).build_decision_snapshot(
                    DECISION, Path(tmp) / "snap", config
                )

    def test_fundamental_event_status_is_required_when_fundamentals_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "raw"
            events_root = Path(tmp) / "fund_events"
            build_raw(raw)
            build_fundamental_events(events_root)
            config = SnapshotConfig(
                events_datasets=(),
                macro_datasets=(),
                text_datasets=(),
                fundamental_datasets=("income_vip",),
                include_intraday=False,
                include_industry=False,
            )

            with self.assertRaisesRegex(ValueError, "status is required"):
                SnapshotBuilder(raw, events_root).build_decision_snapshot(DECISION, Path(tmp) / "snap", config)

    def test_fundamental_event_audit_error_blocks_decision_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "raw"
            events_root = Path(tmp) / "fund_events"
            status_path = Path(tmp) / "fundamental_events_status.json"
            build_raw(raw)
            build_fundamental_events(events_root)
            status_path.write_text(json.dumps({"status": "error", "errors": 1}), encoding="utf-8")
            config = SnapshotConfig(
                events_datasets=(),
                macro_datasets=(),
                text_datasets=(),
                fundamental_datasets=("income_vip",),
                include_intraday=False,
                include_industry=False,
            )

            with self.assertRaises(ValueError):
                SnapshotBuilder(raw, events_root, status_path).build_decision_snapshot(
                    DECISION, Path(tmp) / "snap", config
                )


if __name__ == "__main__":
    unittest.main()
