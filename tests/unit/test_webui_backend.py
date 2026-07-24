"""HITL console backend tests: registry read-models, lifecycle guards, API routes.

No worker subprocesses, Docker, or LLM calls: worker spawn is patched out and
experiment state is synthesized on disk exactly as the orchestrator writes it.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
import zipfile
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from autotrade.environment.artifacts import artifact_hash, model_artifact_hash
from autotrade.environment.runtime import write_json_atomic
from autotrade.pipelines.hitl_state import PARAM_DEFAULTS, ControlState, read_control, write_control
from autotrade.webui.manager import ExperimentManager, ManagerError
from autotrade.webui.server import create_app
from autotrade.webui.traces import read_trace_page, read_trace_tail, trace_stats


def _write_ledger(experiment_dir: Path, records: list[dict[str, object]]) -> None:
    ledger = experiment_dir / "ledgers" / "experiment_ledger.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(
        "".join(json.dumps({"schema_version": 1, **record}) + "\n" for record in records),
        encoding="utf-8",
    )


class WebuiBackendTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo_root = Path(self._tmp.name)
        self.experiments_root = self.repo_root / "experiments"
        self.experiments_root.mkdir(parents=True)
        self._build_hitl_experiment("exp_hitl")
        self.app = create_app(self.repo_root, self.experiments_root)
        self.client = TestClient(self.app)

    # ---- fixtures ------------------------------------------------------------
    def _build_hitl_experiment(self, experiment_id: str) -> Path:
        experiment_dir = self.experiments_root / experiment_id
        hitl = experiment_dir / "hitl"
        hitl.mkdir(parents=True)
        write_json_atomic(
            hitl / "params.json",
            {
                "experiment_id": experiment_id,
                "first_test_period": "2022Q1",
                "last_test_period": "2022Q2",
                "heldout_first_period": "2023Q1",
                "heldout_last_period": "2023Q1",
                "analysis_model": "deepseek-v4-flash",
                "_created_at": "2026-07-06T00:00:00+00:00",
            },
        )
        write_control(hitl / "control.json", ControlState(mode="manual"))
        write_json_atomic(
            hitl / "status.json",
            {"schema_version": 1, "pid": 999_999_999, "state": "running_session", "session_key": "epoch_001/fold_2022Q2"},
        )
        write_json_atomic(
            hitl / "schedule.json",
            {
                "schema_version": 1,
                "epochs": 1,
                "sessions": [
                    {"key": "epoch_001/meta_learning", "kind": "meta_learning", "epoch_id": "epoch_001"},
                    {"key": "epoch_001/fold_2022Q1", "kind": "fold", "epoch_id": "epoch_001", "fold_id": "fold_2022Q1"},
                    {"key": "epoch_001/fold_2022Q2", "kind": "fold", "epoch_id": "epoch_001", "fold_id": "fold_2022Q2"},
                    {"key": "heldout", "kind": "heldout", "epoch_id": "epoch_001", "periods": []},
                ],
            },
        )
        strategy_dir = experiment_dir / "strategy_artifacts" / "epoch_001" / "strategy_epoch_001_fold_2022Q1"
        strategy_dir.mkdir(parents=True)
        (strategy_dir / "main.py").write_text("def main(ctx):\n    pass\n", encoding="utf-8")
        taste_path = experiment_dir / "meta_learning" / "epoch_001" / "taste.md"
        taste_path.parent.mkdir(parents=True)
        taste_path.write_text("fixture-taste\n", encoding="utf-8")
        _write_ledger(
            experiment_dir,
            [
                {
                    "record_type": "meta_learning",
                    "experiment_id": experiment_id,
                    "epoch_id": "epoch_001",
                    "fold_id": "epoch_001_meta_learning",
                    "run_id": "run_meta",
                    "status": "taste_only",
                    "taste_path": str(taste_path),
                },
                {
                    "record_type": "fold",
                    "experiment_id": experiment_id,
                    "epoch_id": "epoch_001",
                    "fold_id": "fold_2022Q1",
                    "run_id": "run_001",
                    "fold_status": "frozen",
                    "validation_period": "20211001..20211231",
                    "test_period": "20220101..20220331",
                    "frozen_strategy_artifact_id": "strategy_epoch_001_fold_2022Q1",
                    "frozen_strategy_artifact_hash": artifact_hash(strategy_dir),
                    "frozen_strategy_artifact_path": str(strategy_dir),
                    "frozen_model_artifact_path": None,
                    "frozen_model_artifact_hash": model_artifact_hash(strategy_dir / ".missing_models"),
                    "validation_result": {"total_return": 0.10, "sharpe": 1.0, "max_drawdown": 0.05,
                                          "long_return": 0.08, "short_return": 0.02},
                    "test_result": {"total_return": 0.20, "sharpe": 1.5, "max_drawdown": 0.04,
                                    "long_return": 0.15, "short_return": 0.05},
                    "selected_step_id": "step_001",
                    "steps": [
                        {"step_id": "step_000",
                         "validation_result_ref": str(experiment_dir / "artifacts" / "run_001" / "results" / "valid_000")},
                        {"step_id": "step_001",
                         "validation_result_ref": str(experiment_dir / "artifacts" / "run_001" / "results" / "valid_001")},
                    ],
                },
                {
                    "record_type": "heldout",
                    "experiment_id": experiment_id,
                    "epoch_id": "epoch_001",
                    "fold_id": "heldout_2023Q1",
                    "run_id": "run_heldout",
                    "test_result": {"total_return": -0.03, "sharpe": -0.2, "max_drawdown": 0.08},
                },
            ],
        )
        import pandas as pd

        orders_dir = experiment_dir / "artifacts" / "run_001" / "results" / "valid_000"
        orders_dir.mkdir(parents=True)
        pd.DataFrame(
            [
                {"order_id": "o1", "account": "stock", "ts_code": "000001.SZ", "action": "buy",
                 "requested_amount": 500, "filled_quantity": 500, "price": 10.0, "status": "filled",
                 "reject_reason": "", "decision_time": "09:32", "trade_date": "20220104"},
                {"order_id": "o2", "account": "stock", "ts_code": "000001.SZ", "action": "sell",
                 "requested_amount": 500, "filled_quantity": 500, "price": 11.0, "status": "filled",
                 "reject_reason": "", "decision_time": "10:00", "trade_date": "20220105"},
                {"order_id": "o3", "account": "credit", "ts_code": "600000.SH", "action": "buy",
                 "requested_amount": 200, "filled_quantity": 0, "price": None, "status": "rejected",
                 "reject_reason": "limit_up_blocked_buy", "decision_time": "09:33", "trade_date": "20220104"},
            ]
        ).to_parquet(orders_dir / "orders.parquet", index=False)
        trace_dir = experiment_dir / "artifacts" / "run_001"
        trace_dir.mkdir(parents=True, exist_ok=True)
        events = [
            {"event_type": "llm_call", "seq": 0, "usage": {"total_tokens": 1000, "prompt_tokens": 800, "completion_tokens": 200}},
            {"event_type": "llm_call", "seq": 1, "usage": {"total_tokens": 2000, "prompt_tokens": 1500, "completion_tokens": 500}},
            {"event_type": "shell", "seq": 2},
            {"event_type": "backtest_start", "seq": 3, "ts": "2026-07-06T00:00:03+00:00"},
            {"event_type": "backtest", "seq": 4, "replay_wall_seconds": 88.5},
            {"event_type": "backtest_start", "seq": 5, "ts": "2026-07-06T00:00:05+00:00"},
        ]
        (trace_dir / "agent_trace.jsonl").write_text(
            "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
        )
        analysis_dir = hitl / "analysis"
        analysis_dir.mkdir()
        (analysis_dir / "epoch_001__fold_2022Q1.md").write_text("## 策略逻辑概述\nok\n", encoding="utf-8")
        return experiment_dir

    # ---- schema & listing ------------------------------------------------------
    def test_frontend_assets_use_clean_urls_and_revalidate(self) -> None:
        index = self.client.get("/")
        self.assertEqual(index.status_code, 200)
        self.assertIn('src="/static/app.js"', index.text)
        self.assertNotIn("app.js?v=", index.text)
        self.assertEqual(index.headers["cache-control"], "no-store, max-age=0")
        script = self.client.get("/static/app.js")
        self.assertEqual(script.status_code, 200)
        self.assertEqual(script.headers["cache-control"], "no-store, max-age=0")
        self.assertIn("性能参考：本次回测策略进程峰值内存约", script.text)
        self.assertIn('kvRow("总耗时", foldDurationNode(detail, session))', script.text)
        self.assertIn("ACTIVE_SESSION_STATES.has(status.state)", script.text)
        self.assertIn("当前 Step 策略分析（可选，仅基于验证期证据）", script.text)
        self.assertIn("Fold 策略分析（可选，仅基于验证期证据）", script.text)
        self.assertIn("重跑本 Fold（最新完成）", script.text)
        self.assertNotIn("DeepSeek 分析", script.text)

    def test_parameter_schema_defaults_track_worker_defaults(self) -> None:
        schema = self.client.get("/api/parameter-schema").json()
        fields = {field["key"]: field for group in schema["groups"] for field in group["fields"]}
        self.assertEqual(fields["epochs"]["default"], PARAM_DEFAULTS["epochs"])
        self.assertEqual(fields["model"]["default"], PARAM_DEFAULTS["model"])
        self.assertEqual(fields["initial_control_mode"]["default"], "step")
        self.assertEqual(fields["gpu_count"]["default"], 1)
        self.assertEqual(fields["gpu_count"]["min"], 1)
        self.assertEqual(fields["gpu_count"]["max"], 4)
        for hidden in (
            "experiments_root", "work_root", "raw_dir", "fundamental_events_root",
            "fundamental_events_status", "template_dir", "local_dev",
            "tavily_api_key_env", "semantic_scholar_api_key_env",
        ):
            self.assertNotIn(hidden, fields, hidden)
        for model_field in ("model", "nl_model", "compact_model", "analysis_model"):
            self.assertNotIn("deepseek-chat", fields[model_field]["choices"])
            self.assertNotIn("deepseek-reasoner", fields[model_field]["choices"])
        visible_copy = "\n".join(
            str(field.get(key, "")) for field in fields.values() for key in ("label", "help")
        )
        self.assertNotIn("DeepSeek", visible_copy)
        self.assertNotIn("provider", visible_copy)
        self.assertEqual(fields["no_thinking"]["label"], "禁用推理模式")
        # No trade calendar under the tmp repo root: period pickers degrade to text.
        self.assertEqual(schema["period_options"], {})
        self.assertEqual(fields["first_test_period"]["type"], "string")
        # Filled per-epoch on the detail page instead of at creation.
        self.assertNotIn("meta_learning_directive", fields)
        self.assertEqual(fields["meta_learning_fold_interval"]["default"], 0)
        self.assertEqual(fields["meta_learning_fold_interval"]["min"], 0)
        self.assertEqual(fields["fold_exploration_directive"]["type"], "text")
        self.assertEqual(fields["fold_exploration_directive"]["default"], "")
        self.assertTrue(fields["fold_exploration_directive"]["wide"])
        self.assertTrue(
            {"auction_enabled", "auction_preopen_time", "auction_decision_time", "auction_close_time"}
            .isdisjoint(fields)
        )
        self.assertIn("09:15/09:25/14:57", fields["intraday_decision_minutes"]["help"])
        self.assertIn("固定交易分钟时钟", fields["include_intraday"]["help"])
        self.assertIn("激活分钟没有行情", fields["execution_lag_bars"]["help"])
        self.assertTrue(all(field.get("help") for field in fields.values()))

    def test_period_options_and_defaults_from_calendar(self) -> None:
        from autotrade.webui.params_schema import build_period_options, parameter_schema, suggest_period_defaults
        import pandas as pd

        trading_days = [day.strftime("%Y%m%d") for day in pd.date_range("2023-01-02", "2024-07-05", freq="B")]
        options = build_period_options(trading_days)
        self.assertEqual(options["year"], ["2023"])
        self.assertEqual(options["quarter"][0], "2023Q1")
        self.assertEqual(options["quarter"][-1], "2024Q2")  # ends 20240630 <= last trading day
        self.assertIn("202401", options["month"])
        self.assertNotIn("202407", options["month"])  # incomplete month excluded
        self.assertTrue(all(len(label) == 8 for label in options["week"]))
        defaults = suggest_period_defaults(options)
        quarter = defaults["quarter"]
        self.assertEqual(quarter["heldout_first_period"], "2024Q2")
        self.assertEqual(quarter["last_test_period"], "2024Q1")
        self.assertLess(quarter["first_test_period"], quarter["last_test_period"])
        # first_test never takes the very first option (its validation period
        # must also exist in the calendar).
        self.assertNotEqual(quarter["first_test_period"], options["quarter"][0])
        schema = parameter_schema(trading_days=trading_days)
        fields = {field["key"]: field for group in schema["groups"] for field in group["fields"]}
        self.assertEqual(fields["first_test_period"]["type"], "period")
        self.assertEqual(fields["heldout_first_period"]["default"], "2024Q2")

    def _reveal(self, experiment_id: str = "exp_hitl") -> None:
        response = self.client.post(
            f"/api/experiments/{experiment_id}/control", json={"action": "reveal_test_results"}
        )
        self.assertEqual(response.status_code, 200)

    def test_health_reports_loaded_and_current_source_versions(self) -> None:
        payload = self.client.get("/api/health").json()
        self.assertEqual(payload["service_code_version"], payload["repo_code_version"])
        self.assertTrue(payload["code_current"])

        with patch(
            "autotrade.webui.server.repo_code_version", side_effect=["loaded-version", "current-version"]
        ):
            stale_client = TestClient(create_app(self.repo_root, self.experiments_root))
            stale = stale_client.get("/api/health").json()
        self.assertEqual(stale["service_code_version"], "loaded-version")
        self.assertEqual(stale["repo_code_version"], "current-version")
        self.assertFalse(stale["code_current"])

    def test_list_experiments_marks_kind_state_and_metrics(self) -> None:
        payload = self.client.get("/api/experiments").json()
        by_id = {entry["experiment_id"]: entry for entry in payload["experiments"]}
        hitl = by_id["exp_hitl"]
        self.assertEqual(hitl["kind"], "hitl")
        # Recorded pid is dead -> the active state degrades to interrupted.
        self.assertEqual(hitl["state"], "interrupted")
        # P1-7: test metrics hidden until the researcher reveals (seals).
        self.assertFalse(hitl["test_revealed"])
        self.assertIsNone(hitl["metrics"]["cum_test_return"])
        self._reveal()
        payload = self.client.get("/api/experiments").json()
        hitl = {entry["experiment_id"]: entry for entry in payload["experiments"]}["exp_hitl"]
        self.assertTrue(hitl["test_revealed"])
        self.assertAlmostEqual(hitl["metrics"]["cum_test_return"], 0.20)
        self.assertEqual(hitl["folds_recorded"], 1)

    def test_heldout_completion_auto_reveals_and_seals(self) -> None:
        from autotrade.webui.registry import test_results_revealed

        experiment_dir = self.experiments_root / "exp_hitl"
        hitl = experiment_dir / "hitl"
        schedule = json.loads((hitl / "schedule.json").read_text(encoding="utf-8"))
        schedule["sessions"][-1]["periods"] = [{"label": "2023Q1"}, {"label": "2023Q2"}]
        write_json_atomic(hitl / "schedule.json", schedule)

        # Partial held-out (fixture records only 2023Q1): stays hidden so the
        # worker can still be resumed to finish the remaining periods.
        self.assertFalse(test_results_revealed(experiment_dir))
        detail = self.client.get("/api/experiments/exp_hitl").json()
        self.assertFalse(detail["test_revealed"])

        # Recording the last planned period auto-reveals without any click.
        ledger_path = experiment_dir / "ledgers" / "experiment_ledger.jsonl"
        with ledger_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "schema_version": 1,
                "record_type": "heldout", "experiment_id": "exp_hitl", "epoch_id": "epoch_001",
                "fold_id": "heldout_2023Q2", "run_id": "run_heldout_2",
                "test_result": {"total_return": 0.01, "sharpe": 0.1, "max_drawdown": 0.02},
            }) + "\n")
        self.assertTrue(test_results_revealed(experiment_dir))
        detail = self.client.get("/api/experiments/exp_hitl").json()
        self.assertTrue(detail["test_revealed"])
        listing = self.client.get("/api/experiments").json()
        entry = {item["experiment_id"]: item for item in listing["experiments"]}["exp_hitl"]
        self.assertTrue(entry["test_revealed"])
        self.assertAlmostEqual(entry["metrics"]["cum_test_return"], 0.20)

        # Auto-reveal applies the same seal as a manual reveal.
        response = self.client.post(
            "/api/experiments/exp_hitl/control",
            json={"action": "approve", "session_key": "heldout"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("封存", response.json()["detail"])

    def test_dead_question_wait_degrades_to_interrupted(self) -> None:
        write_json_atomic(
            self.experiments_root / "exp_hitl" / "hitl" / "status.json",
            {
                "schema_version": 1,
                "pid": 999_999_999,
                "state": "waiting_user_reply",
                "session_key": "epoch_001/fold_2022Q2",
            },
        )

        status = self.client.get("/api/experiments/exp_hitl/status").json()

        self.assertEqual(status["state"], "interrupted")
        self.assertFalse(status["worker_alive"])
        self.assertEqual(status["raw_status"]["state"], "waiting_user_reply")

    def test_experiment_detail_merges_schedule_and_records(self) -> None:
        detail = self.client.get("/api/experiments/exp_hitl").json()
        sessions = {session["key"]: session for session in detail["sessions"]}
        self.assertIn("record", sessions["epoch_001/fold_2022Q1"])
        self.assertNotIn("record", sessions["epoch_001/fold_2022Q2"])
        self.assertTrue(sessions["epoch_001/fold_2022Q1"]["analysis_available"])
        self.assertEqual(detail["control"]["mode"], "manual")
        self.assertEqual(self.client.get("/api/experiments/nope").status_code, 404)

    # ---- guarded fold view --------------------------------------------------------
    def test_fold_detail_separates_test_audit_from_record(self) -> None:
        detail = self.client.get("/api/experiments/exp_hitl/folds/epoch_001/fold_2022Q1").json()
        self.assertNotIn("test_result", detail["record"])
        # Hidden until revealed; revealing seals the experiment.
        self.assertEqual(detail["test_audit"], {"hidden": True})
        self._reveal()
        detail = self.client.get("/api/experiments/exp_hitl/folds/epoch_001/fold_2022Q1").json()
        self.assertEqual(detail["test_audit"]["test_result"]["total_return"], 0.20)
        # Downloads are ZIP-only: no per-file listing or file endpoint.
        self.assertNotIn("strategy_files", detail)
        self.assertTrue(detail["analysis"]["available"])

    def test_strategy_zip_contains_output_tree(self) -> None:
        response = self.client.get("/api/experiments/exp_hitl/folds/epoch_001/fold_2022Q1/strategy.zip")
        self.assertEqual(response.status_code, 200)
        archive = zipfile.ZipFile(io.BytesIO(response.content))
        self.assertEqual(archive.namelist(), ["output/main.py"])

    # ---- trace paging ----------------------------------------------------------------
    def test_trace_pagination_and_partial_tail(self) -> None:
        first = self.client.get("/api/experiments/exp_hitl/trace", params={"run_id": "run_001"}).json()
        self.assertEqual(len(first["events"]), 6)
        self.assertTrue(first["eof"])
        again = self.client.get(
            "/api/experiments/exp_hitl/trace", params={"run_id": "run_001", "offset": first["next_offset"]}
        ).json()
        self.assertEqual(again["events"], [])
        trace_path = Path(first["trace_path"])
        with trace_path.open("a", encoding="utf-8") as handle:
            handle.write('{"event_type": "partial"')  # incomplete line stays unread
        page = read_trace_page(trace_path, offset=first["next_offset"])
        self.assertEqual(page["events"], [])
        self.assertEqual(page["next_offset"], first["next_offset"])

    def test_trace_page_skips_an_oversized_event_line(self) -> None:
        # A single event line larger than the page budget must not stall the
        # pager at the same offset forever (live SSE + replay loader progress).
        path = self.experiments_root / "oversized.jsonl"
        path.write_text(
            json.dumps({"seq": 0, "payload": "x" * 4000}) + "\n" + json.dumps({"seq": 1}) + "\n",
            encoding="utf-8",
        )
        page = read_trace_page(path, offset=0, max_bytes=256)
        self.assertGreater(page["next_offset"], 0)
        self.assertIn("oversized", str(page["events"][0].get("raw")))
        follow = read_trace_page(path, offset=page["next_offset"], max_bytes=256)
        self.assertEqual([event.get("seq") for event in follow["events"]], [1])
        self.assertTrue(follow["eof"])

    def test_trace_run_id_traversal_is_rejected(self) -> None:
        outside = self.experiments_root / "exp_other" / "artifacts" / "run_evil"
        outside.mkdir(parents=True, exist_ok=True)
        (outside / "agent_trace.jsonl").write_text('{"event_type": "secret"}\n', encoding="utf-8")
        for run_id in ("../exp_other/artifacts/run_evil", "..", ".hidden", "/etc"):
            response = self.client.get("/api/experiments/exp_hitl/trace", params={"run_id": run_id})
            self.assertEqual(response.status_code, 404, run_id)

    def test_trace_tail_returns_recent_events_and_stream_offset(self) -> None:
        response = self.client.get(
            "/api/experiments/exp_hitl/trace", params={"run_id": "run_001", "tail_events": 2}
        )
        self.assertEqual(response.status_code, 200)
        tail = response.json()
        self.assertEqual([event["seq"] for event in tail["events"]], [4, 5])
        self.assertTrue(tail["history_truncated"])
        self.assertEqual(tail["next_offset"], Path(tail["trace_path"]).stat().st_size)

        with Path(tail["trace_path"]).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"event_type": "shell", "seq": 6}) + "\n")
        page = read_trace_page(Path(tail["trace_path"]), offset=tail["next_offset"])
        self.assertEqual([event["seq"] for event in page["events"]], [6])

    def test_trace_tail_discards_partial_leading_and_trailing_lines(self) -> None:
        path = self.experiments_root / "tail.jsonl"
        path.write_text(
            "".join(json.dumps({"seq": seq, "payload": "x" * 40}) + "\n" for seq in range(5))
            + '{"seq": 5',
            encoding="utf-8",
        )
        tail = read_trace_tail(path, max_events=2, max_bytes=180)
        self.assertEqual([event["seq"] for event in tail["events"]], [3, 4])
        self.assertLess(tail["next_offset"], path.stat().st_size)

    # ---- lifecycle -------------------------------------------------------------------
    def test_create_experiment_validates_and_writes_control_plane(self) -> None:
        with patch.object(ExperimentManager, "start_worker", return_value={"spawned_pid": 1}):
            response = self.client.post(
                "/api/experiments",
                json={
                    "params": {
                        "experiment_id": "exp_new",
                        "first_test_period": "2022Q1",
                        "last_test_period": "2022Q1",
                        "heldout_first_period": "2023Q1",
                        "heldout_last_period": "2023Q1",
                        "epochs": 2,
                        "fold_exploration_directive": "持续检验事件冲击沿关系网络的传播。",
                    }
                },
            )
        self.assertEqual(response.status_code, 200, response.text)
        params = json.loads((self.experiments_root / "exp_new" / "hitl" / "params.json").read_text(encoding="utf-8"))
        self.assertEqual(params["epochs"], 2)
        self.assertEqual(params["fold_exploration_directive"], "持续检验事件冲击沿关系网络的传播。")
        self.assertTrue(params["work_root"].endswith("/.runtime/sandboxes/exp_new"))
        control = json.loads(
            (self.experiments_root / "exp_new" / "hitl" / "control.json").read_text(encoding="utf-8")
        )
        self.assertEqual(control["mode"], "step")
        # Duplicate and invalid ids are rejected before touching the disk.
        duplicate = self.client.post("/api/experiments", json={"params": params})
        self.assertEqual(duplicate.status_code, 400)
        bad = self.client.post("/api/experiments", json={"params": {**params, "experiment_id": "../evil"}})
        self.assertEqual(bad.status_code, 400)
        unknown = self.client.post("/api/experiments", json={"params": {**params, "experiment_id": "x2", "bogus": 1}})
        self.assertEqual(unknown.status_code, 400)
        self.assertIn("unknown experiment parameters", unknown.json()["detail"])

    def test_create_rejected_by_running_cap_leaves_no_directory(self) -> None:
        from autotrade.webui import manager as manager_module

        with patch.object(
            ExperimentManager, "running_experiments", return_value=["a", "b", "c", "d", "e"]
        ), patch.object(manager_module, "MAX_RUNNING_EXPERIMENTS", 5):
            response = self.client.post(
                "/api/experiments",
                json={
                    "params": {
                        "experiment_id": "exp_capped",
                        "first_test_period": "2022Q1",
                        "last_test_period": "2022Q1",
                        "heldout_first_period": "2023Q1",
                        "heldout_last_period": "2023Q1",
                    }
                },
            )
        self.assertEqual(response.status_code, 400)
        self.assertIn("cap", response.json()["detail"])
        # No half-created experiment: a later retry must not hit "already exists".
        self.assertFalse((self.experiments_root / "exp_capped").exists())

    def test_create_experiment_rejects_hidden_params_and_forces_roots(self) -> None:
        base = {
            "experiment_id": "exp_sec",
            "first_test_period": "2022Q1",
            "last_test_period": "2022Q1",
            "heldout_first_period": "2023Q1",
            "heldout_last_period": "2023Q1",
        }
        # UI hiding is not a permission boundary: operator-only keys (host
        # executor, source roots, credential env names, proxy binaries) must
        # be rejected at the API even though a worker-side params.json may
        # legitimately carry them.
        for key, value in (
            ("local_dev", True),
            ("raw_dir", "/tmp/evil"),
            ("template_dir", "/tmp/evil"),
            ("tavily_api_key_env", "SOME_OTHER_SECRET"),
            ("meta_learning_xray_bin", "/tmp/evil/xray"),
        ):
            refused = self.client.post("/api/experiments", json={"params": {**base, key: value}})
            self.assertEqual(refused.status_code, 400, key)
            self.assertIn(key, refused.json()["detail"])
        self.assertFalse((self.experiments_root / "exp_sec").exists())
        # Server-managed roots are forced (overwrite, not setdefault): a caller
        # cannot redirect where experiment or sandbox work trees land.
        with patch.object(ExperimentManager, "start_worker", return_value={"spawned_pid": 1}):
            created = self.client.post(
                "/api/experiments",
                json={"params": {**base, "experiments_root": "/tmp/elsewhere",
                                 "work_root": "/tmp/elsewhere/work"}},
            )
        self.assertEqual(created.status_code, 200, created.text)
        params = json.loads(
            (self.experiments_root / "exp_sec" / "hitl" / "params.json").read_text(encoding="utf-8")
        )
        self.assertEqual(params["experiments_root"], str(self.experiments_root.resolve()))
        self.assertTrue(params["work_root"].endswith("/.runtime/sandboxes/exp_sec"))
        self.assertNotIn("/tmp/elsewhere", params["work_root"])

    def test_running_cap_blocks_sixth_experiment(self) -> None:
        manager = ExperimentManager(self.repo_root, self.experiments_root)
        with patch.object(ExperimentManager, "running_experiments", return_value=["a", "b", "c", "d", "e"]):
            with self.assertRaisesRegex(ManagerError, "cap reached"):
                manager.start_worker("exp_hitl")

    def test_control_actions_write_control_file(self) -> None:
        approve = self.client.post(
            "/api/experiments/exp_hitl/control",
            json={"action": "approve", "session_key": "epoch_001/fold_2022Q2", "directive": "试试低波动"},
        )
        self.assertEqual(approve.status_code, 200)
        control = approve.json()["control"]
        self.assertIn("epoch_001/fold_2022Q2", control["approved_sessions"])
        self.assertEqual(control["directives"]["epoch_001/fold_2022Q2"], "试试低波动")
        pause = self.client.post("/api/experiments/exp_hitl/control", json={"action": "pause"})
        self.assertEqual(pause.json()["control"]["request"], "pause")
        mode = self.client.post("/api/experiments/exp_hitl/control", json={"action": "set_mode", "mode": "auto"})
        self.assertEqual(mode.json()["control"]["mode"], "auto")
        self.assertEqual(
            self.client.post("/api/experiments/exp_hitl/control", json={"action": "bogus"}).status_code, 400
        )

    def test_prompt_override_and_rerun_fold_controls(self) -> None:
        override = self.client.post(
            "/api/experiments/exp_hitl/control",
            json={"action": "set_prompt_override", "session_key": "epoch_001/fold_2022Q1", "directive": "FULL PROMPT"},
        )
        self.assertEqual(override.json()["control"]["prompt_overrides"], {"epoch_001/fold_2022Q1": "FULL PROMPT"})
        cleared = self.client.post(
            "/api/experiments/exp_hitl/control",
            json={"action": "set_prompt_override", "session_key": "epoch_001/fold_2022Q1", "directive": ""},
        )
        self.assertEqual(cleared.json()["control"]["prompt_overrides"], {})
        # Only the LATEST recorded fold may be re-run.
        wrong = self.client.post(
            "/api/experiments/exp_hitl/control",
            json={"action": "rerun_fold", "session_key": "epoch_001/fold_2022Q2"},
        )
        self.assertEqual(wrong.status_code, 400)
        self.assertIn("只能重跑最新完成的 Fold", wrong.json()["detail"])
        automatic = self.client.post(
            "/api/experiments/exp_hitl/control", json={"action": "set_mode", "mode": "auto"}
        )
        self.assertEqual(automatic.status_code, 200, automatic.text)
        with patch.object(ExperimentManager, "start_worker", return_value={"spawned_pid": 7}):
            ok = self.client.post(
                "/api/experiments/exp_hitl/control",
                json={"action": "rerun_fold", "session_key": "epoch_001/fold_2022Q1"},
            )
        self.assertEqual(ok.status_code, 200, ok.text)
        control = ok.json()["control"]
        self.assertIn("epoch_001/fold_2022Q1", control["rerun_sessions"])
        self.assertNotIn("epoch_001/fold_2022Q1", control["approved_sessions"])
        self.assertEqual(control["mode"], "manual")

    def test_skip_to_heldout_and_gpu_count_controls(self) -> None:
        with patch.object(ExperimentManager, "start_worker", return_value={"spawned_pid": 5}):
            skip = self.client.post("/api/experiments/exp_hitl/control", json={"action": "skip_to_heldout"})
        self.assertEqual(skip.status_code, 200, skip.text)
        self.assertTrue(skip.json()["control"]["skip_to_heldout"])
        cancel = self.client.post("/api/experiments/exp_hitl/control", json={"action": "cancel_skip_to_heldout"})
        self.assertFalse(cancel.json()["control"]["skip_to_heldout"])
        # Without a recorded fold there is nothing to finish early with.
        bare = self.experiments_root / "exp_bare"
        (bare / "hitl").mkdir(parents=True)
        write_json_atomic(bare / "hitl" / "params.json", {"experiment_id": "exp_bare"})
        write_control(bare / "hitl" / "control.json", ControlState())
        refused = self.client.post("/api/experiments/exp_bare/control", json={"action": "skip_to_heldout"})
        self.assertEqual(refused.status_code, 400)
        # GPU counts: validated int in 1..4, empty clears.
        ok = self.client.post(
            "/api/experiments/exp_hitl/control",
            json={"action": "set_gpu_count", "session_key": "epoch_001/fold_2022Q2", "directive": "2"},
        )
        self.assertEqual(ok.json()["control"]["gpu_counts"], {"epoch_001/fold_2022Q2": 2})
        for bad in ("abc", "0", "5", "99"):
            response = self.client.post(
                "/api/experiments/exp_hitl/control",
                json={"action": "set_gpu_count", "session_key": "epoch_001/fold_2022Q2", "directive": bad},
            )
            self.assertEqual(response.status_code, 400, bad)
        cleared = self.client.post(
            "/api/experiments/exp_hitl/control",
            json={"action": "set_gpu_count", "session_key": "epoch_001/fold_2022Q2", "directive": ""},
        )
        self.assertEqual(cleared.json()["control"]["gpu_counts"], {})

    def _build_step_tree(self, experiment_id: str, *, fold_id: str = "fold_2022Q1",
                         result_name: str = "valid_000", with_failed: bool = True) -> str:
        from autotrade.environment.identity import agent_visible_ref
        from autotrade.environment.step_tree import StepTree

        experiment_dir = self.experiments_root / experiment_id
        strategy_dir = experiment_dir / "strategy_artifacts" / "epoch_001" / "strategy_epoch_001_fold_2022Q1"
        detail = experiment_dir / "detail_fixture.json"
        detail.write_text("{}", encoding="utf-8")
        tree = StepTree(experiment_dir / "steps")
        fold_ref = agent_visible_ref(fold_id, prefix="fold_ref")
        node_id = tree.record_step(
            strategy_dir,
            epoch_id="epoch_001",
            fold_id=fold_ref,
            result_name=result_name,
            artifact_hash=artifact_hash(strategy_dir),
            metrics={"total_return": 0.10, "sharpe": 1.0, "max_drawdown": 0.05,
                     "long_return": 0.08, "short_return": 0.02},
            complete_validation=True,
            model_artifact_hash=model_artifact_hash(strategy_dir / ".missing_models"),
            attachments={"detailed_return.json": detail},
        )
        if with_failed:
            tree.record_failed_attempt(
                epoch_id="epoch_001", fold_id=fold_ref, result_name="failed_x", error="boom"
            )
        return node_id

    def test_step_tree_view_deopaques_folds_and_marks_frozen(self) -> None:
        node_id = self._build_step_tree("exp_hitl")
        payload = self.client.get("/api/experiments/exp_hitl/steps").json()
        self.assertEqual(payload["current_node_id"], node_id)
        nodes = {node["node_id"]: node for node in payload["nodes"]}
        self.assertEqual(len(nodes), 2)
        good = nodes[node_id]
        # The agent-opaque fold_ref maps back to the real fold id for the researcher.
        self.assertEqual(good["fold_id"], "fold_2022Q1")
        self.assertTrue(good["has_snapshot"])
        self.assertTrue(good["is_current"])
        self.assertEqual(good["frozen_for"], ["epoch_001/fold_2022Q1"])
        self.assertEqual(good["attachments"], ["detailed_return.json"])
        failed = next(node for node in payload["nodes"] if node["status"] == "failed")
        self.assertFalse(failed["has_snapshot"])
        self.assertEqual(failed["frozen_for"], [])
        self.assertEqual(
            [session["key"] for session in payload["fold_sessions"]],
            ["epoch_001/fold_2022Q1", "epoch_001/fold_2022Q2"],
        )
        # Experiments without a tree return an empty payload, not an error.
        bare = self.experiments_root / "exp_notree"
        (bare / "hitl").mkdir(parents=True)
        write_json_atomic(bare / "hitl" / "params.json", {"experiment_id": "exp_notree"})
        write_control(bare / "hitl" / "control.json", ControlState())
        empty = self.client.get("/api/experiments/exp_notree/steps").json()
        self.assertEqual(empty["nodes"], [])

    def test_step_node_zip_contains_source_and_results(self) -> None:
        node_id = self._build_step_tree("exp_hitl")
        response = self.client.get(f"/api/experiments/exp_hitl/steps/{node_id}/source.zip")
        self.assertEqual(response.status_code, 200, response.text)
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            names = set(archive.namelist())
        self.assertIn("output/main.py", names)
        self.assertIn("detailed_return.json", names)
        failed_id = f"epoch_001__{node_id.split('__')[1]}__failed_x"
        missing = self.client.get(f"/api/experiments/exp_hitl/steps/{failed_id}/source.zip")
        self.assertEqual(missing.status_code, 404)
        unknown = self.client.get("/api/experiments/exp_hitl/steps/nope/source.zip")
        self.assertEqual(unknown.status_code, 404)

    def test_current_step_zip_reads_live_validated_snapshot_only_at_gate(self) -> None:
        from autotrade.environment.identity import agent_visible_ref
        from autotrade.environment.step_tree import StepTree

        run_id = "run_live"
        steps_root = self.repo_root / ".runtime" / "sandboxes" / "exp_hitl" / run_id / "artifacts" / "steps"
        strategy_dir = (
            self.experiments_root / "exp_hitl" / "strategy_artifacts"
            / "epoch_001" / "strategy_epoch_001_fold_2022Q1"
        )
        detail = self.repo_root / "live_detail.json"
        detail.write_text("{}", encoding="utf-8")
        node_id = StepTree(steps_root).record_step(
            strategy_dir,
            epoch_id="epoch_001",
            fold_id=agent_visible_ref("fold_2022Q2", prefix="fold_ref"),
            result_name="valid_003",
            artifact_hash=artifact_hash(strategy_dir),
            metrics={"total_return": 0.01},
            complete_validation=True,
            model_artifact_hash=model_artifact_hash(strategy_dir / ".missing_models"),
            attachments={"detailed_return.json": detail},
        )
        status_path = self.experiments_root / "exp_hitl" / "hitl" / "status.json"
        write_json_atomic(
            status_path,
            {"schema_version": 1, "state": "waiting_step_user", "run_id": run_id, "epoch_id": "epoch_001",
             "fold_id": "fold_2022Q2", "session_key": "epoch_001/fold_2022Q2", "awaiting_step": 2},
        )

        response = self.client.get("/api/experiments/exp_hitl/current-step/source.zip")
        self.assertEqual(response.status_code, 200, response.text)
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            names = set(archive.namelist())
        self.assertIn("output/main.py", names)
        self.assertNotIn("detailed_return.json", names)

        current = self.client.get("/api/experiments/exp_hitl/current-step").json()
        self.assertEqual(current, {"available": True, "node_id": node_id})
        with patch("autotrade.webui.analysis.AnalysisService.regenerate_step") as regenerate:
            started = self.client.post("/api/experiments/exp_hitl/current-step/analysis")
        self.assertEqual(started.status_code, 200, started.text)
        regenerate.assert_called_once()
        analysis_dir = self.experiments_root / "exp_hitl" / "hitl" / "analysis"
        analysis_dir.mkdir(exist_ok=True)
        (analysis_dir / f"step__{node_id}.md").write_text("## 策略逻辑\nok\n", encoding="utf-8")
        (analysis_dir / f"step__{node_id}.json").write_text(
            json.dumps({"status": "ok", "analysis_kind": "step"}), encoding="utf-8"
        )
        analysis = self.client.get("/api/experiments/exp_hitl/current-step/analysis").json()
        self.assertTrue(analysis["available"])
        self.assertIn("策略逻辑", analysis["content"])

        # Agent questions can download the latest completed Step too.
        write_json_atomic(status_path, {"schema_version": 1, "state": "waiting_user_reply", "run_id": run_id})
        question_download = self.client.get("/api/experiments/exp_hitl/current-step/source.zip")
        self.assertEqual(question_download.status_code, 200)

        write_json_atomic(status_path, {"schema_version": 1, "state": "running_session", "run_id": run_id})
        refused = self.client.get("/api/experiments/exp_hitl/current-step/source.zip")
        self.assertEqual(refused.status_code, 404)
        self.assertFalse(self.client.get("/api/experiments/exp_hitl/current-step").json()["available"])

    def test_set_parent_override_control(self) -> None:
        node_id = self._build_step_tree("exp_hitl")
        ok = self.client.post(
            "/api/experiments/exp_hitl/control",
            json={"action": "set_parent_override", "session_key": "epoch_001/fold_2022Q2", "directive": node_id},
        )
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertEqual(ok.json()["control"]["parent_overrides"], {"epoch_001/fold_2022Q2": node_id})
        # Unknown node and non-fold sessions are rejected.
        bad_node = self.client.post(
            "/api/experiments/exp_hitl/control",
            json={"action": "set_parent_override", "session_key": "epoch_001/fold_2022Q2", "directive": "nope"},
        )
        self.assertEqual(bad_node.status_code, 400)
        bad_session = self.client.post(
            "/api/experiments/exp_hitl/control",
            json={"action": "set_parent_override", "session_key": "heldout", "directive": node_id},
        )
        self.assertEqual(bad_session.status_code, 400)
        # Past-only: a later fold's node must not become an earlier fold's parent.
        q2_node = self._build_step_tree("exp_hitl", fold_id="fold_2022Q2", result_name="valid_001",
                                        with_failed=False)
        leak = self.client.post(
            "/api/experiments/exp_hitl/control",
            json={"action": "set_parent_override", "session_key": "epoch_001/fold_2022Q1", "directive": q2_node},
        )
        self.assertEqual(leak.status_code, 400)
        self.assertIn("泄漏", leak.json()["detail"])
        # The node's own session stays allowed (rerun-from-node).
        own = self.client.post(
            "/api/experiments/exp_hitl/control",
            json={"action": "set_parent_override", "session_key": "epoch_001/fold_2022Q2", "directive": q2_node},
        )
        self.assertEqual(own.status_code, 200, own.text)
        cleared = self.client.post(
            "/api/experiments/exp_hitl/control",
            json={"action": "set_parent_override", "session_key": "epoch_001/fold_2022Q2", "directive": ""},
        )
        self.assertEqual(cleared.json()["control"]["parent_overrides"], {})

    def test_terminate_returns_after_graceful_exit(self) -> None:
        import subprocess
        import sys as _sys

        from autotrade.pipelines.hitl_state import proc_start_ticks

        session_key = "epoch_001/meta_learning_after_fold_001"
        hitl_dir = self.experiments_root / "exp_hitl" / "hitl"
        schedule_path = hitl_dir / "schedule.json"
        schedule = json.loads(schedule_path.read_text(encoding="utf-8"))
        schedule["sessions"].insert(
            2,
            {
                "key": session_key,
                "kind": "meta_learning",
                "epoch_id": "epoch_001",
                "meta_learning_id": "epoch_001_after_fold_001",
                "trigger_after_folds": 1,
                "before_fold_id": "fold_2022Q2",
            },
        )
        write_json_atomic(schedule_path, schedule)
        automatic = self.client.post(
            "/api/experiments/exp_hitl/control",
            json={"action": "set_mode", "mode": "auto"},
        )
        self.assertEqual(automatic.status_code, 200, automatic.text)
        approved = self.client.post(
            "/api/experiments/exp_hitl/control",
            json={"action": "approve", "session_key": session_key, "directive": "旧方向"},
        )
        self.assertEqual(approved.status_code, 200, approved.text)
        proc = subprocess.Popen([_sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
        try:
            write_json_atomic(
                hitl_dir / "status.json",
                {
                    "schema_version": 1,
                    "pid": proc.pid,
                    "pid_start_ticks": proc_start_ticks(proc.pid),
                    "state": "running_session",
                    "session_key": session_key,
                },
            )
            manager = ExperimentManager(self.repo_root, self.experiments_root)
            result = manager.control("exp_hitl", "terminate")
            self.assertEqual(result["terminated_pid"], proc.pid)
            self.assertFalse(result["escalated"])  # plain sleep dies on SIGTERM
            self.assertEqual(result["approval_revoked_session"], session_key)
            control = read_control(hitl_dir / "control.json")
            self.assertNotIn(session_key, control.approved_sessions)
            self.assertEqual(control.directives[session_key], "旧方向")
            self.assertEqual(control.mode, "manual")

            revised = "Within this experiment, explore event-driven strategies built upon knowledge graphs, GNNs and natural language analysis."
            preview = self.client.post(
                "/api/experiments/exp_hitl/prompt-preview",
                json={"session_key": session_key, "directive": revised},
            )
            self.assertEqual(preview.status_code, 200, preview.text)
            self.assertIn(revised, preview.json()["prompt"])
            reapproved = self.client.post(
                "/api/experiments/exp_hitl/control",
                json={"action": "approve", "session_key": session_key, "directive": revised},
            )
            self.assertEqual(reapproved.status_code, 200, reapproved.text)
            self.assertIn(session_key, reapproved.json()["control"]["approved_sessions"])
            self.assertEqual(reapproved.json()["control"]["directives"][session_key], revised)
        finally:
            proc.kill()
            proc.wait()

    def test_terminate_preserves_approval_for_a_settled_session(self) -> None:
        import subprocess
        import sys as _sys

        from autotrade.pipelines.hitl_state import proc_start_ticks

        session_key = "epoch_001/fold_2022Q1"
        hitl_dir = self.experiments_root / "exp_hitl" / "hitl"
        automatic = self.client.post(
            "/api/experiments/exp_hitl/control",
            json={"action": "set_mode", "mode": "auto"},
        )
        self.assertEqual(automatic.status_code, 200, automatic.text)
        self.client.post(
            "/api/experiments/exp_hitl/control",
            json={"action": "approve", "session_key": session_key, "directive": "已完成方向"},
        )
        proc = subprocess.Popen([_sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
        try:
            write_json_atomic(
                hitl_dir / "status.json",
                {
                    "schema_version": 1,
                    "pid": proc.pid,
                    "pid_start_ticks": proc_start_ticks(proc.pid),
                    "state": "running_session",
                    "session_key": session_key,
                },
            )
            result = ExperimentManager(self.repo_root, self.experiments_root).control(
                "exp_hitl", "terminate"
            )
            self.assertNotIn("approval_revoked_session", result)
            control = read_control(hitl_dir / "control.json")
            self.assertIn(session_key, control.approved_sessions)
            self.assertEqual(control.mode, "auto")
        finally:
            proc.kill()
            proc.wait()

    def test_terminate_auto_session_without_explicit_approval_still_waits_for_reapproval(self) -> None:
        import subprocess
        import sys as _sys

        from autotrade.pipelines.hitl_state import proc_start_ticks

        session_key = "epoch_001/fold_2022Q2"
        hitl_dir = self.experiments_root / "exp_hitl" / "hitl"
        automatic = self.client.post(
            "/api/experiments/exp_hitl/control", json={"action": "set_mode", "mode": "auto"}
        )
        self.assertEqual(automatic.status_code, 200, automatic.text)
        self.assertNotIn(session_key, automatic.json()["control"]["approved_sessions"])
        proc = subprocess.Popen([_sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
        try:
            write_json_atomic(
                hitl_dir / "status.json",
                {
                    "schema_version": 1,
                    "pid": proc.pid,
                    "pid_start_ticks": proc_start_ticks(proc.pid),
                    "state": "running_session",
                    "session_key": session_key,
                },
            )
            result = ExperimentManager(self.repo_root, self.experiments_root).control(
                "exp_hitl", "terminate"
            )
            self.assertEqual(result["approval_revoked_session"], session_key)
            control = read_control(hitl_dir / "control.json")
            self.assertEqual(control.mode, "manual")
            self.assertNotIn(session_key, control.approved_sessions)
        finally:
            proc.kill()
            proc.wait()

    def test_rerun_and_rollback_reset_step_gate_state(self) -> None:
        # Seed per-step state for both folds, then exercise both cleanup paths.
        self.client.post("/api/experiments/exp_hitl/control",
                         json={"action": "set_step_gate", "session_key": "epoch_001/fold_2022Q2", "directive": "1"})
        write_json_atomic(
            self.experiments_root / "exp_hitl" / "hitl" / "status.json",
            {"schema_version": 1, "pid": 999_999_999, "state": "waiting_step_user",
             "session_key": "epoch_001/fold_2022Q2", "awaiting_step": 2},
        )
        self.client.post("/api/experiments/exp_hitl/control",
                         json={"action": "approve_step", "session_key": "epoch_001/fold_2022Q2", "directive": "旧指令"})
        write_json_atomic(self.experiments_root / "exp_hitl" / "hitl" / "status.json",
                          {"schema_version": 1, "pid": 999_999_999, "state": "stopped"})
        # rerun of the latest recorded fold (Q1) clears ITS step state only —
        # seed Q1 state directly via the control file.
        from autotrade.pipelines.hitl_state import read_control, write_control as wc, CONTROL_NAME
        control_path = self.experiments_root / "exp_hitl" / "hitl" / CONTROL_NAME
        control = read_control(control_path)
        control.step_go["epoch_001/fold_2022Q1"] = 5
        control.step_directives["epoch_001/fold_2022Q1#5"] = "stale"
        wc(control_path, control)
        with patch.object(ExperimentManager, "start_worker", return_value={"spawned_pid": 7}):
            rerun = self.client.post("/api/experiments/exp_hitl/control",
                                     json={"action": "rerun_fold", "session_key": "epoch_001/fold_2022Q1"})
        self.assertEqual(rerun.status_code, 200, rerun.text)
        control = read_control(control_path)
        self.assertNotIn("epoch_001/fold_2022Q1", control.step_go)
        self.assertNotIn("epoch_001/fold_2022Q1#5", control.step_directives)
        # Q2's state is untouched by Q1's rerun...
        self.assertEqual(control.step_go.get("epoch_001/fold_2022Q2"), 2)
        # ...and dropped by a rollback to Q1 (Q2 has a ledger record? give it one).
        experiment_dir = self.experiments_root / "exp_hitl"
        q2_dir = experiment_dir / "strategy_artifacts" / "epoch_001" / "strategy_epoch_001_fold_2022Q2x"
        q2_dir.mkdir(parents=True)
        (q2_dir / "main.py").write_text("def main(ctx):\n    return 2\n", encoding="utf-8")
        ledger = experiment_dir / "ledgers" / "experiment_ledger.jsonl"
        with ledger.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "schema_version": 1,
                "record_type": "fold", "experiment_id": "exp_hitl", "epoch_id": "epoch_001",
                "fold_id": "fold_2022Q2", "run_id": "run_002", "fold_status": "frozen",
                "frozen_strategy_artifact_id": "strategy_epoch_001_fold_2022Q2x",
                "frozen_strategy_artifact_hash": artifact_hash(q2_dir),
                "frozen_strategy_artifact_path": str(q2_dir),
                "frozen_model_artifact_path": None,
                "validation_result": {"total_return": 0.02}, "test_result": {"total_return": 0.01},
            }) + "\n")
        with patch.object(ExperimentManager, "start_worker", return_value={"spawned_pid": 8}):
            rollback = self.client.post("/api/experiments/exp_hitl/control",
                                        json={"action": "rollback_fold", "session_key": "epoch_001/fold_2022Q1"})
        self.assertEqual(rollback.status_code, 200, rollback.text)
        control = read_control(control_path)
        self.assertNotIn("epoch_001/fold_2022Q2", control.step_gate)
        self.assertNotIn("epoch_001/fold_2022Q2", control.step_go)
        self.assertFalse([k for k in control.step_directives if k.startswith("epoch_001/fold_2022Q2#")])

    def test_step_gate_controls(self) -> None:
        on = self.client.post(
            "/api/experiments/exp_hitl/control",
            json={"action": "set_step_gate", "session_key": "epoch_001/fold_2022Q2", "directive": "1"},
        )
        self.assertEqual(on.json()["control"]["step_gate"], {"epoch_001/fold_2022Q2": True})
        # approve_step requires the worker to actually be holding at a step.
        refused = self.client.post(
            "/api/experiments/exp_hitl/control",
            json={"action": "approve_step", "session_key": "epoch_001/fold_2022Q2", "directive": "x"},
        )
        self.assertEqual(refused.status_code, 400)
        write_json_atomic(
            self.experiments_root / "exp_hitl" / "hitl" / "status.json",
            {"schema_version": 1, "pid": 999_999_999, "state": "waiting_step_user",
             "session_key": "epoch_001/fold_2022Q2", "awaiting_step": 3},
        )
        ok = self.client.post(
            "/api/experiments/exp_hitl/control",
            json={"action": "approve_step", "session_key": "epoch_001/fold_2022Q2", "directive": "关注回撤"},
        )
        control = ok.json()["control"]
        self.assertEqual(control["step_go"], {"epoch_001/fold_2022Q2": 3})
        self.assertEqual(control["step_directives"], {"epoch_001/fold_2022Q2#3": "关注回撤"})
        off = self.client.post(
            "/api/experiments/exp_hitl/control",
            json={"action": "set_step_gate", "session_key": "epoch_001/fold_2022Q2", "directive": ""},
        )
        self.assertEqual(off.json()["control"]["step_gate"], {})

    def test_reveal_seals_learning_actions(self) -> None:
        self._reveal()
        for action in ("approve", "rerun_fold", "rollback_fold", "approve_step",
                       "reply_question", "set_step_gate", "set_directive", "resume"):
            refused = self.client.post(
                "/api/experiments/exp_hitl/control",
                json={"action": action, "session_key": "epoch_001/fold_2022Q2", "directive": "x"},
            )
            self.assertEqual(refused.status_code, 400, action)
            self.assertIn("封存", refused.json()["detail"])
        # Lifecycle controls stay available on a sealed experiment.
        ok = self.client.post("/api/experiments/exp_hitl/control", json={"action": "stop"})
        self.assertEqual(ok.status_code, 200)

    def test_reply_question_controls(self) -> None:
        # Requires the worker to actually be holding on a question.
        refused = self.client.post(
            "/api/experiments/exp_hitl/control",
            json={"action": "reply_question", "session_key": "epoch_001/fold_2022Q2", "directive": "x"},
        )
        self.assertEqual(refused.status_code, 400)
        write_json_atomic(
            self.experiments_root / "exp_hitl" / "hitl" / "status.json",
            {"schema_version": 1, "pid": 999_999_999, "state": "waiting_user_reply",
             "session_key": "epoch_001/fold_2022Q2",
             "awaiting_question": {
                 "index": 2,
                 "question": "方案A还是B？",
                 "reply_key": "epoch_001/fold_2022Q2#asknewattempt#q2",
             }},
        )
        ok = self.client.post(
            "/api/experiments/exp_hitl/control",
            json={"action": "reply_question", "session_key": "epoch_001/fold_2022Q2", "directive": "方案A，控制换手"},
        )
        control = ok.json()["control"]
        self.assertEqual(
            control["user_replies"],
            {"epoch_001/fold_2022Q2#asknewattempt#q2": "方案A，控制换手"},
        )
        # Empty reply still releases (recorded as "").
        write_json_atomic(
            self.experiments_root / "exp_hitl" / "hitl" / "status.json",
            {"schema_version": 1, "pid": 999_999_999, "state": "waiting_user_reply",
             "session_key": "epoch_001/fold_2022Q2",
             "awaiting_question": {"index": 3, "question": "继续？"}},
        )
        released = self.client.post(
            "/api/experiments/exp_hitl/control",
            json={"action": "reply_question", "session_key": "epoch_001/fold_2022Q2", "directive": ""},
        )
        self.assertEqual(released.json()["control"]["user_replies"]["epoch_001/fold_2022Q2#q3"], "")

    def test_launching_stub_bridges_spawn_to_first_worker_status(self) -> None:
        from types import SimpleNamespace

        from autotrade.webui.registry import experiment_state

        experiment_dir = self.experiments_root / "exp_hitl"
        status_path = experiment_dir / "hitl" / "status.json"
        write_json_atomic(status_path, {"schema_version": 1, "pid": 999_999_999, "state": "stopped",
                                        "total_sessions": 4, "completed_sessions": 2})
        manager = ExperimentManager(self.repo_root, self.experiments_root)
        with patch("autotrade.webui.manager.subprocess.Popen", return_value=SimpleNamespace(pid=4242)):
            spawn = manager.start_worker("exp_hitl")
        self.assertEqual(spawn["spawned_pid"], 4242)
        status = json.loads(status_path.read_text(encoding="utf-8"))
        # The stub bridges the interpreter/import window and keeps progress visible.
        self.assertEqual(status["state"], "launching")
        self.assertIn("launched_at", status)
        self.assertEqual(status["completed_sessions"], 2)
        self.assertEqual(experiment_state(experiment_dir)["state"], "launching")
        # While launching: no double spawn, destructive history mutation, or delete.
        with self.assertRaisesRegex(ManagerError, "启动中"):
            manager.start_worker("exp_hitl")
        with self.assertRaisesRegex(ManagerError, "停止运行中的 worker"):
            manager.control(
                "exp_hitl", "rerun_fold", session_key="epoch_001/fold_2022Q1"
            )
        with self.assertRaisesRegex(ManagerError, "停止运行中的 worker"):
            manager.control(
                "exp_hitl", "rollback_fold", session_key="epoch_001/fold_2022Q1"
            )
        with self.assertRaisesRegex(ManagerError, "live worker"):
            manager.delete_experiment("exp_hitl")
        self.assertIn("exp_hitl", manager.running_experiments())
        # A stale stub (worker never wrote status) degrades to interrupted.
        write_json_atomic(status_path, {"schema_version": 1, "state": "launching", "launched_at": "2020-01-01T00:00:00+00:00"})
        self.assertEqual(experiment_state(experiment_dir)["state"], "interrupted")

    def test_rollback_drops_later_records_and_archives_artifacts(self) -> None:
        experiment_dir = self.experiments_root / "exp_hitl"
        q1_node = self._build_step_tree("exp_hitl", with_failed=False)
        q2_node = self._build_step_tree("exp_hitl", fold_id="fold_2022Q2", result_name="valid_001",
                                        with_failed=False)
        # Give fold_2022Q2 a record + frozen dir so there is something to drop.
        q2_dir = experiment_dir / "strategy_artifacts" / "epoch_001" / "strategy_epoch_001_fold_2022Q2"
        q2_dir.mkdir(parents=True)
        (q2_dir / "main.py").write_text("def main(ctx):\n    return 2\n", encoding="utf-8")
        ledger = experiment_dir / "ledgers" / "experiment_ledger.jsonl"
        with ledger.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "schema_version": 1,
                "record_type": "fold", "experiment_id": "exp_hitl", "epoch_id": "epoch_001",
                "fold_id": "fold_2022Q2", "run_id": "run_002", "fold_status": "frozen",
                "frozen_strategy_artifact_id": "strategy_epoch_001_fold_2022Q2",
                "frozen_strategy_artifact_hash": artifact_hash(q2_dir),
                "frozen_strategy_artifact_path": str(q2_dir),
                "frozen_model_artifact_path": None,
                "validation_result": {"total_return": 0.02}, "test_result": {"total_return": 0.01},
            }) + "\n")
        # Approvals for later sessions must be withdrawn by the rollback.
        self.client.post("/api/experiments/exp_hitl/control",
                         json={"action": "approve", "session_key": "epoch_001/fold_2022Q2"})
        self.client.post("/api/experiments/exp_hitl/control",
                         json={"action": "approve", "session_key": "heldout"})
        # Rolling back to an unrecorded fold is refused.
        bad = self.client.post("/api/experiments/exp_hitl/control",
                               json={"action": "rollback_fold", "session_key": "epoch_001/fold_2099Q9"})
        self.assertEqual(bad.status_code, 400)
        with patch.object(ExperimentManager, "start_worker", return_value={"spawned_pid": 9}):
            ok = self.client.post("/api/experiments/exp_hitl/control",
                                  json={"action": "rollback_fold", "session_key": "epoch_001/fold_2022Q1"})
        self.assertEqual(ok.status_code, 200, ok.text)
        payload = ok.json()
        self.assertEqual(payload["rolled_back_to"], "epoch_001/fold_2022Q1")
        self.assertEqual(payload["dropped_records"], 2)  # fold Q2 + heldout
        # Ledger: Q1 fold + meta kept, Q2/heldout gone; backup preserved.
        kept = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual({(r["record_type"], r["fold_id"]) for r in kept},
                         {("meta_learning", "epoch_001_meta_learning"), ("fold", "fold_2022Q1")})
        backups = list(ledger.parent.glob("experiment_ledger.rollback_*.jsonl"))
        self.assertEqual(len(backups), 1)
        self.assertIn("fold_2022Q2", backups[0].read_text(encoding="utf-8"))
        # Frozen dir of the dropped fold is archived, original path gone.
        self.assertFalse(q2_dir.exists())
        archives = list((experiment_dir / "strategy_artifacts" / "_archive").glob("rollback_*/strategy_epoch_001_fold_2022Q2"))
        self.assertEqual(len(archives), 1)
        # Step tree pruned: the dropped fold's node (future validation evidence
        # relative to the new frontier) is archived; the kept fold's node stays.
        self.assertEqual(payload["pruned_step_nodes"], 1)
        tree = json.loads((experiment_dir / "steps" / "tree.json").read_text(encoding="utf-8"))
        self.assertEqual([node["node_id"] for node in tree["nodes"]], [q1_node])
        self.assertIsNone(tree["current_node_id"])  # pointed at the pruned node
        archived_steps = list((experiment_dir / "strategy_artifacts" / "_archive").glob(f"rollback_*/steps/{q2_node}"))
        self.assertEqual(len(archived_steps), 1)
        self.assertTrue((experiment_dir / "steps" / q1_node).is_dir())
        control = payload["control"]
        self.assertNotIn("epoch_001/fold_2022Q2", control["approved_sessions"])
        self.assertNotIn("heldout", control["approved_sessions"])
        # Nothing after the frontier now: a second rollback is refused.
        again = self.client.post("/api/experiments/exp_hitl/control",
                                 json={"action": "rollback_fold", "session_key": "epoch_001/fold_2022Q1"})
        self.assertEqual(again.status_code, 400)

    def test_inherit_import_copies_and_verifies(self) -> None:
        manager = ExperimentManager(self.repo_root, self.experiments_root)
        target = self.experiments_root / "exp_child"
        (target / "hitl").mkdir(parents=True)
        payload = manager._import_inherited_artifact(target, "exp_hitl")
        self.assertEqual(payload["source_experiment_id"], "exp_hitl")
        self.assertEqual(payload["source_fold_id"], "fold_2022Q1")
        copied = Path(str(payload["path"]))
        self.assertTrue((copied / "main.py").exists())
        self.assertEqual(payload["artifact_hash"], artifact_hash(copied))
        # A source without any recorded fold is refused.
        bare = self.experiments_root / "exp_bare2"
        (bare / "hitl").mkdir(parents=True)
        with self.assertRaises(ManagerError):
            manager._import_inherited_artifact(target, "exp_bare2")

    def test_equity_endpoint_serves_precomputed_curves(self) -> None:
        experiment_dir = self.experiments_root / "exp_hitl"
        results = experiment_dir / "artifacts" / "run_001" / "results"
        for name, curve in (
            ("valid_000", {"20220104": 1_010_000.0, "20220105": 1_000_000.0}),
            ("valid_001", {"20220106": 1_020_000.0}),
            ("test_000", {"20220401": 990_000.0}),
        ):
            window = results / name
            window.mkdir(parents=True, exist_ok=True)  # the orders fixture pre-creates some windows
            (window / "detailed_return.json").write_text(
                json.dumps({"initial_cash": 1_000_000.0, "equity_curve": curve}), encoding="utf-8"
            )
        # The benchmark series comes from the run's persisted style rollup —
        # the web layer never touches the raw lake.
        (results / "style_valid.json").write_text(
            json.dumps({"benchmark_daily": [["20220104", 0.005], ["20220106", -0.002]]}), encoding="utf-8"
        )
        # Hidden until revealed: the equity payload carries no test series.
        hidden = self.client.get("/api/experiments/exp_hitl/equity").json()
        self.assertNotIn("test", {series["key"] for series in hidden["series"]})
        self._reveal()
        payload = self.client.get("/api/experiments/exp_hitl/equity").json()
        by_key = {series["key"]: series for series in payload["series"]}
        valid = by_key["valid"]
        # ONLY the selected step's window feeds the validation curve: earlier
        # overlapping attempt windows (valid_000, a rejected version) must not
        # blend in, or the curve contradicts the ledger's headline metric.
        self.assertEqual(valid["dates"], ["20220106"])
        self.assertAlmostEqual(valid["cum"][0], 0.02, places=6)             # 1.02M / 1M - 1
        self.assertEqual(valid["final"], valid["cum"][-1])
        self.assertAlmostEqual(by_key["test"]["cum"][0], -0.01)
        benchmark = payload["benchmark"]
        self.assertEqual(benchmark["dates"], ["20220106"])
        fold = self.client.get("/api/experiments/exp_hitl/folds/epoch_001/fold_2022Q1/equity").json()
        self.assertEqual(fold["valid"]["series"][0]["dates"], ["20220106"])
        self.assertEqual(fold["valid"]["benchmark"]["dates"], ["20220106"])
        self.assertEqual(fold["test"]["benchmark"]["dates"], [])  # no rollup for the test chain
        self.assertEqual(len(fold["test"]["series"][0]["dates"]), 1)

    def _append_epoch_002_fold(self) -> None:
        """Second-epoch re-run of the same fold calendar with its own run/results."""
        experiment_dir = self.experiments_root / "exp_hitl"
        results = experiment_dir / "artifacts" / "run_e2" / "results"
        window = results / "valid_000"
        window.mkdir(parents=True, exist_ok=True)
        (window / "detailed_return.json").write_text(
            json.dumps({"initial_cash": 1_000_000.0, "equity_curve": {"20220106": 1_050_000.0, "20220107": 1_029_000.0}}),
            encoding="utf-8",
        )
        (results / "style_valid.json").write_text(
            json.dumps({"benchmark_daily": [["20220106", 0.01], ["20220107", 0.005]]}), encoding="utf-8"
        )
        ledger = experiment_dir / "ledgers" / "experiment_ledger.jsonl"
        record = {
            "schema_version": 1,
            "record_type": "fold",
            "experiment_id": "exp_hitl",
            "epoch_id": "epoch_002",
            "fold_id": "fold_2022Q1",
            "run_id": "run_e2",
            "fold_status": "frozen",
            "test_period": "20220101..20220331",
            "validation_result": {"total_return": 0.029, "sharpe": 0.5, "long_return": 0.029, "short_return": 0.0},
            "test_result": {"total_return": -0.02, "sharpe": -0.3, "long_return": -0.02, "short_return": 0.0},
            "selected_step_id": "step_000",
            "steps": [{"step_id": "step_000", "validation_result_ref": str(window)}],
        }
        with ledger.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")

    def test_equity_payload_carries_position_exposure(self) -> None:
        import pandas as pd

        window = self.experiments_root / "exp_hitl" / "artifacts" / "run_001" / "results" / "valid_001"
        window.mkdir(parents=True, exist_ok=True)
        (window / "detailed_return.json").write_text(
            json.dumps({"initial_cash": 1_000_000.0, "equity_curve": {"20220106": 1_000_000.0}}),
            encoding="utf-8",
        )
        pd.DataFrame([
            {"date": "20220106", "account": "stock", "ts_code": "000001.SZ", "side": "long",
             "quantity": 1000, "last_price": 10.0, "market_value": 600_000.0},
            {"date": "20220106", "account": "credit", "ts_code": "000002.SZ", "side": "short",
             "quantity": 500, "last_price": 20.0, "market_value": -100_000.0},
        ]).to_parquet(window / "positions_eod.parquet")

        payload = self.client.get("/api/experiments/exp_hitl/equity").json()
        exposure = payload["exposure"]["valid"]
        self.assertEqual(exposure["dates"], ["20220106"])
        self.assertAlmostEqual(exposure["long"][0], 0.6)   # 600k gross long / 1M equity
        self.assertAlmostEqual(exposure["short"][0], 0.1)  # |−100k| short / 1M equity
        self.assertNotIn("test", payload["exposure"])  # sealed pre-reveal

    def test_metrics_and_equity_never_mix_epochs(self) -> None:
        # Epochs re-run the SAME fold calendar: cumulative metrics and daily
        # curves must come from one epoch at a time, never compounded across
        # epochs (that counts each quarter once per epoch).
        self._append_epoch_002_fold()
        self._reveal()
        summary = {e["experiment_id"]: e for e in self.client.get("/api/experiments").json()["experiments"]}["exp_hitl"]
        self.assertEqual(summary["metrics"]["epoch_id"], "epoch_002")
        self.assertAlmostEqual(summary["metrics"]["cum_valid_return"], 0.029)  # NOT (1.10)(1.029)-1
        self.assertAlmostEqual(summary["metrics"]["cum_test_return"], -0.02)
        by_epoch = {entry["epoch_id"]: entry for entry in summary["metrics_by_epoch"]}
        self.assertAlmostEqual(by_epoch["epoch_001"]["cum_valid_return"], 0.10)
        self.assertAlmostEqual(by_epoch["epoch_002"]["cum_valid_return"], 0.029)

        payload = self.client.get("/api/experiments/exp_hitl/equity").json()
        self.assertEqual(payload["epoch_id"], "epoch_002")
        self.assertEqual(payload["epochs"], ["epoch_001", "epoch_002"])
        valid = {series["key"]: series for series in payload["series"]}["valid"]
        self.assertEqual(valid["dates"], ["20220106", "20220107"])  # epoch_002's window only
        stats = payload["stats"]["valid"]
        self.assertEqual(stats["n_days"], 2)
        self.assertAlmostEqual(stats["cum_return"], 0.029)
        self.assertAlmostEqual(stats["max_drawdown"], 0.02)
        self.assertAlmostEqual(stats["benchmark_return"], 1.01 * 1.005 - 1.0)
        self.assertAlmostEqual(stats["excess_return"], 0.029 - (1.01 * 1.005 - 1.0))
        self.assertIn("beta", stats)
        self.assertIn("information_ratio", stats)

        earlier = self.client.get("/api/experiments/exp_hitl/equity", params={"epoch_id": "epoch_001"}).json()
        self.assertEqual(earlier["epoch_id"], "epoch_001")
        valid_e1 = {series["key"]: series for series in earlier["series"]}.get("valid")
        if valid_e1 is not None:
            self.assertNotIn("20220107", valid_e1["dates"])

    def test_equity_never_reads_mutable_raw_for_missing_benchmark(self) -> None:
        # P2-3: historical charts use frozen rollups ONLY. A run without a
        # style rollup degrades to a strategy-only curve even when the raw
        # lake could supply the benchmark (raw is mutable; charts must not
        # change when it is revised).
        import pandas as pd

        results = self.experiments_root / "exp_hitl" / "artifacts" / "run_001" / "results"
        window = results / "valid_001"
        window.mkdir(parents=True, exist_ok=True)
        (window / "detailed_return.json").write_text(
            json.dumps({"initial_cash": 1_000_000.0, "equity_curve": {"20220106": 1_020_000.0}}),
            encoding="utf-8",
        )
        (results / "style_valid.json").unlink(missing_ok=True)
        raw = self.repo_root / "data" / "raw" / "index_daily" / "ts_code=000300.SH"
        raw.mkdir(parents=True)
        pd.DataFrame([{"trade_date": "20220106", "pct_chg": 1.5}]).to_parquet(raw / "year=2022.parquet")

        payload = self.client.get("/api/experiments/exp_hitl/equity").json()
        self.assertEqual(payload["benchmark"]["dates"], [])

    def test_fold_initial_prompt_reads_the_recorded_trace(self) -> None:
        # The endpoint returns what the fold session ACTUALLY started with:
        # the first llm_call event's messages from the collected trace.
        trace_path = self.experiments_root / "exp_hitl" / "artifacts" / "run_001" / "agent_trace.jsonl"
        trace_path.parent.mkdir(parents=True, exist_ok=True)
        events = [
            {"event_type": "session_started", "run_id": "run_001"},
            {
                "event_type": "llm_call",
                "run_id": "run_001",
                "model": "deepseek-v4-pro",
                "started_at": "2026-07-21T13:00:00+00:00",
                "new_messages": [
                    {"_seq": 0, "role": "system", "content": "# 角色与目标\nfixture system prompt"},
                    {"_seq": 1, "role": "user", "content": "开始本 Fold。"},
                ],
            },
            {
                "event_type": "llm_call",
                "run_id": "run_001",
                "new_messages": [{"_seq": 2, "role": "user", "content": "later turn"}],
            },
        ]
        trace_path.write_text(
            "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events),
            encoding="utf-8",
        )
        response = self.client.get("/api/experiments/exp_hitl/folds/epoch_001/fold_2022Q1/initial-prompt")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["run_id"], "run_001")
        self.assertEqual(payload["model"], "deepseek-v4-pro")
        self.assertEqual(
            [message["role"] for message in payload["messages"]], ["system", "user"]
        )
        self.assertIn("fixture system prompt", payload["messages"][0]["content"])
        self.assertEqual(payload["messages"][1]["content"], "开始本 Fold。")
        # Unknown fold and missing trace are 404s, not 500s.
        self.assertEqual(
            self.client.get("/api/experiments/exp_hitl/folds/epoch_001/fold_9999/initial-prompt").status_code,
            404,
        )
        trace_path.unlink()
        self.assertEqual(
            self.client.get("/api/experiments/exp_hitl/folds/epoch_001/fold_2022Q1/initial-prompt").status_code,
            404,
        )

    def test_broken_experiment_is_isolated_from_creation_and_detail(self) -> None:
        # A worker that outlived a migration appends records in its old format;
        # the resulting broken ledger must degrade to a structured unreadable
        # view without breaking the creation form or its own detail page.
        broken = self.experiments_root / "exp_broken"
        (broken / "ledgers").mkdir(parents=True)
        (broken / "ledgers" / "experiment_ledger.jsonl").write_text(
            json.dumps({"record_type": "fold", "epoch_id": "epoch_001"}) + "\n",
            encoding="utf-8",
        )
        schema = self.client.get("/api/parameter-schema")
        self.assertEqual(schema.status_code, 200)
        fields = {field["key"]: field for group in schema.json()["groups"] for field in group["fields"]}
        self.assertNotIn("exp_broken", fields["inherit_from"]["choices"])
        self.assertIn("exp_hitl", fields["inherit_from"]["choices"])
        detail = self.client.get("/api/experiments/exp_broken")
        self.assertEqual(detail.status_code, 200)
        payload = detail.json()
        self.assertEqual(payload["state"], "unreadable")
        self.assertIn("schema_version", str(payload["error"]))
        self.assertEqual(payload["sessions"], [])
        # The broken experiment stays deletable through the console.
        deleted = self.client.delete("/api/experiments/exp_broken", params={"confirm": "exp_broken"})
        self.assertEqual(deleted.status_code, 200)
        self.assertFalse(broken.exists())

    def test_health_reports_running_workers_on_stale_code(self) -> None:
        from autotrade.pipelines.hitl_state import proc_start_ticks

        write_json_atomic(
            self.experiments_root / "exp_hitl" / "hitl" / "status.json",
            {
                "schema_version": 1,
                "pid": os.getpid(),
                "pid_start_ticks": proc_start_ticks(os.getpid()),
                "state": "running_session",
                "code_version": "0000000",
            },
        )
        health = self.client.get("/api/health").json()
        self.assertIn("exp_hitl", health["running"])
        self.assertEqual(
            [entry["experiment_id"] for entry in health["stale_running"]], ["exp_hitl"]
        )
        self.assertEqual(health["stale_running"][0]["code_version"], "0000000")

    def test_assert_no_live_writer_guards_migrations(self) -> None:
        from autotrade.pipelines.hitl_state import assert_no_live_writer, proc_start_ticks

        experiment_dir = self.experiments_root / "exp_hitl"
        assert_no_live_writer(experiment_dir)  # fixture status has no live pid
        write_json_atomic(
            experiment_dir / "hitl" / "status.json",
            {
                "schema_version": 1,
                "pid": os.getpid(),
                "pid_start_ticks": proc_start_ticks(os.getpid()),
                "state": "running_session",
                "code_version": "0000000",
            },
        )
        with self.assertRaises(RuntimeError) as ctx:
            assert_no_live_writer(experiment_dir)
        self.assertIn("live worker", str(ctx.exception))
        # The ledger rewrite primitive embeds the same guard.
        from autotrade.pipelines.ledger import ExperimentLedger

        ledger = ExperimentLedger(experiment_dir / "ledgers" / "experiment_ledger.jsonl")
        migrated = ledger.read()
        with self.assertRaises(RuntimeError):
            ledger.rewrite(migrated)
        # A dead process incarnation (stale start ticks) does not block.
        write_json_atomic(
            experiment_dir / "hitl" / "status.json",
            {"schema_version": 1, "pid": os.getpid(), "pid_start_ticks": -1, "state": "running_session"},
        )
        assert_no_live_writer(experiment_dir)
        ledger.rewrite(migrated)
        self.assertEqual(ledger.read(), migrated)
        # rewrite refuses records a migration failed to stamp.
        with self.assertRaises(ValueError):
            ledger.rewrite([{**migrated[0], "schema_version": None}])

    def test_delete_requires_confirm_and_no_live_worker(self) -> None:
        missing_confirm = self.client.delete("/api/experiments/exp_hitl")
        self.assertEqual(missing_confirm.status_code, 400)
        # Simulate a live worker on the HITL experiment (our own pid is alive;
        # liveness requires the recorded kernel start ticks to match).
        from autotrade.pipelines.hitl_state import proc_start_ticks

        write_json_atomic(
            self.experiments_root / "exp_hitl" / "hitl" / "status.json",
            {"schema_version": 1, "pid": os.getpid(), "pid_start_ticks": proc_start_ticks(os.getpid()), "state": "running_session"},
        )
        alive = self.client.delete("/api/experiments/exp_hitl", params={"confirm": "exp_hitl"})
        self.assertEqual(alive.status_code, 409)
        write_json_atomic(
            self.experiments_root / "exp_hitl" / "hitl" / "status.json",
            {"schema_version": 1, "pid": 999_999_999, "state": "stopped"},
        )
        gone = self.client.delete("/api/experiments/exp_hitl", params={"confirm": "exp_hitl"})
        self.assertEqual(gone.status_code, 200)
        self.assertFalse((self.experiments_root / "exp_hitl").exists())

    def test_delete_refused_while_analysis_pending(self) -> None:
        # AnalysisService worker threads keep writing into hitl/analysis/ after
        # their HTTP request returns; deleting the experiment tree under them
        # would race those writes. The server wires the service's pending view
        # into the manager, which must refuse with 409 until the work drains.
        from autotrade.webui.analysis import AnalysisService

        manager = ExperimentManager(
            self.repo_root, self.experiments_root,
            analysis_pending=lambda experiment_id: experiment_id == "exp_hitl",
        )
        write_json_atomic(
            self.experiments_root / "exp_hitl" / "hitl" / "status.json",
            {"schema_version": 1, "pid": 999_999_999, "state": "stopped"},
        )
        with self.assertRaisesRegex(ManagerError, "analysis in progress"):
            manager.delete_experiment("exp_hitl")
        self.assertTrue((self.experiments_root / "exp_hitl").exists())
        # End-to-end through create_app: the wiring exists and maps to 409.
        with patch.object(AnalysisService, "pending_for_experiment", return_value=True) as pending:
            client = TestClient(create_app(self.repo_root, self.experiments_root))
            refused = client.delete("/api/experiments/exp_hitl", params={"confirm": "exp_hitl"})
            self.assertEqual(refused.status_code, 409)
            self.assertIn("analysis in progress", refused.json()["detail"])
            self.assertTrue((self.experiments_root / "exp_hitl").exists())
            pending.return_value = False  # analysis drained -> delete proceeds
            done = client.delete("/api/experiments/exp_hitl", params={"confirm": "exp_hitl"})
            self.assertEqual(done.status_code, 200)
            self.assertFalse((self.experiments_root / "exp_hitl").exists())

    def test_trace_stats_counts_and_backtest_credit(self) -> None:
        stats = self.client.get("/api/experiments/exp_hitl/trace/stats", params={"run_id": "run_001"}).json()
        self.assertEqual(stats["counts"]["llm_call"], 2)
        self.assertEqual(stats["counts"]["shell"], 1)
        self.assertEqual(stats["llm_total_tokens"], 3000)
        self.assertEqual(stats["llm_prompt_tokens"], 2300)
        self.assertEqual(stats["llm_completion_tokens"], 700)
        self.assertAlmostEqual(stats["backtest_wall_seconds"], 88.5)
        self.assertTrue(stats["in_backtest"])  # 2 starts, 1 terminal event
        self.assertIsNotNone(stats["active_backtest_started_at"])
        self.assertEqual(stats["total_events"], 6)

    def test_trace_stats_surfaces_live_progress_and_reply_wait_credit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.jsonl"
            events = [
                {"event_type": "backtest_start", "ts": "2026-07-06T00:00:00+00:00", "total_trade_days": 61},
                {"event_type": "backtest_progress", "day_index": 30, "total_days": 61,
                 "percent": 49.2, "trade_date": "20241112", "elapsed_seconds": 400.0,
                 "orders_so_far": 2},
                {"event_type": "backtest_activity", "ts": "2026-07-06T00:07:00+00:00",
                 "activity": "nl", "activity_status": "running", "nl_call_index": 3,
                 "activity_elapsed_seconds": 0.0},
                {"event_type": "ask_user", "waited_seconds": 12.5},
            ]
            path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")

            running = trace_stats(path)
            self.assertTrue(running["in_backtest"])
            self.assertEqual(running["backtest_progress"]["day_index"], 30)
            self.assertEqual(running["backtest_progress"]["activity"], "nl")
            self.assertEqual(running["backtest_progress"]["activity_status"], "running")
            self.assertEqual(running["backtest_progress"]["nl_call_index"], 3)
            self.assertEqual(
                running["backtest_progress"]["activity_started_at"], "2026-07-06T00:07:00+00:00"
            )
            self.assertAlmostEqual(running["backtest_wall_seconds"], 12.5)

            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"event_type": "backtest", "replay_wall_seconds": 500.0}) + "\n")
            legacy_terminal = trace_stats(path)
            self.assertFalse(legacy_terminal["in_backtest"])
            self.assertAlmostEqual(legacy_terminal["backtest_wall_seconds"], 512.5)

            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"event_type": "budget_exclusion", "reason": "backtest", "seconds": 520.0}) + "\n")
            finished = trace_stats(path)
            self.assertFalse(finished["in_backtest"])
            self.assertIsNone(finished["backtest_progress"])
            self.assertAlmostEqual(finished["backtest_wall_seconds"], 532.5)
            self.assertAlmostEqual(trace_stats(path)["backtest_wall_seconds"], 532.5)

            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"event_type": "backtest_start", "ts": "2026-07-06T01:00:00+00:00"}) + "\n")
                handle.write(json.dumps({"event_type": "backtest", "status": "error", "replay_wall_seconds": 20.0}) + "\n")
            self.assertAlmostEqual(trace_stats(path)["backtest_wall_seconds"], 552.5)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"event_type": "budget_exclusion", "reason": "backtest", "seconds": 25.0}) + "\n")
            failed = trace_stats(path)
            self.assertFalse(failed["in_backtest"])
            self.assertAlmostEqual(failed["backtest_wall_seconds"], 557.5)

    def test_trace_stats_cache_is_atomic_across_concurrent_pollers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.jsonl"
            events = [
                {"event_type": "backtest_start", "ts": "2026-07-06T00:00:00+00:00"},
                {"event_type": "backtest", "replay_wall_seconds": 500.0},
                {"event_type": "budget_exclusion", "reason": "backtest", "seconds": 520.0},
            ]
            path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")

            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(lambda _: trace_stats(path), range(32)))

            self.assertTrue(all(result["backtest_wall_seconds"] == 520.0 for result in results))
            self.assertTrue(all(result["counts"]["budget_exclusion"] == 1 for result in results))

    def test_trace_download_serves_raw_jsonl(self) -> None:
        response = self.client.get("/api/experiments/exp_hitl/trace/download", params={"run_id": "run_001"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.text.strip().splitlines()), 6)
        self.assertIn("attachment", response.headers.get("content-disposition", ""))

    def test_prompt_preview_embeds_directive_and_guards_heldout(self) -> None:
        params_path = self.experiments_root / "exp_hitl" / "hitl" / "params.json"
        params = json.loads(params_path.read_text(encoding="utf-8"))
        params["fold_exploration_directive"] = "持续检验事件冲击沿关系网络的传播。"
        write_json_atomic(params_path, params)
        fold = self.client.post(
            "/api/experiments/exp_hitl/prompt-preview",
            json={"session_key": "epoch_001/fold_2022Q2", "directive": "试试低波动组合"},
        )
        self.assertEqual(fold.status_code, 200, fold.text)
        prompt = fold.json()["prompt"]
        self.assertIn("研究者本 Fold 指令（用户注入）", prompt)
        self.assertIn("试试低波动组合", prompt)
        self.assertIn("实验级默认 Fold 探索方向（用户注入）", prompt)
        self.assertIn("持续检验事件冲击沿关系网络的传播。", prompt)
        self.assertNotIn("test_period", prompt)  # preview mirrors the runtime redaction
        meta = self.client.post(
            "/api/experiments/exp_hitl/prompt-preview",
            json={"session_key": "epoch_001/meta_learning", "directive": "研究流动性冲击"},
        )
        self.assertEqual(meta.status_code, 200)
        self.assertIn("研究流动性冲击", meta.json()["prompt"])
        self.assertIn("实验级默认 Fold 探索方向（用户注入）", meta.json()["prompt"])
        self.assertIn("持续检验事件冲击沿关系网络的传播。", meta.json()["prompt"])
        heldout = self.client.post(
            "/api/experiments/exp_hitl/prompt-preview", json={"session_key": "heldout"}
        )
        self.assertEqual(heldout.status_code, 400)
        missing = self.client.post(
            "/api/experiments/exp_hitl/prompt-preview", json={"session_key": "nope"}
        )
        self.assertEqual(missing.status_code, 404)

    def test_periodic_meta_records_are_unique_causal_and_block_consumed_fold_reruns(self) -> None:
        experiment_dir = self.experiments_root / "exp_hitl"
        hitl = experiment_dir / "hitl"
        base_taste = experiment_dir / "meta_learning" / "epoch_001" / "taste.md"
        periodic_taste = (
            experiment_dir / "meta_learning" / "epoch_001_after_fold_001" / "taste.md"
        )
        base_taste.parent.mkdir(parents=True, exist_ok=True)
        periodic_taste.parent.mkdir(parents=True, exist_ok=True)
        base_taste.write_text("base-taste", encoding="utf-8")
        periodic_taste.write_text("periodic-taste", encoding="utf-8")

        records = [
            json.loads(line)
            for line in (experiment_dir / "ledgers" / "experiment_ledger.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        ]
        records.extend(
            [
                {
                    "record_type": "meta_learning",
                    "experiment_id": "exp_hitl",
                    "epoch_id": "epoch_001",
                    "meta_learning_id": "epoch_001",
                    "trigger_after_folds": 0,
                    "fold_id": "epoch_001_meta_learning",
                    "run_id": "run_meta_base_latest",
                    "status": "taste_only",
                    "taste_path": str(base_taste),
                },
                {
                    "record_type": "meta_learning",
                    "experiment_id": "exp_hitl",
                    "epoch_id": "epoch_001",
                    "meta_learning_id": "epoch_001_after_fold_001",
                    "trigger_after_folds": 1,
                    "fold_id": "epoch_001_after_fold_001_meta_learning",
                    "run_id": "run_meta_periodic",
                    "status": "taste_only_kept_parent",
                    "taste_path": str(periodic_taste),
                },
            ]
        )
        _write_ledger(experiment_dir, records)
        schedule = json.loads((hitl / "schedule.json").read_text(encoding="utf-8"))
        schedule["sessions"].insert(
            2,
            {
                "key": "epoch_001/meta_learning_after_fold_001",
                "kind": "meta_learning",
                "epoch_id": "epoch_001",
                "meta_learning_id": "epoch_001_after_fold_001",
                "trigger_after_folds": 1,
                "before_fold_id": "fold_2022Q2",
            },
        )
        write_json_atomic(hitl / "schedule.json", schedule)

        detail = self.client.get("/api/experiments/exp_hitl").json()
        meta_sessions = [session for session in detail["sessions"] if session["kind"] == "meta_learning"]
        self.assertEqual(len(meta_sessions), 2)
        self.assertEqual(
            [session["record"]["run_id"] for session in meta_sessions],
            ["run_meta_base_latest", "run_meta_periodic"],
        )
        q1_prompt = self.client.post(
            "/api/experiments/exp_hitl/prompt-preview",
            json={"session_key": "epoch_001/fold_2022Q1"},
        ).json()["prompt"]
        q2_prompt = self.client.post(
            "/api/experiments/exp_hitl/prompt-preview",
            json={"session_key": "epoch_001/fold_2022Q2"},
        ).json()["prompt"]
        self.assertIn("base-taste", q1_prompt)
        self.assertNotIn("periodic-taste", q1_prompt)
        self.assertIn("periodic-taste", q2_prompt)

        rerun = self.client.post(
            "/api/experiments/exp_hitl/control",
            json={"action": "rerun_fold", "session_key": "epoch_001/fold_2022Q1"},
        )
        self.assertEqual(rerun.status_code, 400)
        self.assertIn("后续元学习会话", rerun.json()["detail"])

    def test_fold_prompt_preview_waits_for_nearest_pending_meta(self) -> None:
        hitl = self.experiments_root / "exp_hitl" / "hitl"
        schedule = json.loads((hitl / "schedule.json").read_text(encoding="utf-8"))
        schedule["sessions"].insert(
            2,
            {
                "key": "epoch_001/meta_learning_after_fold_001",
                "kind": "meta_learning",
                "epoch_id": "epoch_001",
                "meta_learning_id": "epoch_001_after_fold_001",
                "trigger_after_folds": 1,
            },
        )
        write_json_atomic(hitl / "schedule.json", schedule)

        response = self.client.post(
            "/api/experiments/exp_hitl/prompt-preview",
            json={"session_key": "epoch_001/fold_2022Q2"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("nearest preceding Meta", response.json()["detail"])

    def test_dataset_coverage_reads_partition_bounds(self) -> None:
        from autotrade.webui.registry import dataset_coverage

        raw = self.repo_root / "data" / "raw"
        (raw / "daily").mkdir(parents=True)
        for day in ("20200102", "20240105"):
            (raw / "daily" / f"trade_date={day}.parquet").write_bytes(b"")
        self.assertEqual(dataset_coverage(raw, "daily"), ("20200102", "20240105"))
        self.assertIsNone(dataset_coverage(raw, "stk_mins_1min_by_date"))

    def test_summary_carries_per_period_heldout_returns(self) -> None:
        self._reveal()
        payload = self.client.get("/api/experiments").json()
        hitl = next(e for e in payload["experiments"] if e["experiment_id"] == "exp_hitl")
        self.assertEqual(hitl["heldout_returns"], [{"label": "2023Q1", "return": -0.03}])
        self.assertAlmostEqual(hitl["metrics"]["cum_heldout_return"], -0.03)
        # Long/short trade-type decomposition rides on the fold rows.
        row = hitl["fold_returns"][0]
        self.assertAlmostEqual(row["valid_long"], 0.08)
        self.assertAlmostEqual(row["test_short"], 0.05)

    def test_fold_orders_stats_rows_and_csv_export(self) -> None:
        data = self.client.get("/api/experiments/exp_hitl/folds/epoch_001/fold_2022Q1/orders").json()
        self.assertEqual(data["result"], "valid_000")
        stats = data["stats"]
        self.assertEqual((stats["orders"], stats["filled"], stats["rejected"]), (3, 2, 1))
        self.assertAlmostEqual(stats["turnover"], 500 * 10.0 + 500 * 11.0)
        self.assertEqual(stats["by_action"], {"buy": 2, "sell": 1})
        self.assertEqual(stats["reject_reasons"], {"limit_up_blocked_buy": 1})
        self.assertEqual(len(stats["daily"]), 2)
        self.assertEqual(len(data["rows"]), 3)
        csv_response = self.client.get(
            "/api/experiments/exp_hitl/folds/epoch_001/fold_2022Q1/orders.csv", params={"result": "valid_000"}
        )
        self.assertEqual(csv_response.status_code, 200)
        self.assertIn("attachment", csv_response.headers.get("content-disposition", ""))
        self.assertEqual(len(csv_response.text.strip().splitlines()), 4)  # header + 3 orders
        missing = self.client.get(
            "/api/experiments/exp_hitl/folds/epoch_001/fold_2022Q1/orders.csv", params={"result": "nope"}
        )
        self.assertEqual(missing.status_code, 404)

    def test_style_route_gated_until_reveal(self) -> None:
        results = self.experiments_root / "exp_hitl" / "artifacts" / "run_001" / "results"
        results.mkdir(parents=True, exist_ok=True)
        (results / "style_valid.json").write_text(json.dumps({"prefix": "valid"}), encoding="utf-8")
        (results / "style_test.json").write_text(json.dumps({"prefix": "test"}), encoding="utf-8")
        url = "/api/experiments/exp_hitl/style"
        valid = self.client.get(url, params={"run_id": "run_001", "prefix": "valid"})
        self.assertEqual(valid.status_code, 200, valid.text)
        self.assertEqual(valid.json()["prefix"], "valid")
        hidden = self.client.get(url, params={"run_id": "run_001", "prefix": "test"})
        self.assertEqual(hidden.status_code, 404)
        # Indistinguishable from a run without a rollup: existence must not leak.
        absent = self.client.get(url, params={"run_id": "run_missing", "prefix": "valid"})
        self.assertEqual(hidden.json()["detail"], absent.json()["detail"])
        self._reveal()
        revealed = self.client.get(url, params={"run_id": "run_001", "prefix": "test"})
        self.assertEqual(revealed.status_code, 200, revealed.text)
        self.assertEqual(revealed.json()["prefix"], "test")

    def test_fold_orders_gated_until_reveal(self) -> None:
        import pandas as pd

        test_dir = self.experiments_root / "exp_hitl" / "artifacts" / "run_001" / "results" / "test_000"
        test_dir.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(
            [{"order_id": "t1", "account": "stock", "ts_code": "000001.SZ", "action": "buy",
              "requested_amount": 100, "filled_quantity": 100, "price": 9.0, "status": "filled",
              "reject_reason": "", "decision_time": "09:31", "trade_date": "20220401"}]
        ).to_parquet(test_dir / "orders.parquet", index=False)
        url = "/api/experiments/exp_hitl/folds/epoch_001/fold_2022Q1/orders"
        hidden = self.client.get(url, params={"result": "test_000"})
        self.assertEqual(hidden.status_code, 404)
        # The error enumerates only visible results — test names must not leak.
        self.assertIn("available: ['valid_000']", hidden.json()["detail"])
        listing = self.client.get(url).json()
        self.assertEqual(listing["result"], "valid_000")
        self.assertEqual(listing["test_results"], [])
        csv_hidden = self.client.get(url + ".csv", params={"result": "test_000"})
        self.assertEqual(csv_hidden.status_code, 404)
        self._reveal()
        revealed = self.client.get(url, params={"result": "test_000"})
        self.assertEqual(revealed.status_code, 200, revealed.text)
        self.assertEqual(revealed.json()["result"], "test_000")
        self.assertEqual(self.client.get(url).json()["test_results"], ["test_000"])
        csv_ok = self.client.get(url + ".csv", params={"result": "test_000"})
        self.assertEqual(csv_ok.status_code, 200)
        self.assertEqual(len(csv_ok.text.strip().splitlines()), 2)  # header + 1 order

    def test_analysis_endpoint_serves_existing_markdown(self) -> None:
        payload = self.client.get("/api/experiments/exp_hitl/analysis/epoch_001/fold_2022Q1").json()
        self.assertTrue(payload["available"])
        self.assertIn("策略逻辑概述", payload["content"])
        missing = self.client.get("/api/experiments/exp_hitl/analysis/epoch_001/fold_2022Q2").json()
        self.assertFalse(missing["available"])


if __name__ == "__main__":
    unittest.main()
