"""Feishu notifier + QMT live monitor units (no network: transports injected)."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autotrade.live import QmtLiveMonitor, format_deal_message
from autotrade.notify import FeishuBot, load_dotenv_values
from autotrade.pipelines.interactive import _decision_alert_text


class FeishuBotTest(unittest.TestCase):
    def test_send_text_uses_cached_tenant_token(self):
        bot = FeishuBot("app", "secret", "oc_chat")
        calls = []

        def fake_call(url, payload, token=None):
            calls.append((url, payload, token))
            if url.endswith("tenant_access_token/internal"):
                return {"code": 0, "tenant_access_token": "tok1", "expire": 7200}
            return {"code": 0}

        with patch.object(FeishuBot, "_call", side_effect=fake_call):
            self.assertTrue(bot.send_text("hello"))
            self.assertTrue(bot.send_text("again"))
        token_calls = [c for c in calls if c[0].endswith("internal")]
        send_calls = [c for c in calls if "im/v1/messages" in c[0]]
        self.assertEqual(len(token_calls), 1)  # cached across sends
        self.assertEqual(len(send_calls), 2)
        self.assertEqual(send_calls[0][1]["receive_id"], "oc_chat")
        self.assertEqual(json.loads(send_calls[0][1]["content"])["text"], "hello")

    def test_send_failure_is_swallowed(self):
        bot = FeishuBot("app", "secret", "oc_chat")
        with patch.object(FeishuBot, "_call", side_effect=OSError("network down")):
            self.assertFalse(bot.send_text("hello"))  # never raises

    def test_from_env_requires_full_triple(self):
        self.assertIsNone(FeishuBot.from_env({"FEISHU_APP_ID": "x"}))
        bot = FeishuBot.from_env(
            {"FEISHU_QMT_APP_ID": "a", "FEISHU_QMT_APP_SECRET": "s", "FEISHU_QMT_CHAT_ID": "c"},
            prefix="FEISHU_QMT",
        )
        self.assertEqual(bot.chat_id, "c")

    def test_load_dotenv_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text("# comment\nA=1\nB = spaced \n", encoding="utf-8")
            self.assertEqual(load_dotenv_values(env), {"A": "1", "B": "spaced"})


class DecisionAlertTextTest(unittest.TestCase):
    def test_states_map_to_messages(self):
        step = _decision_alert_text("exp1", "waiting_step_user", {
            "session_key": "epoch_001/fold_2025Q1", "awaiting_step": 3,
            "step_summary": {"total_return": 0.0123},
        })
        self.assertIn("Step 3 待批准", step)
        self.assertIn("1.23%", step)
        question = _decision_alert_text("exp1", "waiting_user_reply", {
            "session_key": "s", "awaiting_question": {"index": 2, "question": "方案A还是B？"},
        })
        self.assertIn("提问 #2", question)
        self.assertIn("方案A还是B", question)
        self.assertIn("等待批准", _decision_alert_text("exp1", "waiting_user", {"session_key": "s"}))
        self.assertIn("失败", _decision_alert_text("exp1", "failed", {"error": "boom"}))
        self.assertIsNone(_decision_alert_text("exp1", "running_session", {}))


class StatusReporterNotifyTest(unittest.TestCase):
    def test_callback_fires_on_state_transitions_only(self):
        import time as _time

        from autotrade.pipelines.hitl_state import StatusReporter

        with tempfile.TemporaryDirectory() as tmp:
            events = []
            status = StatusReporter(
                Path(tmp) / "status.json", work_root=Path(tmp),
                on_state_change=lambda state, snapshot: events.append((state, snapshot.get("session_key"))),
            )
            status.set(state="running_session", session_key="s1")
            status.set(session_key="s1", awaiting_step=2)      # no state change -> no event
            status.set(state="running_session")                 # same state -> no event
            status.set(state="waiting_step_user", awaiting_step=3)
            deadline = _time.monotonic() + 2.0
            while len(events) < 2 and _time.monotonic() < deadline:
                _time.sleep(0.02)
            self.assertEqual([e[0] for e in events], ["running_session", "waiting_step_user"])


class QmtLiveMonitorTest(unittest.TestCase):
    def _deal(self, traded_id, price=10.5):
        return {"exported_at": "t", "kind": "deal", "record": {
            "traded_id": traded_id, "stock_code": "600000.SH", "order_type": "23",
            "traded_volume": 100, "traded_price": price, "traded_amount": price * 100,
            "traded_time": "093001", "order_id": 42,
        }}

    def test_notifies_new_deals_once_and_tracks_export_errors(self):
        import datetime
        from autotrade.live.qmt_monitor import CN_TZ

        today = datetime.datetime.now(CN_TZ).strftime("%Y%m%d")
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp)
            sent = []
            monitor = QmtLiveMonitor(local_dir=local, notify=lambda text: sent.append(text) or True, ssh_dest="test@host")
            deals = local / f"deals_{today}.jsonl"
            deals.write_text(json.dumps(self._deal("T1")) + "\n", encoding="utf-8")
            (local / "account_snapshot.json").write_text(json.dumps({
                "ok": True, "asset": {"total_asset": 1_000_000, "cash": 500_000, "market_value": 480_000},
                "position_count": 5,
            }), encoding="utf-8")

            result = monitor.run_once(pull=False)
            self.assertEqual(result["notified"], 1)
            self.assertIn("600000.SH 买入 100股 @ 10.5", sent[0])
            self.assertIn("总资产 1,000,000.00", sent[0])

            # Same deal again -> no re-notification; a new one notifies.
            with deals.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(self._deal("T2", 10.6)) + "\n")
            result = monitor.run_once(pull=False)
            self.assertEqual(result["notified"], 1)
            self.assertEqual(len(sent), 2)

            # Exporter error surfaces once per distinct error.
            (local / "account_snapshot.json").write_text(
                json.dumps({"ok": False, "error": "MiniQMT connect failed"}), encoding="utf-8")
            monitor.run_once(pull=False)
            monitor.run_once(pull=False)
            alerts = [t for t in sent if "实盘链路告警" in t]
            self.assertEqual(len(alerts), 1)

    def test_format_deal_message_without_snapshot(self):
        text = format_deal_message(self._deal("T9"), None)
        self.assertIn("【实盘成交】", text)
        self.assertNotIn("账户：", text)


if __name__ == "__main__":
    unittest.main()
