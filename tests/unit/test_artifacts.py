import json
import os
import tempfile
import unittest
from pathlib import Path

from hl_trader.environment.artifacts import (
    ArtifactError,
    ModificationConstraints,
    artifact_hash,
    init_from_template,
    load_strategy_artifact,
    modification_delta,
)

TEMPLATE_DIR = Path(__file__).resolve().parents[2] / "configs" / "agent_output_template"

VALID_MAIN = '''
import os
from pathlib import Path
import pandas as pd

SNAPSHOT_DIR = Path(os.environ.get("MQ_SNAPSHOT_DIR", "/mnt/snapshot"))


def factor_momentum():
    return None


def generate_candidates() -> pd.DataFrame:
    return pd.DataFrame(columns=["ts_code", "factor_score", "reason", "source_artifacts"])
'''

VALID_FACTORS = {
    "factors": [
        {"id": "momentum", "function": "factor_momentum", "description": "d", "lookback_days": 20, "direction": "positive", "rationale": "动量在 A 股横截面上有持续溢价"}
    ]
}
VALID_PRIOR = {"rules": [{"id": "r1", "text": "t", "evidence": "e", "effect": "lower_score"}]}


def write_artifact(root: Path, *, main: str = VALID_MAIN, factors: dict = VALID_FACTORS, prior: dict = VALID_PRIOR) -> Path:
    init_from_template(TEMPLATE_DIR, root)
    (root / "factor" / "main.py").write_text(main, encoding="utf-8")
    (root / "factor" / "factors.json").write_text(json.dumps(factors), encoding="utf-8")
    (root / "nl_prior" / "prior.json").write_text(json.dumps(prior), encoding="utf-8")
    return root


class StrategyArtifactTest(unittest.TestCase):
    def test_loads_valid_artifact_and_hash_is_stable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = write_artifact(Path(tmp))
            artifact = load_strategy_artifact(root)
            self.assertEqual(len(artifact.factors), 1)
            self.assertEqual(len(artifact.rules), 1)
            self.assertEqual(artifact.artifact_hash, artifact_hash(root))
            self.assertTrue(artifact.artifact_hash.startswith("sha256:"))

    def test_rejects_missing_registered_function(self):
        bad = {"factors": [{"id": "x", "function": "missing_fn", "description": "d", "lookback_days": 1, "direction": "p", "rationale": "r"}]}
        with tempfile.TemporaryDirectory() as tmp:
            root = write_artifact(Path(tmp), factors=bad)
            with self.assertRaisesRegex(ArtifactError, "missing_fn"):
                load_strategy_artifact(root)

    def test_rejects_stage_directory_references_in_formal_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = write_artifact(Path(tmp), main=VALID_MAIN + '\nBAD = "/mnt/snapshots/train"\n')
            with self.assertRaisesRegex(ArtifactError, "stage directories"):
                load_strategy_artifact(root)

    def test_rejects_bad_prior_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = write_artifact(Path(tmp), prior={"rules": [{"id": "r1", "text": "", "evidence": "e", "effect": "x"}]})
            with self.assertRaisesRegex(ArtifactError, "empty text"):
                load_strategy_artifact(root)

    def test_template_initializes_valid_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_from_template(TEMPLATE_DIR, root)
            artifact = load_strategy_artifact(root)
            self.assertEqual(artifact.factors, ())
            self.assertEqual(artifact.rules, ())

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink not available on this platform")
    def test_rejects_symlinks_inside_artifact_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = write_artifact(Path(tmp))
            (root / "factor" / "linked.py").symlink_to(root / "nl_prior" / "prior.json")
            with self.assertRaisesRegex(ArtifactError, "symlinks"):
                load_strategy_artifact(root)


class ModificationDeltaTest(unittest.TestCase):
    def test_counts_changed_files_and_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = write_artifact(Path(tmp) / "parent")
            work = write_artifact(Path(tmp) / "work")
            new_factors = {
                "factors": VALID_FACTORS["factors"]
                + [{"id": "value", "function": "factor_momentum", "description": "v", "lookback_days": 5, "direction": "negative", "rationale": "估值反转假设"}]
            }
            (work / "factor" / "factors.json").write_text(json.dumps(new_factors), encoding="utf-8")
            (work / "nl_prior" / "prior.json").write_text(json.dumps({"rules": []}), encoding="utf-8")
            delta = modification_delta(parent, work)
            self.assertEqual(set(delta.changed_files), {"factor/factors.json", "nl_prior/prior.json"})
            self.assertEqual(delta.factors_added, ("value",))
            self.assertEqual(delta.rules_removed, ("r1",))
            self.assertEqual(delta.total_factors, 2)
            self.assertEqual(delta.total_rules, 0)

    def test_readonly_violation_blocks_even_in_initial_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = write_artifact(Path(tmp) / "parent")
            work = write_artifact(Path(tmp) / "work")
            (work / "factor" / "README.md").write_text("tampered", encoding="utf-8")
            delta = modification_delta(parent, work)
            allowed, reasons = ModificationConstraints(is_initial_artifact=True).evaluate(delta)
            self.assertFalse(allowed)
            self.assertIn("readonly", reasons[0])

    def test_constraint_thresholds(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = write_artifact(Path(tmp) / "parent")
            work = write_artifact(Path(tmp) / "work")
            many_rules = {
                "rules": [
                    {"id": f"r{i}", "text": "t" * 10, "evidence": "e", "effect": "x"} for i in range(6)
                ]
            }
            (work / "nl_prior" / "prior.json").write_text(json.dumps(many_rules), encoding="utf-8")
            delta = modification_delta(parent, work)
            allowed, reasons = ModificationConstraints(max_rule_changes=2).evaluate(delta)
            self.assertFalse(allowed)
            self.assertTrue(any("rule changes" in reason for reason in reasons))
            allowed, _ = ModificationConstraints(max_rule_changes=10).evaluate(delta)
            self.assertTrue(allowed)


if __name__ == "__main__":
    unittest.main()
