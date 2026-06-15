import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from hl_trader.environment.llm.proxy import LLMProxyError, ScriptedLLM
from hl_trader.environment.nl import (
    ExtractionError,
    NLScoringConfig,
    NLScoringEngine,
    TextRetriever,
    extract_json_object,
    validate_score_payload,
)


def score_json(ts_code="000001.SZ", evidence_ids=(), nl_score=0.5, applied_prior_ids=("r1",)):
    return json.dumps(
        {
            "ts_code": ts_code,
            "nl_score": nl_score,
            "confidence": 0.8,
            "risk_tags": [],
            "applied_prior_ids": list(applied_prior_ids),
            "evidence_ids": list(evidence_ids),
        }
    )


class ExtractionTest(unittest.TestCase):
    def test_accepts_plain_json_object(self):
        extracted = extract_json_object('{"a": 1}')
        self.assertEqual(extracted.payload, {"a": 1})

    def test_accepts_one_json_fence(self):
        extracted = extract_json_object('```json\n{"a": 1}\n```')
        self.assertEqual(extracted.payload, {"a": 1})

    def test_strips_closed_think_block_and_keeps_it_for_logging(self):
        extracted = extract_json_object('<think>internal reasoning</think>{"a": 1}')
        self.assertEqual(extracted.payload, {"a": 1})
        self.assertEqual(extracted.stripped_think, "internal reasoning")

    def test_rejects_unclosed_think(self):
        with self.assertRaisesRegex(ExtractionError, "unclosed"):
            extract_json_object('<think>still thinking {"a": 1}')

    def test_rejects_multiple_json_objects_or_trailing_text(self):
        with self.assertRaisesRegex(ExtractionError, "beyond a single JSON object"):
            extract_json_object('{"a": 1}{"b": 2}')
        with self.assertRaisesRegex(ExtractionError, "beyond a single JSON object"):
            extract_json_object('{"a": 1} the score is 0.9')

    def test_tool_call_arguments_take_precedence(self):
        extracted = extract_json_object("ignored", tool_call_arguments='{"b": 2}')
        self.assertEqual(extracted.payload, {"b": 2})

    def test_score_payload_validation(self):
        payload = json.loads(score_json(evidence_ids=["t1"]))
        validated = validate_score_payload(
            payload,
            expected_ts_code="000001.SZ",
            seen_evidence_ids={"t1"},
            valid_prior_ids={"r1"},
            require_prior_id=True,
        )
        self.assertEqual(validated["nl_score"], 0.5)
        with self.assertRaisesRegex(ExtractionError, "unknown evidence"):
            validate_score_payload(payload, expected_ts_code="000001.SZ", seen_evidence_ids=set(), valid_prior_ids={"r1"})
        with self.assertRaisesRegex(ExtractionError, "does not match"):
            validate_score_payload(payload, expected_ts_code="000009.SZ", seen_evidence_ids={"t1"}, valid_prior_ids={"r1"})
        bad = dict(payload, nl_score=1.5)
        with self.assertRaisesRegex(ExtractionError, "out of range"):
            validate_score_payload(bad, expected_ts_code="000001.SZ", seen_evidence_ids={"t1"}, valid_prior_ids={"r1"})
        bad_prior = dict(payload, applied_prior_ids=["unknown"])
        with self.assertRaisesRegex(ExtractionError, "unknown prior"):
            validate_score_payload(bad_prior, expected_ts_code="000001.SZ", seen_evidence_ids={"t1"}, valid_prior_ids={"r1"})
        neutral = dict(payload, nl_score=0.0, applied_prior_ids=[], evidence_ids=[], risk_tags=[])
        validated = validate_score_payload(
            neutral,
            expected_ts_code="000001.SZ",
            seen_evidence_ids=set(),
            valid_prior_ids={"r1"},
            require_prior_id=True,
        )
        self.assertEqual(validated["applied_prior_ids"], [])


class EngineTest(unittest.TestCase):
    def make_engine(self, responses, tmp: Path):
        index = pd.DataFrame(
            [
                {
                    "text_id": "t1",
                    "dataset": "anns_d",
                    "ts_codes": "000001.SZ",
                    "title": "平安银行 公告",
                    "available_at": "2021-10-01T18:00:00+08:00",
                    "source_hash": "h",
                    "library_file": "anns_d.parquet",
                }
            ]
        )
        index_path = tmp / "text_index.parquet"
        index.to_parquet(index_path, index=False)
        library = tmp / "text_library"
        library.mkdir(parents=True)
        pd.DataFrame({"text_id": ["t1"], "body": ["公告正文"]}).to_parquet(library / "anns_d.parquet", index=False)
        retriever = TextRetriever(index_path, library)
        proxy = ScriptedLLM(responses)
        engine = NLScoringEngine(
            proxy,
            retriever,
            prior_rules=[{"id": "r1", "text": "t", "evidence": "e", "effect": "x"}],
            scoring_readme="score in [-1,1]",
            company_contexts={"000001.SZ": {"ts_code": "000001.SZ", "name": "平安银行"}},
        )
        return engine, proxy

    def test_early_final_json_completes_without_more_rounds(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine, proxy = self.make_engine([score_json()], Path(tmp))
            batch = engine.score_candidates(["000001.SZ"], NLScoringConfig(mode="on", max_workers=1))
            result = batch.results[0]
            self.assertEqual(result.state, "completed")
            self.assertEqual(result.rounds, 1)
            self.assertEqual(len(proxy.calls), 1)
            self.assertEqual(result.score["nl_score"], 0.5)

    def test_search_then_final_with_valid_evidence_citation(self):
        search = json.dumps({"search_requests": [{"keywords": ["公告"], "max_results": 3}]})
        with tempfile.TemporaryDirectory() as tmp:
            engine, _ = self.make_engine([search, score_json(evidence_ids=["t1"])], Path(tmp))
            batch = engine.score_candidates(["000001.SZ"], NLScoringConfig(mode="on", max_workers=1))
            result = batch.results[0]
            self.assertEqual(result.state, "completed")
            self.assertEqual(result.rounds, 2)
            self.assertEqual(len(result.evidence), 1)
            self.assertEqual(result.score["evidence_ids"], ["t1"])

    def test_citation_by_source_hash_is_accepted(self):
        # docs/environment_design.md 4.4: scores may cite text_id OR source_hash.
        search = json.dumps({"search_requests": [{"keywords": ["公告"], "max_results": 3}]})
        with tempfile.TemporaryDirectory() as tmp:
            engine, _ = self.make_engine([search, score_json(evidence_ids=["h"])], Path(tmp))
            batch = engine.score_candidates(["000001.SZ"], NLScoringConfig(mode="on", max_workers=1))
            self.assertEqual(batch.results[0].state, "completed")

    def test_invalid_final_then_repair_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine, _ = self.make_engine(
                ['{"ts_code": "000001.SZ", "nl_score": 2.5, "confidence": 0.5, "risk_tags": [], "applied_prior_ids": [], "evidence_ids": []}', score_json()],
                Path(tmp),
            )
            batch = engine.score_candidates(["000001.SZ"], NLScoringConfig(mode="on", max_workers=1))
            self.assertEqual(batch.results[0].state, "completed")

    def test_failed_state_blocks_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine, _ = self.make_engine(["not json at all", "still not json"], Path(tmp))
            batch = engine.score_candidates(
                ["000001.SZ"], NLScoringConfig(mode="on", max_workers=1, allow_repair_call=False)
            )
            self.assertEqual(batch.results[0].state, "failed")
            self.assertTrue(batch.has_blocking_failure())

    def test_neutral_failure_policy_is_auditable(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine, _ = self.make_engine(["nope"], Path(tmp))
            batch = engine.score_candidates(
                ["000001.SZ"],
                NLScoringConfig(mode="on", max_workers=1, allow_repair_call=False, failure_policy="neutral_with_audit"),
            )
            result = batch.results[0]
            self.assertEqual(result.state, "failed_with_policy")
            self.assertEqual(result.score["nl_score"], 0.0)
            self.assertFalse(batch.has_blocking_failure())

    def test_timeout_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine, _ = self.make_engine([LLMProxyError("provider timed out", timeout=True)], Path(tmp))
            batch = engine.score_candidates(["000001.SZ"], NLScoringConfig(mode="on", max_workers=1))
            self.assertEqual(batch.results[0].state, "timeout")

    def test_off_and_sample_modes(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine, _ = self.make_engine([score_json()], Path(tmp))
            off = engine.score_candidates(["000001.SZ"], NLScoringConfig(mode="off"))
            self.assertEqual(off.results[0].state, "skipped_by_config")
            sample_dir = Path(tmp) / "x2"
            sample_dir.mkdir()
            engine2, _ = self.make_engine([score_json()], sample_dir)
            sample = engine2.score_candidates(
                ["000001.SZ", "000002.SZ"], NLScoringConfig(mode="sample", sample_size=1, max_workers=1)
            )
            states = {r.ts_code: r.state for r in sample.results}
            self.assertEqual(states["000001.SZ"], "completed")
            self.assertEqual(states["000002.SZ"], "skipped_by_config")

    def test_grep_pattern_retrieval_and_body_grep(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine, _ = self.make_engine([], Path(tmp))
            retriever = engine.retriever
            # regex alternation over titles
            hits = retriever.search("公告|处罚", ts_code="000001.SZ", max_results=5)
            self.assertEqual(len(hits), 1)
            self.assertEqual(hits[0]["text_id"], "t1")
            # body-only grep: pattern appears only in the body text
            hits = retriever.search("正文", ts_code="000001.SZ", max_results=5)
            self.assertEqual(len(hits), 1)
            # invalid regex falls back to a literal match instead of crashing
            self.assertEqual(retriever.search("([bad", ts_code="000001.SZ"), [])

    def test_candidate_related_hits_rank_before_generic_hits(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            index = pd.DataFrame(
                [
                    {
                        "text_id": "own",
                        "dataset": "anns_d",
                        "ts_codes": "000001.SZ",
                        "title": "平安银行 监管问询回复",
                        "available_at": "2021-10-02T18:00:00+08:00",
                        "source_hash": "h1",
                    },
                    {
                        "text_id": "other",
                        "dataset": "anns_d",
                        "ts_codes": "000002.SZ",
                        "title": "其他公司 监管问询",
                        "available_at": "2021-10-03T18:00:00+08:00",
                        "source_hash": "h2",
                    },
                ]
            )
            index_path = tmp / "text_index.parquet"
            index.to_parquet(index_path, index=False)
            library = tmp / "text_library"
            library.mkdir()
            pd.DataFrame({"text_id": ["own", "other"], "body": ["正文", "正文"]}).to_parquet(
                library / "anns_d.parquet", index=False
            )
            retriever = TextRetriever(index_path, library)
            hits = retriever.search("监管问询", ts_code="000001.SZ", max_results=2)
            self.assertEqual([hit["text_id"] for hit in hits], ["own", "other"])
            self.assertEqual([hit["relevance"] for hit in hits], ["candidate", "background"])

    def test_background_evidence_cannot_be_cited_as_candidate_evidence(self):
        search = json.dumps({"search_requests": [{"pattern": "监管问询", "max_results": 2}]})
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            index = pd.DataFrame(
                [
                    {
                        "text_id": "own",
                        "dataset": "anns_d",
                        "ts_codes": "000001.SZ",
                        "title": "平安银行 普通公告",
                        "available_at": "2021-10-01T18:00:00+08:00",
                        "source_hash": "h1",
                    },
                    {
                        "text_id": "other",
                        "dataset": "anns_d",
                        "ts_codes": "000002.SZ",
                        "title": "其他公司 监管问询",
                        "available_at": "2021-10-02T18:00:00+08:00",
                        "source_hash": "h2",
                    },
                ]
            )
            index_path = tmp / "text_index.parquet"
            index.to_parquet(index_path, index=False)
            library = tmp / "text_library"
            library.mkdir()
            pd.DataFrame({"text_id": ["own", "other"], "body": ["正文", "正文"]}).to_parquet(
                library / "anns_d.parquet", index=False
            )
            engine = NLScoringEngine(
                ScriptedLLM([search, score_json(evidence_ids=["other"]), score_json(nl_score=0.0, applied_prior_ids=())]),
                TextRetriever(index_path, library),
                prior_rules=[{"id": "r1", "text": "t", "evidence": "e", "effect": "x"}],
                scoring_readme="score in [-1,1]",
                company_contexts={"000001.SZ": {"ts_code": "000001.SZ", "name": "平安银行"}},
            )
            batch = engine.score_candidates(["000001.SZ"], NLScoringConfig(mode="on", max_workers=1))
            result = batch.results[0]
            self.assertEqual(result.state, "completed")
            self.assertEqual(result.score["nl_score"], 0.0)
            self.assertEqual(result.score["evidence_ids"], [])

    def test_legacy_keyword_requests_map_to_patterns(self):
        search = json.dumps({"search_requests": [{"keywords": ["公告", "处罚"], "max_results": 3}]})
        with tempfile.TemporaryDirectory() as tmp:
            engine, _ = self.make_engine([search, score_json(evidence_ids=["t1"])], Path(tmp))
            batch = engine.score_candidates(["000001.SZ"], NLScoringConfig(mode="on", max_workers=1))
            result = batch.results[0]
            self.assertEqual(result.state, "completed")
            self.assertEqual(result.search_requests[0]["pattern"], "公告|处罚")

    def test_prompts_contain_only_ts_code_for_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine, proxy = self.make_engine([score_json()], Path(tmp))
            engine.score_candidates(["000001.SZ"], NLScoringConfig(mode="on", max_workers=1))
            user_payload = json.loads(proxy.calls[0]["messages"][1]["content"])
            self.assertEqual(user_payload["candidate"], {"ts_code": "000001.SZ"})
            self.assertNotIn("factor_score", json.dumps(user_payload))


if __name__ == "__main__":
    unittest.main()
