"""QMT live-state sync + per-fill Feishu notifications (Linux decision host).

Pulls the in-client bridge's outbox files (see ops/qmt/qmt_client_bridge.py)
over scp from the QMT Windows node into ``data/qmt_live/`` and sends one group
message per NEW fill, with the latest account snapshot attached. The Windows
side stays network-free; credentials stay in this host's .env.

State lives in ``data/qmt_live/.monitor_state.json`` so restarts never
re-notify old fills: day-scoped notified-deal keys ("YYYYMMDD:account_type:
traded_id", pruned past the previous calendar day), the currently latched
link alerts, and the consecutive snapshot pull-failure count.

Link health is alerted per condition (exporter error / stale snapshot /
repeated pull failures) exactly once while it holds, and a one-shot recovery
card is sent when a previously alerted condition clears.
"""

from __future__ import annotations

import datetime
import json
import subprocess
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

CN_TZ = ZoneInfo("Asia/Shanghai")

# Alert when the (ok=true) snapshot stops advancing: the bridge rewrites it
# every ~5s, so minutes of silence mean the client/exporter/link is down.
STALE_SNAPSHOT_ALERT_SECONDS = 180.0
# Alert after this many consecutive failed snapshot pulls (20s cycles).
PULL_FAILURE_ALERT_CYCLES = 3
SNAPSHOT_NAME = "account_snapshot.json"


def _fmt_amount(value: object) -> str:
    try:
        return f"{float(value):,.2f}"
    except (TypeError, ValueError):
        return str(value or "—")


def format_deal_card(deal: dict, snapshot: dict | None) -> dict[str, str]:
    """One fill -> one interactive card: order details + account status.

    A-share color convention: buys red, sells green."""
    record = deal.get("record") if isinstance(deal.get("record"), dict) else deal
    side_raw = str(record.get("order_type", ""))
    # Counter encodings vary by API surface: xtquant order_type 23/24, in-client
    # m_nOffsetFlag '0'/'1' (or 48/49), plus plain BUY/SELL from payload echoes.
    side = {
        "23": "买入", "0": "买入", "48": "买入", "BUY": "买入",
        "24": "卖出", "1": "卖出", "49": "卖出", "SELL": "卖出",
    }.get(side_raw.upper(), side_raw or "成交")
    lines = [
        f"**{record.get('stock_code', '?')}** {side} "
        f"**{record.get('traded_volume', '?')}股 @ {record.get('traded_price', '?')}**",
        f"**金额** {_fmt_amount(record.get('traded_amount'))}"
        f" ｜ **委托号** {record.get('order_id', '?')} ｜ **时间** {record.get('traded_time', '?')}",
    ]
    remark = str(record.get("order_remark") or record.get("strategy_name") or "").strip()
    if remark:
        lines.append(f"**策略标记** {remark}")
    asset = (snapshot or {}).get("asset") if isinstance((snapshot or {}).get("asset"), dict) else {}
    if asset:
        lines.append(
            f"**账户** 总资产 {_fmt_amount(asset.get('total_asset'))}"
            f" ｜ 可用 {_fmt_amount(asset.get('cash'))}"
            f" ｜ 持仓市值 {_fmt_amount(asset.get('market_value'))}"
            f" ｜ 持仓 {(snapshot or {}).get('position_count', '?')} 只"
        )
    return {
        "title": f"💰 实盘成交 · {side}",
        "color": "red" if side == "买入" else "green" if side == "卖出" else "blue",
        "body": "\n".join(lines),
    }


class QmtLiveMonitor:
    """One sync-and-notify cycle; the runner script loops it."""

    def __init__(
        self,
        *,
        local_dir: Path,
        notify,  # callable(title, body, *, color) -> bool (FeishuBot.send_card) or None
        ssh_dest: str,
        remote_outbox: str = "C:/xquant/outbox",
        scp_timeout_seconds: float = 60.0,
    ) -> None:
        self.local_dir = Path(local_dir)
        self.notify = notify
        self.ssh_dest = ssh_dest
        self.remote_outbox = remote_outbox.rstrip("/")
        self.scp_timeout_seconds = scp_timeout_seconds
        self.state_path = self.local_dir / ".monitor_state.json"

    # ---- state --------------------------------------------------------------
    def _load_state(self) -> dict:
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = {}
        alerts = payload.get("alerts")
        state = {
            "notified_deals": set(payload.get("notified_deals", [])),
            "alerts": dict(alerts) if isinstance(alerts, dict) else {},
            "pull_failures": int(payload.get("pull_failures", 0) or 0),
        }
        # Legacy state carried the exporter error as "last_error"; fold it into
        # the alerts dict once so it still dedups (and clears with a recovery).
        legacy_error = str(payload.get("last_error", "") or "")
        if legacy_error and "export_error" not in state["alerts"]:
            state["alerts"]["export_error"] = legacy_error
        return state

    @staticmethod
    def _prune_notified(notified: set[str], min_day: str) -> set[str]:
        """Keep day-scoped keys at or after ``min_day``; legacy bare
        traded_id keys (no YYYYMMDD prefix) are dropped."""
        kept = set()
        for key in notified:
            day = key.split(":", 1)[0]
            if len(day) == 8 and day.isdigit() and day >= min_day:
                kept.add(key)
        return kept

    def _save_state(self, state: dict) -> None:
        min_day = (datetime.datetime.now(CN_TZ) - datetime.timedelta(days=1)).strftime("%Y%m%d")
        state["notified_deals"] = self._prune_notified(state["notified_deals"], min_day)
        self.local_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "notified_deals": sorted(state["notified_deals"]),
            "alerts": state["alerts"],
            "pull_failures": state["pull_failures"],
        }, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.state_path)

    # ---- sync ---------------------------------------------------------------
    def _pull(self, names: list[str]) -> list[str]:
        """scp each file individually (Windows-side globs are unreliable) into
        a temp name, replacing the local copy only on a zero exit status (and,
        for the JSON snapshot, only when the payload parses). A missing remote
        file is normal before the exporter's first write. Returns the names
        that did NOT update this cycle; a hung/failed scp never propagates."""
        self.local_dir.mkdir(parents=True, exist_ok=True)
        failed: list[str] = []
        for name in names:
            tmp = self.local_dir / f"{name}.pull"
            try:
                completed = subprocess.run(
                    ["scp", "-q", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                     f"{self.ssh_dest}:{self.remote_outbox}/{name}", str(tmp)],
                    capture_output=True, timeout=self.scp_timeout_seconds,
                )
            except (subprocess.TimeoutExpired, OSError) as exc:
                print(f"qmt_monitor: pull failed for {name}: {type(exc).__name__}: {exc}",
                      file=sys.stderr)
                tmp.unlink(missing_ok=True)
                failed.append(name)
                continue
            if completed.returncode != 0:
                tmp.unlink(missing_ok=True)
                failed.append(name)
                continue
            if name.endswith(".json"):
                try:
                    json.loads(tmp.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    print(f"qmt_monitor: pulled {name} is not valid JSON; keeping previous copy",
                          file=sys.stderr)
                    tmp.unlink(missing_ok=True)
                    failed.append(name)
                    continue
            tmp.replace(self.local_dir / name)
        return failed

    @staticmethod
    def _read_jsonl(path: Path) -> list[dict]:
        if not path.exists():
            return []
        records = []
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict):
                records.append(record)
        return records

    def _snapshot_age_seconds(self, snapshot: dict | None) -> float | None:
        generated_at = str((snapshot or {}).get("generated_at") or "")
        if not generated_at:
            return None
        try:
            stamp = datetime.datetime.fromisoformat(generated_at)
        except ValueError:
            return None
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=CN_TZ)  # bridge writes naive client-local time
        return (datetime.datetime.now(CN_TZ) - stamp).total_seconds()

    # ---- one cycle ----------------------------------------------------------
    def run_once(self, *, pull: bool = True) -> dict:
        today = datetime.datetime.now(CN_TZ).strftime("%Y%m%d")
        yesterday = (datetime.datetime.now(CN_TZ) - datetime.timedelta(days=1)).strftime("%Y%m%d")
        state = self._load_state()
        if pull:
            failed = self._pull([
                SNAPSHOT_NAME,
                f"orders_{today}.jsonl", f"deals_{today}.jsonl",
                f"deals_{yesterday}.jsonl",  # midnight overlap
            ])
            # Missing JSONL files are normal before the first fill; the always-
            # written snapshot is the link-health probe.
            state["pull_failures"] = state["pull_failures"] + 1 if SNAPSHOT_NAME in failed else 0
        snapshot = None
        snapshot_path = self.local_dir / SNAPSHOT_NAME
        if snapshot_path.exists():
            try:
                snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            except ValueError:
                snapshot = None

        notified = 0
        for day in (yesterday, today):
            for deal in self._read_jsonl(self.local_dir / f"deals_{day}.jsonl"):
                record = deal.get("record") if isinstance(deal.get("record"), dict) else {}
                traded_id = str(record.get("traded_id") or "")
                if not traded_id:
                    continue
                key = f"{day}:{record.get('account_type') or ''}:{traded_id}"
                if key in state["notified_deals"]:
                    continue
                if self.notify is not None:
                    card = format_deal_card(deal, snapshot)
                    if not self.notify(card["title"], card["body"], color=card["color"]):
                        continue  # not acked: the 20s loop retries transient failures
                state["notified_deals"].add(key)
                notified += 1

        # Link health: alert once per active condition, recover once per
        # cleared condition, so a broken live link is never silent and a
        # healthy one never spams.
        error = str((snapshot or {}).get("error") or "") if snapshot and not snapshot.get("ok", True) else ""
        age = self._snapshot_age_seconds(snapshot)
        conditions: dict[str, str] = {}
        if error:
            conditions["export_error"] = f"QMT 实时导出异常：{error}"
        if age is not None and age > STALE_SNAPSHOT_ALERT_SECONDS:
            conditions["stale_snapshot"] = (
                f"QMT 快照已 {int(age)} 秒未更新（generated_at="
                f"{(snapshot or {}).get('generated_at')}），客户端或导出链路可能已停摆"
            )
        if state["pull_failures"] >= PULL_FAILURE_ALERT_CYCLES:
            conditions["pull_failed"] = f"连续 {state['pull_failures']} 轮拉取 QMT outbox 快照失败"

        alerted = state["alerts"]
        cleared = {key: body for key, body in alerted.items() if key not in conditions}
        if cleared:
            body = "已恢复：" + "；".join(sorted(cleared))
            if self.notify is None or self.notify("✅ 实盘链路恢复", body, color="green"):
                for key in cleared:
                    alerted.pop(key, None)
        for key, body in conditions.items():
            if key in alerted:
                # Re-alert only when the exporter error text changes; stale/
                # pull conditions stay latched while they hold.
                if key != "export_error" or alerted[key] == body:
                    alerted[key] = body
                    continue
            if self.notify is None or self.notify("⚠️ 实盘链路告警", body, color="red"):
                alerted[key] = body  # latch only after delivery (or no notifier)

        self._save_state(state)
        return {
            "notified": notified,
            "snapshot_ok": bool(snapshot and snapshot.get("ok", True)),
            "error": error,
            "alerts": sorted(conditions),
        }
