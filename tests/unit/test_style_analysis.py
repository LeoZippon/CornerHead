"""Barra-lite attribution: in-loop (replay) entry and math invariants."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pandas as pd

from autotrade.environment.style_analysis import (
    _benchmark_regression,
    _daily_returns_from_curve,
    benchmark_returns,
    replay_style_analysis,
)

D1, D2 = "20220104", "20220105"


def _replay_daily() -> pd.DataFrame:
    rows = []
    for date in (D1, D2):
        # Four stocks spanning the size spectrum; the strategy buys the smallest.
        for code, close, circ_mv, pb, turnover in (
            ("000001.SZ", 10.0, 100.0, 1.0, 1.0),
            ("000002.SZ", 20.0, 500.0, 2.0, 2.0),
            ("000003.SZ", 30.0, 2000.0, 4.0, 3.0),
            ("000004.SZ", 40.0, 9000.0, 8.0, 4.0),
        ):
            rows.append({"ts_code": code, "trade_date": date, "close": close,
                         "circ_mv": circ_mv, "pb": pb, "turnover_rate": turnover})
    return pd.DataFrame(rows)


def _orders() -> list[dict[str, object]]:
    return [
        {"status": "filled", "filled_quantity": 100, "action": "buy",
         "ts_code": "000001.SZ", "trade_date": D1, "decision_time": "2022-01-04T09:31:00"},
        {"status": "rejected", "filled_quantity": 0, "action": "buy",
         "ts_code": "000004.SZ", "trade_date": D1, "decision_time": "2022-01-04T09:32:00"},
    ]


def _stats() -> dict[str, object]:
    return {
        "initial_cash": 1_000_000.0,
        "total_return": 0.002,
        "equity_curve": {D1: 1_001_000.0, D2: 1_002_001.0},
    }


class ReplayStyleAnalysisTest(unittest.TestCase):
    def test_style_from_replay_cross_section_without_raw_dir(self) -> None:
        payload = replay_style_analysis(_replay_daily(), _orders(), _stats(), raw_dir=None)
        style = payload["style"]
        # Position carried forward across both window days.
        self.assertEqual(style["days"], 2)
        self.assertEqual(style["avg_names"], 1.0)
        # Bought the smallest cap (rank 1/4 -> pct 0.25): size tilt -0.5 exactly.
        self.assertAlmostEqual(style["tilts"]["size"], -0.5)
        # No raw lake: benchmark fields degrade to None, n_days still reported.
        regression = payload["benchmark_regression"]
        self.assertEqual(regression["n_days"], 0)
        self.assertIsNone(regression["benchmark_return"])
        compact = payload["compact"]
        self.assertIsNone(compact["excess_return"])
        self.assertEqual(compact["size_tilt"], style["tilts"]["size"])

    def test_benchmark_from_raw_dir_and_excess(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp)
            index_dir = raw / "index_daily" / "ts_code=000300.SH"
            index_dir.mkdir(parents=True)
            pd.DataFrame({
                "trade_date": [D1, D2],
                "pct_chg": [1.0, -0.5],  # percent units, as in the raw lake
            }).to_parquet(index_dir / "year=2022.parquet", index=False)
            payload = replay_style_analysis(_replay_daily(), _orders(), _stats(), raw_dir=str(raw))
        regression = payload["benchmark_regression"]
        self.assertEqual(regression["n_days"], 2)
        self.assertAlmostEqual(regression["benchmark_return"], 1.01 * 0.995 - 1.0, places=6)
        self.assertIsNone(regression["beta"])  # 2 days < regression minimum
        compact = payload["compact"]
        self.assertAlmostEqual(compact["excess_return"], 0.002 - (1.01 * 0.995 - 1.0), places=6)

    def test_degrades_when_style_columns_missing(self) -> None:
        bare = pd.DataFrame({"ts_code": ["000001.SZ"], "trade_date": [D1], "close": [10.0]})
        payload = replay_style_analysis(bare, _orders(), _stats(), raw_dir=None)
        self.assertEqual(payload["style"]["days"], 0)
        self.assertIsNone(payload["style"]["tilts"])

    def test_daily_returns_and_regression_math(self) -> None:
        returns = _daily_returns_from_curve({D1: 1_010_000.0, D2: 999_900.0}, 1_000_000.0)
        self.assertAlmostEqual(returns[0][1], 0.01)
        self.assertAlmostEqual(returns[1][1], 999_900.0 / 1_010_000.0 - 1.0)
        # Perfectly correlated series -> beta 2, r2 1.
        strategy = [(f"202201{day:02d}", 0.02 * ((-1) ** day)) for day in range(1, 11)]
        bench = {date: value / 2 for date, value in strategy}
        regression = _benchmark_regression(strategy, bench)
        self.assertAlmostEqual(regression["beta"], 2.0, places=3)
        self.assertAlmostEqual(regression["r2"], 1.0, places=3)
        self.assertEqual(benchmark_returns(None, [D1]), [])


if __name__ == "__main__":
    unittest.main()
