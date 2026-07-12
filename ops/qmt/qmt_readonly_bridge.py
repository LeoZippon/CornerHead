# coding: gbk
"""Read-only file bridge for the full QMT in-client Python 3.6 runtime.

This example never calls passorder, cancel, or any other mutating API. It reads
account snapshots through QMT's injected get_trade_detail_data function and
publishes one JSON file atomically for the Linux decision host to collect.
"""

from __future__ import print_function

import datetime
import io
import json
import os
import traceback


ROOT_DIR = r"C:\xquant"
CONFIG_PATH = os.path.join(ROOT_DIR, "config", "qmt_bridge.json")
OUTBOX_DIR = os.path.join(ROOT_DIR, "outbox")
SNAPSHOT_PATH = os.path.join(OUTBOX_DIR, "account_snapshot.json")
POLL_PERIOD = "15nSecond"
TIMER_START = "2020-01-01 00:00:00"
TIMER_MARKET = "SH"
ACCOUNT_TYPES = ("STOCK", "CREDIT")
DATA_TYPES = ("ACCOUNT", "POSITION", "ORDER", "DEAL")


def _utc_now():
    return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def _ensure_dir(path):
    if not os.path.isdir(path):
        os.makedirs(path)


def _load_config():
    with io.open(CONFIG_PATH, "r", encoding="utf-8-sig") as handle:
        config = json.load(handle)

    accounts = config.get("accounts")
    if not isinstance(accounts, list) or not accounts:
        raise ValueError("config.accounts must be a non-empty list")

    normalized = []
    seen = set()
    for row in accounts:
        if not isinstance(row, dict):
            raise ValueError("each account entry must be an object")
        account_id = str(row.get("account_id", "")).strip()
        account_type = str(row.get("account_type", "")).strip().upper()
        if not account_id:
            raise ValueError("account_id must not be empty")
        if account_type not in ACCOUNT_TYPES:
            raise ValueError("account_type must be STOCK or CREDIT")
        key = (account_id, account_type)
        if key in seen:
            raise ValueError("duplicate account entry")
        seen.add(key)
        normalized.append({"account_id": account_id, "account_type": account_type})
    return normalized


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


def _query_account(account):
    account_id = account["account_id"]
    account_type = account["account_type"]
    result = {
        "account_id": account_id,
        "account_type": account_type,
    }
    for data_type in DATA_TYPES:
        rows = get_trade_detail_data(account_id, account_type, data_type)
        result[data_type.lower()] = [_qmt_object_to_dict(row) for row in rows]
    return result


def _atomic_write_json(path, payload):
    _ensure_dir(os.path.dirname(path))
    tmp_path = path + ".tmp"
    with io.open(tmp_path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=True, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, path)


def bridge_poll(ContextInfo):
    try:
        accounts = _load_config()
        payload = {
            "generated_at": _utc_now(),
            "ok": True,
            "mode": "read_only",
            "accounts": [_query_account(account) for account in accounts],
        }
    except Exception:
        payload = {
            "generated_at": _utc_now(),
            "ok": False,
            "mode": "read_only",
            "error": traceback.format_exc(),
        }
    _atomic_write_json(SNAPSHOT_PATH, payload)


def init(ContextInfo):
    _ensure_dir(os.path.dirname(CONFIG_PATH))
    _ensure_dir(OUTBOX_DIR)
    ContextInfo.run_time("bridge_poll", POLL_PERIOD, TIMER_START, TIMER_MARKET)
    print("qmt_readonly_bridge initialized")


def handlebar(ContextInfo):
    pass


def stop(ContextInfo):
    print("qmt_readonly_bridge stopped")
