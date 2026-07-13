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
from pathlib import Path
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

    def replay_slot(self, start: str, end: str, view: Path, *, label: str) -> dict[str, object]:
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
        self.assertEqual(len(warnings), 2)

    def test_rule_values_must_be_finite_and_ranged(self):
        with self.assertRaises(ValueError):
            AcceptanceRules(min_return=math.nan)
        with self.assertRaises(ValueError):
            AcceptanceRules(max_drawdown=1.5)


class CachingSnapshotProviderGenerationTest(unittest.TestCase):
    def test_cache_key_includes_raw_generation(self):
        import json

        from autotrade.pipelines.config import CachingSnapshotProvider

        class FakeProvider:
            def __init__(self, raw_dir: Path) -> None:
                self.raw_dir = raw_dir
                self.config = None
                self.builds = 0

            def replay_slot(self, start, end, view, *, label):
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
                json.dumps({"generation_id": "gen2", "completed_at": "now"}), encoding="utf-8"
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
            with mock.patch("autotrade.pipelines.config.SNAPSHOT_CACHE_FORMAT_VERSION", 2):
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

        from autotrade.environment.snapshot import SnapshotConfig
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
