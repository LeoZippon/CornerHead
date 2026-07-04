import json
import os
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from autotrade.agent import AgentSessionConfig, AgentSessionRunner
from autotrade.environment.executor import DockerExecutor, LocalExecutor, docker_available
from autotrade.environment.llm.proxy import ScriptedLLM, tool_call, tool_call_response
from autotrade.environment.runtime import SandboxPaths
from autotrade.environment.sandbox import DockerSandbox, LocalSandbox, SandboxSpec
from autotrade.environment.web_search import (
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
        from autotrade.environment.gpu import select_gpus

        fake = [
            {"index": 0, "name": "NVIDIA L20", "memory_free_mib": 1000, "memory_total_mib": 46000},
            {"index": 1, "name": "NVIDIA L20", "memory_free_mib": 9000, "memory_total_mib": 46000},
            {"index": 2, "name": "NVIDIA A100", "memory_free_mib": 20000, "memory_total_mib": 80000},
            {"index": 3, "name": "NVIDIA L20", "memory_free_mib": 5000, "memory_total_mib": 46000},
        ]
        with patch("autotrade.environment.gpu.list_gpus", return_value=fake):
            self.assertEqual(select_gpus(2), [1, 3])
            self.assertEqual(select_gpus(1, require_name=None), [2])

    def test_docker_sandbox_can_resolve_fixed_gpu_lists_without_docker(self):
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = LocalSandbox(Path(tmp) / "mnt")
            spec = SandboxSpec(gpu=[2, 4])
            docker = DockerSandbox(sandbox, spec)
            self.assertEqual(docker._resolve_gpu_indices(), [2, 4])
            self.assertEqual(docker.allocation_record()["gpu_count"], 1)

    def test_docker_sandbox_quotes_multi_gpu_device_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = LocalSandbox(Path(tmp) / "mnt")
            sandbox.prepare_layout()
            docker = DockerSandbox(sandbox, SandboxSpec(gpu=[5, 6, 7], gpu_count=3))

            class Completed:
                returncode = 0
                stderr = ""

            with patch("subprocess.run", return_value=Completed()) as run:
                docker.start()
            command = run.call_args.args[0]
            self.assertIn("--gpus", command)
            self.assertIn('"device=5,6,7"', command)

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

    def test_docker_sandbox_redirects_tool_caches_out_of_workspace(self):
        # pip/HF/torch/CUDA caches must land in the ephemeral /tmp, never under
        # /mnt/agent (the collected workspace), so collect_artifacts never meets a
        # root-owned cache dir.
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = LocalSandbox(Path(tmp) / "mnt")
            sandbox.prepare_layout()
            docker = DockerSandbox(sandbox, SandboxSpec(gpu=None))

            class Completed:
                returncode = 0
                stderr = ""

            with patch("subprocess.run", return_value=Completed()) as run:
                docker.start()
            command = run.call_args.args[0]
            for key, value in (("HF_HOME", "/tmp/sandbox-cache/hf"),
                               ("PIP_CACHE_DIR", "/tmp/sandbox-cache/pip"),
                               ("CUDA_CACHE_PATH", "/tmp/sandbox-cache/cuda"),
                               ("XDG_CACHE_HOME", "/tmp/sandbox-cache")):
                self.assertIn(f"{key}={value}", command)
                self.assertFalse(value.startswith("/mnt/agent"))

    def test_docker_sandbox_disables_core_dumps(self):
        # A crashing GPU/training child must not write a multi-GB, subuid-owned core
        # into the workspace that the host collector cannot read.
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = LocalSandbox(Path(tmp) / "mnt")
            sandbox.prepare_layout()
            docker = DockerSandbox(sandbox, SandboxSpec(gpu=None))

            class Completed:
                returncode = 0
                stderr = ""

            with patch("subprocess.run", return_value=Completed()) as run:
                docker.start()
            command = run.call_args.args[0]
            ui = command.index("--ulimit")
            self.assertEqual(command[ui : ui + 2], ["--ulimit", "core=0:0"])

    def test_docker_sandbox_env_passthrough_records_names_without_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = LocalSandbox(Path(tmp) / "mnt")
            sandbox.prepare_layout()
            spec = SandboxSpec(
                gpu=None,
                network="bridge",
                env_passthrough=("HF_TOKEN", "GITHUB_TOKEN", "MISSING_SECRET"),
                add_host_gateway=True,
            )
            docker = DockerSandbox(sandbox, spec)

            class Completed:
                returncode = 0
                stderr = ""

            with patch.dict(os.environ, {"HF_TOKEN": "hf-secret-for-test", "GITHUB_TOKEN": "gh-secret-for-test"}):
                with patch("subprocess.run", return_value=Completed()) as run:
                    docker.start()
            command = run.call_args.args[0]
            self.assertIn("--network=bridge", command)
            self.assertIn("--add-host", command)
            self.assertIn("host.docker.internal:host-gateway", command)
            self.assertIn("HF_TOKEN", command)
            self.assertIn("GITHUB_TOKEN", command)
            self.assertNotIn("MISSING_SECRET", command)
            self.assertNotIn("hf-secret-for-test", command)
            self.assertNotIn("gh-secret-for-test", command)
            allocation = docker.allocation_record()
            self.assertEqual(allocation["env_passthrough"], ["HF_TOKEN", "GITHUB_TOKEN", "MISSING_SECRET"])
            self.assertEqual(allocation["requested_env_passthrough"], ["HF_TOKEN", "GITHUB_TOKEN", "MISSING_SECRET"])
            self.assertEqual(allocation["active_env_passthrough"], ["HF_TOKEN", "GITHUB_TOKEN"])
            self.assertTrue(allocation["add_host_gateway"])

    def test_docker_sandbox_proxy_aliases_are_not_active_standard_envs(self):
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = LocalSandbox(Path(tmp) / "mnt")
            sandbox.prepare_layout()
            spec = SandboxSpec(
                gpu=None,
                network="bridge",
                env_aliases=(
                    ("AT_PROXY_HTTPS", "HTTPS_PROXY"),
                    ("AT_PROXY_ALL", "ALL_PROXY"),
                    ("AT_PROXY_MISSING", "MISSING_PROXY"),
                ),
                add_host_gateway=True,
            )
            docker = DockerSandbox(sandbox, spec)

            class Completed:
                returncode = 0
                stderr = ""

            with patch.dict(
                os.environ,
                {
                    "HTTPS_PROXY": "http://127.0.0.1:7890",
                    "ALL_PROXY": "socks5h://localhost:1080",
                },
            ):
                with patch("subprocess.run", return_value=Completed()) as run:
                    docker.start()
            command = run.call_args.args[0]
            run_env = run.call_args.kwargs["env"]
            self.assertIn("AT_PROXY_HTTPS", command)
            self.assertIn("AT_PROXY_ALL", command)
            self.assertNotIn("HTTPS_PROXY", command)
            self.assertNotIn("ALL_PROXY", command)
            self.assertNotIn("AT_PROXY_MISSING", command)
            self.assertNotIn("127.0.0.1:7890", command)
            self.assertNotIn("localhost:1080", command)
            self.assertEqual(run_env["AT_PROXY_HTTPS"], "http://host.docker.internal:7890")
            self.assertEqual(run_env["AT_PROXY_ALL"], "socks5h://host.docker.internal:1080")
            allocation = docker.allocation_record()
            self.assertEqual(
                allocation["requested_env_aliases"],
                [
                    {"container_env": "AT_PROXY_HTTPS", "host_env": "HTTPS_PROXY"},
                    {"container_env": "AT_PROXY_ALL", "host_env": "ALL_PROXY"},
                    {"container_env": "AT_PROXY_MISSING", "host_env": "MISSING_PROXY"},
                ],
            )
            self.assertEqual(
                allocation["active_env_aliases"],
                [
                    {"container_env": "AT_PROXY_HTTPS", "host_env": "HTTPS_PROXY"},
                    {"container_env": "AT_PROXY_ALL", "host_env": "ALL_PROXY"},
                ],
            )
            self.assertEqual(allocation["env_aliases"], allocation["requested_env_aliases"])

    def test_docker_sandbox_can_pin_host_gateway_ip(self):
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = LocalSandbox(Path(tmp) / "mnt")
            sandbox.prepare_layout()
            docker = DockerSandbox(
                sandbox,
                SandboxSpec(gpu=None, network="bridge", add_host_gateway=True, host_gateway_ip="10.10.0.1"),
            )

            class Completed:
                returncode = 0
                stderr = ""

            with patch("subprocess.run", return_value=Completed()) as run:
                docker.start()
            command = run.call_args.args[0]
            self.assertIn("--add-host", command)
            self.assertIn("host.docker.internal:10.10.0.1", command)
            self.assertNotIn("host.docker.internal:host-gateway", command)

    def test_runtime_env_artifact_records_local_and_docker_contracts(self):
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = LocalSandbox(Path(tmp) / "mnt")
            paths = sandbox.prepare_layout()

            local = json.loads(paths.runtime_env.read_text(encoding="utf-8"))
            self.assertEqual(local["mode"], "local")
            self.assertEqual(local["schema_version"], 1)
            self.assertIn("python", local)
            self.assertIn("python_packages", local)
            self.assertIn("numpy", local["python_packages"])

            sandbox.write_runtime_env(mode="docker", sandbox_spec=SandboxSpec(gpu=None))
            docker = json.loads(paths.runtime_env.read_text(encoding="utf-8"))
            self.assertEqual(docker["mode"], "docker")
            self.assertEqual(docker["network"], "none")
            self.assertEqual(docker["python"]["version"], "3.11")
            self.assertEqual(docker["schema_version"], 1)
            self.assertEqual(docker["python_packages"]["pandas"]["version"], "2.2.3")
            self.assertIn("git", docker["tools"])
            self.assertIn("hf", docker["tools"])
            self.assertFalse(docker["policy"]["install_packages_during_fold"])


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
            readme_mode = (paths.agent_output / "README.md").stat().st_mode & 0o777
            self.assertEqual(readme_mode, 0o444)
            parent_main_mode = (paths.parent_output / "main.py").stat().st_mode & 0o777
            self.assertEqual(parent_main_mode, 0o444)

            sandbox.lock_agent_output()
            main_py = paths.agent_output / "main.py"
            self.assertEqual(main_py.stat().st_mode & 0o222, 0)
            with self.assertRaises(PermissionError):
                main_py.write_text("tamper", encoding="utf-8")
            sandbox.unlock_agent_output()
            main_py.write_text(main_py.read_text(encoding="utf-8"), encoding="utf-8")

    def test_collect_artifacts_excludes_transient_caches(self):
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = LocalSandbox(Path(tmp) / "mnt")
            paths = sandbox.prepare_layout()
            paths.workspace.mkdir(parents=True, exist_ok=True)
            (paths.workspace / "note.txt").write_text("keep me", encoding="utf-8")
            cache = paths.workspace / ".cache" / "pip" / "wheels"
            cache.mkdir(parents=True)
            (cache / "selfcheck.json").write_text("{}", encoding="utf-8")
            pycache = paths.workspace / "__pycache__"
            pycache.mkdir()
            (pycache / "mod.pyc").write_text("x", encoding="utf-8")
            nv_cache = paths.workspace / ".nv"
            nv_cache.mkdir()
            (nv_cache / "ComputeCache").mkdir()
            # PID-suffixed core dumps are junk and excluded; a legitimately named
            # file like core.py (or a core/ package) must NOT be false-matched.
            (paths.workspace / "core.7194").write_text("coredump", encoding="utf-8")
            (paths.workspace / "core.py").write_text("x = 1\n", encoding="utf-8")

            dest = Path(tmp) / "collected"
            sandbox.collect_artifacts(dest)

            self.assertTrue((dest / "workspace" / "note.txt").exists())
            self.assertFalse((dest / "workspace" / ".cache").exists())
            self.assertFalse((dest / "workspace" / ".nv").exists())
            self.assertFalse((dest / "workspace" / "__pycache__").exists())
            self.assertFalse((dest / "workspace" / "core.7194").exists())
            self.assertTrue((dest / "workspace" / "core.py").exists())

    def test_collect_artifacts_is_order_safe_when_workspace_is_uncollectable(self):
        # The frozen artifacts (output/, models/) are collected before the
        # adversarial agent workspace, so an uncollectable workspace file (e.g. a
        # subuid-owned core dump) can never pre-empt them; the failure is recorded
        # in a marker file rather than aborting the whole collection.
        import shutil as _shutil

        from autotrade.environment import sandbox as sandbox_mod

        with tempfile.TemporaryDirectory() as tmp:
            sandbox = LocalSandbox(Path(tmp) / "mnt")
            paths = sandbox.prepare_layout()
            (paths.agent_output / "main.py").write_text("x = 1\n", encoding="utf-8")
            (paths.model_artifacts / "model.json").write_text("{}", encoding="utf-8")
            (paths.workspace / "note.txt").write_text("keep", encoding="utf-8")

            real_copy = sandbox_mod._copy_path

            def flaky_copy(source, dest):
                if Path(source).name == "workspace":
                    raise _shutil.Error("simulated unreadable workspace file")
                return real_copy(source, dest)

            dest = Path(tmp) / "collected"
            with patch("autotrade.environment.sandbox._copy_path", side_effect=flaky_copy):
                sandbox.collect_artifacts(dest)  # must not raise

            self.assertTrue((dest / "output" / "main.py").exists())
            self.assertTrue((dest / "models" / "model.json").exists())
            self.assertFalse((dest / "workspace").exists())
            marker = dest / "workspace.collect_error.txt"
            self.assertTrue(marker.exists())
            self.assertIn("simulated unreadable workspace file", marker.read_text(encoding="utf-8"))

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
            (ctx.paths.workspace / "taste.md").write_text("prefer robust meta-learning", encoding="utf-8")
            ctx.extra["allow_backtest"] = False
            responses = [
                tool_call_response(tool_call("backtest")),
                tool_call_response(tool_call("modification_check")),
                tool_call_response(tool_call("done")),
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
            def __init__(self, provider: str) -> None:
                self.provider = provider

            def search(self, query: str, *, max_results: int = 5, category: str = "general"):
                return [WebSearchResult(title=f"{self.provider}:{category}:{query}", url="https://example.test", content="snippet")]

        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            (ctx.paths.workspace / "taste.md").write_text("searched taste", encoding="utf-8")
            responses = [
                tool_call_response(
                    tool_call(
                        "web_search",
                        engine="tavily",
                        perspective="finance_quant_econ",
                        query="walk forward",
                        max_results=1,
                    )
                ),
                tool_call_response(
                    tool_call(
                        "web_search",
                        engine="semantic_scholar",
                        perspective="natural_science_engineering",
                        query="walk forward optimization finance",
                        max_results=1,
                    )
                ),
                tool_call_response(
                    tool_call(
                        "web_search",
                        engine="tavily",
                        perspective="philosophy_methodology",
                        query="falsification robust trading strategy",
                        max_results=1,
                    )
                ),
                tool_call_response(tool_call("done")),
            ]
            runner = AgentSessionRunner(
                ctx,
                ScriptedLLM(responses),
                AgentSessionConfig(fold_deadline_at=datetime.now(timezone.utc) + timedelta(minutes=5)),
                fold_info={"development_history": "workspace/development_history.json"},
                acceptance_rules={},
                mode="meta_learning",
                web_search_providers={
                    "tavily": FakeSearch("tavily"),
                    "semantic_scholar": FakeSearch("semantic_scholar"),
                },
            )
            self.assertNotIn("# Web Search Engines", runner.system_prompt)
            self.assertNotIn("# development 摘要", runner.system_prompt)
            summary = runner.run()
            self.assertEqual(summary["finish_status"], "meta_learning_done")
            searches = [event for event in ctx.trace.read_events() if event["event_type"] == "web_search"]
            self.assertEqual(len(searches), 3)
            self.assertEqual(searches[0]["engine"], "tavily")
            self.assertEqual(searches[0]["perspective"], "finance_quant_econ")
            self.assertEqual(searches[0]["tool_spec"]["schema_version"], 1)
            self.assertEqual(searches[0]["tool_spec"]["result_policy"], "bounded_by_max_results")
            self.assertEqual(searches[1]["provider"], "semantic_scholar")
            self.assertEqual(searches[1]["perspective"], "natural_science_engineering")
            self.assertEqual(searches[2]["perspective"], "philosophy_methodology")

    def test_meta_learning_failed_web_search_is_traced(self):
        class ErrorSearch(WebSearchProvider):
            provider = "fake"

            def search(self, query: str, *, max_results: int = 5, category: str = "general"):
                raise WebSearchError("provider failed hf_" + "a" * 30)

        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            responses = [
                tool_call_response(
                    tool_call(
                        "web_search",
                        engine="fake",
                        perspective="finance_quant_econ",
                        query="market microstructure",
                        max_results=1,
                    )
                ),
                tool_call_response(tool_call("note", text="continue after failure")),
            ]
            runner = AgentSessionRunner(
                ctx,
                ScriptedLLM(responses),
                AgentSessionConfig(fold_deadline_at=datetime.now(timezone.utc) + timedelta(minutes=5), max_llm_calls=2),
                fold_info={"development_history": "workspace/development_history.json"},
                acceptance_rules={},
                mode="meta_learning",
                web_search_providers={"fake": ErrorSearch()},
            )

            summary = runner.run()

            self.assertNotEqual(summary["finish_status"], "meta_learning_done")
            searches = [event for event in ctx.trace.read_events() if event["event_type"] == "web_search"]
            self.assertEqual(len(searches), 1)
            self.assertEqual(searches[0]["status"], "error")
            self.assertIn("provider failed", searches[0]["error"])
            self.assertEqual(searches[0]["engine"], "fake")
            self.assertEqual(searches[0]["perspective"], "finance_quant_econ")
            self.assertEqual(searches[0]["query"], "market microstructure")
            self.assertEqual(searches[0]["tool_spec"]["schema_version"], 1)
            self.assertNotIn("hf_" + "a" * 30, json.dumps(searches, ensure_ascii=False))
            self.assertIn("hf_[redacted]", searches[0]["error"])
            self.assertNotIn("hf_" + "a" * 30, json.dumps(ctx.trace.read_events(), ensure_ascii=False))
            self.assertNotIn("hf_" + "a" * 30, json.dumps(runner.proxy.calls[-1]["messages"], ensure_ascii=False))

    def test_generic_tool_error_path_redacts_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            runner = AgentSessionRunner(
                ctx,
                ScriptedLLM([]),
                AgentSessionConfig(fold_deadline_at=datetime.now(timezone.utc) + timedelta(minutes=5)),
                fold_info={"development_history": "workspace/development_history.json"},
                acceptance_rules={},
                mode="meta_learning",
            )
            token = "hf_" + "b" * 30

            def boom(**kwargs):
                raise RuntimeError("provider exploded " + token)

            runner.search.grep = boom  # type: ignore[method-assign]
            result = runner._dispatch("grep", {"action": "grep", "pattern": "x", "root": "workspace"})

            self.assertEqual(result["observation"], "error")
            self.assertNotIn(token, json.dumps(result, ensure_ascii=False))
            self.assertIn("hf_[redacted]", str(result["error"]))
            events = ctx.trace.read_events()
            self.assertNotIn(token, json.dumps(events, ensure_ascii=False))
            self.assertIn("hf_[redacted]", json.dumps(events, ensure_ascii=False))

    def test_meta_learning_done_requires_nonempty_taste(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            runner = AgentSessionRunner(
                ctx,
                ScriptedLLM([tool_call_response(tool_call("done"))]),
                AgentSessionConfig(fold_deadline_at=datetime.now(timezone.utc) + timedelta(minutes=5), max_llm_calls=1),
                fold_info={"development_history": "workspace/development_history.json"},
                acceptance_rules={},
                mode="meta_learning",
            )

            summary = runner.run()

            self.assertNotEqual(summary["finish_status"], "meta_learning_done")
            errors = [event for event in ctx.trace.read_events() if event["event_type"] == "llm_call"]
            self.assertEqual(len(errors), 1)

    def test_meta_learning_done_rejects_taste_with_calendar_year(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            # A non-transferable calendar date in the Taste must block done.
            (ctx.paths.workspace / "taste.md").write_text(
                "# 品味\n## 一\n日内数据仅覆盖 21 个交易日（2021 年 8-9 月），样本不足。", encoding="utf-8"
            )
            runner = AgentSessionRunner(
                ctx,
                ScriptedLLM([tool_call_response(tool_call("done"))]),
                AgentSessionConfig(fold_deadline_at=datetime.now(timezone.utc) + timedelta(minutes=5), max_llm_calls=1),
                fold_info={"development_history": "workspace/development_history.json"},
                acceptance_rules={},
                mode="meta_learning",
            )

            summary = runner.run()

            self.assertNotEqual(summary["finish_status"], "meta_learning_done")
            self.assertEqual(runner._taste_policy_violation()[:14], "taste.md line ")
            self.assertIn("calendar date", runner._taste_policy_violation())

    def test_meta_learning_year_check_targets_visible_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            # The leak check derives the forbidden years from the visible fold, so it
            # tracks whatever year the window uses (here 2024), not a hard-coded year.
            ctx.manifest.update(
                meta_learning_visible_fold={
                    "input_window": "20240101..20240930",
                    "validation_period": "20241001..20241231",
                    "valid_decision_time": "2024-10-08T09:25:00+08:00",
                },
                valid_decision_time="2024-10-08T09:25:00+08:00",
            )
            runner = AgentSessionRunner(
                ctx,
                ScriptedLLM([tool_call_response(tool_call("done"))]),
                AgentSessionConfig(fold_deadline_at=datetime.now(timezone.utc) + timedelta(minutes=5), max_llm_calls=1),
                fold_info={"development_history": "workspace/development_history.json"},
                acceptance_rules={},
                mode="meta_learning",
            )
            # A bare visible-window year (no date syntax) is still caught.
            (ctx.paths.workspace / "taste.md").write_text("# 品味\n## 一\n对标 2024 的市场结构轮动。", encoding="utf-8")
            self.assertIn("calendar date", runner._taste_policy_violation())
            # A bare non-window number reading as a transferable regime reference is allowed.
            (ctx.paths.workspace / "taste.md").write_text(
                "# 品味\n## 一\n借鉴 2008 式系统性风险的应对，按季度控制回撤。", encoding="utf-8"
            )
            self.assertEqual(runner._taste_policy_violation(), "")

    def test_meta_learning_done_allows_cadence_words_without_year(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            # Quarterly cadence + plain counts/percentages are transferable, not dates.
            (ctx.paths.workspace / "taste.md").write_text(
                "# 品味\n## 一\n核心持仓按季度轮动；日内样本交易日不足（约 21 个），换手率 50%-80%。",
                encoding="utf-8",
            )
            runner = AgentSessionRunner(
                ctx,
                ScriptedLLM([tool_call_response(tool_call("done"))]),
                AgentSessionConfig(fold_deadline_at=datetime.now(timezone.utc) + timedelta(minutes=5)),
                fold_info={"development_history": "workspace/development_history.json"},
                acceptance_rules={},
                mode="meta_learning",
            )

            self.assertEqual(runner._taste_policy_violation(), "")
            summary = runner.run()
            self.assertEqual(summary["finish_status"], "meta_learning_done")

    def test_meta_learning_prompt_includes_experiment_directive(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            runner = AgentSessionRunner(
                ctx,
                ScriptedLLM([tool_call_response(tool_call("done"))]),
                AgentSessionConfig(fold_deadline_at=datetime.now(timezone.utc) + timedelta(minutes=5)),
                fold_info={"development_history": "workspace/development_history.json"},
                acceptance_rules={},
                mode="meta_learning",
                meta_learning_directive="Explore intraday liquidity shock reversal.",
            )

            self.assertIn("# 实验级探索方向（用户注入）", runner.system_prompt)
            self.assertIn("Explore intraday liquidity shock reversal.", runner.system_prompt)
            self.assertIn("不是已验证结论", runner.system_prompt)
            self.assertIn("用 `shell` 调用 Python", runner.system_prompt)
            self.assertIn("git", runner.system_prompt)
            self.assertIn("hf", runner.system_prompt)

    def test_meta_learning_prompt_describes_default_network_without_secret_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            runner = AgentSessionRunner(
                ctx,
                ScriptedLLM([tool_call_response(tool_call("done"))]),
                AgentSessionConfig(fold_deadline_at=datetime.now(timezone.utc) + timedelta(minutes=5)),
                fold_info={"development_history": "workspace/development_history.json"},
                acceptance_rules={},
                mode="meta_learning",
            )

            self.assertIn("## 运行环境、联网与代理", runner.system_prompt)
            self.assertIn("元学习 Fold 是唯一可配置联网的阶段", runner.system_prompt)
            self.assertIn("/mnt/artifacts/runtime_env.json", runner.system_prompt)
            self.assertIn("GITHUB_TOKEN", runner.system_prompt)
            self.assertIn("HF_TOKEN", runner.system_prompt)
            self.assertIn("AT_PROXY_*", runner.system_prompt)
            self.assertNotIn("github_pat_", runner.system_prompt)
            self.assertNotIn("hf_", runner.system_prompt)

    def test_meta_learning_network_policy_is_inside_environment_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            runner = AgentSessionRunner(
                ctx,
                ScriptedLLM([tool_call_response(tool_call("done"))]),
                AgentSessionConfig(fold_deadline_at=datetime.now(timezone.utc) + timedelta(minutes=5)),
                fold_info={"development_history": "workspace/development_history.json"},
                acceptance_rules={},
                mode="meta_learning",
            )

            environment_idx = runner.system_prompt.index("# 环境与配置")
            action_idx = runner.system_prompt.index("# 动作与流程")
            network_idx = runner.system_prompt.index("## 运行环境、联网与代理")
            self.assertGreater(network_idx, environment_idx)
            self.assertLess(network_idx, action_idx)
            self.assertIn("默认先使用直连网络", runner.system_prompt)
            self.assertNotIn("本次没有额外注入联网/代理配置", runner.system_prompt)

    def test_meta_learning_done_requires_configured_search(self):
        class FakeSearch(WebSearchProvider):
            provider = "fake"

            def search(self, query: str, *, max_results: int = 5, category: str = "general"):
                return [WebSearchResult(title=f"{category}:{query}", url="https://example.test", content="snippet")]

        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            responses = [
                tool_call_response(tool_call("done")),
            ]
            runner = AgentSessionRunner(
                ctx,
                ScriptedLLM(responses),
                AgentSessionConfig(fold_deadline_at=datetime.now(timezone.utc) + timedelta(minutes=5), max_llm_calls=1),
                fold_info={"development_history": "workspace/development_history.json"},
                acceptance_rules={},
                mode="meta_learning",
                web_search_providers={"fake": FakeSearch()},
            )

            summary = runner.run()

            self.assertNotEqual(summary["finish_status"], "meta_learning_done")
            self.assertEqual(summary["finish_status"], "deadline_timeout")

    def test_meta_learning_done_requires_all_search_perspectives(self):
        class FakeSearch(WebSearchProvider):
            provider = "fake"

            def search(self, query: str, *, max_results: int = 5, category: str = "general"):
                return [WebSearchResult(title=f"{category}:{query}", url="https://example.test", content="snippet")]

        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            responses = [
                tool_call_response(
                    tool_call(
                        "web_search",
                        engine="fake",
                        perspective="finance_quant_econ",
                        query="market microstructure",
                    )
                ),
                tool_call_response(tool_call("done")),
            ]
            runner = AgentSessionRunner(
                ctx,
                ScriptedLLM(responses),
                AgentSessionConfig(fold_deadline_at=datetime.now(timezone.utc) + timedelta(minutes=5), max_llm_calls=2),
                fold_info={"development_history": "workspace/development_history.json"},
                acceptance_rules={},
                mode="meta_learning",
                web_search_providers={"fake": FakeSearch()},
            )

            summary = runner.run()

            self.assertNotEqual(summary["finish_status"], "meta_learning_done")
            self.assertEqual(summary["finish_status"], "deadline_timeout")

    def test_meta_learning_empty_web_search_result_does_not_satisfy_perspective(self):
        class EmptySearch(WebSearchProvider):
            provider = "fake"

            def search(self, query: str, *, max_results: int = 5, category: str = "general"):
                return []

        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            (ctx.paths.workspace / "taste.md").write_text("empty search should not pass", encoding="utf-8")
            responses = [
                tool_call_response(
                    tool_call(
                        "web_search",
                        engine="fake",
                        perspective="finance_quant_econ",
                        query="market microstructure",
                    )
                ),
                tool_call_response(tool_call("done")),
            ]
            runner = AgentSessionRunner(
                ctx,
                ScriptedLLM(responses),
                AgentSessionConfig(fold_deadline_at=datetime.now(timezone.utc) + timedelta(minutes=5), max_llm_calls=2),
                fold_info={"development_history": "workspace/development_history.json"},
                acceptance_rules={},
                mode="meta_learning",
                web_search_providers={"fake": EmptySearch()},
            )

            summary = runner.run()

            self.assertNotEqual(summary["finish_status"], "meta_learning_done")
            searches = [event for event in ctx.trace.read_events() if event["event_type"] == "web_search"]
            self.assertEqual(searches[0]["status"], "empty_results")

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
            provider = SemanticScholarSearchProvider(key, min_interval_seconds=0, max_retries=0)
            with self.assertRaises(WebSearchError) as caught:
                provider.search("query", max_results=1)
        self.assertNotIn(key, str(caught.exception))
        self.assertIn("[redacted]", str(caught.exception))
        self.assertIn("after 1 attempt", str(caught.exception))

    def test_semantic_scholar_retries_rate_limit_then_succeeds(self):
        error = urllib.error.HTTPError(
            "https://api.semanticscholar.org/graph/v1/paper/search",
            429,
            "Too Many Requests",
            hdrs=None,
            fp=BytesIO(b"rate limited"),
        )
        payload = {"data": [{"paperId": "paper-1", "title": "A Paper", "year": 2025}]}
        with patch("urllib.request.urlopen", side_effect=[error, FakeHTTPResponse(payload)]), patch("time.sleep") as sleep:
            provider = SemanticScholarSearchProvider(
                "s2-secret",
                min_interval_seconds=0,
                max_retries=1,
                retry_initial_seconds=0.01,
                retry_max_seconds=0.01,
            )
            results = provider.search("query", max_results=1)
        self.assertEqual([item.title for item in results], ["A Paper"])
        sleep.assert_called()


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
            self.assertEqual(executor.map_path(paths.agent_output), "/mnt/agent/output")
            self.assertEqual(executor.map_path(paths.run_manifest), "/mnt/artifacts/run_manifest.json")
            with self.assertRaises(Exception):
                executor.map_path(Path("/etc/passwd"))

    def test_docker_executor_wraps_command_with_container_timeout(self):
        # A timed command runs under an in-container `timeout` so a deadline kills the
        # whole in-container process group, not just the host docker exec client.
        import subprocess as sp

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "mnt"
            paths = SandboxPaths(root)
            (root / "artifacts" / "workspace").mkdir(parents=True)
            executor = DockerExecutor("dummy", paths)
            with patch(
                "autotrade.environment.executor.subprocess.run",
                return_value=sp.CompletedProcess([], 0, stdout="", stderr=""),
            ) as run:
                executor.run(["python3", "train.py"], timeout_seconds=30)
            cmd = run.call_args.args[0]
            ti = cmd.index("timeout")
            self.assertEqual(cmd[ti : ti + 4], ["timeout", "--signal=TERM", "--kill-after=5", "30"])
            self.assertEqual(cmd[-2:], ["python3", "train.py"])  # agent argv runs under timeout
            self.assertEqual(run.call_args.kwargs["timeout"], 30 + 15.0)  # host deadline is a longer backstop

    def test_docker_executor_kill_marker_reaps_driver_then_user_tree(self):
        import subprocess as sp

        with tempfile.TemporaryDirectory() as tmp:
            paths = SandboxPaths(Path(tmp) / "mnt")
            executor = DockerExecutor("cont1", paths)
            with patch(
                "autotrade.environment.executor.subprocess.run",
                return_value=sp.CompletedProcess([], 0),
            ) as run:
                executor.kill_marker("at_driver_abc")
            commands = [call.args[0] for call in run.call_args_list]
            # First the targeted marked driver, then a sweep of the unprivileged
            # agent user's whole tree (reaps any child main(ctx) spawned, even if
            # the driver already exited). The container's PID 1 is root, so the
            # sweep cannot touch it.
            self.assertEqual(
                commands,
                [
                    ["docker", "exec", "--user", "agent", "cont1", "pkill", "-KILL", "-f", "at_driver_abc"],
                    ["docker", "exec", "--user", "agent", "cont1", "pkill", "-KILL", "-u", "agent"],
                ],
            )

    def test_docker_executor_cleanup_user_processes_sweeps_agent_user(self):
        import subprocess as sp

        with tempfile.TemporaryDirectory() as tmp:
            paths = SandboxPaths(Path(tmp) / "mnt")
            executor = DockerExecutor("cont1", paths)
            with patch(
                "autotrade.environment.executor.subprocess.run",
                return_value=sp.CompletedProcess([], 0),
            ) as run:
                executor.cleanup_user_processes()
            self.assertEqual(
                run.call_args.args[0],
                ["docker", "exec", "--user", "agent", "cont1", "pkill", "-KILL", "-u", "agent"],
            )


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
