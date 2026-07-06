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
import zipfile
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from autotrade.environment.artifacts import artifact_hash
from autotrade.pipelines.interactive import PARAM_DEFAULTS, ControlState, write_control, write_json_atomic
from autotrade.webui.manager import ExperimentManager, ManagerError
from autotrade.webui.server import create_app
from autotrade.webui.traces import read_trace_page


def _write_ledger(experiment_dir: Path, records: list[dict[str, object]]) -> None:
    ledger = experiment_dir / "ledgers" / "experiment_ledger.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")


class WebuiBackendTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo_root = Path(self._tmp.name)
        self.experiments_root = self.repo_root / "experiments"
        self.experiments_root.mkdir(parents=True)
        self._build_hitl_experiment("exp_hitl")
        self._build_legacy_experiment("exp_legacy")
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
        write_control(hitl / "control.json", ControlState(mode="step"))
        write_json_atomic(
            hitl / "status.json",
            {"pid": 999_999_999, "state": "running_session", "session_key": "epoch_001/fold_2022Q2"},
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
                    "validation_result": {"total_return": 0.10, "sharpe": 1.0, "max_drawdown": 0.05},
                    "test_result": {"total_return": 0.20, "sharpe": 1.5, "max_drawdown": 0.04},
                },
            ],
        )
        trace_dir = experiment_dir / "artifacts" / "run_001"
        trace_dir.mkdir(parents=True)
        events = [{"event_type": "llm_call", "seq": index} for index in range(3)]
        (trace_dir / "agent_trace.jsonl").write_text(
            "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
        )
        analysis_dir = hitl / "analysis"
        analysis_dir.mkdir()
        (analysis_dir / "epoch_001__fold_2022Q1.md").write_text("## 策略逻辑概述\nok\n", encoding="utf-8")
        return experiment_dir

    def _build_legacy_experiment(self, experiment_id: str) -> Path:
        experiment_dir = self.experiments_root / experiment_id
        experiment_dir.mkdir(parents=True)
        _write_ledger(
            experiment_dir,
            [
                {
                    "record_type": "fold",
                    "experiment_id": experiment_id,
                    "epoch_id": "epoch_001",
                    "fold_id": "fold_2021Q4",
                    "run_id": "run_legacy",
                    "fold_status": "no_update",
                    "validation_result": {"total_return": -0.02},
                    "test_result": {"total_return": 0.01},
                }
            ],
        )
        return experiment_dir

    # ---- schema & listing ------------------------------------------------------
    def test_parameter_schema_defaults_track_worker_defaults(self) -> None:
        schema = self.client.get("/api/parameter-schema").json()
        fields = {field["key"]: field for group in schema["groups"] for field in group["fields"]}
        self.assertEqual(fields["epochs"]["default"], PARAM_DEFAULTS["epochs"])
        self.assertEqual(fields["model"]["default"], PARAM_DEFAULTS["model"])
        self.assertNotIn("experiments_root", fields)
        self.assertNotIn("work_root", fields)
        self.assertTrue(all(field.get("help") for field in fields.values()))

    def test_list_experiments_marks_kind_state_and_metrics(self) -> None:
        payload = self.client.get("/api/experiments").json()
        by_id = {entry["experiment_id"]: entry for entry in payload["experiments"]}
        hitl = by_id["exp_hitl"]
        self.assertEqual(hitl["kind"], "hitl")
        # Recorded pid is dead -> the active state degrades to interrupted.
        self.assertEqual(hitl["state"], "interrupted")
        self.assertAlmostEqual(hitl["metrics"]["cum_test_return"], 0.20)
        self.assertEqual(hitl["folds_recorded"], 1)
        legacy = by_id["exp_legacy"]
        self.assertEqual(legacy["kind"], "legacy")
        self.assertEqual(legacy["state"], "legacy")

    def test_experiment_detail_merges_schedule_and_records(self) -> None:
        detail = self.client.get("/api/experiments/exp_hitl").json()
        sessions = {session["key"]: session for session in detail["sessions"]}
        self.assertIn("record", sessions["epoch_001/fold_2022Q1"])
        self.assertNotIn("record", sessions["epoch_001/fold_2022Q2"])
        self.assertTrue(sessions["epoch_001/fold_2022Q1"]["analysis_available"])
        self.assertEqual(detail["control"]["mode"], "step")
        self.assertEqual(self.client.get("/api/experiments/nope").status_code, 404)

    # ---- guarded fold view --------------------------------------------------------
    def test_fold_detail_separates_test_audit_from_record(self) -> None:
        detail = self.client.get("/api/experiments/exp_hitl/folds/epoch_001/fold_2022Q1").json()
        self.assertNotIn("test_result", detail["record"])
        self.assertEqual(detail["test_audit"]["test_result"]["total_return"], 0.20)
        self.assertEqual(detail["strategy_files"], [{"path": "main.py", "bytes": detail["strategy_files"][0]["bytes"]}])
        self.assertTrue(detail["analysis"]["available"])

    def test_strategy_file_serves_content_and_blocks_traversal(self) -> None:
        ok = self.client.get(
            "/api/experiments/exp_hitl/folds/epoch_001/fold_2022Q1/strategy-file", params={"path": "main.py"}
        )
        self.assertEqual(ok.status_code, 200)
        self.assertIn("def main", ok.text)
        for bad in ("../../hitl/params.json", "/etc/passwd"):
            response = self.client.get(
                "/api/experiments/exp_hitl/folds/epoch_001/fold_2022Q1/strategy-file", params={"path": bad}
            )
            self.assertEqual(response.status_code, 404, bad)

    def test_strategy_zip_contains_output_tree(self) -> None:
        response = self.client.get("/api/experiments/exp_hitl/folds/epoch_001/fold_2022Q1/strategy.zip")
        self.assertEqual(response.status_code, 200)
        archive = zipfile.ZipFile(io.BytesIO(response.content))
        self.assertEqual(archive.namelist(), ["output/main.py"])

    # ---- trace paging ----------------------------------------------------------------
    def test_trace_pagination_and_partial_tail(self) -> None:
        first = self.client.get("/api/experiments/exp_hitl/trace", params={"run_id": "run_001"}).json()
        self.assertEqual(len(first["events"]), 3)
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
                    }
                },
            )
        self.assertEqual(response.status_code, 200, response.text)
        params = json.loads((self.experiments_root / "exp_new" / "hitl" / "params.json").read_text(encoding="utf-8"))
        self.assertEqual(params["epochs"], 2)
        self.assertTrue(params["work_root"].endswith("/.runtime/sandboxes/exp_new"))
        self.assertTrue((self.experiments_root / "exp_new" / "hitl" / "control.json").exists())
        # Duplicate and invalid ids are rejected before touching the disk.
        duplicate = self.client.post("/api/experiments", json={"params": params})
        self.assertEqual(duplicate.status_code, 400)
        bad = self.client.post("/api/experiments", json={"params": {**params, "experiment_id": "../evil"}})
        self.assertEqual(bad.status_code, 400)
        unknown = self.client.post("/api/experiments", json={"params": {**params, "experiment_id": "x2", "bogus": 1}})
        self.assertEqual(unknown.status_code, 400)
        self.assertIn("unknown experiment parameters", unknown.json()["detail"])

    def test_running_cap_blocks_fifth_experiment(self) -> None:
        manager = ExperimentManager(self.repo_root, self.experiments_root)
        with patch.object(ExperimentManager, "running_experiments", return_value=["a", "b", "c", "d"]):
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
        legacy = self.client.post("/api/experiments/exp_legacy/control", json={"action": "pause"})
        self.assertEqual(legacy.status_code, 400)

    def test_delete_requires_confirm_and_no_live_worker(self) -> None:
        missing_confirm = self.client.delete("/api/experiments/exp_legacy")
        self.assertEqual(missing_confirm.status_code, 400)
        # Simulate a live worker on the HITL experiment (our own pid is alive).
        write_json_atomic(
            self.experiments_root / "exp_hitl" / "hitl" / "status.json",
            {"pid": os.getpid(), "state": "running_session"},
        )
        alive = self.client.delete("/api/experiments/exp_hitl", params={"confirm": "exp_hitl"})
        self.assertEqual(alive.status_code, 409)
        gone = self.client.delete("/api/experiments/exp_legacy", params={"confirm": "exp_legacy"})
        self.assertEqual(gone.status_code, 200)
        self.assertFalse((self.experiments_root / "exp_legacy").exists())

    def test_analysis_endpoint_serves_existing_markdown(self) -> None:
        payload = self.client.get("/api/experiments/exp_hitl/analysis/epoch_001/fold_2022Q1").json()
        self.assertTrue(payload["available"])
        self.assertIn("策略逻辑概述", payload["content"])
        missing = self.client.get("/api/experiments/exp_hitl/analysis/epoch_001/fold_2022Q2").json()
        self.assertFalse(missing["available"])


if __name__ == "__main__":
    unittest.main()
