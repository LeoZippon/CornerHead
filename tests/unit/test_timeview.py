"""Per-tick Timeview: node-gated six-domain rolling, write-once parts, versioning."""

import tempfile
import unittest
from pathlib import Path
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
    return {"daily": daily, "events": events, "fundamentals": fundamentals}


class TimeviewTest(unittest.TestCase):
    def _build(self, root: Path) -> Timeview:
        return Timeview(
            host_dir=root / "asof",
            executor=FakeExecutor(),
            snapshot_dir=_frozen_snapshot(root),
            replay_frames=_replay_frames(),
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
            # waits for that night's evening node (~02:05 next day).
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
            asof2, _ = tv.refresh(_when("2022-01-05 03:00:00"))
            events2 = pd.read_parquet(Path(asof2) / "events")
            self.assertEqual(set(events2["dataset"]), {"margin_secs", "block_trade"})

    def test_fundamentals_roll_on_pit_node(self):
        with tempfile.TemporaryDirectory() as tmp:
            tv = self._build(Path(tmp))
            asof, _ = tv.refresh(_when("2022-01-04 09:10:00"))
            self.assertEqual(len(pd.read_parquet(Path(asof) / "fundamentals")), 0)  # before the PIT build
            asof2, _ = tv.refresh(_when("2022-01-05 04:00:00"))  # after 03:50 PIT build
            self.assertEqual(len(pd.read_parquet(Path(asof2) / "fundamentals")), 1)

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

    def test_parts_are_write_once_no_duplicate_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            tv = self._build(Path(tmp))
            tv.refresh(_when("2022-01-05 09:10:00"))
            asof, _ = tv.refresh(_when("2022-01-05 09:20:00"))  # same signatures: no new parts
            daily = pd.read_parquet(Path(asof) / "daily")
            # 20220104 appears exactly once even after repeated refreshes.
            self.assertEqual(list(daily["trade_date"].astype(str)).count("20220104"), 1)


if __name__ == "__main__":
    unittest.main()
