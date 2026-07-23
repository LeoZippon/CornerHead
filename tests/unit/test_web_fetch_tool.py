import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from autotrade.agent import AgentSessionConfig, AgentSessionRunner
from autotrade.environment.llm.proxy import ScriptedLLM, tool_call, tool_call_response
from autotrade.environment.tools.web_fetch import AgentWebFetchTool
from autotrade.environment.web.fetch import WebFetchError, WebFetchResult, WebFetchService
from autotrade.environment.web.fetch import _is_same_host_redirect

from .fixtures_http import FakeHTTPResponse
from .test_tools_flow import build_sandbox


class FakeOpener:
    def __init__(self, response: object) -> None:
        self.response = response

    def open(self, request, timeout: float):  # noqa: ANN001, ARG002
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class WebFetchServiceTest(unittest.TestCase):
    def test_fetch_converts_html_to_markdown(self):
        service = WebFetchService()
        response = FakeHTTPResponse(
            b"<html><body><h1>Title</h1><p>Hello <a href='https://example.com/a'>link</a></p></body></html>"
        )
        with (
            patch("socket.getaddrinfo", return_value=[(0, 0, 0, "", ("93.184.216.34", 443))]),
            patch(
                "autotrade.environment.web.fetch._open_pinned_response",
                return_value=response,
            ) as opened,
        ):
            result = service.fetch("https://example.com")
        self.assertEqual(opened.call_args.args[1], ("93.184.216.34",))
        self.assertEqual(result.status_code, 200)
        self.assertIn("# Title", result.markdown)
        self.assertIn("Hello link (https://example.com/a)", result.markdown)
        self.assertEqual(result.content_type, "text/html")
        self.assertFalse(result.use_proxy)

    def test_fetch_pins_the_validated_address_against_dns_rebinding(self):
        service = WebFetchService()
        response = FakeHTTPResponse(b"ok", content_type="text/plain")
        resolver = [(0, 0, 0, "", ("93.184.216.34", 443))]
        with (
            patch("socket.getaddrinfo", return_value=resolver) as resolve,
            patch(
                "autotrade.environment.web.fetch._open_pinned_response",
                return_value=response,
            ) as opened,
        ):
            service.fetch("https://rebind.example/page")
        resolve.assert_called_once()
        self.assertEqual(opened.call_args.args[1], ("93.184.216.34",))

    def test_action_schema_keeps_documented_proxy_toggle(self):
        from autotrade.environment.tools.web_fetch import build_web_fetch_spec

        self.assertIn("use_proxy", {field.name for field in build_web_fetch_spec().fields})

    def test_fetch_use_proxy_selects_proxy_opener(self):
        service = WebFetchService()
        proxy_opener = FakeOpener(FakeHTTPResponse(b"proxied", content_type="text/plain"))
        with (
            patch(
                "autotrade.environment.web.fetch._build_proxy_opener",
                return_value=proxy_opener,
            ) as build,
            patch("autotrade.environment.web.fetch._open_pinned_response") as pinned,
            patch("socket.getaddrinfo", return_value=[(0, 0, 0, "", ("93.184.216.34", 443))]),
        ):
            result = service.fetch(
                "https://example.com",
                use_proxy=True,
                proxy_env={"HTTPS_PROXY": "http://proxy.test:8080"},
            )
        build.assert_called_once_with({"HTTPS_PROXY": "http://proxy.test:8080"})
        pinned.assert_not_called()
        self.assertEqual(result.markdown, "proxied")
        self.assertTrue(result.use_proxy)

    def test_fetch_use_proxy_requires_active_proxy(self):
        service = WebFetchService()
        with self.assertRaises(WebFetchError) as raised:
            service.fetch("https://example.com", use_proxy=True, proxy_env={})
        self.assertIn("no active proxy", str(raised.exception))

    def test_fetch_rejects_cross_host_redirect(self):
        service = WebFetchService()
        response = FakeHTTPResponse(b"", status=302)
        response.headers["Location"] = "https://other.example/path"
        with (
            patch("socket.getaddrinfo", return_value=[(0, 0, 0, "", ("93.184.216.34", 443))]),
            patch("autotrade.environment.web.fetch._open_pinned_response", return_value=response),
        ):
            with self.assertRaises(WebFetchError) as raised:
                service.fetch("https://example.com")
        self.assertIn("cross-host redirect", str(raised.exception))

    def test_redirect_requires_same_scheme_and_effective_port(self):
        self.assertTrue(_is_same_host_redirect("https://example.com/a", "https://www.example.com:443/b"))
        self.assertTrue(_is_same_host_redirect("http://example.com/a", "http://example.com:80/b"))
        self.assertFalse(_is_same_host_redirect("https://example.com/a", "http://example.com/b"))
        self.assertFalse(_is_same_host_redirect("https://example.com/a", "https://example.com:444/b"))

    def test_fetch_rejects_private_address(self):
        service = WebFetchService()
        with self.assertRaises(WebFetchError):
            service.fetch("http://127.0.0.1/page")


class WebFetchRunnerTest(unittest.TestCase):
    def test_meta_learning_web_fetch_action_is_traced(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            ctx.extra["web_fetch_proxy_env"] = {"HTTPS_PROXY": "http://proxy.test:8080"}
            (ctx.paths.workspace / "taste.md").write_text("fetched taste", encoding="utf-8")
            responses = [
                tool_call_response(tool_call("web_fetch", url="https://example.com/article", max_chars=1000, use_proxy=True)),
                tool_call_response(tool_call("done")),
            ]
            runner = AgentSessionRunner(
                ctx,
                ScriptedLLM(responses),
                AgentSessionConfig(fold_deadline_at=datetime.now(timezone.utc) + timedelta(minutes=5)),
                fold_info={"development_history": "workspace/development_history.json"},
                acceptance_rules={},
                mode="meta_learning",
            )
            result = WebFetchResult(
                url="https://example.com/article",
                final_url="https://example.com/article",
                status_code=200,
                content_type="text/html",
                bytes_read=42,
                content_hash="sha256:" + "0" * 64,
                markdown="Research page\n" * 2000,
                markdown_truncated=False,
                body_truncated=False,
                use_proxy=True,
                redirect_chain=(),
                started_at="2026-01-01T00:00:00+00:00",
                completed_at="2026-01-01T00:00:01+00:00",
                duration_ms=1000,
            )

            with patch("autotrade.environment.tools.web_fetch.WebFetchService.fetch", return_value=result) as fetch:
                summary = runner.run()

            self.assertEqual(summary["finish_status"], "meta_learning_done")
            fetch.assert_called_once_with(
                "https://example.com/article",
                use_proxy=True,
                proxy_env={"HTTPS_PROXY": "http://proxy.test:8080"},
            )
            events = [event for event in ctx.trace.read_events() if event["event_type"] == "web_fetch"]
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["status"], "ok")
            self.assertEqual(events[0]["status_code"], 200)
            self.assertEqual(events[0]["tool_spec"]["result_policy"], "bounded_inline_with_artifact")
            self.assertTrue(events[0]["use_proxy"])
            self.assertNotIn("content", events[0])
            self.assertTrue((ctx.paths.logs / "web_fetch").exists())

    def test_web_fetch_markdown_artifact_is_redacted(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            tool = AgentWebFetchTool(ctx)
            result = WebFetchResult(
                url="https://example.com/secret",
                final_url="https://example.com/secret",
                status_code=200,
                content_type="text/plain",
                bytes_read=20,
                content_hash="sha256:" + "1" * 64,
                markdown="token hf_" + "a" * 30,
                markdown_truncated=False,
                body_truncated=False,
                use_proxy=False,
                redirect_chain=(),
                started_at="2026-01-01T00:00:00+00:00",
                completed_at="2026-01-01T00:00:01+00:00",
                duration_ms=1000,
            )
            with patch("autotrade.environment.tools.web_fetch.WebFetchService.fetch", return_value=result):
                payload = tool.run(url="https://example.com/secret", max_chars=1000)
                runner = AgentSessionRunner(
                    ctx,
                    ScriptedLLM([]),
                    AgentSessionConfig(fold_deadline_at=datetime.now(timezone.utc) + timedelta(minutes=5)),
                    fold_info={},
                    acceptance_rules={},
                    mode="meta_learning",
                )
                observation = runner._do_web_fetch(
                    {"url": "https://example.com/secret", "max_chars": 1000, "use_proxy": False}
                )
            saved = Path(str(payload["host_markdown_path"])).read_text(encoding="utf-8")
            self.assertIn("hf_[redacted]", saved)
            self.assertNotIn("hf_" + "a" * 30, saved)
            self.assertNotIn("host_markdown_path", observation)
            events = [event for event in ctx.trace.read_events() if event["event_type"] == "web_fetch"]
            self.assertTrue(events)
            self.assertTrue(all("host_markdown_path" not in event for event in events))

    def test_fold_tool_schema_does_not_expose_web_fetch(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            runner = AgentSessionRunner(
                ctx,
                ScriptedLLM([]),
                AgentSessionConfig(fold_deadline_at=datetime.now(timezone.utc) + timedelta(minutes=5)),
                fold_info={},
                acceptance_rules={},
                mode="fold",
            )
            names = [item["function"]["name"] for item in runner._tool_schemas()]
            self.assertNotIn("web_fetch", names)
