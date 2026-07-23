"""Numeric validation of pipeline config records (acceptance rules, budgets)
and default-value drift guards across the config surfaces."""

import json
import math
import multiprocessing
import os
import tempfile
import time
import unittest
from dataclasses import MISSING, fields
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

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


class _FileCountingReplayProvider:
    """Process-safe-enough fixture: one append records each real build."""

    config = None

    def __init__(self, raw_dir: Path, call_log: Path, delay_seconds: float = 0.0) -> None:
        self.raw_dir = raw_dir
        self.call_log = call_log
        self.delay_seconds = delay_seconds

    def replay_slot(self, start: str, end: str, view: Path, *, label: str, available_from=None) -> dict[str, object]:
        fd = os.open(self.call_log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, f"{os.getpid()}:{label}\n".encode("utf-8"))
        finally:
            os.close(fd)
        time.sleep(self.delay_seconds)
        view = Path(view)
        view.mkdir(parents=True)
        (view / "daily.parquet").write_bytes(b"shared replay bytes")
        manifest: dict[str, object] = {
            "kind": "replay_slot",
            "label": label,
            "period_start": start,
            "period_end": end,
        }
        (view / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return manifest


def _cached_replay_worker(
    raw_dir: Path,
    cache_root: Path,
    call_log: Path,
    out_dir: Path,
    label: str,
    start_event: object,
    result_queue: object,
) -> None:
    from autotrade.pipelines.config import CachingSnapshotProvider

    start_event.wait()
    try:
        provider = _FileCountingReplayProvider(raw_dir, call_log, delay_seconds=0.2)
        manifest = CachingSnapshotProvider(provider, cache_root).replay_slot(
            "20220101", "20220131", out_dir, label=label
        )
        result_queue.put(("ok", label, manifest.get("label")))
    except Exception as exc:  # pragma: no cover - returned to the parent assertion
        result_queue.put(("error", label, repr(exc)))


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
        self.assertEqual(
            warnings,
            ["validation return -1.00% < 0.00%", "sharpe -0.20 < 0.00"],
        )

    def test_rule_values_must_be_finite_and_ranged(self):
        with self.assertRaises(ValueError):
            AcceptanceRules(min_return=math.nan)
        with self.assertRaises(ValueError):
            AcceptanceRules(max_drawdown=1.5)


class CachingSnapshotProviderGenerationTest(unittest.TestCase):
    def test_prefetch_fold_populates_cache_without_run_output(self):
        from autotrade.pipelines.config import CachingSnapshotProvider

        class FakeProvider:
            config = None

            def __init__(self, raw_dir: Path) -> None:
                self.raw_dir = raw_dir
                self.decision_builds = 0
                self.replay_builds = 0

            def decision_snapshot(self, decision_time, view):
                self.decision_builds += 1
                Path(view).mkdir(parents=True)
                (Path(view) / "daily.parquet").write_bytes(str(decision_time).encode())
                manifest = {"kind": "decision_input", "decision_time": decision_time.isoformat()}
                (Path(view) / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
                return manifest

            def replay_slot(self, start, end, view, *, label, available_from=None):
                self.replay_builds += 1
                Path(view).mkdir(parents=True)
                (Path(view) / "daily.parquet").write_bytes(f"{start}:{end}".encode())
                manifest = {"kind": "replay_slot", "label": label}
                (Path(view) / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
                return manifest

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = root / "raw"
            raw.mkdir()
            provider = FakeProvider(raw)
            caching = CachingSnapshotProvider(provider, root / "cache")
            fold = SimpleNamespace(
                valid_decision_time=datetime(2022, 1, 1, 9, 25),
                test_decision_time=datetime(2022, 4, 1, 9, 25),
                validation_start="20220101",
                validation_end="20220331",
                test_start="20220401",
                test_end="20220630",
            )

            manifests = caching.prefetch_fold(fold)

            self.assertEqual(set(manifests), {
                "valid_decision_input", "test_decision_input", "valid_replay", "test_replay"
            })
            self.assertEqual((provider.decision_builds, provider.replay_builds), (2, 2))
            self.assertFalse((root / "run").exists())
            entries = [path for path in (root / "cache").iterdir() if not path.name.startswith(".")]
            self.assertEqual(len(entries), 4)

            caching.decision_snapshot(fold.valid_decision_time, root / "run" / "valid_view")
            caching.decision_snapshot(fold.test_decision_time, root / "run" / "test_view")
            caching.replay_slot(
                fold.validation_start, fold.validation_end, root / "run" / "valid", label="valid",
                available_from=fold.valid_decision_time,
            )
            caching.replay_slot(
                fold.test_start, fold.test_end, root / "run" / "test", label="test",
                available_from=fold.test_decision_time,
            )
            self.assertEqual((provider.decision_builds, provider.replay_builds), (2, 2))
            self.assertEqual(
                json.loads((root / "run" / "valid" / "manifest.json").read_text(encoding="utf-8"))["label"],
                "valid",
            )

    def test_cache_key_includes_raw_generation(self):
        import json

        from autotrade.pipelines.config import CachingSnapshotProvider

        class FakeProvider:
            def __init__(self, raw_dir: Path) -> None:
                self.raw_dir = raw_dir
                self.config = None
                self.builds = 0

            def replay_slot(self, start, end, view, *, label, available_from=None):
                self.builds += 1
                Path(view).mkdir(parents=True, exist_ok=True)
                (Path(view) / "daily.parquet").write_bytes(b"x")
                return {"build": self.builds}

        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "raw"
            raw.mkdir()
            provider = FakeProvider(raw)
            caching = CachingSnapshotProvider(provider, Path(tmp) / "cache")

            caching.replay_slot("20220101", "20220131", Path(tmp) / "out1", label="valid")
            caching.replay_slot("20220101", "20220131", Path(tmp) / "out2", label="valid")
            self.assertEqual(provider.builds, 1)  # same generation -> cache hit

            (raw / ".raw_generation.json").write_text(
                json.dumps({
                    "schema_version": 2,
                    "state": "committed",
                    "generation_id": "gen2",
                    "completed_at": "now",
                }),
                encoding="utf-8",
            )
            caching.replay_slot("20220101", "20220131", Path(tmp) / "out3", label="valid")
            self.assertEqual(provider.builds, 2)  # new generation -> rebuild

    def test_cache_key_includes_explicit_format_version(self):
        from autotrade.pipelines.config import CachingSnapshotProvider

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw_dir = root / "raw"
            raw_dir.mkdir()
            call_log = root / "builds.log"
            provider = _FileCountingReplayProvider(raw_dir, call_log)
            caching = CachingSnapshotProvider(provider, root / "cache")

            caching.replay_slot("20220101", "20220131", root / "out_v1", label="valid")
            with mock.patch("autotrade.pipelines.config.SNAPSHOT_CACHE_FORMAT_VERSION", 99):
                caching.replay_slot("20220101", "20220131", root / "out_v2", label="valid")

            self.assertEqual(len(call_log.read_text(encoding="utf-8").splitlines()), 2)
            entries = [path for path in (root / "cache").iterdir() if not path.name.startswith(".")]
            self.assertEqual(len(entries), 2)

    def test_replay_cache_is_single_flight_and_labels_are_output_local(self):
        from autotrade.pipelines.config import CachingSnapshotProvider

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw_dir = root / "raw"
            cache_root = root / "cache"
            call_log = root / "builds.log"
            raw_dir.mkdir()
            context = multiprocessing.get_context("fork")
            start_event = context.Event()
            result_queue = context.Queue()
            outputs = {"valid": root / "valid", "test": root / "test"}
            processes = [
                context.Process(
                    target=_cached_replay_worker,
                    args=(raw_dir, cache_root, call_log, outputs[label], label, start_event, result_queue),
                )
                for label in outputs
            ]
            try:
                for process in processes:
                    process.start()
                start_event.set()
                for process in processes:
                    process.join(timeout=10)
                self.assertFalse(any(process.is_alive() for process in processes), "cache workers deadlocked")
                self.assertTrue(all(process.exitcode == 0 for process in processes))
                results = [result_queue.get(timeout=2) for _ in processes]
            finally:
                for process in processes:
                    if process.is_alive():
                        process.terminate()
                        process.join(timeout=2)

            self.assertEqual({result[0] for result in results}, {"ok"})
            self.assertEqual({(result[1], result[2]) for result in results}, {("valid", "valid"), ("test", "test")})
            self.assertEqual(len(call_log.read_text(encoding="utf-8").splitlines()), 1)

            entries = [path for path in cache_root.iterdir() if not path.name.startswith(".")]
            self.assertEqual(len(entries), 1)  # label is absent from the replay content key
            cached_view = entries[0] / "view"
            self.assertEqual(json.loads((cached_view / "manifest.json").read_text(encoding="utf-8"))["label"], "")
            self.assertEqual(json.loads((entries[0] / "cache_manifest.json").read_text(encoding="utf-8"))["label"], "")
            for label, out_dir in outputs.items():
                self.assertEqual(json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))["label"], label)
                self.assertNotEqual(
                    (out_dir / "manifest.json").stat().st_ino,
                    (cached_view / "manifest.json").stat().st_ino,
                )
                self.assertEqual(
                    (out_dir / "daily.parquet").stat().st_ino,
                    (cached_view / "daily.parquet").stat().st_ino,
                )

            # A later same-process hit keeps the same content entry and gets its
            # own label manifest without mutating either previous output.
            provider = _FileCountingReplayProvider(raw_dir, call_log)
            probe = root / "probe"
            returned = CachingSnapshotProvider(provider, cache_root).replay_slot(
                "20220101", "20220131", probe, label="probe"
            )
            self.assertEqual(returned["label"], "probe")
            self.assertEqual(len(call_log.read_text(encoding="utf-8").splitlines()), 1)
            valid_manifest = json.loads((outputs["valid"] / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(valid_manifest["label"], "valid")

    def test_cache_publish_rename_error_is_not_swallowed(self):
        from autotrade.pipelines.config import CachingSnapshotProvider

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw_dir = root / "raw"
            raw_dir.mkdir()
            provider = _FileCountingReplayProvider(raw_dir, root / "builds.log")
            caching = CachingSnapshotProvider(provider, root / "cache")
            with mock.patch.object(Path, "rename", side_effect=PermissionError("publish denied")):
                with self.assertRaisesRegex(PermissionError, "publish denied"):
                    caching.replay_slot("20220101", "20220131", root / "out", label="valid")


class SnapshotDomainFilterTest(unittest.TestCase):
    def test_domain_switches_and_dataset_subsets(self):
        from types import SimpleNamespace

        from autotrade.environment.data.snapshot import SnapshotConfig
        from autotrade.pipelines.assembly import build_snapshot_config
        from autotrade.pipelines.hitl_state import PARAM_DEFAULTS, resolve_options

        base = SnapshotConfig()
        defaults = SimpleNamespace(**PARAM_DEFAULTS)
        config = build_snapshot_config(defaults)
        self.assertEqual(config.events_datasets, base.events_datasets)  # empty subset = full default
        self.assertTrue(config.include_intraday)

        filtered = SimpleNamespace(**{
            **PARAM_DEFAULTS,
            "include_events": False,
            "include_intraday": False,
            "macro_datasets": ["cn_gdp", "index_daily"],
        })
        config = build_snapshot_config(filtered)
        self.assertEqual(config.events_datasets, ())          # domain off everywhere
        self.assertFalse(config.replay_include_events)
        self.assertFalse(config.include_intraday)
        self.assertFalse(config.replay_include_minutes)
        self.assertEqual(config.macro_datasets, ("cn_gdp", "index_daily"))
        self.assertTrue(config.replay_include_macro)

        with self.assertRaisesRegex(ValueError, "unknown macro_datasets"):
            build_snapshot_config(SimpleNamespace(**{**PARAM_DEFAULTS, "macro_datasets": ["nope"]}))


class ProductionPipelineWiringTest(unittest.TestCase):
    def test_build_pipeline_uses_the_experiment_research_release(self):
        from autotrade.pipelines.assembly import build_pipeline

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = make_config(root)
            source_raw = root / "data" / "raw"
            source_events = root / "data" / "pit" / "fundamental_events"
            source_status = root / "results" / "data_quality" / "fundamental_events_status.json"
            pinned_raw = root / "release" / "raw"
            pinned_events = root / "release" / "fundamental_events"
            pinned_status = root / "release" / "data_quality" / "fundamental_events_status.json"
            args = SimpleNamespace(
                raw_dir=source_raw,
                fundamental_events_root=source_events,
                fundamental_events_status=source_status,
            )
            release = SimpleNamespace(
                raw_dir=pinned_raw,
                fundamental_events_root=pinned_events,
                fundamental_events_status=pinned_status,
            )
            proxies = SimpleNamespace(proxy=None, nl_proxy=None)

            with mock.patch(
                "autotrade.pipelines.assembly.pin_research_release", return_value=release
            ) as pin:
                pipeline = build_pipeline(config, args, lambda *_args: None, None, proxies)

            snapshot_config = config.snapshot_config
            pin.assert_called_once_with(
                experiment_dir=config.experiment_dir,
                raw_dir=source_raw.resolve(),
                fundamental_events_root=source_events.resolve(),
                fundamental_events_status=source_status.resolve(),
                required_raw_datasets=(
                    *snapshot_config.macro_datasets,
                    *snapshot_config.events_datasets,
                    *snapshot_config.text_datasets,
                ),
            )
            self.assertEqual(pipeline.raw_dir, pinned_raw)
            self.assertEqual(pipeline.snapshots._provider.raw_dir, pinned_raw)
            self.assertEqual(pipeline.snapshots._provider.builder.fundamental_events_root, pinned_events)
            self.assertEqual(pipeline.snapshots._provider.builder.fundamental_events_status, pinned_status)


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
            self.assertEqual(config.meta_learning_fold_interval, 0)
            self.assertEqual(config.fold_exploration_directive, "")

    def test_meta_learning_fold_interval_is_a_non_negative_integer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for value in (-1, math.nan, math.inf, 1.5):
                with self.subTest(value=value):
                    with self.assertRaisesRegex(ValueError, "meta_learning_fold_interval"):
                        make_config(root, meta_learning_fold_interval=value)
            self.assertEqual(make_config(root, meta_learning_fold_interval=3).meta_learning_fold_interval, 3)


class DefaultsDriftTest(unittest.TestCase):
    """The three default surfaces (domain dataclasses, HITL PARAM_DEFAULTS,
    run_experiment CLI) must agree; the dataclasses are the source of truth."""

    def test_param_defaults_match_domain_dataclasses(self):
        removed_auction_knobs = {
            "auction_enabled", "auction_preopen_time", "auction_decision_time", "auction_close_time",
        }
        self.assertTrue(removed_auction_knobs.isdisjoint(field_obj.name for field_obj in fields(ExperimentConfig)))
        self.assertTrue(removed_auction_knobs.isdisjoint(PARAM_DEFAULTS))
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
            # Repo-root-resolved path defaults (PARAM_DEFAULTS keeps them
            # repo-relative by design).
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


class ConfigBuilderCompletenessTest(unittest.TestCase):
    """Every ExperimentConfig field must flow through the single shared builder
    (assembly.build_experiment_config, consumed by run_experiment,
    run_audit_session, and the HITL worker) or be explicitly recorded here as
    not entrypoint-configurable — a future field cannot silently reach only one
    entrypoint again (the H1 drift this batch consolidated away)."""

    # Dataclass-default-only knobs: deliberately no CLI/HITL surface.
    NOT_ENTRYPOINT_CONFIGURABLE = {
        "afterhours_decision_time",
        "backtest_final_eval_max_seconds_per_decision",
        "backtest_final_eval_max_seconds_per_trading_day",
        "timeview_enabled",
        "step_constraints",
        "regularization_constraints",
    }
    # Fields the builder composes from renamed keys, several keys, or explicit
    # builder parameters (sandbox_spec and the meta specs derived from it).
    COMPOSED_SOURCES = {
        "step_tree_enabled": ("disable_step_tree",),
        "use_docker": ("local_dev",),
        "meta_sandbox_rebuild_enabled": ("disable_meta_sandbox_rebuild",),
        "acceptance": ("min_return", "min_sharpe", "max_drawdown"),
        "broker_profile": (
            "stock_initial_cash", "credit_initial_cash", "commission_bps", "slippage_bps",
            "max_total_holdings", "max_single_name_weight", "fin_rate_annual", "slo_rate_annual",
        ),
        "snapshot_config": ("window_months", "intraday_trade_days"),
        "sandbox_spec": ("gpu_count",),
        "meta_learning_sandbox_spec": ("meta_learning_network",),
        "meta_learning_managed_proxy": ("meta_learning_network",),
    }
    PATH_FIELDS = {"experiments_root", "work_root", "template_dir"}
    # One valid non-default value per direct-named configurable field; the
    # coverage assertion below forces this map to grow with the dataclass.
    DIRECT_OVERRIDES = {
        "experiment_id": "exp_builder_completeness",
        "first_test_period": "2022Q1",
        "last_test_period": "2022Q2",
        "heldout_first_period": "2023Q1",
        "heldout_last_period": "2023Q2",
        "fold_period": "quarter",
        "epochs": 5,
        "window_months": 9,
        "max_fold_minutes": 33,
        "finalize_before_deadline_seconds": 240,
        "per_call_timeout_seconds": 111,
        "max_steps_per_fold": 4,
        "max_backtests_per_fold": 9,
        "offsession_tick_minutes": 15,
        "intraday_decision_minutes": 5,
        "execution_lag_bars": 3,
        "decision_max_sim_minutes": 12.5,
        "backtest_max_seconds_per_decision": 100.0,
        "backtest_max_seconds_per_trading_day": 200.0,
        "nl_max_calls_per_decision_day": 6,
        "nl_max_calls_per_backtest": 44,
        "nl_failure_policy": "fail",
        "convergence_start_epoch": 2,
        "meta_learning_directive": "研究方向：事件驱动",
        "fold_exploration_directive": "探索方向：低换手",
        "meta_learning_fold_interval": 2,
        "meta_memory_max_epochs": 1,
        "record_failed_attempts": False,
        "meta_sandbox_rebuild_timeout_seconds": 900,
        "meta_sandbox_image_keep": 2,
    }

    def test_every_field_has_a_builder_source_or_is_recorded(self):
        for field_obj in fields(ExperimentConfig):
            name = field_obj.name
            if name in self.NOT_ENTRYPOINT_CONFIGURABLE:
                self.assertNotIn(name, PARAM_DEFAULTS, name)
                continue
            for source in self.COMPOSED_SOURCES.get(name, (name,)):
                self.assertIn(
                    source, PARAM_DEFAULTS,
                    f"ExperimentConfig.{name} has no PARAM_DEFAULTS source {source!r}; wire it "
                    "through assembly.build_experiment_config (and PARAM_DEFAULTS) or record it "
                    "in NOT_ENTRYPOINT_CONFIGURABLE",
                )

    def test_builder_propagates_every_direct_field(self):
        from autotrade.environment.sandbox import SandboxSpec
        from autotrade.pipelines.assembly import build_experiment_config
        from autotrade.pipelines.hitl_state import resolve_options

        direct_fields = {
            field_obj.name
            for field_obj in fields(ExperimentConfig)
            if field_obj.name not in self.NOT_ENTRYPOINT_CONFIGURABLE
            and field_obj.name not in self.COMPOSED_SOURCES
            and field_obj.name not in self.PATH_FIELDS
        }
        self.assertEqual(
            sorted(direct_fields), sorted(self.DIRECT_OVERRIDES),
            "extend DIRECT_OVERRIDES (and the builder) when ExperimentConfig gains a field",
        )
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp).resolve()
            options = resolve_options(
                {**self.DIRECT_OVERRIDES, "min_return": 0.01, "disable_step_tree": True, "local_dev": True},
                repo_root,
            )
            config = build_experiment_config(
                options, repo_root=repo_root, sandbox_spec=SandboxSpec(gpu=None)
            )
            for name in self.PATH_FIELDS:
                self.assertEqual(getattr(config, name), (repo_root / PARAM_DEFAULTS[name]).resolve(), name)
        for name, expected in self.DIRECT_OVERRIDES.items():
            self.assertEqual(getattr(config, name), expected, name)
        # Renamed/composed knobs land too (spot-checks; broker/session budgets
        # are covered by the interactive worker's extended-params test).
        self.assertEqual(config.acceptance.min_return, 0.01)
        self.assertFalse(config.step_tree_enabled)
        self.assertFalse(config.use_docker)


if __name__ == "__main__":
    unittest.main()
