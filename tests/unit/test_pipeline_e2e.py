import json
import re
import argparse
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from autotrade.agent import AgentSessionConfig
from autotrade.environment.artifacts import artifact_hash, model_artifact_hash
from autotrade.environment.data.summary import write_agent_data_summary
from autotrade.environment.runtime import RunManifest
from autotrade.environment.sandbox import LocalSandbox
from autotrade.environment.data.snapshot import SnapshotConfig
from autotrade.environment.llm.proxy import ScriptedLLM
from autotrade.environment.tools import BacktestTool, FinishFoldTool, ModificationCheckTool, ToolContext, ToolError
from autotrade.pipelines import (
    ExperimentConfig,
    ExperimentLedger,
    ExperimentPipeline,
    FrozenArtifact,
    build_fold_schedule,
)
from autotrade.pipelines.folds import heldout_periods, period_bounds, quarter_bounds
from autotrade.pipelines.meta_schedule import meta_learning_trigger_counts
from autotrade.pipelines.experiment import _agent_visible_ledger_record
from autotrade.pipelines.assembly import _session_config_summary
from scripts.experiments._cli import (
    EXPERIMENT_META_REBUILD_HELP,
    add_meta_sandbox_arguments,
    build_meta_learning_managed_proxy_spec,
    build_meta_learning_sandbox_spec,
)
from autotrade.environment.sandbox import SandboxSpec

from .fixtures_sandbox import TEMPLATE_DIR, TRADING_DAYS, FakeSnapshotProvider, write_strategy

SRC_ENV_DIR = Path(__file__).resolve().parents[2] / "src" / "autotrade" / "environment"


class ScriptedFoldAgent:
    """Deterministic stand-in for the LLM-driven Agent session."""

    def __init__(self, ctx: ToolContext) -> None:
        self.ctx = ctx

    def run(self) -> dict[str, object]:
        write_strategy(self.ctx.paths.agent_output)
        ModificationCheckTool(self.ctx).run()
        BacktestTool(self.ctx).run(mode="valid")
        FinishFoldTool(self.ctx).run()
        return {"finish_status": "fold_finished"}


class ModelArtifactFoldAgent:
    """Fold agent that produces a small persisted model parameter artifact."""

    def __init__(self, ctx: ToolContext) -> None:
        self.ctx = ctx

    def run(self) -> dict[str, object]:
        write_strategy(self.ctx.paths.agent_output)
        (self.ctx.paths.model_artifacts / "params.json").write_text('{"threshold": 0.42}\n', encoding="utf-8")
        ModificationCheckTool(self.ctx).run()
        BacktestTool(self.ctx).run(mode="valid")
        FinishFoldTool(self.ctx).run()
        return {"finish_status": "fold_finished"}


class SwitchingGenerationProvider(FakeSnapshotProvider):
    """Simulates a committed raw-lake publication between input builds."""

    def __init__(self, *, switch_after: int) -> None:
        self.switch_after = switch_after
        self.builds = 0

    def _stamp(self, manifest: dict[str, object]) -> dict[str, object]:
        self.builds += 1
        generation_id = "generation_before" if self.builds <= self.switch_after else "generation_after"
        return {**manifest, "raw_generation": {"generation_id": generation_id}}

    def decision_snapshot(self, decision_time, out_dir):
        return self._stamp(super().decision_snapshot(decision_time, out_dir))

    def replay_slot(self, start, end, out_dir, *, label, available_from=None):
        return self._stamp(super().replay_slot(start, end, out_dir, label=label))


def make_config(tmp: Path, **overrides) -> ExperimentConfig:
    defaults = dict(
        experiment_id="exp_e2e",
        experiments_root=tmp / "experiments",
        work_root=tmp / "sandboxes",
        template_dir=TEMPLATE_DIR,
        first_test_period="2022Q1",
        last_test_period="2022Q1",
        heldout_first_period="2026Q1",
        heldout_last_period="2026Q1",
        use_docker=False,
        # The harness default is single-epoch (the production default is 3);
        # multi-epoch tests override explicitly.
        epochs=1,
    )
    defaults.update(overrides)
    return ExperimentConfig(**defaults)


class FoldScheduleTest(unittest.TestCase):
    def test_meta_learning_interval_keeps_epoch_start_and_avoids_trailing_run(self):
        self.assertEqual(meta_learning_trigger_counts(8, 0), (0,))
        self.assertEqual(meta_learning_trigger_counts(8, 3), (0, 3, 6))
        self.assertEqual(meta_learning_trigger_counts(8, 8), (0,))

    def test_fold_2022q1_matches_documented_windows(self):
        folds = build_fold_schedule("2022Q1", "2022Q2", TRADING_DAYS)
        first = folds[0]
        self.assertEqual(first.fold_id, "fold_2022Q1")
        self.assertEqual(first.input_window_start, "20200101")
        self.assertEqual(first.input_window_end, "20210930")
        self.assertEqual((first.validation_start, first.validation_end), ("20211001", "20211231"))
        self.assertEqual((first.test_start, first.test_end), ("20220101", "20220331"))
        # Research-snapshot anchor = close (23:59:59) of the prior trading day, not 09:25.
        self.assertEqual(first.valid_decision_time.strftime("%Y%m%d %H:%M:%S"), "20210930 23:59:59")
        self.assertEqual(first.test_decision_time.strftime("%Y%m%d %H:%M:%S"), "20211230 23:59:59")
        self.assertEqual(folds[1].validation_start, "20220101")  # previous test quarter rolls forward

    def test_heldout_must_not_overlap_development(self):
        with self.assertRaisesRegex(ValueError, "must not overlap"):
            make_config(Path("/tmp"), heldout_first_period="2022Q1", heldout_last_period="2022Q1")

    def test_final_eval_wall_caps_derive_from_validation_caps_unless_overridden(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            derived = make_config(
                tmp_path,
                backtest_max_seconds_per_decision=99.0,
                backtest_max_seconds_per_trading_day=123.0,
            )
            self.assertEqual(derived.final_eval_max_seconds_per_decision(), 297.0)
            self.assertEqual(derived.final_eval_max_seconds_per_trading_day(), 369.0)

            explicit = make_config(
                tmp_path,
                experiment_id="exp_e2e_explicit",
                backtest_max_seconds_per_decision=99.0,
                backtest_max_seconds_per_trading_day=123.0,
                backtest_final_eval_max_seconds_per_decision=111.0,
                backtest_final_eval_max_seconds_per_trading_day=222.0,
            )
            self.assertEqual(explicit.final_eval_max_seconds_per_decision(), 111.0)
            self.assertEqual(explicit.final_eval_max_seconds_per_trading_day(), 222.0)

    def test_quarter_bounds(self):
        self.assertEqual(quarter_bounds("2022Q1"), ("20220101", "20220331"))
        self.assertEqual(quarter_bounds("2021Q4"), ("20211001", "20211231"))

    def test_fold_period_can_be_month_week_or_year(self):
        # Denser purpose-built calendars: every validation/test region needs >=2
        # trading days plus a prior-day anchor.
        month_days = ["20211130", "20211201", "20211230", "20220104", "20220131"]
        month = build_fold_schedule("202201", "202201", month_days, period="month")[0]
        self.assertEqual(month.fold_id, "fold_202201")
        self.assertEqual((month.validation_start, month.validation_end), ("20211201", "20211231"))
        self.assertEqual((month.test_start, month.test_end), ("20220101", "20220131"))

        week_days = ["20211227", "20211228", "20220103", "20220104", "20220110"]
        week = build_fold_schedule("20220104", "20220104", week_days, period="week")[0]
        self.assertEqual(week.fold_id, "fold_20220104")
        self.assertEqual((week.validation_start, week.validation_end), ("20211228", "20220103"))
        self.assertEqual((week.test_start, week.test_end), ("20220104", "20220110"))

        year = build_fold_schedule("2022", "2022", TRADING_DAYS, period="year")[0]
        self.assertEqual(year.fold_id, "fold_2022")
        self.assertEqual((year.validation_start, year.validation_end), ("20210101", "20211231"))
        self.assertEqual((year.test_start, year.test_end), ("20220101", "20221231"))
        self.assertEqual(period_bounds("20220104..20220110", period="week"), ("20220104", "20220110"))

    def test_day_fold_period_is_rejected(self):
        # Day folds always yield single-trading-day validation/test regions, which
        # the replay engine categorically rejects (entry + forced-liquidation days).
        with self.assertRaisesRegex(ValueError, "unsupported fold period"):
            build_fold_schedule("20220104", "20220104", TRADING_DAYS, period="day")

    def test_fold_regions_require_two_trade_days(self):
        # Validation week 20211228..20220103 holds a single trading day here, so the
        # schedule must fail fast instead of burning a doomed sandbox + LLM session.
        with self.assertRaisesRegex(ValueError, "trading day"):
            build_fold_schedule("20220104", "20220104", ["20211230", "20220104", "20220110"], period="week")
        with self.assertRaisesRegex(ValueError, "trading day"):
            heldout_periods("20220104", "20220104", ["20211230", "20220104"], period="week")


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


class ImportSmokeNamesTest(unittest.TestCase):
    def test_import_names_only_high_confidence(self):
        from autotrade.environment.sandbox_images import _python_import_names

        # Aliases and simple names emit a smoke import (version/extras stripped).
        self.assertEqual(_python_import_names(["numpy==2.1.3", "torch"]), ["numpy", "torch"])
        self.assertEqual(_python_import_names(["scikit-learn>=1.5"]), ["sklearn"])
        self.assertEqual(_python_import_names(["umap-learn>=0.5"]), ["umap"])
        self.assertEqual(_python_import_names(["opencv-contrib-python[extra]"]), ["cv2"])
        # An unaliased hyphenated/dotted name has an unguessable import — skip it
        # (a wrong `import` line would reject a validly-installed package).
        self.assertEqual(_python_import_names(["some-weird-pkg", "a.b.c"]), [])


class ExperimentCliTest(unittest.TestCase):
    def _meta_parser(self) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser()
        add_meta_sandbox_arguments(parser, verbose_help=True, disable_rebuild_help=EXPERIMENT_META_REBUILD_HELP)
        return parser

    def test_help_exposes_meta_learning_network_options(self):
        script = Path(__file__).resolve().parents[2] / "scripts" / "experiments" / "run_experiment.py"
        result = subprocess.run(
            [sys.executable, str(script), "--help"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("--meta-learning-network", result.stdout)
        self.assertIn("--meta-learning-env", result.stdout)
        self.assertNotIn("--meta-learning-host-proxy ", result.stdout)  # dead no-op key removed
        self.assertIn("--disable-meta-learning-host-proxy", result.stdout)
        self.assertIn("--disable-meta-learning-managed-proxy", result.stdout)

    def test_meta_learning_sandbox_exposes_proxy_aliases_when_managed_xray_config_exists(self):
        parser = self._meta_parser()
        args = parser.parse_args([])
        class Completed:
            returncode = 0
            stdout = "45: docker0    inet 10.10.0.1/24 brd 10.10.0.255 scope global docker0\\n"
            stderr = ""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / ".env.xray.json").write_text(
                json.dumps({"inbounds": [], "outbounds": [{"protocol": "freedom"}]}),
                encoding="utf-8",
            )
            with patch("autotrade.pipelines.assembly.subprocess.run", return_value=Completed()):
                spec = build_meta_learning_sandbox_spec(args, SandboxSpec(gpu=None), repo_root=repo_root)
                managed_proxy = build_meta_learning_managed_proxy_spec(
                    args,
                    repo_root=repo_root,
                    sandbox_spec=spec,
                )

        aliases = {container for container, _host in spec.env_aliases}
        self.assertEqual(spec.network, "bridge")
        self.assertTrue(spec.add_host_gateway)
        self.assertEqual(spec.host_gateway_ip, "10.10.0.1")
        self.assertTrue(managed_proxy.enabled)
        self.assertEqual(managed_proxy.listen_host, "10.10.0.1")
        self.assertEqual(managed_proxy.container_host, "10.10.0.1")
        self.assertIn("AT_PROXY_HTTP", aliases)
        self.assertIn("AT_PROXY_HTTPS", aliases)
        self.assertIn("AT_PROXY_ALL", aliases)

    def test_meta_learning_sandbox_does_not_map_ambient_host_proxy_without_managed_config(self):
        parser = self._meta_parser()
        args = parser.parse_args([])
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            spec = build_meta_learning_sandbox_spec(args, SandboxSpec(gpu=None), repo_root=repo_root)
            managed_proxy = build_meta_learning_managed_proxy_spec(
                args,
                repo_root=repo_root,
                sandbox_spec=spec,
            )

        self.assertEqual(spec.network, "bridge")
        self.assertEqual(spec.env_aliases, ())
        self.assertFalse(spec.add_host_gateway)
        self.assertFalse(managed_proxy.enabled)
        self.assertEqual(managed_proxy.disabled_status, "not_configured")

    def test_meta_learning_network_none_disables_proxy_aliases_and_managed_proxy(self):
        parser = self._meta_parser()
        args = parser.parse_args(["--meta-learning-network", "none"])
        with tempfile.TemporaryDirectory() as tmp:
            spec = build_meta_learning_sandbox_spec(args, SandboxSpec(gpu=None), repo_root=Path(tmp))
            managed_proxy = build_meta_learning_managed_proxy_spec(
                args,
                repo_root=Path(tmp),
                sandbox_spec=spec,
            )

        self.assertEqual(spec.network, "none")
        self.assertEqual(spec.env_aliases, ())
        self.assertFalse(spec.add_host_gateway)
        self.assertFalse(managed_proxy.enabled)
        self.assertEqual(managed_proxy.disabled_status, "disabled_by_network_none")

    def test_meta_learning_proxy_aliases_can_be_disabled(self):
        parser = self._meta_parser()
        args = parser.parse_args(["--disable-meta-learning-host-proxy"])
        with tempfile.TemporaryDirectory() as tmp:
            spec = build_meta_learning_sandbox_spec(args, SandboxSpec(gpu=None), repo_root=Path(tmp))
            managed_proxy = build_meta_learning_managed_proxy_spec(
                args,
                repo_root=Path(tmp),
                sandbox_spec=spec,
            )

        self.assertEqual(spec.env_aliases, ())
        self.assertFalse(spec.add_host_gateway)
        self.assertFalse(managed_proxy.enabled)

    def test_meta_learning_managed_proxy_fails_fast_when_bridge_ip_missing_with_config(self):
        parser = self._meta_parser()
        args = parser.parse_args([])
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / ".env.xray.json").write_text(
                json.dumps({"inbounds": [], "outbounds": [{"protocol": "freedom"}]}),
                encoding="utf-8",
            )
            spec = SandboxSpec(gpu=None, network="bridge", env_aliases=(("AT_PROXY_HTTP", "HTTP_PROXY"),))
            with self.assertRaisesRegex(RuntimeError, "Docker bridge host IP"):
                build_meta_learning_managed_proxy_spec(args, repo_root=repo_root, sandbox_spec=spec)

    def test_non_quarter_period_requires_explicit_generic_periods(self):
        script = Path(__file__).resolve().parents[2] / "scripts" / "experiments" / "run_experiment.py"
        result = subprocess.run(
            [sys.executable, str(script), "--experiment-id", "x", "--fold-period", "month"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("requires explicit generic period args", result.stderr)

    def test_audit_session_non_quarter_period_requires_explicit_generic_periods(self):
        # The audit CLI must share the production guard instead of silently
        # feeding its quarter defaults into a non-quarter schedule.
        script = Path(__file__).resolve().parents[2] / "scripts" / "experiments" / "run_audit_session.py"
        result = subprocess.run(
            [sys.executable, str(script), "--mode", "fold", "--experiment-id", "x", "--fold-period", "month"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("requires explicit generic period args", result.stderr)

    def test_session_config_summary_records_context_token_thresholds(self):
        config = AgentSessionConfig(fold_deadline_at=datetime.now(timezone.utc))
        summary = _session_config_summary(config, compact_enabled=True)

        self.assertEqual(summary["trim_token_threshold"], 60000)
        self.assertEqual(summary["trim_message_headroom"], 30)
        self.assertEqual(summary["tool_result_clear_token_threshold"], 24000)
        self.assertTrue(summary["clear_tool_results"])

    def test_data_summary_metadata_error_redacts_host_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            snapshot_dir = Path(tmp) / "snapshot"
            snapshot_dir.mkdir()
            (snapshot_dir / "manifest.json").write_text('{"kind":"decision_input"}', encoding="utf-8")
            (snapshot_dir / "broken.parquet").write_text("not parquet", encoding="utf-8")

            summary = write_agent_data_summary(
                Path(tmp) / "data_summary.json",
                kind="fold",
                fold_id="fold_x",
                views={"snapshot": (snapshot_dir, "/mnt/snapshot")},
            )

            error = summary["views"]["snapshot"]["files"][0]["metadata_error"]
            self.assertNotIn(str(snapshot_dir), error)
            self.assertNotIn(str(Path(tmp)), error)


class PipelineEndToEndTest(unittest.TestCase):
    def test_development_history_uses_compact_fold_summaries(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            config = make_config(tmp)
            manifest_path = tmp / "run_manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "backtest_summaries": [
                            {
                                "result_name": "valid_000",
                                "mode": "valid",
                                "status": "ok",
                                "total_return": 0.1,
                                "large_internal_field": "drop",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            pipeline = ExperimentPipeline(
                config,
                FakeSnapshotProvider(),
                lambda ctx, fold, manifest: ScriptedFoldAgent(ctx),
                proxy=ScriptedLLM([]),
            )
            pipeline.ledger.append(
                {
                    "record_type": "fold",
                    "experiment_id": "exp_e2e",
                    "epoch_id": "epoch_001",
                    "fold_id": "fold_2022Q1",
                    "run_id": "run_001",
                    "fold_status": "frozen",
                    "finish_reason": "fold_finished",
                    "run_manifest_ref": str(manifest_path),
                    "validation_result": {"total_return": 0.1},
                    "test_result": {
                        "total_return": 0.2,
                        "sharpe": 0.4,
                        "turnover": 1.25,
                        "trade_count": 7,
                        "exposure": {
                            "avg_gross": 0.35,
                            "max_gross": 0.8,
                            "zero_position_days": 2,
                            "replay_days": 60,
                            "daily": [0.1, 0.2],
                        },
                        "result_path": "/secret/test/result",
                        "orders": [{"ts_code": "000001.SZ"}],
                        "benchmark": {
                            "label": "000300.SH",
                            "excess_return": 0.03,
                            "raw_daily_returns": [0.1],
                        },
                    },
                    "verbose_agent_trace": ["not for meta history"],
                }
            )

            history = pipeline._development_history("taste")

            self.assertNotIn("folds", history)
            self.assertEqual(len(history["fold_backtest_summaries"]), 1)
            compact = history["fold_backtest_summaries"][0]
            self.assertTrue(compact["fold_id"].startswith("fold_ref_"))
            self.assertNotEqual(compact["fold_id"], "fold_2022Q1")
            self.assertEqual(compact["backtest_summaries"][0]["total_return"], 0.1)
            self.assertEqual(
                compact["test_result"],
                {
                    "total_return": 0.2,
                    "sharpe": 0.4,
                    "trade_count": 7,
                    "turnover": 1.25,
                    "benchmark": {"label": "000300.SH", "excess_return": 0.03},
                    "exposure": {
                        "avg_gross": 0.35,
                        "max_gross": 0.8,
                        "zero_position_days": 2,
                        "replay_days": 60,
                    },
                },
            )
            self.assertNotIn("test_result", _agent_visible_ledger_record(pipeline.ledger.read("fold")[0]))
            rendered_test = json.dumps(compact["test_result"], ensure_ascii=False)
            self.assertNotIn("result_path", rendered_test)
            self.assertNotIn("orders", rendered_test)
            self.assertNotIn("raw_daily_returns", rendered_test)
            self.assertNotIn("large_internal_field", compact["backtest_summaries"][0])

    def test_single_epoch_runs_meta_learning_before_fold_and_heldout(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            snapshot_config = SnapshotConfig(window_months=6, macro_window_months=12, intraday_trade_days=2)
            config = make_config(tmp, window_months=6, snapshot_config=snapshot_config)
            proxy = ScriptedLLM([])
            captured_meta: dict[str, object] = {}

            def meta_learner(ctx: ToolContext) -> None:
                captured_meta["snapshot_files"] = sorted(path.name for path in ctx.paths.snapshot.iterdir())
                captured_meta["train_files"] = sorted(path.name for path in ctx.paths.train.iterdir())
                captured_meta["valid_files"] = sorted(path.name for path in ctx.paths.valid.iterdir())
                captured_meta["test_files"] = sorted(path.name for path in ctx.paths.test.iterdir())
                captured_meta["snapshot_manifest"] = json.loads(
                    (ctx.paths.snapshot / "manifest.json").read_text(encoding="utf-8")
                )
                captured_meta["valid_manifest"] = json.loads(
                    (ctx.paths.valid / "manifest.json").read_text(encoding="utf-8")
                )
                captured_meta["data_summary"] = json.loads(ctx.paths.data_summary.read_text(encoding="utf-8"))
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
            self.assertNotIn("modification_check_ref", record["steps"][0])
            self.assertIsNotNone(record["steps"][0]["modification_delta_summary"])
            self.assertIn("code_diff_lines", record["steps"][0]["modification_delta_summary"])
            self.assertGreater(record["validation_result"]["total_return"], 0.0)
            self.assertGreater(record["test_result"]["total_return"], 0.0)
            self.assertIn("exposure", record["test_result"])
            self.assertIn("turnover", record["test_result"])
            self.assertIn("trade_count", record["test_result"])

            frozen_dir = Path(record["frozen_strategy_artifact_path"])
            self.assertTrue((frozen_dir / "manifest.json").exists())
            manifest = json.loads((frozen_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["source_fold_id"], "fold_2022Q1")
            self.assertEqual(manifest["source_step_id"], "step_001")
            self.assertNotIn("frozen", manifest)
            self.assertNotIn("validation_result_ref", manifest)
            self.assertNotIn("modification_check_ref", manifest)
            self.assertNotIn("run_manifest_ref", manifest)

            run_dir = config.experiment_dir / "artifacts" / record["run_id"]
            self.assertTrue((run_dir / "run_manifest.json").exists())
            run_manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(run_manifest["runtime_env_ref"], "/mnt/artifacts/runtime_env.json")
            self.assertEqual(run_manifest["data_summary_ref"], "/mnt/artifacts/data_summary.json")
            self.assertEqual(run_manifest["fold_period"], "quarter")
            self.assertEqual(run_manifest["fold"]["input_window"], "20210401..20210930")
            self.assertEqual(run_manifest["snapshot_config"]["decision_windows"]["daily_months"], 6)
            self.assertEqual(run_manifest["snapshot_config"]["decision_windows"]["macro_months"], 12)
            self.assertEqual(run_manifest["snapshot_config"]["decision_windows"]["intraday_trade_days"], 2)
            self.assertEqual(run_manifest["snapshots"]["train_snapshot"]["alias_of"], "valid_decision_input")
            self.assertEqual(
                run_manifest["snapshots"]["train_snapshot"]["snapshot_hash"],
                run_manifest["snapshots"]["valid_decision_input"]["snapshot_hash"],
            )
            self.assertNotIn("test_decision_time", run_manifest)
            self.assertNotIn("test_period", run_manifest["fold"])
            self.assertNotIn("test_decision_input", run_manifest["snapshots"])
            self.assertTrue((run_dir / "host_run_manifest.json").exists())
            host_run_manifest = json.loads((run_dir / "host_run_manifest.json").read_text(encoding="utf-8"))
            self.assertIn("test_period", host_run_manifest["fold"])
            self.assertIn("test_replay", host_run_manifest["snapshots"])
            # The shared replay-config block (_replay_config_fields) is present in the
            # fold manifest (and, identically, in the held-out manifest).
            for key in (
                "execution_lag_bars", "offsession_tick_minutes",
                "backtest_max_seconds_per_decision", "nl_max_calls_per_backtest", "timeview_enabled",
            ):
                self.assertIn(key, host_run_manifest)
            for key in (
                "auction_enabled", "auction_preopen_time", "auction_decision_time", "auction_close_time",
            ):
                self.assertNotIn(key, host_run_manifest)
            self.assertTrue((run_dir / "runtime_env.json").exists())
            self.assertTrue((run_dir / "data_summary.json").exists())
            self.assertTrue((run_dir / "agent_trace.jsonl").exists())
            self.assertTrue((run_dir / "results" / "test_000" / "detailed_return.json").exists())

            meta = pipeline.ledger.read("meta_learning")
            self.assertEqual(len(meta), 1)
            self.assertEqual(meta[0]["epoch_id"], "epoch_001")
            self.assertEqual(meta[0]["status"], "taste_only")
            self.assertGreater(meta[0]["taste_chars"], 0)
            meta_run_dir = config.experiment_dir / "artifacts" / meta[0]["run_id"]
            meta_manifest = json.loads((meta_run_dir / "run_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(meta_manifest["runtime_env_ref"], "/mnt/artifacts/runtime_env.json")
            self.assertEqual(meta_manifest["data_summary_ref"], "/mnt/artifacts/data_summary.json")
            self.assertTrue(meta_manifest["meta_learning_visible_fold"]["fold_id"].startswith("fold_ref_"))
            self.assertNotEqual(meta_manifest["meta_learning_visible_fold"]["fold_id"], "fold_2022Q1")
            self.assertTrue(meta_manifest["valid_decision_time"].startswith("2021-09-30T23:59:59"))
            self.assertEqual(meta_manifest["snapshots"]["train_snapshot"]["alias_of"], "valid_decision_input")
            self.assertEqual(
                meta_manifest["snapshots"]["train_snapshot"]["snapshot_hash"],
                meta_manifest["snapshots"]["valid_decision_input"]["snapshot_hash"],
            )
            self.assertIn("valid_replay", meta_manifest["snapshots"])
            self.assertEqual(meta_manifest["experiment_parameters"]["fold_period"], "quarter")
            self.assertEqual(meta_manifest["experiment_parameters"]["snapshot_config"]["decision_windows"]["daily_months"], 6)
            self.assertEqual(meta_manifest["experiment_parameters"]["snapshot_config"]["decision_windows"]["intraday_trade_days"], 2)
            self.assertEqual(meta_manifest["experiment_parameters"]["max_fold_minutes"], 20)
            self.assertTrue((meta_run_dir / "runtime_env.json").exists())
            self.assertTrue((meta_run_dir / "data_summary.json").exists())
            self.assertIn("daily.parquet", captured_meta["snapshot_files"])
            self.assertIn("manifest.json", captured_meta["train_files"])
            self.assertIn("daily.parquet", captured_meta["valid_files"])
            self.assertIn("manifest.json", captured_meta["valid_files"])
            self.assertEqual(captured_meta["test_files"], [])
            self.assertEqual(captured_meta["snapshot_manifest"]["kind"], "decision_input")
            self.assertEqual(captured_meta["snapshot_manifest"]["decision_date"], "20210930")
            self.assertEqual(captured_meta["valid_manifest"]["kind"], "replay_slot")
            self.assertEqual(captured_meta["valid_manifest"]["label"], "valid")
            self.assertEqual(captured_meta["valid_manifest"]["period_start"], "20211001")
            self.assertEqual(captured_meta["valid_manifest"]["period_end"], "20211231")
            data_summary = captured_meta["data_summary"]
            self.assertEqual(data_summary["kind"], "meta_learning")
            self.assertNotIn("schema_version", data_summary)
            # The contract is a constant-size pointer; the per-column table is
            # the sibling unit_reference.json artifact.
            self.assertEqual(
                data_summary["unit_contract"]["unit_reference"],
                "/mnt/artifacts/unit_reference.json",
            )
            self.assertNotIn("source_unit_rules", data_summary["unit_contract"])
            unit_reference = json.loads(
                (meta_run_dir / "unit_reference.json").read_text(encoding="utf-8")
            )
            records = {
                (record["file"], record["dataset"], record["column"]): record
                for record in unit_reference["records"]
            }
            self.assertEqual(
                records[("daily.parquet", None, "vol")],
                {
                    "file": "daily.parquet",
                    "dataset": None,
                    "column": "vol",
                    "semantic_type": "numeric",
                    "source_unit": "hands",
                    "factor": 100.0,
                    "normalized_unit": "shares",
                    "status": "verified",
                },
            )
            self.assertEqual(
                records[("events.parquet", "margin_secs", "ts_code")]["semantic_type"],
                "identifier",
            )
            # Only file/dataset/column tuples visible in THIS run's views ship.
            self.assertNotIn(
                ("events.parquet", "share_float_complete", "float_share"), records
            )
            self.assertIn(
                "must not be used", unit_reference["unknown_unit_policy"]
            )
            self.assertIn("large_table_guidance", data_summary)
            self.assertEqual(sorted(data_summary["views"]), ["snapshot", "train", "valid"])
            self.assertNotIn("test", data_summary["views"])
            snapshot_view = data_summary["views"]["snapshot"]
            self.assertNotIn("build_profile", snapshot_view)
            daily_summary = next(item for item in snapshot_view["files"] if item["path"] == "daily.parquet")
            self.assertIn("key_columns", daily_summary)
            self.assertNotIn("columns", daily_summary)
            heldout = pipeline.ledger.read("heldout")[0]
            self.assertEqual(heldout["strategy_artifact_id"], "strategy_epoch_001_fold_2022Q1")
            self.assertGreater(heldout["test_result"]["total_return"], 0.0)

    def test_heldout_manifest_includes_replay_and_budget_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            config = make_config(
                tmp,
                execution_lag_bars=4,
                decision_max_sim_minutes=17.0,
                backtest_max_seconds_per_decision=99.0,
                backtest_max_seconds_per_trading_day=123.0,
                timeview_enabled=False,
                nl_max_calls_per_decision_day=7,
                nl_max_calls_per_backtest=19,
            )
            final_output = tmp / "final_output"
            final_models = tmp / "final_models"
            final_output.mkdir()
            final_models.mkdir()
            write_strategy(final_output)
            final = FrozenArtifact(
                artifact_id="strategy_final",
                path=final_output,
                artifact_hash=artifact_hash(final_output),
                model_path=final_models,
                model_artifact_hash=model_artifact_hash(final_models),
            )
            captured: dict[str, object] = {}

            def fake_run(tool, *args, **kwargs):
                captured.update(tool.ctx.manifest.data)
                return {
                    "status": "ok",
                    "mode": "frozen_eval",
                    "total_return": 0.0,
                    "sharpe": 0.0,
                    "max_drawdown": 0.0,
                }

            pipeline = ExperimentPipeline(
                config,
                FakeSnapshotProvider(),
                lambda ctx, fold, manifest: ScriptedFoldAgent(ctx),
                proxy=ScriptedLLM([]),
            )
            with patch("autotrade.pipelines.experiment.BacktestTool.run", autospec=True, side_effect=fake_run):
                pipeline.run_heldout(final, TRADING_DAYS, epoch_id="epoch_001")

            self.assertEqual(captured["execution_lag_bars"], 4)
            self.assertEqual(captured["decision_max_sim_minutes"], 17.0)
            self.assertEqual(captured["backtest_max_seconds_per_decision"], 99.0)
            self.assertEqual(captured["backtest_max_seconds_per_trading_day"], 123.0)
            # Final evals (held-out is one) carry their own generous anti-hang caps.
            self.assertEqual(
                captured["backtest_final_eval_max_seconds_per_decision"],
                config.final_eval_max_seconds_per_decision(),
            )
            self.assertEqual(
                captured["backtest_final_eval_max_seconds_per_trading_day"],
                config.final_eval_max_seconds_per_trading_day(),
            )
            self.assertIs(captured["timeview_enabled"], False)
            self.assertEqual(captured["nl_max_calls_per_decision_day"], 7)
            self.assertEqual(captured["nl_max_calls_per_backtest"], 19)
            self.assertNotIn("auction_decision_time", captured)

    def test_agent_visible_data_summary_and_trace_opaque_fold_id(self):
        # data_summary.json and agent_trace.jsonl are both agent-readable, so the
        # calendar period must not leak through them; host correlation stays on
        # run_id + the host-only manifest.
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            config = make_config(tmp)
            pipeline = ExperimentPipeline(
                config, FakeSnapshotProvider(), lambda ctx, fold, manifest: ScriptedFoldAgent(ctx), proxy=ScriptedLLM([])
            )
            fold = build_fold_schedule("2022Q1", "2022Q1", TRADING_DAYS)[0]
            environment_progress: list[tuple[str, dict[str, object] | None]] = []
            outcome = pipeline.run_fold(
                fold,
                epoch_id="epoch_001",
                parent=None,
                environment_progress_hook=(
                    lambda stage, progress: environment_progress.append((stage, progress))
                ),
            )

            stages = [stage for stage, _ in environment_progress]
            self.assertEqual(stages[0], "acceptance")
            self.assertIn("frozen_test", stages)
            self.assertEqual(stages[-1], "persistence")

            run_dir = config.experiment_dir / "artifacts" / outcome.run_id
            data_summary = json.loads((run_dir / "data_summary.json").read_text(encoding="utf-8"))
            self.assertTrue(str(data_summary["fold_id"]).startswith("fold_ref_"))
            self.assertNotEqual(data_summary["fold_id"], "fold_2022Q1")

            trace_text = (run_dir / "agent_trace.jsonl").read_text(encoding="utf-8")
            self.assertNotIn("fold_2022Q1", trace_text)
            events = [json.loads(line) for line in trace_text.splitlines() if line.strip()]
            self.assertTrue(events)
            for event in events:
                self.assertTrue(str(event.get("fold_id", "")).startswith("fold_ref_"))

            # Host correlation is preserved: the host-only manifest keeps the raw id.
            host_manifest = json.loads((run_dir / "host_run_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(host_manifest["fold_id"], "fold_2022Q1")

    def test_run_fold_writes_durable_ledger_when_collection_fails(self):
        # C1: a failed artifact collection must not leave a frozen strategy without a
        # ledger record (which previously made the experiment unresumable).
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            config = make_config(tmp)
            pipeline = ExperimentPipeline(
                config, FakeSnapshotProvider(), lambda ctx, fold, manifest: ScriptedFoldAgent(ctx), proxy=ScriptedLLM([])
            )
            fold = build_fold_schedule("2022Q1", "2022Q1", TRADING_DAYS)[0]
            with patch.object(LocalSandbox, "collect_artifacts", side_effect=RuntimeError("disk full")):
                with self.assertRaisesRegex(RuntimeError, "disk full"):
                    pipeline.run_fold(fold, epoch_id="epoch_001", parent=None)

            folds = pipeline.ledger.read("fold")
            self.assertEqual(len(folds), 1)
            self.assertEqual(folds[0]["fold_status"], "frozen")  # frozen before collect failed
            self.assertIn("disk full", folds[0]["finalize_error"])

    def test_run_fold_test_eval_failure_is_non_fatal_and_recorded(self):
        # C1 + H2: the OOS test_000 eval is diagnostic, so its failure must not
        # discard the validation-accepted strategy or abort the experiment.
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            config = make_config(tmp)
            pipeline = ExperimentPipeline(
                config, FakeSnapshotProvider(), lambda ctx, fold, manifest: ScriptedFoldAgent(ctx), proxy=ScriptedLLM([])
            )
            fold = build_fold_schedule("2022Q1", "2022Q1", TRADING_DAYS)[0]
            with patch.object(
                ExperimentPipeline, "_frozen_test_eval", side_effect=RuntimeError("test eval blew up")
            ):
                outcome = pipeline.run_fold(fold, epoch_id="epoch_001", parent=None)  # must not raise

            self.assertEqual(outcome.fold_status, "frozen")
            record = pipeline.ledger.read("fold")[0]
            self.assertIsNone(record["test_result"])
            self.assertIn("test eval blew up", record["finalize_error"])

    def test_run_heldout_writes_durable_ledger_when_collection_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            config = make_config(tmp)
            final_output = tmp / "final_output"
            final_models = tmp / "final_models"
            final_output.mkdir()
            final_models.mkdir()
            write_strategy(final_output)
            final = FrozenArtifact(
                artifact_id="strategy_final",
                path=final_output,
                artifact_hash=artifact_hash(final_output),
                model_path=final_models,
                model_artifact_hash=model_artifact_hash(final_models),
            )
            pipeline = ExperimentPipeline(
                config, FakeSnapshotProvider(), lambda ctx, fold, manifest: ScriptedFoldAgent(ctx), proxy=ScriptedLLM([])
            )

            def fake_bt(tool, *args, **kwargs):
                return {"status": "ok", "mode": "frozen_eval", "total_return": 0.0, "sharpe": 0.0, "max_drawdown": 0.0}

            with patch("autotrade.pipelines.experiment.BacktestTool.run", autospec=True, side_effect=fake_bt), patch.object(
                LocalSandbox, "collect_artifacts", side_effect=RuntimeError("disk full")
            ):
                with self.assertRaisesRegex(RuntimeError, "disk full"):
                    pipeline.run_heldout(final, TRADING_DAYS, epoch_id="epoch_001")

            heldout = pipeline.ledger.read("heldout")
            self.assertEqual(len(heldout), 1)
            self.assertIn("disk full", heldout[0]["finalize_error"])

    def test_pipeline_freezes_model_artifacts_with_strategy(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            config = make_config(tmp)
            pipeline = ExperimentPipeline(
                config,
                FakeSnapshotProvider(),
                lambda ctx, fold, manifest: ModelArtifactFoldAgent(ctx),
                proxy=ScriptedLLM([]),
            )

            result = pipeline.run(TRADING_DAYS)

            self.assertEqual(result["final_strategy_artifact"], "strategy_epoch_001_fold_2022Q1")
            record = pipeline.ledger.read("fold")[0]
            model_path = Path(record["frozen_model_artifact_path"])
            self.assertTrue((model_path / "params.json").exists())
            self.assertTrue(record["frozen_model_artifact_hash"].startswith("sha256:"))
            manifest = json.loads((Path(record["frozen_strategy_artifact_path"]) / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["model_artifact_hash"], record["frozen_model_artifact_hash"])
            heldout = pipeline.ledger.read("heldout")[0]
            self.assertEqual(heldout["model_artifact_hash"], record["frozen_model_artifact_hash"])

    def test_multi_epoch_runs_meta_learning_before_each_epoch(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            config = make_config(tmp, epochs=2)
            proxy = ScriptedLLM([])
            meta_epochs: list[str] = []

            def meta_learner(ctx: ToolContext) -> None:
                meta_epochs.append(str(ctx.manifest.require("epoch_id")))
                (ctx.paths.workspace / "taste.md").write_text(
                    f"taste for {ctx.manifest.require('epoch_id')}", encoding="utf-8"
                )
                prompt_path = ctx.paths.agent_output / "nl_prompt.md"
                prompt_path.write_text("prefer robust negative evidence checks\n", encoding="utf-8")
                with self.assertRaisesRegex(ToolError, "not allowed"):
                    BacktestTool(ctx).run(mode="valid")

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
            for record in meta_records:
                self.assertGreaterEqual(record["run_wall_seconds"], 0)
                self.assertEqual(record["researcher_wait_seconds"], 0.0)
                trace_ref = Path(str(record["agent_trace_ref"]))
                self.assertTrue(trace_ref.exists())
                self.assertFalse(
                    (config.experiment_dir / "meta_learning" / str(record["epoch_id"]) / "agent_trace.jsonl").exists()
                )
            heldout = pipeline.ledger.read("heldout")[0]
            self.assertEqual(heldout["strategy_artifact_id"], "strategy_epoch_002_fold_2022Q1")

            # Agent-visible manifests must never carry the raw fold label; the
            # epoch-2 fold inherits epoch-1's frozen artifact whose id embeds it.
            manifests = sorted((config.experiment_dir / "artifacts").glob("run_*/run_manifest.json"))
            self.assertTrue(manifests)
            parent_ids = []
            for path in manifests:
                content = path.read_text(encoding="utf-8")
                self.assertNotIn("fold_2022", content, msg=str(path))
                record = json.loads(content)
                if record.get("parent_strategy_artifact_id"):
                    parent_ids.append(str(record["parent_strategy_artifact_id"]))
            self.assertTrue(parent_ids)
            self.assertTrue(all(pid.startswith("strategy_ref_") for pid in parent_ids))

    def test_batch_interval_meta_uses_upcoming_fold_and_updates_only_later_folds(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            config = make_config(
                tmp,
                first_test_period="2022Q1",
                last_test_period="2022Q2",
                meta_learning_fold_interval=1,
            )
            meta_seen: list[dict[str, object]] = []
            fold_tastes: list[tuple[str, str]] = []

            def meta_learner(ctx: ToolContext) -> None:
                meta_seen.append(
                    {
                        "id": ctx.manifest.get("meta_learning_id"),
                        "trigger": ctx.manifest.get("trigger_after_folds"),
                        "visible_fold": (ctx.manifest.get("meta_learning_visible_fold") or {}).get("fold_id"),
                        "parent": ctx.manifest.get("parent_strategy_artifact_id"),
                    }
                )
                (ctx.paths.workspace / "taste.md").write_text(
                    f"taste-{ctx.manifest.get('meta_learning_id')}", encoding="utf-8"
                )

            def agent_factory(ctx, fold, manifest):
                fold_tastes.append((fold.fold_id, str(manifest.get("taste_prompt") or "")))
                return ScriptedFoldAgent(ctx)

            pipeline = ExperimentPipeline(
                config,
                FakeSnapshotProvider(),
                agent_factory,
                proxy=ScriptedLLM([]),
                meta_learner=meta_learner,
            )
            pipeline.run(TRADING_DAYS)

            self.assertEqual(
                meta_seen,
                [
                    {"id": "epoch_001", "trigger": 0, "visible_fold": "fold_2022Q1", "parent": None},
                    {
                        "id": "epoch_001_after_fold_001",
                        "trigger": 1,
                        "visible_fold": "fold_2022Q2",
                        "parent": "strategy_epoch_001_fold_2022Q1",
                    },
                ],
            )
            self.assertEqual(
                fold_tastes,
                [
                    ("fold_2022Q1", "taste-epoch_001"),
                    ("fold_2022Q2", "taste-epoch_001_after_fold_001"),
                ],
            )
            self.assertEqual(
                [record["meta_learning_id"] for record in pipeline.ledger.read("meta_learning")],
                ["epoch_001", "epoch_001_after_fold_001"],
            )

    def test_meta_learning_injects_full_records_and_prior_epoch_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            config = make_config(tmp, epochs=2)
            captured: dict[str, dict[str, str]] = {}

            def meta_learner(ctx: ToolContext) -> None:
                eid = str(ctx.manifest.require("epoch_id"))
                captured[eid] = {
                    "ledger_full": (ctx.paths.workspace / "experiment_ledger_full.jsonl").read_text(encoding="utf-8"),
                    "memory": (ctx.paths.workspace / "meta_learning_memory.jsonl").read_text(encoding="utf-8"),
                }
                ctx.trace.emit("note", {"marker": f"meta-marker-{eid}"})
                (ctx.paths.workspace / "taste.md").write_text(f"taste {eid}", encoding="utf-8")

            pipeline = ExperimentPipeline(
                config,
                FakeSnapshotProvider(),
                lambda ctx, fold, manifest: ScriptedFoldAgent(ctx),
                proxy=ScriptedLLM([]),
                meta_learner=meta_learner,
            )
            pipeline.run(TRADING_DAYS)

            # Epoch 1 runs before any fold/meta record exists.
            self.assertEqual(captured["epoch_001"]["ledger_full"], "")
            self.assertEqual(captured["epoch_001"]["memory"], "")
            # Item 2: epoch 2 sees the full raw records of epoch 1 (no held-out).
            epoch2_ledger = captured["epoch_002"]["ledger_full"]
            self.assertIn("fold_ref_", epoch2_ledger)
            self.assertNotIn("fold_2022Q1", epoch2_ledger)
            self.assertIn("meta_learning", epoch2_ledger)
            self.assertNotIn("heldout", epoch2_ledger)
            self.assertIn('"test_result"', epoch2_ledger)
            self.assertNotIn("test_period", epoch2_ledger)
            self.assertNotIn("test_decision_time", epoch2_ledger)
            self.assertNotIn("agent_trace_ref", epoch2_ledger)
            self.assertNotIn(str(tmp), epoch2_ledger)
            # Item 3: epoch 2's memory concatenates epoch 1's meta-learning log.
            self.assertIn("meta-marker-epoch_001", captured["epoch_002"]["memory"])
            self.assertNotIn("meta-marker-epoch_002", captured["epoch_002"]["memory"])
            for record in pipeline.ledger.read("meta_learning"):
                self.assertTrue(Path(str(record["agent_trace_ref"])).exists())
                self.assertFalse(
                    (config.experiment_dir / "meta_learning" / str(record["epoch_id"]) / "agent_trace.jsonl").exists()
                )

    def test_run_rejects_already_populated_experiment(self):
        # Full re-runs would collide inside _freeze past the durable-ledger guard;
        # they must fail fast at the entrypoint instead.
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            config = make_config(tmp)
            pipeline = ExperimentPipeline(
                config,
                FakeSnapshotProvider(),
                lambda ctx, fold, manifest: ScriptedFoldAgent(ctx),
                proxy=ScriptedLLM([]),
            )
            pipeline.ledger.append(
                {
                    "record_type": "fold",
                    "experiment_id": config.experiment_id,
                    "epoch_id": "epoch_001",
                    "fold_id": "fold_2022Q1",
                    "run_id": "run_prior",
                }
            )
            with self.assertRaisesRegex(RuntimeError, "not supported"):
                pipeline.run(TRADING_DAYS)

    def test_prior_meta_learning_logs_bounded_by_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            config = make_config(tmp, meta_memory_max_epochs=2)
            pipeline = ExperimentPipeline(
                config,
                FakeSnapshotProvider(),
                lambda ctx, fold, manifest: ScriptedFoldAgent(ctx),
                proxy=ScriptedLLM([]),
            )
            for index in range(1, 5):
                run_dir = config.experiment_dir / "artifacts" / f"run_meta_{index}"
                run_dir.mkdir(parents=True)
                trace = run_dir / "agent_trace.jsonl"
                trace.write_text(json.dumps({"marker": f"meta-mark-{index}"}) + "\n", encoding="utf-8")
                pipeline.ledger.append(
                    {
                        "record_type": "meta_learning",
                        "experiment_id": config.experiment_id,
                        "epoch_id": f"epoch_{index:03d}",
                        "fold_id": f"epoch_{index:03d}_meta_learning",
                        "run_id": f"run_meta_{index}",
                        "agent_trace_ref": str(trace),
                    }
                )
            memory = pipeline._prior_meta_learning_logs("epoch_005")
            self.assertNotIn("meta-mark-1", memory)
            self.assertNotIn("meta-mark-2", memory)
            self.assertIn("meta-mark-3", memory)
            self.assertIn("meta-mark-4", memory)

    def test_prior_meta_learning_logs_use_latest_prior_session_per_epoch(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            config = make_config(tmp, meta_memory_max_epochs=1)
            pipeline = ExperimentPipeline(
                config,
                FakeSnapshotProvider(),
                lambda ctx, fold, manifest: ScriptedFoldAgent(ctx),
                proxy=ScriptedLLM([]),
            )
            for meta_id, trigger, marker in (
                ("epoch_001", 0, "epoch-start"),
                ("epoch_001_after_fold_001", 1, "periodic-latest"),
            ):
                run_dir = config.experiment_dir / "artifacts" / f"run_{meta_id}"
                run_dir.mkdir(parents=True)
                trace = run_dir / "agent_trace.jsonl"
                trace.write_text(json.dumps({"marker": marker}) + "\n", encoding="utf-8")
                pipeline.ledger.append(
                    {
                        "record_type": "meta_learning",
                        "experiment_id": config.experiment_id,
                        "epoch_id": "epoch_001",
                        "meta_learning_id": meta_id,
                        "trigger_after_folds": trigger,
                        "fold_id": f"{meta_id}_meta_learning",
                        "run_id": f"run_{meta_id}",
                        "agent_trace_ref": str(trace),
                    }
                )

            memory = pipeline._prior_meta_learning_logs("epoch_001_after_fold_002")
            self.assertNotIn("epoch-start", memory)
            self.assertIn("periodic-latest", memory)

    def test_snapshot_builds_are_cached_within_an_experiment(self):
        calls = {"decision": 0, "replay": 0}

        class CountingProvider(FakeSnapshotProvider):
            def decision_snapshot(self, decision_time, out_dir):
                calls["decision"] += 1
                return super().decision_snapshot(decision_time, out_dir)

            def replay_slot(self, start, end, out_dir, *, label, available_from=None):
                calls["replay"] += 1
                return super().replay_slot(start, end, out_dir, label=label, available_from=available_from)

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            config = make_config(tmp)
            pipeline = ExperimentPipeline(
                config,
                CountingProvider(),
                lambda ctx, fold, manifest: ScriptedFoldAgent(ctx),
                proxy=ScriptedLLM([]),
            )
            folds = build_fold_schedule("2022Q1", "2022Q1", TRADING_DAYS)
            first = pipeline.run_fold(folds[0], epoch_id="epoch_001", parent=None)
            self.assertEqual(first.fold_status, "frozen")
            decision_builds, replay_builds = calls["decision"], calls["replay"]
            self.assertGreater(decision_builds, 0)
            self.assertGreater(replay_builds, 0)

            # The identical fold in the next epoch replays entirely from cache but
            # still gets working hardlinked views (the scripted agent backtests
            # against them).
            second = pipeline.run_fold(folds[0], epoch_id="epoch_002", parent=first.frozen)
            self.assertEqual(calls["decision"], decision_builds)
            self.assertEqual(calls["replay"], replay_builds)
            self.assertEqual(second.fold_status, "frozen")
            cache_entries = [p for p in (config.experiment_dir.parent / ".snapshot_cache").iterdir() if not p.name.startswith(".")]
            self.assertEqual(len(cache_entries), decision_builds + replay_builds)

    def test_fold_rejects_generation_switch_before_agent_or_container_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            agent_calls: list[str] = []

            def agent_factory(ctx, fold, manifest):
                agent_calls.append(fold.fold_id)
                return ScriptedFoldAgent(ctx)

            pipeline = ExperimentPipeline(
                make_config(tmp),
                SwitchingGenerationProvider(switch_after=2),
                agent_factory,
                proxy=ScriptedLLM([]),
            )
            fold = build_fold_schedule("2022Q1", "2022Q1", TRADING_DAYS)[0]
            with patch.object(pipeline, "_start_container") as start_container:
                with self.assertRaisesRegex(RuntimeError, "raw lake generation changed"):
                    pipeline.run_fold(fold, epoch_id="epoch_001", parent=None)

            start_container.assert_not_called()
            self.assertEqual(agent_calls, [])

    def test_meta_learning_rejects_generation_switch_before_agent_or_container_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            meta_calls: list[str] = []

            def meta_learner(ctx):
                meta_calls.append("called")

            pipeline = ExperimentPipeline(
                make_config(tmp),
                SwitchingGenerationProvider(switch_after=1),
                lambda ctx, fold, manifest: ScriptedFoldAgent(ctx),
                proxy=ScriptedLLM([]),
                meta_learner=meta_learner,
            )
            fold = build_fold_schedule("2022Q1", "2022Q1", TRADING_DAYS)[0]
            with patch.object(pipeline, "_start_container") as start_container:
                with self.assertRaisesRegex(RuntimeError, "raw lake generation changed"):
                    pipeline.run_meta_learning(
                        epoch_id="epoch_001",
                        parent=None,
                        visible_fold=fold,
                    )

            start_container.assert_not_called()
            self.assertEqual(meta_calls, [])

    def test_heldout_rejects_generation_switch_before_container_or_backtest_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            final_output = tmp / "final_output"
            final_models = tmp / "final_models"
            final_output.mkdir()
            final_models.mkdir()
            write_strategy(final_output)
            final = FrozenArtifact(
                artifact_id="strategy_final",
                path=final_output,
                artifact_hash=artifact_hash(final_output),
                model_path=final_models,
                model_artifact_hash=model_artifact_hash(final_models),
            )
            pipeline = ExperimentPipeline(
                make_config(tmp),
                SwitchingGenerationProvider(switch_after=1),
                lambda ctx, fold, manifest: ScriptedFoldAgent(ctx),
                proxy=ScriptedLLM([]),
            )
            with patch.object(pipeline, "_start_container") as start_container, patch(
                "autotrade.pipelines.experiment.BacktestTool.run"
            ) as backtest:
                with self.assertRaisesRegex(RuntimeError, "raw lake generation changed"):
                    pipeline.run_heldout(final, TRADING_DAYS, epoch_id="epoch_001")

            start_container.assert_not_called()
            backtest.assert_not_called()

    def test_failed_attempt_writes_permanent_ledger_record(self):
        # A run that throws before its success record must leave an
        # attempt_failed record (error evidence, re-runnable), never nothing.
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            config = make_config(tmp)

            class ExplodingAgent:
                def run(self) -> dict[str, object]:
                    raise RuntimeError("boom mid-run")

            pipeline = ExperimentPipeline(
                config, FakeSnapshotProvider(), lambda ctx, fold, manifest: ExplodingAgent(), proxy=ScriptedLLM([])
            )
            folds = build_fold_schedule("2022Q1", "2022Q1", TRADING_DAYS)
            with self.assertRaisesRegex(RuntimeError, "boom mid-run"):
                pipeline.run_fold(folds[0], epoch_id="epoch_001", parent=None)
            (failure,) = pipeline.ledger.read("attempt_failed")
            self.assertEqual(failure["fold_id"], folds[0].fold_id)
            self.assertEqual(failure["epoch_id"], "epoch_001")
            self.assertEqual(failure["error_type"], "RuntimeError")
            self.assertIn("boom mid-run", failure["trace"])
            self.assertTrue(failure["run_id"])
            self.assertEqual(pipeline.ledger.read("fold"), [])

    def test_failed_acceptance_falls_back_to_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            config = make_config(tmp)
            proxy = ScriptedLLM([])
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
                config, FakeSnapshotProvider(), lambda ctx, fold, manifest: IdleAgent(), proxy=ScriptedLLM([])
            )
            second = pipeline_idle.run_fold(folds[0], epoch_id="epoch_001b", parent=outcome.frozen)
            self.assertEqual(second.fold_status, "no_valid_backtest")
            self.assertEqual(second.frozen.artifact_id, outcome.frozen.artifact_id)

            # The step tree accumulated in fold 1 is handed to later folds and
            # the second fold starts positioned at the parent artifact's node.
            from autotrade.environment.step_tree import StepTree

            experiment_tree = StepTree(config.experiment_dir / "steps")
            self.assertGreaterEqual(len(experiment_tree.nodes()), 1)
            self.assertEqual(
                experiment_tree.position_for_hash(outcome.frozen.artifact_hash),
                experiment_tree.current_node_id,
            )

    def test_unvalidated_meta_regularization_cannot_become_fold_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            config = make_config(tmp)
            fold = build_fold_schedule("2022Q1", "2022Q1", TRADING_DAYS)[0]
            pipeline = ExperimentPipeline(
                config,
                FakeSnapshotProvider(),
                lambda ctx, spec, manifest: ScriptedFoldAgent(ctx),
                proxy=ScriptedLLM([]),
            )
            validated = pipeline.run_fold(fold, epoch_id="epoch_001", parent=None).frozen

            def regularize(ctx: ToolContext) -> None:
                strategy = ctx.paths.agent_output / "main.py"
                strategy.write_text(
                    strategy.read_text(encoding="utf-8") + "\n# meta regularization\n",
                    encoding="utf-8",
                )
                (ctx.paths.workspace / "taste.md").write_text(
                    "validate this regularization before reuse", encoding="utf-8"
                )

            pipeline.meta_learner = regularize
            meta_parent, _ = pipeline.run_meta_learning(
                epoch_id="epoch_002", parent=validated, visible_fold=fold
            )
            self.assertIsNotNone(meta_parent)
            self.assertTrue(meta_parent.requires_validation)

            class IdleAgent:
                def run(self) -> dict[str, object]:
                    return {"finish_status": "deadline_timeout"}

            pipeline_idle = ExperimentPipeline(
                config,
                FakeSnapshotProvider(),
                lambda ctx, spec, manifest: IdleAgent(),
                proxy=ScriptedLLM([]),
            )
            with self.assertRaisesRegex(RuntimeError, "Meta-regularized parent"):
                pipeline_idle.run_fold(fold, epoch_id="epoch_002", parent=meta_parent)

            class ValidateParentThenDivergeAgent:
                def __init__(self, ctx: ToolContext) -> None:
                    self.ctx = ctx

                def run(self) -> dict[str, object]:
                    ModificationCheckTool(self.ctx).run()
                    BacktestTool(self.ctx).run(mode="valid")
                    strategy = self.ctx.paths.agent_output / "main.py"
                    strategy.write_text(
                        strategy.read_text(encoding="utf-8") + "\n# unvalidated later idea\n",
                        encoding="utf-8",
                    )
                    ModificationCheckTool(self.ctx).run()
                    return {"finish_status": "deadline_timeout"}

            pipeline_validate = ExperimentPipeline(
                config,
                FakeSnapshotProvider(),
                lambda ctx, spec, manifest: ValidateParentThenDivergeAgent(ctx),
                proxy=ScriptedLLM([]),
            )
            fallback = pipeline_validate.run_fold(fold, epoch_id="epoch_003", parent=meta_parent)
            self.assertEqual(fallback.fold_status, "no_update")
            self.assertFalse(fallback.frozen.requires_validation)

            # Once accepted by a complete ordinary-Fold Validation, the same
            # lineage behaves identically in-process and after ledger resume.
            later = pipeline_idle.run_fold(fold, epoch_id="epoch_004", parent=fallback.frozen)
            self.assertEqual(later.fold_status, "no_valid_backtest")

    def test_two_epochs_do_not_collide_in_step_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            config = make_config(tmp, epochs=2)
            proxy = ScriptedLLM([])
            pipeline = ExperimentPipeline(
                config, FakeSnapshotProvider(), lambda ctx, fold, manifest: ScriptedFoldAgent(ctx), proxy=proxy
            )
            result = pipeline.run(TRADING_DAYS)
            self.assertEqual(result["heldout_runs"], 1)

            from autotrade.environment.step_tree import StepTree

            nodes = StepTree(config.experiment_dir / "steps").nodes()
            self.assertEqual(len(nodes), 2)
            self.assertNotEqual(nodes[0]["node_id"], nodes[1]["node_id"])
            # Epoch prefix keeps the two folds distinct; the fold id itself is opaqued
            # so the held-out calendar period never leaks into the agent-readable tree.
            self.assertTrue(nodes[0]["node_id"].startswith("epoch_001__fold_ref_"))
            self.assertTrue(nodes[1]["node_id"].startswith("epoch_002__fold_ref_"))
            self.assertNotIn("2022Q1", nodes[0]["node_id"])
            self.assertNotIn("2022Q1", nodes[1]["node_id"])

    def test_meta_learning_can_read_existing_step_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            config = make_config(tmp)
            pipeline = ExperimentPipeline(
                config, FakeSnapshotProvider(), lambda ctx, fold, manifest: ScriptedFoldAgent(ctx), proxy=ScriptedLLM([])
            )
            folds = build_fold_schedule("2022Q1", "2022Q1", TRADING_DAYS)
            outcome = pipeline.run_fold(folds[0], epoch_id="epoch_001", parent=None)
            captured: dict[str, object] = {}

            def inspect_meta_learner(ctx: ToolContext) -> None:
                tree_json = ctx.paths.steps / "tree.json"
                tree_txt = ctx.paths.steps / "tree.txt"
                captured["tree"] = json.loads(tree_json.read_text(encoding="utf-8"))
                captured["rendered"] = tree_txt.read_text(encoding="utf-8")
                (ctx.paths.workspace / "taste.md").write_text("read step tree", encoding="utf-8")

            pipeline.meta_learner = inspect_meta_learner
            frozen, taste = pipeline.run_meta_learning(epoch_id="epoch_002", parent=outcome.frozen)

            self.assertEqual(frozen.artifact_id, outcome.frozen.artifact_id)
            self.assertIn("read step tree", taste)
            tree = captured["tree"]
            self.assertGreaterEqual(len(tree["nodes"]), 1)
            self.assertEqual(tree["current_node_id"], tree["nodes"][-1]["node_id"])
            self.assertIn("epoch_001__fold_ref_", captured["rendered"])
            self.assertNotIn("2022Q1", captured["rendered"])

    def test_default_fold_exploration_direction_reaches_every_fold_manifest_and_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            default_direction = "持续检验事件冲击沿关系网络传播形成的定价时滞。"
            session_direction = "本 Fold 先比较行业边与概念边。"
            config = make_config(tmp, fold_exploration_directive=default_direction)
            captured: dict[str, object] = {}

            def agent_factory(ctx, fold, manifest):
                captured.update(manifest)
                return ScriptedFoldAgent(ctx)

            pipeline = ExperimentPipeline(
                config,
                FakeSnapshotProvider(),
                agent_factory,
                proxy=ScriptedLLM([]),
            )
            fold = build_fold_schedule("2022Q1", "2022Q1", TRADING_DAYS)[0]
            pipeline.run_fold(
                fold,
                epoch_id="epoch_001",
                parent=None,
                fold_directive=session_direction,
            )

            self.assertEqual(captured["fold_exploration_directive"], default_direction)
            self.assertEqual(captured["fold_directive"], session_direction)
            record = pipeline.ledger.read("fold")[0]
            self.assertEqual(record["fold_exploration_directive"], default_direction)
            self.assertEqual(record["fold_directive"], session_direction)

    def test_meta_learning_directive_is_recorded_in_manifest_and_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            directive = "Explore liquidity shock reversal under minute replay."
            fold_direction = "Explore event propagation with graphs and language evidence."
            config = make_config(
                tmp,
                meta_learning_directive=directive,
                fold_exploration_directive=fold_direction,
            )
            captured: dict[str, object] = {}

            def inspect_meta_learner(ctx: ToolContext) -> None:
                captured["manifest_directive"] = ctx.manifest.get("meta_learning_directive")
                captured["fold_exploration_directive"] = ctx.manifest.get("fold_exploration_directive")
                captured["manifest_keys"] = set(ctx.manifest.data)
                (ctx.paths.workspace / "taste.md").write_text("directive checked", encoding="utf-8")

            pipeline = ExperimentPipeline(
                config,
                FakeSnapshotProvider(),
                lambda ctx, fold, manifest: ScriptedFoldAgent(ctx),
                proxy=ScriptedLLM([]),
                meta_learner=inspect_meta_learner,
            )

            frozen, taste = pipeline.run_meta_learning(epoch_id="epoch_001", parent=None)

            self.assertIsNone(frozen)
            self.assertEqual(taste, "directive checked")
            self.assertEqual(captured["manifest_directive"], directive)
            self.assertEqual(captured["fold_exploration_directive"], fold_direction)
            self.assertTrue(
                {"auction_enabled", "auction_preopen_time", "auction_decision_time", "auction_close_time"}
                .isdisjoint(captured["manifest_keys"])
            )
            meta = pipeline.ledger.read("meta_learning")[0]
            self.assertEqual(meta["meta_learning_directive"], directive)
            self.assertEqual(meta["fold_exploration_directive"], fold_direction)

    def test_meta_learning_public_entry_forwards_user_question_hook(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            config = make_config(tmp)
            calls: list[tuple[int, str]] = []

            def question_hook(step_index: int, question: str) -> str:
                calls.append((step_index, question))
                return "继续验证"

            def inspect_meta_learner(ctx: ToolContext) -> None:
                hook = ctx.extra.get("user_question_hook")
                self.assertIs(hook, question_hook)
                self.assertEqual(hook(2, "是否继续？"), "继续验证")
                (ctx.paths.workspace / "taste.md").write_text("hook checked", encoding="utf-8")

            pipeline = ExperimentPipeline(
                config,
                FakeSnapshotProvider(),
                lambda ctx, fold, manifest: ScriptedFoldAgent(ctx),
                proxy=ScriptedLLM([]),
                meta_learner=inspect_meta_learner,
            )

            frozen, taste = pipeline.run_meta_learning(
                epoch_id="epoch_001",
                parent=None,
                user_question_hook=question_hook,
            )

            self.assertIsNone(frozen)
            self.assertEqual(taste, "hook checked")
            self.assertEqual(calls, [(2, "是否继续？")])

    def test_meta_learning_agent_ready_hook_runs_before_learner(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            config = make_config(tmp)
            events: list[str] = []

            def ready_hook() -> None:
                events.append("ready")

            def inspect_meta_learner(ctx: ToolContext) -> None:
                self.assertEqual(events, ["ready"])
                self.assertTrue(ctx.paths.run_manifest.exists())
                events.append("learner")
                (ctx.paths.workspace / "taste.md").write_text("hook checked", encoding="utf-8")

            pipeline = ExperimentPipeline(
                config,
                FakeSnapshotProvider(),
                lambda ctx, fold, manifest: ScriptedFoldAgent(ctx),
                proxy=ScriptedLLM([]),
                meta_learner=inspect_meta_learner,
            )

            _, taste = pipeline.run_meta_learning(
                epoch_id="epoch_001",
                parent=None,
                agent_ready_hook=ready_hook,
            )

            self.assertEqual(taste, "hook checked")
            self.assertEqual(events, ["ready", "learner"])

    def test_meta_learning_workspace_includes_sandbox_environment_example_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            config = make_config(tmp)
            pipeline = ExperimentPipeline(
                config,
                FakeSnapshotProvider(),
                lambda ctx, fold, manifest: ScriptedFoldAgent(ctx),
                proxy=ScriptedLLM([]),
            )

            meta_sandbox, _ = pipeline._start_sandbox("run_meta", kind="meta_learning")
            example_path = meta_sandbox.paths.workspace / "sandbox_environment.example.json"
            self.assertTrue(example_path.exists())
            self.assertFalse((meta_sandbox.paths.workspace / "sandbox_environment.json").exists())
            example = json.loads(example_path.read_text(encoding="utf-8"))
            self.assertEqual(example["python_packages"], [])
            self.assertEqual(example["apt_packages"], [])
            self.assertEqual(example["npm_packages"], [])
            self.assertIn("sandbox_environment.json", example["reason"])

            fold_sandbox, _ = pipeline._start_sandbox("run_fold", kind="fold")
            self.assertFalse((fold_sandbox.paths.workspace / "sandbox_environment.example.json").exists())
            self.assertFalse((fold_sandbox.paths.workspace / "sandbox_environment.json").exists())

    def test_meta_learning_rejects_unfinished_session_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            config = make_config(tmp)
            pipeline = ExperimentPipeline(
                config, FakeSnapshotProvider(), lambda ctx, fold, manifest: ScriptedFoldAgent(ctx), proxy=ScriptedLLM([])
            )

            def unfinished_meta_learner(ctx: ToolContext) -> dict[str, object]:
                (ctx.paths.workspace / "taste.md").write_text("should not be accepted", encoding="utf-8")
                return {"finish_status": "deadline_timeout"}

            pipeline.meta_learner = unfinished_meta_learner
            with self.assertRaisesRegex(RuntimeError, "did not finish with done"):
                pipeline.run_meta_learning(epoch_id="epoch_001", parent=None)

    def test_meta_learning_violating_constraints_keeps_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            config = make_config(tmp)
            proxy = ScriptedLLM([])
            pipeline = ExperimentPipeline(
                config, FakeSnapshotProvider(), lambda ctx, fold, manifest: ScriptedFoldAgent(ctx), proxy=proxy
            )
            folds = build_fold_schedule("2022Q1", "2022Q1", TRADING_DAYS)
            outcome = pipeline.run_fold(folds[0], epoch_id="epoch_001", parent=None)

            def bad_meta_learner(ctx: ToolContext) -> None:
                (ctx.paths.workspace / "taste.md").write_text("too many helper files", encoding="utf-8")
                for i in range(30):
                    (ctx.paths.agent_output / f"helper_{i:02d}.py").write_text("x = 1\n", encoding="utf-8")

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
                config, FakeSnapshotProvider(), lambda ctx, fold, manifest: ScriptedFoldAgent(ctx), proxy=ScriptedLLM([])
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

    def test_meta_learning_environment_request_builds_derived_sandbox_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            config = make_config(tmp, use_docker=True)
            pipeline = ExperimentPipeline(
                config, FakeSnapshotProvider(), lambda ctx, fold, manifest: ScriptedFoldAgent(ctx), proxy=ScriptedLLM([])
            )
            workspace = tmp / "workspace"
            workspace.mkdir()
            request_path = workspace / "sandbox_environment.json"
            request_path.write_text(
                json.dumps(
                    {
                        "python_packages": ["lightgbm==4.5.0"],
                        "apt_packages": ["libgomp1"],
                        "npm_packages": ["@scope/tool@1.2.3"],
                        "reason": "meta-learning selected a stable tree model dependency",
                    }
                ),
                encoding="utf-8",
            )
            manifest = RunManifest.create(
                tmp / "artifacts" / "run_manifest.json",
                {"kind": "meta_learning", "experiment_id": "exp_e2e"},
            )

            completed = subprocess.CompletedProcess(
                args=["docker", "build"],
                returncode=0,
                stdout="build ok",
                stderr="",
            )
            from autotrade.environment.sandbox_images import maybe_rebuild_sandbox_image

            with patch("autotrade.environment.sandbox_images.subprocess.run", return_value=completed) as mocked_run, patch(
                "autotrade.environment.sandbox_images.resolve_image_identity", return_value=("sha256:fake", [])
            ):
                result, active_spec = maybe_rebuild_sandbox_image(
                    request_path,
                    base_spec=pipeline._active_sandbox_spec,
                    experiment_id=config.experiment_id,
                    epoch_id="epoch_001",
                    experiment_dir=config.experiment_dir,
                    manifest=manifest,
                    use_docker=config.use_docker,
                    rebuild_enabled=config.meta_sandbox_rebuild_enabled,
                    timeout_seconds=config.meta_sandbox_rebuild_timeout_seconds,
                    image_keep=config.meta_sandbox_image_keep,
                )

            self.assertEqual(result["status"], "ok")
            self.assertTrue(str(active_spec.image).startswith("autotrade-sandbox:exp_e2e-epoch_001-"))
            dockerfile = Path(str(result["dockerfile_ref"]))
            text = dockerfile.read_text(encoding="utf-8")
            self.assertIn("FROM autotrade-sandbox:latest", text)
            self.assertIn("libgomp1", text)
            self.assertIn("lightgbm==4.5.0", text)
            self.assertIn("@scope/tool@1.2.3", text)
            # First subprocess call is the image build; a trailing `docker images`
            # GC call may follow once the build succeeds.
            self.assertEqual(mocked_run.call_args_list[0].args[0][0:2], ["docker", "build"])
            stored = json.loads((tmp / "artifacts" / "run_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(stored["sandbox_image_update"]["status"], "ok")
            self.assertNotIn("host_request_ref", stored["sandbox_image_update"])
            self.assertNotIn("dockerfile_ref", stored["sandbox_image_update"])

    def test_active_sandbox_image_reloads_from_ledger_on_fresh_pipeline(self):
        # A successful rebuild updates the active image only in-memory; a fresh
        # process (resumed/fold-only run) must reload the latest good derived image
        # from the ledger instead of falling back to the base.
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            config = make_config(tmp, use_docker=True)
            derived = "autotrade-sandbox:exp_e2e-epoch_002-aaaaaaaaaaaa"
            seed = ExperimentPipeline(
                config, FakeSnapshotProvider(), lambda ctx, fold, manifest: ScriptedFoldAgent(ctx), proxy=ScriptedLLM([])
            )
            def _meta(epoch, status, image):
                return {"record_type": "meta_learning", "experiment_id": "exp_e2e",
                        "epoch_id": epoch, "fold_id": "fold_2022Q1", "run_id": f"run_{epoch}_{status}",
                        "sandbox_image_update": {"status": status, "image": image}}

            seed.ledger.append(_meta("epoch_001", "ok", "autotrade-sandbox:exp_e2e-epoch_001-old0"))
            seed.ledger.append(_meta("epoch_001", "failed", "autotrade-sandbox:exp_e2e-broken"))
            seed.ledger.append(_meta("epoch_002", "ok", derived))

            resumed = ExperimentPipeline(
                config, FakeSnapshotProvider(), lambda ctx, fold, manifest: ScriptedFoldAgent(ctx), proxy=ScriptedLLM([])
            )
            self.assertEqual(resumed._active_sandbox_spec.image, derived)  # latest ok wins, failed ignored

    def test_derived_sandbox_image_gc_prunes_old_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            config = make_config(tmp, use_docker=True, meta_sandbox_image_keep=1)
            pipeline = ExperimentPipeline(
                config, FakeSnapshotProvider(), lambda ctx, fold, manifest: ScriptedFoldAgent(ctx), proxy=ScriptedLLM([])
            )
            keep_image = "autotrade-sandbox:exp_e2e-epoch_002-new000000000"
            listing = (
                f"{keep_image}\t2026-06-27 10:00:00\n"
                "autotrade-sandbox:exp_e2e-epoch_001-old111111111\t2026-06-26 10:00:00\n"
                "autotrade-sandbox:exp_e2e-epoch_001-old222222222\t2026-06-25 10:00:00\n"
                "autotrade-sandbox:other_exp-epoch_001-zzz\t2026-06-24 10:00:00\n"
            )

            def fake_run(cmd, **_kwargs):
                if cmd[:2] == ["docker", "images"]:
                    return subprocess.CompletedProcess(cmd, 0, stdout=listing, stderr="")
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

            from autotrade.environment.sandbox_images import _gc_derived_sandbox_images

            with patch("autotrade.environment.sandbox_images.subprocess.run", side_effect=fake_run):
                pruned = _gc_derived_sandbox_images(config.experiment_id, keep=1, keep_image=keep_image)
            # keep the newest 1, drop the older same-experiment tail; never the active
            # image, never another experiment's images.
            self.assertEqual(
                pruned,
                ["autotrade-sandbox:exp_e2e-epoch_001-old111111111",
                 "autotrade-sandbox:exp_e2e-epoch_001-old222222222"],
            )

    def test_meta_learning_records_artifacts_when_sandbox_image_rebuild_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            config = make_config(tmp)
            pipeline = ExperimentPipeline(
                config,
                FakeSnapshotProvider(),
                lambda ctx, fold, manifest: ScriptedFoldAgent(ctx),
                proxy=ScriptedLLM([]),
            )

            def meta_learner(ctx: ToolContext) -> None:
                (ctx.paths.workspace / "taste.md").write_text("gnn taste", encoding="utf-8")
                (ctx.paths.workspace / "sandbox_environment.json").write_text(
                    json.dumps({"python_packages": ["torch_geometric>=2.5.0"]}),
                    encoding="utf-8",
                )

            def fail_rebuild(*_args, **kwargs) -> None:
                kwargs["manifest"].update(sandbox_image_update={"status": "failed", "image": "broken"})
                raise RuntimeError("meta-learning sandbox image rebuild failed: broken")

            pipeline.meta_learner = meta_learner
            with patch("autotrade.pipelines.experiment.maybe_rebuild_sandbox_image", side_effect=fail_rebuild):
                with self.assertRaisesRegex(RuntimeError, "sandbox image rebuild failed"):
                    pipeline.run_meta_learning(epoch_id="epoch_001", parent=None)

            meta = pipeline.ledger.read("meta_learning")[0]
            self.assertEqual(meta["taste_chars"], len("gnn taste"))
            self.assertEqual(meta["sandbox_image_update"]["status"], "failed")
            self.assertTrue(Path(str(meta["agent_trace_ref"])).exists())
            self.assertTrue((config.experiment_dir / "meta_learning" / "epoch_001" / "taste.md").exists())

    def test_meta_learning_environment_request_rejects_invalid_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            config = make_config(tmp, use_docker=True)
            pipeline = ExperimentPipeline(
                config, FakeSnapshotProvider(), lambda ctx, fold, manifest: ScriptedFoldAgent(ctx), proxy=ScriptedLLM([])
            )
            workspace = tmp / "workspace"
            workspace.mkdir()
            request_path = workspace / "sandbox_environment.json"
            request_path.write_text(
                json.dumps({"python_packages": ["--extra-index-url"], "shell": "pip install x"}),
                encoding="utf-8",
            )
            manifest = RunManifest.create(
                tmp / "artifacts" / "run_manifest.json",
                {"kind": "meta_learning", "experiment_id": "exp_e2e"},
            )

            from autotrade.environment.sandbox_images import maybe_rebuild_sandbox_image

            with self.assertRaisesRegex(RuntimeError, "sandbox environment request rejected"):
                maybe_rebuild_sandbox_image(
                    request_path,
                    base_spec=pipeline._active_sandbox_spec,
                    experiment_id=config.experiment_id,
                    epoch_id="epoch_001",
                    experiment_dir=config.experiment_dir,
                    manifest=manifest,
                    use_docker=config.use_docker,
                    rebuild_enabled=config.meta_sandbox_rebuild_enabled,
                    timeout_seconds=config.meta_sandbox_rebuild_timeout_seconds,
                    image_keep=config.meta_sandbox_image_keep,
                )

            stored = json.loads((tmp / "artifacts" / "run_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(stored["sandbox_image_update"]["status"], "rejected")


def _docker_with_image() -> bool:
    import subprocess

    from autotrade.environment.executor import docker_available
    from autotrade.environment.sandbox import SandboxSpec

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
            proxy = ScriptedLLM([])
            pipeline = ExperimentPipeline(
                config, FakeSnapshotProvider(), lambda ctx, fold, manifest: ScriptedFoldAgent(ctx), proxy=proxy
            )
            fold = build_fold_schedule("2022Q1", "2022Q1", TRADING_DAYS)[0]
            outcome = pipeline.run_fold(fold, epoch_id="epoch_001", parent=None)
            self.assertEqual(outcome.fold_status, "frozen")
            self.assertGreater(outcome.test_summary["total_return"], 0.0)


class ArchitectureBoundaryTest(unittest.TestCase):
    def test_environment_does_not_import_agent(self):
        pattern = re.compile(r"^\s*(from|import)\s+autotrade\.agent\b", re.MULTILINE)
        offenders = [
            str(path)
            for path in SRC_ENV_DIR.rglob("*.py")
            if pattern.search(path.read_text(encoding="utf-8"))
        ]
        self.assertEqual(offenders, [])

    def test_environment_does_not_import_data_sources(self):
        # The ingest adapter imports the environment's raw-lake contract
        # (environment/data/contracts.py), never the reverse.
        pattern = re.compile(r"^\s*(from|import)\s+autotrade\.data_sources\b", re.MULTILINE)
        offenders = [
            str(path)
            for path in SRC_ENV_DIR.rglob("*.py")
            if pattern.search(path.read_text(encoding="utf-8"))
        ]
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
