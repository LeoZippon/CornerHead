import json
import re
import tempfile
import unittest
from pathlib import Path

from hl_trader.environment.llm.proxy import ScriptedLLM
from hl_trader.environment.tools import BacktestTool, FinishFoldTool, ModificationCheckTool, ToolContext, ToolError
from hl_trader.pipelines import (
    ExperimentConfig,
    ExperimentLedger,
    ExperimentPipeline,
    build_fold_schedule,
)
from hl_trader.pipelines.folds import quarter_bounds

from .fixtures_sandbox import TEMPLATE_DIR, TRADING_DAYS, FakeSnapshotProvider, nl_score_response, write_strategy

SRC_ENV_DIR = Path(__file__).resolve().parents[2] / "src" / "hl_trader" / "environment"


class ScriptedFoldAgent:
    """Deterministic stand-in for the LLM-driven Agent session."""

    def __init__(self, ctx: ToolContext) -> None:
        self.ctx = ctx

    def run(self) -> dict[str, object]:
        write_strategy(self.ctx.paths.agent_output)
        ModificationCheckTool(self.ctx).run()
        BacktestTool(self.ctx).run(mode="valid", nl_mode="on")
        FinishFoldTool(self.ctx).run()
        return {"finish_status": "fold_finished"}


def make_config(tmp: Path, **overrides) -> ExperimentConfig:
    defaults = dict(
        experiment_id="exp_e2e",
        experiments_root=tmp / "experiments",
        work_root=tmp / "sandboxes",
        template_dir=TEMPLATE_DIR,
        first_test_quarter="2022Q1",
        last_test_quarter="2022Q1",
        heldout_first_quarter="2026Q1",
        heldout_last_quarter="2026Q1",
        use_docker=False,
    )
    defaults.update(overrides)
    return ExperimentConfig(**defaults)


class FoldScheduleTest(unittest.TestCase):
    def test_fold_2022q1_matches_documented_windows(self):
        folds = build_fold_schedule("2022Q1", "2022Q2", TRADING_DAYS)
        first = folds[0]
        self.assertEqual(first.fold_id, "fold_2022Q1")
        self.assertEqual(first.input_window_start, "20200101")
        self.assertEqual(first.input_window_end, "20210930")
        self.assertEqual((first.validation_start, first.validation_end), ("20211001", "20211231"))
        self.assertEqual((first.test_start, first.test_end), ("20220101", "20220331"))
        self.assertEqual(first.valid_decision_time.strftime("%Y%m%d %H:%M"), "20211008 09:25")
        self.assertEqual(first.test_decision_time.strftime("%Y%m%d %H:%M"), "20220104 09:25")
        self.assertEqual(folds[1].validation_start, "20220101")  # previous test quarter rolls forward

    def test_heldout_must_not_overlap_development(self):
        with self.assertRaisesRegex(ValueError, "must not overlap"):
            make_config(Path("/tmp"), heldout_first_quarter="2022Q1", heldout_last_quarter="2022Q1")

    def test_quarter_bounds(self):
        self.assertEqual(quarter_bounds("2022Q1"), ("20220101", "20220331"))
        self.assertEqual(quarter_bounds("2021Q4"), ("20211001", "20211231"))


class LedgerTest(unittest.TestCase):
    def test_append_requires_record_type_and_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = ExperimentLedger(Path(tmp) / "ledger.jsonl")
            with self.assertRaisesRegex(ValueError, "record_type"):
                ledger.append({"record_type": "bogus", "experiment_id": "e", "epoch_id": "p", "fold_id": "f", "run_id": "r"})
            with self.assertRaisesRegex(ValueError, "link keys"):
                ledger.append({"record_type": "fold", "experiment_id": "e"})
            ledger.append({"record_type": "fold", "experiment_id": "e", "epoch_id": "p", "fold_id": "f", "run_id": "r"})
            self.assertEqual(len(ledger.read("fold")), 1)


class PipelineEndToEndTest(unittest.TestCase):
    def test_single_epoch_runs_meta_learning_before_fold_and_heldout(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            config = make_config(tmp)
            # NL responses: fold valid + fold frozen test + heldout frozen eval.
            proxy = ScriptedLLM([nl_score_response(), nl_score_response(), nl_score_response()])

            def meta_learner(ctx: ToolContext) -> None:
                (ctx.paths.workspace / "taste.md").write_text("prefer robust price-volume tests", encoding="utf-8")

            pipeline = ExperimentPipeline(
                config,
                FakeSnapshotProvider(),
                lambda ctx, fold, manifest: ScriptedFoldAgent(ctx),
                proxy=proxy,
                meta_learner=meta_learner,
            )
            result = pipeline.run(TRADING_DAYS)
            self.assertEqual(result["heldout_runs"], 1)
            self.assertEqual(result["final_strategy_artifact"], "strategy_epoch_001_fold_2022Q1")

            folds = pipeline.ledger.read("fold")
            self.assertEqual(len(folds), 1)
            record = folds[0]
            self.assertEqual(record["fold_status"], "frozen")
            self.assertEqual(record["finish_reason"], "fold_finished")
            self.assertFalse(record["state_changed_during_test"])
            self.assertEqual(record["selected_step_id"], "step_001")
            self.assertEqual(record["steps"][0]["status"], "accepted")
            self.assertEqual(record["steps"][0]["modification_check_ref"], "embedded:modification_delta_summary")
            self.assertIsNotNone(record["steps"][0]["modification_delta_summary"])
            self.assertIn("factor_changes", record["steps"][0]["modification_delta_summary"])
            self.assertGreater(record["validation_result"]["total_return"], 0.0)
            self.assertGreater(record["test_result"]["total_return"], 0.0)

            frozen_dir = Path(record["frozen_strategy_artifact_path"])
            self.assertTrue((frozen_dir / "manifest.json").exists())
            manifest = json.loads((frozen_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertTrue(manifest["frozen"])
            self.assertEqual(manifest["created_at_step"], "step_001")

            run_dir = config.experiment_dir / "artifacts" / record["run_id"]
            self.assertTrue((run_dir / "run_manifest.json").exists())
            self.assertTrue((run_dir / "agent_trace.jsonl").exists())
            self.assertTrue((run_dir / "results" / "test_000" / "detailed_return.json").exists())

            meta = pipeline.ledger.read("meta_learning")
            self.assertEqual(len(meta), 1)
            self.assertEqual(meta[0]["epoch_id"], "epoch_001")
            self.assertEqual(meta[0]["status"], "taste_only")
            self.assertGreater(meta[0]["taste_chars"], 0)
            heldout = pipeline.ledger.read("heldout")[0]
            self.assertEqual(heldout["strategy_artifact_id"], "strategy_epoch_001_fold_2022Q1")
            self.assertGreater(heldout["test_result"]["total_return"], 0.0)

    def test_multi_epoch_runs_meta_learning_before_each_epoch(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            config = make_config(tmp, epochs=2)
            # Epoch 1 fold valid/test, Epoch 2 fold valid/test, then heldout.
            proxy = ScriptedLLM(
                [
                    nl_score_response(),
                    nl_score_response(),
                    nl_score_response(),
                    nl_score_response(),
                    nl_score_response(),
                ]
            )
            meta_epochs: list[str] = []

            def meta_learner(ctx: ToolContext) -> None:
                meta_epochs.append(str(ctx.manifest.require("epoch_id")))
                (ctx.paths.workspace / "taste.md").write_text(
                    f"taste for {ctx.manifest.require('epoch_id')}", encoding="utf-8"
                )
                prior_path = ctx.paths.agent_output / "nl_prior" / "prior.json"
                payload = json.loads(prior_path.read_text(encoding="utf-8"))
                if payload["rules"]:
                    payload["rules"][0]["text"] = "negative regulatory evidence lowers the score"
                prior_path.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaisesRegex(ToolError, "not allowed"):
                    BacktestTool(ctx).run(mode="valid", nl_mode="off")

            pipeline = ExperimentPipeline(
                config,
                FakeSnapshotProvider(),
                lambda ctx, fold, manifest: ScriptedFoldAgent(ctx),
                proxy=proxy,
                meta_learner=meta_learner,
            )
            result = pipeline.run(TRADING_DAYS)
            self.assertEqual(result["heldout_runs"], 1)
            self.assertEqual(result["final_strategy_artifact"], "strategy_epoch_002_fold_2022Q1")
            self.assertEqual(meta_epochs, ["epoch_001", "epoch_002"])

            folds = pipeline.ledger.read("fold")
            self.assertEqual(len(folds), 2)
            self.assertEqual(folds[0]["epoch_id"], "epoch_001")
            self.assertEqual(folds[1]["epoch_id"], "epoch_002")
            meta_records = pipeline.ledger.read("meta_learning")
            self.assertEqual(len(meta_records), 2)
            self.assertEqual([r["epoch_id"] for r in meta_records], ["epoch_001", "epoch_002"])
            self.assertEqual(meta_records[1]["status"], "meta_regularized")
            heldout = pipeline.ledger.read("heldout")[0]
            self.assertEqual(heldout["strategy_artifact_id"], "strategy_epoch_002_fold_2022Q1")

    def test_failed_acceptance_falls_back_to_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            config = make_config(tmp)
            proxy = ScriptedLLM([nl_score_response(), nl_score_response(), nl_score_response()])
            pipeline = ExperimentPipeline(
                config, FakeSnapshotProvider(), lambda ctx, fold, manifest: ScriptedFoldAgent(ctx), proxy=proxy
            )
            folds = build_fold_schedule("2022Q1", "2022Q1", TRADING_DAYS)
            outcome = pipeline.run_fold(folds[0], epoch_id="epoch_001", parent=None)
            self.assertEqual(outcome.fold_status, "frozen")

            class IdleAgent:
                def run(self) -> dict[str, object]:
                    return {"finish_status": "deadline_timeout"}

            pipeline_idle = ExperimentPipeline(
                config, FakeSnapshotProvider(), lambda ctx, fold, manifest: IdleAgent(), proxy=ScriptedLLM([nl_score_response()])
            )
            second = pipeline_idle.run_fold(folds[0], epoch_id="epoch_001b", parent=outcome.frozen)
            self.assertEqual(second.fold_status, "no_update_timeout")
            self.assertEqual(second.frozen.artifact_id, outcome.frozen.artifact_id)

            # The step tree accumulated in fold 1 is handed to later folds and
            # the second fold starts positioned at the parent artifact's node.
            from hl_trader.environment.step_tree import StepTree

            experiment_tree = StepTree(config.experiment_dir / "steps")
            self.assertGreaterEqual(len(experiment_tree.nodes()), 1)
            self.assertEqual(
                experiment_tree.position_for_hash(outcome.frozen.artifact_hash),
                experiment_tree.current_node_id,
            )

    def test_two_epochs_do_not_collide_in_step_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            config = make_config(tmp, epochs=2)
            proxy = ScriptedLLM(
                [
                    nl_score_response(),
                    nl_score_response(),
                    nl_score_response(),
                    nl_score_response(),
                    nl_score_response(),
                ]
            )
            pipeline = ExperimentPipeline(
                config, FakeSnapshotProvider(), lambda ctx, fold, manifest: ScriptedFoldAgent(ctx), proxy=proxy
            )
            result = pipeline.run(TRADING_DAYS)
            self.assertEqual(result["heldout_runs"], 1)

            from hl_trader.environment.step_tree import StepTree

            nodes = StepTree(config.experiment_dir / "steps").nodes()
            self.assertEqual(len(nodes), 2)
            self.assertNotEqual(nodes[0]["node_id"], nodes[1]["node_id"])
            self.assertTrue(nodes[0]["node_id"].startswith("epoch_001__fold_2022Q1__"))
            self.assertTrue(nodes[1]["node_id"].startswith("epoch_002__fold_2022Q1__"))

    def test_meta_learning_violating_constraints_keeps_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            config = make_config(tmp)
            proxy = ScriptedLLM([nl_score_response(), nl_score_response()])
            pipeline = ExperimentPipeline(
                config, FakeSnapshotProvider(), lambda ctx, fold, manifest: ScriptedFoldAgent(ctx), proxy=proxy
            )
            folds = build_fold_schedule("2022Q1", "2022Q1", TRADING_DAYS)
            outcome = pipeline.run_fold(folds[0], epoch_id="epoch_001", parent=None)

            def bad_meta_learner(ctx: ToolContext) -> None:
                (ctx.paths.workspace / "taste.md").write_text("too many prior rules", encoding="utf-8")
                prior_path = ctx.paths.agent_output / "nl_prior" / "prior.json"
                rules = [
                    {"id": f"r{i}", "text": "x", "evidence": "e", "effect": "f"} for i in range(30)
                ]
                prior_path.write_text(json.dumps({"rules": rules}), encoding="utf-8")

            pipeline.meta_learner = bad_meta_learner
            frozen, taste = pipeline.run_meta_learning(epoch_id="epoch_002", parent=outcome.frozen)
            self.assertEqual(frozen.artifact_id, outcome.frozen.artifact_id)
            self.assertIn("too many", taste)
            meta = pipeline.ledger.read("meta_learning")[0]
            self.assertEqual(meta["status"], "rejected_kept_parent")

    def test_meta_learning_zero_diff_keeps_parent_without_new_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            config = make_config(tmp)
            pipeline = ExperimentPipeline(
                config, FakeSnapshotProvider(), lambda ctx, fold, manifest: ScriptedFoldAgent(ctx), proxy=ScriptedLLM([nl_score_response(), nl_score_response()])
            )
            fold = build_fold_schedule("2022Q1", "2022Q1", TRADING_DAYS)[0]
            outcome = pipeline.run_fold(fold, epoch_id="epoch_001", parent=None)

            def taste_only(ctx: ToolContext) -> None:
                (ctx.paths.workspace / "taste.md").write_text("keep parent unchanged", encoding="utf-8")

            pipeline.meta_learner = taste_only
            frozen, taste = pipeline.run_meta_learning(epoch_id="epoch_002", parent=outcome.frozen)
            self.assertEqual(frozen.artifact_id, outcome.frozen.artifact_id)
            self.assertEqual(taste, "keep parent unchanged")
            meta = pipeline.ledger.read("meta_learning")[0]
            self.assertEqual(meta["status"], "taste_only_kept_parent")
            artifacts = list((config.experiment_dir / "strategy_artifacts" / "epoch_002").glob("*"))
            self.assertEqual(artifacts, [])


def _docker_with_image() -> bool:
    import subprocess

    from hl_trader.environment.executor import docker_available
    from hl_trader.environment.sandbox import SandboxSpec

    if not docker_available():
        return False
    check = subprocess.run(["docker", "image", "inspect", SandboxSpec().image], capture_output=True, timeout=30)
    return check.returncode == 0


@unittest.skipUnless(_docker_with_image(), "docker daemon or sandbox image unavailable")
class DockerizedFoldE2ETest(unittest.TestCase):
    def test_fold_runs_with_containerized_strategy_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            config = make_config(tmp, use_docker=True)
            proxy = ScriptedLLM([nl_score_response(), nl_score_response()])
            pipeline = ExperimentPipeline(
                config, FakeSnapshotProvider(), lambda ctx, fold, manifest: ScriptedFoldAgent(ctx), proxy=proxy
            )
            fold = build_fold_schedule("2022Q1", "2022Q1", TRADING_DAYS)[0]
            outcome = pipeline.run_fold(fold, epoch_id="epoch_001", parent=None)
            self.assertEqual(outcome.fold_status, "frozen")
            self.assertGreater(outcome.test_summary["total_return"], 0.0)


class ArchitectureBoundaryTest(unittest.TestCase):
    def test_environment_does_not_import_agent(self):
        pattern = re.compile(r"^\s*(from|import)\s+hl_trader\.agent\b", re.MULTILINE)
        offenders = [
            str(path)
            for path in SRC_ENV_DIR.rglob("*.py")
            if pattern.search(path.read_text(encoding="utf-8"))
        ]
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
