"""Source-version stamps used by long-lived services and experiment workers."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from autotrade.environment.runtime import repo_code_version


class RepoCodeVersionTest(unittest.TestCase):
    def test_tracks_dirty_and_untracked_contents_without_counting_ignored_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            def git(*args: str) -> str:
                result = subprocess.run(
                    ["git", *args], cwd=root, check=True, capture_output=True, text=True
                )
                return result.stdout.strip()

            git("init", "-q")
            git("config", "user.email", "runtime-version@example.invalid")
            git("config", "user.name", "Runtime Version Test")
            (root / ".gitignore").write_text("ignored/\n", encoding="utf-8")
            source = root / "service.py"
            source.write_text("VALUE = 1\n", encoding="utf-8")
            git("add", ".gitignore", "service.py")
            git("commit", "-qm", "initial")

            head = git("rev-parse", "--short", "HEAD")
            self.assertEqual(repo_code_version(root), head)

            source.write_text("VALUE = 2\n", encoding="utf-8")
            tracked_v1 = repo_code_version(root)
            source.write_text("VALUE = 3\n", encoding="utf-8")
            tracked_v2 = repo_code_version(root)
            self.assertRegex(tracked_v1, rf"^{head}\+dirty\.[0-9a-f]{{12}}$")
            self.assertNotEqual(tracked_v1, tracked_v2)

            source.write_text("VALUE = 1\n", encoding="utf-8")
            extra = root / "new_module.py"
            extra.write_text("VALUE = 4\n", encoding="utf-8")
            untracked_v1 = repo_code_version(root)
            extra.write_text("VALUE = 5\n", encoding="utf-8")
            self.assertNotEqual(untracked_v1, repo_code_version(root))

            extra.unlink()
            ignored = root / "ignored" / "cache.pyc"
            ignored.parent.mkdir()
            ignored.write_bytes(b"runtime cache")
            self.assertEqual(repo_code_version(root), head)

    def test_returns_empty_outside_git_repository(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(repo_code_version(Path(tmp)), "")


if __name__ == "__main__":
    unittest.main()
