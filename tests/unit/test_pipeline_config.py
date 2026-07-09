"""Numeric validation of pipeline config records (acceptance rules, budgets)."""

import math
import tempfile
import unittest
from pathlib import Path

from autotrade.pipelines.config import AcceptanceRules, ExperimentConfig


def make_config(tmp: Path, **overrides: object) -> ExperimentConfig:
    kwargs: dict[str, object] = dict(
        experiment_id="exp_test",
        experiments_root=tmp / "experiments",
        work_root=tmp / "work",
        template_dir=tmp / "template",
        first_test_period="2022Q1",
        last_test_period="2022Q1",
        heldout_first_period="2022Q3",
        heldout_last_period="2022Q3",
    )
    kwargs.update(overrides)
    return ExperimentConfig(**kwargs)


class AcceptanceRulesTest(unittest.TestCase):
    def test_nan_metrics_are_hard_rejects(self):
        # NaN compares False against every threshold; without the finiteness
        # guard a NaN total_return would pass acceptance outright.
        rules = AcceptanceRules()
        summary = {"total_return": math.nan, "sharpe": 1.0, "max_drawdown": 0.1, "complete_validation": True}
        accepted, reasons = rules.evaluate(summary)
        self.assertFalse(accepted)
        self.assertTrue(any("non-finite" in reason for reason in reasons))

    def test_finite_metrics_keep_threshold_semantics(self):
        rules = AcceptanceRules()
        ok = {"total_return": 0.02, "sharpe": 0.5, "max_drawdown": 0.1, "complete_validation": True}
        self.assertEqual(rules.evaluate(ok), (True, []))
        bad = {"total_return": 0.02, "sharpe": 0.5, "max_drawdown": 0.30, "complete_validation": True}
        accepted, reasons = rules.evaluate(bad)
        self.assertFalse(accepted)
        self.assertIn("max drawdown", reasons[0])

    def test_rule_values_must_be_finite_and_ranged(self):
        with self.assertRaises(ValueError):
            AcceptanceRules(min_return=math.nan)
        with self.assertRaises(ValueError):
            AcceptanceRules(max_drawdown=1.5)


class ExperimentConfigValidationTest(unittest.TestCase):
    def test_budget_knobs_must_be_positive_finite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for field_name, value in (
                ("backtest_max_seconds_per_decision", math.nan),
                ("per_call_timeout_seconds", 0),
                ("max_backtests_per_fold", -1),
                ("decision_max_sim_minutes", math.inf),
            ):
                with self.assertRaisesRegex(ValueError, field_name):
                    make_config(root, **{field_name: value})

    def test_valid_defaults_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(Path(tmp))
            self.assertEqual(config.first_test_period, "2022Q1")


if __name__ == "__main__":
    unittest.main()
