# DeepSeek provider client tests (relocated to autotrade.environment.llm.deepseek).
# Source: test_deepseek_client.py
from io import BytesIO
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from autotrade.environment.llm.deepseek import ChatMessage, DeepSeekAPIError, DeepSeekClient, DeepSeekConfig, DeepSeekResponse, load_deepseek_api_key
from autotrade.environment.llm.proxy import DeepSeekProxy


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


class FakeRawHTTPResponse:
    def __init__(self, body):
        self.body = body
        self.status = 200

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return self.body.encode("utf-8")


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
                user_id="autotrade_user-1",
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
        self.assertEqual(payload["user_id"], "autotrade_user-1")

    def test_proxy_defaults_thinking_reasoning_effort_to_max(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / ".env"
            path.write_text("DEEPSEEK_API_KEY='secret-value'\n", encoding="utf-8")
            proxy = DeepSeekProxy.from_env(model="deepseek-v4-pro", env_file=path)
            self.assertTrue(proxy.client.config.thinking_enabled)
            self.assertEqual(proxy.client.config.reasoning_effort, "max")

            compact_proxy = DeepSeekProxy.from_env(
                model="deepseek-v4-flash",
                env_file=path,
                thinking_enabled=False,
            )
            self.assertFalse(compact_proxy.client.config.thinking_enabled)
            self.assertIsNone(compact_proxy.client.config.reasoning_effort)

    def test_tool_call_can_disable_thinking_without_mutating_proxy(self):
        configs = []

        def fake_chat_tools(client, messages, *, tools, tool_choice, max_tokens):
            configs.append(client.config)
            return DeepSeekResponse(content="PASS", model=client.config.model)

        proxy = DeepSeekProxy(DeepSeekClient(test_config(thinking_enabled=True, reasoning_effort="max")))
        with patch.object(DeepSeekClient, "chat_tools", new=fake_chat_tools):
            response = proxy.complete_tools(
                [{"role": "user", "content": "classify"}],
                tools=[],
                tool_choice="none",
                timeout_seconds=10.0,
                max_tokens=512,
                thinking_enabled=False,
            )

        self.assertEqual(response.content, "PASS")
        self.assertFalse(configs[0].thinking_enabled)
        self.assertIsNone(configs[0].reasoning_effort)
        self.assertTrue(proxy.client.config.thinking_enabled)
        self.assertEqual(proxy.client.config.reasoning_effort, "max")

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
        with patch("autotrade.environment.llm.deepseek.urlopen", return_value=FakeHTTPResponse(payload)):
            response = DeepSeekClient(test_config()).chat_json([
                ChatMessage("system", "Return JSON only."),
                ChatMessage("user", "json please"),
            ])
        self.assertEqual(response.json_content()["action"], "hold")
        self.assertEqual(response.usage["total_tokens"], 12)

    def test_chat_tools_stream_merges_tool_call_chunks(self):
        body = "\n\n".join(
            [
                "data: "
                + json.dumps(
                    {
                        "id": "resp-tools",
                        "model": "deepseek-v4-pro",
                        "choices": [
                            {
                                "delta": {
                                    "tool_calls": [
                                        {
                                            "index": 0,
                                            "id": "call_1",
                                            "type": "function",
                                            "function": {"name": "grep", "arguments": "{\"pattern\":"},
                                        }
                                    ]
                                }
                            }
                        ],
                    }
                ),
                "data: "
                + json.dumps(
                    {
                        "choices": [
                            {
                                "delta": {
                                    "tool_calls": [
                                        {"index": 0, "function": {"arguments": "\"alpha\"}"}}
                                    ]
                                }
                            }
                        ]
                    }
                ),
                "data: " + json.dumps({"choices": [{"finish_reason": "tool_calls", "delta": {}}], "usage": {"total_tokens": 9}}),
                "data: [DONE]",
            ]
        )
        client = DeepSeekClient(test_config(model="deepseek-v4-pro"))
        tools = [{"type": "function", "function": {"name": "grep", "parameters": {"type": "object", "properties": {}}}}]
        messages = [
            {"role": "system", "content": "Use tools."},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "call_old", "type": "function", "function": {"name": "grep", "arguments": "{}"}}
                ],
            },
            {"role": "tool", "tool_call_id": "call_old", "content": "{}"},
        ]

        with patch("autotrade.environment.llm.deepseek.urlopen", return_value=FakeRawHTTPResponse(body)) as mocked_urlopen:
            response = client.chat_tools(messages, tools=tools)

        request = mocked_urlopen.call_args[0][0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertTrue(payload["stream"])
        self.assertEqual(payload["messages"][1]["tool_calls"][0]["id"], "call_old")
        self.assertEqual(payload["messages"][2]["tool_call_id"], "call_old")
        self.assertEqual(response.content, "")
        self.assertEqual(response.usage["total_tokens"], 9)
        self.assertEqual(response.tool_calls[0]["function"]["arguments"], '{"pattern":"alpha"}')

    def test_chat_json_rejects_non_object_content_before_returning(self):
        payload = {
            "id": "resp",
            "model": "deepseek-v4-flash",
            "choices": [{"message": {"content": "[1]"}}],
        }
        with patch("autotrade.environment.llm.deepseek.urlopen", return_value=FakeHTTPResponse(payload)):
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
        with patch("autotrade.environment.llm.deepseek.urlopen", side_effect=error):
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
                with patch("autotrade.environment.llm.deepseek.urlopen", side_effect=error):
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
        with patch("autotrade.environment.llm.deepseek.urlopen", side_effect=[error, response]) as mocked_urlopen:
            result = client.chat_json([ChatMessage("user", "json please")])
        self.assertEqual(result.json_content()["action"], "hold")
        self.assertEqual(mocked_urlopen.call_count, 2)

    def test_http_error_body_redacts_secret_like_values(self):
        fake_secret = "sk-" + "secretvalue123456"
        body = json.dumps({"error": {"message": "bad key " + fake_secret}}).encode("utf-8")
        error = HTTPError("https://api.deepseek.com/chat/completions", 401, "auth", {}, BytesIO(body))
        client = DeepSeekClient(test_config(max_retries=0))
        with patch("autotrade.environment.llm.deepseek.urlopen", side_effect=error):
            with self.assertRaises(DeepSeekAPIError) as ctx:
                client.chat_json([ChatMessage("user", "json please")])
        self.assertIn("redacted", str(ctx.exception).lower())
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
            with patch("autotrade.environment.llm.deepseek.urlopen", return_value=FakeHTTPResponse(payload)):
                client.chat_json([
                    ChatMessage("system", "Return JSON only."),
                    ChatMessage("user", "json please"),
                ])
            files = list(Path(tmpdir).rglob("*.jsonl"))
            self.assertEqual(len(files), 1)
            records = [json.loads(line) for line in files[0].read_text(encoding="utf-8").splitlines()]
            record = records[-1]
        self.assertEqual([item["status"] for item in records], ["started", "ok"])
        started = records[0]
        self.assertEqual(record["status"], "ok")
        self.assertEqual(record["provider"], "deepseek")
        # The payload is stored once (the first attempt's started record); the
        # terminal record joins it via call_id + request_hash, not a duplicate.
        self.assertEqual(started["payload"]["messages"][1]["content"], "json please")
        self.assertNotIn("payload", record)
        self.assertTrue(record["call_id"])
        self.assertEqual(record["call_id"], started["call_id"])
        self.assertEqual(record["request_hash"], started["request_hash"])
        self.assertEqual(record["raw_response"]["choices"][0]["message"]["content"], "{\"action\":\"hold\"}")
        self.assertEqual(record["usage"]["total_tokens"], 12)
        self.assertNotIn(fake_secret, json.dumps(records))

    def test_http_error_writes_conversation_log(self):
        fake_secret = "sk-" + "secretvalue123456"
        body = json.dumps(
            {"error": {"message": "bad key " + fake_secret + " Authorization: Bearer plain-bearer-token"}}
        ).encode("utf-8")
        error = HTTPError("https://api.deepseek.com/chat/completions", 401, "auth", {}, BytesIO(body))
        with tempfile.TemporaryDirectory() as tmpdir:
            client = DeepSeekClient(test_config(api_key=fake_secret, max_retries=0, conversation_log_dir=tmpdir))
            with patch("autotrade.environment.llm.deepseek.urlopen", side_effect=error):
                with self.assertRaises(DeepSeekAPIError) as raised:
                    client.chat_json([ChatMessage("user", "json please")])
            files = list(Path(tmpdir).rglob("*.jsonl"))
            self.assertEqual(len(files), 1)
            records = [json.loads(line) for line in files[0].read_text(encoding="utf-8").splitlines()]
            record = records[-1]
        self.assertEqual([item["status"] for item in records], ["started", "error"])
        self.assertEqual(record["status"], "error")
        self.assertEqual(record["error"]["status_code"], 401)
        self.assertIn("redacted", record["error"]["message"].lower())
        self.assertIn("redacted", str(raised.exception).lower())
        self.assertNotIn(fake_secret, json.dumps(records))
        self.assertNotIn("plain-bearer-token", json.dumps(records))

    def test_conversation_log_redacts_sensitive_dict_keys(self):
        payload = {
            "id": "resp-sk-secretvalue123456",
            "model": "deepseek-v4-flash",
            "choices": [{"message": {"content": "{\"action\":\"hold\"}"}}],
            "usage": {"total_tokens": 12, "secret": "usage-secret"},
            "api_key": "plain-secret",
            "authorization": "Bearer plain-token",
            "notes": "provider echoed Authorization: Bearer plain-bearer-token",
            "nested": {"token": "plain-token", "total_tokens": 12},
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            client = DeepSeekClient(test_config(conversation_log_dir=tmpdir))
            with patch("autotrade.environment.llm.deepseek.urlopen", return_value=FakeHTTPResponse(payload)):
                client.chat_json([
                    ChatMessage("system", "Return JSON only."),
                    ChatMessage("user", "json please"),
                ])
            log_path = next(Path(tmpdir).rglob("*.jsonl"))
            records = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
        record = records[-1]
        self.assertEqual(record["raw_response"]["api_key"], "[REDACTED]")
        self.assertEqual(record["raw_response"]["authorization"], "[REDACTED]")
        self.assertEqual(record["raw_response"]["nested"]["token"], "[REDACTED]")
        self.assertEqual(record["raw_response"]["nested"]["total_tokens"], 12)
        self.assertIn("redacted", record["response_id"].lower())
        self.assertEqual(record["usage"]["total_tokens"], 12)
        self.assertNotIn("plain-bearer-token", json.dumps(records))
        self.assertEqual(record["usage"]["secret"], "[REDACTED]")
        self.assertNotIn("plain-secret", json.dumps(records))
        self.assertNotIn("plain-token", json.dumps(records))
        self.assertNotIn("usage-secret", json.dumps(records))

    def test_conversation_log_failure_stops_before_provider_call(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            blocked = Path(tmpdir) / "blocked"
            blocked.write_text("not a directory", encoding="utf-8")
            client = DeepSeekClient(test_config(conversation_log_dir=blocked))
            with patch("autotrade.environment.llm.deepseek.urlopen") as mocked_urlopen:
                with self.assertRaises(DeepSeekAPIError):
                    client.chat_json([
                        ChatMessage("system", "Return JSON only."),
                        ChatMessage("user", "json please"),
                    ])
            mocked_urlopen.assert_not_called()
