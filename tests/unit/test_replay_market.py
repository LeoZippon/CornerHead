"""Minute replay market indexing, parsing, and bounded Parquet prefetch."""

import tempfile
import unittest
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from autotrade.environment.replay_market import (
    MinuteMarketData,
    ParquetMinuteReplaySource,
    _minute_key,
    minute_rows_with_daily_fallback,
)


class MinuteMarketDataTest(unittest.TestCase):
    def test_rows_for_date_uses_precomputed_contiguous_bounds(self) -> None:
        market = MinuteMarketData(
            pd.DataFrame(
                [
                    {"trade_date": "20220105", "ts_code": "000002.SZ", "trade_time": "09:31", "close": 20.1},
                    {"trade_date": "20220104", "ts_code": "000002.SZ", "trade_time": "09:31", "close": 10.2},
                    {"trade_date": "20220104", "ts_code": "000001.SZ", "trade_time": "09:30", "close": 10.0},
                    {"trade_date": "20220105", "ts_code": "000001.SZ", "trade_time": "09:30", "close": 20.0},
                ]
            )
        )

        self.assertEqual(market._date_bounds, {"20220104": (0, 2), "20220105": (2, 4)})
        first = market.rows_for_date("20220104")
        self.assertEqual(
            list(zip(first["minute_key"], first["ts_code"])),
            [("09:30", "000001.SZ"), ("09:31", "000002.SZ")],
        )

        # Callers still receive an isolated frame, and an unknown date preserves
        # the normalized replay schema without scanning the full trade_date column.
        first.loc[:, "close"] = 0.0
        self.assertEqual(market.rows_for_date("20220104")["close"].tolist(), [10.0, 10.2])
        missing = market.rows_for_date("20220106")
        self.assertTrue(missing.empty)
        self.assertEqual(missing.columns.tolist(), market._frame.columns.tolist())

    def test_vectorized_iso_parser_preserves_legacy_fallback_formats(self) -> None:
        times = [
            "2022-01-04 09:30:00",
            "09:31",
            "093200",
            "20220104093300",
            pd.Timestamp("2022-01-04 09:34:00"),
        ]
        market = MinuteMarketData(
            pd.DataFrame(
                {
                    "trade_date": ["20220104"] * len(times),
                    "ts_code": [f"00000{i + 1}.SZ" for i in range(len(times))],
                    "trade_time": times,
                    "close": range(10, 10 + len(times)),
                    "internal_only": "drop-me",
                }
            )
        )
        self.assertEqual(market._frame["minute_key"].tolist(), [_minute_key(value) for value in times])
        self.assertEqual(market._frame["minute_sort"].tolist(), [570, 571, 572, 573, 574])
        self.assertNotIn("internal_only", market._frame.columns)

    def test_invalid_time_still_fails_explicitly(self) -> None:
        with self.assertRaisesRegex(ValueError, "invalid trade_time"):
            MinuteMarketData(
                pd.DataFrame(
                    [{"trade_date": "20220104", "ts_code": "000001.SZ", "trade_time": "bad", "close": 10.0}]
                )
            )

    def test_partial_minutes_receive_missing_daily_open_and_close_events(self) -> None:
        daily = pd.DataFrame(
            [{
                "trade_date": "20220104", "ts_code": "000001.SZ",
                "open": 10.0, "high": 12.0, "low": 9.0, "close": 11.0,
            }]
        )
        seed = MinuteMarketData(
            pd.DataFrame(
                [{
                    "trade_date": "20220104", "ts_code": "000001.SZ",
                    "trade_time": "10:00", "open": 10.2, "high": 10.3,
                    "low": 10.1, "close": 10.25,
                }]
            )
        ).rows_for_date("20220104")

        events = minute_rows_with_daily_fallback(daily, "20220104", seed)

        self.assertEqual(events["minute_key"].tolist(), ["09:30", "10:00", "15:00"])
        opening = events[events["minute_key"] == "09:30"].iloc[0]
        closing = events[events["minute_key"] == "15:00"].iloc[0]
        self.assertEqual((opening["open"], opening["close"]), (10.0, 10.0))
        self.assertEqual((closing["open"], closing["close"]), (11.0, 11.0))


class ParquetMinuteReplaySourceTest(unittest.TestCase):
    def test_reads_only_selected_day_and_keeps_one_future(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "intraday_1min.parquet"
            first = pd.DataFrame(
                [
                    {
                        "trade_date": "20220104", "ts_code": "000002.SZ",
                        "trade_time": "2022-01-04 09:31:00", "open": 20.0,
                        "high": 20.2, "low": 19.9, "close": 20.1, "vol": 2.0,
                        "amount": 40.0, "available_at": "2022-01-04T09:31:00+08:00",
                        "timeview_only": "a",
                    },
                    {
                        "trade_date": "20220104", "ts_code": "000001.SZ",
                        "trade_time": "2022-01-04 09:30:00", "open": 10.0,
                        "high": 10.2, "low": 9.9, "close": 10.1, "vol": 1.0,
                        "amount": 10.0, "available_at": "2022-01-04T09:30:00+08:00",
                        "timeview_only": "b",
                    },
                ]
            )
            second = first.assign(
                trade_date="20220105",
                trade_time=first["trade_time"].str.replace("2022-01-04", "2022-01-05"),
                available_at=first["available_at"].str.replace("2022-01-04", "2022-01-05"),
            )
            schema = pa.Table.from_pandas(first, preserve_index=False).schema
            with pq.ParquetWriter(path, schema) as writer:
                writer.write_table(pa.Table.from_pandas(first, schema=schema, preserve_index=False))
                writer.write_table(pa.Table.from_pandas(second, schema=schema, preserve_index=False))

            with ParquetMinuteReplaySource(
                path,
                trade_dates=("20220104", "20220105"),
                include_timeview_rows=True,
            ) as source:
                self.assertEqual(source.selected_rows, 4)
                source.prefetch("20220104")
                with self.assertRaisesRegex(RuntimeError, "already holds"):
                    source.prefetch("20220105")
                partition = source.rows_for_date("20220104")
                self.assertEqual(
                    list(zip(partition.market_rows["minute_key"], partition.market_rows["ts_code"])),
                    [("09:30", "000001.SZ"), ("09:31", "000002.SZ")],
                )
                self.assertNotIn("timeview_only", partition.market_rows.columns)
                self.assertEqual(partition.timeview_rows["timeview_only"].tolist(), ["a", "b"])
                stats = source.stats()
                self.assertEqual(stats["minute_partitions_loaded"], 1)
                self.assertEqual(stats["minute_rows_loaded"], 2)

    def test_projection_mode_does_not_retain_timeview_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "intraday_1min.parquet"
            pd.DataFrame(
                [{
                    "trade_date": "20220104", "ts_code": "000001.SZ",
                    "trade_time": "2022-01-04 09:30:00", "close": 10.0,
                    "available_at": "2022-01-04T09:30:00+08:00", "large_unused": "x",
                }]
            ).to_parquet(path, index=False)
            with ParquetMinuteReplaySource(path, include_timeview_rows=False) as source:
                partition = source.rows_for_date("20220104")
                self.assertIsNone(partition.timeview_rows)
                self.assertNotIn("large_unused", partition.market_rows.columns)


if __name__ == "__main__":
    unittest.main()
