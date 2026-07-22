# coding: gbk
"""File bridge for the full QMT in-client Python 3.6 runtime (stdlib only).

One strategy script, imported manually into the QMT client, covering the whole
in-client contract (docs/deployment_documentation.md):

  1. REALTIME EXPORT (always on): every poll writes an atomic account snapshot
     and appends NEW orders / order-status changes / NEW deals as JSONL to
     C:\\xquant\\outbox. The Linux host pulls these over scp (qmt_monitor.sh)
     and sends Feishu fill notifications; this script opens no network channel.
  2. ORDER EXECUTION (config-gated, default OFF): polls C:\\xquant\\inbox for
     signal payloads with fully explicit orders, validates, places them via
     passorder with an idempotency remark, and writes an execute_/error_ result
     to the outbox. Position sizing belongs to the decision host (it has the
     synced account snapshot); this side never invents volumes.

Config: C:\\xquant\\config\\qmt_bridge.json (see qmt_bridge_config.example.json).
State:  C:\\xquant\\state\\bridge_state.json (processed payloads + export dedup).
        Export dedup keys are day-scoped ("YYYYMMDD:account_type:id"); entries
        older than the previous calendar day are pruned on save, so counters
        that reset order/deal ids per day never collide and state stays small.
Journal: C:\\xquant\\state\\order_journal_YYYYMMDD.jsonl gets one "intent"
        record before every passorder call and one "submitted"/"error"
        terminal record after it, so a payload that dies mid-loop leaves a
        persisted terminal state for every order it already submitted.
Inbox payloads must be published atomically (write tmp name, then rename).

Payload schema (schema_version 2, frozen by this implementation):

  {
    "schema_version": 2,
    "payload_id": "unique-id",
    "strategy_id": "must-be-whitelisted-in-config",
    "trade_date": "YYYYMMDD (must equal the client's local date)",
    "execute": false,          # false => dry-run (validate + report only)
    "confirm": "",             # live orders additionally require confirm == payload_id
    "orders": [
      {"code": "600000.SH", "side": "BUY", "volume": 100,
       "price": 10.50, "remark": "optional extra idempotency suffix"}
    ]
  }

Dry-run gates are independent: config execution.enabled AND payload execute
AND confirm must all pass before any passorder call.
"""

from __future__ import print_function

import datetime
import io
import json
import math
import os
import traceback

ROOT_DIR = r"C:\xquant"
CONFIG_PATH = os.path.join(ROOT_DIR, "config", "qmt_bridge.json")
INBOX_DIR = os.path.join(ROOT_DIR, "inbox")
OUTBOX_DIR = os.path.join(ROOT_DIR, "outbox")
ARCHIVE_DIR = os.path.join(ROOT_DIR, "archive")
STATE_PATH = os.path.join(ROOT_DIR, "state", "bridge_state.json")
SNAPSHOT_PATH = os.path.join(OUTBOX_DIR, "account_snapshot.json")

POLL_PERIOD = "5nSecond"
TIMER_START = "2020-01-01 00:00:00"
TIMER_MARKET = "SH"
ACCOUNT_TYPES = ("STOCK", "CREDIT")
DATA_TYPES = ("ACCOUNT", "POSITION", "ORDER", "DEAL")
MAX_PAYLOADS_PER_TICK = 1  # keep each timer callback short (single shared runtime thread)

_STATE = None  # loaded lazily on the first tick


# ---------------------------------------------------------------------------
# small io helpers
# ---------------------------------------------------------------------------
def _now_text():
    return datetime.datetime.now().isoformat()[:19]


def _today():
    return datetime.date.today().strftime("%Y%m%d")


def _ensure_dir(path):
    if not os.path.isdir(path):
        os.makedirs(path)


def _atomic_write_json(path, payload):
    _ensure_dir(os.path.dirname(path))
    tmp_path = path + ".tmp"
    with io.open(tmp_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(payload, ensure_ascii=True, sort_keys=True))
        handle.write(u"\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, path)


def _append_jsonl(path, record):
    _ensure_dir(os.path.dirname(path))
    with io.open(path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, ensure_ascii=True, sort_keys=True))
        handle.write(u"\n")
        handle.flush()
        os.fsync(handle.fileno())


def _reject_json_constant(value):
    raise ValueError("non-standard JSON constant is not allowed: %s" % value)


def _read_json_strict(path):
    """Load config/payload JSON, rejecting NaN/Infinity/-Infinity tokens."""
    with io.open(path, "r", encoding="utf-8-sig") as handle:
        return json.load(handle, parse_constant=_reject_json_constant)


def _keep_state_key(key, min_day):
    """True for day-scoped keys ("YYYYMMDD:...") at or after min_day.

    Legacy un-prefixed keys read False and are dropped on the first save."""
    day = str(key).split(":", 1)[0]
    return len(day) == 8 and day.isdigit() and day >= min_day


def _load_state():
    global _STATE
    if _STATE is not None:
        return _STATE
    try:
        with io.open(STATE_PATH, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        _STATE = {
            "processed_payloads": dict(payload.get("processed_payloads", {})),
            "order_fingerprints": dict(payload.get("order_fingerprints", {})),
            "seen_deal_ids": set(payload.get("seen_deal_ids", [])),
        }
    except Exception:
        _STATE = {"processed_payloads": {}, "order_fingerprints": {}, "seen_deal_ids": set()}
    return _STATE


def _save_state(state):
    min_day = (datetime.date.today() - datetime.timedelta(days=1)).strftime("%Y%m%d")
    state["order_fingerprints"] = dict(
        (key, value) for key, value in state["order_fingerprints"].items()
        if _keep_state_key(key, min_day)
    )
    state["processed_payloads"] = dict(
        (key, value) for key, value in state["processed_payloads"].items()
        if _keep_state_key(key, min_day)
    )
    state["seen_deal_ids"] = set(
        key for key in state["seen_deal_ids"] if _keep_state_key(key, min_day)
    )
    _atomic_write_json(STATE_PATH, {
        "processed_payloads": state["processed_payloads"],
        "order_fingerprints": state["order_fingerprints"],
        "seen_deal_ids": sorted(state["seen_deal_ids"]),
    })


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------
def _finite_positive(value, name):
    number = float(value)
    if not (math.isfinite(number) and number > 0):
        raise ValueError("%s must be a finite positive number" % name)
    return number


def _load_config():
    config = _read_json_strict(CONFIG_PATH)
    accounts = config.get("accounts")
    if not isinstance(accounts, list) or not accounts:
        raise ValueError("config.accounts must be a non-empty list")
    for row in accounts:
        if str(row.get("account_type", "")).upper() not in ACCOUNT_TYPES:
            raise ValueError("account_type must be STOCK or CREDIT")
        if not str(row.get("account_id", "")).strip():
            raise ValueError("account_id must not be empty")
    execution = config.get("execution") or {}
    enabled = execution.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError("execution.enabled must be a JSON boolean")
    config["execution"] = {
        "enabled": enabled,
        "allowed_strategy_ids": list(execution.get("allowed_strategy_ids", [])),
        "max_order_notional": _finite_positive(
            execution.get("max_order_notional", 50000), "execution.max_order_notional"),
        "max_payload_notional": _finite_positive(
            execution.get("max_payload_notional", 100000), "execution.max_payload_notional"),
        # Counter mappings are config, not code: doc open question #4 says the
        # exact opType/prType behavior must be verified on the live counter.
        "op_type_buy": int(execution.get("op_type_buy", 23)),
        "op_type_sell": int(execution.get("op_type_sell", 24)),
        "order_type": int(execution.get("order_type", 1101)),
        "pr_type_limit": int(execution.get("pr_type_limit", 11)),
        "quick_trade": int(execution.get("quick_trade", 1)),
        "trading_windows": list(execution.get("trading_windows", [["09:30", "11:30"], ["13:00", "14:57"]])),
    }
    return config


def _in_trading_window(windows):
    now = datetime.datetime.now().strftime("%H:%M")
    for window in windows:
        if len(window) == 2 and window[0] <= now <= window[1]:
            return True
    return False


# ---------------------------------------------------------------------------
# realtime export (always on)
# ---------------------------------------------------------------------------
def _simple_value(value):
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def _qmt_object_to_dict(obj):
    result = {}
    for name in dir(obj):
        if not name.startswith("m_"):
            continue
        try:
            value = getattr(obj, name)
        except Exception:
            continue
        if callable(value):
            continue
        result[name] = _simple_value(value)
    return result


def _first(record, names, default=None):
    for name in names:
        value = record.get(name)
        if value not in (None, ""):
            return value
    return default


def _normalize_order(record, account):
    """Normalized keys the Linux monitor consumes; raw m_ fields kept for audit."""
    return {
        "account_type": account["account_type"],
        "order_id": _first(record, ("m_strOrderSysID", "m_nOrderID", "m_strOrderRef")),
        "stock_code": _first(record, ("m_strInstrumentID", "m_strStockCode")),
        "order_type": _first(record, ("m_nOffsetFlag", "m_nDirection")),
        "order_status": _first(record, ("m_nOrderStatus", "m_strStatusMsg")),
        "order_volume": _first(record, ("m_nVolumeTotalOriginal", "m_nVolumeOriginal")),
        "traded_volume": _first(record, ("m_nVolumeTraded", "m_nTradedVolume")),
        "price": _first(record, ("m_dLimitPrice", "m_dPrice")),
        "order_time": _first(record, ("m_strInsertTime", "m_strOrderTime")),
        "order_remark": _first(record, ("m_strRemark", "m_strUserOrderId"), ""),
        "raw": record,
    }


def _normalize_deal(record, account):
    return {
        "account_type": account["account_type"],
        "traded_id": _first(record, ("m_strTradeID", "m_strTradeId")),
        "order_id": _first(record, ("m_strOrderSysID", "m_nOrderID", "m_strOrderRef")),
        "stock_code": _first(record, ("m_strInstrumentID", "m_strStockCode")),
        "order_type": _first(record, ("m_nOffsetFlag", "m_nDirection")),
        "traded_volume": _first(record, ("m_nVolume", "m_nTradedVolume")),
        "traded_price": _first(record, ("m_dPrice", "m_dTradedPrice")),
        "traded_amount": _first(record, ("m_dTradeAmount", "m_dTradedAmount")),
        "traded_time": _first(record, ("m_strTradeTime", "m_strTradedTime")),
        "order_remark": _first(record, ("m_strRemark", "m_strUserOrderId"), ""),
        "raw": record,
    }


def _normalize_asset(rows):
    """Best-effort ACCOUNT row -> the monitor's summary keys."""
    record = rows[0] if rows else {}
    return {
        "total_asset": _first(record, ("m_dBalance", "m_dTotalAsset", "m_dAsset")),
        "cash": _first(record, ("m_dAvailable", "m_dCash")),
        "market_value": _first(record, ("m_dInstrumentValue", "m_dStockValue", "m_dMarketValue")),
        "raw": record,
    }


def _export_cycle(state, accounts):
    day = _today()
    snapshot_accounts = []
    changed = False
    for account in accounts:
        account_id = account["account_id"]
        account_type = account["account_type"]
        data = {}
        for data_type in DATA_TYPES:
            rows = get_trade_detail_data(account_id, account_type, data_type)  # noqa: F821 - injected by QMT
            data[data_type] = [_qmt_object_to_dict(row) for row in rows]
        for record in data["ORDER"]:
            normalized = _normalize_order(record, account)
            order_id = str(normalized.get("order_id") or "")
            if not order_id:
                continue
            fingerprint = "%s|%s" % (normalized.get("order_status"), normalized.get("traded_volume"))
            # Day + account scoping: counters may reset order ids per day, and
            # STOCK/CREDIT accounts may reuse the same id space.
            key = "%s:%s:%s" % (day, account_type, order_id)
            if state["order_fingerprints"].get(key) == fingerprint:
                continue
            state["order_fingerprints"][key] = fingerprint
            _append_jsonl(os.path.join(OUTBOX_DIR, "orders_%s.jsonl" % day),
                          {"exported_at": _now_text(), "kind": "order", "record": normalized})
            changed = True
        for record in data["DEAL"]:
            normalized = _normalize_deal(record, account)
            traded_id = str(normalized.get("traded_id") or "")
            if not traded_id:
                continue
            deal_key = "%s:%s:%s" % (day, account_type, traded_id)
            if deal_key in state["seen_deal_ids"]:
                continue
            state["seen_deal_ids"].add(deal_key)
            _append_jsonl(os.path.join(OUTBOX_DIR, "deals_%s.jsonl" % day),
                          {"exported_at": _now_text(), "kind": "deal", "record": normalized})
            changed = True
        snapshot_accounts.append({
            "account_id": account_id,
            "account_type": account_type,
            "asset": _normalize_asset(data["ACCOUNT"]),
            "position_count": len(data["POSITION"]),
            "positions": data["POSITION"],
            "order_count": len(data["ORDER"]),
            "deal_count": len(data["DEAL"]),
        })
    primary = snapshot_accounts[0] if snapshot_accounts else {}
    _atomic_write_json(SNAPSHOT_PATH, {
        "generated_at": _now_text(),
        "ok": True,
        "source": "qmt_client_bridge",
        "asset": primary.get("asset", {}),
        "position_count": primary.get("position_count", 0),
        "accounts": snapshot_accounts,
    })
    return changed


# ---------------------------------------------------------------------------
# order execution (config-gated)
# ---------------------------------------------------------------------------
def _validate_payload(payload, config):
    errors = []
    execution = config["execution"]
    if payload.get("schema_version") != 2:
        errors.append("schema_version must be 2")
    if not str(payload.get("payload_id") or "").strip():
        errors.append("payload_id is required")
    if str(payload.get("strategy_id") or "") not in execution["allowed_strategy_ids"]:
        errors.append("strategy_id is not whitelisted in config")
    if str(payload.get("trade_date") or "") != _today():
        errors.append("trade_date must be the client's local date %s" % _today())
    # Live gates must never depend on truthiness: "false" (string) is truthy.
    if not isinstance(payload.get("execute"), bool):
        errors.append("execute must be JSON true or false")
    confirm = payload.get("confirm")
    if confirm is not None and not isinstance(confirm, str):
        errors.append("confirm must be a string when present")
    orders = payload.get("orders")
    if not isinstance(orders, list) or not orders:
        errors.append("orders must be a non-empty list")
        return errors
    total_notional = 0.0
    seen_keys = set()
    seen_remarks = set()
    for index, order in enumerate(orders):
        prefix = "orders[%d]" % index
        if not isinstance(order, dict):
            errors.append(prefix + " must be an object")
            continue
        code = str(order.get("code") or "")
        side = str(order.get("side") or "").upper()
        if not (code.endswith(".SH") or code.endswith(".SZ")):
            errors.append(prefix + ".code must be an SH/SZ code")
        if side not in ("BUY", "SELL"):
            errors.append(prefix + ".side must be BUY or SELL")
        volume = order.get("volume")
        price = order.get("price")
        if isinstance(volume, bool) or not isinstance(volume, int):
            errors.append(prefix + ".volume must be a JSON integer")
            continue
        if isinstance(price, bool) or not isinstance(price, (int, float)) or not math.isfinite(price):
            errors.append(prefix + ".price must be a finite JSON number")
            continue
        price = float(price)
        if volume <= 0 or (side == "BUY" and volume % 100 != 0):
            errors.append(prefix + ".volume must be positive (BUY in 100-share lots)")
        if price <= 0:
            errors.append(prefix + ".price must be positive")
        notional = max(0, volume) * max(0.0, price)
        if notional > execution["max_order_notional"]:
            errors.append(prefix + " notional %.2f exceeds max_order_notional" % notional)
        total_notional += notional
        key = (code, side)
        if key in seen_keys:
            errors.append(prefix + " duplicates %s %s" % (side, code))
        seen_keys.add(key)
        # The broker-side idempotency wall keys on the remark; two orders in
        # one payload sharing a remark (explicit remark collision, or an
        # explicit remark equal to another order's positional index) would be
        # submitted under one identity and silently defeat replay protection.
        remark_suffix = str(order.get("remark") or index)
        if remark_suffix in seen_remarks:
            errors.append(prefix + " remark %r collides with another order in this payload" % remark_suffix)
        seen_remarks.add(remark_suffix)
    if total_notional > execution["max_payload_notional"]:
        errors.append("payload notional %.2f exceeds max_payload_notional" % total_notional)
    return errors


def _order_remark(payload, order, index):
    suffix = str(order.get("remark") or index)
    return "MQ:%s:%s" % (payload["payload_id"], suffix)


def _existing_remarks(accounts):
    remarks = set()
    for account in accounts:
        rows = get_trade_detail_data(account["account_id"], account["account_type"], "ORDER")  # noqa: F821
        for row in rows:
            record = _qmt_object_to_dict(row)
            remark = _first(record, ("m_strRemark", "m_strUserOrderId"), "")
            if remark:
                remarks.add(str(remark))
    return remarks


def _journal_order(event, payload_id, row, error=None):
    """Persist one per-order journal record (fsynced before returning)."""
    record = {
        "logged_at": _now_text(),
        "event": event,
        "payload_id": payload_id,
        "remark": row.get("remark"),
        "code": row.get("code"),
        "side": row.get("side"),
        "volume": row.get("volume"),
        "price": row.get("price"),
    }
    if error:
        record["error"] = error
    _append_jsonl(os.path.join(os.path.dirname(STATE_PATH), "order_journal_%s.jsonl" % _today()), record)


def _process_payload(path, config, state, ContextInfo):
    execution = config["execution"]
    payload = _read_json_strict(path)
    payload_id = str(payload.get("payload_id") or os.path.basename(path))
    result = {
        "ok": False,
        "payload_id": payload_id,
        "source_file": os.path.basename(path),
        "checked_at": _now_text(),
        "mode": "dry_run",
        "orders": [],
    }
    if ("%s:%s" % (_today(), payload_id)) in state["processed_payloads"]:
        result["error"] = "payload already processed"
        return result
    errors = _validate_payload(payload, config)
    if errors:
        result["error"] = "; ".join(errors)
        return result

    # Both operands are validated JSON booleans by this point.
    live = execution["enabled"] and payload["execute"]
    if live and str(payload.get("confirm") or "") != payload_id:
        result["error"] = "live execution requires confirm == payload_id"
        return result
    if live and not _in_trading_window(execution["trading_windows"]):
        result["error"] = "outside configured trading windows"
        return result
    result["mode"] = "live" if live else "dry_run"

    # Idempotency wall: never resubmit a remark the counter already knows.
    accounts = config["accounts"]
    account = accounts[0]  # explicit account routing arrives with CREDIT support
    known_remarks = _existing_remarks(accounts) if live else set()
    submitted = 0
    for index, order in enumerate(payload["orders"]):
        remark = _order_remark(payload, order, index)
        side = str(order.get("side")).upper()
        row = {
            "code": order.get("code"), "side": side,
            "volume": int(order.get("volume")), "price": float(order.get("price")),
            "remark": remark, "submitted": False,
        }
        if not live:
            row["note"] = "dry_run"
        elif remark in known_remarks:
            row["note"] = "skipped: remark already on the counter"
        else:
            op_type = execution["op_type_buy"] if side == "BUY" else execution["op_type_sell"]
            _journal_order("intent", payload_id, row)
            try:
                passorder(  # noqa: F821 - injected by QMT
                    op_type, execution["order_type"], account["account_id"],
                    str(order.get("code")), execution["pr_type_limit"], float(order.get("price")),
                    int(order.get("volume")), "qmt_client_bridge", execution["quick_trade"],
                    remark, ContextInfo,
                )
            except Exception:
                # Abort the payload: earlier submissions keep their persisted
                # "submitted" journal records; this order gets an "error"
                # terminal record. The payload is NOT marked processed, so a
                # retry payload is possible and the remark idempotency wall
                # protects the already-submitted orders from resubmission.
                _journal_order("error", payload_id, row, error=traceback.format_exc())
                row["note"] = "error: passorder raised"
                result["orders"].append(row)
                result["submitted_count"] = submitted
                result["error"] = (
                    "passorder failed at orders[%d]; earlier submitted orders stand "
                    "(see state order journal)" % index
                )
                return result
            row["submitted"] = True
            submitted += 1
            _journal_order("submitted", payload_id, row)
        result["orders"].append(row)
    result["ok"] = True
    result["submitted_count"] = submitted
    state["processed_payloads"]["%s:%s" % (_today(), payload_id)] = {
        "processed_at": _now_text(), "mode": result["mode"], "submitted_count": submitted,
    }
    return result


def _poll_inbox(config, state, ContextInfo):
    if not os.path.isdir(INBOX_DIR):
        return
    names = sorted(
        name for name in os.listdir(INBOX_DIR)
        if name.endswith(".json") and not name.endswith(".tmp")
    )
    for name in names[:MAX_PAYLOADS_PER_TICK]:
        path = os.path.join(INBOX_DIR, name)
        try:
            result = _process_payload(path, config, state, ContextInfo)
        except Exception:
            result = {"ok": False, "payload_id": name, "checked_at": _now_text(),
                      "error": traceback.format_exc()}
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        prefix = "execute" if result.get("ok") else "error"
        _atomic_write_json(os.path.join(OUTBOX_DIR, "%s_%s.json" % (prefix, stamp)), result)
        _ensure_dir(ARCHIVE_DIR)
        os.replace(path, os.path.join(ARCHIVE_DIR, "%s_%s" % (stamp, name)))
        _save_state(state)


# ---------------------------------------------------------------------------
# QMT entry points
# ---------------------------------------------------------------------------
def bridge_poll(ContextInfo):
    state = _load_state()
    try:
        config = _load_config()
        changed = _export_cycle(state, config["accounts"])
        if changed:
            _save_state(state)
        _poll_inbox(config, state, ContextInfo)
    except Exception:
        _atomic_write_json(SNAPSHOT_PATH, {
            "generated_at": _now_text(), "ok": False,
            "source": "qmt_client_bridge", "error": traceback.format_exc(),
        })


def init(ContextInfo):
    for path in (os.path.dirname(CONFIG_PATH), INBOX_DIR, OUTBOX_DIR, ARCHIVE_DIR, os.path.dirname(STATE_PATH)):
        _ensure_dir(path)
    ContextInfo.run_time("bridge_poll", POLL_PERIOD, TIMER_START, TIMER_MARKET)
    print("qmt_client_bridge initialized (execution %s)" %
          ("ENABLED" if _load_config()["execution"]["enabled"] else "disabled/dry-run"))


def handlebar(ContextInfo):
    pass


def stop(ContextInfo):
    print("qmt_client_bridge stopped")
