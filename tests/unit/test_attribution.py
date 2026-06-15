import unittest

import pandas as pd

from hl_trader.environment.attribution import build_attribution_report, shapley_attribution


def candidates_frame():
    return pd.DataFrame(
        {
            "ts_code": ["000001.SZ", "000002.SZ", "000003.SZ", "000004.SZ"],
            "factor_score": [1.0, 0.5, -0.5, -1.0],
            "factor_alpha": [1.0, 0.5, -0.5, -1.0],
            "factor_noise": [0.0, 0.0, 0.0, 0.0],
        }
    )


def linear_evaluate(scores: pd.Series) -> float:
    """Toy market: replay return is proportional to the score-weighted payoff."""
    payoff = pd.Series([0.10, 0.05, -0.05, -0.10], index=scores.index)
    return float((scores * payoff).sum())


class ShapleyAttributionTest(unittest.TestCase):
    def test_informative_factor_gets_the_contribution(self):
        frame = candidates_frame()
        values = shapley_attribution(frame, ["alpha", "noise"], linear_evaluate)
        self.assertGreater(values["alpha"]["shapley_value"], 0.0)
        self.assertAlmostEqual(values["noise"]["standalone_return"], 0.0, places=9)
        self.assertGreater(values["alpha"]["shapley_value"], abs(values["noise"]["shapley_value"]))
        # Efficiency: contributions sum to the grand-coalition value minus the empty value.
        full = linear_evaluate(pd.concat([frame["factor_alpha"], frame["factor_noise"]], axis=1).pipe(
            lambda df: (df / df.abs().to_numpy().max()).mean(axis=1)
        ))
        total = values["alpha"]["shapley_value"] + values["noise"]["shapley_value"]
        self.assertAlmostEqual(total, full, places=6)

    def test_permutation_sampling_for_many_factors(self):
        frame = candidates_frame()
        for i in range(10):
            frame[f"factor_f{i}"] = frame["factor_alpha"]
        ids = [f"f{i}" for i in range(10)]
        values = shapley_attribution(frame, ids, linear_evaluate, max_exact_factors=4, permutation_samples=8)
        self.assertEqual(set(values), set(ids))
        # Identical factors: only the first added in a permutation earns marginal
        # credit, so individual values may be zero, but efficiency must hold.
        total = sum(v["shapley_value"] for v in values.values())
        grand = linear_evaluate(frame["factor_alpha"] / frame["factor_alpha"].abs().max())
        self.assertAlmostEqual(total, grand, places=6)
        self.assertTrue(all(v["shapley_value"] >= 0 for v in values.values()))

    def test_report_includes_rationale_and_skip_reasons(self):
        frame = candidates_frame()
        factors = [
            {"id": "alpha", "rationale": "动量假设"},
            {"id": "missing", "rationale": "无对应列"},
        ]
        report = build_attribution_report(frame, factors, linear_evaluate, full_return=0.2)
        self.assertIsNone(report["skipped"])
        rows = {row["id"]: row for row in report["factors"]}
        self.assertEqual(rows["alpha"]["rationale"], "动量假设")
        self.assertIn("shapley_value", rows["alpha"])
        self.assertEqual(rows["missing"]["skipped"], "no_factor_column")

    def test_report_skips_without_factor_columns(self):
        frame = candidates_frame()[["ts_code", "factor_score"]]
        report = build_attribution_report(frame, [{"id": "alpha", "rationale": "r"}], linear_evaluate, full_return=0.1)
        self.assertIn("no per-factor columns", report["skipped"])


if __name__ == "__main__":
    unittest.main()
