import json
import os
import signal
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autotrade.environment.managed_proxy import (
    META_XRAY_CONFIG_B64_ENV,
    META_XRAY_CONFIG_JSON_ENV,
    META_XRAY_CONFIG_PATH_ENV,
    ManagedProxySpec,
    _prepare_xray_config,
)


class FakeProcess:
    def __init__(self) -> None:
        self.terminated = False
        self.killed = False

    def poll(self):
        return None

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout=None):
        return 0

    def kill(self) -> None:
        self.killed = True


class ManagedProxyTest(unittest.TestCase):
    def test_prepared_xray_config_replaces_conflicting_inbound_ports(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as busy:
            busy.bind(("127.0.0.1", 0))
            busy.listen()
            busy_port = int(busy.getsockname()[1])
            prepared = _prepare_xray_config(
                {
                    "inbounds": [
                        {"tag": "user-http", "protocol": "http", "listen": "127.0.0.1", "port": busy_port},
                        {"tag": "user-socks", "protocol": "socks", "listen": "127.0.0.1", "port": busy_port},
                        {"tag": "unused-fixed-port", "protocol": "dokodemo-door", "port": busy_port},
                    ],
                    "outbounds": [{"protocol": "freedom"}],
                }
            )

        inbounds = prepared.config["inbounds"]
        ports = {item["port"] for item in inbounds}
        self.assertEqual(len(inbounds), 2)
        self.assertNotIn(busy_port, ports)
        self.assertEqual(len(ports), 2)
        self.assertEqual([item["tag"] for item in inbounds], ["user-http", "user-socks"])
        self.assertTrue(all(item["listen"] == "127.0.0.1" for item in inbounds))
        self.assertTrue(all(item["settings"]["accounts"] for item in inbounds))
        self.assertEqual(inbounds[1]["settings"]["auth"], "password")

    def test_managed_proxy_start_records_only_redacted_runtime_facts(self):
        fake = FakeProcess()
        config = {"inbounds": [], "outbounds": [{"protocol": "freedom", "secret": "do-not-record"}]}
        env = {
            META_XRAY_CONFIG_PATH_ENV: "",
            META_XRAY_CONFIG_JSON_ENV: json.dumps(config),
            META_XRAY_CONFIG_B64_ENV: "",
        }
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, env, clear=False):
                with patch("autotrade.environment.managed_proxy.shutil.which", return_value="/usr/bin/xray"):
                    with patch("autotrade.environment.managed_proxy.subprocess.Popen", return_value=fake) as popen:
                        with patch("autotrade.environment.managed_proxy._wait_for_tcp"):
                            session = ManagedProxySpec(container_host="10.10.0.1").start(Path(tmp))

            self.assertEqual(session.record["status"], "started")
            self.assertEqual(session.record["source"], "json_env")
            self.assertEqual(session.record["listen_host"], "127.0.0.1")
            self.assertEqual(session.record["container_host"], "10.10.0.1")
            self.assertIn("HTTP_PROXY", session.env)
            self.assertIn("ALL_PROXY", session.env)
            self.assertIn("@10.10.0.1:", session.env["HTTP_PROXY"])
            self.assertEqual((Path(tmp) / "xray.generated.json").stat().st_mode & 0o777, 0o600)
            generated = json.loads((Path(tmp) / "xray.generated.json").read_text(encoding="utf-8"))
            auth_user = generated["inbounds"][0]["settings"]["accounts"][0]["user"]
            auth_pass = generated["inbounds"][1]["settings"]["accounts"][0]["pass"]
            self.assertTrue(auth_user.startswith("u"))
            self.assertTrue(auth_pass.startswith("p"))
            record_text = json.dumps(session.record, ensure_ascii=False)
            self.assertNotIn("do-not-record", record_text)
            self.assertNotIn(auth_user, record_text)
            self.assertNotIn(auth_pass, record_text)
            self.assertNotIn("10.10.0.1", json.dumps(generated["outbounds"], ensure_ascii=False))
            self.assertNotIn("do-not-record", " ".join(map(str, popen.call_args.args[0])))
            session.stop()
            self.assertTrue(fake.terminated)

    def test_managed_proxy_skips_when_no_config_is_present(self):
        env = {META_XRAY_CONFIG_PATH_ENV: "", META_XRAY_CONFIG_JSON_ENV: "", META_XRAY_CONFIG_B64_ENV: ""}
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, env, clear=False):
                session = ManagedProxySpec().start(Path(tmp))
        self.assertEqual(session.record["status"], "not_configured")
        self.assertEqual(session.env, {})

    def test_managed_proxy_reads_default_local_config_file(self):
        env = {META_XRAY_CONFIG_PATH_ENV: "", META_XRAY_CONFIG_JSON_ENV: "", META_XRAY_CONFIG_B64_ENV: ""}
        fake = FakeProcess()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env.xray.json"
            path.write_text(json.dumps({"inbounds": [], "outbounds": [{"protocol": "freedom"}]}), encoding="utf-8")
            path.chmod(0o600)
            with patch.dict(os.environ, env, clear=False):
                with patch("autotrade.environment.managed_proxy.shutil.which", return_value="/usr/bin/xray"):
                    with patch("autotrade.environment.managed_proxy.subprocess.Popen", return_value=fake):
                        with patch("autotrade.environment.managed_proxy._wait_for_tcp"):
                            session = ManagedProxySpec(default_config_path=str(path)).start(Path(tmp) / "runtime")

        self.assertEqual(session.record["status"], "started")
        self.assertEqual(session.record["source"], "default_file")
        session.stop()

    def test_managed_proxy_rejects_group_readable_config_file(self):
        env = {META_XRAY_CONFIG_PATH_ENV: "", META_XRAY_CONFIG_JSON_ENV: "", META_XRAY_CONFIG_B64_ENV: ""}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env.xray.json"
            path.write_text(json.dumps({"inbounds": [], "outbounds": [{"protocol": "freedom"}]}), encoding="utf-8")
            path.chmod(0o640)
            with patch.dict(os.environ, env, clear=False):
                with self.assertRaisesRegex(RuntimeError, "must not be group/world accessible"):
                    ManagedProxySpec(default_config_path=str(path)).start(Path(tmp) / "runtime")

    def test_stop_process_signals_process_group_when_pid_is_available(self):
        fake = FakeProcess()
        fake.pid = 12345
        with patch("autotrade.environment.managed_proxy.os.getpgid", return_value=12345):
            with patch("autotrade.environment.managed_proxy.os.killpg") as killpg:
                from autotrade.environment.managed_proxy import _stop_process

                _stop_process(fake)

        killpg.assert_called_once_with(12345, signal.SIGTERM)


if __name__ == "__main__":
    unittest.main()
