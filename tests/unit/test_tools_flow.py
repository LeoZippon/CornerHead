import json
import os
import shutil
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from autotrade.agent import AgentSessionConfig, AgentSessionRunner, ContextCompactionConfig, ContextCompactor
from autotrade.environment.artifacts import ModificationConstraints, artifact_hash
from autotrade.environment.broker import BrokerProfile
from autotrade.environment.executor import DockerExecutor
from autotrade.environment.llm.proxy import (
    LLMProxyError,
    ProviderResponse,
    ScriptedLLM,
    tool_call,
    tool_call_response,
)
from autotrade.environment.explore import ExploreSubAgentEngine
from autotrade.environment.runtime import AgentTraceWriter, RunManifest
from autotrade.environment.sandbox import LocalSandbox, SandboxLifecycleFatal
from autotrade.environment.tools.base import SessionInterrupt
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


def main(ctx):
    with ctx.substep("main_tick", budget_minutes=0.5):
        if ctx.cur_time != "09:30":  # decide once on a priced bar; fills later
            return
        snapshot_dir = Path(str(ctx.snapshot_dir or os.environ.get("AT_SNAPSHOT_DIR")))
        daily = pd.read_parquet(snapshot_dir / "daily.parquet")
        code = sorted(daily["ts_code"].astype(str).unique())[0]
        if ctx.broker.position(code) == 0 and ctx.price(code) is not None:
            ctx.broker.buy(code, amount=1000, reason="direct_long")
'''

MINUTE_STRATEGY_MAIN = '''
import os
from pathlib import Path

import pandas as pd


def main(ctx):
    with ctx.substep("main_tick", budget_minutes=0.5):
        snapshot_dir = Path(str(ctx.snapshot_dir or os.environ.get("AT_SNAPSHOT_DIR")))
        daily = pd.read_parquet(snapshot_dir / "daily.parquet")
        code = sorted(daily["ts_code"].astype(str).unique())[0]
        if ctx.cur_time == "09:31" and ctx.broker.position(code) == 0 and ctx.price(code) is not None:
            ctx.broker.buy(code, amount=1000, reason="minute_close_buy")
'''

MODEL_READ_STRATEGY_MAIN = '''
import json
from pathlib import Path

import pandas as pd


def main(ctx):
    with ctx.substep("main_tick", budget_minutes=0.5):
        model_dir = Path(str(ctx.model_dir))
        params = json.loads((model_dir / "params.json").read_text(encoding="utf-8"))
        snapshot_dir = Path(str(ctx.snapshot_dir))
        daily = pd.read_parquet(snapshot_dir / "daily.parquet")
        code = sorted(daily["ts_code"].astype(str).unique())[0]
        if params.get("threshold") == 0.42 and ctx.broker.position(code) == 0 and ctx.price(code) is not None:
            ctx.broker.buy(code, amount=1000, reason="model_artifact_buy")
'''

MODEL_WRITE_STRATEGY_MAIN = '''
import json
from pathlib import Path


def main(ctx):
    with ctx.substep("main_tick", budget_minutes=0.5):
        model_dir = Path(str(ctx.model_dir))
        model_dir.mkdir(parents=True, exist_ok=True)
        (model_dir / "params.json").write_text(json.dumps({"threshold": 0.42}, sort_keys=True), encoding="utf-8")
'''

PROBE_LIFECYCLE_FEEDBACK_MAIN = '''
_REJECTED = False
_UNSUBMITTED = False


def main(ctx):
    global _REJECTED, _UNSUBMITTED
    if ctx.cur_time == "09:30" and not _REJECTED:
        _REJECTED = True
        with ctx.substep("invalid_lot", budget_minutes=0.5):
            ctx.broker.buy("000001.SZ", amount=1, reason="invalid_lot")
    if ctx.cur_time == "15:00" and not _UNSUBMITTED:
        _UNSUBMITTED = True
        with ctx.substep("too_late", budget_minutes=0.5):
            ctx.broker.buy("000001.SZ", amount=100, reason="too_late")
'''

MODEL_SUBPROCESS_WRITE_STRATEGY_MAIN = '''
import subprocess


def main(ctx):
    with ctx.substep("main_tick", budget_minutes=0.5):
        subprocess.run(
            ["/bin/sh", "-lc", f"echo x > {ctx.model_dir}/subprocess.txt"],
            check=True,
        )
'''

CUSTOM_POLICY_MAIN = '''
from trading import buy_if_dip


def main(ctx):
    buy_if_dip(ctx)
'''

CUSTOM_POLICY_TRADING = '''
import os
from pathlib import Path

import pandas as pd


def buy_if_dip(ctx):
    with ctx.substep("main_tick", budget_minutes=0.5):
        snapshot_dir = Path(str(ctx.snapshot_dir or os.environ.get("AT_SNAPSHOT_DIR")))
        daily = pd.read_parquet(snapshot_dir / "daily.parquet")
        code = sorted(daily["ts_code"].astype(str).unique())[0]
        bar = ctx.bar(code) or {}
        low = bar.get("low")
        if low is not None and float(low) <= 9.95 and ctx.broker.position(code) == 0:
            ctx.broker.buy(code, amount=1000, reason="minute_dip")
'''

INTRA_MINUTE_MAIN = '''
def main(ctx):
    with ctx.substep("main_tick", budget_minutes=0.5):
        code = "000001.SZ"
        if ctx.cur_time != "09:25":  # fixed blind submission tick; this test does not size from price
            return
        if ctx.broker.position(code) == 0:
            ctx.broker.buy(code, amount=1000, reason="first_amount_buy")
        if ctx.broker.position(code) == 0:
            ctx.broker.buy(code, amount=1000, reason="duplicate_amount_buy")
'''

NOISY_POLICY_MAIN = '''
print("main import noise")


def main(ctx):
    with ctx.substep("main_tick", budget_minutes=0.5):
        print("strategy call noise")
        code = "000001.SZ"
        if ctx.cur_time != "09:30":  # noise prints every tick; order once
            return
        if ctx.broker.position(code) == 0 and ctx.price(code) is not None:
            ctx.broker.buy(code, amount=1000, reason="noisy_buy")
'''

BROKEN_STRATEGY_MAIN = '''
def main(ctx):
    raise RuntimeError("boom before result artifacts")
'''

BROKEN_SECRET_STRATEGY_MAIN = '''
def main(ctx):
    raise RuntimeError("decision failed Authorization: Bearer secret-token-abc")
'''

PROBE_ACCOUNT_VIEW_CALL_MAIN = '''
def main(ctx):
    with ctx.substep("bad_account_view", budget_minutes=0.5):
        ctx.broker.stock()
'''

PROBE_STATE_OUTSIDE_SUBSTEP_MAIN = '''
def main(ctx):
    _ = ctx.state_dir
'''

PROBE_CUR_DATETIME_ISOFORMAT_MAIN = '''
def main(ctx):
    with ctx.substep("bad_cur_datetime", budget_minutes=0.5):
        ctx.cur_datetime.isoformat()
'''

PROBE_OTHER_STRING_ISOFORMAT_MAIN = '''
def main(ctx):
    with ctx.substep("unrelated_string", budget_minutes=0.5):
        "plain string".isoformat()
'''

PROBE_CONTEXT_PATH_STRING_MAIN = '''
def main(ctx):
    with ctx.substep("bad_context_path", budget_minutes=0.5):
        asof_dir = ctx.asof_dir
        _ = asof_dir / "daily"
'''

PROBE_OTHER_STRING_DIVISION_MAIN = '''
def main(ctx):
    with ctx.substep("unrelated_strings", budget_minutes=0.5):
        left = "plain"
        _ = left / "string"
'''

PROBE_MISSING_KEY_MAIN = '''
def main(ctx):
    with ctx.substep("bad_key", budget_minutes=0.5):
        row = {"close": 1.0}
        row["total_mv"]
'''

PROBE_UNIVERSE_PATH_MAIN = '''
from pathlib import Path

import pandas as pd


def main(ctx):
    with ctx.substep("bad_universe_path", budget_minutes=0.5):
        pd.read_parquet(Path(str(ctx.asof_dir)) / "universe")
'''

PROBE_ASOF_DATASET_PATH_MAIN = '''
from pathlib import Path


class IOException(Exception):
    pass


def main(ctx):
    with ctx.substep("bad_asof_dataset_path", budget_minutes=0.5):
        wrong = Path(str(ctx.asof_dir)) / "events.parquet"
        raise IOException('IO Error: No files found that match the pattern "' + str(wrong) + '"')
'''

PROBE_DUCKDB_ASOF_DIR_MAIN = '''
from pathlib import Path


class IOException(Exception):
    pass


def main(ctx):
    with ctx.substep("bad_duckdb_asof_dir", budget_minutes=0.5):
        wrong = Path(str(ctx.asof_dir)) / "events"
        raise IOException('IO Error: No files found that match the pattern "' + str(wrong) + '"')
'''

PROBE_IMPORT_ERROR_MAIN = '''
raise RuntimeError("import fixture failure")


def main(ctx):
    return None
'''

POLICY_SECRET_ERROR_MAIN = '''
def leak_secret(ctx):
    raise RuntimeError("provider failed Authorization: Bearer secret-token-abc")


def main(ctx):
    leak_secret(ctx)
'''

POLICY_IMPORT_SECRET_MAIN = '''
import trading  # noqa: F401


def main(ctx):
    return None
'''

POLICY_IMPORT_SECRET_TRADING = '''
raise RuntimeError("import failed Authorization: Bearer secret-token-abc")
'''

POLICY_OUTPUT_WRITE_MAIN = '''
from pathlib import Path


def main(ctx):
    Path("output/replay_mutation.txt").write_text("bad", encoding="utf-8")
'''

POLICY_CREATE_SYMLINK_MAIN = '''
from pathlib import Path


def main(ctx):
    Path("tmp_model_link").symlink_to("models", target_is_directory=True)
'''

ARTIFACT_READ_MAIN = '''
from pathlib import Path


def main(ctx):
    forbidden = Path(str(ctx.snapshot_dir)).parent / "artifacts" / "run_manifest.json"
    open(forbidden, "r", encoding="utf-8").read()
'''

NL_CALL_MAIN = '''
from at_tools import nl

_DONE = False


def main(ctx):
    with ctx.substep("main_tick", budget_minutes=0.5):
        global _DONE
        if _DONE:
            return
        _DONE = True
        result = nl("000001.SZ", prompt="score this fixture")
        content = result.get("content", "")
        code = "000001.SZ"
        should_buy = "positive" in content
        if should_buy and ctx.broker.position(code) == 0 and ctx.price(code) is not None:
            ctx.broker.buy(code, amount=1000, reason="nl_buy")
'''

EVENT_FILTER_NL_CALL_MAIN = '''
from at_tools import nl

_DONE = False


def main(ctx):
    with ctx.substep("main_tick", budget_minutes=0.5):
        global _DONE
        if _DONE:
            return
        _DONE = True
        nl(
            "000001.SZ",
            prompt="classify material litigation risk",
            event_filter={"patterns": ["重大诉讼|仲裁"], "lookback_days": 30},
            response_format={"type": "enum", "values": ["PASS", "DOWNGRADE", "REJECT"]},
        )
'''

GENERAL_NL_CALL_MAIN = '''
from at_tools import nl
from pathlib import Path

_DONE = False


def main(ctx):
    with ctx.substep("main_tick", budget_minutes=0.5):
        global _DONE
        if _DONE:
            return
        _DONE = True
        result = nl(prompt="检索当前可见文本里的市场级事件")
        state_dir = Path(ctx.state_dir)
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "nl_scope.txt").write_text(str(result.get("scope", "")), encoding="utf-8")
'''

TEMPLATE_CANDIDATE_WITH_ROW = '''
import os
from pathlib import Path

import pandas as pd


def main(ctx):
    with ctx.substep("main_tick", budget_minutes=0.5):
        if ctx.cur_time != "09:30":  # decide once on a priced bar; fills later
            return
        snapshot_dir = Path(str(ctx.snapshot_dir or os.environ.get("AT_SNAPSHOT_DIR")))
        daily = pd.read_parquet(snapshot_dir / "daily.parquet")
        code = sorted(daily["ts_code"].astype(str).unique())[0]
        if ctx.broker.position(code) == 0 and ctx.price(code) is not None:
            ctx.broker.buy(code, amount=1000, reason="template_candidate")
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
            self.assertGreater(summary["total_return"], 0.0)
            # Cost feedback surfaced for the Agent.
            self.assertIn("started_at", summary)
            self.assertIsInstance(summary["replay_wall_seconds"], float)
            self.assertGreaterEqual(summary["replayed_trade_days"], 1)
            self.assertIn("substep_runtime", summary)
            result_dir = Path(summary["result_path"])
            self.assertTrue((result_dir / "detailed_return.json").exists())
            self.assertTrue((result_dir / "orders.parquet").exists())

            with patch.object(ctx.executor, "cleanup_user_processes", wraps=ctx.executor.cleanup_user_processes) as cleanup:
                finish = FinishFoldTool(ctx).run()
            self.assertEqual(finish["status"], "fold_finished")
            cleanup.assert_called_once_with(user="agent")
            self.assertTrue(ctx.write_locked)
            self.assertEqual(ctx.paths.agent_output.stat().st_mode & 0o222, 0)
            self.assertEqual(ctx.paths.model_artifacts.stat().st_mode & 0o222, 0)
            with self.assertRaisesRegex(ToolError, "locked"):
                BacktestTool(ctx).run(mode="valid")

            event_types = {event["event_type"] for event in ctx.trace.read_events()}
            # The backtest opens an audit bracket (backtest_start) and closes it (backtest).
            self.assertLessEqual({"tool", "backtest_start", "backtest", "finish_fold"}, event_types)

    def test_finish_fold_requires_complete_validation_of_current_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            sandbox, ctx = build_sandbox(Path(tmp))

            ModificationCheckTool(ctx).run()
            with self.assertRaisesRegex(ToolError, "complete validation"):
                FinishFoldTool(ctx).run()
            self.assertFalse(ctx.write_locked)
            self.assertNotEqual(ctx.paths.agent_output.stat().st_mode & 0o222, 0)
            self.assertNotEqual(ctx.paths.model_artifacts.stat().st_mode & 0o222, 0)

            # replay_window debug passes stay non-qualifying for finishing.
            summary = BacktestTool(ctx).run(mode="valid", replay_window=2)
            self.assertFalse(summary["complete_validation"])
            with self.assertRaisesRegex(ToolError, "complete validation"):
                FinishFoldTool(ctx).run()
            self.assertFalse(ctx.write_locked)
            self.assertNotEqual(ctx.paths.agent_output.stat().st_mode & 0o222, 0)
            self.assertNotEqual(ctx.paths.model_artifacts.stat().st_mode & 0o222, 0)

            BacktestTool(ctx).run(mode="valid")
            finish = FinishFoldTool(ctx).run()
            self.assertEqual(finish["status"], "fold_finished")

    def test_modification_check_requires_parent_model_hash_when_parent_models_exist(self):
        # Audit fix: symmetric with the strategy diff base, when a parent model
        # artifact actually exists its hash must be recorded in the manifest — it may
        # not be trivially trusted against its own recomputed hash.
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            parent_models = ctx.paths.parent_model_artifacts
            parent_models.chmod(0o755)
            (parent_models / "params.json").write_text('{"threshold": 0.4}', encoding="utf-8")
            ctx.manifest.update(
                is_initial_artifact=False,
                parent_strategy_artifact_hash=artifact_hash(ctx.paths.parent_output),
                modification_constraints=ModificationConstraints(is_initial_artifact=False).to_record(),
            )
            self.assertNotIn("parent_model_artifact_hash", ctx.manifest.data)

            with self.assertRaises(KeyError) as raised:
                ModificationCheckTool(ctx).run()
            self.assertIn("parent_model_artifact_hash", str(raised.exception))

    def test_modification_check_trusts_empty_parent_models_without_manifest_hash(self):
        # The empty/absent parent models root carries the canonical empty-model hash,
        # so a non-initial fold with no parent model artifact still verifies without a
        # parent_model_artifact_hash manifest field (byte-identical to the old default).
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            ctx.manifest.update(
                is_initial_artifact=False,
                parent_strategy_artifact_hash=artifact_hash(ctx.paths.parent_output),
                modification_constraints=ModificationConstraints(is_initial_artifact=False).to_record(),
            )
            self.assertNotIn("parent_model_artifact_hash", ctx.manifest.data)

            check = ModificationCheckTool(ctx).run()
            self.assertTrue(check["allowed_to_backtest"])

    def test_preflight_tool_error_not_recorded_as_aborted(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            (ctx.paths.results / "valid_dup").mkdir(parents=True)
            # A pre-flight rejection (here: duplicate result dir) is an ordinary tool
            # error, not a backtest outcome — it must not emit an 'aborted' terminal
            # event (which would also lack a matching backtest_start bracket).
            with self.assertRaisesRegex(ToolError, "already exists") as raised:
                BacktestTool(ctx).run(mode="valid", result_name="valid_dup")
            self.assertNotIn(str(ctx.paths.root), str(raised.exception))
            self.assertIn("/mnt/artifacts/results/valid_dup", str(raised.exception))
            events = ctx.trace.read_events()
            aborted = [e for e in events if e["event_type"] == "backtest" and e.get("status") == "aborted"]
            self.assertEqual(aborted, [])

    def test_post_start_tool_error_closes_the_trace_bracket(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            # A ToolError raised AFTER backtest_start (here: the post-replay modification
            # refresh) must still emit a terminal backtest event, not leave an open bracket.
            host_error = f"refresh rejected at {ctx.paths.root}/private"
            with patch.object(
                BacktestTool, "_refresh_modification_check_after_replay", side_effect=ToolError(host_error)
            ):
                with self.assertRaisesRegex(ToolError, "refresh rejected") as raised:
                    BacktestTool(ctx).run(mode="valid")
            self.assertNotIn(str(ctx.paths.root), str(raised.exception))
            self.assertIn("/mnt/private", str(raised.exception))
            events = ctx.trace.read_events()
            self.assertTrue(any(e["event_type"] == "backtest_start" for e in events))
            terminals = [e for e in events if e["event_type"] == "backtest"]
            self.assertTrue(any(e.get("status") == "error" for e in terminals))  # bracket closed
            self.assertGreater(float(terminals[-1]["replay_wall_seconds"]), 0.0)

    def test_main_runs_and_records_orders(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            (ctx.paths.agent_output / "main.py").write_text(CUSTOM_STRATEGY_MAIN, encoding="utf-8")

            summary = BacktestTool(ctx).run(mode="valid")

            self.assertEqual(summary["status"], "ok")
            self.assertEqual(summary["order_count"], 1)
            result_dir = Path(summary["result_path"])
            self.assertTrue((result_dir / "orders.parquet").exists())
            orders = pd.read_parquet(result_dir / "orders.parquet")
            self.assertEqual(orders.loc[0, "action"], "buy")
            self.assertEqual(orders.loc[0, "status"], "filled")
            self.assertEqual(orders.loc[0, "ts_code"], "000001.SZ")
            backtest_event = [event for event in ctx.trace.read_events() if event["event_type"] == "backtest"][-1]
            self.assertNotIn("host_result_path", backtest_event)
            self.assertNotIn("host_orders_path", backtest_event)
            self.assertIn("host_exit_liquidation_count", backtest_event)
            self.assertIn("host_replay_overhead", backtest_event["phase_seconds"])

    def test_zero_order_backtest_returns_soft_diagnostics(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            (ctx.paths.agent_output / "main.py").write_text("def main(ctx):\n    pass\n", encoding="utf-8")

            summary = BacktestTool(ctx).run(mode="valid")

            self.assertEqual(summary["status"], "ok")
            self.assertTrue(summary["complete_validation"])
            self.assertEqual(summary["order_count"], 0)
            self.assertGreater(summary["decision_calls"], 0)
            self.assertEqual(summary["strategy_action_count"], 0)
            self.assertIsInstance(summary["agent_peak_rss_bytes"], int)
            self.assertTrue(any("zero orders" in warning for warning in summary["diagnostic_warnings"]))
            # Diagnostics explain but never become an Environment-side fence.
            self.assertTrue(ModificationCheckTool(ctx).run()["allowed_to_backtest"])

    def test_zero_order_links_suppressed_broad_exception_advisory(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            (ctx.paths.agent_output / "main.py").write_text(
                "def main(ctx):\n    try:\n        raise ValueError('x')\n    except Exception:\n        pass\n",
                encoding="utf-8",
            )

            summary = BacktestTool(ctx).run(mode="valid")

            self.assertEqual(summary["status"], "ok")
            self.assertTrue(any("1 处吞掉宽泛异常" in item for item in summary["diagnostic_warnings"]))
            self.assertTrue(ModificationCheckTool(ctx).run()["allowed_to_backtest"])

    def test_zero_order_links_blind_auction_price_advisory(self):
        from autotrade.environment.tools.backtest import _diagnostic_warnings

        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            (ctx.paths.agent_output / "main.py").write_text(
                """def _submit(ctx):
    price = ctx.price('000001.SZ')
    if price:
        ctx.broker.buy('000001.SZ', amount=100)

def main(ctx):
    if ctx.cur_time == '09:25':
        _submit(ctx)
""",
                encoding="utf-8",
            )

            check = ModificationCheckTool(ctx).run()
            blind = [item for item in check["advisories"] if item["kind"] == "blind_auction_price_lookup"]
            self.assertEqual(len(blind), 1)
            warnings = _diagnostic_warnings(
                {"order_count": 0, "decision_calls": 3, "strategy_action_count": 0},
                strategy_advisories=check["advisories"],
            )
            self.assertTrue(any("09:15/09:25 盲竞价" in item for item in warnings))
            self.assertTrue(check["allowed_to_backtest"])

    def test_probe_error_identity_exposes_class_and_agent_line_only(self):
        from autotrade.environment.tools.backtest import _probe_error_identity
        from autotrade.environment.tools.base import ToolError

        self.assertEqual(
            _probe_error_identity("AttributeError: 'int' object has no attribute 'get'", ValueError("x")),
            "AttributeError",
        )
        traceback_detail = (
            "Traceback (most recent call last):\n"
            '  File "/mnt/runtime/driver.py", line 10, in run\n'
            '  File "/mnt/agent/output/candidate.py", line 52, in _held\n'
            "KeyError: '20250107'\n"
        )
        identity = _probe_error_identity(traceback_detail, KeyError("20250107"))
        self.assertEqual(identity, "KeyError at candidate.py:52")
        self.assertNotIn("20250107", identity)  # message content stays host-only
        # Wrapper classes with no parsable detail yield no identity at all.
        self.assertEqual(_probe_error_identity("main policy runner failed: ", ToolError("x")), "")

    def test_all_host_liquidation_exits_trigger_exit_path_warning(self):
        from autotrade.environment.tools.backtest import _diagnostic_warnings

        warnings = _diagnostic_warnings(
            {
                "order_count": 80,
                "trade_count": 55,
                "host_exit_liquidation_count": 55,
                "strategy_exit_fill_count": 0,
                "liquidation_complete": False,
                "unliquidated_positions": [{"ts_code": "000001.SZ"}, {"ts_code": "000002.SZ"}],
            }
        )

        self.assertTrue(any("退出路径检查" in item and "host_exit_liquidation_count=55" in item for item in warnings))
        self.assertTrue(any("2 个持仓未能清仓" in item for item in warnings))
        # A strategy that actually exits on its own gets no exit-path warning.
        clean = _diagnostic_warnings(
            {
                "order_count": 80,
                "trade_count": 55,
                "host_exit_liquidation_count": 3,
                "strategy_exit_fill_count": 52,
                "liquidation_complete": True,
                "unliquidated_positions": [],
            }
        )
        self.assertFalse(any("退出路径检查" in item for item in clean))

    def test_memory_diagnostic_is_a_neutral_chinese_performance_note(self):
        from autotrade.environment.tools.backtest import _diagnostic_warnings

        warnings = _diagnostic_warnings(
            {"order_count": 1, "agent_peak_rss_bytes": int(5.3 * 1024**3)}
        )

        self.assertEqual(warnings, ["性能参考：本次回测策略进程峰值内存约 5.3 GiB，不影响验证结果。"])

    def test_modification_check_advises_without_blocking_strategy_choices(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            (ctx.paths.agent_output / "main.py").write_text(
                """import pandas as pd

def main(ctx):
    try:
        rows = pd.read_parquet(ctx.snapshot_dir / \"events.parquet\")
    except Exception:
        rows = []
""",
                encoding="utf-8",
            )

            check = ModificationCheckTool(ctx).run()

            self.assertTrue(check["allowed_to_backtest"])
            kinds = {item["kind"] for item in check["advisories"]}
            self.assertEqual(kinds, {"unprojected_parquet_read", "suppressed_broad_exception"})

    def test_modification_check_flags_unknown_position_row_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            (ctx.paths.agent_output / "main.py").write_text(
                """def main(ctx):
    rows = ctx.positions
    held = {p["ts_code"] for p in rows if float(p.get("volume", 0) or 0) > 0}
    for pos in ctx.positions:
        qty = int(pos.get("qty", 0) or 0)
        sellable = int(pos.get("sellable_quantity", 0) or 0)
        cb = pos["entry_price"]
    for order in ctx.broker.pending():
        age = order.get("age_minutes")
""",
                encoding="utf-8",
            )

            check = ModificationCheckTool(ctx).run()

            self.assertTrue(check["allowed_to_backtest"])
            flagged = [item for item in check["advisories"] if item["kind"] == "unknown_position_row_key"]
            self.assertEqual({item["message"].split("'")[1] for item in flagged}, {"volume", "qty"})
            self.assertEqual(len(flagged), 2)

    def test_strategy_stdout_does_not_corrupt_rpc(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            (ctx.paths.agent_output / "main.py").write_text(NOISY_POLICY_MAIN, encoding="utf-8")

            summary = BacktestTool(ctx).run(mode="valid")

            self.assertEqual(summary["status"], "ok")
            self.assertEqual(summary["order_count"], 1)

    def test_main_rpc_error_is_redacted(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            (ctx.paths.agent_output / "main.py").write_text(POLICY_SECRET_ERROR_MAIN, encoding="utf-8")

            with self.assertRaisesRegex(ToolError, "redacted|REDACTED") as raised:
                BacktestTool(ctx).run(mode="valid")

            self.assertNotIn("secret-token-abc", str(raised.exception))

    def test_main_import_error_is_redacted(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            (ctx.paths.agent_output / "main.py").write_text(POLICY_IMPORT_SECRET_MAIN, encoding="utf-8")
            (ctx.paths.agent_output / "trading.py").write_text(POLICY_IMPORT_SECRET_TRADING, encoding="utf-8")

            with self.assertRaisesRegex(ToolError, "redacted|REDACTED") as raised:
                BacktestTool(ctx).run(mode="valid")

            self.assertNotIn("secret-token-abc", str(raised.exception))

    def test_main_cannot_write_output_during_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            (ctx.paths.agent_output / "main.py").write_text(POLICY_OUTPUT_WRITE_MAIN, encoding="utf-8")

            with self.assertRaisesRegex(ToolError, "forbidden path") as raised:
                BacktestTool(ctx).run(mode="valid")

            self.assertIn("write", str(raised.exception))
            self.assertFalse((ctx.paths.agent_output / "replay_mutation.txt").exists())

    def test_main_cannot_create_links_during_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            (ctx.paths.agent_output / "main.py").write_text(POLICY_CREATE_SYMLINK_MAIN, encoding="utf-8")

            with self.assertRaisesRegex(ToolError, "cannot create links"):
                BacktestTool(ctx).run(mode="valid")

            self.assertFalse((ctx.paths.agent / "tmp_model_link").exists())

    def test_main_failure_stderr_is_redacted(self):
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
            self.assertGreater(errors[0]["data_load"]["host_peak_rss_bytes"], 0)

    def test_minute_replay_when_available(self):
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
                        "trade_time": "09:32",
                        "open": 10.05,
                        "high": 10.15,
                        "low": 10.0,
                        "close": 10.1,
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
            self.assertEqual(summary["replay_granularity"], "minute")
            self.assertEqual(summary["data_load"]["minute_rows"], 3)
            self.assertEqual(summary["data_load"]["minute_rows_loaded"], 3)
            self.assertGreater(summary["data_load"]["minute_partitions_loaded"], 0)
            self.assertIn("minute_prefetch_wait_seconds", summary["data_load"])
            self.assertGreater(summary["host_peak_rss_bytes"], 0)
            self.assertEqual(
                summary["host_peak_rss_bytes"],
                summary["data_load"]["host_peak_rss_bytes"],
            )
            result_dir = Path(summary["result_path"])
            detailed = json.loads((result_dir / "detailed_return.json").read_text(encoding="utf-8"))
            self.assertEqual(detailed["replay_granularity"], "minute")
            fill = [event for event in detailed["broker_events"] if event["event_type"] == "order_filled"][0]
            # Decided on 09:31, the order fills at the next bar (14:57) open (10.1).
            self.assertEqual(fill["price_label"], "minute:14:57")
            self.assertAlmostEqual(fill["price"], BrokerProfile().slipped_price(10.1, is_buy=True))

    def test_main_reads_prebuilt_model_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            (ctx.paths.agent_output / "main.py").write_text(MODEL_READ_STRATEGY_MAIN, encoding="utf-8")
            (ctx.paths.model_artifacts / "params.json").write_text(
                json.dumps({"threshold": 0.42}, sort_keys=True), encoding="utf-8"
            )

            summary = BacktestTool(ctx).run(mode="valid")

            self.assertEqual(summary["status"], "ok")
            self.assertEqual(summary["model_artifact_files"], 1)
            self.assertTrue((ctx.paths.model_artifacts / "params.json").exists())
            self.assertEqual(
                summary["model_artifact_hash"],
                ctx.manifest.get("backtest_summaries")[-1]["model_artifact_hash"],
            )
            delta_summary = summary["modification_delta_summary"]
            self.assertEqual(delta_summary["model_changed_file_count"], 1)
            self.assertEqual(delta_summary["model_total_files"], 1)
            self.assertEqual(
                delta_summary["model_total_bytes"],
                (ctx.paths.model_artifacts / "params.json").stat().st_size,
            )
            last_check = ctx.manifest.get("last_modification_check")
            self.assertEqual(last_check["model_artifact_hash"], summary["model_artifact_hash"])
            self.assertEqual(last_check["model_delta"]["changed_file_count"], 1)

            check = ModificationCheckTool(ctx).run()
            self.assertTrue(check["allowed_to_backtest"])
            self.assertEqual(check["model_delta"]["changed_file_count"], 1)
            self.assertEqual(check["model_artifact_hash"], summary["model_artifact_hash"])

            finish = FinishFoldTool(ctx).run()
            self.assertEqual(finish["status"], "fold_finished")

    def test_main_cannot_write_model_artifacts_during_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            (ctx.paths.agent_output / "main.py").write_text(MODEL_WRITE_STRATEGY_MAIN, encoding="utf-8")

            with self.assertRaisesRegex(ToolError, "formal strategy cannot write forbidden path"):
                BacktestTool(ctx).run(mode="valid")

    def test_main_subprocess_cannot_write_model_artifacts_during_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            (ctx.paths.agent_output / "main.py").write_text(
                MODEL_SUBPROCESS_WRITE_STRATEGY_MAIN, encoding="utf-8"
            )

            with self.assertRaisesRegex(ToolError, "returned non-zero exit status|Permission denied"):
                BacktestTool(ctx).run(mode="valid")

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
                    {
                        "trade_date": "20211008",
                        "ts_code": "000001.SZ",
                        "trade_time": "09:33",
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
            self.assertEqual(summary["order_count"], 1)
            result_dir = Path(summary["result_path"])
            detailed = json.loads((result_dir / "detailed_return.json").read_text(encoding="utf-8"))
            custom_events = [event for event in detailed["broker_events"] if event["event_type"] == "main_actions"]
            self.assertEqual(len(custom_events), 1)
            fill = [event for event in detailed["broker_events"] if event["event_type"] == "order_filled"][0]
            # The 09:31 dip signal fills 2 bars later (execution_lag_bars=2) at 09:33 open (10.0).
            self.assertEqual(fill["price_label"], "minute:09:33")
            self.assertAlmostEqual(fill["price"], BrokerProfile().slipped_price(10.0, is_buy=True))

    def test_empty_minute_replay_file_keeps_fixed_minute_granularity(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            pd.DataFrame(columns=["trade_date", "ts_code", "trade_time", "open", "high", "low", "close"]).to_parquet(
                ctx.paths.valid / "intraday_1min.parquet",
                index=False,
            )
            (ctx.paths.agent_output / "main.py").write_text(MINUTE_STRATEGY_MAIN, encoding="utf-8")

            summary = BacktestTool(ctx).run(mode="valid")

            self.assertEqual(summary["status"], "ok")
            self.assertEqual(summary["replay_granularity"], "minute")
            detailed = json.loads((Path(summary["result_path"]) / "detailed_return.json").read_text(encoding="utf-8"))
            self.assertEqual(detailed["replay_granularity"], "minute")

    def test_template_strategy_can_be_overridden(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp), with_strategy=False)
            (ctx.paths.agent_output / "main.py").write_text(TEMPLATE_CANDIDATE_WITH_ROW, encoding="utf-8")

            summary = BacktestTool(ctx).run(mode="valid")

            self.assertEqual(summary["status"], "ok")
            self.assertEqual(summary["order_count"], 1)

    def test_substep_actions_do_not_project_position_view(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            (ctx.paths.agent_output / "main.py").write_text(INTRA_MINUTE_MAIN, encoding="utf-8")

            summary = BacktestTool(ctx).run(mode="valid")

            self.assertEqual(summary["status"], "ok")
            detailed = json.loads((Path(summary["result_path"]) / "detailed_return.json").read_text(encoding="utf-8"))
            actions = [event for event in detailed["broker_events"] if event.get("event_type") == "main_actions"]
            self.assertEqual(len(actions), 1)
            # Broker actions created inside a substep are delayed-submit plans, so
            # ctx.broker.position() still reflects filled positions only inside the
            # same substep. Both buys are emitted and the host broker constrains them.
            self.assertEqual(len(actions[0]["actions"]), 2)
            action = actions[0]["actions"][0]
            self.assertEqual(
                {key: action.get(key) for key in ("action", "ts_code", "amount", "reason")},
                {"action": "buy", "ts_code": "000001.SZ", "amount": 1000, "reason": "first_amount_buy"},
            )
            self.assertEqual(actions[0]["actions"][1].get("reason"), "duplicate_amount_buy")
            self.assertTrue(str(action.get("order_id") or "").startswith("C"))
            self.assertEqual(action.get("submitted_time"), "09:25")
            self.assertEqual(summary["order_count"], 2)

    def test_main_cannot_read_artifacts_even_with_constructed_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            (ctx.paths.agent_output / "main.py").write_text(ARTIFACT_READ_MAIN, encoding="utf-8")
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
            sealed: list[bool] = []
            environment_progress: list[tuple[str, dict[str, object] | None]] = []
            ctx.executor.formal_seal_factory = lambda: sealed.append(True)
            ctx.extra["environment_progress_hook"] = (
                lambda stage, progress: environment_progress.append((stage, progress))
            )
            ctx.extra["environment_replay_stage"] = "frozen_test"
            public_trace_count = len(ctx.trace.read_events())
            summary = BacktestTool(ctx).run(mode="frozen_eval", result_name="test_000")
            self.assertEqual(sealed, [True])
            self.assertEqual(summary["result_name"], "test_000")
            self.assertEqual(len(ctx.trace.read_events()), public_trace_count)
            self.assertTrue(environment_progress)
            self.assertEqual(environment_progress[0][0], "frozen_test")
            self.assertEqual(environment_progress[0][1]["day_index"], 0)
            self.assertNotIn("trade_date", environment_progress[0][1])
            self.assertNotIn("orders_so_far", environment_progress[0][1])
            # Regression pin: host status/progress carries ONLY runtime keys —
            # never returns, dates, orders or NL activity of the sealed replay.
            for _, progress in environment_progress:
                if progress is not None:
                    self.assertEqual(
                        set(progress), {"day_index", "total_days", "percent", "elapsed_seconds"}
                    )
            host_manifest = json.loads(ctx.manifest.host_path.read_text(encoding="utf-8"))
            frozen = [item for item in host_manifest["backtest_summaries"] if item["mode"] == "frozen_eval"]
            self.assertEqual(len(frozen), 1)
            self.assertEqual(frozen[0]["result_name"], "test_000")

    def test_wall_caps_are_tight_for_validation_and_generous_for_final_eval(self):
        # H2: the tight per-decision/per-day wall caps bound only agent-iteration
        # validation; the final evals (frozen_eval) use a generous anti-hang
        # backstop so a load spike cannot make acceptance/held-out non-reproducible.
        from autotrade.environment.main_ctx_engine import MainPolicyRunner as RealRunner
        from autotrade.environment.main_ctx_engine import run_main_ctx_replay as real_replay

        with tempfile.TemporaryDirectory() as tmp:
            sandbox, ctx = build_sandbox(Path(tmp))
            ctx.manifest.update(
                backtest_max_seconds_per_decision=300.0,
                backtest_max_seconds_per_trading_day=900.0,
            )
            captured: dict[str, object] = {}

            def runner_spy(*args, **kwargs):
                captured["decision_cap"] = kwargs.get("timeout_seconds")
                return RealRunner(*args, **kwargs)

            def replay_spy(*args, **kwargs):
                captured["per_day_cap"] = kwargs.get("max_seconds_per_trading_day")
                return real_replay(*args, **kwargs)

            with patch("autotrade.environment.tools.backtest.MainPolicyRunner", side_effect=runner_spy), patch(
                "autotrade.environment.tools.backtest.run_main_ctx_replay", side_effect=replay_spy
            ):
                ModificationCheckTool(ctx).run()
                BacktestTool(ctx).run(mode="valid")
                self.assertEqual(captured["decision_cap"], 300.0)
                self.assertEqual(captured["per_day_cap"], 900.0)

                ctx.phase = "frozen"
                ctx.write_locked = True
                ctx.manifest.update(frozen_strategy_artifact_hash=artifact_hash(ctx.paths.agent_output))
                sandbox.bind_snapshot_view(ctx.paths.snapshot_views / "test_decision_input")
                BacktestTool(ctx).run(mode="frozen_eval", result_name="test_000")
                self.assertEqual(captured["decision_cap"], 900.0)
                self.assertEqual(captured["per_day_cap"], 2700.0)

    def test_backtest_rejects_wrong_snapshot_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            sandbox, ctx = build_sandbox(Path(tmp))
            ctx.proxy = ScriptedLLM([nl_subagent_text()])
            sandbox.bind_snapshot_view(ctx.paths.snapshot_views / "test_decision_input")
            with self.assertRaisesRegex(ToolError, "does not match the pipeline record"):
                BacktestTool(ctx).run(mode="valid")

    def test_main_nl_call_records_audit_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            (ctx.paths.agent_output / "main.py").write_text(NL_CALL_MAIN, encoding="utf-8")
            ctx.proxy = ScriptedLLM([nl_subagent_response()])
            summary = BacktestTool(ctx).run(mode="valid")
            self.assertEqual(summary["status"], "ok")
            nl_calls = ctx.paths.results / "valid_000" / "nl_tool" / "nl_llm_calls.jsonl"
            self.assertTrue(nl_calls.exists())
            activity = [
                event
                for event in ctx.trace.read_events()
                if event.get("event_type") == "backtest_activity"
            ]
            self.assertEqual([event["activity_status"] for event in activity], ["running", "finished"])
            self.assertEqual([event["nl_call_index"] for event in activity], [1, 1])
            self.assertNotIn("prompt", activity[0])
            self.assertNotIn("ts_code", activity[0])

    def test_event_filter_contract_crosses_runtime_rpc_and_skips_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            (ctx.paths.agent_output / "main.py").write_text(
                EVENT_FILTER_NL_CALL_MAIN,
                encoding="utf-8",
            )
            ctx.proxy = ScriptedLLM(["unused"])

            summary = BacktestTool(ctx).run(mode="valid")

            self.assertEqual(summary["nl_outcome_counts"], {"no_matching_evidence": 1})
            self.assertEqual(summary["nl_executed_calls"], 0)
            self.assertEqual(ctx.proxy.calls, [])
            self.assertEqual(
                (
                    summary["nl_cost"]["no_evidence_skips"],
                    summary["nl_cost"]["provider_calls"],
                    summary["nl_cost"]["retrieval_calls"],
                    summary["nl_cost"]["event_filter_calls"],
                ),
                (1, 0, 0, 1),
            )

    def test_general_nl_call_uses_runtime_rpc_and_cleans_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            (ctx.paths.agent_output / "main.py").write_text(GENERAL_NL_CALL_MAIN, encoding="utf-8")
            ctx.proxy = ScriptedLLM(["general market event summary"])
            summary = BacktestTool(ctx).run(mode="valid")
            self.assertEqual(summary["status"], "ok")
            nl_requests = ctx.paths.results / "valid_000" / "nl_tool" / "nl_requests.jsonl"
            records = [json.loads(line) for line in nl_requests.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(records[0]["result"]["scope"], "general")
            self.assertEqual(records[0]["request"]["ts_code"], "")
            self.assertFalse((ctx.paths.agent / ".runtime" / "nl_rpc").exists())

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
            # Full source snapshot plus the detailed validation results per node.
            node_dir = ctx.paths.steps / str(nodes[0]["node_id"])
            self.assertTrue((node_dir / "output" / "main.py").exists())
            self.assertTrue((node_dir / "models").is_dir())
            self.assertTrue((node_dir / "detailed_return.json").exists())
            self.assertTrue((node_dir / "style_analysis.json").exists())
            self.assertTrue((node_dir / "orders.parquet").exists())
            # The fold id is opaqued in the agent-readable step tree so the calendar
            # period (e.g. 2022Q1 = the held-out test quarter) cannot leak.
            node_id = str(nodes[0]["node_id"])
            self.assertTrue(node_id.startswith("epoch_001__fold_ref_"), node_id)
            self.assertIn("__valid_", node_id)
            self.assertNotIn("2022Q1", node_id)

    def test_fold_rerun_records_new_nodes_without_id_collision(self):
        from autotrade.environment.sandbox import link_copytree
        from autotrade.environment.step_tree import StepTree

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            _, ctx1 = build_sandbox(tmp / "first")
            ctx1.manifest.update(step_tree_enabled=True)
            self.assertEqual(BacktestTool(ctx1).run(mode="valid")["status"], "ok")
            node1 = StepTree(ctx1.paths.steps).current_node_id
            self.assertIn("__run_x__valid_000", node1)

            # Simulate a rerun of the SAME fold: fresh sandbox/run, same
            # epoch/fold ids, tree handed over exactly like _install_step_tree.
            _, ctx2 = build_sandbox(tmp / "second")
            link_copytree(ctx1.paths.steps, ctx2.paths.steps)
            ctx2.manifest.update(step_tree_enabled=True, run_id="run_y")
            summary = BacktestTool(ctx2).run(mode="valid")
            self.assertEqual(summary["status"], "ok")
            tree = StepTree(ctx2.paths.steps)
            nodes = {n["node_id"]: n for n in tree.nodes()}
            self.assertEqual(len(nodes), 2)
            node2 = tree.current_node_id
            self.assertIn("__run_y__valid_000", node2)
            # The rerun's first node chains onto the previous run's frontier.
            self.assertEqual(nodes[node2]["parent_node_id"], node1)

    def test_step_rollback_restores_snapshot_and_branches(self):
        from autotrade.environment.step_tree import StepTree
        from autotrade.environment.tools import StepRollbackTool

        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            ctx.manifest.update(step_tree_enabled=True)
            first = BacktestTool(ctx).run(mode="valid")
            self.assertEqual(first["status"], "ok")
            node1 = StepTree(ctx.paths.steps).current_node_id
            hash1 = artifact_hash(ctx.paths.agent_output)

            main_py = ctx.paths.agent_output / "main.py"
            main_py.write_text(main_py.read_text(encoding="utf-8") + "\n# variant a\n", encoding="utf-8")
            second = BacktestTool(ctx).run(mode="valid")
            self.assertEqual(second["status"], "ok")
            node2 = StepTree(ctx.paths.steps).current_node_id
            self.assertNotEqual(node1, node2)

            result = StepRollbackTool(ctx).run(node1)
            self.assertEqual(result["restored_node_id"], node1)
            self.assertEqual(result["artifact_hash"], hash1)
            self.assertEqual(artifact_hash(ctx.paths.agent_output), hash1)
            self.assertEqual(StepTree(ctx.paths.steps).current_node_id, node1)

            # The next validated backtest branches from the restored node, not node2.
            main_py.write_text(main_py.read_text(encoding="utf-8") + "\n# variant b\n", encoding="utf-8")
            third = BacktestTool(ctx).run(mode="valid")
            self.assertEqual(third["status"], "ok")
            reloaded = StepTree(ctx.paths.steps)
            nodes = {n["node_id"]: n for n in reloaded.nodes()}
            self.assertEqual(nodes[reloaded.current_node_id]["parent_node_id"], node1)
            self.assertEqual(nodes[node2]["parent_node_id"], node1)
            rollback_events = [
                event for event in ctx.trace.read_events()
                if event["event_type"] == "tool" and event.get("tool") == "step_rollback_tool"
            ]
            self.assertEqual(len(rollback_events), 1)

    def test_step_rollback_models_toggle(self):
        from autotrade.environment.step_tree import StepTree
        from autotrade.environment.tools import StepRollbackTool

        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            ctx.manifest.update(step_tree_enabled=True)
            BacktestTool(ctx).run(mode="valid")
            node1 = StepTree(ctx.paths.steps).current_node_id
            marker = ctx.paths.model_artifacts / "weights.json"
            marker.write_text('{"w": 1}', encoding="utf-8")

            StepRollbackTool(ctx).run(node1, include_models=False)
            self.assertTrue(marker.exists())

            StepRollbackTool(ctx).run(node1)
            # include_models=True restores the node's (empty) models snapshot.
            self.assertFalse(marker.exists())

    def test_step_rollback_guards(self):
        from autotrade.environment.step_tree import StepTree
        from autotrade.environment.tools import StepRollbackTool

        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            tool = StepRollbackTool(ctx)
            with self.assertRaisesRegex(ToolError, "disabled"):
                tool.run("any_node")
            ctx.manifest.update(step_tree_enabled=True)
            with self.assertRaisesRegex(ToolError, "unknown step tree node"):
                tool.run("missing_node")
            failed = StepTree(ctx.paths.steps).record_failed_attempt(
                fold_id="fold_ref_x", result_name="failed_1", error="boom"
            )
            with self.assertRaisesRegex(ToolError, "failed attempt"):
                tool.run(failed)
            ctx.write_locked = True
            with self.assertRaisesRegex(ToolError, "locked"):
                tool.run(failed)

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

    def test_replay_window_is_a_non_freezable_debug_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            ctx.manifest.update(step_tree_enabled=True)
            summary = BacktestTool(ctx).run(mode="valid", replay_window=2)
            self.assertEqual(summary["status"], "ok")
            self.assertEqual(summary["replay_window"], 2)
            self.assertFalse(summary["complete_validation"])
            # A short debug window is not a validated step, so no step-tree node.
            self.assertFalse((ctx.paths.steps / "tree.json").exists())
            # Probe responses and the agent-readable results dir carry NO
            # financial output: the probed window is the strategy's future.
            for key in ("total_return", "sharpe", "max_drawdown", "benchmark", "orders_path"):
                self.assertNotIn(key, summary)
            self.assertIn("replayed_trade_days", summary)
            self.assertEqual(summary["replayed_trade_days"], 2)
            self.assertEqual(summary["replayed_exit_days"], 1)
            self.assertEqual(summary["data_load"]["daily_rows"], 3)
            self.assertTrue(summary["probe_note"])
            result_dir = Path(summary["host_result_path"])
            self.assertEqual(list(result_dir.iterdir()), [])
            self.assertNotIn("nl_tool_dir", summary)
            self.assertNotIn("host_nl_tool_dir", summary)

    def test_probe_returns_only_safe_action_lifecycle_feedback(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            (ctx.paths.agent_output / "main.py").write_text(
                PROBE_LIFECYCLE_FEEDBACK_MAIN,
                encoding="utf-8",
            )

            summary = BacktestTool(ctx).run(mode="valid", replay_window=2)

            self.assertEqual(summary["order_count"], 1)
            self.assertEqual(summary["trade_count"], 0)
            self.assertEqual(summary["unsubmitted_action_count"], 1)
            self.assertEqual(summary["unsubmitted_action_reason_counts"], {"no_fill_bar_ahead": 1})
            self.assertEqual(summary["strategy_reject_count"], 1)
            self.assertEqual(summary["strategy_reject_category_counts"], {"request_contract": 1})
            self.assertNotIn("margin_secs_reject_count", summary)
            self.assertNotIn("max_holdings_reject_count", summary)
            self.assertTrue(any("zero trades" in warning for warning in summary["diagnostic_warnings"]))

            public_manifest = json.loads(ctx.paths.run_manifest.read_text(encoding="utf-8"))
            public_summary = public_manifest["backtest_summaries"][-1]
            self.assertEqual(public_summary["unsubmitted_action_count"], 1)
            self.assertEqual(public_summary["strategy_reject_category_counts"], {"request_contract": 1})
            self.assertNotIn("margin_secs_reject_count", public_summary)
            self.assertEqual(list(Path(summary["host_result_path"]).iterdir()), [])

    def test_probe_reject_categories_exclude_market_eligibility(self):
        from autotrade.environment.tools.backtest import _strategy_reject_category_counts

        categories = _strategy_reject_category_counts({
            "invalid_amount": 2,
            "side_mismatch:sell:short": 1,
            "max_holdings_reached": 3,
            "insufficient_cash": 4,
            "margin_secs_not_finable": 5,
            "limit_up_blocked_buy": 6,
            "broker_inventory_unavailable": 7,
            "code_not_in_universe": 8,
        })

        self.assertEqual(
            categories,
            {"account_capacity": 4, "position_contract": 3, "request_contract": 3},
        )

    def test_full_replay_keeps_raw_reject_statistics(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            (ctx.paths.agent_output / "main.py").write_text(
                PROBE_LIFECYCLE_FEEDBACK_MAIN,
                encoding="utf-8",
            )

            summary = BacktestTool(ctx).run(mode="valid")

            self.assertIn("margin_secs_reject_count", summary)
            self.assertIn("max_holdings_reject_count", summary)
            detailed = json.loads(
                (Path(summary["result_path"]) / "detailed_return.json").read_text(encoding="utf-8")
            )
            self.assertEqual(detailed["reject_counts"], {"amount_below_lot_size": 1})
            rejected = [event for event in detailed["broker_events"] if event["event_type"] == "order_rejected"]
            self.assertEqual([event["reason"] for event in rejected], ["amount_below_lot_size"])
            self.assertEqual(detailed["unsubmitted_action_reason_counts"], {"no_fill_bar_ahead": 1})

    def test_probe_daily_predicate_matches_full_read_then_filter(self):
        from autotrade.environment.tools.backtest import (
            _read_replay_daily,
            _replay_trade_dates,
        )

        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            dates = _replay_trade_dates(ctx.paths.valid / "daily.parquet")[:2]
            filtered = _read_replay_daily(ctx.paths.valid, trade_dates=dates)
            full = _read_replay_daily(ctx.paths.valid)
            expected = full[full["trade_date"].astype(str).isin(dates)].reset_index(drop=True)

            pd.testing.assert_frame_equal(filtered.reset_index(drop=True), expected)

    def test_probe_skips_legacy_null_typed_empty_auction_before_filtering(self):
        from autotrade.environment.tools.backtest import _read_replay_auction

        with tempfile.TemporaryDirectory() as tmp:
            replay_dir = Path(tmp)
            pd.DataFrame(columns=["ts_code", "trade_date", "session"]).to_parquet(
                replay_dir / "auction.parquet", index=False
            )

            loaded = _read_replay_auction(
                replay_dir,
                trade_dates=("20241008", "20241231"),
            )

            self.assertIsNone(loaded)

    def test_probe_nl_content_is_withheld_but_counts_are_returned(self):
        marker = "positive_PROBE_NL_PRIVATE_MARKER"
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            (ctx.paths.agent_output / "main.py").write_text(NL_CALL_MAIN, encoding="utf-8")
            ctx.proxy = ScriptedLLM([nl_subagent_response(stance=marker)])

            summary = BacktestTool(ctx).run(mode="valid", replay_window=2)

            self.assertEqual(summary["nl_calls"], 1)
            self.assertEqual(summary["nl_executed_calls"], 0)
            self.assertEqual(summary["nl_cache_hits"], 0)
            self.assertEqual(summary["nl_cache_misses"], 0)
            self.assertEqual(summary["nl_outcome_counts"], {"withheld_probe": 1})
            nl_cost = summary["nl_cost"]
            self.assertEqual((nl_cost["logical_calls"], nl_cost["provider_calls"]), (1, 0))
            self.assertEqual(nl_cost["max_provider_calls_per_logical_call"], 4)
            self.assertEqual(
                nl_cost["probe_projected_full_provider_call_upper_bound"],
                nl_cost["probe_projected_full_logical_calls"] * 4,
            )
            self.assertFalse(summary["runtime_representative"])
            self.assertTrue(any("不可外推完整 Valid" in warning for warning in summary["diagnostic_warnings"]))
            self.assertNotIn("nl_tool_dir", summary)
            public_summary = ctx.manifest.data["backtest_summaries"][-1]
            self.assertEqual(public_summary["nl_cost"], nl_cost)
            self.assertFalse(public_summary["runtime_representative"])
            public = json.dumps(
                {
                    "summary": summary,
                    "manifest": ctx.manifest.data,
                    "trace": ctx.trace.read_events(),
                },
                ensure_ascii=False,
                default=str,
            )
            self.assertNotIn(marker, public)
            result_dir = Path(summary["host_result_path"])
            self.assertEqual(list(result_dir.iterdir()), [])
            evidence_files = list((ctx.paths.root / "runtime" / "host_evidence").rglob("*.jsonl"))
            self.assertTrue(evidence_files)
            self.assertFalse(any(marker in path.read_text(encoding="utf-8") for path in evidence_files))

    def test_probe_nl_cannot_exfiltrate_through_successful_substep_name(self):
        marker = "PROBE_SUCCESS_SUBSTEP_EXFIL_MARKER"
        strategy = '''
from at_tools import nl

_DONE = False

def main(ctx):
    global _DONE
    if _DONE:
        return
    _DONE = True
    with ctx.substep("nl_request", budget_minutes=0.5):
        result = nl("000001.SZ", prompt="fixture")
    name = result.get("content") or "nl_withheld"
    with ctx.substep(name, budget_minutes=0.5):
        return
'''
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            (ctx.paths.agent_output / "main.py").write_text(strategy, encoding="utf-8")
            ctx.proxy = ScriptedLLM([nl_subagent_response(stance=marker)])

            summary = BacktestTool(ctx).run(mode="valid", replay_window=2)

            self.assertEqual(set(summary["substep_runtime"]), {"aggregate"})
            public = json.dumps(
                {"summary": summary, "manifest": ctx.manifest.data, "trace": ctx.trace.read_events()},
                ensure_ascii=False,
                default=str,
            )
            self.assertNotIn(marker, public)
            self.assertNotIn("nl_withheld", public)

    def test_probe_cannot_exfiltrate_nl_content_through_strategy_error(self):
        marker = "PROBE_ERROR_PRIVATE_MARKER"
        strategy = '''
from at_tools import nl

_DONE = False

def main(ctx):
    global _DONE
    with ctx.substep("probe_error", budget_minutes=0.5):
        if _DONE:
            return
        _DONE = True
        result = nl("000001.SZ", prompt="fixture")
        raise RuntimeError(result.get("content", "missing"))
'''
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            (ctx.paths.agent_output / "main.py").write_text(strategy, encoding="utf-8")
            ctx.proxy = ScriptedLLM([nl_subagent_response(stance=marker)])

            with self.assertRaisesRegex(ToolError, "raw strategy/runtime error text is host-only") as raised:
                BacktestTool(ctx).run(mode="valid", replay_window=2)

            self.assertNotIn(marker, str(raised.exception))
            public = json.dumps(
                {"manifest": ctx.manifest.data, "trace": ctx.trace.read_events()},
                ensure_ascii=False,
                default=str,
            )
            self.assertNotIn(marker, public)
            evidence = ctx.paths.root / "runtime" / "host_evidence"
            self.assertFalse(any(marker in path.read_text(encoding="utf-8") for path in evidence.rglob("*.*")))

    def test_probe_contract_errors_return_safe_repair_metadata(self):
        cases = (
            (
                PROBE_ACCOUNT_VIEW_CALL_MAIN,
                "account_view_not_callable",
                "without parentheses",
            ),
            (
                PROBE_STATE_OUTSIDE_SUBSTEP_MAIN,
                "state_dir_outside_substep",
                "inside ctx.substep",
            ),
            (
                PROBE_CUR_DATETIME_ISOFORMAT_MAIN,
                "cur_datetime_string_contract",
                "already an ISO-8601 string",
            ),
            (
                PROBE_CONTEXT_PATH_STRING_MAIN,
                "context_path_string_contract",
                "Path(str(...))",
            ),
            (
                PROBE_UNIVERSE_PATH_MAIN,
                "universe_path_mismatch",
                "universe.parquet",
            ),
            (
                PROBE_ASOF_DATASET_PATH_MAIN,
                "asof_path_mismatch",
                "without a .parquet suffix",
            ),
            (
                PROBE_DUCKDB_ASOF_DIR_MAIN,
                "duckdb_asof_glob_required",
                "*.parquet",
            ),
            (
                PROBE_IMPORT_ERROR_MAIN,
                "strategy_import_failed",
                "module-level code",
            ),
            (
                PROBE_MISSING_KEY_MAIN,
                "missing_key_in_strategy",
                "'total_mv'",
            ),
        )
        for strategy, reason, hint in cases:
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as tmp:
                _, ctx = build_sandbox(Path(tmp))
                (ctx.paths.agent_output / "main.py").write_text(strategy, encoding="utf-8")

                with self.assertRaisesRegex(ToolError, "raw strategy/runtime error text is host-only") as raised:
                    BacktestTool(ctx).run(mode="valid", replay_window=2)

                error = raised.exception
                self.assertEqual(error.error_type, "strategy_contract_error")
                self.assertEqual(error.reason, reason)
                self.assertIn(hint, error.retry_hint or "")
                summary = ctx.manifest.get("backtest_summaries", [])[-1]
                self.assertEqual(summary["error_type"], "strategy_contract_error")
                self.assertEqual(summary["reason"], reason)
                self.assertIn(hint, summary["retry_hint"])
                evidence = ctx.paths.root / "runtime" / "host_evidence"
                self.assertTrue(any(path.name == "error.txt" for path in evidence.rglob("error.txt")))

    def test_probe_unexpected_exception_detail_is_host_only(self):
        marker = "PROBE_UNEXPECTED_EXCEPTION_PRIVATE_MARKER"
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            tool = BacktestTool(ctx)

            def fail_after_start(**_kwargs):
                tool._backtest_started = True
                raise PermissionError(marker)

            with patch.object(tool, "_execute", side_effect=fail_after_start):
                with self.assertRaisesRegex(ToolError, "raw strategy/runtime error text is host-only") as raised:
                    tool.run(mode="valid", replay_window=2)

            self.assertNotIn(marker, str(raised.exception))
            self.assertEqual(raised.exception.error_type, "probe_runtime_error")
            self.assertIsNone(raised.exception.reason)
            self.assertIn("smallest failing control flow", raised.exception.retry_hint or "")
            public = json.dumps(
                {"manifest": ctx.manifest.data, "trace": ctx.trace.read_events()},
                ensure_ascii=False,
                default=str,
            )
            self.assertNotIn(marker, public)
            evidence = ctx.paths.root / "runtime" / "host_evidence"
            self.assertTrue(any(marker in path.read_text(encoding="utf-8") for path in evidence.rglob("*.*")))

    def test_probe_clock_classifier_does_not_cover_unrelated_strings(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            (ctx.paths.agent_output / "main.py").write_text(
                PROBE_OTHER_STRING_ISOFORMAT_MAIN,
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ToolError, "raw strategy/runtime error text is host-only") as raised:
                BacktestTool(ctx).run(mode="valid", replay_window=2)

            self.assertEqual(raised.exception.error_type, "probe_runtime_error")
            self.assertIsNone(raised.exception.reason)

    def test_probe_path_classifier_does_not_cover_unrelated_strings(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            (ctx.paths.agent_output / "main.py").write_text(
                PROBE_OTHER_STRING_DIVISION_MAIN,
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ToolError, "raw strategy/runtime error text is host-only") as raised:
                BacktestTool(ctx).run(mode="valid", replay_window=2)

            self.assertEqual(raised.exception.error_type, "probe_runtime_error")
            self.assertIsNone(raised.exception.reason)

    def test_probe_evidence_write_failure_keeps_public_error_fixed(self):
        marker = "PROBE_EVIDENCE_WRITE_FAILURE_PRIVATE_MARKER"
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            tool = BacktestTool(ctx)

            def fail_after_start(**_kwargs):
                tool._backtest_started = True
                raise PermissionError(marker)

            with (
                patch.object(tool, "_execute", side_effect=fail_after_start),
                patch(
                    "autotrade.environment.tools.backtest._host_evidence_dir",
                    side_effect=OSError("evidence disk unavailable"),
                ),
            ):
                with self.assertRaisesRegex(ToolError, "raw strategy/runtime error text is host-only") as raised:
                    tool.run(mode="valid", replay_window=2)

            self.assertNotIn(marker, str(raised.exception))
            public = json.dumps(
                {"manifest": ctx.manifest.data, "trace": ctx.trace.read_events()},
                ensure_ascii=False,
                default=str,
            )
            self.assertNotIn(marker, public)

    def test_session_interrupt_propagates_without_tool_error_wrapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            tool = BacktestTool(ctx)
            with patch.object(tool, "_execute", side_effect=SessionInterrupt("researcher stop")):
                with self.assertRaisesRegex(SessionInterrupt, "researcher stop"):
                    tool.run(mode="valid")


class ShellToolTest(unittest.TestCase):
    def test_shell_flags_stderr_suppression(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp), with_strategy=False)
            suppressed = SandboxShellTool(ctx).run("echo hi 2>/dev/null").to_record()
            self.assertIn("stderr_suppression_reminder", suppressed)  # advisory only, still ran
            clean = SandboxShellTool(ctx).run("echo hi").to_record()
            self.assertNotIn("stderr_suppression_reminder", clean)

    def test_shell_runs_and_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp), with_strategy=False)
            result = SandboxShellTool(ctx).run("echo hello")
            self.assertEqual(result.exit_code, 0)
            self.assertIn("hello", result.stdout)
            cwd = SandboxShellTool(ctx).run("pwd")
            self.assertEqual(Path(cwd.stdout.strip()), ctx.paths.agent)
            SandboxShellTool(ctx).run("touch workspace/ok")
            self.assertTrue((ctx.paths.workspace / "ok").exists())
            SandboxShellTool(ctx).run('rg "a>b" /mnt/snapshots/train')
            events = [e for e in ctx.trace.read_events() if e["event_type"] == "shell"]
            self.assertEqual(len(events), 4)

    def test_shell_rejects_after_write_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp), with_strategy=False)
            ctx.write_locked = True
            runner = AgentSessionRunner(
                ctx,
                ScriptedLLM([]),
                AgentSessionConfig(fold_deadline_at=datetime.now(timezone.utc) + timedelta(minutes=5)),
                fold_info={},
                acceptance_rules={},
            )

            result = runner._dispatch("shell", {"action": "shell", "command": "echo locked"})

            self.assertEqual(result["observation"], "error")
            self.assertIn("fold writes are locked", result["error"])

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
            shell_event = [event for event in ctx.trace.read_events() if event["event_type"] == "shell"][-1]
            self.assertNotIn("host_stdout_path", shell_event)

            runner = AgentSessionRunner(
                ctx,
                ScriptedLLM([]),
                AgentSessionConfig(fold_deadline_at=datetime.now(timezone.utc) + timedelta(minutes=5)),
                fold_info={},
                acceptance_rules={},
            )
            observation = runner._do_shell(
                {
                    "command": "python3 -c \"print('y' * 21050)\"",
                    "max_output_chars": 20_000,
                    "timeout_seconds": 120,
                }
            )
            self.assertNotIn("host_stdout_path", observation)

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
            written = shell.run("touch workspace/kind.txt")

            self.assertEqual(listed.command_kind, "list")
            self.assertEqual(read.command_kind, "read")
            self.assertEqual(written.command_kind, "write")
            events = [event for event in ctx.trace.read_events() if event["event_type"] == "shell"]
            self.assertEqual([event["command_kind"] for event in events[-3:]], ["list", "read", "write"])

    def test_shell_timeout_seconds_shortens_run(self):
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

    def test_shell_timeout_seconds_allows_above_default_up_to_hard_cap(self):
        from autotrade.environment.tools.shell import DEFAULT_TIMEOUT_SECONDS, MAX_TIMEOUT_SECONDS

        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp), with_strategy=False)
            shell = SandboxShellTool(ctx)
            # A heavy probe may opt above the 120s default, up to the hard cap.
            above_default = int(DEFAULT_TIMEOUT_SECONDS) + 60
            result = shell.run("echo ok", timeout_seconds=above_default)
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(result.timeout_seconds, float(above_default))
            # ... but never beyond the hard cap.
            with self.assertRaisesRegex(ToolError, "timeout_seconds"):
                shell.run("echo hi", timeout_seconds=int(MAX_TIMEOUT_SECONDS) + 1)

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
    def test_truncated_search_hides_host_storage_coordinates(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp), with_strategy=False)
            (ctx.paths.workspace / "large.txt").write_text(
                "".join(f"needle-{index}-{'x' * 80}\n" for index in range(1000)),
                encoding="utf-8",
            )
            tool = StructuredSearchTool(ctx)
            raw = tool.grep(
                pattern="needle",
                root="workspace",
                path="large.txt",
                output_mode="content",
                head_limit=1000,
            )
            self.assertTrue(raw["truncated_by_chars"])
            self.assertIn("host_grep_content_path", raw)

            runner = AgentSessionRunner(
                ctx,
                ScriptedLLM([]),
                AgentSessionConfig(fold_deadline_at=datetime.now(timezone.utc) + timedelta(minutes=5)),
                fold_info={},
                acceptance_rules={},
            )
            observation = runner._do_grep(
                {
                    "pattern": "needle",
                    "root": "workspace",
                    "path": "large.txt",
                    "glob": None,
                    "output_mode": "content",
                    "head_limit": 1000,
                    "offset": 0,
                    "timeout_seconds": 60,
                }
            )
            self.assertNotIn("host_grep_content_path", observation)
            events = [event for event in ctx.trace.read_events() if event["event_type"] == "grep"]
            self.assertTrue(events)
            self.assertNotIn("host_grep_content_path", json.dumps(events))

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

    def test_read_returns_line_numbered_paginated_and_guarded(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp), with_strategy=False)
            (ctx.paths.workspace / "f.txt").write_text("l1\nl2\nl3\nl4\n", encoding="utf-8")
            tool = StructuredSearchTool(ctx)
            full = tool.read(root="workspace", path="f.txt")
            self.assertEqual(full["line_count"], 4)
            self.assertIn("1\tl1", full["content"])  # cat -n style
            self.assertIn("4\tl4", full["content"])
            page = tool.read(root="workspace", path="f.txt", offset=1, limit=2)
            self.assertIn("2\tl2", page["content"])
            self.assertNotIn("1\tl1", page["content"])
            self.assertNotIn("4\tl4", page["content"])
            # Guards: empty path, directories, test/hidden roots/paths all blocked.
            with self.assertRaisesRegex(ToolError, "relative file path"):
                tool.read(root="workspace", path="")
            (ctx.paths.workspace / "sub").mkdir()
            with self.assertRaisesRegex(ToolError, "directory"):
                tool.read(root="workspace", path="sub")
            with self.assertRaisesRegex(ToolError, "unsupported search root"):
                tool.read(root="test", path="daily.parquet")
            with self.assertRaisesRegex(ToolError, "hidden"):
                tool.read(root="workspace", path=".secret")
            self.assertIn("read", [e["event_type"] for e in ctx.trace.read_events()])


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

    def test_sandbox_lifecycle_fatal_is_not_converted_to_agent_observation(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            runner = self._runner(ctx)
            runner._action_handlers["shell"] = lambda _args: (_ for _ in ()).throw(
                SandboxLifecycleFatal("container remains paused")
            )
            with self.assertRaisesRegex(SandboxLifecycleFatal, "container remains paused"):
                runner._dispatch("shell", {"action": "shell", "command": "true"})

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
    def test_failed_backtest_reports_consumed_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            runner = AgentSessionRunner(
                ctx,
                ScriptedLLM([]),
                AgentSessionConfig(
                    fold_deadline_at=datetime.now(timezone.utc) + timedelta(minutes=5),
                    max_backtests_per_fold=2,
                ),
                fold_info={"fold_id": "fold_2022Q1"},
                acceptance_rules={},
            )
            with patch.object(runner.backtest, "run", side_effect=ToolError("probe failed")):
                observation = runner._dispatch("backtest", {"replay_window": 2})

            self.assertEqual(observation["observation"], "error")
            self.assertEqual(observation["backtests_used"], 1)
            self.assertEqual(observation["backtests_limit"], 2)
            self.assertEqual(observation["backtests_remaining"], 1)

    def test_context_compactor_reserves_time_for_next_main_call(self):
        compact_payload = {
            "goal": "continue",
            "progress": {"in_progress": ["state"]},
            "next_steps": ["next"],
        }
        compact_proxy = ScriptedLLM(
            [
                ProviderResponse(
                    content=json.dumps(compact_payload),
                    provider="scripted",
                    model="compact-model",
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

    def test_forced_compaction_bypasses_only_the_token_threshold(self):
        compactor = ContextCompactor(
            ScriptedLLM([]),
            ContextCompactionConfig(
                token_threshold=10_000_000,
                min_messages=3,
                keep_recent_messages=1,
                min_remaining_seconds=0,
            ),
        )
        messages = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "short"},
            {"role": "assistant", "content": "old"},
            {"role": "user", "content": "latest"},
        ]
        blocked, decision = compactor.should_compact(messages, remaining_seconds=600)
        self.assertFalse(blocked)
        self.assertEqual(decision["skip_reason"], "below_token_threshold")
        forced, forced_decision = compactor.should_compact(messages, remaining_seconds=600, force=True)
        self.assertTrue(forced)
        self.assertEqual(forced_decision["trigger_reason"], "forced_context_overflow")
        # The failure circuit still applies even when forced.
        compactor._consecutive_failures = 3
        still_blocked, circuit = compactor.should_compact(messages, remaining_seconds=600, force=True)
        self.assertFalse(still_blocked)
        self.assertEqual(circuit["skip_reason"], "failure_circuit_open")

    def test_llm_failure_circuit_aborts_the_session(self):
        from autotrade.environment.llm.proxy import LLMProxyError

        class FailingProxy:
            provider = "deepseek"
            model = "m"

            def complete_tools(self, *args, **kwargs):
                raise LLMProxyError("connection refused")

        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            runner = AgentSessionRunner(
                ctx,
                FailingProxy(),
                AgentSessionConfig(fold_deadline_at=datetime.now(timezone.utc) + timedelta(minutes=5)),
                fold_info={},
                acceptance_rules={},
            )
            summary = runner.run()
            # A fast-failing provider must trip the circuit, not burn the whole
            # max_llm_calls budget one error observation at a time.
            self.assertEqual(summary["finish_status"], "llm_unavailable")
            self.assertLessEqual(int(summary.get("llm_calls") or 0), 3)

    def test_backtest_budget_reports_step_counters(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            runner = AgentSessionRunner(
                ctx,
                ScriptedLLM([]),
                AgentSessionConfig(fold_deadline_at=datetime.now(timezone.utc) + timedelta(minutes=5)),
                fold_info={},
                acceptance_rules={},
            )
            runner._accepted_steps = 2
            budget = runner._backtest_budget()
            self.assertEqual(budget["steps_used"], 2)
            self.assertEqual(budget["steps_limit"], runner.config.max_steps)
            self.assertEqual(budget["steps_remaining"], runner.config.max_steps - 2)

    def test_context_compactor_zero_call_limit_blocks_provider_call(self):
        compact_proxy = ScriptedLLM([json.dumps({"goal": "should not run"})])
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
                tool_call_response(tool_call("glob", pattern="**/*.py", root="output")),
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
            backtest_observation = next(
                json.loads(message["content"])
                for message in proxy.calls[-1]["messages"]
                if message.get("tool_call_id") == "call_backtest"
            )
            self.assertEqual(backtest_observation["backtests_used"], 1)
            self.assertEqual(backtest_observation["backtests_limit"], 30)
            self.assertEqual(backtest_observation["backtests_remaining"], 29)

    def test_step_gate_hook_holds_and_injects_directive(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            calls: list[tuple[int, dict]] = []

            def hook(step_index, summary):
                calls.append((step_index, summary))
                return "试试行业中性化残差"

            ctx.extra["step_gate_hook"] = hook
            proxy = ScriptedLLM([
                tool_call_response(tool_call("backtest")),
                tool_call_response(tool_call("finish_fold")),
            ])
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
            self.assertGreaterEqual(summary["researcher_wait_seconds"], 0.0)
            # Hook fired once with the backtest result of the formal validation.
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][0], 1)
            self.assertTrue(calls[0][1].get("complete_validation"))
            # The directive reached the model inside the backtest observation.
            rendered = json.dumps(proxy.calls[-1]["messages"], ensure_ascii=False, default=str)
            self.assertIn("researcher_step_directive", rendered)
            self.assertIn("行业中性化", rendered)

    def test_ask_user_tool_waits_for_reply_and_injects_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            questions: list[tuple[int, str]] = []

            def hook(index, question):
                questions.append((index, question))
                return "先做多因子对比，别急着上模型"

            ctx.extra["user_question_hook"] = hook
            proxy = ScriptedLLM([
                tool_call_response(tool_call("ask_user", question="探针耗时超预期：方案A缩小股票池 / 方案B降频。建议A，是否同意？")),
                tool_call_response(tool_call("backtest")),
                tool_call_response(tool_call("finish_fold")),
            ])
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
            self.assertEqual(len(questions), 1)
            self.assertEqual(questions[0][0], 1)
            self.assertIn("方案A", questions[0][1])
            rendered = json.dumps(proxy.calls[-1]["messages"], ensure_ascii=False, default=str)
            self.assertIn("researcher_reply", rendered)
            self.assertIn("多因子对比", rendered)
            events = [e for e in ctx.trace.read_events() if e["event_type"] == "ask_user"]
            self.assertEqual(len(events), 1)
            self.assertIn("先做多因子对比", str(events[0].get("reply")))

    def test_ask_user_tool_is_unattended_without_hook(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            proxy = ScriptedLLM([
                tool_call_response(tool_call("ask_user", question="没人值守时怎么办？")),
                tool_call_response(tool_call("backtest")),
                tool_call_response(tool_call("finish_fold")),
            ])
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
            rendered = json.dumps(proxy.calls[1]["messages"], ensure_ascii=False, default=str)
            self.assertIn("unattended", rendered)

    def test_agent_and_nl_can_use_different_model_proxies(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            (ctx.paths.agent_output / "main.py").write_text(
                '''
from at_tools import nl

_DONE = False


def main(ctx):
    with ctx.substep("main_tick", budget_minutes=0.5):
        global _DONE
        if _DONE:
            return
        _DONE = True
        result = nl("000001.SZ", prompt="fixture")
        code = "000001.SZ"
        should_buy = "positive" in result.get("content", "")
        if should_buy and ctx.broker.position(code) == 0 and ctx.price(code) is not None:
            ctx.broker.buy(code, amount=1000, reason="nl_buy")
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
                    tool_call_response(
                        tool_call(
                            "shell",
                            command="python3 -c \"print('x' * 100)\"",
                            max_output_chars=10,
                            timeout_seconds=60,
                        )
                    ),
                    "摘要：已核对 shell 输出。",
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
            self.assertNotIn("host_stdout_path", json.dumps(explore_proxy.calls, ensure_ascii=False))
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

    def test_explore_uses_backtest_excluded_time_credit(self):
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
            runner._excluded_backtest_seconds = 120.0

            result = runner._dispatch("explore", {"task": "quick inspect"})

            self.assertEqual(result["status"], "completed")
            self.assertGreater(explore_proxy.calls[0]["timeout_seconds"], 60.0)

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

            # A calendar year is a non-transferable leak and is a hard done-gate
            # reject (other transferability constraints stay prompt guidance).
            (ctx.paths.workspace / "taste.md").write_text(
                "优先资金流向主信号。验证期 2021Q4 先做流动性过滤。\n", encoding="utf-8"
            )
            dated = runner._dispatch("done", {})
            self.assertEqual(dated["observation"], "error")
            self.assertIn("calendar date", dated["error"])

            # A transferable Taste without any calendar year is accepted.
            (ctx.paths.workspace / "taste.md").write_text(
                "优先资金流向主信号；初始 Fold 先做流动性过滤，按季度轮动。\n", encoding="utf-8"
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

    def test_every_action_spec_has_a_dispatch_handler(self):
        # The spec registry and the dispatch handler map must stay in lock-step, so a
        # new tool cannot be registered with a schema but no handler (or vice versa).
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            runner = AgentSessionRunner(
                ctx,
                ScriptedLLM([]),
                AgentSessionConfig(fold_deadline_at=datetime.now(timezone.utc) + timedelta(minutes=5)),
                fold_info={},
                acceptance_rules={},
            )
            self.assertEqual(set(runner.action_specs), set(runner._action_handlers))
            for action, handler in runner._action_handlers.items():
                self.assertTrue(callable(handler), f"handler for {action} is not callable")

    def test_runner_injects_deterministic_context_summary_when_history_is_trimmed(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            proxy = ScriptedLLM(
                [
                    tool_call_response(tool_call("glob", pattern="one*", root="workspace")),
                    tool_call_response(tool_call("glob", pattern="two*", root="workspace")),
                    tool_call_response(tool_call("glob", pattern="three*", root="workspace")),
                    tool_call_response(tool_call("glob", pattern="four*", root="workspace")),
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

    def test_deterministic_trim_leaves_cacheable_turn_headroom(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            runner = AgentSessionRunner(
                ctx,
                ScriptedLLM([]),
                AgentSessionConfig(
                    fold_deadline_at=datetime.now(timezone.utc) + timedelta(minutes=5),
                    max_history_messages=10,
                    trim_message_headroom=4,
                    trim_token_threshold=10_000_000,
                ),
                fold_info={},
                acceptance_rules={},
            )
            messages = [{"role": "system", "content": "system"}]
            messages.extend(
                {"role": "assistant" if index % 2 == 0 else "user", "content": f"message-{index}"}
                for index in range(10)
            )

            trimmed = runner._trim(messages)

            self.assertEqual(len(trimmed), 6)
            events = [event for event in ctx.trace.read_events() if event["event_type"] == "context_summary"]
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["trim_message_headroom"], 4)
            self.assertEqual(events[0]["dropped_messages"], 5)

            expanded = [
                *trimmed,
                {"role": "assistant", "content": "next"},
                {"role": "user", "content": "next-result"},
            ]
            self.assertIs(runner._trim(expanded), expanded)
            events = [event for event in ctx.trace.read_events() if event["event_type"] == "context_summary"]
            self.assertEqual(len(events), 1)

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
                "tool_calls": [{"id": "call_x", "type": "function", "function": {"name": "glob", "arguments": "{}"}}],
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
                    tool_call_response(tool_call("glob", pattern="x" * 3000, root="workspace")),
                    tool_call_response(tool_call("glob", pattern="after_compact*", root="workspace")),
                ]
            )
            compact_payload = {
                "goal": "continue the fold",
                "constraints_and_preferences": ["preserve current strategy state"],
                "progress": {"in_progress": ["large glob request was observed"]},
                "key_decisions": ["use compact before the next main call"],
                "errors_and_fixes": [],
                "next_steps": ["run modification_check", "inspect current artifacts"],
                "critical_context": ["large glob request was observed"],
                "relevant_files": ["/mnt/agent/output/main.py"],
            }
            compact_proxy = ScriptedLLM(
                [
                    ProviderResponse(
                        content=json.dumps(compact_payload),
                        provider="scripted",
                        model="compact-model",
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
            self.assertEqual(events[0]["model"], "compact-model")
            self.assertEqual(events[0]["usage"]["output_tokens"], 60)

    def test_runner_recomputes_deadline_after_compaction_before_main_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            proxy = ScriptedLLM(
                [
                    tool_call_response(tool_call("glob", pattern="x" * 3000, root="workspace")),
                    tool_call_response(tool_call("glob", pattern="should_not_run*", root="workspace")),
                ]
            )
            compact_proxy = ScriptedLLM(
                [
                    ProviderResponse(
                        content=json.dumps({"goal": "continue", "critical_context": ["state"]}),
                        provider="scripted",
                        model="compact-model",
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
                    tool_call_response(tool_call("glob", pattern="x" * 3000, root="workspace")),
                    tool_call_response(tool_call("glob", pattern="after_failure*", root="workspace")),
                    tool_call_response(tool_call("glob", pattern="circuit_should_skip*", root="workspace")),
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
