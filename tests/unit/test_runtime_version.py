"""Source-version stamps used by long-lived services and experiment workers."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from autotrade.environment.runtime import repo_code_version


class RepoCodeVersionTest(unittest.TestCase):
    def test_tracks_code_content_and_ignores_doc_only_changes(self) -> None:
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
            source = root / "src" / "service.py"
            source.parent.mkdir()
            source.write_text("VALUE = 1\n", encoding="utf-8")
            docs = root / "LOGBOOK.md"
            docs.write_text("entry 1\n", encoding="utf-8")
            git("add", ".gitignore", "src/service.py", "LOGBOOK.md")
            git("commit", "-qm", "initial")

            tree = git("rev-parse", "--short", "HEAD:src")
            self.assertEqual(repo_code_version(root), tree)

            # A commit touching only docs/logbooks does not change the code
            # identity: no stale flag for running services after such pushes.
            docs.write_text("entry 2\n", encoding="utf-8")
            self.assertEqual(repo_code_version(root), tree)
            git("add", "LOGBOOK.md")
            git("commit", "-qm", "logbook only")
            self.assertEqual(repo_code_version(root), tree)
            self.assertEqual(git("rev-parse", "--short", "HEAD:src"), tree)

            source.write_text("VALUE = 2\n", encoding="utf-8")
            tracked_v1 = repo_code_version(root)
            source.write_text("VALUE = 3\n", encoding="utf-8")
            tracked_v2 = repo_code_version(root)
            self.assertRegex(tracked_v1, rf"^{tree}\+dirty\.[0-9a-f]{{12}}$")
            self.assertNotEqual(tracked_v1, tracked_v2)

            source.write_text("VALUE = 1\n", encoding="utf-8")
            extra = root / "src" / "new_module.py"
            extra.write_text("VALUE = 4\n", encoding="utf-8")
            untracked_v1 = repo_code_version(root)
            extra.write_text("VALUE = 5\n", encoding="utf-8")
            self.assertNotEqual(untracked_v1, repo_code_version(root))

            extra.unlink()
            ignored = root / "src" / "ignored" / "cache.pyc"
            ignored.parent.mkdir()
            ignored.write_bytes(b"runtime cache")
            self.assertEqual(repo_code_version(root), tree)

    def test_returns_empty_outside_git_repository(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(repo_code_version(Path(tmp)), "")


if __name__ == "__main__":
    unittest.main()
