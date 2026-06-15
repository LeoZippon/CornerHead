import tempfile
import unittest
from pathlib import Path

from hl_trader.agent.prompts import build_system_prompt
from hl_trader.environment.artifacts import artifact_hash
from hl_trader.environment.step_tree import StepTree

from .test_artifacts import write_artifact


class StepTreeTest(unittest.TestCase):
    def test_records_nodes_with_parent_lineage_and_position(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            artifact = write_artifact(tmp / "artifact")
            digest = artifact_hash(artifact)
            tree = StepTree(tmp / "steps")
            node1 = tree.record_step(
                artifact,
                fold_id="fold_2022Q1",
                result_name="valid_000",
                artifact_hash=digest,
                metrics={"total_return": 0.01},
                complete_validation=True,
            )
            node2 = tree.record_step(
                artifact,
                fold_id="fold_2022Q1",
                result_name="valid_001",
                artifact_hash=digest,
                metrics={"total_return": 0.02},
                complete_validation=True,
            )
            reloaded = StepTree(tmp / "steps")
            self.assertEqual(reloaded.current_node_id, node2)
            nodes = {n["node_id"]: n for n in reloaded.nodes()}
            self.assertIsNone(nodes[node1]["parent_node_id"])
            self.assertEqual(nodes[node2]["parent_node_id"], node1)
            self.assertTrue((tmp / "steps" / node1 / "factor" / "main.py").exists())
            self.assertEqual(reloaded.position_for_hash(digest), node2)
            rendered = reloaded.render_ascii()
            self.assertIn(node1, rendered)
            self.assertIn("<- current", rendered)
            with self.assertRaisesRegex(ValueError, "already exists"):
                reloaded.record_step(
                    artifact, fold_id="fold_2022Q1", result_name="valid_000",
                    artifact_hash=digest, metrics={}, complete_validation=True,
                )

    def test_epoch_id_prevents_cross_epoch_node_collisions(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            artifact = write_artifact(tmp / "artifact")
            digest = artifact_hash(artifact)
            tree = StepTree(tmp / "steps")
            node1 = tree.record_step(
                artifact,
                epoch_id="epoch_001",
                fold_id="fold_2022Q1",
                result_name="valid_000",
                artifact_hash=digest,
                metrics={},
                complete_validation=True,
            )
            node2 = tree.record_step(
                artifact,
                epoch_id="epoch_002",
                fold_id="fold_2022Q1",
                result_name="valid_000",
                artifact_hash=digest,
                metrics={},
                complete_validation=True,
            )
            self.assertNotEqual(node1, node2)
            self.assertIn("epoch_001", node1)
            self.assertIn("epoch_002", node2)

    def test_set_position_validates_node(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = StepTree(Path(tmp) / "steps")
            with self.assertRaisesRegex(ValueError, "unknown"):
                tree.set_position("nope")
            tree.set_position(None)
            self.assertIsNone(tree.current_node_id)


class PhasePromptTest(unittest.TestCase):
    def test_phase_and_step_tree_sections(self):
        base = dict(fold_info={"fold_id": "f"}, acceptance_rules={})
        exploration = build_system_prompt(**base)
        self.assertIn("探索期", exploration)
        self.assertNotIn("Step 产物树", exploration)
        convergence = build_system_prompt(**base, phase="convergence", step_tree_enabled=True)
        self.assertIn("收敛期", convergence)
        self.assertIn("不再修改", convergence)
        self.assertIn("Step 产物树", convergence)


if __name__ == "__main__":
    unittest.main()
