"""Minute replay market indexing and date-slice behavior."""

import unittest

import pandas as pd

from autotrade.environment.replay_market import MinuteMarketData


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


if __name__ == "__main__":
    unittest.main()
