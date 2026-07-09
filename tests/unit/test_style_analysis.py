"""Barra-lite attribution: frozen-input adapters, window math, prefix rollups."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from autotrade.environment.style_analysis import (
    _benchmark_regression,
    daily_returns_from_curve,
    replay_style_analysis,
    write_style_rollup,
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


def _positions_eod() -> list[dict[str, object]]:
    # Broker end-of-day snapshots: the long carried across both days.
    return [
        {"date": D1, "account": "stock", "ts_code": "000001.SZ", "side": "long",
         "quantity": 100, "last_price": 10.0, "market_value": 1000.0},
        {"date": D2, "account": "stock", "ts_code": "000001.SZ", "side": "long",
         "quantity": 100, "last_price": 10.0, "market_value": 1000.0},
    ]


def _stats() -> dict[str, object]:
    return {
        "initial_cash": 1_000_000.0,
        "total_return": 0.002,
        "equity_curve": {D1: 1_001_000.0, D2: 1_002_001.0},
    }


def _write_slot(tmp: Path, *, with_benchmark: bool, with_industry: bool) -> tuple[Path, Path]:
    replay_dir = tmp / "replay"
    snapshot_dir = tmp / "snapshot"
    replay_dir.mkdir()
    snapshot_dir.mkdir()
    if with_benchmark:
        pd.DataFrame([
            {"dataset": "index_daily", "ts_code": "000300.SH", "trade_date": D1, "pct_chg": 1.0},
            {"dataset": "index_daily", "ts_code": "000300.SH", "trade_date": D2, "pct_chg": -0.5},
            {"dataset": "shibor", "ts_code": None, "trade_date": D1, "pct_chg": None},
        ]).to_parquet(replay_dir / "macro.parquet", index=False)
    if with_industry:
        pd.DataFrame({
            "ts_code": ["000001.SZ", "000004.SZ"],
            "l1_name": ["银行", "电子"],
        }).to_parquet(snapshot_dir / "universe.parquet", index=False)
    return replay_dir, snapshot_dir


class ReplayStyleAnalysisTest(unittest.TestCase):
    def test_frozen_inputs_benchmark_industry_and_tilts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            replay_dir, snapshot_dir = _write_slot(Path(tmp), with_benchmark=True, with_industry=True)
            payload = replay_style_analysis(
                _replay_daily(), _positions_eod(), _stats(), replay_dir=replay_dir, snapshot_dir=snapshot_dir
            )
        regression = payload["benchmark_regression"]
        self.assertEqual(regression["n_days"], 2)
        self.assertAlmostEqual(regression["benchmark_return"], 1.01 * 0.995 - 1.0, places=6)
        self.assertIsNone(regression["beta"])  # 2 days < regression minimum
        style = payload["style"]
        self.assertEqual(style["days"], 2)  # position carried forward
        self.assertAlmostEqual(style["tilts"]["size"], -0.5)  # smallest of 4
        self.assertEqual(style["industries"][0], {"name": "银行", "weight": 1.0})
        self.assertEqual(payload["style_rollup"]["industry_sums"], {"银行": 2.0})
        compact = payload["compact"]
        self.assertAlmostEqual(compact["excess_return"], 0.002 - (1.01 * 0.995 - 1.0), places=6)
        # Persisted series make downstream consumers artifact-only.
        self.assertEqual([d for d, _ in payload["strategy_daily"]], [D1, D2])
        self.assertEqual([d for d, _ in payload["benchmark_daily"]], [D1, D2])

    def test_degrades_without_slot_benchmark_or_universe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            replay_dir, snapshot_dir = _write_slot(Path(tmp), with_benchmark=False, with_industry=False)
            payload = replay_style_analysis(
                _replay_daily(), _positions_eod(), _stats(), replay_dir=replay_dir, snapshot_dir=snapshot_dir
            )
        self.assertEqual(payload["benchmark_regression"]["n_days"], 0)
        self.assertIsNone(payload["compact"]["excess_return"])
        self.assertEqual(payload["style"]["industries"][0]["name"], "未分类")
        # Missing style columns degrade the exposure block, never raise.
        bare = pd.DataFrame({"ts_code": ["000001.SZ"], "trade_date": [D1], "close": [10.0]})
        with tempfile.TemporaryDirectory() as tmp:
            replay_dir, snapshot_dir = _write_slot(Path(tmp), with_benchmark=False, with_industry=False)
            payload = replay_style_analysis(bare, _positions_eod(), _stats(), replay_dir=replay_dir, snapshot_dir=snapshot_dir)
        self.assertEqual(payload["style"]["days"], 0)
        self.assertIsNone(payload["style"]["tilts"])

    def test_regression_math(self) -> None:
        returns = daily_returns_from_curve({D1: 1_010_000.0, D2: 999_900.0}, 1_000_000.0)
        self.assertAlmostEqual(returns[0][1], 0.01)
        self.assertAlmostEqual(returns[1][1], 999_900.0 / 1_010_000.0 - 1.0)
        strategy = [(f"202201{day:02d}", 0.02 * ((-1) ** day)) for day in range(1, 11)]
        bench = {date: value / 2 for date, value in strategy}
        regression = _benchmark_regression(strategy, bench)
        self.assertAlmostEqual(regression["beta"], 2.0, places=3)
        self.assertAlmostEqual(regression["r2"], 1.0, places=3)


class StyleRollupTest(unittest.TestCase):
    def test_rollup_merges_windows_days_weighted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            results = Path(tmp)
            windows = {
                "valid_000": {
                    "strategy_daily": [[D1, 0.01]],
                    "benchmark_daily": [[D1, 0.005]],
                    "style": {"days": 1, "tilts": {"size": -0.5, "pb": 0.0, "turnover": 0.0},
                              "industries": [{"name": "银行", "weight": 1.0}],
                              "avg_names": 1, "avg_long_gross": 1000, "avg_short_gross": 0},
                    "style_rollup": {"days": 1, "tilt_sums": {"size": -0.5, "pb": 0.0, "turnover": 0.0},
                                     "industry_sums": {"银行": 1.0, "传媒": 0.2},
                                     "names": 1, "long_gross": 1000, "short_gross": 0},
                },
                "valid_001": {
                    "strategy_daily": [[D2, 0.02]],
                    "benchmark_daily": [[D2, -0.005]],
                    "style": {"days": 3, "tilts": {"size": 0.5, "pb": 0.4, "turnover": 0.0},
                              "industries": [{"name": "电子", "weight": 1.0}],
                              "avg_names": 3, "avg_long_gross": 3000, "avg_short_gross": 0},
                    "style_rollup": {"days": 3, "tilt_sums": {"size": 1.5, "pb": 1.2, "turnover": 0.0},
                                     "industry_sums": {"电子": 3.0, "传媒": 0.3},
                                     "names": 9, "long_gross": 9000, "short_gross": 0},
                },
            }
            for name, payload in windows.items():
                window_dir = results / name
                window_dir.mkdir()
                (window_dir / "style_analysis.json").write_text(json.dumps(payload), encoding="utf-8")
            rollup = write_style_rollup(results, "valid")
            self.assertIsNotNone(rollup)
            written = json.loads((results / "style_valid.json").read_text(encoding="utf-8"))
        self.assertEqual(written["windows"], ["valid_000", "valid_001"])
        # Chained series + regression over them.
        self.assertEqual([d for d, _ in written["strategy_daily"]], [D1, D2])
        self.assertAlmostEqual(written["benchmark_regression"]["benchmark_return"], 1.005 * 0.995 - 1.0, places=6)
        # Days-weighted tilts: (-0.5*1 + 0.5*3) / 4 = 0.25.
        self.assertAlmostEqual(written["style"]["tilts"]["size"], 0.25)
        self.assertEqual(written["style"]["days"], 4)
        weights = {item["name"]: item["weight"] for item in written["style"]["industries"]}
        self.assertAlmostEqual(weights["电子"], 0.75)
        self.assertAlmostEqual(weights["银行"], 0.25)
        self.assertAlmostEqual(weights["传媒"], 0.125)
        # Compact excess uses the chained compounded return.
        expected_strategy = 1.01 * 1.02 - 1.0
        self.assertAlmostEqual(
            written["compact"]["excess_return"], expected_strategy - (1.005 * 0.995 - 1.0), places=6
        )

    def test_rollup_absent_without_sidecars(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            results = Path(tmp)
            (results / "valid_000").mkdir()
            self.assertIsNone(write_style_rollup(results, "valid"))
            self.assertFalse((results / "style_valid.json").exists())


if __name__ == "__main__":
    unittest.main()
