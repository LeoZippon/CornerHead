"""Numeric validation of pipeline config records (acceptance rules, budgets)
and default-value drift guards across the config surfaces."""

import math
import tempfile
import unittest
from dataclasses import MISSING, fields
from pathlib import Path

from autotrade.environment.broker import BrokerProfile
from autotrade.pipelines.config import AcceptanceRules, ExperimentConfig
from autotrade.pipelines.hitl_state import PARAM_DEFAULTS


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
        reasons, warnings = rules.evaluate(summary)
        self.assertTrue(any("non-finite" in reason for reason in reasons))
        self.assertEqual(warnings, [])

    def test_finite_metrics_keep_threshold_semantics(self):
        rules = AcceptanceRules()
        ok = {"total_return": 0.02, "sharpe": 0.5, "max_drawdown": 0.1, "complete_validation": True}
        self.assertEqual(rules.evaluate(ok), ([], []))
        # Drawdown breach stays a HARD reject (risk limit).
        bad = {"total_return": 0.02, "sharpe": 0.5, "max_drawdown": 0.30, "complete_validation": True}
        reasons, warnings = rules.evaluate(bad)
        self.assertIn("max drawdown", reasons[0])
        # Return/Sharpe shortfalls only WARN: the fold freezes instead of resetting.
        weak = {"total_return": -0.01, "sharpe": -0.2, "max_drawdown": 0.1, "complete_validation": True}
        reasons, warnings = rules.evaluate(weak)
        self.assertEqual(reasons, [])
        self.assertEqual(len(warnings), 2)

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


class DefaultsDriftTest(unittest.TestCase):
    """The three default surfaces (domain dataclasses, HITL PARAM_DEFAULTS,
    run_experiment CLI) must agree; the dataclasses are the source of truth."""

    def test_param_defaults_match_domain_dataclasses(self):
        for field_obj in fields(ExperimentConfig):
            if field_obj.name in PARAM_DEFAULTS and field_obj.default is not MISSING:
                self.assertEqual(PARAM_DEFAULTS[field_obj.name], field_obj.default, field_obj.name)
        profile = BrokerProfile()
        for key in (
            "stock_initial_cash", "credit_initial_cash", "commission_bps", "slippage_bps",
            "max_total_holdings", "max_single_name_weight", "fin_rate_annual", "slo_rate_annual",
        ):
            self.assertEqual(PARAM_DEFAULTS[key], getattr(profile, key), key)
        rules = AcceptanceRules()
        for key in ("min_return", "min_sharpe", "max_drawdown"):
            self.assertEqual(PARAM_DEFAULTS[key], getattr(rules, key), key)

    def test_cli_defaults_match_param_defaults(self):
        from scripts.experiments.run_experiment import build_parser

        repo_root = Path(__file__).resolve().parents[2]
        parser = build_parser(repo_root)
        skip = {
            # Legacy quarter conveniences and repo-root-resolved path defaults
            # (PARAM_DEFAULTS keeps them repo-relative by design).
            "first_test_quarter", "last_test_quarter",
            "raw_dir", "fundamental_events_root", "fundamental_events_status",
            "experiments_root", "work_root", "template_dir",
        }
        mismatches = {}
        for action in parser._actions:
            if action.dest not in PARAM_DEFAULTS or action.dest in skip:
                continue
            cli_default = tuple(action.default) if isinstance(action.default, list) else action.default
            expected = PARAM_DEFAULTS[action.dest]
            expected = tuple(expected) if isinstance(expected, list) else expected
            if cli_default != expected:
                mismatches[action.dest] = (cli_default, expected)
        self.assertEqual(mismatches, {})


if __name__ == "__main__":
    unittest.main()
