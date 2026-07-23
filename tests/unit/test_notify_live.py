"""Feishu notifier + QMT live monitor/bridge units (no network: transports injected)."""

import datetime
import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from autotrade.live import QmtLiveMonitor, format_deal_card
from autotrade.live.qmt_monitor import CN_TZ, PULL_FAILURE_ALERT_CYCLES
from autotrade.notify import FeishuBot, load_dotenv_values
from autotrade.notify.feishu import decision_alert_card

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_bridge():
    """Import ops/qmt/qmt_client_bridge.py fresh (stdlib-only, no QMT imports
    at module level; get_trade_detail_data/passorder are injected per test)."""
    spec = importlib.util.spec_from_file_location(
        "qmt_client_bridge_under_test", _REPO_ROOT / "ops" / "qmt" / "qmt_client_bridge.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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

    def test_send_card_builds_interactive_payload(self):
        bot = FeishuBot("app", "secret", "oc_chat")
        calls = []

        def fake_call(url, payload, token=None):
            calls.append((url, payload))
            if url.endswith("internal"):
                return {"code": 0, "tenant_access_token": "tok", "expire": 7200}
            return {"code": 0}

        with patch.object(FeishuBot, "_call", side_effect=fake_call):
            self.assertTrue(bot.send_card("标题", "**k** v", color="red",
                                          button_text="打开", button_url="http://x"))
        send = next(p for u, p in calls if "im/v1/messages" in u)
        self.assertEqual(send["msg_type"], "interactive")
        card = json.loads(send["content"])
        self.assertEqual(card["header"]["template"], "red")
        self.assertEqual(card["header"]["title"]["content"], "标题")
        self.assertEqual(card["elements"][0]["text"]["content"], "**k** v")
        self.assertEqual(card["elements"][1]["actions"][0]["url"], "http://x")

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
            env.write_text('# comment\nA=1\nB = spaced \nC="quoted"\nD=\'sq\'\nE="a=b"\n', encoding="utf-8")
            self.assertEqual(load_dotenv_values(env), {
                "A": "1", "B": "spaced", "C": "quoted", "D": "sq", "E": "a=b"})

    def test_load_dotenv_into_environ_shares_quote_semantics(self):
        from autotrade.pipelines.assembly import load_dotenv_into_environ

        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text('TEST_DOTENV_Q="hello"\nTEST_DOTENV_R=raw\n', encoding="utf-8")
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("TEST_DOTENV_Q", None)
                os.environ.pop("TEST_DOTENV_R", None)
                loaded = load_dotenv_into_environ(env, keys=("TEST_DOTENV_Q", "TEST_DOTENV_R", "TEST_DOTENV_NONE"))
                self.assertEqual(set(loaded), {"TEST_DOTENV_Q", "TEST_DOTENV_R"})
                self.assertEqual(os.environ["TEST_DOTENV_Q"], "hello")  # quotes stripped like the shared parser
                self.assertEqual(os.environ["TEST_DOTENV_R"], "raw")


class DecisionAlertCardTest(unittest.TestCase):
    def test_states_map_to_cards(self):
        step = decision_alert_card("exp1", "waiting_step_user", {
            "session_key": "epoch_001/fold_2025Q1", "awaiting_step": 3,
            "step_summary": {"total_return": 0.0123},
            "completed_sessions": 2, "total_sessions": 9,
        })
        self.assertIn("Step 3 待批准", step["title"])
        self.assertEqual(step["color"], "orange")
        self.assertIn("1.23%", step["body"])
        self.assertIn("**实验** exp1", step["body"])
        self.assertIn("**进度** 2/9", step["body"])
        question = decision_alert_card("exp1", "waiting_user_reply", {
            "session_key": "s", "awaiting_question": {"index": 2, "question": "方案A还是B？"},
        })
        self.assertIn("提问 #2", question["title"])
        self.assertIn("方案A还是B", question["body"])
        self.assertIn("等待批准", decision_alert_card("exp1", "waiting_user", {"session_key": "s"})["title"])
        failed = decision_alert_card("exp1", "failed", {"error": "boom"})
        self.assertEqual(failed["color"], "red")
        self.assertIn("boom", failed["body"])
        self.assertIsNone(decision_alert_card("exp1", "running_session", {}))


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

            def notify(title, body, *, color="blue"):
                sent.append((title, body, color))
                return True

            monitor = QmtLiveMonitor(
                local_dir=local, notify=notify, ssh_dest="test@host",
                ssh_known_hosts=local / "known_hosts",
            )
            deals = local / f"deals_{today}.jsonl"
            deals.write_text(json.dumps(self._deal("T1")) + "\n", encoding="utf-8")
            (local / "account_snapshot.json").write_text(json.dumps({
                "ok": True, "asset": {"total_asset": 1_000_000, "cash": 500_000, "market_value": 480_000},
                "position_count": 5,
            }), encoding="utf-8")

            result = monitor.run_once(pull=False)
            self.assertEqual(result["notified"], 1)
            title, body, color = sent[0]
            self.assertIn("实盘成交 · 买入", title)
            self.assertEqual(color, "red")  # A-share: buys red
            self.assertIn("**600000.SH** 买入 **100股 @ 10.5**", body)
            self.assertIn("总资产 1,000,000.00", body)

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
            alerts = [entry for entry in sent if "实盘链路告警" in entry[0]]
            self.assertEqual(len(alerts), 1)
            self.assertEqual(alerts[0][2], "red")

    def test_format_deal_card_without_snapshot(self):
        card = format_deal_card(self._deal("T9"), None)
        self.assertIn("实盘成交", card["title"])
        self.assertNotIn("**账户**", card["body"])

    def test_day_scoped_dedup_pruning_and_failed_notify_not_acked(self):
        today = datetime.datetime.now(CN_TZ).strftime("%Y%m%d")
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp)
            sent = []
            outcome = {"ok": False}

            def notify(title, body, *, color="blue"):
                sent.append(title)
                return outcome["ok"]

            monitor = QmtLiveMonitor(
                local_dir=local, notify=notify, ssh_dest="test@host",
                ssh_known_hosts=local / "known_hosts",
            )
            (local / f"deals_{today}.jsonl").write_text(json.dumps(self._deal("T1")) + "\n", encoding="utf-8")
            # Seed pre-existing state: an old-day key and a legacy bare traded_id.
            (local / ".monitor_state.json").write_text(json.dumps({
                "notified_deals": ["20200101:STOCK:OLD", "T0"], "last_error": ""}), encoding="utf-8")

            result = monitor.run_once(pull=False)
            self.assertEqual(result["notified"], 0)  # delivery failed -> not acked
            state = json.loads((local / ".monitor_state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["notified_deals"], [])  # old + legacy keys pruned, T1 unacked

            outcome["ok"] = True
            result = monitor.run_once(pull=False)  # retried and acked this cycle
            self.assertEqual(result["notified"], 1)
            self.assertEqual(len(sent), 2)  # one failed + one delivered attempt
            state = json.loads((local / ".monitor_state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["notified_deals"], [f"{today}::T1"])  # day-scoped key

            result = monitor.run_once(pull=False)  # acked -> no third attempt
            self.assertEqual(result["notified"], 0)
            self.assertEqual(len(sent), 2)

    def test_stale_snapshot_alert_once_and_recovery_notice(self):
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp)
            sent = []

            def notify(title, body, *, color="blue"):
                sent.append((title, body, color))
                return True

            monitor = QmtLiveMonitor(
                local_dir=local, notify=notify, ssh_dest="test@host",
                ssh_known_hosts=local / "known_hosts",
            )
            stale_at = (datetime.datetime.now(CN_TZ) - datetime.timedelta(seconds=600)) \
                .replace(tzinfo=None).isoformat()[:19]
            (local / "account_snapshot.json").write_text(
                json.dumps({"ok": True, "generated_at": stale_at}), encoding="utf-8")

            result = monitor.run_once(pull=False)
            self.assertEqual(result["alerts"], ["stale_snapshot"])
            monitor.run_once(pull=False)  # still stale -> no re-alert
            alerts = [entry for entry in sent if "实盘链路告警" in entry[0]]
            self.assertEqual(len(alerts), 1)
            self.assertIn("未更新", alerts[0][1])

            fresh_at = datetime.datetime.now(CN_TZ).replace(tzinfo=None).isoformat()[:19]
            (local / "account_snapshot.json").write_text(
                json.dumps({"ok": True, "generated_at": fresh_at}), encoding="utf-8")
            result = monitor.run_once(pull=False)
            self.assertEqual(result["alerts"], [])
            monitor.run_once(pull=False)
            recoveries = [entry for entry in sent if "实盘链路恢复" in entry[0]]
            self.assertEqual(len(recoveries), 1)  # one-shot recovery card
            self.assertEqual(recoveries[0][2], "green")

    def test_scp_timeout_never_kills_cycle_and_pull_failures_alert_then_recover(self):
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp)
            sent = []

            def notify(title, body, *, color="blue"):
                sent.append((title, body, color))
                return True

            monitor = QmtLiveMonitor(
                local_dir=local, notify=notify, ssh_dest="test@host",
                ssh_known_hosts=local / "known_hosts",
            )
            with patch("autotrade.live.qmt_monitor.subprocess.run",
                       side_effect=subprocess.TimeoutExpired(cmd="scp", timeout=1)):
                results = [monitor.run_once() for _ in range(PULL_FAILURE_ALERT_CYCLES)]
            self.assertEqual(results[0]["alerts"], [])  # below threshold
            self.assertEqual(results[-1]["alerts"], ["pull_failed"])
            alerts = [entry for entry in sent if "实盘链路告警" in entry[0]]
            self.assertEqual(len(alerts), 1)  # alerted once at the threshold

            fresh_at = datetime.datetime.now(CN_TZ).replace(tzinfo=None).isoformat()[:19]

            def fake_run(cmd, **kwargs):
                Path(cmd[-1]).write_text(
                    json.dumps({"ok": True, "generated_at": fresh_at}), encoding="utf-8")
                return SimpleNamespace(returncode=0)

            with patch("autotrade.live.qmt_monitor.subprocess.run", side_effect=fake_run):
                result = monitor.run_once()
            self.assertEqual(result["alerts"], [])
            recoveries = [entry for entry in sent if "实盘链路恢复" in entry[0]]
            self.assertEqual(len(recoveries), 1)

    def test_pull_honors_return_code_and_keeps_previous_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp)
            known_hosts = local / "known_hosts"
            known_hosts.write_text("host ssh-ed25519 AAAA\n", encoding="utf-8")
            monitor = QmtLiveMonitor(
                local_dir=local, notify=None, ssh_dest="test@host", ssh_known_hosts=known_hosts
            )
            snapshot = local / "account_snapshot.json"
            snapshot.write_text('{"ok": true}', encoding="utf-8")

            with patch("autotrade.live.qmt_monitor.subprocess.run",
                       return_value=SimpleNamespace(returncode=1)):
                failed = monitor._pull(["account_snapshot.json"])
            self.assertEqual(failed, ["account_snapshot.json"])
            self.assertEqual(json.loads(snapshot.read_text(encoding="utf-8")), {"ok": True})

            def corrupt_run(cmd, **kwargs):
                Path(cmd[-1]).write_text("{truncated", encoding="utf-8")
                return SimpleNamespace(returncode=0)

            with patch("autotrade.live.qmt_monitor.subprocess.run", side_effect=corrupt_run):
                failed = monitor._pull(["account_snapshot.json"])
            self.assertEqual(failed, ["account_snapshot.json"])  # corrupt payload rejected
            self.assertEqual(json.loads(snapshot.read_text(encoding="utf-8")), {"ok": True})
            self.assertFalse((local / "account_snapshot.json.pull").exists())

            def good_run(cmd, **kwargs):
                Path(cmd[-1]).write_text('{"ok": false, "error": "x"}', encoding="utf-8")
                return SimpleNamespace(returncode=0)

            with patch("autotrade.live.qmt_monitor.subprocess.run", side_effect=good_run):
                failed = monitor._pull(["account_snapshot.json"])
            self.assertEqual(failed, [])
            self.assertEqual(json.loads(snapshot.read_text(encoding="utf-8"))["error"], "x")


class QmtClientBridgeTest(unittest.TestCase):
    """Direct function tests against ops/qmt/qmt_client_bridge.py (Py3.6/stdlib)."""

    @staticmethod
    def _execution_config():
        return {
            "accounts": [{"account_id": "A1", "account_type": "STOCK"}],
            "execution": {
                "enabled": True, "allowed_strategy_ids": ["s1"],
                "max_order_notional": 50000.0, "max_payload_notional": 100000.0,
                "op_type_buy": 23, "op_type_sell": 24, "order_type": 1101,
                "pr_type_limit": 11, "quick_trade": 1,
                "trading_windows": [["00:00", "23:59"]],
            },
        }

    def test_strict_json_rejects_nan_and_gates_require_real_booleans(self):
        bridge = _load_bridge()
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.json"
            bad.write_text('{"price": NaN}', encoding="utf-8")
            with self.assertRaises(ValueError):
                bridge._read_json_strict(str(bad))

            config_path = Path(tmp) / "qmt_bridge.json"
            config_path.write_text(json.dumps({
                "accounts": [{"account_id": "A1", "account_type": "STOCK"}],
                "execution": {"enabled": "false"},
            }), encoding="utf-8")
            bridge.CONFIG_PATH = str(config_path)
            with self.assertRaisesRegex(ValueError, "enabled must be a JSON boolean"):
                bridge._load_config()
            # 1e999 is standard JSON number syntax parsing to inf (parse_constant
            # never fires), so the finite-cap check must reject it itself.
            config_path.write_text(
                '{"accounts": [{"account_id": "A1", "account_type": "STOCK"}],'
                ' "execution": {"enabled": false, "max_order_notional": 1e999}}',
                encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "max_order_notional"):
                bridge._load_config()

        config = self._execution_config()

        def errors(**overrides):
            payload = {
                "schema_version": 2, "payload_id": "p", "strategy_id": "s1",
                "trade_date": bridge._today(), "execute": False, "confirm": "",
                "orders": [{"code": "600000.SH", "side": "BUY", "volume": 100, "price": 10.0}],
            }
            payload.update(overrides)
            return bridge._validate_payload(payload, config)

        self.assertEqual(errors(), [])
        self.assertTrue(any("execute" in e for e in errors(execute="false")))
        self.assertTrue(any("execute" in e for e in errors(execute=1)))
        self.assertTrue(any("confirm" in e for e in errors(confirm=True)))
        inf_order = [{"code": "600000.SH", "side": "BUY", "volume": 100, "price": 1e999}]
        self.assertTrue(any("finite" in e for e in errors(orders=inf_order)))
        float_volume = [{"code": "600000.SH", "side": "BUY", "volume": 100.0, "price": 10.0}]
        self.assertTrue(any("volume" in e for e in errors(orders=float_volume)))
        bool_volume = [{"code": "600000.SH", "side": "BUY", "volume": True, "price": 10.0}]
        self.assertTrue(any("volume" in e for e in errors(orders=bool_volume)))

    def test_payload_rejects_colliding_order_remarks(self):
        # The broker-side idempotency wall keys on the remark; a payload whose
        # orders share one remark identity must be rejected before submission.
        bridge = _load_bridge()
        config = self._execution_config()

        def payload(orders):
            return {
                "schema_version": 2, "payload_id": "p", "strategy_id": "s1",
                "trade_date": bridge._today(), "execute": False, "confirm": "",
                "orders": orders,
            }

        explicit = [
            {"code": "600000.SH", "side": "BUY", "volume": 100, "price": 10.0, "remark": "dup"},
            {"code": "600016.SH", "side": "BUY", "volume": 100, "price": 10.0, "remark": "dup"},
        ]
        self.assertTrue(any("collides" in e for e in bridge._validate_payload(payload(explicit), config)))

        # An explicit remark equal to another order's positional index shares
        # that order's default identity: orders[2] remark "1" == orders[1] default.
        positional = [
            {"code": "600000.SH", "side": "BUY", "volume": 100, "price": 10.0},
            {"code": "600016.SH", "side": "BUY", "volume": 100, "price": 10.0},
            {"code": "600028.SH", "side": "BUY", "volume": 100, "price": 10.0, "remark": "1"},
        ]
        self.assertTrue(any("collides" in e for e in bridge._validate_payload(payload(positional), config)))

        distinct = [
            {"code": "600000.SH", "side": "BUY", "volume": 100, "price": 10.0},
            {"code": "600016.SH", "side": "SELL", "volume": 100, "price": 10.0, "remark": "exit-leg"},
        ]
        self.assertEqual(bridge._validate_payload(payload(distinct), config), [])

    def test_export_dedup_keys_are_day_scoped_and_state_is_pruned(self):
        bridge = _load_bridge()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bridge.STATE_PATH = str(root / "state" / "bridge_state.json")
            bridge.OUTBOX_DIR = str(root / "outbox")
            bridge.SNAPSHOT_PATH = str(root / "outbox" / "account_snapshot.json")

            class OrderRow:
                m_strOrderSysID = "O1"
                m_strInstrumentID = "600000.SH"
                m_nOrderStatus = 50
                m_nVolumeTraded = 100

            class DealRow:
                m_strTradeID = "D1"
                m_strOrderSysID = "O1"
                m_strInstrumentID = "600000.SH"
                m_nVolume = 100
                m_dPrice = 10.5

            rows = {"ORDER": [OrderRow()], "DEAL": [DealRow()], "ACCOUNT": [], "POSITION": []}
            bridge.get_trade_detail_data = lambda account_id, account_type, data_type: rows[data_type]
            accounts = [{"account_id": "A1", "account_type": "STOCK"}]
            state = {"processed_payloads": {}, "order_fingerprints": {}, "seen_deal_ids": set()}

            today = bridge._today()
            self.assertTrue(bridge._export_cycle(state, accounts))
            self.assertIn(f"{today}:STOCK:D1", state["seen_deal_ids"])
            self.assertIn(f"{today}:STOCK:O1", state["order_fingerprints"])
            self.assertFalse(bridge._export_cycle(state, accounts))  # dedup: nothing new
            deals_file = root / "outbox" / f"deals_{today}.jsonl"
            self.assertEqual(len(deals_file.read_text(encoding="utf-8").splitlines()), 1)

            # Saving prunes entries older than yesterday plus legacy un-prefixed keys.
            state["seen_deal_ids"].update({"20200101:STOCK:OLD", "legacy-bare-id"})
            state["order_fingerprints"]["20200101:STOCK:X"] = "f|1"
            state["order_fingerprints"]["STOCK:legacy"] = "f|2"
            bridge._save_state(state)
            saved = json.loads(Path(bridge.STATE_PATH).read_text(encoding="utf-8"))
            self.assertEqual(saved["seen_deal_ids"], [f"{today}:STOCK:D1"])
            self.assertEqual(list(saved["order_fingerprints"]), [f"{today}:STOCK:O1"])

    def test_order_journal_records_intent_and_terminal_states(self):
        bridge = _load_bridge()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bridge.STATE_PATH = str(root / "state" / "bridge_state.json")
            bridge.get_trade_detail_data = lambda account_id, account_type, data_type: []
            calls = []

            def fake_passorder(*args):
                calls.append(args)
                if len(calls) == 2:
                    raise RuntimeError("counter rejected order")

            bridge.passorder = fake_passorder
            config = self._execution_config()
            today = bridge._today()
            payload = {
                "schema_version": 2, "payload_id": "p1", "strategy_id": "s1",
                "trade_date": today, "execute": True, "confirm": "p1",
                "orders": [
                    {"code": "600000.SH", "side": "BUY", "volume": 100, "price": 10.5},
                    {"code": "000001.SZ", "side": "SELL", "volume": 100, "price": 9.0},
                ],
            }
            path = root / "signal.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            state = {"processed_payloads": {}, "order_fingerprints": {}, "seen_deal_ids": set()}

            result = bridge._process_payload(str(path), config, state, None)
            self.assertFalse(result["ok"])
            self.assertIn("passorder failed at orders[1]", result["error"])
            self.assertTrue(result["orders"][0]["submitted"])
            self.assertFalse(result["orders"][1]["submitted"])
            self.assertEqual(result["submitted_count"], 1)
            self.assertNotIn("p1", state["processed_payloads"])  # retryable via remark wall

            journal = root / "state" / f"order_journal_{today}.jsonl"
            events = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([e["event"] for e in events], ["intent", "submitted", "intent", "error"])
            self.assertEqual(events[1]["remark"], "MQ:p1:0")  # submitted order keeps its terminal state
            self.assertIn("counter rejected order", events[3]["error"])

            # A fully successful payload journals intent+submitted and is marked processed.
            payload2 = dict(payload, payload_id="p2", confirm="p2",
                            orders=[{"code": "600000.SH", "side": "BUY", "volume": 100, "price": 10.5,
                                     "remark": "r1"}])
            path.write_text(json.dumps(payload2), encoding="utf-8")
            result = bridge._process_payload(str(path), config, state, None)
            self.assertTrue(result["ok"])
            self.assertEqual(result["submitted_count"], 1)
            self.assertIn("%s:p2" % bridge._today(), state["processed_payloads"])  # day-scoped key
            events = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([e["event"] for e in events[-2:]], ["intent", "submitted"])
            self.assertEqual(events[-1]["remark"], "MQ:p2:r1")


if __name__ == "__main__":
    unittest.main()
