import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hl_trader.agent import AgentSessionConfig, AgentSessionRunner
from hl_trader.environment.artifacts import ModificationConstraints, artifact_hash
from hl_trader.environment.broker import BrokerProfile
from hl_trader.environment.llm.proxy import ScriptedLLM
from hl_trader.environment.runtime import AgentTraceWriter, RunManifest
from hl_trader.environment.sandbox import LocalSandbox
from hl_trader.environment.tools import (
    BacktestTool,
    FinishFoldTool,
    ModificationCheckTool,
    SandboxShellTool,
    ToolContext,
    ToolError,
)

from .fixtures_sandbox import TEMPLATE_DIR, make_replay_dir, make_snapshot_dir, nl_score_response, write_strategy

IDS = {
    "experiment_id": "exp_test",
    "epoch_id": "epoch_001",
    "fold_id": "fold_2022Q1",
    "run_id": "run_x",
    "conversation_id": "conv_x",
}


def build_sandbox(tmp: Path, *, with_strategy: bool = True) -> tuple[LocalSandbox, ToolContext]:
    sandbox = LocalSandbox(tmp / "mnt")
    paths = sandbox.prepare_layout()
    valid_view = paths.snapshot_views / "valid_decision_input"
    test_view = paths.snapshot_views / "test_decision_input"
    valid_snap = make_snapshot_dir(valid_view, decision_date="20211008", kind="decision_input")
    test_snap = make_snapshot_dir(test_view, decision_date="20220104", kind="decision_input")
    make_replay_dir(paths.valid, start="20211008", end="20211230", label="valid")
    make_replay_dir(paths.test, start="20220104", end="20220331", label="test")
    sandbox.install_strategy_artifact(None, TEMPLATE_DIR)
    if with_strategy:
        write_strategy(paths.agent_output)
    profile = BrokerProfile()
    manifest = RunManifest.create(
        paths.run_manifest,
        {
            **IDS,
            "valid_decision_time": "2021-10-08T09:25:00+08:00",
            "test_decision_time": "2022-01-04T09:25:00+08:00",
            "snapshots": {
                "valid_decision_input": {"snapshot_id": valid_snap["snapshot_id"], "snapshot_hash": valid_snap["snapshot_hash"]},
                "test_decision_input": {"snapshot_id": test_snap["snapshot_id"], "snapshot_hash": test_snap["snapshot_hash"]},
            },
            "is_initial_artifact": True,
            "template_dir": str(TEMPLATE_DIR),
            "modification_constraints": ModificationConstraints(is_initial_artifact=True).to_record(),
            "broker_profile": profile.to_record(),
            "long_score_threshold": profile.long_score_threshold,
            "short_score_threshold": profile.short_score_threshold,
            "max_total_holdings": profile.max_total_holdings,
            "short_inventory_mode": profile.short_inventory_mode,
            "max_candidates": 10,
            "per_call_timeout_seconds": 120,
        },
    )
    trace = AgentTraceWriter(paths.agent_trace, ids=IDS)
    ctx = ToolContext(paths=paths, manifest=manifest, trace=trace)
    sandbox.bind_snapshot_view(valid_view)
    return sandbox, ctx


class ToolFlowTest(unittest.TestCase):
    def test_modification_check_backtest_and_finish_fold(self):
        with tempfile.TemporaryDirectory() as tmp:
            sandbox, ctx = build_sandbox(Path(tmp))
            ctx.proxy = ScriptedLLM([nl_score_response()])

            check = ModificationCheckTool(ctx).run()
            self.assertTrue(check["allowed_to_backtest"])

            summary = BacktestTool(ctx).run(mode="valid", nl_mode="on")
            self.assertEqual(summary["status"], "ok")
            self.assertTrue(summary["complete_validation"])
            self.assertGreater(summary["total_return"], 0.0)
            result_dir = Path(summary["result_path"])
            self.assertTrue((result_dir / "detailed_return.json").exists())
            self.assertTrue((result_dir / "order_plan.parquet").exists())
            for name in ("company_context.jsonl", "search_requests.jsonl", "evidence.jsonl", "scores.jsonl", "nl_llm_calls.jsonl"):
                self.assertTrue((result_dir / "nl_output" / name).exists(), name)

            finish = FinishFoldTool(ctx).run()
            self.assertEqual(finish["status"], "fold_finished")
            self.assertTrue(ctx.write_locked)
            with self.assertRaisesRegex(ToolError, "locked"):
                BacktestTool(ctx).run(mode="valid", nl_mode="off")

            event_types = {event["event_type"] for event in ctx.trace.read_events()}
            self.assertLessEqual({"tool", "backtest", "nl_batch_summary", "finish_fold"}, event_types)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink not available on this platform")
    def test_modification_check_returns_structured_failure_for_invalid_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            factors = ctx.paths.agent_output / "factor" / "factors.json"
            factors.unlink()
            factors.symlink_to(ctx.paths.agent_output / "nl_prior" / "prior.json")

            check = ModificationCheckTool(ctx).run()
            self.assertFalse(check["allowed_to_backtest"])
            self.assertIsNone(check["artifact_hash"])
            self.assertTrue(any("symlink" in reason for reason in check["reasons"]))

    def test_frozen_eval_requires_frozen_phase_and_unchanged_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            sandbox, ctx = build_sandbox(Path(tmp))
            ctx.proxy = ScriptedLLM([nl_score_response(), nl_score_response()])
            ModificationCheckTool(ctx).run()
            BacktestTool(ctx).run(mode="valid", nl_mode="on")
            with self.assertRaisesRegex(ToolError, "not available in phase"):
                BacktestTool(ctx).run(mode="frozen_eval", nl_mode="on")
            ctx.phase = "frozen"
            ctx.write_locked = True
            ctx.manifest.update(frozen_strategy_artifact_hash=artifact_hash(ctx.paths.agent_output))
            sandbox.bind_snapshot_view(ctx.paths.snapshot_views / "test_decision_input")
            with self.assertRaisesRegex(ToolError, "full natural-language scoring"):
                BacktestTool(ctx).run(mode="frozen_eval", nl_mode="sample")
            summary = BacktestTool(ctx).run(mode="frozen_eval", nl_mode="on", result_name="test_000")
            self.assertEqual(summary["result_name"], "test_000")

    def test_backtest_rejects_wrong_snapshot_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            sandbox, ctx = build_sandbox(Path(tmp))
            ctx.proxy = ScriptedLLM([nl_score_response()])
            sandbox.bind_snapshot_view(ctx.paths.snapshot_views / "test_decision_input")
            with self.assertRaisesRegex(ToolError, "does not match the pipeline record"):
                BacktestTool(ctx).run(mode="valid", nl_mode="off")

    def test_nl_failure_fails_formal_backtest_but_keeps_audit_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            ctx.manifest.update(nl_failure_policy="fail")
            ctx.proxy = ScriptedLLM(["not json", "still not json"])
            with self.assertRaisesRegex(ToolError, "natural-language scoring failed"):
                BacktestTool(ctx).run(mode="valid", nl_mode="on")
            nl_calls = ctx.paths.results / "valid_000" / "nl_output" / "nl_llm_calls.jsonl"
            self.assertTrue(nl_calls.exists())

    def test_contract_check_runs_without_results_or_nl(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            summary = BacktestTool(ctx).contract_check()
            self.assertEqual(summary["status"], "ok")
            self.assertEqual(list(ctx.paths.results.iterdir()), [])

    def test_step_tree_records_validated_steps_when_enabled(self):
        from hl_trader.environment.step_tree import StepTree

        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            ctx.manifest.update(step_tree_enabled=True, factor_attribution_enabled=True)
            ctx.proxy = ScriptedLLM([nl_score_response(), nl_score_response()])
            BacktestTool(ctx).run(mode="valid", nl_mode="off")
            BacktestTool(ctx).run(mode="valid", nl_mode="on")
            BacktestTool(ctx).run(mode="valid", nl_mode="on")
            tree = StepTree(ctx.paths.steps)
            nodes = tree.nodes()
            self.assertEqual(len(nodes), 2)
            self.assertEqual(nodes[1]["parent_node_id"], nodes[0]["node_id"])
            self.assertEqual(tree.current_node_id, nodes[1]["node_id"])
            self.assertTrue((ctx.paths.steps / nodes[0]["node_id"] / "factor" / "main.py").exists())
            self.assertTrue((ctx.paths.steps / nodes[0]["node_id"] / "factor_attribution.json").exists())
            self.assertTrue(nodes[0]["node_id"].startswith("epoch_001__fold_2022Q1__valid_"))

    def test_attribution_enabled_requires_registered_factor_columns_for_complete_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            ctx.manifest.update(factor_attribution_enabled=True)
            factors_path = ctx.paths.agent_output / "factor" / "factors.json"
            factors_path.write_text(json.dumps({"factors": []}), encoding="utf-8")
            ctx.proxy = ScriptedLLM([nl_score_response()])
            with self.assertRaisesRegex(ToolError, "no registered factors"):
                BacktestTool(ctx).run(mode="valid", nl_mode="on")

    def test_step_tree_disabled_records_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            ctx.proxy = ScriptedLLM([nl_score_response()])
            BacktestTool(ctx).run(mode="valid", nl_mode="on")
            self.assertFalse((ctx.paths.steps / "tree.json").exists())


class ShellToolTest(unittest.TestCase):
    def test_shell_runs_and_logs_and_guards_test_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp), with_strategy=False)
            result = SandboxShellTool(ctx).run("echo hello")
            self.assertEqual(result.exit_code, 0)
            self.assertIn("hello", result.stdout)
            cwd = SandboxShellTool(ctx).run("pwd")
            self.assertEqual(Path(cwd.stdout.strip()), ctx.paths.agent)
            SandboxShellTool(ctx).run("touch workspace/ok")
            self.assertTrue((ctx.paths.workspace / "ok").exists())
            with self.assertRaisesRegex(ToolError, "forbidden path"):
                SandboxShellTool(ctx).run(f"cat {ctx.paths.test}/daily.parquet")
            events = [e for e in ctx.trace.read_events() if e["event_type"] == "shell"]
            self.assertEqual(len(events), 3)

    def test_shell_can_read_step_tree_from_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp), with_strategy=False)
            (ctx.paths.steps / "tree.json").write_text('{"nodes": []}', encoding="utf-8")
            read = SandboxShellTool(ctx).run(f"rg nodes {ctx.paths.steps}")
            self.assertEqual(read.exit_code, 0)
            self.assertIn("nodes", read.stdout)


class AgentSessionRunnerTest(unittest.TestCase):
    def test_scripted_session_finishes_fold(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            nl_proxy_responses = [
                json.dumps({"action": "note", "text": "inspect data"}),
                json.dumps({"action": "modification_check"}),
                json.dumps({"action": "backtest", "nl_mode": "on"}),
                nl_score_response(),  # consumed by NL scoring inside backtest_tool
                json.dumps({"action": "finish_fold"}),
            ]
            proxy = ScriptedLLM(nl_proxy_responses)
            ctx.proxy = proxy
            runner = AgentSessionRunner(
                ctx,
                proxy,
                AgentSessionConfig(
                    fold_deadline_at=datetime.now(timezone.utc) + timedelta(minutes=5),
                    per_call_timeout_seconds=60,
                ),
                fold_info={"fold_id": "fold_2022Q1"},
                acceptance_rules={"min_return": 0.0},
            )
            summary = runner.run()
            self.assertEqual(summary["finish_status"], "fold_finished")
            self.assertTrue(ctx.write_locked)
            llm_events = [e for e in ctx.trace.read_events() if e["event_type"] == "llm_call"]
            self.assertGreaterEqual(len(llm_events), 4)
            self.assertTrue(all("messages" in e and "raw_content" in e for e in llm_events))

    def test_agent_and_nl_can_use_different_model_proxies(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            agent_proxy = ScriptedLLM(
                [
                    json.dumps({"action": "modification_check"}),
                    json.dumps({"action": "backtest", "nl_mode": "on"}),
                    json.dumps({"action": "finish_fold"}),
                ]
            )
            agent_proxy.model = "agent-model"
            nl_proxy = ScriptedLLM([nl_score_response()])
            nl_proxy.model = "nl-model"
            ctx.proxy = agent_proxy
            ctx.nl_proxy = nl_proxy

            runner = AgentSessionRunner(
                ctx,
                agent_proxy,
                AgentSessionConfig(
                    fold_deadline_at=datetime.now(timezone.utc) + timedelta(minutes=5),
                    per_call_timeout_seconds=60,
                ),
                fold_info={"fold_id": "fold_2022Q1"},
                acceptance_rules={"min_return": 0.0},
            )
            summary = runner.run()
            self.assertEqual(summary["finish_status"], "fold_finished")
            self.assertEqual(len(agent_proxy.calls), 3)
            self.assertEqual(len(nl_proxy.calls), 1)

            agent_models = [
                event["model"] for event in ctx.trace.read_events() if event["event_type"] == "llm_call"
            ]
            self.assertEqual(agent_models, ["agent-model", "agent-model", "agent-model"])
            nl_calls = (
                ctx.paths.results / "valid_000" / "nl_output" / "nl_llm_calls.jsonl"
            ).read_text(encoding="utf-8")
            self.assertIn('"model": "nl-model"', nl_calls)

    def test_deadline_zero_stops_immediately(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            proxy = ScriptedLLM([])
            runner = AgentSessionRunner(
                ctx,
                proxy,
                AgentSessionConfig(fold_deadline_at=datetime.now(timezone.utc) - timedelta(seconds=1)),
                fold_info={},
                acceptance_rules={},
            )
            summary = runner.run()
            self.assertEqual(summary["finish_status"], "deadline_timeout")
            self.assertEqual(summary["llm_calls"], 0)


if __name__ == "__main__":
    unittest.main()
