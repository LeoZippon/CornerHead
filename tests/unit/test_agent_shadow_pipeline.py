# Consolidated unit tests: test_agent_shadow_pipeline.py


# Source: test_llm_shadow_pipeline.py
import json
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
import subprocess
import sys

import pandas as pd

from hl_trader.agent.evidence import EvidencePackBuilder
from hl_trader.pipelines.llm_shadow import (
    DEFAULT_EVIDENCE_OUT,
    DEFAULT_SHADOW_LEDGER_PATH,
    LLMShadowPipeline,
    build_evidence_pack_from_feature_file,
    load_evidence_records,
)
from hl_trader.agent.shadow import LLMShadowAdvisor, NLShadowRecorder
from hl_trader.environment.storage import TrialLedger


@dataclass(frozen=True)
class FakeResponse:
    content: str
    model: str = "test-model"
    usage: dict | None = None
    response_id: str = "resp"

    def json_content(self):
        return json.loads(self.content)


class FakeClient:
    def chat_json(self, messages, *, max_tokens=None):
        return FakeResponse(
            content=(
                '{"decisions":['
                '{"ts_code":"000001.SZ","action":"hold","confidence":0.7,"rationale":"ok"},'
                '{"ts_code":"000002.SZ","action":"human_review","confidence":0.2,"rationale":"thin"}'
                ']}'
            ),
            usage={"total_tokens": 22},
            response_id="resp",
        )


def feature_frame():
    return pd.DataFrame([
        {
            "feature_date": "20200131",
            "source_trade_date": "20200131",
            "tradable_date": "20200203",
            "available_at": "2020-01-31T18:00:00+08:00",
            "ts_code": "000001.SZ",
            "pe_ttm": 8.0,
            "pb": 0.8,
            "pct_chg": 10.2,
            "amount": 100000.0,
            "amount_ma20": 20000.0,
            "ret_20d": 0.03,
            "limit": "U",
        },
        {
            "feature_date": "20200131",
            "source_trade_date": "20200131",
            "tradable_date": "20200203",
            "available_at": "2020-01-31T18:00:00+08:00",
            "ts_code": "000002.SZ",
            "pe_ttm": 20.0,
            "pb": 2.1,
            "pct_chg": -1.0,
            "amount": 50000.0,
            "amount_ma20": 40000.0,
            "ret_20d": -0.01,
            "limit": "",
        },
    ])


class LLMShadowPipelineTest(unittest.TestCase):
    def test_build_evidence_from_feature_file_and_detect_checkpoints(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            feature_path = Path(tmpdir) / "features.parquet"
            evidence_path = Path(tmpdir) / "evidence.jsonl"
            feature_frame().to_parquet(feature_path)
            pack, checkpoints = build_evidence_pack_from_feature_file(
                feature_path,
                decision_date="20200131",
                tradable_date="20200203",
                ts_codes=["000001.SZ", "000002.SZ"],
                evidence_out=evidence_path,
            )
            records = EvidencePackBuilder.read_jsonl(evidence_path)
        self.assertEqual(records[0]["pack_id"], pack.pack_id)
        self.assertEqual({item["event_type"] for item in checkpoints}, {"large_price_move", "large_amount_spike", "price_limit_status"})
        payload = records[0]["items"][0]["payload"]
        self.assertEqual(payload["pit"]["decision_date"], "20200131")
        self.assertEqual(payload["pit"]["tradable_date"], "20200203")
        self.assertEqual(payload["units"]["pct_chg"], "percent")
        self.assertEqual(payload["units"]["amount"], "thousand_cny")

    def test_feature_file_requires_pit_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            feature_path = Path(tmpdir) / "features.parquet"
            feature_frame().drop(columns=["available_at"]).to_parquet(feature_path)
            with self.assertRaisesRegex(ValueError, "missing PIT columns"):
                build_evidence_pack_from_feature_file(
                    feature_path,
                    decision_date="20200131",
                    tradable_date="20200203",
                    ts_codes=["000001.SZ"],
                )

    def test_pipeline_writes_shadow_decisions_and_run_records(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger_path = Path(tmpdir) / "shadow.jsonl"
            pipeline = LLMShadowPipeline(
                LLMShadowAdvisor(FakeClient(), provider_name="test-provider"),
                recorder=NLShadowRecorder(ledger_path),
                run_ledger=TrialLedger(ledger_path),
            )
            pack = EvidencePackBuilder().from_feature_cross_section(
                feature_frame(),
                decision_date="20200131",
                tradable_date="20200203",
                ts_codes=["000001.SZ", "000002.SZ"],
                feature_columns=["pe_ttm", "pb", "pct_chg", "amount", "amount_ma20", "ret_20d"],
            )
            result = pipeline.run_records([pack.to_record()])
            records = NLShadowRecorder(ledger_path).read_all()
        self.assertEqual(result.decisions, 2)
        self.assertTrue(any(record["event_type"] == "llm_shadow_pack" for record in records))
        pack_record = next(record for record in records if record["event_type"] == "llm_shadow_pack")
        self.assertEqual(pack_record["provider_metadata"]["provider"], "test-provider")
        self.assertEqual(len([record for record in records if record["event_type"] == "nl_shadow_decision"]), 2)
        for record in records:
            self.assertFalse(record.get("can_affect_trading", False))
            if record["event_type"] == "nl_shadow_decision":
                self.assertFalse(record["decision"]["can_affect_trading"])
                self.assertEqual(record["decision"]["action_impact"], "shadow_only")

    def test_pipeline_dry_run_does_not_call_advisor(self):
        class RaisingAdvisor:
            def advise(self, *args, **kwargs):
                raise AssertionError("should not call advisor")

        with tempfile.TemporaryDirectory() as tmpdir:
            ledger_path = Path(tmpdir) / "shadow.jsonl"
            pipeline = LLMShadowPipeline(
                RaisingAdvisor(),
                recorder=NLShadowRecorder(ledger_path),
                run_ledger=TrialLedger(ledger_path),
            )
            pack = EvidencePackBuilder().from_feature_cross_section(
                feature_frame(),
                decision_date="20200131",
                tradable_date="20200203",
                ts_codes=["000001.SZ"],
                feature_columns=["pe_ttm", "pb"],
            )
            result = pipeline.run_records([pack.to_record()], dry_run=True)
            records = TrialLedger(ledger_path).read_all()
        self.assertTrue(result.dry_run)
        self.assertEqual(records[0]["event_type"], "llm_shadow_dry_run")

    def test_pipeline_dry_run_rejects_tampered_pack_before_ledger(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger_path = Path(tmpdir) / "shadow.jsonl"
            pipeline = LLMShadowPipeline.dry_run_only(shadow_ledger_path=ledger_path)
            pack = EvidencePackBuilder().from_feature_cross_section(
                feature_frame(),
                decision_date="20200131",
                tradable_date="20200203",
                ts_codes=["000001.SZ"],
                feature_columns=["pe_ttm", "pb"],
            )
            record = pack.to_record()
            record["items"][0]["payload"]["rows"][0]["pb"] = 9.9
            with self.assertRaisesRegex(ValueError, "payload_hash verification failed"):
                pipeline.run_records([record], dry_run=True)
            self.assertEqual(TrialLedger(ledger_path).read_all(), [])

    def test_load_evidence_records_respects_max_packs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "evidence.jsonl"
            builder = EvidencePackBuilder()
            pack = builder.from_feature_cross_section(
                feature_frame(),
                decision_date="20200131",
                tradable_date="20200203",
                ts_codes=["000001.SZ"],
                feature_columns=["pe_ttm", "pb"],
            )
            builder.append_jsonl(path, pack)
            builder.append_jsonl(path, pack)
            self.assertEqual(len(load_evidence_records(path, max_packs=1)), 1)

    def test_load_evidence_records_rejects_tampered_hash(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "evidence.jsonl"
            builder = EvidencePackBuilder()
            pack = builder.from_feature_cross_section(
                feature_frame(),
                decision_date="20200131",
                tradable_date="20200203",
                ts_codes=["000001.SZ"],
                feature_columns=["pe_ttm", "pb"],
            )
            builder.append_jsonl(path, pack)
            record = json.loads(path.read_text(encoding="utf-8"))
            record["items"][0]["payload"]["rows"][0]["pe_ttm"] = 99.0
            path.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "payload_hash verification failed"):
                load_evidence_records(path)

    def test_default_output_paths_are_gitignored(self):
        ignore_text = Path(".gitignore").read_text(encoding="utf-8")
        self.assertTrue(str(DEFAULT_EVIDENCE_OUT).startswith("data/"))
        self.assertTrue(str(DEFAULT_SHADOW_LEDGER_PATH).startswith("experiments/trial_ledger/"))
        self.assertIn("/data/", ignore_text)
        self.assertIn("/experiments/trial_ledger/", ignore_text)

    def test_cli_feature_file_dry_run_does_not_require_api_key(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            feature_path = Path(tmpdir) / "features.parquet"
            evidence_path = Path(tmpdir) / "evidence.jsonl"
            ledger_path = Path(tmpdir) / "shadow.jsonl"
            feature_frame().to_parquet(feature_path)
            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/hl.py",
                    "llm-shadow",
                    "--feature-file",
                    str(feature_path),
                    "--decision-date",
                    "20200131",
                    "--tradable-date",
                    "20200203",
                    "--ts-code",
                    "000001.SZ",
                    "--evidence-out",
                    str(evidence_path),
                    "--shadow-ledger",
                    str(ledger_path),
                    "--dry-run",
                ],
                cwd=Path(__file__).resolve().parents[2],
                env={"PYTHONPATH": "src"},
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"status": "ok"', result.stdout)
        self.assertNotIn("sk-", result.stdout + result.stderr)

    def test_cli_existing_evidence_dry_run_validates_hash_without_api_key(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            evidence_path = Path(tmpdir) / "evidence.jsonl"
            ledger_path = Path(tmpdir) / "shadow.jsonl"
            builder = EvidencePackBuilder()
            pack = builder.from_feature_cross_section(
                feature_frame(),
                decision_date="20200131",
                tradable_date="20200203",
                ts_codes=["000001.SZ"],
                feature_columns=["pe_ttm", "pb"],
            )
            builder.append_jsonl(evidence_path, pack)
            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/hl.py",
                    "llm-shadow",
                    "--evidence-jsonl",
                    str(evidence_path),
                    "--shadow-ledger",
                    str(ledger_path),
                    "--dry-run",
                ],
                cwd=Path(__file__).resolve().parents[2],
                env={"PYTHONPATH": "src"},
                text=True,
                capture_output=True,
                check=False,
            )
            records = TrialLedger(ledger_path).read_all()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(records[0]["event_type"], "llm_shadow_dry_run")
        self.assertFalse(records[0]["can_affect_trading"])
