"""QMT live-state sync + per-fill Feishu notifications (Linux decision host).

Pulls the realtime exporter's outbox files (see ops/qmt/qmt_realtime_export.py)
over scp from the QMT Windows node into ``data/qmt_live/`` and sends one group
message per NEW fill, with the latest account snapshot attached. The Windows
side stays network-free; credentials stay in this host's .env.

State (consumed traded_ids, last error) lives in ``data/qmt_live/.monitor_state.json``
so restarts never re-notify old fills.
"""

from __future__ import annotations

import datetime
import json
import subprocess
from pathlib import Path
from zoneinfo import ZoneInfo

CN_TZ = ZoneInfo("Asia/Shanghai")


def _fmt_amount(value: object) -> str:
    try:
        return f"{float(value):,.2f}"
    except (TypeError, ValueError):
        return str(value or "—")


def format_deal_message(deal: dict, snapshot: dict | None) -> str:
    """One fill -> one group message: order details + account status."""
    record = deal.get("record") if isinstance(deal.get("record"), dict) else deal
    side_raw = str(record.get("order_type", ""))
    side = {"23": "买入", "24": "卖出"}.get(side_raw, side_raw or "成交")
    lines = [
        "【实盘成交】"
        f"{record.get('stock_code', '?')} {side} "
        f"{record.get('traded_volume', '?')}股 @ {record.get('traded_price', '?')}",
        f"金额 {_fmt_amount(record.get('traded_amount'))}"
        f" ｜ 委托号 {record.get('order_id', '?')} ｜ 成交时间 {record.get('traded_time', '?')}",
    ]
    remark = str(record.get("order_remark") or record.get("strategy_name") or "").strip()
    if remark:
        lines.append(f"策略标记 {remark}")
    asset = (snapshot or {}).get("asset") if isinstance((snapshot or {}).get("asset"), dict) else {}
    if asset:
        lines.append(
            f"账户：总资产 {_fmt_amount(asset.get('total_asset'))}"
            f" ｜ 可用 {_fmt_amount(asset.get('cash'))}"
            f" ｜ 持仓市值 {_fmt_amount(asset.get('market_value'))}"
            f" ｜ 持仓 {(snapshot or {}).get('position_count', '?')} 只"
        )
    return "\n".join(lines)


class QmtLiveMonitor:
    """One sync-and-notify cycle; the runner script loops it."""

    def __init__(
        self,
        *,
        local_dir: Path,
        notify,  # callable(str) -> bool (FeishuBot.send_text) or None
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
            return {"notified_deals": set(payload.get("notified_deals", [])),
                    "last_error": str(payload.get("last_error", ""))}
        except (OSError, ValueError):
            return {"notified_deals": set(), "last_error": ""}

    def _save_state(self, state: dict) -> None:
        self.local_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "notified_deals": sorted(state["notified_deals"]),
            "last_error": state["last_error"],
        }, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.state_path)

    # ---- sync ---------------------------------------------------------------
    def _pull(self, names: list[str]) -> None:
        """scp each file individually (Windows-side globs are unreliable);
        a missing remote file is normal before the exporter's first write."""
        self.local_dir.mkdir(parents=True, exist_ok=True)
        for name in names:
            subprocess.run(
                ["scp", "-q", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                 f"{self.ssh_dest}:{self.remote_outbox}/{name}", str(self.local_dir / name)],
                capture_output=True, timeout=self.scp_timeout_seconds,
            )

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

    # ---- one cycle ----------------------------------------------------------
    def run_once(self, *, pull: bool = True) -> dict:
        today = datetime.datetime.now(CN_TZ).strftime("%Y%m%d")
        yesterday = (datetime.datetime.now(CN_TZ) - datetime.timedelta(days=1)).strftime("%Y%m%d")
        if pull:
            self._pull([
                "account_snapshot.json",
                f"orders_{today}.jsonl", f"deals_{today}.jsonl",
                f"deals_{yesterday}.jsonl",  # midnight overlap
            ])
        snapshot = None
        snapshot_path = self.local_dir / "account_snapshot.json"
        if snapshot_path.exists():
            try:
                snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            except ValueError:
                snapshot = None

        state = self._load_state()
        notified = 0
        for day in (yesterday, today):
            for deal in self._read_jsonl(self.local_dir / f"deals_{day}.jsonl"):
                record = deal.get("record") if isinstance(deal.get("record"), dict) else {}
                traded_id = str(record.get("traded_id") or "")
                if not traded_id or traded_id in state["notified_deals"]:
                    continue
                if self.notify is not None:
                    self.notify(format_deal_message(deal, snapshot))
                state["notified_deals"].add(traded_id)
                notified += 1

        # Exporter-side failures (e.g. MiniQMT disconnected) surface once per
        # distinct error, so a broken live link is never silent.
        error = str((snapshot or {}).get("error") or "") if snapshot and not snapshot.get("ok", True) else ""
        if error and error != state["last_error"] and self.notify is not None:
            self.notify(f"【实盘链路告警】QMT 实时导出异常：{error}")
        state["last_error"] = error
        self._save_state(state)
        return {"notified": notified, "snapshot_ok": bool(snapshot and snapshot.get("ok", True)), "error": error}
