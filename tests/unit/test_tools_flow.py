import json
import os
import shutil
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from autotrade.agent import AgentSessionConfig, AgentSessionRunner, ContextCompactionConfig, ContextCompactor
from autotrade.environment.artifacts import ModificationConstraints, artifact_hash
from autotrade.environment.broker import BrokerProfile
from autotrade.environment.llm.proxy import (
    LLMProxyError,
    ProviderResponse,
    ScriptedLLM,
    tool_call,
    tool_call_response,
)
from autotrade.environment.explore import ExploreSubAgentEngine
from autotrade.environment.runtime import AgentTraceWriter, RunManifest
from autotrade.environment.sandbox import LocalSandbox
from autotrade.environment.tools import (
    BacktestTool,
    FinishFoldTool,
    ModificationCheckTool,
    SandboxShellTool,
    StructuredSearchTool,
    ToolContext,
    ToolError,
)

from .fixtures_sandbox import TEMPLATE_DIR, make_replay_dir, make_snapshot_dir, nl_subagent_response, write_strategy

IDS = {
    "experiment_id": "exp_test",
    "epoch_id": "epoch_001",
    "fold_id": "fold_2022Q1",
    "run_id": "run_x",
    "conversation_id": "conv_x",
}


def nl_subagent_text(ts_code: str = "000001.SZ", stance: str = "positive") -> str:
    return json.dumps({"ts_code": ts_code, "stance": stance, "note": "fixture"}, ensure_ascii=False)

CUSTOM_STRATEGY_MAIN = '''
import os
from pathlib import Path

import pandas as pd


def buy_hold(ctx):
    if ctx.stock.position == 0:
        ctx.broker.buy(weight=0.1, reason="direct_long")


def run_strategy(context):
    snapshot_dir = Path(str(context.get("snapshot_dir") or os.environ.get("AT_SNAPSHOT_DIR")))
    daily = pd.read_parquet(snapshot_dir / "daily.parquet")
    code = sorted(daily["ts_code"].astype(str).unique())[0]
    return {
        "candidates": pd.DataFrame([{"ts_code": code, "reason": "fixture_top", "source_artifacts": ["daily_window"]}]),
        "trade_intents": pd.DataFrame(
            [{"code": code, "trade_strategy": "buy_hold", "reason": "direct_long",
              "source_artifacts": ["daily_window"]}]
        ),
        "metadata": {"stage": "direct_trading"},
    }
'''

MINUTE_STRATEGY_MAIN = '''
import os
from pathlib import Path

import pandas as pd


def close_entry(ctx):
    if ctx.stock.position == 0 and ctx.cur_time >= str(ctx.params.get("time", "14:57")):
        ctx.broker.buy(weight=0.1, reason="minute_close_buy")


def run_strategy(context):
    snapshot_dir = Path(str(context.get("snapshot_dir") or os.environ.get("AT_SNAPSHOT_DIR")))
    daily = pd.read_parquet(snapshot_dir / "daily.parquet")
    code = sorted(daily["ts_code"].astype(str).unique())[0]
    return {
        "candidates": pd.DataFrame([{"ts_code": code, "reason": "fixture_top", "source_artifacts": ["daily_window"]}]),
        "trade_intents": pd.DataFrame(
            [{"code": code, "trade_strategy": "close_entry", "time": "14:57",
              "reason": "minute_close_buy", "source_artifacts": ["minute_test"]}]
        ),
        "metadata": {"replay_granularity": context.get("replay_granularity")},
    }
'''

MODEL_ARTIFACT_STRATEGY_MAIN = '''
import json
from pathlib import Path

import pandas as pd


def buy_with_model(ctx):
    if ctx.stock.position == 0:
        ctx.broker.buy(weight=0.1, reason="model_artifact_buy")


def run_strategy(context):
    model_dir = Path(str(context["model_dir"]))
    model_dir.mkdir(parents=True, exist_ok=True)
    (model_dir / "params.json").write_text(json.dumps({"threshold": 0.42}, sort_keys=True), encoding="utf-8")
    snapshot_dir = Path(str(context["snapshot_dir"]))
    daily = pd.read_parquet(snapshot_dir / "daily.parquet")
    code = sorted(daily["ts_code"].astype(str).unique())[0]
    return {
        "trade_intents": pd.DataFrame([{"code": code, "trade_strategy": "buy_with_model"}]),
        "metadata": {"model_dir": str(model_dir)},
    }
'''

MUTATING_MODEL_ARTIFACT_STRATEGY_MAIN = '''
from pathlib import Path

import pandas as pd


def buy_with_model(ctx):
    if ctx.stock.position == 0:
        ctx.broker.buy(weight=0.1, reason="mutating_model_buy")


def run_strategy(context):
    model_dir = Path(str(context["model_dir"]))
    model_dir.mkdir(parents=True, exist_ok=True)
    path = model_dir / "counter.txt"
    current = int(path.read_text(encoding="utf-8")) if path.exists() else 0
    path.write_text(str(current + 1), encoding="utf-8")
    snapshot_dir = Path(str(context["snapshot_dir"]))
    daily = pd.read_parquet(snapshot_dir / "daily.parquet")
    code = sorted(daily["ts_code"].astype(str).unique())[0]
    return {"trade_intents": pd.DataFrame([{"code": code, "trade_strategy": "buy_with_model"}])}
'''

CUSTOM_POLICY_MAIN = '''
import os
from pathlib import Path

import pandas as pd


def run_strategy(context):
    snapshot_dir = Path(str(context.get("snapshot_dir") or os.environ.get("AT_SNAPSHOT_DIR")))
    daily = pd.read_parquet(snapshot_dir / "daily.parquet")
    code = sorted(daily["ts_code"].astype(str).unique())[0]
    return {
        "trade_intents": pd.DataFrame(
            [{"code": code, "trade_strategy": "buy_if_dip"}]
        ),
        "metadata": {"policy": "buy_if_dip"},
    }
'''

CUSTOM_POLICY_TRADING = '''
def buy_if_dip(ctx):
    low = (ctx.bar or {}).get("low")
    if low is not None and float(low) <= 9.95 and ctx.stock.position == 0:
        ctx.broker.buy(weight=0.1, reason="minute_dip")
'''

CTX_BOUNDARY_TRADING = '''
def inspect_ctx(ctx):
    forbidden = [name for name in ("model_dir", "workspace_dir", "nl") if hasattr(ctx, name)]
    if forbidden:
        raise RuntimeError("decision-only ctx fields exposed: " + ",".join(forbidden))
    if ctx.stock.position == 0:
        ctx.broker.buy(weight=0.1, reason="ctx_boundary")
'''

POLICY_NL_TRADING = '''
from at_tools import nl


def call_nl(ctx):
    nl(ctx.stock.code, prompt="this should not run during replay")
'''

NOISY_POLICY_MAIN = '''
print("main import noise")


def noisy_buy(ctx):
    print("strategy call noise")
    if ctx.stock.position == 0:
        ctx.broker.buy(weight=0.1, reason="noisy_buy")


def run_strategy(context):
    print("entrypoint noise")
    return {"trade_intents": [{"ts_code": "000001.SZ", "trade_strategy": "noisy_buy"}]}
'''

BROKEN_STRATEGY_MAIN = '''
def run_strategy(context):
    raise RuntimeError("boom before result artifacts")
'''

BROKEN_SECRET_STRATEGY_MAIN = '''
def run_strategy(context):
    raise RuntimeError("decision failed Authorization: Bearer secret-token-abc")
'''

POLICY_SECRET_ERROR_MAIN = '''
def leak_secret(ctx):
    raise RuntimeError("provider failed Authorization: Bearer secret-token-abc")


def run_strategy(context):
    return {"trade_intents": [{"ts_code": "000001.SZ", "trade_strategy": "leak_secret"}]}
'''

POLICY_IMPORT_SECRET_MAIN = '''
def run_strategy(context):
    return {"trade_intents": [{"ts_code": "000001.SZ", "trade_strategy": "imported_strategy"}]}
'''

POLICY_IMPORT_SECRET_TRADING = '''
raise RuntimeError("import failed Authorization: Bearer secret-token-abc")
'''

POLICY_OUTPUT_WRITE_MAIN = '''
from pathlib import Path


def mutate_output(ctx):
    Path("output/replay_mutation.txt").write_text("bad", encoding="utf-8")


def run_strategy(context):
    return {"trade_intents": [{"ts_code": "000001.SZ", "trade_strategy": "mutate_output"}]}
'''

POLICY_CREATE_SYMLINK_MAIN = '''
from pathlib import Path


def create_link(ctx):
    Path("tmp_model_link").symlink_to("models", target_is_directory=True)


def run_strategy(context):
    return {"trade_intents": [{"ts_code": "000001.SZ", "trade_strategy": "create_link"}]}
'''

TEMPLATE_CANDIDATE_WITH_ROW = '''
from pathlib import Path

import pandas as pd


def buy_hold(ctx):
    if ctx.stock.position == 0:
        ctx.broker.buy(weight=0.1, reason="template_candidate")


def run_strategy(context):
    snapshot_dir = Path(str(context["snapshot_dir"]))
    daily = pd.read_parquet(snapshot_dir / "daily.parquet")
    code = sorted(daily["ts_code"].astype(str).unique())[0]
    return {"trade_intents": pd.DataFrame(
        [{"code": code, "trade_strategy": "buy_hold", "reason": "template_candidate",
          "source_artifacts": ["daily_window"]}]
    ), "metadata": {"stage": "template_override"}}
'''


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
            "template_ref": "agent_output_template",
            "initial_template_hash": artifact_hash(paths.parent_output),
            "modification_constraints": ModificationConstraints(is_initial_artifact=True).to_record(),
            "broker_profile": profile.to_record(),
            "short_inventory_mode": profile.short_inventory_mode,
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

            check = ModificationCheckTool(ctx).run()
            self.assertTrue(check["allowed_to_backtest"])

            summary = BacktestTool(ctx).run(mode="valid")
            self.assertEqual(summary["status"], "ok")
            self.assertTrue(summary["complete_validation"])
            self.assertNotIn("modification_check_auto_run", summary)
            self.assertGreater(summary["total_return"], 0.0)
            result_dir = Path(summary["result_path"])
            self.assertTrue((result_dir / "detailed_return.json").exists())
            self.assertTrue((result_dir / "trade_intents.parquet").exists())
            self.assertTrue((result_dir / "strategy_metadata.json").exists())

            finish = FinishFoldTool(ctx).run()
            self.assertEqual(finish["status"], "fold_finished")
            self.assertTrue(ctx.write_locked)
            with self.assertRaisesRegex(ToolError, "locked"):
                BacktestTool(ctx).run(mode="valid")

            event_types = {event["event_type"] for event in ctx.trace.read_events()}
            self.assertLessEqual({"tool", "backtest", "finish_fold"}, event_types)

    def test_backtest_runs_strategy_program_trade_intents(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            (ctx.paths.agent_output / "main.py").write_text(CUSTOM_STRATEGY_MAIN, encoding="utf-8")

            summary = BacktestTool(ctx).run(mode="valid")

            self.assertEqual(summary["status"], "ok")
            self.assertNotIn("modification_check_auto_run", summary)
            self.assertEqual(summary["trade_strategies"], ["buy_hold"])
            result_dir = Path(summary["result_path"])
            self.assertTrue((result_dir / "trade_intents.parquet").exists())
            self.assertTrue((result_dir / "strategy_metadata.json").exists())
            intents = pd.read_parquet(result_dir / "trade_intents.parquet")
            self.assertEqual(intents.loc[0, "trade_strategy"], "buy_hold")
            metadata = json.loads((result_dir / "strategy_metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["stage"], "direct_trading")

    def test_strategy_stdout_does_not_corrupt_policy_rpc(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            (ctx.paths.agent_output / "main.py").write_text(NOISY_POLICY_MAIN, encoding="utf-8")

            summary = BacktestTool(ctx).run(mode="valid")

            self.assertEqual(summary["status"], "ok")
            self.assertEqual(summary["trade_strategies"], ["noisy_buy"])
            self.assertEqual(summary["order_count"], 1)

    def test_policy_rpc_error_is_redacted(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            (ctx.paths.agent_output / "main.py").write_text(POLICY_SECRET_ERROR_MAIN, encoding="utf-8")

            with self.assertRaisesRegex(ToolError, "redacted|REDACTED") as raised:
                BacktestTool(ctx).run(mode="valid")

            self.assertNotIn("secret-token-abc", str(raised.exception))

    def test_policy_import_error_is_redacted(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            (ctx.paths.agent_output / "main.py").write_text(POLICY_IMPORT_SECRET_MAIN, encoding="utf-8")
            (ctx.paths.agent_output / "trading.py").write_text(POLICY_IMPORT_SECRET_TRADING, encoding="utf-8")

            with self.assertRaisesRegex(ToolError, "redacted|REDACTED") as raised:
                BacktestTool(ctx).run(mode="valid")

            self.assertNotIn("secret-token-abc", str(raised.exception))

    def test_trade_policy_cannot_write_output_artifacts_during_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            (ctx.paths.agent_output / "main.py").write_text(POLICY_OUTPUT_WRITE_MAIN, encoding="utf-8")

            with self.assertRaisesRegex(ToolError, "forbidden path") as raised:
                BacktestTool(ctx).run(mode="valid")

            self.assertIn("write", str(raised.exception))
            self.assertFalse((ctx.paths.agent_output / "replay_mutation.txt").exists())

    def test_trade_policy_cannot_read_model_artifacts_through_output_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            (ctx.paths.model_artifacts / "secret.txt").write_text("model-secret", encoding="utf-8")
            link = Path(tmp) / "model_link"
            link.symlink_to(ctx.paths.model_artifacts, target_is_directory=True)
            main = f'''
from pathlib import Path


def read_model_link(ctx):
    (Path({str(link)!r}) / "secret.txt").read_text(encoding="utf-8")


def run_strategy(context):
    return {{"trade_intents": [{{"ts_code": "000001.SZ", "trade_strategy": "read_model_link"}}]}}
'''
            (ctx.paths.agent_output / "main.py").write_text(main, encoding="utf-8")

            with self.assertRaisesRegex(ToolError, "forbidden path") as raised:
                BacktestTool(ctx).run(mode="valid")

            self.assertNotIn("model-secret", str(raised.exception))

    def test_trade_policy_cannot_create_links_during_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            (ctx.paths.agent_output / "main.py").write_text(POLICY_CREATE_SYMLINK_MAIN, encoding="utf-8")

            with self.assertRaisesRegex(ToolError, "cannot create links"):
                BacktestTool(ctx).run(mode="valid")

            self.assertFalse((ctx.paths.agent / "tmp_model_link").exists())

    def test_strategy_program_failure_stderr_is_redacted(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            (ctx.paths.agent_output / "main.py").write_text(BROKEN_SECRET_STRATEGY_MAIN, encoding="utf-8")

            with self.assertRaisesRegex(ToolError, "redacted|REDACTED") as raised:
                BacktestTool(ctx).run(mode="valid")

            self.assertNotIn("secret-token-abc", str(raised.exception))

    def test_backtest_failure_does_not_leave_empty_result_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            (ctx.paths.agent_output / "main.py").write_text(BROKEN_STRATEGY_MAIN, encoding="utf-8")

            with self.assertRaisesRegex(ToolError, "boom before result artifacts"):
                BacktestTool(ctx).run(mode="valid")

            self.assertEqual(list(ctx.paths.results.glob("valid_*")), [])
            errors = [item for item in ctx.manifest.get("backtest_summaries", []) if item.get("status") == "error"]
            self.assertEqual(len(errors), 1)

    def test_custom_trade_intents_use_minute_replay_when_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            pd.DataFrame(
                [
                    {
                        "trade_date": "20211008",
                        "ts_code": "000001.SZ",
                        "trade_time": "09:31",
                        "open": 10.0,
                        "high": 10.1,
                        "low": 9.9,
                        "close": 10.05,
                    },
                    {
                        "trade_date": "20211008",
                        "ts_code": "000001.SZ",
                        "trade_time": "14:57",
                        "open": 10.1,
                        "high": 10.3,
                        "low": 10.0,
                        "close": 10.25,
                    },
                ]
            ).to_parquet(ctx.paths.valid / "intraday_1min.parquet", index=False)
            (ctx.paths.agent_output / "main.py").write_text(MINUTE_STRATEGY_MAIN, encoding="utf-8")

            summary = BacktestTool(ctx).run(mode="valid")

            self.assertEqual(summary["status"], "ok")
            self.assertEqual(summary["trade_strategies"], ["close_entry"])
            self.assertEqual(summary["replay_granularity"], "minute")
            result_dir = Path(summary["result_path"])
            metadata = json.loads((result_dir / "strategy_metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["replay_granularity"], "minute")
            detailed = json.loads((result_dir / "detailed_return.json").read_text(encoding="utf-8"))
            self.assertEqual(detailed["replay_granularity"], "minute")
            fill = [event for event in detailed["broker_events"] if event["event_type"] == "order_filled"][0]
            self.assertEqual(fill["price_label"], "minute:14:57")
            self.assertAlmostEqual(fill["price"], BrokerProfile().slipped_price(10.25, is_buy=True))

    def test_strategy_program_context_does_not_expose_workspace_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            main = '''
import os
from pathlib import Path

import pandas as pd


def noop(ctx):
    return None


def run_strategy(context):
    if "workspace_dir" in context:
        raise RuntimeError("workspace_dir leaked through context")
    if os.environ.get("AT_WORKSPACE_DIR"):
        raise RuntimeError("AT_WORKSPACE_DIR leaked through environment")
    snapshot_dir = Path(str(context["snapshot_dir"]))
    daily = pd.read_parquet(snapshot_dir / "daily.parquet")
    code = sorted(daily["ts_code"].astype(str).unique())[0]
    return {
        "trade_intents": [{"code": code, "trade_strategy": "noop"}],
        "metadata": {"context_keys": sorted(context.keys())},
    }
'''
            (ctx.paths.agent_output / "main.py").write_text(main, encoding="utf-8")

            summary = BacktestTool(ctx).run(mode="valid")

            result_dir = Path(summary["result_path"])
            metadata = json.loads((result_dir / "strategy_metadata.json").read_text(encoding="utf-8"))
            self.assertNotIn("workspace_dir", metadata["context_keys"])
            self.assertIn("model_dir", metadata["context_keys"])

    def test_strategy_program_can_persist_model_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            (ctx.paths.agent_output / "main.py").write_text(MODEL_ARTIFACT_STRATEGY_MAIN, encoding="utf-8")

            summary = BacktestTool(ctx).run(mode="valid")

            self.assertEqual(summary["status"], "ok")
            self.assertEqual(summary["model_artifact_files"], 1)
            self.assertTrue((ctx.paths.model_artifacts / "params.json").exists())
            self.assertEqual(summary["model_artifact_hash"], ctx.manifest.get("backtest_summaries")[-1]["model_artifact_hash"])

            check = ModificationCheckTool(ctx).run()
            self.assertTrue(check["allowed_to_backtest"])
            self.assertEqual(check["model_delta"]["changed_file_count"], 1)
            self.assertEqual(check["model_artifact_hash"], summary["model_artifact_hash"])

            finish = FinishFoldTool(ctx).run()
            self.assertEqual(finish["status"], "fold_finished")

    def test_finish_fold_rejects_contract_check_model_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            (ctx.paths.agent_output / "main.py").write_text(MUTATING_MODEL_ARTIFACT_STRATEGY_MAIN, encoding="utf-8")

            BacktestTool(ctx).run(mode="valid")
            ModificationCheckTool(ctx).run()

            with self.assertRaisesRegex(ToolError, "contract check changed formal artifacts"):
                FinishFoldTool(ctx).run()

    def test_custom_trading_function_runs_during_minute_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            pd.DataFrame(
                [
                    {
                        "trade_date": "20211008",
                        "ts_code": "000001.SZ",
                        "trade_time": "09:31",
                        "open": 10.0,
                        "high": 10.1,
                        "low": 9.9,
                        "close": 10.0,
                    },
                    {
                        "trade_date": "20211008",
                        "ts_code": "000001.SZ",
                        "trade_time": "09:32",
                        "open": 10.0,
                        "high": 10.2,
                        "low": 10.0,
                        "close": 10.1,
                    },
                ]
            ).to_parquet(ctx.paths.valid / "intraday_1min.parquet", index=False)
            (ctx.paths.agent_output / "main.py").write_text(CUSTOM_POLICY_MAIN, encoding="utf-8")
            (ctx.paths.agent_output / "trading.py").write_text(CUSTOM_POLICY_TRADING, encoding="utf-8")

            summary = BacktestTool(ctx).run(mode="valid")

            self.assertEqual(summary["status"], "ok")
            self.assertEqual(summary["trade_strategies"], ["buy_if_dip"])
            self.assertEqual(summary["order_count"], 1)
            result_dir = Path(summary["result_path"])
            detailed = json.loads((result_dir / "detailed_return.json").read_text(encoding="utf-8"))
            custom_events = [event for event in detailed["broker_events"] if event["event_type"] == "strategy_actions"]
            self.assertEqual(len(custom_events), 1)
            fill = [event for event in detailed["broker_events"] if event["event_type"] == "order_filled"][0]
            self.assertEqual(fill["price_label"], "minute:09:31")
            self.assertAlmostEqual(fill["price"], BrokerProfile().slipped_price(10.0, is_buy=True))

    def test_trade_policy_ctx_excludes_decision_only_helpers(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            main = '''
def run_strategy(context):
    return {"trade_intents": [{"ts_code": "000001.SZ", "trade_strategy": "inspect_ctx"}]}
'''
            (ctx.paths.agent_output / "main.py").write_text(main, encoding="utf-8")
            (ctx.paths.agent_output / "trading.py").write_text(CTX_BOUNDARY_TRADING, encoding="utf-8")

            summary = BacktestTool(ctx).run(mode="valid")

            self.assertEqual(summary["status"], "ok")
            self.assertEqual(summary["trade_strategies"], ["inspect_ctx"])

    def test_trade_policy_cannot_call_nl_during_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            main = '''
def run_strategy(context):
    return {"trade_intents": [{"ts_code": "000001.SZ", "trade_strategy": "call_nl"}]}
'''
            (ctx.paths.agent_output / "main.py").write_text(main, encoding="utf-8")
            (ctx.paths.agent_output / "trading.py").write_text(POLICY_NL_TRADING, encoding="utf-8")

            with self.assertRaisesRegex(ToolError, "decision stage"):
                BacktestTool(ctx).run(mode="valid")

    def test_empty_minute_replay_file_reports_daily_granularity(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            pd.DataFrame(columns=["trade_date", "ts_code", "trade_time", "open", "high", "low", "close"]).to_parquet(
                ctx.paths.valid / "intraday_1min.parquet",
                index=False,
            )
            (ctx.paths.agent_output / "main.py").write_text(MINUTE_STRATEGY_MAIN, encoding="utf-8")

            summary = BacktestTool(ctx).run(mode="valid")

            self.assertEqual(summary["status"], "ok")
            self.assertEqual(summary["replay_granularity"], "daily")
            result_dir = Path(summary["result_path"])
            metadata = json.loads((result_dir / "strategy_metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["replay_granularity"], "daily")
            detailed = json.loads((result_dir / "detailed_return.json").read_text(encoding="utf-8"))
            self.assertEqual(detailed["replay_granularity"], "daily")

    def test_default_template_strategy_can_be_overridden_with_direct_trades(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp), with_strategy=False)
            (ctx.paths.agent_output / "main.py").write_text(TEMPLATE_CANDIDATE_WITH_ROW, encoding="utf-8")

            summary = BacktestTool(ctx).run(mode="valid")

            self.assertEqual(summary["status"], "ok")
            result_dir = Path(summary["result_path"])
            metadata = json.loads((result_dir / "strategy_metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["stage"], "template_override")
            intents = pd.read_parquet(result_dir / "trade_intents.parquet")
            self.assertEqual(intents.loc[0, "trade_strategy"], "buy_hold")

    def test_strategy_weight_order_updates_intra_bar_position_view(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            main = '''
import pandas as pd


def buy_once(ctx):
    if ctx.stock.position == 0:
        ctx.broker.buy(weight=0.1, reason="first_weight_buy")
    if ctx.stock.position == 0:
        ctx.broker.buy(weight=0.1, reason="duplicate_weight_buy")


def run_strategy(context):
    return pd.DataFrame([{"ts_code": "000001.SZ", "trade_strategy": "buy_once"}])
'''
            (ctx.paths.agent_output / "main.py").write_text(main, encoding="utf-8")

            summary = BacktestTool(ctx).run(mode="valid")

            self.assertEqual(summary["status"], "ok")
            detailed = json.loads((Path(summary["result_path"]) / "detailed_return.json").read_text(encoding="utf-8"))
            actions = [
                event
                for event in detailed["broker_events"]
                if event.get("event_type") == "strategy_actions" and event.get("trade_strategy") == "buy_once"
            ]
            self.assertEqual(len(actions), 1)
            self.assertEqual(actions[0]["actions"], [{"action": "buy", "weight": 0.1, "reason": "first_weight_buy"}])
            self.assertEqual(summary["order_count"], 1)

    def test_strategy_program_cannot_read_artifacts_even_with_constructed_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            main = f'''
from pathlib import Path

import pandas as pd


def run_strategy(context):
    forbidden = Path(str(context["snapshot_dir"])).parent / "artifacts" / "run_manifest.json"
    open(forbidden, "r", encoding="utf-8").read()
    return [{{"ts_code": "000001.SZ", "trade_strategy": "example_build_once"}}]
'''
            (ctx.paths.agent_output / "main.py").write_text(main, encoding="utf-8")
            with self.assertRaisesRegex(ToolError, "forbidden path"):
                BacktestTool(ctx).run(mode="valid")

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink not available on this platform")
    def test_modification_check_returns_structured_failure_for_invalid_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            main_py = ctx.paths.agent_output / "main.py"
            main_py.unlink()
            main_py.symlink_to(ctx.paths.agent_output / "README.md")

            check = ModificationCheckTool(ctx).run()
            self.assertFalse(check["allowed_to_backtest"])
            self.assertIsNone(check["artifact_hash"])
            self.assertTrue(any("symlink" in reason for reason in check["reasons"]))

    def test_frozen_eval_requires_frozen_phase_and_unchanged_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            sandbox, ctx = build_sandbox(Path(tmp))
            ctx.proxy = ScriptedLLM([nl_subagent_response(), nl_subagent_response()])
            ModificationCheckTool(ctx).run()
            BacktestTool(ctx).run(mode="valid")
            with self.assertRaisesRegex(ToolError, "not available in phase"):
                BacktestTool(ctx).run(mode="frozen_eval")
            ctx.phase = "frozen"
            ctx.write_locked = True
            ctx.manifest.update(frozen_strategy_artifact_hash=artifact_hash(ctx.paths.agent_output))
            sandbox.bind_snapshot_view(ctx.paths.snapshot_views / "test_decision_input")
            summary = BacktestTool(ctx).run(mode="frozen_eval", result_name="test_000")
            self.assertEqual(summary["result_name"], "test_000")

    def test_backtest_rejects_wrong_snapshot_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            sandbox, ctx = build_sandbox(Path(tmp))
            ctx.proxy = ScriptedLLM([nl_subagent_text()])
            sandbox.bind_snapshot_view(ctx.paths.snapshot_views / "test_decision_input")
            with self.assertRaisesRegex(ToolError, "does not match the pipeline record"):
                BacktestTool(ctx).run(mode="valid")

    def test_strategy_nl_call_records_audit_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            main = '''
from at_tools import nl


def run_strategy(context):
    result = nl("000001.SZ", prompt="score this fixture")
    content = result.get("content", "")
    weight = 0.1 if "positive" in content else 0.0
    return {"trade_intents": [{"ts_code": "000001.SZ", "trade_strategy": "example_build_once", "weight": weight}], "metadata": {"nl_result": result}}
'''
            (ctx.paths.agent_output / "main.py").write_text(main, encoding="utf-8")
            ctx.proxy = ScriptedLLM([nl_subagent_response()])
            summary = BacktestTool(ctx).run(mode="valid")
            self.assertEqual(summary["status"], "ok")
            nl_calls = ctx.paths.results / "valid_000" / "nl_tool" / "nl_llm_calls.jsonl"
            self.assertTrue(nl_calls.exists())

    def test_contract_check_runs_without_results_or_nl(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            summary = BacktestTool(ctx).contract_check()
            self.assertEqual(summary["status"], "ok")
            self.assertEqual(list(ctx.paths.results.iterdir()), [])

    def test_step_tree_records_validated_steps_when_enabled(self):
        from autotrade.environment.step_tree import StepTree

        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            ctx.manifest.update(step_tree_enabled=True)
            BacktestTool(ctx).run(mode="valid")
            BacktestTool(ctx).run(mode="valid")
            tree = StepTree(ctx.paths.steps)
            nodes = tree.nodes()
            self.assertEqual(len(nodes), 2)
            self.assertEqual(nodes[1]["parent_node_id"], nodes[0]["node_id"])
            self.assertEqual(tree.current_node_id, nodes[1]["node_id"])
            self.assertTrue((ctx.paths.steps / nodes[0]["node_id"] / "main.py").exists())
            self.assertTrue(nodes[0]["node_id"].startswith("epoch_001__fold_2022Q1__valid_"))

    def test_artifact_rejects_runtime_cache_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            cache = ctx.paths.agent_output / "__pycache__"
            cache.mkdir()
            (cache / "x.pyc").write_bytes(b"x")
            check = ModificationCheckTool(ctx).run()
            self.assertFalse(check["allowed_to_backtest"])
            self.assertTrue(any("runtime cache" in reason for reason in check["reasons"]))

    def test_failed_attempt_record_handles_invalid_artifact_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            ctx.manifest.update(step_tree_enabled=True, record_failed_attempts=True)
            cache = ctx.paths.agent_output / "__pycache__"
            cache.mkdir()
            (cache / "x.pyc").write_bytes(b"x")

            BacktestTool(ctx)._record_failure("valid", "synthetic failure")

            tree = json.loads((ctx.paths.steps / "tree.json").read_text(encoding="utf-8"))
            failed = [node for node in tree["nodes"] if node["status"] == "failed"]
            self.assertEqual(len(failed), 1)
            self.assertIsNone(failed[0]["artifact_hash"])

    def test_step_tree_disabled_records_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            BacktestTool(ctx).run(mode="valid")
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
            with self.assertRaisesRegex(ToolError, "outside the sandbox"):
                SandboxShellTool(ctx).run("cat /etc/passwd")
            with self.assertRaisesRegex(ToolError, "read-only path"):
                SandboxShellTool(ctx).run(f"touch {ctx.paths.steps}/agent_write")
            SandboxShellTool(ctx).run('rg "a>b" /mnt/snapshots/train')
            shell = SandboxShellTool(ctx)
            shell._guard_paths("ls /mnt/snapshot/ 2>&1; echo '---'")
            shell._guard_paths("ls -la /mnt/artifacts 2>/dev/null")
            shell._guard_paths("python3 -c \"import os; print(os.listdir('/mnt'))\"")
            shell._guard_paths("rg -i nodes /mnt/snapshots/train")
            shell._guard_paths("cat /mnt/artifacts/run_manifest.json > /mnt/agent/workspace/manifest.copy")
            shell._guard_paths("cp /mnt/artifacts/run_manifest.json /mnt/agent/workspace/run_manifest.copy")
            with self.assertRaisesRegex(ToolError, "read-only path"):
                shell._guard_paths("echo hi > /mnt/artifacts/forbidden")
            with self.assertRaisesRegex(ToolError, "read-only path"):
                shell._guard_paths("ls /mnt/snapshot &>/mnt/artifacts/forbidden")
            with self.assertRaisesRegex(ToolError, "read-only path"):
                shell._guard_paths("true; touch /mnt/artifacts/forbidden")
            # Interpreter code is not shell syntax: Python comparisons/slices in a
            # heredoc body or a quoted ``-c`` payload must not be read as redirects
            # or paths (regression for the ``>150`` / ``[:5]`` false positives).
            shell._guard_paths(
                "cd /mnt/snapshot && python3 << 'EOF'\nfor d in xs[:5]:\n  if n > 150:\n    print(d)\nEOF"
            )
            shell._guard_paths("python3 -c \"y = a[:5]; z = b > 150; p = '/5'\"")
            with self.assertRaisesRegex(ToolError, "unmanaged sandbox path"):
                shell._guard_paths("touch stray")
            with self.assertRaisesRegex(ToolError, "unmanaged sandbox path"):
                shell._guard_paths("printf x > stray")
            with self.assertRaisesRegex(ToolError, "unmanaged sandbox path"):
                shell._guard_paths("printf x>stray")
            with self.assertRaisesRegex(ToolError, "unmanaged sandbox path"):
                shell._guard_paths("cp /mnt/artifacts/run_manifest.json stray")
            with self.assertRaisesRegex(ToolError, "read-only path"):
                SandboxShellTool(ctx).run(f"sed -i.bak s/a/b/ {ctx.paths.steps}/tree.json")
            with self.assertRaisesRegex(ToolError, "unmanaged sandbox path"):
                SandboxShellTool(ctx).run("touch /mnt/agent/workspace_evil/file")
            with self.assertRaisesRegex(ToolError, "forbidden path"):
                SandboxShellTool(ctx).run("cat /mnt/agent/workspace/../../snapshots/test/daily.parquet")
            with self.assertRaisesRegex(ToolError, "forbidden path"):
                SandboxShellTool(ctx).run("cat $PWD/../snapshots/test/daily.parquet")
            with self.assertRaisesRegex(ToolError, "forbidden runtime path"):
                SandboxShellTool(ctx).run("ls ../runtime/snapshot_views")
            with self.assertRaisesRegex(ToolError, "cannot install packages or download"):
                SandboxShellTool(ctx).run("python3 -m pip install requests")
            with self.assertRaisesRegex(ToolError, "cannot install packages or download"):
                SandboxShellTool(ctx).run("echo ok && pip install requests")
            with self.assertRaisesRegex(ToolError, "cannot install packages or download"):
                SandboxShellTool(ctx).run("true; curl https://example.test")
            with self.assertRaisesRegex(ToolError, "cannot install packages or download"):
                SandboxShellTool(ctx).run("printf ok | wget https://example.test")
            with self.assertRaisesRegex(ToolError, "cannot install packages or download"):
                SandboxShellTool(ctx).run("bash -lc 'git clone https://example.test/repo.git'")
            with self.assertRaisesRegex(ToolError, "cannot install packages or download"):
                SandboxShellTool(ctx).run("env FOO=1 curl https://example.test")
            with self.assertRaisesRegex(ToolError, "cannot install packages or download"):
                SandboxShellTool(ctx).run("true & curl https://example.test")
            with self.assertRaisesRegex(ToolError, "cannot install packages or download"):
                SandboxShellTool(ctx).run("echo $(curl https://example.test)")
            with self.assertRaisesRegex(ToolError, "cannot install packages or download"):
                SandboxShellTool(ctx).run("echo `curl https://example.test`")
            with self.assertRaisesRegex(ToolError, "cannot install packages or download"):
                SandboxShellTool(ctx).run("find /mnt/artifacts -exec curl https://example.test \\;")
            with self.assertRaisesRegex(ToolError, "cannot install packages or download"):
                SandboxShellTool(ctx).run("find /mnt/artifacts -exec sh -c 'curl https://example.test' sh {} \\;")
            events = [e for e in ctx.trace.read_events() if e["event_type"] == "shell"]
            self.assertEqual(len(events), 4)

    def test_shell_guard_error_returns_retry_hint_to_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp), with_strategy=False)
            runner = AgentSessionRunner(
                ctx,
                ScriptedLLM([]),
                AgentSessionConfig(fold_deadline_at=datetime.now(timezone.utc) + timedelta(minutes=5)),
                fold_info={},
                acceptance_rules={},
            )

            result = runner._dispatch("shell", {"action": "shell", "command": "touch stray"})

            self.assertEqual(result["observation"], "error")
            self.assertEqual(result["error_type"], "path_guard")
            self.assertIn("workspace", str(result["retry_hint"]))
            self.assertIn("stray", str(result["blocked_target"]))

    def test_shell_can_read_step_tree_from_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp), with_strategy=False)
            (ctx.paths.steps / "tree.json").write_text('{"nodes": []}', encoding="utf-8")
            read = SandboxShellTool(ctx).run(f"rg nodes {ctx.paths.steps}")
            self.assertEqual(read.exit_code, 0)
            self.assertIn("nodes", read.stdout)

    def test_shell_truncated_output_is_stored_outside_context_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp), with_strategy=False)
            result = SandboxShellTool(ctx).run("python3 -c \"print('x' * 21050)\"")
            self.assertEqual(result.exit_code, 0)
            self.assertTrue(result.stdout_truncated)
            self.assertFalse(result.stdout_capture_truncated)
            self.assertTrue(result.stdout_path)
            self.assertTrue(Path(str(result.host_stdout_path)).exists())
            self.assertEqual(len(result.stdout), 20_000)

            large = SandboxShellTool(ctx).run("python3 -c \"print('x' * 250000)\"")
            self.assertEqual(large.exit_code, 0)
            self.assertTrue(large.stdout_truncated)
            self.assertTrue(large.stdout_capture_truncated)

    def test_shell_max_output_chars_limits_inline_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp), with_strategy=False)
            shell = SandboxShellTool(ctx)
            result = shell.run("python3 -c \"print('abcdef' * 20)\"", max_output_chars=12)

            self.assertEqual(result.exit_code, 0)
            self.assertEqual(result.command_kind, "unknown")
            self.assertEqual(result.stdout, "abcdefabcdef")
            self.assertTrue(result.stdout_truncated)
            self.assertFalse(result.stdout_capture_truncated)
            self.assertTrue(result.stdout_path)
            stored = Path(str(result.host_stdout_path)).read_text(encoding="utf-8")
            self.assertIn("abcdef" * 20, stored)
            shell_events = [event for event in ctx.trace.read_events() if event["event_type"] == "shell"]
            self.assertEqual(shell_events[-1]["max_output_chars"], 12)
            self.assertEqual(shell_events[-1]["command_kind"], "unknown")
            self.assertEqual(shell_events[-1]["tool_spec"]["schema_version"], 1)
            self.assertEqual(
                shell_events[-1]["tool_spec"]["result_policy"],
                "bounded_inline_with_persisted_captured_output",
            )

            with self.assertRaisesRegex(ToolError, "max_output_chars"):
                shell.run("echo hi", max_output_chars=0)

            with self.assertRaisesRegex(ToolError, "timeout_seconds"):
                shell.run("echo hi", timeout_seconds=0)

    def test_shell_records_command_kind_for_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp), with_strategy=False)
            shell = SandboxShellTool(ctx)

            listed = shell.run("ls /mnt/agent")
            read = shell.run("cat /mnt/artifacts/run_manifest.json", max_output_chars=80)
            written = shell.run("printf hi > /mnt/agent/workspace/kind.txt")

            self.assertEqual(listed.command_kind, "list")
            self.assertEqual(read.command_kind, "read")
            self.assertEqual(written.command_kind, "write")
            events = [event for event in ctx.trace.read_events() if event["event_type"] == "shell"]
            self.assertEqual([event["command_kind"] for event in events[-3:]], ["list", "read", "write"])

    def test_shell_timeout_seconds_can_only_shorten_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp), with_strategy=False)
            result = SandboxShellTool(ctx).run(
                "python3 -c \"import time; time.sleep(2)\"",
                timeout_seconds=1,
                max_output_chars=200,
            )

            self.assertEqual(result.exit_code, 124)
            self.assertTrue(result.timed_out)
            self.assertEqual(result.timeout_seconds, 1.0)
            events = [event for event in ctx.trace.read_events() if event["event_type"] == "shell"]
            self.assertEqual(events[-1]["timeout_seconds"], 1.0)
            self.assertTrue(events[-1]["timed_out"])

    def test_shell_trace_and_large_output_files_redact_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp), with_strategy=False)
            token = "hf_" + "a" * 30
            result = SandboxShellTool(ctx).run(
                "python3 -c \"print('hf_" + "a" * 30 + "' * 1000)\""
            )
            self.assertTrue(result.stdout_truncated)
            stored = Path(str(result.host_stdout_path)).read_text(encoding="utf-8")
            self.assertNotIn(token, stored)
            self.assertIn("hf_[redacted]", stored)
            shell_events = [event for event in ctx.trace.read_events() if event["event_type"] == "shell"]
            self.assertNotIn(token, json.dumps(shell_events, ensure_ascii=False))


@unittest.skipUnless(shutil.which("rg"), "ripgrep is required for structured grep tests")
class StructuredSearchToolTest(unittest.TestCase):
    def test_grep_and_glob_are_structured_and_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp), with_strategy=False)
            (ctx.paths.workspace / "alpha.txt").write_text("alpha\nbeta\n", encoding="utf-8")
            (ctx.paths.workspace / "foo-bar.txt").write_text("alpha\n", encoding="utf-8")
            (ctx.paths.workspace / ".hidden.txt").write_text("alpha\n", encoding="utf-8")
            (ctx.paths.workspace / "nested").mkdir()
            (ctx.paths.workspace / "nested" / "gamma.json").write_text('{"key": "alpha"}', encoding="utf-8")
            (ctx.paths.agent_output / "main.py").write_text(
                "def run_strategy(context):\n    unique_output_marker = True\n    return {'trade_intents': []}\n",
                encoding="utf-8",
            )
            (ctx.paths.model_artifacts / "model.json").write_text('{"name": "agent-model"}\n', encoding="utf-8")
            ctx.paths.parent_model_artifacts.chmod(0o755)
            (ctx.paths.parent_model_artifacts / "parent.json").write_text('{"name": "parent-model"}\n', encoding="utf-8")
            (ctx.paths.parent_model_artifacts / "parent.json").chmod(0o444)
            ctx.paths.parent_model_artifacts.chmod(0o555)
            tool = StructuredSearchTool(ctx)

            files = tool.grep(pattern="alpha", root="workspace", output_mode="files")
            self.assertEqual(files["mode"], "files")
            self.assertEqual(files["num_files"], 3)
            self.assertIn("alpha.txt", files["filenames"])
            self.assertNotIn(".hidden.txt", files["filenames"])

            content = tool.grep(pattern="alpha", root="workspace", output_mode="content", glob="*.txt")
            self.assertEqual(content["mode"], "content")
            self.assertIn("alpha.txt:1:alpha", content["content"])
            self.assertIn("foo-bar.txt", content["filenames"])

            counts = tool.grep(pattern="alpha", root="workspace", output_mode="count")
            self.assertEqual(counts["page_matches"], 3)
            self.assertTrue(counts["num_matches_known"])

            listing = tool.glob(pattern="**/*.json", root="workspace")
            self.assertEqual(listing["filenames"], ["nested/gamma.json"])
            (ctx.paths.workspace / "b.py").write_text("", encoding="utf-8")
            (ctx.paths.workspace / "a.py").write_text("", encoding="utf-8")
            (ctx.paths.workspace / "nested" / "c.py").write_text("", encoding="utf-8")
            top_py = tool.glob(pattern="*.py", root="workspace")
            self.assertEqual(top_py["filenames"], ["a.py", "b.py"])
            py_page_1 = tool.glob(pattern="**/*.py", root="workspace", head_limit=2)
            py_page_2 = tool.glob(pattern="**/*.py", root="workspace", head_limit=2, offset=2)
            self.assertEqual(py_page_1["filenames"], ["a.py", "b.py"])
            self.assertEqual(py_page_2["filenames"], ["nested/c.py"])
            if hasattr(os, "symlink"):
                (ctx.paths.workspace / "loop").mkdir()
                os.symlink(ctx.paths.workspace, ctx.paths.workspace / "loop" / "self")
                symlink_listing = tool.glob(pattern="**/*.py", root="workspace", head_limit=10)
                self.assertEqual(
                    symlink_listing["filenames"],
                    ["a.py", "b.py", "nested/c.py"],
                )
            output_files = tool.grep(pattern="unique_output_marker", root="output", output_mode="files")
            self.assertEqual(output_files["filenames"], ["main.py"])
            model_files = tool.grep(pattern="agent-model", root="models", output_mode="files")
            self.assertEqual(model_files["filenames"], ["model.json"])
            parent_model_files = tool.grep(pattern="parent-model", root="parent_models", output_mode="files")
            self.assertEqual(parent_model_files["filenames"], ["parent.json"])
            listing_offset = tool.glob(pattern="**/*.json", root="workspace", offset=1)
            self.assertEqual(listing_offset["offset"], 1)
            self.assertEqual(listing_offset["filenames"], [])
            limited = tool.grep(pattern="alpha", root="workspace", output_mode="files", head_limit=1)
            self.assertEqual(limited["returned"], 1)
            self.assertTrue(limited["truncated"])
            self.assertIsNone(limited["total"])

            with self.assertRaisesRegex(ToolError, "unsupported search root"):
                tool.grep(pattern="x", root="test")
            with self.assertRaisesRegex(ToolError, "must not contain"):
                tool.glob(pattern="../*.json", root="workspace")
            with self.assertRaisesRegex(ToolError, "hidden path"):
                tool.glob(pattern=".hidden.txt", root="workspace")

            event_types = [event["event_type"] for event in ctx.trace.read_events()]
            self.assertGreaterEqual(event_types.count("grep"), 3)
            self.assertIn("glob", event_types)


class ArtifactIOToolTest(unittest.TestCase):
    def _runner(self, ctx):
        return AgentSessionRunner(
            ctx,
            ScriptedLLM([]),
            AgentSessionConfig(fold_deadline_at=datetime.now(timezone.utc) + timedelta(minutes=5)),
            fold_info={},
            acceptance_rules={},
        )

    def test_write_then_edit_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            runner = self._runner(ctx)
            written = runner._dispatch("write_file", {"root": "output", "path": "helpers/sig.py", "content": "x = 1\ny = 2\n"})
            self.assertEqual(written["observation"], "write_file")
            self.assertTrue((ctx.paths.agent_output / "helpers" / "sig.py").exists())
            edited = runner._dispatch(
                "edit_file", {"root": "output", "path": "helpers/sig.py", "old_string": "x = 1", "new_string": "x = 42"}
            )
            self.assertEqual(edited["observation"], "edit_file")
            self.assertEqual(edited["replacements"], 1)
            self.assertIn("x = 42", (ctx.paths.agent_output / "helpers" / "sig.py").read_text(encoding="utf-8"))

    def test_edit_missing_and_stale_are_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            runner = self._runner(ctx)
            miss = runner._dispatch("edit_file", {"root": "output", "path": "nope.py", "old_string": "a", "new_string": "b"})
            self.assertEqual(miss["observation"], "error")
            self.assertEqual(miss.get("error_type"), "not_found")
            runner._dispatch("write_file", {"root": "workspace", "path": "t.txt", "content": "hello world"})
            stale = runner._dispatch("edit_file", {"root": "workspace", "path": "t.txt", "old_string": "absent", "new_string": "x"})
            self.assertEqual(stale.get("error_type"), "stale")

    def test_edit_ambiguous_requires_replace_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            runner = self._runner(ctx)
            runner._dispatch("write_file", {"root": "workspace", "path": "d.txt", "content": "a\na\na"})
            amb = runner._dispatch("edit_file", {"root": "workspace", "path": "d.txt", "old_string": "a", "new_string": "b"})
            self.assertEqual(amb.get("error_type"), "ambiguous")
            ok = runner._dispatch(
                "edit_file",
                {"root": "workspace", "path": "d.txt", "old_string": "a", "new_string": "b", "replace_all": True},
            )
            self.assertEqual(ok["replacements"], 3)

    def test_write_rejects_escape_readonly_and_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            runner = self._runner(ctx)
            escape = runner._dispatch("write_file", {"root": "output", "path": "../snapshot/x.py", "content": "x"})
            self.assertEqual(escape.get("error_type"), "path_error")
            readonly = runner._dispatch("write_file", {"root": "output", "path": "README.md", "content": "x"})
            self.assertEqual(readonly.get("error_type"), "readonly")
            ctx.write_locked = True
            locked = runner._dispatch("write_file", {"root": "output", "path": "a.py", "content": "x"})
            self.assertEqual(locked["observation"], "error")

    def test_write_rejects_absolute_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            runner = self._runner(ctx)
            result = runner._dispatch(
                "write_file",
                {"root": "output", "path": "/mnt/agent/output/abs_bug.py", "content": "x = 1\n"},
            )
            self.assertEqual(result["observation"], "error")
            self.assertEqual(result.get("error_type"), "path_error")
            self.assertFalse((ctx.paths.agent_output / "mnt" / "agent" / "output" / "abs_bug.py").exists())


class AgentSessionRunnerTest(unittest.TestCase):
    def test_context_compactor_reserves_time_for_next_main_call(self):
        compact_payload = {
            "primary_request": "continue",
            "current_state": "state",
            "next_action": "next",
        }
        compact_proxy = ScriptedLLM(
            [
                ProviderResponse(
                    content=json.dumps(compact_payload),
                    provider="scripted",
                    model="compact-v0",
                )
            ]
        )
        compactor = ContextCompactor(
            compact_proxy,
            ContextCompactionConfig(
                token_threshold=1,
                min_messages=3,
                keep_recent_messages=1,
                timeout_seconds=90,
                min_remaining_seconds=60,
            ),
        )
        messages = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "x" * 1000},
            {"role": "assistant", "content": "old"},
        ]

        result = compactor.compact(messages, remaining_seconds=70, step_id="step_001")

        self.assertIsNotNone(result)
        self.assertEqual(compact_proxy.calls[0]["timeout_seconds"], 10)

    def test_context_compactor_zero_call_limit_blocks_provider_call(self):
        compact_proxy = ScriptedLLM([json.dumps({"primary_request": "should not run"})])
        compactor = ContextCompactor(
            compact_proxy,
            ContextCompactionConfig(
                token_threshold=1,
                min_messages=3,
                keep_recent_messages=1,
                max_calls=0,
                min_remaining_seconds=0,
            ),
        )
        messages = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "x" * 1000},
            {"role": "assistant", "content": "old"},
        ]

        result = compactor.compact(messages, remaining_seconds=300, step_id="step_001")

        self.assertIsNone(result)
        self.assertEqual(len(compact_proxy.calls), 0)

    def test_scripted_session_finishes_fold(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            nl_proxy_responses = [
                tool_call_response(tool_call("note", text="inspect data")),
                tool_call_response(tool_call("modification_check")),
                tool_call_response(tool_call("backtest")),
                tool_call_response(tool_call("finish_fold")),
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
            self.assertTrue(all("new_messages" in e and "content" in e for e in llm_events))

    def test_agent_and_nl_can_use_different_model_proxies(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            (ctx.paths.agent_output / "main.py").write_text(
                '''
from at_tools import nl


def run_strategy(context):
    result = nl("000001.SZ", prompt="fixture")
    weight = 0.1 if "positive" in result.get("content", "") else 0.0
    return {"trade_intents": [{"ts_code": "000001.SZ", "trade_strategy": "example_build_once", "weight": weight}]}
''',
                encoding="utf-8",
            )
            agent_proxy = ScriptedLLM(
                [
                    tool_call_response(tool_call("modification_check")),
                    tool_call_response(tool_call("backtest")),
                    tool_call_response(tool_call("finish_fold")),
                ]
            )
            agent_proxy.model = "agent-model"
            nl_proxy = ScriptedLLM([nl_subagent_text()])
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
                ctx.paths.results / "valid_000" / "nl_tool" / "nl_llm_calls.jsonl"
            ).read_text(encoding="utf-8")
            self.assertIn('"model": "nl-model"', nl_calls)

    def test_explore_subagent_returns_digest_via_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            explore_proxy = ScriptedLLM(["数据摘要：daily 有 1 行，字段含 ts_code。"])
            runner = AgentSessionRunner(
                ctx,
                ScriptedLLM([]),
                AgentSessionConfig(fold_deadline_at=datetime.now(timezone.utc) + timedelta(minutes=5)),
                fold_info={"fold_id": "fold_2022Q1"},
                acceptance_rules={},
                explore_proxy=explore_proxy,
            )
            result = runner._dispatch("explore", {"task": "inspect daily.parquet"})
            self.assertEqual(result["observation"], "explore")
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["rounds"], 1)
            self.assertEqual(result["tool_calls"], 0)
            self.assertIn("数据摘要", result["digest"])
            events = [e for e in ctx.trace.read_events() if e["event_type"] == "explore"]
            self.assertEqual(len(events), 1)
            self.assertIn("数据摘要", events[0]["digest"])
            self.assertIn("不要替主 Agent 设计最终策略", explore_proxy.calls[0]["messages"][0]["content"])

    def test_explore_salvages_digest_after_length_cutoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            # A round cut off by output length must not fail the whole task: the
            # loop should stop and force a concise final summary.
            explore_proxy = ScriptedLLM(
                [
                    LLMProxyError(
                        "deepseek request failed: DeepSeek response stopped with finish_reason=length",
                        timeout=False,
                    ),
                    "摘要：universe 约 4560 只，关键列齐全。",
                ]
            )
            runner = AgentSessionRunner(
                ctx,
                ScriptedLLM([]),
                AgentSessionConfig(fold_deadline_at=datetime.now(timezone.utc) + timedelta(minutes=5)),
                fold_info={"fold_id": "fold_x"},
                acceptance_rules={},
                explore_proxy=explore_proxy,
            )
            result = runner._dispatch("explore", {"task": "probe universe"})
            self.assertEqual(result["status"], "completed")
            self.assertIn("4560", result["digest"])

    def test_explore_subagent_runs_read_only_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            explore_proxy = ScriptedLLM(
                [
                    tool_call_response(tool_call("glob", pattern="**/*.py", root="output")),
                    "摘要：output 下找到 main.py。",
                ]
            )
            runner = AgentSessionRunner(
                ctx,
                ScriptedLLM([]),
                AgentSessionConfig(fold_deadline_at=datetime.now(timezone.utc) + timedelta(minutes=5)),
                fold_info={"fold_id": "fold_2022Q1"},
                acceptance_rules={},
                explore_proxy=explore_proxy,
            )
            result = runner._dispatch("explore", {"task": "list python files"})
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["tool_calls"], 1)
            self.assertEqual(result["rounds"], 2)
            self.assertIn("摘要", result["digest"])
            llm_events = [e for e in ctx.trace.read_events() if e["event_type"] == "explore_llm_call"]
            self.assertGreaterEqual(len(llm_events), 2)

    def test_explore_subagent_handles_parallel_grep_glob_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            (ctx.paths.workspace / "alpha.txt").write_text("alpha\n", encoding="utf-8")
            explore_proxy = ScriptedLLM(
                [
                    tool_call_response(
                        tool_call("grep", pattern="alpha", root="workspace", output_mode="files"),
                        tool_call("glob", pattern="*.txt", root="workspace"),
                    ),
                    "摘要：找到 alpha.txt。",
                ]
            )
            runner = AgentSessionRunner(
                ctx,
                ScriptedLLM([]),
                AgentSessionConfig(fold_deadline_at=datetime.now(timezone.utc) + timedelta(minutes=5)),
                fold_info={"fold_id": "fold_2022Q1"},
                acceptance_rules={},
                explore_proxy=explore_proxy,
            )

            result = runner._dispatch("explore", {"task": "parallel inspect"})

            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["tool_calls"], 2)
            tool_messages = [
                message
                for call in explore_proxy.calls[1:]
                for message in call["messages"]
                if message["role"] == "tool"
            ]
            self.assertEqual(len(tool_messages), 2)
            self.assertIn("alpha.txt", str(tool_messages))

    def test_explore_uses_fold_deadline_for_proxy_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            explore_proxy = ScriptedLLM(["摘要：ok"])
            runner = AgentSessionRunner(
                ctx,
                ScriptedLLM([]),
                AgentSessionConfig(fold_deadline_at=datetime.now(timezone.utc) + timedelta(seconds=2)),
                fold_info={"fold_id": "fold_2022Q1"},
                acceptance_rules={},
                explore_proxy=explore_proxy,
            )

            result = runner._dispatch("explore", {"task": "quick inspect"})

            self.assertEqual(result["status"], "completed")
            self.assertLessEqual(explore_proxy.calls[0]["timeout_seconds"], 2.0)

    def test_explore_search_helpers_reject_expired_deadline(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            (ctx.paths.workspace / "alpha.txt").write_text("alpha\n", encoding="utf-8")
            engine = ExploreSubAgentEngine(
                ScriptedLLM([]),
                shell=SandboxShellTool(ctx),
                search=StructuredSearchTool(ctx),
                trace=ctx.trace,
                deadline_at=datetime.now(timezone.utc) - timedelta(seconds=1),
            )

            with self.assertRaisesRegex(ToolError, "explore deadline reached"):
                engine._search_timeout()
            with self.assertRaisesRegex(ToolError, "explore deadline reached"):
                engine._search_deadline()
            with self.assertRaisesRegex(ToolError, "glob timed out"):
                StructuredSearchTool(ctx).glob(
                    pattern="*.txt", root="workspace", deadline_monotonic=time.monotonic() - 1
                )

    def test_explore_llm_error_trace_redacts_bearer_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            explore_proxy = ScriptedLLM([LLMProxyError("failed with Bearer secret-token-abc")])
            runner = AgentSessionRunner(
                ctx,
                ScriptedLLM([]),
                AgentSessionConfig(fold_deadline_at=datetime.now(timezone.utc) + timedelta(minutes=5)),
                fold_info={"fold_id": "fold_2022Q1"},
                acceptance_rules={},
                explore_proxy=explore_proxy,
            )

            result = runner._dispatch("explore", {"task": "fail"})

            self.assertEqual(result["status"], "error")
            self.assertNotIn("secret-token-abc", result["error"])
            events = [event for event in ctx.trace.read_events() if event["event_type"] == "explore_llm_call"]
            self.assertNotIn("secret-token-abc", events[0]["error"])
            self.assertIn("[redacted]", events[0]["error"])

    def test_terminal_tool_cancels_later_calls_in_same_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            (ctx.paths.workspace / "taste.md").write_text("探索低波动质量反转。\n", encoding="utf-8")
            runner = AgentSessionRunner(
                ctx,
                ScriptedLLM([]),
                AgentSessionConfig(fold_deadline_at=datetime.now(timezone.utc) + timedelta(minutes=5)),
                fold_info={},
                acceptance_rules={},
                mode="meta_learning",
            )

            results = runner._dispatch_tool_calls(
                [
                    tool_call("done"),
                    tool_call("write_file", root="workspace", path="after_done.txt", content="x"),
                ]
            )

            self.assertEqual(results[0][2]["observation"], "meta_learning_done")
            self.assertEqual(results[1][2]["observation"], "cancelled")
            self.assertEqual(results[1][2]["reason"], "terminal_tool_already_called")
            self.assertFalse((ctx.paths.workspace / "after_done.txt").exists())

    def test_meta_done_requires_written_non_empty_taste(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            runner = AgentSessionRunner(
                ctx,
                ScriptedLLM([]),
                AgentSessionConfig(fold_deadline_at=datetime.now(timezone.utc) + timedelta(minutes=5)),
                fold_info={},
                acceptance_rules={},
                mode="meta_learning",
            )
            missing = runner._dispatch("done", {})
            self.assertEqual(missing["observation"], "error")
            self.assertIn("write /mnt/agent/workspace/taste.md", missing["error"])
            (ctx.paths.workspace / "taste.md").write_text("\n", encoding="utf-8")
            empty = runner._dispatch("done", {})
            self.assertEqual(empty["observation"], "error")
            self.assertIn("non-empty", empty["error"])

            # Transferability constraints are prompt guidance, not hard regex
            # guards. The runner only enforces that Taste exists and is non-empty.
            (ctx.paths.workspace / "taste.md").write_text(
                "优先资金流向主信号。Fold_2022Q1 先做流动性过滤，验证期 2021Q4。\n", encoding="utf-8"
            )
            accepted = runner._dispatch("done", {})
            self.assertEqual(accepted["observation"], "meta_learning_done")

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

    def test_main_llm_error_trace_redacts_bearer_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            proxy = ScriptedLLM([LLMProxyError("provider failed with Bearer secret-token-abc")])
            runner = AgentSessionRunner(
                ctx,
                proxy,
                AgentSessionConfig(
                    fold_deadline_at=datetime.now(timezone.utc) + timedelta(minutes=5),
                    max_llm_calls=1,
                ),
                fold_info={},
                acceptance_rules={},
            )

            summary = runner.run()

            self.assertEqual(summary["llm_calls"], 1)
            events = [event for event in ctx.trace.read_events() if event["event_type"] == "llm_call"]
            self.assertEqual(len(events), 1)
            self.assertNotIn("secret-token-abc", events[0]["error"])
            self.assertIn("[redacted]", events[0]["error"])

    def test_runner_validates_action_schema_and_records_deadline_cancellation(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            runner = AgentSessionRunner(
                ctx,
                ScriptedLLM([]),
                AgentSessionConfig(fold_deadline_at=datetime.now(timezone.utc) - timedelta(seconds=1)),
                fold_info={},
                acceptance_rules={},
            )
            invalid = runner._dispatch("grep", {"action": "grep"})
            self.assertEqual(invalid["observation"], "error")
            self.assertIn("missing required field", invalid["error"])

            unknown = runner._dispatch("grep", {"action": "grep", "pattern": "alpha", "typo": True})
            self.assertEqual(unknown["observation"], "error")
            self.assertIn("unknown field", unknown["error"])

            cancelled = runner._dispatch("grep", {"action": "grep", "pattern": "alpha", "root": "workspace"})
            self.assertEqual(cancelled["observation"], "cancelled")
            self.assertEqual(cancelled["reason"], "fold_deadline_reached")
            events = [event for event in ctx.trace.read_events() if event["event_type"] == "tool_cancelled"]
            self.assertEqual(len(events), 1)

    def test_tool_schemas_include_actionable_field_descriptions(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            runner = AgentSessionRunner(
                ctx,
                ScriptedLLM([]),
                AgentSessionConfig(fold_deadline_at=datetime.now(timezone.utc) + timedelta(minutes=5)),
                fold_info={},
                acceptance_rules={},
            )
            specs = runner._build_action_specs()

            shell_schema = specs["shell"].to_tool_schema()
            shell_props = shell_schema["function"]["parameters"]["properties"]
            self.assertIn("stderr", shell_props["command"]["description"])
            self.assertIn("stdout/stderr", shell_props["max_output_chars"]["description"])

            grep_schema = specs["grep"].to_tool_schema()
            grep_props = grep_schema["function"]["parameters"]["properties"]
            self.assertIn("Allowlisted sandbox root", grep_props["root"]["description"])
            self.assertIn("Pagination", grep_props["offset"]["description"])

            explore_schema = specs["explore"].to_tool_schema()
            explore_props = explore_schema["function"]["parameters"]["properties"]
            self.assertIn("not final strategy design", explore_props["task"]["description"])

    def test_runner_injects_deterministic_context_summary_when_history_is_trimmed(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            proxy = ScriptedLLM(
                [
                    tool_call_response(tool_call("note", text="one")),
                    tool_call_response(tool_call("note", text="two")),
                    tool_call_response(tool_call("note", text="three")),
                    tool_call_response(tool_call("note", text="four")),
                ]
            )
            runner = AgentSessionRunner(
                ctx,
                proxy,
                AgentSessionConfig(
                    fold_deadline_at=datetime.now(timezone.utc) + timedelta(minutes=5),
                    max_llm_calls=4,
                    max_history_messages=4,
                ),
                fold_info={},
                acceptance_rules={},
            )
            runner.run()

            self.assertTrue(
                any(
                    '"observation": "context_summary"' in message["content"]
                    for call in proxy.calls[2:]
                    for message in call["messages"]
                )
            )
            events = [event for event in ctx.trace.read_events() if event["event_type"] == "context_summary"]
            self.assertTrue(events)

    def test_runner_preserves_llm_compaction_summary_during_deterministic_trim(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            runner = AgentSessionRunner(
                ctx,
                ScriptedLLM([]),
                AgentSessionConfig(
                    fold_deadline_at=datetime.now(timezone.utc) + timedelta(minutes=5),
                    max_history_messages=5,
                ),
                fold_info={},
                acceptance_rules={},
            )
            llm_summary = {
                "role": "user",
                "content": json.dumps(
                    {
                        "observation": "context_compaction",
                        "summary_kind": "llm_compact_summary",
                        "summary": {"current_state": "preserve me"},
                    }
                ),
            }
            messages = [
                {"role": "system", "content": "system"},
                llm_summary,
                {"role": "assistant", "content": "old"},
                {"role": "user", "content": "old result"},
                {"role": "assistant", "content": "new"},
                {"role": "user", "content": "new result"},
            ]

            trimmed = runner._trim(messages)

            self.assertIn(llm_summary, trimmed)
            self.assertTrue(any('"observation": "context_summary"' in message["content"] for message in trimmed))
            events = [event for event in ctx.trace.read_events() if event["event_type"] == "context_summary"]
            self.assertTrue(events[-1]["kept_llm_compaction"])

    def test_context_compaction_request_strips_runner_internal_fields(self):
        compactor = ContextCompactor(ScriptedLLM([]))
        messages = [
            {"role": "system", "content": "system", "_seq": 0},
            {"role": "user", "content": "hello", "_seq": 1},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call_x", "type": "function", "function": {"name": "note", "arguments": "{}"}}],
                "_seq": 2,
            },
            {"role": "tool", "tool_call_id": "call_x", "content": "ok", "_seq": 3},
        ]

        request = compactor._build_compact_request(messages)
        payload = json.loads(request[1]["content"])

        self.assertNotIn("_seq", json.dumps(payload, ensure_ascii=False))
        self.assertEqual(payload["messages_since_previous_summary"][0]["content"], "hello")

    def test_context_compaction_request_anchors_previous_summary(self):
        compactor = ContextCompactor(ScriptedLLM([]))
        previous = {
            "role": "user",
            "content": json.dumps(
                {
                    "observation": "context_compaction",
                    "summary_kind": "llm_compact_summary",
                    "summary": {"goal": "continue fold", "next_steps": ["run backtest"]},
                }
            ),
        }
        messages = [
            {"role": "system", "content": "system"},
            previous,
            {"role": "assistant", "content": "recent action"},
            {"role": "user", "content": "recent observation"},
        ]

        request = compactor._build_compact_request(messages)
        payload = json.loads(request[1]["content"])

        self.assertEqual(payload["previous_summary"]["goal"], "continue fold")
        self.assertNotIn("context_compaction", json.dumps(payload["messages_since_previous_summary"], ensure_ascii=False))
        self.assertIn("messages_since_previous_summary", payload)

    def test_context_edit_preserves_current_turn_tool_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            runner = AgentSessionRunner(
                ctx,
                ScriptedLLM([]),
                AgentSessionConfig(
                    fold_deadline_at=datetime.now(timezone.utc) + timedelta(minutes=5),
                    tool_result_keep_recent=2,
                    tool_result_clear_min_chars=10,
                    tool_result_clear_token_threshold=1,
                ),
                fold_info={},
                acceptance_rules={},
            )
            old_tool = {"role": "tool", "tool_call_id": "old", "content": "old-" + "x" * 50}
            messages = [{"role": "system", "content": "system"}, old_tool]
            protect_from = len(messages)
            for index in range(10):
                messages.append({"role": "tool", "tool_call_id": f"new_{index}", "content": f"new-{index}-" + "y" * 50})

            edited = runner._clear_stale_tool_results(messages, protect_from_index=protect_from)

            self.assertIn('"observation": "cleared"', str(edited[1]["content"]))
            for message in edited[protect_from:]:
                self.assertNotIn('"observation": "cleared"', str(message["content"]))

    def test_runner_compacts_long_context_with_dedicated_proxy(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            proxy = ScriptedLLM(
                [
                    tool_call_response(tool_call("note", text="x" * 3000)),
                    tool_call_response(tool_call("note", text="after compact")),
                ]
            )
            compact_payload = {
                "primary_request": "continue the fold",
                "current_state": "large note was observed",
                "user_constraints": ["preserve current strategy state"],
                "files_and_artifacts": ["/mnt/agent/output/main.py"],
                "decisions": ["use compact before the next main call"],
                "validated_results": [],
                "errors_and_fixes": [],
                "pending_tasks": ["run modification_check"],
                "next_action": "inspect current artifacts",
            }
            compact_proxy = ScriptedLLM(
                [
                    ProviderResponse(
                        content=json.dumps(compact_payload),
                        provider="scripted",
                        model="compact-v0",
                        usage={"input_tokens": 100, "output_tokens": 60},
                        response_id="compact-response-1",
                    )
                ]
            )

            runner = AgentSessionRunner(
                ctx,
                proxy,
                AgentSessionConfig(
                    fold_deadline_at=datetime.now(timezone.utc) + timedelta(minutes=5),
                    max_llm_calls=2,
                    max_history_messages=20,
                    context_compaction=ContextCompactionConfig(
                        token_threshold=100,
                        min_messages=4,
                        keep_recent_messages=2,
                        max_response_tokens=512,
                    ),
                ),
                fold_info={},
                acceptance_rules={},
                compact_proxy=compact_proxy,
            )
            summary = runner.run()

            self.assertEqual(summary["context_compactions"], 1)
            self.assertEqual(len(compact_proxy.calls), 1)
            self.assertEqual(compact_proxy.calls[0]["max_tokens"], 512)
            self.assertTrue(
                any(
                    '"summary_kind": "llm_compact_summary"' in message["content"]
                    for message in proxy.calls[1]["messages"]
                )
            )
            events = [event for event in ctx.trace.read_events() if event["event_type"] == "context_compaction"]
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["status"], "ok")
            self.assertEqual(events[0]["model"], "compact-v0")
            self.assertEqual(events[0]["usage"]["output_tokens"], 60)

    def test_runner_recomputes_deadline_after_compaction_before_main_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            proxy = ScriptedLLM(
                [
                    tool_call_response(tool_call("note", text="x" * 3000)),
                    tool_call_response(tool_call("note", text="should not run")),
                ]
            )
            compact_proxy = ScriptedLLM(
                [
                    ProviderResponse(
                        content=json.dumps({"primary_request": "continue", "current_state": "state"}),
                        provider="scripted",
                        model="compact-v0",
                    )
                ]
            )
            runner = AgentSessionRunner(
                ctx,
                proxy,
                AgentSessionConfig(
                    fold_deadline_at=datetime.now(timezone.utc) + timedelta(minutes=5),
                    max_llm_calls=2,
                    max_history_messages=20,
                    context_compaction=ContextCompactionConfig(
                        token_threshold=100,
                        min_messages=4,
                        keep_recent_messages=2,
                        min_remaining_seconds=0,
                    ),
                ),
                fold_info={},
                acceptance_rules={},
                compact_proxy=compact_proxy,
            )
            remaining_values = iter([300.0, 300.0, 300.0, 300.0, -1.0])
            runner._remaining_seconds = lambda: next(remaining_values, -1.0)  # type: ignore[method-assign]

            summary = runner.run()

            self.assertEqual(summary["finish_status"], "deadline_timeout")
            self.assertEqual(len(proxy.calls), 1)
            self.assertEqual(len(compact_proxy.calls), 1)

    def test_runner_traces_compaction_failure_and_opens_circuit(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            proxy = ScriptedLLM(
                [
                    tool_call_response(tool_call("note", text="x" * 3000)),
                    tool_call_response(tool_call("note", text="after failure")),
                    tool_call_response(tool_call("note", text="circuit should skip")),
                ]
            )
            compact_proxy = ScriptedLLM([LLMProxyError("temporary failure Bearer secret-token-abc")])
            runner = AgentSessionRunner(
                ctx,
                proxy,
                AgentSessionConfig(
                    fold_deadline_at=datetime.now(timezone.utc) + timedelta(minutes=5),
                    max_llm_calls=3,
                    max_history_messages=20,
                    context_compaction=ContextCompactionConfig(
                        token_threshold=100,
                        min_messages=4,
                        keep_recent_messages=2,
                        max_failures=1,
                        min_remaining_seconds=0,
                    ),
                ),
                fold_info={},
                acceptance_rules={},
                compact_proxy=compact_proxy,
            )

            summary = runner.run()

            self.assertEqual(summary["context_compactions"], 0)
            self.assertEqual(summary["context_compaction_calls"], 1)
            self.assertEqual(len(proxy.calls), 3)
            self.assertEqual(len(compact_proxy.calls), 1)
            events = [event for event in ctx.trace.read_events() if event["event_type"] == "context_compaction"]
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["status"], "error")
            self.assertNotIn("secret-token-abc", events[0]["error"])
            self.assertIn("[redacted]", events[0]["error"])


if __name__ == "__main__":
    unittest.main()
