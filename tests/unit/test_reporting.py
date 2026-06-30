import tempfile
import unittest
from pathlib import Path

import pandas as pd

from autotrade.pipelines.ledger import ExperimentLedger
from autotrade.pipelines.reporting import build_experiment_report


PERIODS = {
    "fold_2022Q1": "20220101..20220331",
    "fold_2022Q2": "20220401..20220630",
}


def fold_record(fold_id, valid_ret, test_ret, epoch_id="epoch_001"):
    return {
        "record_type": "fold",
        "experiment_id": "e",
        "epoch_id": epoch_id,
        "fold_id": fold_id,
        "run_id": f"run_{fold_id}",
        "fold_status": "frozen",
        "test_period": PERIODS[fold_id],
        "validation_result": {"total_return": valid_ret, "sharpe": 1.1, "max_drawdown": 0.05},
        "test_result": {"total_return": test_ret, "sharpe": 0.8, "max_drawdown": 0.07, "order_count": 4, "margin_secs_reject_count": 1},
    }


def write_csi300(raw_dir: Path) -> None:
    path = raw_dir / "index_daily" / "ts_code=000300.SH"
    path.mkdir(parents=True)
    pd.DataFrame(
        [
            {"ts_code": "000300.SH", "trade_date": "20220104", "open": 100.0, "close": 101.0},
            {"ts_code": "000300.SH", "trade_date": "20220331", "open": 101.0, "close": 105.0},
            {"ts_code": "000300.SH", "trade_date": "20220401", "open": 105.0, "close": 104.0},
            {"ts_code": "000300.SH", "trade_date": "20220630", "open": 104.0, "close": 110.0},
            {"ts_code": "000300.SH", "trade_date": "20260105", "open": 110.0, "close": 111.0},
            {"ts_code": "000300.SH", "trade_date": "20260331", "open": 111.0, "close": 121.0},
        ]
    ).to_parquet(path / "year=2022.parquet", index=False)


class ReportingTest(unittest.TestCase):
    def test_builds_charts_and_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            ledger = ExperimentLedger(tmp / "ledger.jsonl")
            ledger.append(fold_record("fold_2022Q1", 0.03, 0.02))
            ledger.append(fold_record("fold_2022Q2", 0.01, -0.01))
            ledger.append(fold_record("fold_2022Q1", 0.02, 0.03, epoch_id="epoch_002"))
            ledger.append(fold_record("fold_2022Q2", 0.04, 0.04, epoch_id="epoch_002"))
            ledger.append(
                {
                    "record_type": "heldout",
                    "experiment_id": "e",
                    "epoch_id": "epoch_002",
                    "fold_id": "heldout_2026Q1",
                    "run_id": "run_ho",
                    "period": {"start": "20260101", "end": "20260331"},
                    "test_result": {"total_return": 0.015, "sharpe": 0.5, "max_drawdown": 0.04, "order_count": 3, "margin_secs_reject_count": 0},
                }
            )
            write_csi300(tmp / "raw")
            (tmp / "reports").mkdir()
            (tmp / "reports" / "fold_returns.png").write_text("legacy", encoding="utf-8")
            (tmp / "reports" / "cumulative_test_return.png").write_text("legacy", encoding="utf-8")
            (tmp / "reports" / "summary.json").write_text("legacy", encoding="utf-8")
            summary = build_experiment_report(tmp / "ledger.jsonl", tmp / "reports", benchmark_raw_dir=tmp / "raw")
            self.assertTrue((tmp / "reports" / "epoch_comparison_returns.png").exists())
            self.assertTrue((tmp / "reports" / "epoch_returns" / "epoch_001_returns.png").exists())
            self.assertTrue((tmp / "reports" / "epoch_returns" / "epoch_002_returns.png").exists())
            self.assertFalse((tmp / "reports" / "fold_returns.png").exists())
            self.assertFalse((tmp / "reports" / "cumulative_test_return.png").exists())
            self.assertFalse((tmp / "reports" / "summary.json").exists())
            self.assertEqual(summary["folds"], 4)
            self.assertEqual(summary["heldout_periods"], 1)
            self.assertEqual(
                summary["epoch_return_charts"],
                [
                    str(tmp / "reports" / "epoch_returns" / "epoch_001_returns.png"),
                    str(tmp / "reports" / "epoch_returns" / "epoch_002_returns.png"),
                ],
            )
            self.assertEqual(summary["epoch_comparison_chart"], str(tmp / "reports" / "epoch_comparison_returns.png"))
            self.assertAlmostEqual(summary["development"]["positive_test_rate"], 0.75)
            self.assertAlmostEqual(summary["heldout"]["mean_return"], 0.015)
            self.assertEqual(summary["benchmark"]["status"], "ok")
            self.assertEqual(summary["status"], "ok")
            self.assertAlmostEqual(summary["development"]["mean_benchmark_return"], (0.05 + (110.0 / 105.0 - 1.0)) / 2)
            self.assertAlmostEqual(summary["development"]["mean_active_return"], ((0.02 - 0.05) + (-0.01 - (110.0 / 105.0 - 1.0)) + (0.03 - 0.05) + (0.04 - (110.0 / 105.0 - 1.0))) / 4)

    def test_warns_when_benchmark_data_missing(self):
        # R9: a missing benchmark must flag the report status as "warning"
        # (docs/pipeline_design.md 8.4/10.1), while an intentional --no-benchmark
        # ("disabled") and a covered benchmark ("ok") are not warnings.
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            ledger = ExperimentLedger(tmp / "ledger.jsonl")
            ledger.append(fold_record("fold_2022Q1", 0.03, 0.02))
            (tmp / "raw").mkdir()  # exists but has no index_daily/000300.SH
            summary = build_experiment_report(tmp / "ledger.jsonl", tmp / "reports", benchmark_raw_dir=tmp / "raw")
            self.assertEqual(summary["benchmark"]["status"], "missing_data")
            self.assertEqual(summary["status"], "warning")

            disabled = build_experiment_report(
                tmp / "ledger.jsonl", tmp / "reports_nb", benchmark_code=None
            )
            self.assertEqual(disabled["benchmark"]["status"], "disabled")
            self.assertEqual(disabled["status"], "ok")

    def test_requires_fold_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = Path(tmp) / "ledger.jsonl"
            ledger_path.write_text("", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "no fold records"):
                build_experiment_report(ledger_path, Path(tmp) / "reports")


if __name__ == "__main__":
    unittest.main()
