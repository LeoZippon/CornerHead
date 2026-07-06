"""Interactive (HITL) orchestration tests: gating, directives, resume, analysis.

Uses a fake pipeline that mirrors the real ledger/freeze contracts (real
ExperimentLedger, real artifact hashes) so resume verification runs the same
code paths as production without Docker or LLM calls.
"""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

import pandas as pd

from autotrade.environment.artifacts import artifact_hash, model_artifact_hash
from autotrade.pipelines import ExperimentConfig, ExperimentLedger, FoldOutcome, FrozenArtifact
from autotrade.pipelines.fold_analysis import (
    analyze_fold,
    build_fold_analysis_messages,
    guarded_record_view,
    read_strategy_files,
)
from autotrade.pipelines.interactive import (
    CONTROL_NAME,
    HELDOUT_SESSION_KEY,
    SCHEDULE_NAME,
    STATUS_NAME,
    ControlState,
    ExperimentStopped,
    InteractiveExperimentRunner,
    PARAM_DEFAULTS,
    StatusReporter,
    fold_session_key,
    meta_session_key,
    read_control,
    read_status,
    resolve_options,
    write_control,
)


def _weekday_trading_days(first: str, last: str) -> list[str]:
    days = pd.date_range(first, last, freq="B")
    return [day.strftime("%Y%m%d") for day in days]


TRADING_DAYS = _weekday_trading_days("2020-01-01", "2023-12-31")


class FakePipeline:
    """Mirrors the ExperimentPipeline surface the interactive runner drives."""

    def __init__(self, config: ExperimentConfig, *, meta_enabled: bool = True) -> None:
        self.config = config
        self.ledger = ExperimentLedger(config.ledger_path)
        self.meta_learner = object() if meta_enabled else None
        self.calls: list[tuple] = []
        self._counter = 0

    # -- helpers -----------------------------------------------------------
    def _freeze_fake(self, epoch_id: str, artifact_id: str, *, content: str) -> FrozenArtifact:
        path = Path(self.config.experiment_dir) / "strategy_artifacts" / epoch_id / artifact_id
        path.mkdir(parents=True, exist_ok=True)
        (path / "main.py").write_text(content, encoding="utf-8")
        return FrozenArtifact(
            artifact_id=artifact_id,
            path=path,
            artifact_hash=artifact_hash(path),
            model_path=None,
            model_artifact_hash=model_artifact_hash(path / ".missing_models"),
        )

    # -- pipeline surface ---------------------------------------------------
    def run_meta_learning(self, *, epoch_id, parent, previous_taste="", visible_fold=None, directive_override=None):
        self.calls.append(("meta", epoch_id, previous_taste, directive_override))
        taste = f"taste-{epoch_id}"
        meta_dir = Path(self.config.experiment_dir) / "meta_learning" / epoch_id
        meta_dir.mkdir(parents=True, exist_ok=True)
        (meta_dir / "taste.md").write_text(taste + "\n", encoding="utf-8")
        self.ledger.append(
            {
                "record_type": "meta_learning",
                "experiment_id": self.config.experiment_id,
                "epoch_id": epoch_id,
                "fold_id": f"{epoch_id}_meta_learning",
                "run_id": f"run_meta_{epoch_id}",
                "status": "taste_only" if parent is None else "taste_only_kept_parent",
                "taste_path": str(meta_dir / "taste.md"),
                "meta_learning_directive": directive_override or "",
            }
        )
        return parent, taste

    def run_fold(self, fold, *, epoch_id, parent, taste_prompt="", fold_directive=""):
        self.calls.append(("fold", epoch_id, fold.fold_id, taste_prompt, fold_directive, parent.artifact_id if parent else None))
        self._counter += 1
        frozen = self._freeze_fake(epoch_id, f"strategy_{epoch_id}_{fold.fold_id}", content=f"# v{self._counter}\n")
        self.ledger.append(
            {
                "record_type": "fold",
                "experiment_id": self.config.experiment_id,
                "epoch_id": epoch_id,
                "fold_id": fold.fold_id,
                "run_id": f"run_{self._counter:03d}",
                "fold_status": "frozen",
                "fold_directive": fold_directive or None,
                "frozen_strategy_artifact_id": frozen.artifact_id,
                "frozen_strategy_artifact_hash": frozen.artifact_hash,
                "frozen_model_artifact_hash": frozen.model_artifact_hash,
                "frozen_strategy_artifact_path": str(frozen.path),
                "frozen_model_artifact_path": None,
                "validation_result": {"total_return": 0.01, "sharpe": 0.5, "max_drawdown": 0.05},
                "test_result": {"total_return": 0.02, "sharpe": 0.6, "max_drawdown": 0.04},
            }
        )
        return FoldOutcome(
            fold_id=fold.fold_id,
            run_id=f"run_{self._counter:03d}",
            fold_status="frozen",
            frozen=frozen,
            validation_summary={"total_return": 0.01},
            test_summary={"total_return": 0.02},
        )

    def run_heldout(self, final, trading_days, *, epoch_id, skip_labels=None):
        self.calls.append(("heldout", epoch_id, frozenset(skip_labels or ())))
        from autotrade.pipelines.folds import heldout_periods

        summaries = []
        for period in heldout_periods(
            self.config.heldout_first_period,
            self.config.heldout_last_period,
            trading_days,
            period=self.config.fold_period,
        ):
            if skip_labels and str(period["label"]) in skip_labels:
                continue
            self.ledger.append(
                {
                    "record_type": "heldout",
                    "experiment_id": self.config.experiment_id,
                    "epoch_id": epoch_id,
                    "fold_id": f"heldout_{period['label']}",
                    "run_id": f"run_heldout_{period['label']}",
                    "test_result": {"total_return": 0.0},
                }
            )
            summaries.append({"total_return": 0.0})
        return summaries


class InteractiveRunnerTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.config = ExperimentConfig(
            experiment_id="hitl_exp",
            experiments_root=self.root / "experiments",
            work_root=self.root / "sandboxes",
            template_dir=self.root / "template",
            first_test_period="2022Q1",
            last_test_period="2022Q2",
            heldout_first_period="2023Q1",
            heldout_last_period="2023Q1",
            epochs=1,
            use_docker=False,
        )
        self.hitl_dir = self.config.experiment_dir / "hitl"
        self.hitl_dir.mkdir(parents=True, exist_ok=True)

    def _runner(self, pipeline, poll_seconds: float = 0.02) -> InteractiveExperimentRunner:
        status = StatusReporter(self.hitl_dir / STATUS_NAME, work_root=self.config.work_root, interval_seconds=60.0)
        return InteractiveExperimentRunner(
            pipeline, hitl_dir=self.hitl_dir, poll_seconds=poll_seconds, status=status
        )

    def _control(self, **kwargs) -> None:
        write_control(self.hitl_dir / CONTROL_NAME, ControlState(**kwargs))

    def test_auto_mode_runs_meta_folds_heldout_in_order(self) -> None:
        pipeline = FakePipeline(self.config)
        self._control(mode="auto")
        result = self._runner(pipeline).run(TRADING_DAYS)
        kinds = [call[0] for call in pipeline.calls]
        self.assertEqual(kinds, ["meta", "fold", "fold", "heldout"])
        # Parent chains fold N frozen -> fold N+1 parent.
        self.assertIsNone(pipeline.calls[1][5])
        self.assertEqual(pipeline.calls[2][5], "strategy_epoch_001_fold_2022Q1")
        # Taste from the epoch's meta session reaches every fold.
        self.assertEqual(pipeline.calls[1][3], "taste-epoch_001")
        self.assertEqual(result["final_strategy_artifact"], "strategy_epoch_001_fold_2022Q2")
        self.assertEqual(result["heldout_runs"], 1)
        schedule = json.loads((self.hitl_dir / SCHEDULE_NAME).read_text(encoding="utf-8"))
        self.assertEqual(len(schedule["sessions"]), 4)
        status = read_status(self.hitl_dir / STATUS_NAME)
        self.assertEqual(status["state"], "completed")
        self.assertEqual(status["completed_sessions"], 4)

    def test_step_mode_waits_for_approval_and_passes_directives(self) -> None:
        pipeline = FakePipeline(self.config)
        self._control(
            mode="step",
            approved_sessions=(meta_session_key("epoch_001"),),
            directives={
                meta_session_key("epoch_001"): "meta directive",
                fold_session_key("epoch_001", "fold_2022Q1"): "try industry-neutral momentum",
            },
        )
        runner = self._runner(pipeline)
        result_box: dict[str, object] = {}
        thread = threading.Thread(target=lambda: result_box.update(runner.run(TRADING_DAYS)), daemon=True)
        thread.start()
        # Meta was pre-approved; the first fold must block in waiting_user.
        deadline = time.time() + 5
        while time.time() < deadline:
            status = read_status(self.hitl_dir / STATUS_NAME)
            if status.get("state") == "waiting_user" and status.get("session_key") == "epoch_001/fold_2022Q1":
                break
            time.sleep(0.01)
        else:
            self.fail(f"first fold never reached waiting_user: {read_status(self.hitl_dir / STATUS_NAME)}")
        self.assertEqual([call[0] for call in pipeline.calls], ["meta"])
        self.assertEqual(pipeline.calls[0][3], "meta directive")
        # Approve everything else.
        self._control(
            mode="step",
            approved_sessions=(
                meta_session_key("epoch_001"),
                fold_session_key("epoch_001", "fold_2022Q1"),
                fold_session_key("epoch_001", "fold_2022Q2"),
                HELDOUT_SESSION_KEY,
            ),
            directives={fold_session_key("epoch_001", "fold_2022Q1"): "try industry-neutral momentum"},
        )
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result_box.get("heldout_runs"), 1)
        fold_calls = [call for call in pipeline.calls if call[0] == "fold"]
        self.assertEqual(fold_calls[0][4], "try industry-neutral momentum")
        self.assertEqual(fold_calls[1][4], "")

    def test_stop_request_halts_at_session_boundary(self) -> None:
        pipeline = FakePipeline(self.config)
        self._control(mode="auto", request="stop")
        with self.assertRaises(ExperimentStopped):
            self._runner(pipeline).run(TRADING_DAYS)
        self.assertEqual(pipeline.calls, [])

    def test_resume_skips_completed_sessions_and_rebuilds_parent(self) -> None:
        pipeline = FakePipeline(self.config)
        self._control(mode="auto")
        # First run: complete meta + first fold, then stop before the second fold.
        original_run_fold = pipeline.run_fold
        def stop_after_first(fold, **kwargs):
            outcome = original_run_fold(fold, **kwargs)
            self._control(mode="auto", request="stop")
            return outcome
        pipeline.run_fold = stop_after_first
        with self.assertRaises(ExperimentStopped):
            self._runner(pipeline).run(TRADING_DAYS)
        self.assertEqual([call[0] for call in pipeline.calls], ["meta", "fold"])

        # Resume with a fresh pipeline over the same ledger.
        resumed = FakePipeline(self.config)
        self._control(mode="auto")
        result = self._runner(resumed).run(TRADING_DAYS)
        kinds = [call[0] for call in resumed.calls]
        self.assertEqual(kinds, ["fold", "heldout"])  # meta + fold 1 restored, not re-run
        self.assertEqual(resumed.calls[0][2], "fold_2022Q2")
        self.assertEqual(resumed.calls[0][5], "strategy_epoch_001_fold_2022Q1")  # parent from ledger
        self.assertEqual(resumed.calls[0][3], "taste-epoch_001")  # taste restored from file
        self.assertEqual(result["final_strategy_artifact"], "strategy_epoch_001_fold_2022Q2")

    def test_resume_detects_tampered_frozen_artifact(self) -> None:
        pipeline = FakePipeline(self.config)
        self._control(mode="auto", request=None)
        original_run_fold = pipeline.run_fold
        def stop_after_first(fold, **kwargs):
            outcome = original_run_fold(fold, **kwargs)
            self._control(mode="auto", request="stop")
            return outcome
        pipeline.run_fold = stop_after_first
        with self.assertRaises(ExperimentStopped):
            self._runner(pipeline).run(TRADING_DAYS)
        frozen_main = (
            Path(self.config.experiment_dir)
            / "strategy_artifacts" / "epoch_001" / "strategy_epoch_001_fold_2022Q1" / "main.py"
        )
        frozen_main.write_text("# tampered\n", encoding="utf-8")
        self._control(mode="auto")
        with self.assertRaisesRegex(RuntimeError, "hash changed"):
            self._runner(FakePipeline(self.config)).run(TRADING_DAYS)

    def test_resume_skips_recorded_heldout_periods(self) -> None:
        pipeline = FakePipeline(self.config)
        self._control(mode="auto")
        self._runner(pipeline).run(TRADING_DAYS)
        # Second full pass: everything restored, heldout fully recorded -> no calls.
        resumed = FakePipeline(self.config)
        result = self._runner(resumed).run(TRADING_DAYS)
        self.assertEqual(resumed.calls, [])
        self.assertEqual(result["heldout_runs"], 0)

    def test_orphan_frozen_artifact_fails_fast(self) -> None:
        pipeline = FakePipeline(self.config)
        orphan = (
            Path(self.config.experiment_dir) / "strategy_artifacts" / "epoch_001" / "strategy_epoch_001_fold_2022Q1"
        )
        orphan.mkdir(parents=True)
        self._control(mode="auto")
        with self.assertRaisesRegex(RuntimeError, "orphan frozen artifact"):
            self._runner(pipeline).run(TRADING_DAYS)

    def test_post_fold_hook_failure_is_advisory(self) -> None:
        pipeline = FakePipeline(self.config, meta_enabled=False)
        self._control(mode="auto")
        status = StatusReporter(self.hitl_dir / STATUS_NAME, work_root=self.config.work_root, interval_seconds=60.0)
        def broken_hook(record, outcome):
            raise RuntimeError("analysis provider down")
        runner = InteractiveExperimentRunner(
            pipeline, hitl_dir=self.hitl_dir, poll_seconds=0.02, post_fold_hook=broken_hook, status=status
        )
        result = runner.run(TRADING_DAYS)
        self.assertEqual(result["heldout_runs"], 1)
        self.assertIn("analysis provider down", str(read_status(self.hitl_dir / STATUS_NAME)["analysis_error"]))


class ResolveOptionsTest(unittest.TestCase):
    def test_defaults_merge_and_path_resolution(self) -> None:
        repo_root = Path("/repo")
        options = resolve_options(
            {
                "experiment_id": "exp1",
                "first_test_period": "2022Q1",
                "last_test_period": "2022Q2",
                "heldout_first_period": "2023Q1",
                "heldout_last_period": "2023Q1",
            },
            repo_root,
        )
        self.assertEqual(options.raw_dir, repo_root / "data/raw")
        self.assertEqual(options.model, PARAM_DEFAULTS["model"])
        self.assertEqual(options.web_search_engines, ("tavily", "semantic_scholar"))
        self.assertTrue(options.analysis_enabled)

    def test_unknown_and_missing_params_fail_fast(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown experiment parameters"):
            resolve_options({"experiment_id": "x", "no_such_knob": 1}, Path("/repo"))
        with self.assertRaisesRegex(ValueError, "missing required"):
            resolve_options({"experiment_id": "x"}, Path("/repo"))


class ControlFileTest(unittest.TestCase):
    def test_control_round_trip_and_bad_values_degrade_safely(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "control.json"
            write_control(path, ControlState(mode="auto", request="pause", approved_sessions=("a",), directives={"a": "d"}))
            state = read_control(path)
            self.assertEqual((state.mode, state.request, state.approved_sessions, state.directives), ("auto", "pause", ("a",), {"a": "d"}))
            path.write_text(json.dumps({"mode": "bogus", "request": "bogus"}), encoding="utf-8")
            state = read_control(path)
            self.assertEqual((state.mode, state.request), ("step", None))
            self.assertEqual(read_control(Path(tmp) / "missing.json").mode, "step")


class FoldAnalysisTest(unittest.TestCase):
    def test_guarded_view_excludes_test_evidence(self) -> None:
        record = {
            "epoch_id": "epoch_001",
            "fold_id": "fold_2022Q1",
            "validation_period": "20211001..20211231",
            "test_period": "20220101..20220331",
            "validation_result": {"total_return": 0.01},
            "test_result": {"total_return": 0.09},
            "fold_directive": "try X",
        }
        view = guarded_record_view(record)
        self.assertNotIn("test_result", view)
        self.assertNotIn("test_period", view)
        self.assertEqual(view["validation_result"], {"total_return": 0.01})
        messages = build_fold_analysis_messages(record, [{"path": "main.py", "content": "print(1)", "truncated": False}])
        user = messages[1]["content"]
        self.assertNotIn("0.09", user)
        self.assertNotIn("20220101..20220331", user)
        self.assertIn("print(1)", user)

    def test_analyze_fold_writes_markdown_and_sidecar(self) -> None:
        class FakeProxy:
            provider = "fake"
            model = "fake-model"
            def complete(self, messages, *, json_mode, timeout_seconds, max_tokens=None):
                assert not json_mode
                from types import SimpleNamespace
                return SimpleNamespace(content="## 策略逻辑概述\n看多动量。", usage={"total_tokens": 10})

        with tempfile.TemporaryDirectory() as tmp:
            strategy = Path(tmp) / "strategy"
            strategy.mkdir()
            (strategy / "main.py").write_text("def main(ctx):\n    pass\n", encoding="utf-8")
            (strategy / "manifest.json").write_text("{}", encoding="utf-8")
            out_dir = Path(tmp) / "analysis"
            record = {"epoch_id": "epoch_001", "fold_id": "fold_2022Q1", "validation_result": {"total_return": 0.01}}
            md_path = analyze_fold(
                FakeProxy(), ledger_record=record, strategy_dir=strategy, model_dir=None, out_dir=out_dir
            )
            self.assertIn("看多动量", md_path.read_text(encoding="utf-8"))
            meta = json.loads((out_dir / "epoch_001__fold_2022Q1.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["status"], "ok")
            self.assertEqual(meta["guarded_view"], "validation_only")

    def test_read_strategy_files_orders_main_first_and_skips_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            strategy = Path(tmp)
            (strategy / "aaa.py").write_text("a", encoding="utf-8")
            (strategy / "main.py").write_text("m", encoding="utf-8")
            (strategy / "manifest.json").write_text("{}", encoding="utf-8")
            (strategy / "weights.pt").write_bytes(b"\x00\x01")
            entries = read_strategy_files(strategy)
            self.assertEqual(entries[0]["path"], "main.py")
            paths = [entry["path"] for entry in entries]
            self.assertNotIn("manifest.json", paths)
            binary = next(entry for entry in entries if entry["path"] == "weights.pt")
            self.assertIn("non-text", binary["skipped"])


if __name__ == "__main__":
    unittest.main()
