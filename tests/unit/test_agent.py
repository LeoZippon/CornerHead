# Consolidated unit tests: test_agent.py


# Source: test_agent_portfolio_metrics.py
import unittest

import pandas as pd

from hl_trader.environment.evaluation import annualized_return, max_drawdown, sharpe_ratio
from hl_trader.agent import FormulaicScoreRule, score_cross_section
from hl_trader.environment.portfolio import equal_weight_targets, normalize_targets


class AgentPortfolioMetricsTest(unittest.TestCase):
    def test_score_cross_section_orders_low_score_first(self):
        frame = pd.DataFrame({
            "ts_code": ["B", "A", "C"],
            "pe_ttm": [20.0, 10.0, 30.0],
            "roe": [0.1, 0.2, 0.05],
        })
        scored = score_cross_section(frame, [
            FormulaicScoreRule("pe_ttm", ascending=True, weight=1.0),
            FormulaicScoreRule("roe", ascending=False, weight=1.0),
        ])
        self.assertEqual(scored.iloc[0]["ts_code"], "A")

    def test_equal_and_normalized_weights(self):
        self.assertEqual(equal_weight_targets(["A", "A", "B"]), {"A": 0.5, "B": 0.5})
        normalized = normalize_targets({"A": 2.0, "B": 1.0})
        self.assertAlmostEqual(sum(normalized.values()), 1.0)
        self.assertGreater(normalized["A"], normalized["B"])

    def test_weight_caps_apply_to_final_target_weights(self):
        capped = normalize_targets({"A": 10.0, "B": 1.0}, max_weight=0.6)
        self.assertAlmostEqual(capped["A"], 0.6)
        self.assertAlmostEqual(capped["B"], 0.4)
        with self.assertRaisesRegex(ValueError, "infeasible"):
            normalize_targets({"A": 1.0, "B": 1.0}, max_weight=0.4)
        with self.assertRaisesRegex(ValueError, "max_names"):
            equal_weight_targets(["A"], max_names=0)

    def test_metrics_are_defined(self):
        equity = pd.Series([1.0, 1.1, 1.05, 1.2])
        self.assertLess(max_drawdown(equity), 0.0)
        self.assertGreater(annualized_return(equity, periods_per_year=4), 0.0)
        self.assertGreater(sharpe_ratio(equity.pct_change(), periods_per_year=4), 0.0)


# Source: test_deepseek_client.py
from io import BytesIO
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from hl_trader.agent.llm import ChatMessage, DeepSeekAPIError, DeepSeekClient, DeepSeekConfig, DeepSeekResponse, load_deepseek_api_key


def test_config(**kwargs):
    values = {"api_key": "secret", "conversation_log_dir": None}
    values.update(kwargs)
    return DeepSeekConfig(**values)


class FakeHTTPResponse:
    def __init__(self, payload):
        self.payload = payload
        self.status = 200

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class DeepSeekClientTest(unittest.TestCase):
    def test_load_api_key_from_env_file_without_printing_secret(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / ".env"
            path.write_text("DEEPSEEK_API_KEY='secret-value'\n", encoding="utf-8")
            self.assertEqual(load_deepseek_api_key(env_file=path), "secret-value")

    def test_payload_uses_json_mode_and_deepseek_defaults(self):
        client = DeepSeekClient(test_config(model="deepseek-v4-flash"))
        payload = client._payload(
            [
                ChatMessage("system", "Return JSON only."),
                ChatMessage("user", "json please"),
            ],
            json_mode=True,
        )
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertEqual(payload["model"], "deepseek-v4-flash")
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertNotIn("api_key", payload)

    def test_config_repr_redacts_api_key(self):
        fake_secret = "sk-" + "testsecret123456"
        config = test_config(api_key=fake_secret, model="deepseek-v4-flash")
        self.assertNotIn(fake_secret, repr(config))

    def test_payload_includes_thinking_reasoning_effort_and_user_id(self):
        client = DeepSeekClient(
            test_config(
                model="deepseek-v4-pro",
                thinking_enabled=True,
                reasoning_effort="xhigh",
                user_id="macroquant_user-1",
            )
        )
        payload = client._payload(
            [
                ChatMessage("system", "Return a json object."),
                ChatMessage("user", "json please"),
            ],
            json_mode=True,
        )
        self.assertEqual(payload["thinking"], {"type": "enabled"})
        self.assertEqual(payload["reasoning_effort"], "xhigh")
        self.assertEqual(payload["user_id"], "macroquant_user-1")

    def test_json_mode_requires_prompt_to_mention_json(self):
        client = DeepSeekClient(test_config())
        with self.assertRaises(ValueError):
            client._payload([ChatMessage("user", "please decide")], json_mode=True)

    def test_chat_json_parses_openai_compatible_response(self):
        payload = {
            "id": "resp",
            "model": "deepseek-v4-flash",
            "choices": [{"message": {"content": "{\"action\":\"hold\"}"}}],
            "usage": {"total_tokens": 12},
        }
        with patch("hl_trader.agent.llm.deepseek.urlopen", return_value=FakeHTTPResponse(payload)):
            response = DeepSeekClient(test_config()).chat_json([
                ChatMessage("system", "Return JSON only."),
                ChatMessage("user", "json please"),
            ])
        self.assertEqual(response.json_content()["action"], "hold")
        self.assertEqual(response.usage["total_tokens"], 12)

    def test_chat_json_rejects_non_object_content_before_returning(self):
        payload = {
            "id": "resp",
            "model": "deepseek-v4-flash",
            "choices": [{"message": {"content": "[1]"}}],
        }
        with patch("hl_trader.agent.llm.deepseek.urlopen", return_value=FakeHTTPResponse(payload)):
            with self.assertRaises(DeepSeekAPIError):
                DeepSeekClient(test_config()).chat_json([
                    ChatMessage("system", "Return JSON only."),
                    ChatMessage("user", "json please"),
                ])

    def test_json_content_rejects_non_object(self):
        with self.assertRaises(ValueError):
            DeepSeekResponse(content="[1]", model="m").json_content()

    def test_http_429_is_retryable(self):
        error = HTTPError("https://api.deepseek.com/chat/completions", 429, "rate limited", {}, None)
        client = DeepSeekClient(test_config(max_retries=0))
        with patch("hl_trader.agent.llm.deepseek.urlopen", side_effect=error):
            with self.assertRaises(DeepSeekAPIError) as ctx:
                client.chat_json([ChatMessage("user", "json please")])
        self.assertEqual(ctx.exception.status_code, 429)
        self.assertTrue(ctx.exception.retryable)

    def test_http_500_and_503_are_retryable(self):
        for status_code in (500, 503):
            with self.subTest(status_code=status_code):
                body = json.dumps({"error": {"message": "temporary"}}).encode("utf-8")
                error = HTTPError("https://api.deepseek.com/chat/completions", status_code, "temporary", {}, BytesIO(body))
                client = DeepSeekClient(test_config(max_retries=0))
                with patch("hl_trader.agent.llm.deepseek.urlopen", side_effect=error):
                    with self.assertRaises(DeepSeekAPIError) as ctx:
                        client.chat_json([ChatMessage("user", "json please")])
                self.assertEqual(ctx.exception.status_code, status_code)
                self.assertTrue(ctx.exception.retryable)
                self.assertIn("temporary", str(ctx.exception))

    def test_http_500_retries_then_succeeds(self):
        error = HTTPError("https://api.deepseek.com/chat/completions", 500, "temporary", {}, BytesIO(b""))
        response = FakeHTTPResponse({
            "id": "resp",
            "model": "deepseek-v4-flash",
            "choices": [{"message": {"content": "{\"action\":\"hold\"}"}}],
        })
        client = DeepSeekClient(test_config(max_retries=1, retry_backoff_seconds=0))
        with patch("hl_trader.agent.llm.deepseek.urlopen", side_effect=[error, response]) as mocked_urlopen:
            result = client.chat_json([ChatMessage("user", "json please")])
        self.assertEqual(result.json_content()["action"], "hold")
        self.assertEqual(mocked_urlopen.call_count, 2)

    def test_http_error_body_redacts_secret_like_values(self):
        fake_secret = "sk-" + "secretvalue123456"
        body = json.dumps({"error": {"message": "bad key " + fake_secret}}).encode("utf-8")
        error = HTTPError("https://api.deepseek.com/chat/completions", 401, "auth", {}, BytesIO(body))
        client = DeepSeekClient(test_config(max_retries=0))
        with patch("hl_trader.agent.llm.deepseek.urlopen", side_effect=error):
            with self.assertRaises(DeepSeekAPIError) as ctx:
                client.chat_json([ChatMessage("user", "json please")])
        self.assertIn("sk-***", str(ctx.exception))
        self.assertNotIn(fake_secret, str(ctx.exception))

    def test_chat_json_writes_conversation_log(self):
        payload = {
            "id": "resp",
            "model": "deepseek-v4-flash",
            "choices": [{"message": {"content": "{\"action\":\"hold\"}"}}],
            "usage": {"total_tokens": 12},
        }
        fake_secret = "sk-" + "testsecret123456"
        with tempfile.TemporaryDirectory() as tmpdir:
            client = DeepSeekClient(test_config(api_key=fake_secret, conversation_log_dir=tmpdir))
            with patch("hl_trader.agent.llm.deepseek.urlopen", return_value=FakeHTTPResponse(payload)):
                client.chat_json([
                    ChatMessage("system", "Return JSON only."),
                    ChatMessage("user", "json please"),
                ])
            files = list(Path(tmpdir).rglob("*.jsonl"))
            self.assertEqual(len(files), 1)
            record = json.loads(files[0].read_text(encoding="utf-8").strip())
        self.assertEqual(record["status"], "ok")
        self.assertEqual(record["provider"], "deepseek")
        self.assertEqual(record["payload"]["messages"][1]["content"], "json please")
        self.assertEqual(record["raw_response"]["choices"][0]["message"]["content"], "{\"action\":\"hold\"}")
        self.assertEqual(record["usage"]["total_tokens"], 12)
        self.assertNotIn(fake_secret, json.dumps(record))

    def test_http_error_writes_conversation_log(self):
        fake_secret = "sk-" + "secretvalue123456"
        body = json.dumps({"error": {"message": "bad key " + fake_secret}}).encode("utf-8")
        error = HTTPError("https://api.deepseek.com/chat/completions", 401, "auth", {}, BytesIO(body))
        with tempfile.TemporaryDirectory() as tmpdir:
            client = DeepSeekClient(test_config(api_key=fake_secret, max_retries=0, conversation_log_dir=tmpdir))
            with patch("hl_trader.agent.llm.deepseek.urlopen", side_effect=error):
                with self.assertRaises(DeepSeekAPIError):
                    client.chat_json([ChatMessage("user", "json please")])
            files = list(Path(tmpdir).rglob("*.jsonl"))
            self.assertEqual(len(files), 1)
            record = json.loads(files[0].read_text(encoding="utf-8").strip())
        self.assertEqual(record["status"], "error")
        self.assertEqual(record["error"]["status_code"], 401)
        self.assertIn("sk-***", record["error"]["message"])
        self.assertNotIn(fake_secret, json.dumps(record))


# Source: test_evidence_events_shadow.py
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import pandas as pd

from hl_trader.agent.shadow import NLShadowDecision, NLShadowRecorder
from hl_trader.agent.evidence import EvidencePackBuilder
from hl_trader.environment.events import CheckpointDetector
from hl_trader.environment.storage.ledger import stable_hash


class EvidenceEventsShadowTest(unittest.TestCase):
    def _feature_frame(self):
        return pd.DataFrame([
            {
                "feature_date": "20200131",
                "source_trade_date": "20200131",
                "tradable_date": "20200203",
                "available_at": "2020-01-31T15:30:00+08:00",
                "ts_code": "A",
                "pe_ttm": 8.0,
                "pb": 0.9,
                "pct_chg": 10.1,
                "amount": 5000.0,
                "amount_ma20": 1000.0,
                "ret_20d": 0.03,
            },
            {
                "feature_date": "20200131",
                "source_trade_date": "20200131",
                "tradable_date": "20200203",
                "available_at": "2020-01-31T15:30:00+08:00",
                "ts_code": "B",
                "pe_ttm": 20.0,
                "pb": 2.0,
                "pct_chg": 1.0,
                "amount": 1000.0,
                "amount_ma20": 1000.0,
                "ret_20d": -0.01,
            },
        ])

    def test_evidence_pack_hash_is_content_stable_and_appendable(self):
        builder = EvidencePackBuilder()
        pack = builder.from_feature_cross_section(
            self._feature_frame(),
            decision_date="20200131",
            tradable_date="20200203",
            ts_codes=["A"],
            feature_columns=["pe_ttm", "pb", "pct_chg", "amount", "amount_ma20", "ret_20d"],
        )
        self.assertEqual(pack.ts_codes, ("A",))
        record = pack.to_record()
        altered_created_at = replace(pack, created_at="2099-01-01T00:00:00Z").to_record()
        self.assertEqual(record["pack_id"], record["pack_hash"])
        self.assertEqual(record["pack_hash"], altered_created_at["pack_hash"])
        self.assertEqual(record["items"][0]["payload"]["units"]["pct_chg"], "percent")
        self.assertEqual(record["items"][0]["payload"]["pit"]["source_trade_date_max"], "20200131")
        self.assertTrue(record["created_at"].endswith("Z"))
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "evidence.jsonl"
            builder.append_jsonl(path, pack)
            records = builder.read_jsonl(path)
        self.assertEqual(records[0]["pack_hash"], record["pack_hash"])

    def test_evidence_jsonl_read_verifies_hashes(self):
        builder = EvidencePackBuilder()
        pack = builder.from_feature_cross_section(
            self._feature_frame(),
            decision_date="20200131",
            tradable_date="20200203",
            ts_codes=["A"],
            feature_columns=["pe_ttm", "pb", "ret_20d"],
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "evidence.jsonl"
            builder.append_jsonl(path, pack)
            record = builder.read_jsonl(path)[0]
            record["items"][0]["payload"]["row_count"] = 2
            path.write_text(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                builder.read_jsonl(path)

    def test_evidence_pack_rejects_future_or_non_cross_section_data(self):
        builder = EvidencePackBuilder()
        future = self._feature_frame().copy()
        future.loc[future["ts_code"] == "A", "source_trade_date"] = "20200204"
        with self.assertRaises(ValueError):
            builder.from_feature_cross_section(
                future,
                decision_date="20200131",
                tradable_date="20200203",
                ts_codes=["A"],
                feature_columns=["pe_ttm", "pb"],
            )

        mismatch = self._feature_frame().copy()
        mismatch.loc[mismatch["ts_code"] == "A", "tradable_date"] = "20200204"
        with self.assertRaises(ValueError):
            builder.from_feature_cross_section(
                mismatch,
                decision_date="20200131",
                tradable_date="20200203",
                ts_codes=["A"],
                feature_columns=["pe_ttm", "pb"],
            )

        duplicated = pd.concat([self._feature_frame(), self._feature_frame().iloc[[0]]], ignore_index=True)
        with self.assertRaises(ValueError):
            builder.from_feature_cross_section(
                duplicated,
                decision_date="20200131",
                tradable_date="20200203",
                ts_codes=["A"],
                feature_columns=["pe_ttm", "pb"],
            )

    def test_checkpoint_detector_flags_only_available_feature_events_with_units(self):
        features = pd.DataFrame([
            {
                "feature_date": "20200131",
                "tradable_date": "20200203",
                "ts_code": "A",
                "pct_chg": 10.1,
                "amount": 5000.0,
                "amount_ma20": 1000.0,
                "limit": "U",
            },
            {
                "feature_date": "20200131",
                "tradable_date": "20200203",
                "ts_code": "B",
                "pct_chg": 1.0,
                "amount": 1000.0,
                "amount_ma20": 1000.0,
                "limit": "",
            },
        ])
        checkpoints = CheckpointDetector().detect(features)
        event_types = {item.event_type for item in checkpoints}
        self.assertEqual(event_types, {"large_price_move", "large_amount_spike", "price_limit_status"})
        price_event = next(item for item in checkpoints if item.event_type == "large_price_move")
        self.assertEqual(price_event.payload["pct_chg_unit"], "percent")
        self.assertAlmostEqual(price_event.payload["pct_chg"], 10.1)
        amount_event = next(item for item in checkpoints if item.event_type == "large_amount_spike")
        self.assertEqual(amount_event.payload["amount_unit"], "thousand_cny")
        self.assertEqual(amount_event.payload["amount_to_ma20"], 5.0)
        self.assertTrue(all(item.feature_date == "20200131" for item in checkpoints))
        self.assertTrue(all(item.tradable_date == "20200203" for item in checkpoints))

    def test_checkpoint_detector_rejects_duplicate_feature_rows(self):
        features = pd.DataFrame([
            {"feature_date": "20200131", "tradable_date": "20200203", "ts_code": "A", "pct_chg": 10.0},
            {"feature_date": "20200131", "tradable_date": "20200203", "ts_code": "A", "pct_chg": -10.0},
        ])
        with self.assertRaises(ValueError):
            CheckpointDetector().detect(features)

    def test_nl_shadow_recorder_rejects_trading_impact(self):
        with self.assertRaises(ValueError):
            NLShadowDecision(
                decision_id="d1",
                decision_date="20200131",
                tradable_date="20200203",
                ts_code="A",
                prompt_hash="p",
                response_hash="r",
                rationale="test",
                nl_weight=0.1,
            )
        with self.assertRaises(ValueError):
            NLShadowDecision(
                decision_id="d1b",
                decision_date="20200131",
                tradable_date="20200203",
                ts_code="A",
                prompt_hash="p",
                response_hash="r",
                rationale="test",
                action_impact="order_signal",
            )
        with self.assertRaises(ValueError):
            NLShadowDecision(
                decision_id="d1c",
                decision_date="20200131",
                tradable_date="20200203",
                ts_code="A",
                prompt_hash="p",
                response_hash="r",
                rationale="test",
                action="buy",
            )
        decision = NLShadowDecision(
            decision_id="d2",
            decision_date="20200131",
            tradable_date="20200203",
            ts_code="A",
            prompt_hash="p",
            response_hash="r",
            rationale="shadow note",
            action="enter",
            confidence=0.4,
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            recorder = NLShadowRecorder(Path(tmpdir) / "shadow.jsonl")
            recorder.append(decision, evidence_pack_id="pack")
            records = recorder.read_all()
        self.assertEqual(records[0]["event_type"], "nl_shadow_decision")
        self.assertFalse(records[0]["can_affect_trading"])
        self.assertFalse(records[0]["decision"]["can_affect_trading"])
        self.assertEqual(records[0]["decision"]["action_impact"], "shadow_only")
        self.assertEqual(records[0]["decision"]["nl_weight"], 0.0)
        self.assertTrue(records[0]["recorded_at"].endswith("Z"))
        self.assertIn("record_hash", records[0])
        self.assertIn("decision_hash", records[0]["decision"])

    def test_stable_hash_sorts_sets_before_hashing(self):
        self.assertEqual(stable_hash({"codes": {"B", "A"}}), stable_hash({"codes": {"A", "B"}}))


# Source: test_llm_shadow_advisor.py
import json
import unittest
from dataclasses import dataclass

import pandas as pd

from hl_trader.agent.shadow import LLMShadowAdvisor, NLShadowRecorder
from hl_trader.agent.evidence import EvidencePackBuilder


@dataclass(frozen=True)
class FakeResponse:
    content: str
    model: str = "test-model"
    usage: dict | None = None
    response_id: str = "resp"

    def json_content(self):
        return json.loads(self.content)


class FakeClient:
    def __init__(self, payload, *, model="test-model", usage=None, response_id="resp"):
        self.payload = payload
        self.model = model
        self.usage = usage or {"total_tokens": 100}
        self.response_id = response_id
        self.messages = None
        self.calls = 0

    def chat_json(self, messages, *, max_tokens=None):
        self.messages = messages
        self.calls += 1
        return FakeResponse(
            content=self.payload,
            model=self.model,
            usage=self.usage,
            response_id=self.response_id,
        )


def evidence_record():
    frame = pd.DataFrame([
        {
            "feature_date": "20200131",
            "source_trade_date": "20200131",
            "tradable_date": "20200203",
            "available_at": "2020-01-31T18:00:00+08:00",
            "ts_code": "000001.SZ",
            "pe_ttm": 8.0,
            "pb": 0.8,
            "pct_chg": 2.0,
            "amount": 100000.0,
            "amount_ma20": 90000.0,
        },
        {
            "feature_date": "20200131",
            "source_trade_date": "20200131",
            "tradable_date": "20200203",
            "available_at": "2020-01-31T18:00:00+08:00",
            "ts_code": "000002.SZ",
            "pe_ttm": 20.0,
            "pb": 2.2,
            "pct_chg": -1.0,
            "amount": 80000.0,
            "amount_ma20": 70000.0,
        },
    ])
    pack = EvidencePackBuilder().from_feature_cross_section(
        frame,
        decision_date="20200131",
        tradable_date="20200203",
        ts_codes=["000001.SZ", "000002.SZ"],
        feature_columns=["pe_ttm", "pb", "pct_chg", "amount", "amount_ma20"],
    )
    return pack.to_record()


class LLMShadowAdvisorTest(unittest.TestCase):
    def test_advisor_builds_shadow_decisions_for_all_pack_codes(self):
        client = FakeClient(
            '{"pack_summary":"ok","decisions":['
            '{"ts_code":"000001.SZ","action":"hold","confidence":0.7,"rationale":"cheap","risk_flags":["none"]},'
            '{"ts_code":"000002.SZ","action":"exit","confidence":0.4,"rationale":"expensive","risk_flags":["valuation"]}'
            '],"model_notes":"shadow"}'
        )
        result = LLMShadowAdvisor(client, provider_name="test-provider").advise(evidence_record(), checkpoints=[{"event_type": "large_price_move"}])
        self.assertEqual(len(result.decisions), 2)
        self.assertTrue(all(decision.nl_weight == 0.0 for decision in result.decisions))
        self.assertTrue(all(decision.action_impact == "shadow_only" for decision in result.decisions))
        self.assertEqual(result.provider_metadata["provider"], "test-provider")
        self.assertEqual(result.provider_metadata["usage"]["total_tokens"], 100)
        self.assertIn("JSON", client.messages[0].content)
        self.assertIn("shadow-only", client.messages[0].content)
        self.assertIn("exactly one decision", client.messages[0].content)
        self.assertIn("event_checkpoints", client.messages[1].content)
        self.assertIn("JSON", client.messages[1].content)
        self.assertIn("cannot_affect_trading", client.messages[1].content)

    def test_unknown_or_missing_codes_fail_fast(self):
        bad_client = FakeClient(
            '{"decisions":['
            '{"ts_code":"000001.SZ","action":"hold","confidence":0.5,"rationale":"x"},'
            '{"ts_code":"999999.SZ","action":"hold","confidence":0.5,"rationale":"x"}'
            ']}'
        )
        with self.assertRaises(ValueError):
            LLMShadowAdvisor(bad_client).advise(evidence_record())

        missing_client = FakeClient('{"decisions":[{"ts_code":"000001.SZ","action":"hold","confidence":0.5,"rationale":"x"}]}')
        with self.assertRaises(ValueError):
            LLMShadowAdvisor(missing_client).advise(evidence_record())

    def test_duplicate_code_fails_fast(self):
        duplicate_client = FakeClient(
            '{"decisions":['
            '{"ts_code":"000001.SZ","action":"hold","confidence":0.7,"rationale":"x"},'
            '{"ts_code":"000001.SZ","action":"exit","confidence":0.4,"rationale":"y"}'
            ']}'
        )
        with self.assertRaises(ValueError):
            LLMShadowAdvisor(duplicate_client).advise(evidence_record())

    def test_disallowed_action_becomes_human_review(self):
        client = FakeClient(
            '{"decisions":['
            '{"ts_code":"000001.SZ","action":"all_in","confidence":0.7,"rationale":"bad action"},'
            '{"ts_code":"000002.SZ","action":"hold","confidence":0.4,"rationale":"ok"}'
            ']}'
        )
        result = LLMShadowAdvisor(client).advise(evidence_record())
        by_code = {decision.ts_code: decision for decision in result.decisions}
        self.assertEqual(by_code["000001.SZ"].action, "human_review")
        self.assertFalse(by_code["000001.SZ"].to_record()["can_affect_trading"])

    def test_tampered_evidence_pack_hash_fails_before_api_call(self):
        record = evidence_record()
        record["items"][0]["payload"]["row_count"] = 999
        client = FakeClient('{"decisions":[]}')
        with self.assertRaises(ValueError):
            LLMShadowAdvisor(client).advise(record)
        self.assertEqual(client.calls, 0)

    def test_provider_metadata_is_sanitized(self):
        secret = "sk-" + "unitsecret123"
        client = FakeClient(
            '{"decisions":['
            '{"ts_code":"000001.SZ","action":"hold","confidence":0.7,"rationale":"ok"},'
            '{"ts_code":"000002.SZ","action":"hold","confidence":0.4,"rationale":"ok"}'
            ']}',
            response_id="resp-" + secret,
            usage={"total_tokens": 100, "cache_key": secret},
        )
        result = LLMShadowAdvisor(client, provider_name="test-provider").advise(evidence_record())
        serialized = json.dumps(result.provider_metadata, ensure_ascii=False, sort_keys=True)
        self.assertNotIn(secret, serialized)
        self.assertEqual(result.provider_metadata["usage"]["total_tokens"], 100)

    def test_recorder_can_store_provider_metadata(self):
        import tempfile
        from pathlib import Path

        secret = "sk-" + "unitsecret456"
        client = FakeClient(
            '{"decisions":['
            '{"ts_code":"000001.SZ","action":"hold","confidence":0.7,"rationale":"ok"},'
            '{"ts_code":"000002.SZ","action":"hold","confidence":0.4,"rationale":"ok"}'
            ']}'
        )
        result = LLMShadowAdvisor(client, provider_name="test-provider").advise(evidence_record())
        with tempfile.TemporaryDirectory() as tmpdir:
            recorder = NLShadowRecorder(Path(tmpdir) / "shadow.jsonl")
            metadata = dict(result.provider_metadata)
            metadata["api_key"] = secret
            recorder.append(result.decisions[0], evidence_pack_id="pack", provider_metadata=metadata)
            records = recorder.read_all()
        self.assertFalse(records[0]["can_affect_trading"])
        self.assertEqual(records[0]["provider_metadata"]["model"], "test-model")
        self.assertNotIn(secret, json.dumps(records[0]["provider_metadata"], ensure_ascii=False, sort_keys=True))
        self.assertEqual(records[0]["provider_metadata"]["api_key"], "[REDACTED]")
