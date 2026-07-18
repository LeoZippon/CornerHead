"""On-demand immutable research-release checkpoints."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from autotrade.environment.research_release import DOMAIN_STATUS_FILES, pin_research_release


class ResearchReleaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.raw = self.root / "data" / "raw"
        self.pit = self.root / "data" / "pit" / "fundamental_events"
        self.quality = self.root / "results" / "data_quality"
        self.status = self.quality / "fundamental_events_status.json"
        self.lock = self.root / ".runtime" / "tushare" / "locks" / "tushare_update.lock"
        self.raw.mkdir(parents=True)
        self.pit.mkdir(parents=True)
        self.quality.mkdir(parents=True)
        self.lock.parent.mkdir(parents=True)
        self.lock.touch()
        self._write_generation("gen1", state="committed")
        self._write_raw_pair("daily/trade_date=20260102", b"raw-v1")
        (self.raw / "reference.json").write_text('{"version": 1}\n', encoding="utf-8")
        pit_path = self.pit / "income" / "available_month=202601.parquet"
        pit_path.parent.mkdir(parents=True)
        pit_path.write_bytes(b"pit-v1")
        for index, name in enumerate(DOMAIN_STATUS_FILES.values()):
            (self.quality / name).write_text(
                json.dumps({"status": "ok", "version": index}) + "\n", encoding="utf-8"
            )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write_generation(self, generation_id: str, *, state: str) -> None:
        (self.raw / ".raw_generation.json").write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "state": state,
                    "generation_id": generation_id,
                    "completed_at": "2026-01-03T00:00:00+00:00",
                    "transaction": {"job": "test"},
                }
            )
            + "\n",
            encoding="utf-8",
        )

    def _write_raw_pair(self, stem: str, payload: bytes) -> tuple[Path, Path]:
        parquet = self.raw / f"{stem}.parquet"
        metadata = self.raw / f"{stem}.parquet.meta.json"
        parquet.parent.mkdir(parents=True, exist_ok=True)
        parquet.write_bytes(payload)
        metadata.write_text('{"row_count": 1}\n', encoding="utf-8")
        return parquet, metadata

    def _pin(self, experiment: str, required: tuple[str, ...] = ()):
        return pin_research_release(
            experiment_dir=self.root / "experiments" / experiment,
            raw_dir=self.raw,
            fundamental_events_root=self.pit,
            fundamental_events_status=self.status,
            required_raw_datasets=required,
        )

    def test_committed_release_hardlinks_only_parquet_contract_files(self) -> None:
        live_parquet = self.raw / "daily" / "trade_date=20260102.parquet"
        live_meta = live_parquet.with_suffix(".parquet.meta.json")
        live_pit = self.pit / "income" / "available_month=202601.parquet"
        live_marker = self.raw / ".raw_generation.json"
        live_json = self.raw / "reference.json"
        live_dir = self.raw / "rt_min_live"
        live_dir.mkdir()
        (live_dir / "trade_date=20260102.parquet").write_bytes(b"live-only")

        release = self._pin("exp1")

        self.assertEqual(release.generation_id, "gen1")
        self.assertEqual(
            os.stat(live_parquet).st_ino,
            os.stat(release.raw_dir / live_parquet.relative_to(self.raw)).st_ino,
        )
        self.assertEqual(os.stat(live_meta).st_ino, os.stat(release.raw_dir / live_meta.relative_to(self.raw)).st_ino)
        self.assertEqual(
            os.stat(live_pit).st_ino,
            os.stat(release.fundamental_events_root / live_pit.relative_to(self.pit)).st_ino,
        )
        self.assertNotEqual(os.stat(live_marker).st_ino, os.stat(release.raw_dir / live_marker.name).st_ino)
        self.assertNotEqual(os.stat(live_json).st_ino, os.stat(release.raw_dir / live_json.name).st_ino)
        self.assertNotEqual(os.stat(self.status).st_ino, os.stat(release.fundamental_events_status).st_ino)
        self.assertFalse((release.raw_dir / "rt_min_live").exists())
        self.assertTrue((self.root / "experiments" / "exp1" / "research_release" / "manifest.json").is_file())

    def test_atomic_live_replacement_does_not_change_release(self) -> None:
        release = self._pin("exp1")
        live = self.raw / "daily" / "trade_date=20260102.parquet"
        frozen = release.raw_dir / live.relative_to(self.raw)
        replacement = live.with_suffix(".parquet.tmp-replacement")
        replacement.write_bytes(b"raw-v2")
        replacement.replace(live)

        self.assertEqual(live.read_bytes(), b"raw-v2")
        self.assertEqual(frozen.read_bytes(), b"raw-v1")

        live_pit = self.pit / "income" / "available_month=202601.parquet"
        frozen_pit = release.fundamental_events_root / live_pit.relative_to(self.pit)
        pit_replacement = live_pit.with_suffix(".parquet.tmp-replacement")
        pit_replacement.write_bytes(b"pit-v2")
        pit_replacement.replace(live_pit)
        self.assertEqual(live_pit.read_bytes(), b"pit-v2")
        self.assertEqual(frozen_pit.read_bytes(), b"pit-v1")

    def test_existing_pin_wins_after_live_generation_changes(self) -> None:
        first = self._pin("exp1")
        self._write_generation("gen2", state="committed")
        (self.quality / "fundamental_events_status.json").write_text(
            '{"status":"error"}\n', encoding="utf-8"
        )

        resumed = self._pin("exp1")

        self.assertEqual(resumed, first)
        self.assertEqual(resumed.generation_id, "gen1")
        self.assertIn('"status": "ok"', resumed.fundamental_events_status.read_text(encoding="utf-8"))

    def test_same_generation_rejects_a_different_source_contract(self) -> None:
        self._pin("seed")
        custom_status = self.quality / "custom_fundamental_status.json"
        custom_status.write_text('{"status":"ok"}\n', encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "different raw/PIT/status contract"):
            pin_research_release(
                experiment_dir=self.root / "experiments" / "custom-status",
                raw_dir=self.raw,
                fundamental_events_root=self.pit,
                fundamental_events_status=custom_status,
            )

        alternate_pit = self.root / "data" / "alternate_pit"
        alternate_pit.mkdir()
        with self.assertRaisesRegex(RuntimeError, "different raw/PIT/status contract"):
            pin_research_release(
                experiment_dir=self.root / "experiments" / "alternate-pit",
                raw_dir=self.raw,
                fundamental_events_root=alternate_pit,
                fundamental_events_status=self.status,
            )

    def test_updating_generation_uses_previous_release_and_baseline_quality(self) -> None:
        first = self._pin("seed")
        self._write_generation("gen1", state="updating")
        self.status.write_text('{"status":"partial"}\n', encoding="utf-8")
        live = self.raw / "daily" / "trade_date=20260102.parquet"
        replacement = live.with_suffix(".parquet.tmp-new")
        replacement.write_bytes(b"partial")
        replacement.replace(live)

        during_update = self._pin("exp2")

        self.assertEqual(during_update.raw_dir, first.raw_dir)
        self.assertEqual(during_update.generation_id, "gen1")
        self.assertEqual(
            during_update.fundamental_events_status.read_text(encoding="utf-8"),
            first.fundamental_events_status.read_text(encoding="utf-8"),
        )
        self.assertEqual(
            (during_update.raw_dir / "daily" / "trade_date=20260102.parquet").read_bytes(),
            b"raw-v1",
        )

    def test_busy_updater_lock_immediately_uses_existing_release(self) -> None:
        previous = self._pin("seed")
        self._write_generation("gen2", state="committed")
        held = self.lock.open("rb")
        fcntl.flock(held.fileno(), fcntl.LOCK_EX)
        try:
            started = time.monotonic()
            release = self._pin("exp1")
            elapsed = time.monotonic() - started
        finally:
            fcntl.flock(held.fileno(), fcntl.LOCK_UN)
            held.close()

        self.assertLess(elapsed, 0.5)
        self.assertEqual(release.generation_id, "gen1")
        self.assertEqual(release.raw_dir, previous.raw_dir)

    def test_busy_updater_lock_without_release_fails_immediately(self) -> None:
        held = self.lock.open("rb")
        fcntl.flock(held.fileno(), fcntl.LOCK_EX)
        try:
            started = time.monotonic()
            with self.assertRaisesRegex(RuntimeError, "no immutable release covers"):
                self._pin("exp1")
            elapsed = time.monotonic() - started
        finally:
            fcntl.flock(held.fileno(), fcntl.LOCK_UN)
            held.close()
        self.assertLess(elapsed, 0.5)

    def test_stale_release_missing_required_datasets_is_not_selected(self) -> None:
        # A release materialized before a dataset existed must not satisfy an
        # experiment configured to read it (observed: lzp-test22 pinned a
        # pre-derivatives release during a nightly update).
        self._pin("seed")
        held = self.lock.open("rb")
        fcntl.flock(held.fileno(), fcntl.LOCK_EX)
        try:
            with self.assertRaisesRegex(RuntimeError, r"no immutable release covers.*fut_basic"):
                self._pin("exp1", required=("daily", "fut_basic"))
        finally:
            fcntl.flock(held.fileno(), fcntl.LOCK_UN)
            held.close()

    def test_pinned_stale_release_fails_actionably_on_resume(self) -> None:
        self._pin("exp1")
        with self.assertRaisesRegex(
            RuntimeError, r"pinned research release gen1 lacks configured raw datasets \['fut_basic'\]"
        ):
            self._pin("exp1", required=("fut_basic",))

    def test_fresh_release_with_required_datasets_pins_normally(self) -> None:
        self._write_raw_pair("fut_basic/exchange=CFFEX", b"registry-v1")
        release = self._pin("exp1", required=("daily", "fut_basic"))
        self.assertEqual(release.generation_id, "gen1")
        self.assertTrue((release.raw_dir / "fut_basic" / "exchange=CFFEX.parquet").exists())

    def test_empty_required_dataset_dir_is_rejected(self) -> None:
        # An interrupted backfill leaves the directory present but empty; that
        # must fail the pin-time contract like a missing directory.
        (self.raw / "opt_basic").mkdir()
        with self.assertRaisesRegex(RuntimeError, r"lacks configured raw datasets \['opt_basic'\]"):
            self._pin("exp1", required=("daily", "opt_basic"))

    def test_legacy_ledger_without_pin_is_rejected(self) -> None:
        ledger = self.root / "experiments" / "legacy" / "ledgers" / "experiment_ledger.jsonl"
        ledger.parent.mkdir(parents=True)
        ledger.write_text('{"record_type":"fold"}\n', encoding="utf-8")

        with self.assertRaisesRegex(RuntimeError, "ledger records but no research-release pin"):
            self._pin("legacy")

    def test_no_production_marker_or_lock_returns_live_paths(self) -> None:
        self.lock.unlink()
        (self.raw / ".raw_generation.json").unlink()
        release = self._pin("local")
        self.assertEqual(release.raw_dir, self.raw.resolve())
        self.assertEqual(release.fundamental_events_root, self.pit.resolve())
        self.assertFalse((self.root / "experiments" / "local" / "research_release").exists())

    def test_partial_production_contract_is_rejected(self) -> None:
        self.lock.unlink()
        with self.assertRaisesRegex(RuntimeError, "require both"):
            self._pin("misconfigured")

    def test_temp_symlink_and_unpaired_raw_are_rejected_without_publication(self) -> None:
        cases = ("temporary", "symlink", "unpaired")
        for case in cases:
            with self.subTest(case=case):
                # Use a fresh generation/experiment for every failure and clean
                # the prior invalid source before the next subtest.
                bad_paths: list[Path] = []
                if case == "temporary":
                    bad = self.raw / "daily" / "orphan.parquet.tmp"
                    bad.write_bytes(b"tmp")
                    bad_paths.append(bad)
                    pattern = "temporary"
                elif case == "symlink":
                    bad = self.raw / "daily" / "linked.json"
                    bad.symlink_to(self.raw / "reference.json")
                    bad_paths.append(bad)
                    pattern = "symbolic link"
                else:
                    bad = self.raw / "daily" / "unpaired.parquet"
                    bad.write_bytes(b"bad")
                    bad_paths.append(bad)
                    pattern = "pairing failed"
                with self.assertRaisesRegex(RuntimeError, pattern):
                    self._pin(f"bad-{case}")
                self.assertFalse((self.raw.parent / "research_releases" / "gen1").exists())
                for path in bad_paths:
                    path.unlink(missing_ok=True)

    def test_pin_quality_hash_detects_later_tampering(self) -> None:
        release = self._pin("exp1")
        release.fundamental_events_status.write_text("tampered\n", encoding="utf-8")

        with self.assertRaisesRegex(RuntimeError, "quality file hash mismatch"):
            self._pin("exp1")


if __name__ == "__main__":
    unittest.main()
