import json
import tempfile
import time
import unittest
from pathlib import Path

import pandas as pd

from autotrade.environment.llm.proxy import (
    LLMProxyError,
    ProviderResponse,
    ScriptedLLM,
    tool_call as make_tool_call,
    tool_call_response,
)
from autotrade.environment.nl import (
    ExtractionError,
    NLSubAgentConfig,
    NLSubAgentEngine,
    TextRetriever,
    extract_json_object,
)
from autotrade.environment.nl.engine import TEXT_RETRIEVE_SCHEMA, TEXT_RETRIEVE_SPEC


def tool_call(pattern: str = "公告", *, max_results: int = 3):
    return tool_call_response(make_tool_call("text_retrieve", pattern=pattern, max_results=max_results))


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
            extract_json_object('{"a": 1} trailing')

    def test_tool_call_arguments_take_precedence(self):
        extracted = extract_json_object("ignored", tool_call_arguments='{"b": 2}')
        self.assertEqual(extracted.payload, {"b": 2})


class NLSubAgentEngineTest(unittest.TestCase):
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
        engine = NLSubAgentEngine(
            proxy,
            retriever,
            company_contexts={"000001.SZ": {"ts_code": "000001.SZ", "name": "平安银行"}},
        )
        return engine, proxy

    def run_agent(self, engine: NLSubAgentEngine, *, prompt: str = "分析可见文本"):
        return engine.run(
            ts_code="000001.SZ",
            prompt=prompt,
            request_kwargs={},
            config=NLSubAgentConfig(per_call_timeout_seconds=30),
        )

    def test_freeform_final_content_is_returned_without_score_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine, proxy = self.make_engine(["可见文本不足，结论保持中性。"], Path(tmp))
            result = self.run_agent(engine)
            self.assertEqual(result.state, "completed")
            self.assertIn("中性", result.content)
            self.assertEqual(result.tool_calls, [])
            self.assertIn("tools", proxy.calls[0])

    def test_enum_contract_bounds_provider_work_and_canonicalizes_first_value(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        with tempfile.TemporaryDirectory() as tmp:
            engine, proxy = self.make_engine(
                [tool_call("公告", max_results=20), "**DOWNGRADE** because evidence; not REJECT"],
                Path(tmp),
            )
            engine.retriever.as_of = datetime(
                2021, 10, 2, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai")
            )
            engine.retriever._snippets[("anns_d", "t1")] = "x" * 1500
            result = engine.run(
                ts_code="000001.SZ",
                prompt="classify",
                request_kwargs={},
                config=NLSubAgentConfig(
                    per_call_timeout_seconds=30,
                    max_tokens=128,
                    max_tool_rounds=1,
                    response_choices=("PASS", "DOWNGRADE", "REJECT"),
                    max_results_per_search=5,
                    max_evidence_snippet_chars=1000,
                ),
            )

            self.assertEqual(result.state, "completed")
            self.assertEqual(result.content, "DOWNGRADE")
            self.assertEqual(len(proxy.calls), 2)
            self.assertEqual([call["max_tokens"] for call in proxy.calls], [128, 128])
            self.assertEqual([call["thinking_enabled"] for call in proxy.calls], [None, None])
            self.assertEqual(proxy.calls[-1]["tool_choice"], "none")
            self.assertEqual(result.tool_calls[0]["arguments"]["max_results"], 5)
            self.assertEqual(len(result.evidence[0]["snippet"]), 1500)
            tool_message = next(
                message for message in proxy.calls[-1]["messages"] if message["role"] == "tool"
            )
            provider_payload = json.loads(tool_message["content"])
            self.assertEqual(len(provider_payload["results"][0]["snippet"]), 1000)
            initial = json.loads(proxy.calls[0]["messages"][1]["content"])
            self.assertIn("2021-10-02", initial["decision_as_of"])
            self.assertEqual(initial["response_contract"]["values"], ["PASS", "DOWNGRADE", "REJECT"])

    def test_enum_contract_rejects_unrecognized_provider_answer(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine, _ = self.make_engine(["MAYBE"], Path(tmp))
            result = engine.run(
                ts_code="000001.SZ",
                prompt="classify",
                request_kwargs={},
                config=NLSubAgentConfig(
                    failure_policy="return_error_with_audit",
                    response_choices=("PASS", "REJECT"),
                ),
            )
            self.assertEqual(result.state, "failed_with_policy")
            self.assertEqual(result.content, "")
            self.assertEqual(result.llm_calls[0]["content"], "MAYBE")

    def test_text_retrieve_schema_is_generated_from_standard_spec(self):
        schema = TEXT_RETRIEVE_SCHEMA["function"]["parameters"]
        self.assertEqual(TEXT_RETRIEVE_SCHEMA, TEXT_RETRIEVE_SPEC.to_tool_schema())
        self.assertEqual(TEXT_RETRIEVE_SPEC.schema_version, 1)
        self.assertEqual(TEXT_RETRIEVE_SPEC.result_policy, "bounded_structured_evidence")
        self.assertEqual(schema["required"], ["pattern"])
        self.assertIn("ts_code", schema["properties"])

    def test_tool_call_then_freeform_answer(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine, _ = self.make_engine([tool_call("平安银行|公告"), "公告正文未见重大负面事项。"], Path(tmp))
            result = self.run_agent(engine)
            self.assertEqual(result.state, "completed")
            self.assertEqual(result.rounds, 2)
            self.assertEqual(result.tool_calls[0]["arguments"]["pattern"], "平安银行|公告")
            self.assertGreaterEqual(result.tool_calls[0]["duration_seconds"], 0.0)
            self.assertEqual(result.evidence[0]["text_id"], "t1")
            self.assertIn("重大负面", result.content)

    def test_stock_task_cannot_override_its_candidate_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            request = make_tool_call(
                "text_retrieve", pattern="公告", ts_code="000002.SZ"
            )
            engine, _ = self.make_engine(
                [tool_call_response(request), "候选边界保持不变。"], Path(tmp)
            )

            result = self.run_agent(engine)

            self.assertEqual(result.tool_calls[0]["arguments"]["ts_code"], "000001.SZ")
            self.assertEqual([item["text_id"] for item in result.evidence], ["t1"])

    def test_general_nl_request_has_no_single_stock_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine, _ = self.make_engine([tool_call("公告"), "全局文本检索完成。"], Path(tmp))
            result = engine.run(
                ts_code="",
                prompt="检索当前可见文本里的市场事件",
                request_kwargs={},
                config=NLSubAgentConfig(per_call_timeout_seconds=30),
            )
            self.assertEqual(result.state, "completed")
            self.assertEqual(result.to_record()["scope"], "general")
            self.assertEqual(result.company_context["scope"], "general")
            self.assertEqual(result.tool_calls[0]["arguments"]["ts_code"], "")
            self.assertEqual(result.evidence[0]["relevance"], "background")

    def test_content_alongside_tool_call_is_executed(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = tool_call_response(
                make_tool_call("text_retrieve", pattern="平安银行|公告"), content="我需要先检索。"
            )
            engine, _ = self.make_engine([first, "检索后结论保持中性。"], Path(tmp))
            result = self.run_agent(engine)
            self.assertEqual(result.state, "completed")
            self.assertEqual(result.rounds, 2)
            self.assertEqual(result.tool_calls[0]["arguments"]["pattern"], "平安银行|公告")
            self.assertEqual(result.evidence[0]["text_id"], "t1")

    def test_json_final_is_allowed_but_not_validated_as_score(self):
        with tempfile.TemporaryDirectory() as tmp:
            content = '{"bias": "positive", "confidence": "low", "note": "agent-defined"}'
            engine, _ = self.make_engine([content], Path(tmp))
            result = self.run_agent(engine)
            self.assertEqual(result.state, "completed")
            self.assertEqual(result.content, content)

    def test_return_error_policy_is_auditable_without_neutral_score(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine, _ = self.make_engine(
                [LLMProxyError("provider timed out Bearer secret-token-abc", timeout=True)], Path(tmp)
            )
            result = engine.run(
                ts_code="000001.SZ",
                prompt="fixture",
                request_kwargs={},
                config=NLSubAgentConfig(failure_policy="return_error_with_audit"),
            )
            record = result.to_record()
            self.assertEqual(result.state, "timeout")
            self.assertEqual(record["status"], "error")
            self.assertNotIn("nl_score", record)
            self.assertIn("timed out", result.error)
            self.assertNotIn("secret-token-abc", result.error)

    def test_invalid_native_tool_arguments_return_tool_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad_call = {
                "id": "call_bad",
                "type": "function",
                "function": {"name": "text_retrieve", "arguments": "{"},
            }
            engine, _ = self.make_engine([tool_call_response(bad_call), "参数错误后保持中性。"], Path(tmp))
            result = self.run_agent(engine)
            self.assertEqual(result.state, "completed")
            self.assertEqual(result.tool_calls[0]["status"], "error")
            self.assertIn("invalid text_retrieve arguments JSON", result.tool_calls[0]["error"])
            self.assertEqual(result.evidence, [])
            self.assertIn("中性", result.content)

    def test_unknown_native_tool_call_returns_tool_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine, _ = self.make_engine(
                [tool_call_response(make_tool_call("unknown_tool", query="公告")), "未知工具后保持中性。"],
                Path(tmp),
            )
            result = self.run_agent(engine)
            self.assertEqual(result.state, "completed")
            self.assertEqual(result.tool_calls[0]["name"], "unknown_tool")
            self.assertEqual(result.tool_calls[0]["status"], "error")
            self.assertIn("unsupported NL tool call", result.tool_calls[0]["error"])
            self.assertEqual(result.evidence, [])

    def test_missing_text_retrieve_pattern_returns_tool_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine, _ = self.make_engine(
                [tool_call_response(make_tool_call("text_retrieve", max_results=5)), "参数缺失后保持中性。"],
                Path(tmp),
            )
            result = self.run_agent(engine)
            self.assertEqual(result.state, "completed")
            self.assertEqual(result.tool_calls[0]["status"], "error")
            self.assertIn("non-empty pattern", result.tool_calls[0]["error"])
            self.assertEqual(result.evidence, [])

    def test_non_string_text_retrieve_pattern_returns_tool_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine, _ = self.make_engine(
                [tool_call_response(make_tool_call("text_retrieve", pattern=123)), "参数类型错误后保持中性。"],
                Path(tmp),
            )
            result = self.run_agent(engine)
            self.assertEqual(result.state, "completed")
            self.assertEqual(result.tool_calls[0]["status"], "error")
            self.assertIn("pattern must be a string", result.tool_calls[0]["error"])
            self.assertEqual(result.evidence, [])

    def test_grep_pattern_retrieval_and_body_grep(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine, _ = self.make_engine([], Path(tmp))
            retriever = engine.retriever
            hits = retriever.search("公告|处罚", ts_code="000001.SZ", max_results=5)
            self.assertEqual(len(hits), 1)
            self.assertEqual(hits[0]["text_id"], "t1")
            hits = retriever.search("正文", ts_code="000001.SZ", max_results=5)
            self.assertEqual(len(hits), 1)

    def test_pattern_outside_re2_contract_is_rejected(self):
        # Invalid syntax, backreferences and oversize patterns raise a fixable
        # ValueError (the tool surfaces it as status=error); a valid pattern that
        # would be catastrophic under backtracking runs fine on RE2.
        with tempfile.TemporaryDirectory() as tmp:
            engine, _ = self.make_engine([], Path(tmp))
            retriever = engine.retriever
            with self.assertRaisesRegex(ValueError, "RE2"):
                retriever.search("([bad", ts_code="000001.SZ")
            with self.assertRaisesRegex(ValueError, "RE2"):
                retriever.search(r"(公告)\1", ts_code="000001.SZ")
            with self.assertRaisesRegex(ValueError, "too long"):
                retriever.search("a" * 300, ts_code="000001.SZ")
            self.assertEqual(retriever.search("(a+)+$", ts_code="000001.SZ"), [])

    def test_decision_deadline_clamps_provider_timeout_and_disables_retries(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine, proxy = self.make_engine(["结论中性。"], Path(tmp))
            config = NLSubAgentConfig(per_call_timeout_seconds=300, deadline_at=time.monotonic() + 5.0)
            result = engine.run(ts_code="000001.SZ", prompt="p", request_kwargs={}, config=config)
            self.assertEqual(result.state, "completed")
            call = proxy.calls[0]
            self.assertLessEqual(call["timeout_seconds"], 5.0)
            self.assertEqual(call["max_retries"], 0)  # no retry can fit near the deadline

    def test_exhausted_decision_deadline_fails_before_any_provider_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine, proxy = self.make_engine(["unused"], Path(tmp))
            config = NLSubAgentConfig(
                per_call_timeout_seconds=300,
                failure_policy="return_error_with_audit",
                deadline_at=time.monotonic() - 1.0,
            )
            result = engine.run(ts_code="000001.SZ", prompt="p", request_kwargs={}, config=config)
            self.assertEqual(result.state, "timeout")
            self.assertIn("deadline", result.error)
            self.assertEqual(proxy.calls, [])

    def test_unsupported_regex_returns_tool_error_to_subagent(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine, _ = self.make_engine(
                [tool_call_response(make_tool_call("text_retrieve", pattern=r"(公告)\1")), "模式修正后保持中性。"],
                Path(tmp),
            )
            result = self.run_agent(engine)
            self.assertEqual(result.state, "completed")
            self.assertEqual(result.tool_calls[0]["status"], "error")
            self.assertIn("RE2", result.tool_calls[0]["error"])
            self.assertEqual(result.evidence, [])

    def test_stock_search_stays_inside_candidate_linked_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            index = pd.DataFrame(
                [
                    {
                        "text_id": "own",
                        "dataset": "anns_d",
                        "ts_codes": "",
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
            hits = retriever.search(
                "监管问询", ts_code="000001.SZ", company_terms=["平安银行"], max_results=2
            )
            self.assertEqual([hit["text_id"] for hit in hits], ["own"])
            self.assertEqual([hit["relevance"] for hit in hits], ["candidate"])

    def test_candidate_body_subset_is_loaded_once_across_patterns(self):
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            index = pd.DataFrame(
                [
                    {"text_id": "a", "dataset": "anns_d", "ts_codes": "000001.SZ",
                     "title": "first note", "available_at": "2021-10-02T18:00:00+08:00"},
                    {"text_id": "b", "dataset": "anns_d", "ts_codes": "000001.SZ",
                     "title": "second note", "available_at": "2021-10-02T18:00:00+08:00"},
                ]
            )
            index_path = tmp / "text_index.parquet"
            index.to_parquet(index_path, index=False)
            library = tmp / "text_library"
            library.mkdir()
            pd.DataFrame({"text_id": ["a", "b"], "body": ["alpha risk", "beta risk"]}).to_parquet(
                library / "anns_d.parquet", index=False
            )
            retriever = TextRetriever(index_path, library)

            with mock.patch.object(retriever, "_body_query", wraps=retriever._body_query) as query:
                self.assertEqual([h["text_id"] for h in retriever.search(
                    "alpha", ts_code="000001.SZ", max_results=5
                )], ["a"])
                self.assertEqual([h["text_id"] for h in retriever.search(
                    "beta", ts_code="000001.SZ", max_results=5
                )], ["b"])

            self.assertEqual(query.call_count, 1)

    def test_stock_body_search_excludes_unrelated_background_bodies(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            index = pd.DataFrame(
                [
                    {"text_id": "own", "dataset": "anns_d", "ts_codes": "000001.SZ",
                     "title": "company note", "available_at": "2021-10-02T18:00:00+08:00"},
                    {"text_id": "other", "dataset": "anns_d", "ts_codes": "000002.SZ",
                     "title": "other note", "available_at": "2021-10-02T18:00:00+08:00"},
                ]
            )
            index_path = tmp / "text_index.parquet"
            index.to_parquet(index_path, index=False)
            library = tmp / "text_library"
            library.mkdir()
            pd.DataFrame(
                {"text_id": ["own", "other"], "body": ["rare body signal", "rare body signal"]}
            ).to_parquet(library / "anns_d.parquet", index=False)
            retriever = TextRetriever(index_path, library)

            stock_hits = retriever.search("rare body", ts_code="000001.SZ", max_results=5)
            general_hits = retriever.search("rare body", max_results=5)

            self.assertEqual([hit["text_id"] for hit in stock_hits], ["own"])
            self.assertEqual({hit["text_id"] for hit in general_hits}, {"own", "other"})

    def test_general_body_search_excludes_pit_invisible_replay_rows(self):
        # A general (unscoped) body search must never surface a replay body whose
        # dataset has not been released by as_of: _visible_index() drops the
        # not-yet-visible row, and search() keeps only body matches that are in
        # that visible index, while the frozen row (always visible) still returns.
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            frozen = pd.DataFrame(
                [{"text_id": "fz", "dataset": "news", "ts_codes": "000001.SZ",
                  "title": "frozen note", "available_at": "2025-03-01T18:00:00+08:00"}]
            )
            replay = pd.DataFrame(
                [{"text_id": "rp", "dataset": "news", "ts_codes": "000002.SZ",
                  "title": "future note", "available_at": "2099-01-01T18:00:00+08:00"}]
            )
            fz_idx = tmp / "text_index.parquet"; frozen.to_parquet(fz_idx, index=False)
            fz_lib = tmp / "text_library"; fz_lib.mkdir()
            pd.DataFrame({"text_id": ["fz"], "body": ["shared rare signal"]}).to_parquet(
                fz_lib / "news.parquet", index=False
            )
            rp_idx = tmp / "replay_index.parquet"; replay.to_parquet(rp_idx, index=False)
            rp_lib = tmp / "replay_library"; rp_lib.mkdir()
            pd.DataFrame({"text_id": ["rp"], "body": ["shared rare signal"]}).to_parquet(
                rp_lib / "news.parquet", index=False
            )
            retriever = TextRetriever(
                fz_idx, fz_lib, replay_index_path=rp_idx, replay_library_dir=rp_lib,
                as_of=pd.Timestamp("2025-05-01T09:30:00+08:00").to_pydatetime(),
            )
            hits = retriever.search("shared rare signal", max_results=5)
            self.assertEqual({hit["text_id"] for hit in hits}, {"fz"})

    def test_legacy_keyword_requests_map_to_patterns(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine, _ = self.make_engine(
                [
                    tool_call_response(make_tool_call("text_retrieve", keywords=["公告", "处罚"])),
                    "done",
                ],
                Path(tmp),
            )
            result = self.run_agent(engine)
            self.assertEqual(result.tool_calls[0]["arguments"]["pattern"], "公告|处罚")


class NLBudgetTest(unittest.TestCase):
    def _make_snapshot(self, tmp: Path) -> Path:
        snap = tmp / "snap"
        snap.mkdir(parents=True)
        pd.DataFrame(
            columns=["text_id", "dataset", "ts_codes", "title", "available_at", "source_hash", "library_file"]
        ).to_parquet(snap / "text_index.parquet", index=False)
        pd.DataFrame({"ts_code": pd.Series(dtype="string")}).to_parquet(
            snap / "universe.parquet", index=False
        )
        (snap / "text_library").mkdir()
        return snap

    def test_nl_call_budget_returns_budget_exhausted_past_cap(self):
        from autotrade.environment.nl.service import StrategyNLService

        with tempfile.TemporaryDirectory() as tmp:
            snap = self._make_snapshot(Path(tmp))
            service = StrategyNLService(
                proxy=None,
                snapshot_dir=snap,
                log_dir=Path(tmp) / "log",
                failure_policy="return_error_with_audit",
                per_call_timeout_seconds=1.0,
                max_calls=1,
            )
            first = service.run("000001.SZ", prompt="x", kwargs={}, request={"request_id": "1"})
            second = service.run("000001.SZ", prompt="x", kwargs={}, request={"request_id": "2"})
            self.assertEqual(service.calls, 2)
            self.assertNotEqual(first["state"], "budget_exhausted")
            self.assertEqual(second["state"], "budget_exhausted")
            self.assertEqual(second["status"], "error")
            # Failed calls carry explanatory feedback (cause + degrade path),
            # not just a bare error string.
            self.assertIn("配额已用完", second["feedback"])
            self.assertIn("退化路径", first["feedback"])  # proxy=None -> failed_with_policy guidance

    def test_probe_withheld_calls_still_obey_budget(self):
        from autotrade.environment.nl.service import StrategyNLService

        with tempfile.TemporaryDirectory() as tmp:
            service = StrategyNLService(
                proxy=None,
                snapshot_dir=Path(tmp) / "unused_snapshot",
                log_dir=Path(tmp) / "log",
                failure_policy="return_error_with_audit",
                per_call_timeout_seconds=1.0,
                max_calls=1,
                withhold_response=True,
            )
            first = service.run("000001.SZ", prompt="x", kwargs={}, request={"request_id": "1"})
            second = service.run("000001.SZ", prompt="x", kwargs={}, request={"request_id": "2"})

            self.assertEqual(first["state"], "withheld_probe")
            self.assertEqual(second["state"], "budget_exhausted")

    def test_probe_reports_structural_provider_bounds_without_execution(self):
        from autotrade.environment.nl.service import StrategyNLService

        with tempfile.TemporaryDirectory() as tmp:
            service = StrategyNLService(
                proxy=None,
                snapshot_dir=Path(tmp) / "unused_snapshot",
                log_dir=Path(tmp) / "log",
                failure_policy="return_error_with_audit",
                per_call_timeout_seconds=1.0,
                withhold_response=True,
            )
            service.run(
                "000001.SZ",
                prompt="x",
                kwargs={"response_format": {"type": "enum", "values": ["PASS", "REJECT"]}},
                request={"request_id": "1"},
            )
            self.assertEqual(service.cost_summary()["max_provider_calls_per_logical_call"], 2)
            service.run("000001.SZ", prompt="x", kwargs={}, request={"request_id": "2"})
            summary = service.cost_summary()
            self.assertEqual(summary["max_provider_calls_per_logical_call"], 4)
            self.assertEqual((summary["provider_calls"], summary["retrieval_calls"]), (0, 0))

    def test_explicit_nl_contract_validation_is_strict(self):
        from autotrade.environment.nl.service import _parse_event_filter, _parse_response_format

        event = _parse_event_filter(
            {"patterns": ["处罚|重大诉讼"], "lookback_days": 30},
            ts_code="000001.SZ",
        )
        self.assertEqual(event.patterns, ("处罚|重大诉讼",))
        self.assertEqual(event.lookback_days, 30)
        response = _parse_response_format(
            {"type": "enum", "values": ["PASS", "DOWNGRADE", "REJECT"]}
        )
        self.assertEqual(response.choices, ("PASS", "DOWNGRADE", "REJECT"))

        invalid_events = [
            ({"patterns": ["处罚"], "lookback_days": 30}, ""),
            ({"patterns": ["处罚"], "lookback_days": True}, "000001.SZ"),
            ({"patterns": ["处罚"], "lookback_days": 0}, "000001.SZ"),
            ({"patterns": [r"(处罚)\1"], "lookback_days": 30}, "000001.SZ"),
            ({"patterns": ["x" * 250, "y"], "lookback_days": 30}, "000001.SZ"),
        ]
        for raw, code in invalid_events:
            with self.subTest(raw=raw, code=code), self.assertRaises(ValueError):
                _parse_event_filter(raw, ts_code=code)
        with self.assertRaisesRegex(ValueError, "unique"):
            _parse_response_format({"type": "enum", "values": ["PASS", "pass"]})

    def test_invalid_contract_is_audited_without_provider_call(self):
        from autotrade.environment.nl.service import StrategyNLService

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proxy = ScriptedLLM(["unused"])
            service = StrategyNLService(
                proxy=proxy,
                snapshot_dir=self._make_snapshot(root),
                log_dir=root / "log",
                failure_policy="return_error_with_audit",
                per_call_timeout_seconds=30.0,
            )
            result = service.run(
                "000001.SZ",
                prompt="risk",
                kwargs={"response_format": {"type": "json", "values": ["PASS"]}},
                request={"request_id": "1"},
            )
            self.assertEqual(result["state"], "invalid_request")
            self.assertEqual(result["status"], "error")
            self.assertEqual(proxy.calls, [])

    def test_full_nl_reports_start_and_finish_activity(self):
        from autotrade.environment.nl.service import StrategyNLService

        with tempfile.TemporaryDirectory() as tmp:
            snap = self._make_snapshot(Path(tmp))
            activity = []
            service = StrategyNLService(
                proxy=ScriptedLLM(
                    [
                        ProviderResponse(
                            content="结论保持中性。",
                            provider="scripted",
                            model="scripted",
                            usage={"input_tokens": 7, "output_tokens": 2},
                        )
                    ]
                ),
                snapshot_dir=snap,
                log_dir=Path(tmp) / "log",
                failure_policy="return_error_with_audit",
                per_call_timeout_seconds=30.0,
                activity_callback=activity.append,
            )

            result = service.run("000001.SZ", prompt="x", kwargs={}, request={"request_id": "1"})

            self.assertEqual(result["status"], "ok")
            self.assertEqual([item["activity_status"] for item in activity], ["running", "finished"])
            self.assertEqual([item["nl_call_index"] for item in activity], [1, 1])
            self.assertGreaterEqual(activity[-1]["activity_elapsed_seconds"], 0.0)
            cost = service.cost_summary()
            self.assertEqual((cost["provider_calls"], cost["retrieval_calls"]), (1, 0))
            self.assertEqual((cost["provider_prompt_tokens"], cost["provider_completion_tokens"]), (7, 2))

    def test_completed_stock_analysis_reuses_until_candidate_evidence_changes(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from autotrade.environment.nl.service import StrategyNLService

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snap, replay = root / "snap", root / "replay"
            (snap / "text_library").mkdir(parents=True)
            (replay / "text_library").mkdir(parents=True)
            pd.DataFrame(
                [{"text_id": "f1", "dataset": "anns_d", "ts_codes": "000001.SZ",
                  "title": "Frozen note", "available_at": "2021-10-01T18:00:00+08:00", "source_hash": "h1"}]
            ).to_parquet(snap / "text_index.parquet", index=False)
            pd.DataFrame({"text_id": ["f1"], "body": ["frozen body"]}).to_parquet(
                snap / "text_library" / "anns_d.parquet", index=False
            )
            pd.DataFrame(
                [{"text_id": "r1", "dataset": "anns_d", "ts_codes": "000001.SZ",
                  "title": "Replay note", "available_at": "2022-01-04T18:00:00+08:00", "source_hash": "h2"}]
            ).to_parquet(replay / "text_index.parquet", index=False)
            pd.DataFrame({"text_id": ["r1"], "body": ["replay body"]}).to_parquet(
                replay / "text_library" / "anns_d.parquet", index=False
            )
            pd.DataFrame(
                {"ts_code": ["000001.SZ"], "name": ["平安银行"], "exchange": ["SZSE"]}
            ).to_parquet(snap / "universe.parquet", index=False)

            proxy = ScriptedLLM(["first", "after-event"])
            activity = []
            service = StrategyNLService(
                proxy=proxy,
                snapshot_dir=snap,
                replay_dir=replay,
                log_dir=root / "log",
                failure_policy="return_error_with_audit",
                per_call_timeout_seconds=30.0,
                activity_callback=activity.append,
            )
            service.current_when = datetime(2022, 1, 4, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
            first = service.run("000001.SZ", prompt="risk", kwargs={}, request={"request_id": "1"})
            service.current_when = datetime(2022, 1, 4, 13, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
            reused = service.run("000001.SZ", prompt="risk", kwargs={}, request={"request_id": "2"})
            service.current_when = datetime(2022, 1, 5, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
            refreshed = service.run("000001.SZ", prompt="risk", kwargs={}, request={"request_id": "3"})

            self.assertEqual([first["content"], reused["content"], refreshed["content"]],
                             ["first", "first", "after-event"])
            self.assertEqual([first["cache"]["status"], reused["cache"]["status"],
                              refreshed["cache"]["status"]], ["miss", "hit", "miss"])
            self.assertEqual((service.calls, service.executed_calls, service.cache_hits, service.cache_misses),
                             (3, 2, 1, 2))
            self.assertEqual(len(proxy.calls), 2)
            self.assertEqual([a["nl_cache_status"] for a in activity if a["activity_status"] == "finished"],
                             ["miss", "hit", "miss"])
            self.assertEqual(len((root / "log" / "nl_requests.jsonl").read_text().splitlines()), 3)
            self.assertEqual(len((root / "log" / "nl_llm_calls.jsonl").read_text().splitlines()), 2)

    def test_failed_analysis_is_not_reused(self):
        from autotrade.environment.nl.service import StrategyNLService

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snap = self._make_snapshot(root)
            proxy = ScriptedLLM([LLMProxyError("temporary", timeout=True), "recovered"])
            service = StrategyNLService(
                proxy=proxy,
                snapshot_dir=snap,
                log_dir=root / "log",
                failure_policy="return_error_with_audit",
                per_call_timeout_seconds=30.0,
            )

            first = service.run("000001.SZ", prompt="risk", kwargs={}, request={"request_id": "1"})
            second = service.run("000001.SZ", prompt="risk", kwargs={}, request={"request_id": "2"})

            self.assertEqual(first["status"], "error")
            self.assertEqual(second["content"], "recovered")
            self.assertEqual((service.executed_calls, service.cache_hits, service.cache_misses), (2, 0, 2))
            self.assertEqual(len(proxy.calls), 2)

    def test_explicit_event_filter_skips_reuses_refreshes_and_expires(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from autotrade.environment.nl.service import StrategyNLService

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snap, replay = root / "snap", root / "replay"
            (snap / "text_library").mkdir(parents=True)
            (replay / "text_library").mkdir(parents=True)
            pd.DataFrame(
                [{"text_id": "f1", "dataset": "anns_d", "ts_codes": "000001.SZ",
                  "title": "Frozen note", "available_at": "2021-10-01T18:00:00+08:00",
                  "source_hash": "h1"}]
            ).to_parquet(snap / "text_index.parquet", index=False)
            pd.DataFrame({"text_id": ["f1"], "body": ["ordinary body"]}).to_parquet(
                snap / "text_library" / "anns_d.parquet", index=False
            )
            pd.DataFrame(
                [
                    {"text_id": "r1", "dataset": "anns_d", "ts_codes": "000001.SZ",
                     "title": "Routine update", "available_at": "2022-01-04T18:00:00+08:00",
                     "source_hash": "h2"},
                    {"text_id": "r2", "dataset": "anns_d", "ts_codes": "000001.SZ",
                     "title": "Risk update", "available_at": "2022-01-05T18:00:00+08:00",
                     "source_hash": "h3"},
                ]
            ).to_parquet(replay / "text_index.parquet", index=False)
            pd.DataFrame(
                {"text_id": ["r1", "r2"], "body": ["routine operations", "重大诉讼"]}
            ).to_parquet(replay / "text_library" / "anns_d.parquet", index=False)
            pd.DataFrame({"ts_code": ["000001.SZ"], "name": ["平安银行"]}).to_parquet(
                snap / "universe.parquet", index=False
            )

            proxy = ScriptedLLM(["**DOWNGRADE** not REJECT"])
            service = StrategyNLService(
                proxy=proxy,
                snapshot_dir=snap,
                replay_dir=replay,
                log_dir=root / "log",
                failure_policy="return_error_with_audit",
                per_call_timeout_seconds=30.0,
            )
            kwargs = {
                "event_filter": {"patterns": ["重大诉讼"], "lookback_days": 30},
                "response_format": {
                    "type": "enum",
                    "values": ["PASS", "DOWNGRADE", "REJECT"],
                },
            }
            service.current_when = datetime(2022, 1, 4, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
            first = service.run("000001.SZ", prompt="risk", kwargs=kwargs, request={"request_id": "1"})
            service.current_when = datetime(2022, 1, 5, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
            unrelated = service.run("000001.SZ", prompt="risk", kwargs=kwargs, request={"request_id": "2"})
            service.current_when = datetime(2022, 1, 6, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
            refreshed = service.run("000001.SZ", prompt="risk", kwargs=kwargs, request={"request_id": "3"})
            service.current_when = datetime(2022, 1, 6, 13, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
            reused = service.run("000001.SZ", prompt="risk", kwargs=kwargs, request={"request_id": "4"})
            service.current_when = datetime(2022, 2, 6, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
            expired = service.run("000001.SZ", prompt="risk", kwargs=kwargs, request={"request_id": "5"})

            self.assertEqual(
                [first["state"], unrelated["state"], refreshed["state"], reused["state"], expired["state"]],
                [
                    "no_matching_evidence",
                    "no_matching_evidence",
                    "completed",
                    "completed",
                    "no_matching_evidence",
                ],
            )
            self.assertEqual(
                [first["content"], unrelated["content"], refreshed["content"], reused["content"], expired["content"]],
                ["", "", "DOWNGRADE", "DOWNGRADE", ""],
            )
            self.assertEqual(
                [item["cache"]["status"] for item in (first, unrelated, refreshed, reused, expired)],
                ["miss", "hit", "miss", "hit", "miss"],
            )
            self.assertEqual((service.executed_calls, service.cache_hits, service.cache_misses), (1, 2, 3))
            self.assertEqual((service.no_evidence_skips, service.provider_calls, service.retrieval_calls), (3, 1, 0))
            self.assertEqual(len(proxy.calls), 1)
            self.assertEqual(proxy.calls[0]["max_tokens"], 512)
            self.assertEqual(proxy.calls[0]["tool_choice"], "none")
            self.assertFalse(proxy.calls[0]["thinking_enabled"])
            self.assertEqual(refreshed["tool_calls"], [])
            initial = json.loads(proxy.calls[0]["messages"][1]["content"])
            self.assertEqual([item["text_id"] for item in initial["prefetched_evidence"]], ["r2"])
            cost = service.cost_summary()
            self.assertEqual((cost["event_filter_calls"], cost["evidence_items"]), (5, 1))
            self.assertEqual(cost["max_provider_calls_per_logical_call"], 1)


class CompanyContextStoreTest(unittest.TestCase):
    """The frozen snapshot is immutable for a backtest, so the company-context
    sources are read once and each ts_code's context is memoized (R17)."""

    def _make_snapshot(self, tmp: Path) -> Path:
        snap = tmp / "snap"
        snap.mkdir(parents=True)
        pd.DataFrame(
            {"ts_code": ["000001.SZ"], "name": ["平安银行"], "exchange": ["SZSE"], "l1_name": ["银行"]}
        ).to_parquet(snap / "universe.parquet", index=False)
        pd.DataFrame(
            {
                "dataset": ["fina_mainbz_vip"],
                "ts_code": ["000001.SZ"],
                "bz_item": ["零售金融"],
                "end_date": ["20211231"],
                "available_at": ["2022-01-04T18:00:00+08:00"],
            }
        ).to_parquet(snap / "fundamentals.parquet", index=False)
        return snap

    def test_sources_read_once_and_contexts_memoized(self):
        from unittest import mock

        from autotrade.environment.nl.context import CompanyContextStore

        with tempfile.TemporaryDirectory() as tmp:
            snap = self._make_snapshot(Path(tmp))
            with mock.patch(
                "autotrade.environment.nl.context.pd.read_parquet", wraps=pd.read_parquet
            ) as spy:
                store = CompanyContextStore(snap)
                self.assertEqual(spy.call_count, 0)  # lazy: nothing read at construction
                first = store.context("000001.SZ")
                again = store.context("000001.SZ")
                other = store.context("999999.SZ")
            # universe.parquet + fundamentals.parquet read exactly once across all calls.
            self.assertEqual(spy.call_count, 2)
            self.assertIs(first, again)  # memoized object, not rebuilt
            self.assertEqual(first["name"], "平安银行")
            self.assertEqual(first["main_business"], ["零售金融"])
            self.assertEqual(other["context"], "insufficient_company_information")


class TextRetrieverRollingTest(unittest.TestCase):
    """ctx.nl() text rolls on the cron refresh nodes: frozen corpus always visible,
    replay-period text only once its dataset's node has completed by as_of."""

    def _retriever(self, tmp: Path) -> TextRetriever:
        snap, replay = tmp / "snap", tmp / "replay"
        (snap / "text_library").mkdir(parents=True)
        (replay / "text_library").mkdir(parents=True)
        cols = ["text_id", "dataset", "ts_codes", "title", "available_at", "source_hash", "library_file"]
        pd.DataFrame(
            [["f1", "anns_d", "000001.SZ", "Frozen announcement", "2021-10-01T18:00:00+08:00", "h", "anns_d.parquet"]],
            columns=cols,
        ).to_parquet(snap / "text_index.parquet", index=False)
        pd.DataFrame({"text_id": ["f1"], "body": ["frozen body"]}).to_parquet(snap / "text_library" / "anns_d.parquet", index=False)
        pd.DataFrame(
            [["r1", "anns_d", "000001.SZ", "Replay announcement", "2022-01-04T18:00:00+08:00", "h", "anns_d.parquet"]],
            columns=cols,
        ).to_parquet(replay / "text_index.parquet", index=False)
        pd.DataFrame({"text_id": ["r1"], "body": ["replay body"]}).to_parquet(replay / "text_library" / "anns_d.parquet", index=False)
        return TextRetriever(
            snap / "text_index.parquet", snap / "text_library",
            replay_index_path=replay / "text_index.parquet", replay_library_dir=replay / "text_library",
        )

    def _ids(self, retriever: TextRetriever) -> set:
        return {hit["text_id"] for hit in retriever.search("announcement", ts_code="000001.SZ", max_results=10)}

    def test_as_of_none_is_frozen_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            retriever = self._retriever(Path(tmp))
            retriever.as_of = None
            self.assertEqual(self._ids(retriever), {"f1"})

    def test_replay_text_hidden_before_its_evening_node(self):
        with tempfile.TemporaryDirectory() as tmp:
            from datetime import datetime
            from zoneinfo import ZoneInfo
            retriever = self._retriever(Path(tmp))
            # 20220104 noon: the announcement (available 18:00) and its evening node
            # (historical conservative boundary ~03:05 next day) have not landed.
            retriever.as_of = datetime(2022, 1, 4, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
            self.assertEqual(self._ids(retriever), {"f1"})

    def test_replay_text_visible_after_evening_node(self):
        with tempfile.TemporaryDirectory() as tmp:
            from datetime import datetime
            from zoneinfo import ZoneInfo
            retriever = self._retriever(Path(tmp))
            retriever.as_of = datetime(2022, 1, 5, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
            self.assertEqual(self._ids(retriever), {"f1", "r1"})

    def test_candidate_body_cache_loads_only_pit_visible_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            from datetime import datetime
            from zoneinfo import ZoneInfo

            retriever = self._retriever(Path(tmp))
            retriever.as_of = datetime(2022, 1, 4, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
            retriever.search("body", ts_code="000001.SZ", max_results=10)
            corpus = next(iter(retriever._candidate_cache.values()))
            self.assertEqual(corpus.loaded_body_ids, {("anns_d", "f1")})

            retriever.as_of = datetime(2022, 1, 5, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
            retriever.search("body", ts_code="000001.SZ", max_results=10)
            self.assertEqual(corpus.loaded_body_ids, {("anns_d", "f1"), ("anns_d", "r1")})

    def test_visible_index_is_reused_within_one_simulated_tick(self):
        with tempfile.TemporaryDirectory() as tmp:
            from datetime import datetime
            from zoneinfo import ZoneInfo
            retriever = self._retriever(Path(tmp))
            retriever.as_of = datetime(2022, 1, 5, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
            first = retriever._visible_index()
            self.assertIs(retriever._visible_index(), first)

            retriever.as_of = datetime(2022, 1, 6, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
            self.assertIsNot(retriever._visible_index(), first)

    def test_candidate_revision_changes_only_when_linked_evidence_becomes_visible(self):
        with tempfile.TemporaryDirectory() as tmp:
            from datetime import datetime
            from zoneinfo import ZoneInfo
            retriever = self._retriever(Path(tmp))
            retriever.as_of = datetime(2022, 1, 4, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
            before = retriever.candidate_revision("000001.SZ")
            retriever.as_of = datetime(2022, 1, 4, 13, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
            self.assertEqual(retriever.candidate_revision("000001.SZ"), before)
            retriever.as_of = datetime(2022, 1, 5, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
            self.assertNotEqual(retriever.candidate_revision("000001.SZ"), before)

    def test_candidate_revision_can_follow_substantive_event_patterns(self):
        with tempfile.TemporaryDirectory() as tmp:
            from datetime import datetime
            from zoneinfo import ZoneInfo
            retriever = self._retriever(Path(tmp))
            retriever.as_of = datetime(2022, 1, 4, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
            all_before = retriever.candidate_revision("000001.SZ")
            risk_before = retriever.candidate_revision("000001.SZ", patterns=("replay body",))
            other_before = retriever.candidate_revision("000001.SZ", patterns=("处罚",))

            retriever.as_of = datetime(2022, 1, 5, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

            self.assertNotEqual(retriever.candidate_revision("000001.SZ"), all_before)
            self.assertNotEqual(
                retriever.candidate_revision("000001.SZ", patterns=("replay body",)),
                risk_before,
            )
            self.assertEqual(
                retriever.candidate_revision("000001.SZ", patterns=("处罚",)),
                other_before,
            )

    def test_candidate_scope_uses_rolling_pit_window_and_expires(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        with tempfile.TemporaryDirectory() as tmp:
            retriever = self._retriever(Path(tmp))
            retriever.as_of = datetime(2022, 1, 5, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
            active = retriever.candidate_evidence_state(
                "000001.SZ", patterns=("body",), lookback_days=30, max_results=5
            )
            self.assertEqual(active.match_count, 1)
            self.assertEqual([item["text_id"] for item in active.evidence], ["r1"])
            self.assertEqual(
                [hit["text_id"] for hit in retriever.search(
                    "body", ts_code="000001.SZ", max_results=10, lookback_days=30
                )],
                ["r1"],
            )

            retriever.as_of = datetime(2022, 2, 5, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
            expired = retriever.candidate_evidence_state(
                "000001.SZ", patterns=("body",), lookback_days=30
            )
            self.assertEqual(expired.match_count, 0)
            self.assertNotEqual(expired.revision, active.revision)
            self.assertEqual(
                retriever.search("body", ts_code="000001.SZ", lookback_days=30),
                [],
            )

    def test_candidate_lookback_rejects_invalid_values_even_for_empty_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            retriever = self._retriever(Path(tmp))
            for value in (True, 1.5, 0):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    retriever.candidate_evidence_state("999999.SZ", lookback_days=value)
            with self.assertRaisesRegex(ValueError, "as_of"):
                retriever.candidate_evidence_state("999999.SZ", lookback_days=30)


if __name__ == "__main__":
    unittest.main()
