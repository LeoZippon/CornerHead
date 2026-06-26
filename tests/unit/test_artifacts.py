import shutil
import tempfile
import unittest
from pathlib import Path

from autotrade.environment.artifacts import (
    ArtifactError,
    ModificationConstraints,
    artifact_hash,
    copy_artifact,
    copy_model_artifacts,
    init_from_template,
    load_model_artifacts,
    load_strategy_artifact,
    modification_delta,
    model_artifact_delta,
    model_artifact_hash,
)

TEMPLATE_DIR = Path(__file__).resolve().parents[2] / "configs" / "agent_output_template"

VALID_MAIN = """
def main(ctx):
    return None
"""


def write_artifact(root: Path, *, main: str = VALID_MAIN) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "README.md").write_text("readonly", encoding="utf-8")
    (root / "main.py").write_text(main, encoding="utf-8")
    (root / "candidate.py").write_text("def select_candidates(context):\n    return []\n", encoding="utf-8")
    (root / "trading.py").write_text("def build_trades(context, candidates):\n    return []\n", encoding="utf-8")
    (root / "nl_prompt.md").write_text("neutral when evidence is thin\n", encoding="utf-8")
    return root


class ArtifactContractTest(unittest.TestCase):
    def test_loads_valid_artifact_directory_and_hashes_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = write_artifact(Path(tmp))
            artifact = load_strategy_artifact(root)
            self.assertIn("main.py", artifact.files)
            self.assertTrue(artifact.artifact_hash.startswith("sha256:"))
            self.assertEqual(artifact.artifact_hash, artifact_hash(root))

    def test_rejects_missing_entrypoint_and_forbidden_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = write_artifact(Path(tmp), main="x = 1\n")
            with self.assertRaisesRegex(ArtifactError, "must define"):
                load_strategy_artifact(root)

        with tempfile.TemporaryDirectory() as tmp:
            root = write_artifact(Path(tmp), main="def run_strategy(context):\n    return {}\n")
            with self.assertRaisesRegex(ArtifactError, "main"):
                load_strategy_artifact(root)

        with tempfile.TemporaryDirectory() as tmp:
            root = write_artifact(Path(tmp), main='def main(ctx):\n    return open("/mnt/artifacts/x").read()\n')
            with self.assertRaisesRegex(ArtifactError, "stage directories"):
                load_strategy_artifact(root)

    def test_forbidden_path_scan_ignores_docstrings(self):
        main = '''
"""Documentation may mention /mnt/artifacts without becoming executable access."""


def helper():
    """Function docs may mention /mnt/snapshots/ for user guidance."""
    return None


def main(ctx):
    helper()
    return None
'''
        with tempfile.TemporaryDirectory() as tmp:
            root = write_artifact(Path(tmp), main=main)
            artifact = load_strategy_artifact(root)
            self.assertIn("main.py", artifact.files)

    def test_init_from_template_skips_runtime_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            template = Path(tmp) / "template"
            shutil.copytree(TEMPLATE_DIR, template)
            cache_dir = template / "__pycache__"
            cache_dir.mkdir()
            (cache_dir / "x.pyc").write_bytes(b"x")
            dest = Path(tmp) / "dest"
            init_from_template(template, dest)
            self.assertTrue((dest / "main.py").exists())
            self.assertFalse((dest / "__pycache__").exists())
            load_strategy_artifact(dest)

    def test_allows_subdirectories_and_rejects_unsupported_files_cache_and_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = write_artifact(Path(tmp))
            helper_dir = root / "helpers"
            helper_dir.mkdir()
            (helper_dir / "signals.py").write_text("def score(context):\n    return 0.0\n", encoding="utf-8")
            artifact = load_strategy_artifact(root)
            self.assertIn("helpers/signals.py", artifact.files)

        with tempfile.TemporaryDirectory() as tmp:
            root = write_artifact(Path(tmp))
            (root / "lookup.csv").write_text("ts_code,score\n000001.SZ,1\n", encoding="utf-8")
            with self.assertRaisesRegex(ArtifactError, "unsupported"):
                load_strategy_artifact(root)

        with tempfile.TemporaryDirectory() as tmp:
            root = write_artifact(Path(tmp))
            (root / "helpers").mkdir()
            (root / "helpers" / "bad.py").write_text(
                'def leak():\n    return open("/mnt/artifacts/x").read()\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ArtifactError, "stage directories"):
                load_strategy_artifact(root)

        with tempfile.TemporaryDirectory() as tmp:
            root = write_artifact(Path(tmp))
            cache = root / "__pycache__"
            cache.mkdir()
            (cache / "x.pyc").write_bytes(b"x")
            with self.assertRaisesRegex(ArtifactError, "runtime cache"):
                load_strategy_artifact(root)

        if hasattr(Path, "symlink_to"):
            with tempfile.TemporaryDirectory() as tmp:
                root = write_artifact(Path(tmp))
                (root / "linked.py").symlink_to(root / "main.py")
                with self.assertRaisesRegex(ArtifactError, "symlinks"):
                    load_strategy_artifact(root)

    def test_modification_delta_counts_files_and_code_lines_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = write_artifact(Path(tmp) / "parent")
            work = Path(tmp) / "work"
            copy_artifact(parent, work)
            (work / "main.py").write_text(VALID_MAIN + "\n# new condition\n", encoding="utf-8")
            (work / "nl_prompt.md").write_text("short prompt\nwith detail\n", encoding="utf-8")

            delta = modification_delta(parent, work)
            self.assertEqual(set(delta.changed_files), {"main.py", "nl_prompt.md"})
            self.assertGreaterEqual(delta.diff_lines, 2)
            self.assertGreaterEqual(delta.code_diff_lines, 1)
            self.assertEqual(delta.total_files, 5)

    def test_constraints_ignore_factor_prior_counts_and_tighten_after_early_epochs(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = write_artifact(Path(tmp) / "parent")
            work = Path(tmp) / "work"
            copy_artifact(parent, work)
            (work / "README.md").write_text("tampered", encoding="utf-8")
            delta = modification_delta(parent, work)
            allowed, reasons = ModificationConstraints().evaluate(delta)
            self.assertFalse(allowed)
            self.assertTrue(any("readonly" in reason for reason in reasons))

            loose = ModificationConstraints(max_diff_lines=1, early_max_diff_lines=100).for_epoch(1)
            strict = ModificationConstraints(max_diff_lines=1, early_max_diff_lines=100).for_epoch(3)
            self.assertEqual(loose.max_diff_lines, 100)
            self.assertEqual(strict.max_diff_lines, 1)

    def test_model_artifacts_are_separate_hashable_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "models"
            (root / "ranker").mkdir(parents=True)
            (root / "params.json").write_text('{"alpha": 1}\n', encoding="utf-8")
            (root / "ranker" / "weights.pt").write_bytes(b"weights")

            artifact = load_model_artifacts(root)

            self.assertEqual(set(artifact.files), {"params.json", "ranker/weights.pt"})
            self.assertEqual(artifact.artifact_hash, model_artifact_hash(root))
            self.assertGreater(artifact.total_bytes, 0)

            dest = Path(tmp) / "copied"
            copy_model_artifacts(root, dest)
            self.assertEqual(model_artifact_hash(dest), artifact.artifact_hash)

            delta = model_artifact_delta(Path(tmp) / "empty_parent", dest)
            self.assertEqual(delta.total_files, 2)
            self.assertEqual(set(delta.changed_files), {"params.json", "ranker/weights.pt"})

    def test_model_artifacts_reject_hidden_cache_and_unsupported_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "models"
            root.mkdir()
            (root / "data.parquet").write_bytes(b"not a model")
            with self.assertRaisesRegex(ArtifactError, "unsupported"):
                load_model_artifacts(root)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "models"
            root.mkdir()
            (root / "helper.py").write_text("VALUE = 1\n", encoding="utf-8")
            with self.assertRaisesRegex(ArtifactError, "unsupported"):
                load_model_artifacts(root)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "models"
            root.mkdir()
            (root / ".secret.json").write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(ArtifactError, "hidden"):
                load_model_artifacts(root)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "models"
            hidden_dir = root / ".hidden"
            hidden_dir.mkdir(parents=True)
            (hidden_dir / "params.json").write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(ArtifactError, "hidden"):
                load_model_artifacts(root)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "models"
            cache = root / "nested" / "__pycache__"
            cache.mkdir(parents=True)
            (cache / "x.pyc").write_bytes(b"x")
            with self.assertRaisesRegex(ArtifactError, "runtime cache"):
                load_model_artifacts(root)


if __name__ == "__main__":
    unittest.main()
