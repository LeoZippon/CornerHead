"""Realtime MiniQMT order/deal/account exporter (Windows side, read-only).

Runs OUTSIDE the QMT client as a persistent process under C:\\xquant\\Python38,
next to the user's qmt_executor.py and reusing its conventions (CQ_* env vars,
C:\\xquant layout, xtquant session). It never calls order/cancel APIs.

Each cycle it publishes to C:\\xquant\\outbox:
  - account_snapshot.json            atomic full snapshot (asset/positions/counts)
  - orders_YYYYMMDD.jsonl            one line per NEW order or order-status change
  - deals_YYYYMMDD.jsonl             one line per NEW fill (traded_id dedup)

The Linux decision host pulls these over scp and sends Feishu notifications;
this process deliberately opens no network channel of its own. Seen-state is
persisted (C:\\xquant\\state\\realtime_export_seen.json) so restarts do not
re-emit old records; delete that file to force a full re-export.

Run (QMT client + MiniQMT must be logged in):
  C:\\xquant\\Python38\\python.exe C:\\xquant\\qmt_realtime_export.py
Register at logon (optional):
  schtasks /Create /TN xquant_realtime_export /SC ONLOGON ^
    /TR "C:\\xquant\\Python38\\python.exe C:\\xquant\\qmt_realtime_export.py"
"""

from __future__ import annotations

import datetime
import json
import os
import sys
import time
from pathlib import Path

QMT_DATA_PATH = os.environ.get("CQ_QMT_DATA_PATH", r"C:\国金证券QMT交易端\userdata_mini")
XQUANT_ROOT = Path(os.environ.get("CQ_XQUANT_ROOT", r"C:\xquant"))
EXPECTED_ACCOUNT_ID = os.environ.get("CQ_EXPECTED_ACCOUNT_ID", "").strip()
OUTBOX_DIR = XQUANT_ROOT / "outbox"
SEEN_PATH = XQUANT_ROOT / "state" / "realtime_export_seen.json"
SNAPSHOT_PATH = OUTBOX_DIR / "account_snapshot.json"
POLL_SECONDS = float(os.environ.get("CQ_EXPORT_POLL_SECONDS", "10"))
RECONNECT_SECONDS = 30.0

SENSITIVE_KEYS = {"account_id", "accountid", "m_straccountid", "secu_account", "m_strsecuaccount"}


def now_text() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def mask_value(value) -> str:
    text = str(value or "")
    if len(text) <= 4:
        return "*" * len(text)
    return text[:2] + "*" * max(0, len(text) - 4) + text[-2:]


def obj_to_dict(obj) -> dict:
    """xtquant objects expose plain attributes; keep JSON-simple values and
    mask account identifiers (they never need to leave the trading box)."""
    if obj is None:
        return {}
    result = {}
    for name in dir(obj):
        if name.startswith("_"):
            continue
        try:
            value = getattr(obj, name)
        except Exception:
            continue
        if callable(value):
            continue
        if not isinstance(value, (bool, int, float, str)) and value is not None:
            value = str(value)
        if name.lower() in SENSITIVE_KEYS:
            value = mask_value(value)
        result[name] = value
    return result


def atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def load_seen() -> dict:
    try:
        with SEEN_PATH.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        return {"orders": dict(payload.get("orders", {})), "deals": set(payload.get("deals", []))}
    except Exception:
        return {"orders": {}, "deals": set()}


def save_seen(seen: dict) -> None:
    atomic_write_json(SEEN_PATH, {"orders": seen["orders"], "deals": sorted(seen["deals"])})


class ExportSession:
    """qmt_executor.py's QmtSession, reduced to read-only queries."""

    def __init__(self) -> None:
        from xtquant.xttrader import XtQuantTrader
        from xtquant.xttype import StockAccount

        self.trader = XtQuantTrader(QMT_DATA_PATH, int(time.time() * 1000) % 100000000)
        self.trader.start()
        if self.trader.connect() != 0:
            raise RuntimeError("MiniQMT connect failed (is the QMT client logged in?)")
        infos = [obj_to_dict(x) for x in (self.trader.query_account_infos() or [])]
        if not infos:
            raise RuntimeError("MiniQMT returned no account info")
        if EXPECTED_ACCOUNT_ID:
            chosen = [i for i in infos if EXPECTED_ACCOUNT_ID in str(i)]
            if not chosen:
                raise RuntimeError("expected MiniQMT account not found; check CQ_EXPECTED_ACCOUNT_ID")
        elif len(infos) > 1:
            raise RuntimeError("multiple MiniQMT accounts found; set CQ_EXPECTED_ACCOUNT_ID")
        account_id = EXPECTED_ACCOUNT_ID or self._account_id(infos[0])
        if not account_id:
            raise RuntimeError("account id missing from MiniQMT account info")
        self.account = StockAccount(str(account_id), "STOCK")
        if self.trader.subscribe(self.account) not in (0, None):
            raise RuntimeError("MiniQMT subscribe failed")

    @staticmethod
    def _account_id(info: dict) -> str:
        for key in ("account_id", "accountid", "m_strAccountID"):
            for name, value in info.items():
                if name.lower() == key.lower() and value:
                    # info values are masked; re-query raw is unnecessary because
                    # EXPECTED_ACCOUNT_ID is the supported multi-account path.
                    return str(value).replace("*", "") if "*" not in str(value) else ""
        return ""

    def close(self) -> None:
        try:
            self.trader.stop()
        except Exception:
            pass

    def poll(self) -> dict:
        asset = obj_to_dict(self.trader.query_stock_asset(self.account))
        positions = [obj_to_dict(p) for p in (self.trader.query_stock_positions(self.account) or [])]
        orders = [obj_to_dict(o) for o in (self.trader.query_stock_orders(self.account) or [])]
        trades = [obj_to_dict(t) for t in (self.trader.query_stock_trades(self.account) or [])]
        return {"asset": asset, "positions": positions, "orders": orders, "trades": trades}


def export_cycle(session: ExportSession, seen: dict) -> tuple[int, int]:
    data = session.poll()
    day = datetime.date.today().strftime("%Y%m%d")
    new_orders = new_deals = 0
    for order in data["orders"]:
        order_id = str(order.get("order_id") or order.get("order_sysid") or "")
        if not order_id:
            continue
        # Re-emit on lifecycle progress: status or filled volume changed.
        fingerprint = f"{order.get('order_status')}|{order.get('traded_volume')}"
        if seen["orders"].get(order_id) == fingerprint:
            continue
        seen["orders"][order_id] = fingerprint
        append_jsonl(OUTBOX_DIR / f"orders_{day}.jsonl",
                     {"exported_at": now_text(), "kind": "order", "record": order})
        new_orders += 1
    for trade in data["trades"]:
        traded_id = str(trade.get("traded_id") or "")
        if not traded_id or traded_id in seen["deals"]:
            continue
        seen["deals"].add(traded_id)
        append_jsonl(OUTBOX_DIR / f"deals_{day}.jsonl",
                     {"exported_at": now_text(), "kind": "deal", "record": trade})
        new_deals += 1
    atomic_write_json(SNAPSHOT_PATH, {
        "generated_at": now_text(),
        "ok": True,
        "source": "qmt_realtime_export",
        "asset": data["asset"],
        "position_count": len(data["positions"]),
        "positions": data["positions"],
        "order_count": len(data["orders"]),
        "deal_count": len(data["trades"]),
    })
    if new_orders or new_deals:
        save_seen(seen)
    return new_orders, new_deals


def main() -> int:
    seen = load_seen()
    session = None
    print(f"qmt_realtime_export starting; poll={POLL_SECONDS}s outbox={OUTBOX_DIR}")
    while True:
        try:
            if session is None:
                session = ExportSession()
                print(f"{now_text()} connected to MiniQMT")
            new_orders, new_deals = export_cycle(session, seen)
            if new_orders or new_deals:
                print(f"{now_text()} exported orders+{new_orders} deals+{new_deals}")
        except KeyboardInterrupt:
            break
        except Exception as exc:  # noqa: BLE001 - keep exporting through client restarts
            atomic_write_json(SNAPSHOT_PATH, {
                "generated_at": now_text(), "ok": False,
                "source": "qmt_realtime_export", "error": f"{type(exc).__name__}: {exc}",
            })
            print(f"{now_text()} export error: {exc}; reconnecting in {RECONNECT_SECONDS:.0f}s", file=sys.stderr)
            if session is not None:
                session.close()
                session = None
            time.sleep(RECONNECT_SECONDS)
            continue
        time.sleep(POLL_SECONDS)
    if session is not None:
        session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
