import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from autotrade.environment.llm.proxy import (
    LLMProxyError,
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

    def test_tool_call_then_freeform_answer(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine, _ = self.make_engine([tool_call("平安银行|公告"), "公告正文未见重大负面事项。"], Path(tmp))
            result = self.run_agent(engine)
            self.assertEqual(result.state, "completed")
            self.assertEqual(result.rounds, 2)
            self.assertEqual(result.tool_calls[0]["arguments"]["pattern"], "平安银行|公告")
            self.assertEqual(result.evidence[0]["text_id"], "t1")
            self.assertIn("重大负面", result.content)

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
        (snap / "text_library").mkdir()
        return snap

    def test_nl_call_budget_returns_budget_exhausted_past_cap(self):
        from autotrade.environment.tools.backtest import _StrategyNLService

        with tempfile.TemporaryDirectory() as tmp:
            snap = self._make_snapshot(Path(tmp))
            service = _StrategyNLService(
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
            # (completes ~02:05 the next day) have not landed.
            retriever.as_of = datetime(2022, 1, 4, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
            self.assertEqual(self._ids(retriever), {"f1"})

    def test_replay_text_visible_after_evening_node(self):
        with tempfile.TemporaryDirectory() as tmp:
            from datetime import datetime
            from zoneinfo import ZoneInfo
            retriever = self._retriever(Path(tmp))
            retriever.as_of = datetime(2022, 1, 5, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
            self.assertEqual(self._ids(retriever), {"f1", "r1"})


if __name__ == "__main__":
    unittest.main()
