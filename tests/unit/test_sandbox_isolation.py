import json
import os
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from hl_trader.agent import AgentSessionConfig, AgentSessionRunner
from hl_trader.environment.executor import DockerExecutor, LocalExecutor, docker_available
from hl_trader.environment.llm.proxy import ScriptedLLM
from hl_trader.environment.runtime import SandboxPaths
from hl_trader.environment.sandbox import DockerSandbox, LocalSandbox, SandboxSpec
from hl_trader.environment.web_search import (
    SemanticScholarSearchProvider,
    TavilySearchProvider,
    WebSearchError,
    WebSearchProvider,
    WebSearchResult,
)

from .fixtures_sandbox import TEMPLATE_DIR, write_strategy
from .test_tools_flow import build_sandbox


class FakeHTTPResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def __enter__(self) -> "FakeHTTPResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


class SandboxSpecTest(unittest.TestCase):
    def test_from_host_fraction_limits_to_share_of_host(self):
        spec = SandboxSpec.from_host_fraction(0.10)
        host_cpus = os.cpu_count() or 4
        self.assertGreaterEqual(spec.cpus, 1.0)
        self.assertLessEqual(spec.cpus, host_cpus * 0.10 + 1.0)
        self.assertTrue(spec.memory.endswith("g"))
        self.assertGreaterEqual(int(spec.memory[:-1]), 1)
        with self.assertRaises(ValueError):
            SandboxSpec.from_host_fraction(0.0)

    def test_gpu_count_must_be_positive(self):
        with self.assertRaises(ValueError):
            SandboxSpec(gpu_count=0)

    def test_auto_gpu_resolves_top_free_l20_devices(self):
        from hl_trader.environment.gpu import select_gpus

        fake = [
            {"index": 0, "name": "NVIDIA L20", "memory_free_mib": 1000, "memory_total_mib": 46000},
            {"index": 1, "name": "NVIDIA L20", "memory_free_mib": 9000, "memory_total_mib": 46000},
            {"index": 2, "name": "NVIDIA A100", "memory_free_mib": 20000, "memory_total_mib": 80000},
            {"index": 3, "name": "NVIDIA L20", "memory_free_mib": 5000, "memory_total_mib": 46000},
        ]
        with patch("hl_trader.environment.gpu.list_gpus", return_value=fake):
            self.assertEqual(select_gpus(2), [1, 3])
            self.assertEqual(select_gpus(1, require_name=None), [2])

    def test_docker_sandbox_can_resolve_fixed_gpu_lists_without_docker(self):
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = LocalSandbox(Path(tmp) / "mnt")
            spec = SandboxSpec(gpu=[2, 4])
            docker = DockerSandbox(sandbox, spec)
            self.assertEqual(docker._resolve_gpu_indices(), [2, 4])
            self.assertEqual(docker.allocation_record()["gpu_count"], 1)

    def test_docker_sandbox_mounts_agent_rw_and_artifacts_ro(self):
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = LocalSandbox(Path(tmp) / "mnt")
            paths = sandbox.prepare_layout()
            docker = DockerSandbox(sandbox, SandboxSpec(gpu=None))

            class Completed:
                returncode = 0
                stderr = ""

            with patch("subprocess.run", return_value=Completed()) as run:
                docker.start()
            command = run.call_args.args[0]
            self.assertIn(f"{paths.artifacts}:/mnt/artifacts:ro", command)
            self.assertIn(f"{paths.agent}:/mnt/agent:rw", command)
            self.assertIn(f"{paths.current_snapshot}:/mnt/snapshot:ro", command)
            self.assertNotIn("/mnt/runtime/snapshot_views", command)


class FilesystemPermissionTest(unittest.TestCase):
    def test_readonly_enforcement_and_locking(self):
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = LocalSandbox(Path(tmp) / "mnt")
            paths = sandbox.prepare_layout()
            parent_dir = Path(tmp) / "parent"
            from .test_artifacts import write_artifact

            write_artifact(parent_dir)
            is_initial = sandbox.install_strategy_artifact(parent_dir, TEMPLATE_DIR)
            self.assertFalse(is_initial)
            readme_mode = (paths.agent_output / "factor" / "README.md").stat().st_mode & 0o777
            self.assertEqual(readme_mode, 0o444)
            parent_main_mode = (paths.parent_output / "factor" / "main.py").stat().st_mode & 0o777
            self.assertEqual(parent_main_mode, 0o444)

            sandbox.lock_agent_output()
            main_py = paths.agent_output / "factor" / "main.py"
            self.assertEqual(main_py.stat().st_mode & 0o222, 0)
            with self.assertRaises(PermissionError):
                main_py.write_text("tamper", encoding="utf-8")
            sandbox.unlock_agent_output()
            main_py.write_text(main_py.read_text(encoding="utf-8"), encoding="utf-8")

    def test_test_slot_is_owner_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = LocalSandbox(Path(tmp) / "mnt")
            sandbox.prepare_layout()
            source = Path(tmp) / "slot"
            source.mkdir()
            (source / "daily.parquet").write_bytes(b"x")
            target = sandbox.install_replay_slot("test", source)
            self.assertEqual(target.stat().st_mode & 0o777, 0o700)

    def test_replay_slot_install_uses_hardlinks_when_possible(self):
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = LocalSandbox(Path(tmp) / "mnt")
            sandbox.prepare_layout()
            source = Path(tmp) / "slot"
            source.mkdir()
            payload = source / "daily.parquet"
            payload.write_bytes(b"x" * 128)
            target = sandbox.install_replay_slot("train", source)
            self.assertEqual((target / "daily.parquet").stat().st_ino, payload.stat().st_ino)

    def test_snapshot_binding_refreshes_current_snapshot_mirror(self):
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = LocalSandbox(Path(tmp) / "mnt")
            paths = sandbox.prepare_layout()
            valid_view = paths.snapshot_views / "valid_decision_input"
            test_view = paths.snapshot_views / "test_decision_input"
            valid_view.mkdir()
            test_view.mkdir()
            (valid_view / "manifest.json").write_text('{"slot":"valid"}', encoding="utf-8")
            (test_view / "manifest.json").write_text('{"slot":"test"}', encoding="utf-8")

            sandbox.bind_snapshot_view(valid_view)
            self.assertTrue(paths.snapshot.is_symlink())
            self.assertEqual((paths.snapshot / "manifest.json").read_text(encoding="utf-8"), '{"slot":"valid"}')
            self.assertEqual((paths.current_snapshot / "manifest.json").read_text(encoding="utf-8"), '{"slot":"valid"}')

            sandbox.bind_snapshot_view(test_view)
            self.assertEqual((paths.snapshot / "manifest.json").read_text(encoding="utf-8"), '{"slot":"test"}')
            self.assertEqual((paths.current_snapshot / "manifest.json").read_text(encoding="utf-8"), '{"slot":"test"}')


class MetaLearningSessionTest(unittest.TestCase):
    def test_meta_learning_mode_blocks_backtests_and_ends_with_done(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            write_strategy(ctx.paths.agent_output)
            ctx.extra["allow_backtest"] = False
            responses = [
                json.dumps({"action": "backtest", "nl_mode": "on"}),
                json.dumps({"action": "modification_check"}),
                json.dumps({"action": "done"}),
            ]
            proxy = ScriptedLLM(responses)
            runner = AgentSessionRunner(
                ctx,
                proxy,
                AgentSessionConfig(fold_deadline_at=datetime.now(timezone.utc) + timedelta(minutes=5)),
                fold_info={"development_history": "workspace/development_history.json"},
                acceptance_rules={},
                mode="meta_learning",
            )
            summary = runner.run()
            self.assertEqual(summary["finish_status"], "meta_learning_done")
            events = ctx.trace.read_events()
            backtest_errors = [
                e for e in events if e["event_type"] == "llm_call"
            ]
            self.assertGreaterEqual(len(backtest_errors), 3)

    def test_meta_learning_web_search_action_is_traced(self):
        class FakeSearch(WebSearchProvider):
            provider = "fake"

            def search(self, query: str, *, max_results: int = 5, category: str = "general"):
                return [WebSearchResult(title=f"{category}:{query}", url="https://example.test", content="snippet")]

        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            responses = [
                json.dumps({"action": "web_search", "category": "finance", "query": "walk forward", "max_results": 1}),
                json.dumps({"action": "done"}),
            ]
            runner = AgentSessionRunner(
                ctx,
                ScriptedLLM(responses),
                AgentSessionConfig(fold_deadline_at=datetime.now(timezone.utc) + timedelta(minutes=5)),
                fold_info={"development_history": "workspace/development_history.json"},
                acceptance_rules={},
                mode="meta_learning",
                web_search_provider=FakeSearch(),
            )
            summary = runner.run()
            self.assertEqual(summary["finish_status"], "meta_learning_done")
            searches = [event for event in ctx.trace.read_events() if event["event_type"] == "web_search"]
            self.assertEqual(len(searches), 1)
            self.assertEqual(searches[0]["provider"], "fake")

    def test_tavily_http_errors_redact_api_key(self):
        key = "tvly-dev-secret-for-test"
        error = urllib.error.HTTPError(
            "https://api.tavily.com/search",
            401,
            "Unauthorized",
            hdrs=None,
            fp=BytesIO(f"bad key {key}".encode("utf-8")),
        )
        with patch("urllib.request.urlopen", side_effect=error):
            provider = TavilySearchProvider(key)
            with self.assertRaises(WebSearchError) as caught:
                provider.search("query", max_results=1)
        self.assertNotIn(key, str(caught.exception))
        self.assertIn("[redacted]", str(caught.exception))

    def test_semantic_scholar_search_parses_papers(self):
        payload = {
            "data": [
                {
                    "paperId": "paper-1",
                    "title": "Walk-Forward Optimization in Finance",
                    "abstract": "A paper about robust evaluation.",
                    "year": 2024,
                    "venue": "Journal",
                    "citationCount": 12,
                    "authors": [{"name": "A. Author"}],
                }
            ]
        }
        with patch("urllib.request.urlopen", return_value=FakeHTTPResponse(payload)):
            results = SemanticScholarSearchProvider("s2-secret", min_interval_seconds=0).search(
                "walk forward finance", max_results=3
            )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].title, "Walk-Forward Optimization in Finance")
        self.assertIn("paper-1", results[0].url)
        self.assertIn("citations=12", results[0].content)
        self.assertIn("A. Author", results[0].content)

    def test_semantic_scholar_http_errors_redact_api_key(self):
        key = "s2-secret-for-test"
        error = urllib.error.HTTPError(
            "https://api.semanticscholar.org/graph/v1/paper/search",
            429,
            "Too Many Requests",
            hdrs=None,
            fp=BytesIO(f"rate limited key {key}".encode("utf-8")),
        )
        with patch("urllib.request.urlopen", side_effect=error):
            provider = SemanticScholarSearchProvider(key, min_interval_seconds=0)
            with self.assertRaises(WebSearchError) as caught:
                provider.search("query", max_results=1)
        self.assertNotIn(key, str(caught.exception))
        self.assertIn("[redacted]", str(caught.exception))


class ExecutorTest(unittest.TestCase):
    def test_local_executor_runs_and_maps_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = SandboxPaths(Path(tmp))
            paths.workspace.mkdir(parents=True)
            executor = LocalExecutor(paths)
            result = executor.run(["/bin/echo", "hi"], timeout_seconds=10)
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(executor.map_path(Path(tmp) / "x"), str(Path(tmp) / "x"))

    def test_local_executor_tolerates_binary_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = SandboxPaths(Path(tmp))
            paths.workspace.mkdir(parents=True)
            executor = LocalExecutor(paths)
            result = executor.run(["/bin/bash", "-c", "printf '\\xac\\xed binary'"], timeout_seconds=10)
            self.assertEqual(result.exit_code, 0)
            self.assertIn("binary", result.stdout)  # invalid bytes replaced, no crash

    def test_docker_executor_maps_sandbox_paths_to_mnt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "mnt"
            paths = SandboxPaths(root)
            (root / "artifacts" / "workspace").mkdir(parents=True)
            executor = DockerExecutor("dummy", paths)
            mapped = executor.map_path(paths.workspace)
            self.assertEqual(mapped, "/mnt/agent/workspace")
            self.assertEqual(executor.map_path(paths.agent_output), "/mnt/agent/agent_output")
            self.assertEqual(executor.map_path(paths.run_manifest), "/mnt/artifacts/run_manifest.json")
            with self.assertRaises(Exception):
                executor.map_path(Path("/etc/passwd"))


@unittest.skipUnless(docker_available(), "docker daemon not accessible for the current user")
class DockerSandboxE2ETest(unittest.TestCase):
    """Runs only where the docker socket is reachable and the image exists."""

    def test_container_lifecycle_and_agent_exec(self):
        import subprocess

        image_check = subprocess.run(
            ["docker", "image", "inspect", SandboxSpec().image], capture_output=True, timeout=30
        )
        if image_check.returncode != 0:
            self.skipTest(f"sandbox image not built: {SandboxSpec().image}")
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = LocalSandbox(Path(tmp) / "mnt")
            paths = sandbox.prepare_layout()
            docker = DockerSandbox(sandbox, SandboxSpec.from_host_fraction(0.05))
            docker.start()
            try:
                view = paths.snapshot_views / "valid_decision_input"
                view.mkdir(parents=True)
                (view / "manifest.json").write_text('{"slot":"valid"}', encoding="utf-8")
                docker.bind_snapshot_view("valid_decision_input")
                executor = DockerExecutor(docker.container, paths)
                result = executor.run(["/bin/sh", "-c", "id -un && touch /mnt/agent/workspace/ok"], timeout_seconds=30)
                self.assertEqual(result.exit_code, 0)
                self.assertIn("agent", result.stdout)
                self.assertTrue((paths.workspace / "ok").exists())
                current = executor.run(["/bin/sh", "-c", "cat /mnt/snapshot/manifest.json"], timeout_seconds=30)
                self.assertEqual(current.exit_code, 0)
                self.assertIn('"valid"', current.stdout)
                hidden_views = executor.run(["/bin/sh", "-c", "ls /mnt/runtime/snapshot_views"], timeout_seconds=30)
                self.assertNotEqual(hidden_views.exit_code, 0)
                blocked_artifacts = executor.run(["/bin/sh", "-c", "touch /mnt/artifacts/steps/agent_write"], timeout_seconds=30)
                self.assertNotEqual(blocked_artifacts.exit_code, 0)
                blocked = executor.run(["/bin/sh", "-c", "ls /mnt/snapshots/test"], timeout_seconds=30)
                self.assertNotEqual(blocked.exit_code, 0)  # 0700 test slot is unreadable for agent
            finally:
                docker.stop()


if __name__ == "__main__":
    unittest.main()
