"""Per-tick Timeview: node-gated six-domain rolling, write-once parts, versioning."""

import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import pandas as pd

from autotrade.environment.timeview import Timeview

CN_TZ = ZoneInfo("Asia/Shanghai")
TS = "000001.SZ"


class FakeExecutor:
    def map_path(self, path) -> str:
        return str(path)


def _when(text: str) -> pd.Timestamp:
    return pd.Timestamp(text, tz=CN_TZ)


def _write(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(path, index=False)


def _frozen_snapshot(root: Path) -> Path:
    snap = root / "snapshot"
    snap.mkdir(parents=True, exist_ok=True)
    _write(snap / "daily.parquet", pd.DataFrame([{"trade_date": "20211231", "ts_code": TS, "open": 9.0, "close": 9.5}]))
    _write(snap / "universe.parquet", pd.DataFrame([{"ts_code": TS, "name": "x"}]))
    _write(
        snap / "text_index.parquet",
        pd.DataFrame(
            [
                {
                    "text_id": "frozen_news",
                    "dataset": "news",
                    "ts_codes": TS,
                    "title": "frozen",
                    "available_at": "2021-12-31T08:55:00+08:00",
                    "source_hash": "frozen_hash",
                    "library_file": "news.parquet",
                }
            ]
        ),
    )
    _write(snap / "text_library" / "news.parquet", pd.DataFrame([{"text_id": "frozen_news", "body": "frozen body"}]))
    for name in ("events", "macro", "fundamentals", "intraday_1min"):
        _write(snap / f"{name}.parquet", pd.DataFrame(columns=["dataset", "ts_code", "available_at"]))
    return snap


def _replay_frames() -> dict[str, pd.DataFrame]:
    daily = pd.DataFrame(
        [
            {"trade_date": "20220104", "ts_code": TS, "open": 10.0, "close": 10.2, "available_at": "2022-01-04T17:30:00+08:00"},
            {"trade_date": "20220105", "ts_code": TS, "open": 10.3, "close": 11.0, "available_at": "2022-01-05T17:30:00+08:00"},
        ]
    )
    events = pd.DataFrame(
        [
            {"dataset": "margin_secs", "ts_code": TS, "trade_date": "20220104", "available_at": "2022-01-04T09:00:00+08:00"},
            {"dataset": "block_trade", "ts_code": TS, "trade_date": "20220104", "available_at": "2022-01-04T21:00:00+08:00"},
        ]
    )
    fundamentals = pd.DataFrame(
        [{"dataset": "income_vip", "ts_code": TS, "business_key": "k", "available_at": "2022-01-04T18:00:00+08:00"}]
    )
    text_index = pd.DataFrame(
        [
            {
                "text_id": "news_early",
                "dataset": "news",
                "ts_codes": TS,
                "title": "early",
                "available_at": "2022-01-04T08:55:00+08:00",
                "source_hash": "early_hash",
                "library_file": "news.parquet",
            },
            {
                "text_id": "news_late",
                "dataset": "news",
                "ts_codes": TS,
                "title": "late",
                "available_at": "2022-01-04T09:05:00+08:00",
                "source_hash": "late_hash",
                "library_file": "news.parquet",
            },
        ]
    )
    return {"daily": daily, "events": events, "fundamentals": fundamentals, "text_index": text_index}


class TimeviewTest(unittest.TestCase):
    def _build(self, root: Path) -> Timeview:
        replay_library = root / "replay" / "text_library"
        _write(
            replay_library / "news.parquet",
            pd.DataFrame(
                [
                    {"text_id": "news_early", "body": "early body"},
                    {"text_id": "news_late", "body": "late body"},
                ]
            ),
        )
        return Timeview(
            host_dir=root / "asof",
            executor=FakeExecutor(),
            snapshot_dir=_frozen_snapshot(root),
            replay_frames=_replay_frames(),
            replay_text_library_dir=replay_library,
        )

    def _dates(self, asof_dir: str, domain: str) -> set[str]:
        frame = pd.read_parquet(Path(asof_dir) / domain)
        col = "trade_date" if "trade_date" in frame.columns else "available_at"
        return set(frame[col].astype(str)) if col in frame.columns else set()

    def test_frozen_base_is_part_zero_and_today_is_hidden(self):
        with tempfile.TemporaryDirectory() as tmp:
            tv = self._build(Path(tmp))
            asof, version = tv.refresh(_when("2022-01-04 09:10:00"))
            # Intraday-session day: daily view is just the frozen history; today's bar
            # waits for that night's conservative evening boundary (~03:05 next day).
            self.assertEqual(self._dates(asof, "daily"), {"20211231"})
            self.assertTrue((Path(asof) / "daily" / "part_0000.parquet").exists())

    def test_daily_rolls_after_evening_node(self):
        with tempfile.TemporaryDirectory() as tmp:
            tv = self._build(Path(tmp))
            tv.refresh(_when("2022-01-04 09:10:00"))
            asof, _ = tv.refresh(_when("2022-01-05 09:10:00"))
            # Prior replay day visible once its evening node completed; today still not.
            self.assertEqual(self._dates(asof, "daily"), {"20211231", "20220104"})

    def test_margin_secs_visible_same_day_block_trade_waits_for_evening(self):
        with tempfile.TemporaryDirectory() as tmp:
            tv = self._build(Path(tmp))
            # 09:10 on 20220104: the 09:03 margin_secs node is done, the evening node is not.
            asof, _ = tv.refresh(_when("2022-01-04 09:10:00"))
            events = pd.read_parquet(Path(asof) / "events")
            self.assertEqual(set(events["dataset"]), {"margin_secs"})
            # Block trade (evening dataset) only rolls in after its evening node completes.
            asof2, _ = tv.refresh(_when("2022-01-05 03:06:00"))
            events2 = pd.read_parquet(Path(asof2) / "events")
            self.assertEqual(set(events2["dataset"]), {"margin_secs", "block_trade"})

    def test_fundamentals_roll_on_pit_node(self):
        with tempfile.TemporaryDirectory() as tmp:
            tv = self._build(Path(tmp))
            asof, _ = tv.refresh(_when("2022-01-04 09:10:00"))
            self.assertEqual(len(pd.read_parquet(Path(asof) / "fundamentals")), 0)  # before the PIT build
            asof2, _ = tv.refresh(_when("2022-01-05 04:00:00"))  # after 03:50 PIT build
            self.assertEqual(len(pd.read_parquet(Path(asof2) / "fundamentals")), 1)

    def test_text_index_and_library_roll_together(self):
        with tempfile.TemporaryDirectory() as tmp:
            tv = self._build(Path(tmp))
            asof, _ = tv.refresh(_when("2022-01-04 08:59:00"))
            index = pd.read_parquet(Path(asof) / "text_index")
            bodies = pd.concat(pd.read_parquet(path) for path in sorted((Path(asof) / "text_library").glob("*.parquet")))
            self.assertEqual(set(index["text_id"].astype(str)), {"frozen_news"})
            self.assertEqual(set(bodies["text_id"].astype(str)), {"frozen_news"})

            asof2, _ = tv.refresh(_when("2022-01-04 09:01:00"))
            index2 = pd.read_parquet(Path(asof2) / "text_index")
            bodies2 = pd.concat(pd.read_parquet(path) for path in sorted((Path(asof2) / "text_library").glob("*.parquet")))
            self.assertEqual(set(index2["text_id"].astype(str)), {"frozen_news", "news_early"})
            self.assertEqual(set(bodies2["text_id"].astype(str)), {"frozen_news", "news_early"})
            self.assertNotIn("news_late", set(bodies2["text_id"].astype(str)))

    def test_version_bumps_on_roll_and_is_stable_in_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            tv = self._build(Path(tmp))
            _, v_open = tv.refresh(_when("2022-01-04 09:10:00"))
            # No covering node completes across the session, so the view is frozen.
            _, v_mid = tv.refresh(_when("2022-01-04 11:00:00"))
            _, v_close = tv.refresh(_when("2022-01-04 14:30:00"))
            self.assertEqual(v_open, v_mid)
            self.assertEqual(v_open, v_close)
            # The next day's evening + pre-open nodes roll new rows, advancing the version.
            _, v_next = tv.refresh(_when("2022-01-05 09:20:00"))
            self.assertNotEqual(v_open, v_next)

    def test_ticks_before_next_boundary_do_not_traverse_views(self):
        with tempfile.TemporaryDirectory() as tmp:
            tv = self._build(Path(tmp))
            tv.refresh(_when("2022-01-04 09:10:00"))
            with ExitStack() as stack:
                rolls = [
                    stack.enter_context(mock.patch.object(view, "roll", wraps=view.roll))
                    for view in tv._domains.values()
                ]
                text_roll = stack.enter_context(mock.patch.object(tv._text, "roll", wraps=tv._text.roll))
                tv.refresh(_when("2022-01-04 11:00:00"))
                tv.refresh(_when("2022-01-04 14:30:00"))
                for roll in rolls:
                    roll.assert_not_called()
                text_roll.assert_not_called()

    def test_node_boundary_is_inclusive(self):
        with tempfile.TemporaryDirectory() as tmp:
            tv = self._build(Path(tmp))
            asof, _ = tv.refresh(_when("2022-01-04 09:04:59"))
            self.assertEqual(list((Path(asof) / "events").glob("*.parquet")), [])

            asof, _ = tv.refresh(_when("2022-01-04 09:05:00"))
            events = pd.read_parquet(Path(asof) / "events")
            self.assertEqual(events["dataset"].tolist(), ["margin_secs"])

    def test_one_refresh_catches_up_across_multiple_boundaries(self):
        with tempfile.TemporaryDirectory() as tmp:
            tv = self._build(Path(tmp))
            tv.refresh(_when("2022-01-04 08:59:00"))

            # One clock jump crosses the 09:00 text and 09:05 margin nodes. Both
            # cursors catch up to the latest eligible cutoff in a single refresh.
            asof, _ = tv.refresh(_when("2022-01-04 09:10:00"))
            events = pd.read_parquet(Path(asof) / "events")
            text = pd.read_parquet(Path(asof) / "text_index")
            self.assertEqual(events["dataset"].tolist(), ["margin_secs"])
            self.assertIn("news_early", set(text["text_id"].astype(str)))

    def test_parts_are_write_once_no_duplicate_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            tv = self._build(Path(tmp))
            tv.refresh(_when("2022-01-05 09:10:00"))
            asof, _ = tv.refresh(_when("2022-01-05 09:20:00"))  # same signatures: no new parts
            daily = pd.read_parquet(Path(asof) / "daily")
            # 20220104 appears exactly once even after repeated refreshes.
            self.assertEqual(list(daily["trade_date"].astype(str)).count("20220104"), 1)

    def test_auction_rolls_at_observed_row_time_not_evening_node(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            frames = _replay_frames()
            frames["auction"] = pd.DataFrame(
                [{
                    "trade_date": "20220104",
                    "session": "open",
                    "ts_code": TS,
                    "price": 10.0,
                    "available_at": "2022-01-04T09:28:36+08:00",
                }]
            )
            tv = Timeview(
                host_dir=root / "asof",
                executor=FakeExecutor(),
                snapshot_dir=_frozen_snapshot(root),
                replay_frames=frames,
            )

            asof, before = tv.refresh(_when("2022-01-04 09:28:30"))
            self.assertEqual(list((Path(asof) / "auction").glob("*.parquet")), [])
            # The observed boundary is second-precision; crossing it within the
            # same minute must not be hidden by a minute-rounded signature.
            _, still_before = tv.refresh(_when("2022-01-04 09:28:35"))
            self.assertEqual(still_before, before)
            asof, after = tv.refresh(_when("2022-01-04 09:28:40"))
            self.assertNotEqual(before, after)
            auction = pd.read_parquet(Path(asof) / "auction")
            self.assertEqual(auction["ts_code"].tolist(), [TS])
            part_count = len(list((Path(asof) / "auction").glob("*.parquet")))
            _, repeated = tv.refresh(_when("2022-01-04 09:29:00"))
            self.assertEqual(repeated, after)
            self.assertEqual(len(list((Path(asof) / "auction").glob("*.parquet"))), part_count)


class TimeviewIntradaySchemaTest(unittest.TestCase):
    """The frozen and replay intraday domains share one schema: no internal
    available_at, and the auction-correction columns are never NaN-backfilled (R19-4)."""

    def _minute(self, trade_date: str, available_at: str | None) -> pd.DataFrame:
        from autotrade.environment.features.auction import apply_open_auction_correction

        row = {
            "ts_code": TS,
            "trade_time": f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]} 09:30:00",
            "trade_date": trade_date,
            "open": 10.0, "high": 10.1, "low": 9.9, "close": 10.0, "vol": 20000.0, "amount": 200000.0,
        }
        if available_at is not None:
            row["available_at"] = available_at
        return apply_open_auction_correction(pd.DataFrame([row]))

    def test_intraday_view_has_no_available_at_and_keeps_auction_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snap = _frozen_snapshot(root)
            # Real frozen intraday: auction columns present, internal available_at dropped
            # (mirrors snapshot._build_intraday).
            _write(snap / "intraday_1min.parquet", self._minute("20211231", available_at=None))
            replay = {
                "daily": _replay_frames()["daily"],
                # Replay intraday keeps available_at as the Timeview gate (mirrors
                # snapshot._read_minutes_range).
                "intraday_1min": self._minute("20220104", available_at="2022-01-04T09:30:00+08:00"),
            }
            tv = Timeview(host_dir=root / "asof", executor=FakeExecutor(), snapshot_dir=snap, replay_frames=replay)
            # After the 20220104 evening node completes (fallback ~03:05 on 0105) the replay bar rolls in.
            asof, _ = tv.refresh(_when("2022-01-05 09:10:00"))
            intraday = pd.read_parquet(Path(asof) / "intraday_1min")
            self.assertEqual(sorted(intraday["trade_date"].astype(str)), ["20211231", "20220104"])
            self.assertNotIn("available_at", intraday.columns)
            self.assertIn("auction_correction_rule", intraday.columns)
            # The replay row carries real correction columns, not NaN-backfill.
            self.assertFalse(intraday["auction_correction_rule"].isna().any())
            self.assertFalse(intraday["vol_pit"].isna().any())


if __name__ == "__main__":
    unittest.main()
