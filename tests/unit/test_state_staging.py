"""Host-side managed ctx.state_dir staging: ready_at merge timing + audit."""

import errno
import os
import tempfile
import unittest
from stat import S_IMODE
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from autotrade.environment.replay.state_staging import StateStager

CN_TZ = ZoneInfo("Asia/Shanghai")
T0 = datetime(2022, 1, 4, 9, 31, tzinfo=CN_TZ)


def _stager(tmp: Path) -> StateStager:
    return StateStager(visible_dir=tmp / ".state", staging_dir=tmp / ".state_staging")


def _stage_file(stager: StateStager, staging_rel: str, body: str) -> None:
    path = stager.staging_dir / staging_rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


class StateStagerTest(unittest.TestCase):
    def test_directories_are_reset_on_construction(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / ".state").mkdir()
            (tmp / ".state" / "stale.txt").write_text("old", encoding="utf-8")
            stager = _stager(tmp)
            self.assertFalse((stager.visible_dir / "stale.txt").exists())

    def test_state_directories_are_world_writable_for_docker_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            stager = _stager(Path(tmp))
            self.assertEqual(S_IMODE(stager.visible_dir.stat().st_mode), 0o777)
            self.assertEqual(S_IMODE(stager.staging_dir.stat().st_mode), 0o777)

    def test_write_not_visible_before_ready_then_merges(self):
        with tempfile.TemporaryDirectory() as tmp:
            stager = _stager(Path(tmp))
            _stage_file(stager, "t0/screen/plan.txt", "go")
            stager.register(
                [{"staging_rel": "t0/screen/plan.txt", "state_rel": "plan.txt", "substep": "screen", "budget_minutes": 10}],
                when=T0,
            )
            stager.merge_ready(T0 + timedelta(minutes=5))  # ready_at 09:41 not reached
            self.assertFalse((stager.visible_dir / "plan.txt").exists())
            merged = stager.merge_ready(T0 + timedelta(minutes=10))  # ready_at reached
            self.assertEqual(merged, 1)
            self.assertEqual((stager.visible_dir / "plan.txt").read_text(encoding="utf-8"), "go")

    def test_merge_handles_nested_state_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            stager = _stager(Path(tmp))
            _stage_file(stager, "t0/screen/sub/dir/p.txt", "x")
            stager.register(
                [{"staging_rel": "t0/screen/sub/dir/p.txt", "state_rel": "sub/dir/p.txt", "substep": "screen", "budget_minutes": 1}],
                when=T0,
            )
            stager.merge_ready(T0 + timedelta(minutes=2))
            self.assertTrue((stager.visible_dir / "sub" / "dir" / "p.txt").exists())

    def test_later_generated_write_wins_on_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            stager = _stager(Path(tmp))
            _stage_file(stager, "a/screen/plan.txt", "first")
            stager.register(
                [{"staging_rel": "a/screen/plan.txt", "state_rel": "plan.txt", "substep": "screen", "budget_minutes": 1}],
                when=T0,
            )
            _stage_file(stager, "b/refine/plan.txt", "second")
            stager.register(
                [{"staging_rel": "b/refine/plan.txt", "state_rel": "plan.txt", "substep": "refine", "budget_minutes": 1}],
                when=T0 + timedelta(minutes=1),
            )
            stager.merge_ready(T0 + timedelta(minutes=30))
            self.assertEqual((stager.visible_dir / "plan.txt").read_text(encoding="utf-8"), "second")

    def test_audit_reports_merged_and_unmerged(self):
        with tempfile.TemporaryDirectory() as tmp:
            stager = _stager(Path(tmp))
            _stage_file(stager, "t0/a/done.txt", "d")
            _stage_file(stager, "t0/b/late.txt", "l")
            stager.register(
                [
                    {"staging_rel": "t0/a/done.txt", "state_rel": "done.txt", "substep": "a", "budget_minutes": 1},
                    {"staging_rel": "t0/b/late.txt", "state_rel": "late.txt", "substep": "b", "budget_minutes": 600},
                ],
                when=T0,
            )
            stager.merge_ready(T0 + timedelta(minutes=5))  # only the 1-min write is ready
            audit = {r["state_rel"]: r for r in stager.audit()}
            self.assertTrue(audit["done.txt"]["merged"])
            self.assertEqual(audit["done.txt"]["status"], "merged")
            self.assertTrue(str(audit["done.txt"]["file_hash"]).startswith("sha256:"))
            self.assertFalse(audit["late.txt"]["merged"])
            self.assertEqual(audit["late.txt"]["status"], "unmerged_at_region_end")

    def test_path_escape_is_rejected_at_registration(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            stager = _stager(tmp)
            outside = tmp / "outside.txt"
            for rel in ("../outside.txt", "a/../../outside.txt"):
                with self.assertRaisesRegex(ValueError, "escapes"):
                    stager.register(
                        [{"staging_rel": "t0/s/x.txt", "state_rel": rel, "substep": "s", "budget_minutes": 1}],
                        when=T0,
                    )
            # Symlink escape: staging path resolving outside its root.
            link = stager.staging_dir / "t0" / "s"
            link.parent.mkdir(parents=True, exist_ok=True)
            outside.write_text("secret", encoding="utf-8")
            link.symlink_to(tmp)
            with self.assertRaisesRegex(ValueError, "escapes"):
                stager.register(
                    [{"staging_rel": "t0/s/outside.txt", "state_rel": "x.txt", "substep": "s", "budget_minutes": 1}],
                    when=T0,
                )
            self.assertEqual(stager.audit(), [])

    def test_non_regular_staging_file_is_rejected_at_merge(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            stager = _stager(tmp)
            _stage_file(stager, "t0/s/x.txt", "ok")
            stager.register(
                [{"staging_rel": "t0/s/x.txt", "state_rel": "x.txt", "substep": "s", "budget_minutes": 1}],
                when=T0,
            )
            # TOCTOU: swap the registered regular file for a symlink before ready_at.
            target = stager.staging_dir / "t0" / "s" / "x.txt"
            target.unlink()
            secret = tmp / "secret.txt"
            secret.write_text("secret", encoding="utf-8")
            target.symlink_to(secret)
            self.assertEqual(stager.merge_ready(T0 + timedelta(minutes=2)), 0)
            self.assertFalse((stager.visible_dir / "x.txt").exists())
            self.assertEqual(stager.audit()[0]["status"], "rejected_not_regular_file")

    def test_parent_directory_symlink_swap_is_rejected_at_merge(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            stager = _stager(tmp)
            _stage_file(stager, "parent/x.txt", "registered")
            stager.register(
                [{"staging_rel": "parent/x.txt", "state_rel": "x.txt", "substep": "s", "budget_minutes": 1}],
                when=T0,
            )
            original = stager.staging_dir / "original_parent"
            (stager.staging_dir / "parent").rename(original)
            outside = tmp / "outside"
            outside.mkdir()
            (outside / "x.txt").write_text("HOST_MARKER", encoding="utf-8")
            (stager.staging_dir / "parent").symlink_to(outside, target_is_directory=True)

            self.assertEqual(stager.merge_ready(T0 + timedelta(minutes=2)), 0)
            self.assertFalse((stager.visible_dir / "x.txt").exists())
            self.assertEqual((outside / "x.txt").read_text(encoding="utf-8"), "HOST_MARKER")
            self.assertEqual(stager.audit()[0]["status"], "rejected_not_regular_file")

    def test_visible_parent_symlink_is_not_followed(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            stager = _stager(tmp)
            _stage_file(stager, "source/x.txt", "safe")
            stager.register(
                [{"staging_rel": "source/x.txt", "state_rel": "parent/x.txt", "substep": "s", "budget_minutes": 1}],
                when=T0,
            )
            outside = tmp / "outside"
            outside.mkdir()
            (stager.visible_dir / "parent").symlink_to(outside, target_is_directory=True)

            self.assertEqual(stager.merge_ready(T0 + timedelta(minutes=2)), 0)
            self.assertFalse((outside / "x.txt").exists())
            self.assertEqual(stager.audit()[0]["status"], "rejected_not_regular_file")

    def test_host_storage_error_fails_fast(self):
        with tempfile.TemporaryDirectory() as tmp:
            stager = _stager(Path(tmp))
            _stage_file(stager, "source/x.txt", "safe")
            stager.register(
                [{"staging_rel": "source/x.txt", "state_rel": "x.txt", "substep": "s", "budget_minutes": 1}],
                when=T0,
            )
            with patch(
                "autotrade.environment.replay.state_staging._secure_merge_file",
                side_effect=OSError(errno.ENOSPC, "disk full"),
            ):
                with self.assertRaises(OSError) as raised:
                    stager.merge_ready(T0 + timedelta(minutes=2))
            self.assertEqual(raised.exception.errno, errno.ENOSPC)

    def test_source_open_is_nonblocking_and_file_size_is_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            stager = _stager(Path(tmp))
            _stage_file(stager, "source/x.txt", "safe")
            stager.register(
                [{"staging_rel": "source/x.txt", "state_rel": "x.txt", "substep": "s", "budget_minutes": 1}],
                when=T0,
            )
            with patch("autotrade.environment.replay.state_staging.os.open", wraps=os.open) as opened:
                stager.merge_ready(T0 + timedelta(minutes=2))
            source_calls = [call for call in opened.call_args_list if call.args[0] == "x.txt"]
            self.assertTrue(source_calls[0].args[1] & os.O_NONBLOCK)

        with tempfile.TemporaryDirectory() as tmp:
            stager = _stager(Path(tmp))
            _stage_file(stager, "source/x.txt", "four")
            stager.register(
                [{"staging_rel": "source/x.txt", "state_rel": "x.txt", "substep": "s", "budget_minutes": 1}],
                when=T0,
            )
            with patch("autotrade.environment.replay.state_staging._MAX_STATE_FILE_BYTES", 3):
                self.assertEqual(stager.merge_ready(T0 + timedelta(minutes=2)), 0)
            self.assertEqual(stager.audit()[0]["status"], "rejected_not_regular_file")

    def test_missing_staging_file_is_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            stager = _stager(Path(tmp))
            stager.register(
                [{"staging_rel": "t0/a/gone.txt", "state_rel": "gone.txt", "substep": "a", "budget_minutes": 1}],
                when=T0,
            )
            stager.merge_ready(T0 + timedelta(minutes=5))
            self.assertEqual(stager.audit()[0]["status"], "missing_staging_file")


if __name__ == "__main__":
    unittest.main()
