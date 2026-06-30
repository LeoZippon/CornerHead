"""Shared minute-replay market data, fallbacks, and return statistics.

Holds the pieces the per-minute ``main(ctx)`` engine (``main_ctx_engine.py``)
builds on: daily/minute replay market data, the daily-synthesized 09:30/15:00
fallback, the formal-strategy path guard, the NL request pump, and
``compute_return_stats``. The Broker owns no strategy logic; it only enforces
market rules and records fills.
"""

from __future__ import annotations

import json
import math
import os
import re
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pandas as pd

from autotrade.environment.broker import MarketData, SimBroker
from autotrade.environment.runtime import sanitize_for_log

TRADING_DAYS_PER_YEAR = 252

_STRATEGY_PATH_GUARD = """\
def _normalize_path(value):
    try:
        raw = os.fspath(value)
    except TypeError:
        return None
    path = Path(raw)
    if not path.is_absolute():
        path = Path.cwd() / path
    return Path(os.path.normpath(str(path)))


def _path_aliases(value):
    path = _normalize_path(value)
    if path is None:
        return ()
    aliases = [path]
    try:
        real_path = Path(os.path.realpath(str(path)))
    except OSError:
        real_path = path
    if real_path not in aliases:
        aliases.append(real_path)
    return tuple(aliases)


def _path_roots(env_name):
    roots = []
    for item in os.environ.get(env_name, "").split(os.pathsep):
        if not item:
            continue
        roots.extend(_path_aliases(item))
    return tuple(roots)


_FORBIDDEN_PATHS = _path_roots("AT_FORBIDDEN_PATHS")
_WRITE_FORBIDDEN_PATHS = _path_roots("AT_WRITE_FORBIDDEN_PATHS")
_DISABLE_LINKS = os.environ.get("AT_DISABLE_LINKS", "") == "1"


def _is_under(path, root):
    return path == root or root in path.parents


def _guard_path(value, *, write=False):
    aliases = _path_aliases(value)
    if not aliases:
        return
    roots = _FORBIDDEN_PATHS + (_WRITE_FORBIDDEN_PATHS if write else ())
    for path in aliases:
        for forbidden in roots:
            if _is_under(path, forbidden):
                action = "write" if write else "access"
                raise PermissionError(f"formal strategy cannot {action} forbidden path: {value}")


def _open_is_write(args, kwargs):
    mode = kwargs.get("mode")
    if mode is None and args:
        mode = args[0]
    if mode is None:
        return False
    mode_text = str(mode)
    return any(flag in mode_text for flag in ("w", "a", "x", "+"))


def _os_open_is_write(flags):
    write_flags = (
        os.O_WRONLY
        | os.O_RDWR
        | os.O_CREAT
        | os.O_APPEND
        | os.O_TRUNC
    )
    return bool(int(flags) & write_flags)


def _deny_link_creation():
    if _DISABLE_LINKS:
        raise PermissionError("formal strategy cannot create links during replay")


_open = builtins.open


def _guarded_open(file, *args, **kwargs):
    _guard_path(file, write=_open_is_write(args, kwargs))
    return _open(file, *args, **kwargs)


_path_open = Path.open


def _guarded_path_open(self, *args, **kwargs):
    _guard_path(self, write=_open_is_write(args, kwargs))
    return _path_open(self, *args, **kwargs)


_os_open = os.open


def _guarded_os_open(path, *args, **kwargs):
    flags = kwargs.get("flags")
    if flags is None and args:
        flags = args[0]
    _guard_path(path, write=_os_open_is_write(flags or 0))
    return _os_open(path, *args, **kwargs)


_os_mkdir = os.mkdir


def _guarded_os_mkdir(path, *args, **kwargs):
    _guard_path(path, write=True)
    return _os_mkdir(path, *args, **kwargs)


_os_makedirs = os.makedirs


def _guarded_os_makedirs(name, *args, **kwargs):
    _guard_path(name, write=True)
    return _os_makedirs(name, *args, **kwargs)


_path_mkdir = Path.mkdir


def _guarded_path_mkdir(self, *args, **kwargs):
    _guard_path(self, write=True)
    return _path_mkdir(self, *args, **kwargs)


_os_unlink = os.unlink


def _guarded_os_unlink(path, *args, **kwargs):
    _guard_path(path, write=True)
    return _os_unlink(path, *args, **kwargs)


os.remove = _guarded_os_unlink


_path_unlink = Path.unlink


def _guarded_path_unlink(self, *args, **kwargs):
    _guard_path(self, write=True)
    return _path_unlink(self, *args, **kwargs)


_os_rmdir = os.rmdir


def _guarded_os_rmdir(path, *args, **kwargs):
    _guard_path(path, write=True)
    return _os_rmdir(path, *args, **kwargs)


_path_rmdir = Path.rmdir


def _guarded_path_rmdir(self, *args, **kwargs):
    _guard_path(self, write=True)
    return _path_rmdir(self, *args, **kwargs)


_os_rename = os.rename


def _guarded_os_rename(src, dst, *args, **kwargs):
    _guard_path(src, write=True)
    _guard_path(dst, write=True)
    return _os_rename(src, dst, *args, **kwargs)


_os_replace = os.replace


def _guarded_os_replace(src, dst, *args, **kwargs):
    _guard_path(src, write=True)
    _guard_path(dst, write=True)
    return _os_replace(src, dst, *args, **kwargs)


_path_rename = Path.rename


def _guarded_path_rename(self, target):
    _guard_path(self, write=True)
    _guard_path(target, write=True)
    return _path_rename(self, target)


_path_replace = Path.replace


def _guarded_path_replace(self, target):
    _guard_path(self, write=True)
    _guard_path(target, write=True)
    return _path_replace(self, target)


_os_symlink = os.symlink


def _guarded_os_symlink(src, dst, *args, **kwargs):
    _deny_link_creation()
    _guard_path(src)
    _guard_path(dst, write=True)
    return _os_symlink(src, dst, *args, **kwargs)


_path_symlink_to = Path.symlink_to


def _guarded_path_symlink_to(self, target, *args, **kwargs):
    _deny_link_creation()
    _guard_path(target)
    _guard_path(self, write=True)
    return _path_symlink_to(self, target, *args, **kwargs)


_os_link = os.link


def _guarded_os_link(src, dst, *args, **kwargs):
    _deny_link_creation()
    _guard_path(src)
    _guard_path(dst, write=True)
    return _os_link(src, dst, *args, **kwargs)


_path_hardlink_to = Path.hardlink_to


def _guarded_path_hardlink_to(self, target):
    _deny_link_creation()
    _guard_path(target)
    _guard_path(self, write=True)
    return _path_hardlink_to(self, target)


_os_listdir = os.listdir


def _guarded_os_listdir(path=None):
    if path is not None:
        _guard_path(path)
        return _os_listdir(path)
    return _os_listdir()


_os_scandir = os.scandir


def _guarded_os_scandir(path=None):
    if path is not None:
        _guard_path(path)
        return _os_scandir(path)
    return _os_scandir()


builtins.open = _guarded_open
Path.open = _guarded_path_open
os.open = _guarded_os_open
os.mkdir = _guarded_os_mkdir
os.makedirs = _guarded_os_makedirs
Path.mkdir = _guarded_path_mkdir
os.unlink = _guarded_os_unlink
Path.unlink = _guarded_path_unlink
os.rmdir = _guarded_os_rmdir
Path.rmdir = _guarded_path_rmdir
os.rename = _guarded_os_rename
os.replace = _guarded_os_replace
Path.rename = _guarded_path_rename
Path.replace = _guarded_path_replace
os.symlink = _guarded_os_symlink
Path.symlink_to = _guarded_path_symlink_to
os.link = _guarded_os_link
Path.hardlink_to = _guarded_path_hardlink_to
os.listdir = _guarded_os_listdir
os.scandir = _guarded_os_scandir

"""


class BacktestError(RuntimeError):
    """A formal backtest step failed; the error is explicit, never silent."""


def _serve_nl_requests(
    requests_path: Path,
    responses_path: Path,
    served: set[str],
    nl_service,
    offset: int = 0,
) -> int:
    """Serve NL requests appended past ``offset``; return the new byte offset.

    Only the bytes after ``offset`` are read, and only whole lines are consumed:
    a partial trailing line (a request still being flushed) is left in place for
    the next call, so the incremental read never loses or splits a request. The
    ``served`` set stays a dedup backstop.
    """
    if not requests_path.exists():
        return offset
    with requests_path.open("rb") as handle:
        handle.seek(offset)
        chunk = handle.read()
    head, sep, _partial = chunk.rpartition(b"\n")
    if not sep:
        return offset  # no complete line appended yet
    new_offset = offset + len(head) + len(sep)
    for raw in head.splitlines():
        line = raw.decode("utf-8", "replace").strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        request_id = str(request.get("request_id", ""))
        if not request_id or request_id in served:
            continue
        served.add(request_id)
        if nl_service is None:
            response = {"request_id": request_id, "status": "error", "error": "nl proxy is not configured"}
        else:
            try:
                result = nl_service.run(
                    str(request.get("ts_code", "")),
                    prompt=str(request.get("prompt", "") or ""),
                    kwargs=dict(request.get("kwargs") or {}),
                    request=dict(request),
                )
                response = {"request_id": request_id, "status": "ok", "result": result}
            except Exception as exc:  # noqa: BLE001 - strategy sees a fixable tool error
                error = sanitize_for_log(f"{type(exc).__name__}: {exc}")
                response = {"request_id": request_id, "status": "error", "error": error}
        with responses_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(response, ensure_ascii=False, default=str) + "\n")
    return new_offset


def _jsonable(value):
    if isinstance(value, pd.Series):
        return {str(k): _jsonable(v) for k, v in value.to_dict().items()}
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if pd.isna(value):
        return None
    if hasattr(value, "item"):
        try:
            return _jsonable(value.item())
        except Exception:  # noqa: BLE001 - keep JSON conversion best-effort
            pass
    return value


@contextmanager
def hide_snapshot_slots_from_agent(paths):
    """Temporarily hide replay/exploration/artifact slots from strategy code.

    Docker runs candidate code as the non-root ``agent`` user. Making the slot
    roots owner-only is enough to prevent traversal while keeping the current
    `/mnt/snapshot` view and staged workspace inputs available.
    """
    slots: list[tuple[Path, int]] = []
    for path in (paths.train, paths.valid, paths.test, paths.artifacts):
        if path.exists():
            slots.append((path, stat.S_IMODE(path.stat().st_mode)))
    try:
        for path, _mode in slots:
            path.chmod(0o700)
        yield
    finally:
        for path, mode in slots:
            path.chmod(mode)


def _executor_pathsep_join(executor, paths: list[Path]) -> str:
    return os.pathsep.join(executor.map_path(path) for path in paths if path.exists())


@dataclass
class ReplayResult:
    equity_curve: pd.Series
    broker: SimBroker
    decision_date: str
    exit_date: str
    granularity: str = "minute"
    # Cost feedback: per-sub-step wall-time aggregates, total replay wall-clock, and
    # the number of trade days replayed (so the Agent can extrapolate a full run from
    # a small replay_window test pass).
    substep_runtime: dict[str, dict[str, float]] | None = None
    replay_wall_seconds: float | None = None
    replayed_trade_days: int | None = None
    # 24h tick-grid breakdown: total main(ctx) ticks and how many were intraday
    # (matchable session/auction bars) vs off-session (research/state only), so the
    # Agent can see the extra cost the off-session grid adds.
    total_ticks: int | None = None
    intraday_ticks: int | None = None
    offsession_ticks: int | None = None
    # Managed ctx.state_dir staging ledger: one record per sub-step-staged write with
    # its ready_at and merge status (some may stay unmerged past the region end).
    state_staging_audit: list[dict[str, object]] | None = None


class MinuteMarketData:
    """Minute replay bars indexed by trade date, minute, and code."""

    REQUIRED = ("trade_date", "ts_code", "close")
    TIME_COLUMNS = ("trade_time", "datetime", "timestamp", "time")

    def __init__(self, minutes: pd.DataFrame) -> None:
        if minutes.empty:
            raise ValueError("minute replay data is empty")
        missing = [col for col in self.REQUIRED if col not in minutes.columns]
        if missing:
            raise ValueError(f"replay minute data missing columns: {missing}")
        time_column = next((col for col in self.TIME_COLUMNS if col in minutes.columns), None)
        if time_column is None:
            raise ValueError(f"replay minute data missing one of time columns: {list(self.TIME_COLUMNS)}")
        frame = minutes.copy()
        frame["trade_date"] = frame["trade_date"].astype(str)
        frame["ts_code"] = frame["ts_code"].astype(str)
        frame["minute_key"] = frame[time_column].map(_minute_key)
        if frame["minute_key"].isna().any():
            bad = frame.loc[frame["minute_key"].isna(), time_column].head(5).tolist()
            raise ValueError(f"replay minute data has invalid trade_time values: {bad}")
        frame["minute_sort"] = frame["minute_key"].map(_minute_sort)
        frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
        if "open" not in frame.columns:
            frame["open"] = frame["close"]
        else:
            frame["open"] = pd.to_numeric(frame["open"], errors="coerce")
        if "high" not in frame.columns:
            frame["high"] = frame[["open", "close"]].max(axis=1)
        else:
            frame["high"] = pd.to_numeric(frame["high"], errors="coerce")
        if "low" not in frame.columns:
            frame["low"] = frame[["open", "close"]].min(axis=1)
        else:
            frame["low"] = pd.to_numeric(frame["low"], errors="coerce")
        frame = frame.sort_values(["trade_date", "minute_sort", "ts_code"], kind="stable").reset_index(drop=True)
        self._frame = frame

    def rows_for_date(self, trade_date: str) -> pd.DataFrame:
        return self._frame[self._frame["trade_date"] == str(trade_date)].copy()


def _minute_key(value: object) -> str | None:
    if value is None or pd.isna(value):
        return None
    text = str(value).strip()
    if not text:
        return None
    match = re.search(r"(\d{1,2}):(\d{2})", text)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        return f"{hour:02d}:{minute:02d}" if 0 <= hour <= 23 and 0 <= minute <= 59 else None
    digits = re.sub(r"\D", "", text)
    if len(digits) >= 12:
        hour, minute = int(digits[8:10]), int(digits[10:12])
        return f"{hour:02d}:{minute:02d}" if 0 <= hour <= 23 and 0 <= minute <= 59 else None
    if len(digits) in {4, 6}:
        hour, minute = int(digits[:2]), int(digits[2:4])
        return f"{hour:02d}:{minute:02d}" if 0 <= hour <= 23 and 0 <= minute <= 59 else None
    parsed = pd.to_datetime(text, errors="coerce")
    if pd.isna(parsed):
        return None
    return parsed.strftime("%H:%M")


def _minute_sort(minute_key: str) -> int:
    hour, minute = str(minute_key).split(":", maxsplit=1)
    return int(hour) * 60 + int(minute)


def _empty_minute_rows() -> pd.DataFrame:
    return pd.DataFrame(columns=["trade_date", "ts_code", "open", "close", "high", "low", "minute_key", "minute_sort"])


def _synthetic_daily_minutes(replay_daily: pd.DataFrame, trade_date: str) -> pd.DataFrame:
    """Fallback minute bars (09:30 open, 15:00 close) for daily-only dates."""
    rows = replay_daily[replay_daily["trade_date"].astype(str) == str(trade_date)].copy()
    if rows.empty:
        return _empty_minute_rows()
    lows = rows.apply(_daily_low, axis=1)
    highs = rows.apply(_daily_high, axis=1)
    open_rows = rows.copy()
    open_rows["close"] = open_rows["open"]
    # The 09:30 open bar must expose only the opening price: day high/low and the
    # full-day vol/amount are post-open information and would leak look-ahead.
    open_rows["high"] = open_rows["open"]
    open_rows["low"] = open_rows["open"]
    for column in ("vol", "amount"):
        if column in open_rows.columns:
            open_rows[column] = math.nan
    open_rows["minute_key"] = "09:30"
    close_rows = rows.copy()
    close_rows["open"] = close_rows["close"]
    close_rows["high"] = highs
    close_rows["low"] = lows
    close_rows["minute_key"] = "15:00"
    frame = pd.concat([open_rows, close_rows], ignore_index=True)
    frame["minute_sort"] = frame["minute_key"].map(_minute_sort)
    return frame.sort_values(["minute_sort", "ts_code"], kind="stable").reset_index(drop=True)


def _minute_rows_with_daily_fallback(
    replay_daily: pd.DataFrame,
    trade_date: str,
    minute_rows: pd.DataFrame,
) -> pd.DataFrame:
    fallback = _synthetic_daily_minutes(replay_daily, trade_date)
    if minute_rows.empty:
        return fallback
    present_codes = set(minute_rows["ts_code"].astype(str))
    missing_rows = fallback[~fallback["ts_code"].astype(str).isin(present_codes)]
    close_fallback = fallback[
        (fallback["minute_key"] == "15:00")
        & fallback["ts_code"].astype(str).isin(present_codes)
    ].copy()
    if not close_fallback.empty:
        existing_keys = set(zip(minute_rows["ts_code"].astype(str), minute_rows["minute_key"].astype(str)))
        close_fallback = close_fallback[
            [
                (str(row.ts_code), str(row.minute_key)) not in existing_keys
                for row in close_fallback.itertuples()
            ]
        ]
    if missing_rows.empty and close_fallback.empty:
        return minute_rows
    return pd.concat([minute_rows, missing_rows, close_fallback], ignore_index=True).sort_values(
        ["minute_sort", "ts_code"],
        kind="stable",
    ).reset_index(drop=True)


def _daily_low(bar: pd.Series) -> float:
    values = [bar.get("low"), bar.get("open"), bar.get("close")]
    numeric = [float(value) for value in values if pd.notna(value)]
    return min(numeric) if numeric else math.nan


def _daily_high(bar: pd.Series) -> float:
    values = [bar.get("high"), bar.get("open"), bar.get("close")]
    numeric = [float(value) for value in values if pd.notna(value)]
    return max(numeric) if numeric else math.nan


def compute_return_stats(result: ReplayResult) -> dict[str, object]:
    """The minimum return statistics from docs/environment_design.md 7.6."""
    broker = result.broker
    curve = result.equity_curve
    initial = broker.initial_equity
    total_return = curve.iloc[-1] / initial - 1.0 if len(curve) else 0.0
    daily_returns = curve.pct_change().dropna()
    sharpe = 0.0
    if len(daily_returns) > 1 and daily_returns.std(ddof=1) > 0:
        sharpe = float(daily_returns.mean() / daily_returns.std(ddof=1) * math.sqrt(TRADING_DAYS_PER_YEAR))
    peak = curve.cummax()
    max_drawdown = float(((peak - curve) / peak).max()) if len(curve) else 0.0
    years = max(len(curve), 1) / TRADING_DAYS_PER_YEAR
    annualized = float((1.0 + total_return) ** (1.0 / years) - 1.0) if total_return > -1.0 else -1.0
    realized = [event for event in broker.events if event["event_type"] in {"position_closed", "position_reduced"}]
    full_closes = [event for event in broker.events if event["event_type"] == "position_closed"]
    long_pnl = sum(e["realized_pnl"] for e in realized if e["side"] == "long")
    short_pnl = sum(e["realized_pnl"] for e in realized if e["side"] == "short")
    wins = sum(1 for e in realized if e["realized_pnl"] > 0)
    orders = broker.query_stock_orders()
    per_stock = [
        {
            "ts_code": event["ts_code"],
            "side": event["side"],
            "exit_date": event["trade_date"],
            "exit_price": event["price"],
            "exit_price_label": event.get("price_label"),
            "quantity": event.get("quantity"),
            "realized_pnl": event["realized_pnl"],
            "kind": event["event_type"],
            "forced": event.get("forced", False),
        }
        for event in realized
    ]
    status_counts: dict[str, int] = {}
    for order in orders:
        status_counts[str(order["status"])] = status_counts.get(str(order["status"]), 0) + 1
    return {
        "initial_cash": initial,
        "final_equity": float(curve.iloc[-1]) if len(curve) else initial,
        "total_return": float(total_return),
        "long_return": float(long_pnl / initial),
        "short_return": float(short_pnl / initial),
        "annualized_return": annualized,
        "sharpe": sharpe,
        "max_drawdown": max_drawdown,
        "win_rate": float(wins / len(realized)) if realized else 0.0,
        "holdings_count": len(full_closes),
        "trade_count": len(realized),
        "turnover": float(broker.traded_notional / initial) if initial else 0.0,
        "order_count": len(orders),
        "order_status_counts": status_counts,
        "reject_counts": dict(broker.reject_counts),
        "margin_secs_reject_count": broker.reject_counts.get("margin_secs_not_shortable", 0),
        "broker_inventory_reject_count": broker.reject_counts.get("broker_inventory_unavailable", 0),
        "max_holdings_reject_count": broker.reject_counts.get("max_holdings_reached", 0),
        "fees_paid": float(broker.fees_paid),
        "stamp_duty_paid": float(broker.stamp_duty_paid),
        "slippage_bps_assumed": broker.profile.slippage_bps,
        "short_borrow_fees": float(broker.borrow_fees),
        "forced_close_events": sum(1 for e in broker.events if e["event_type"] == "forced_close_triggered"),
        "replay_granularity": result.granularity,
        "replay_wall_seconds": result.replay_wall_seconds,
        "replayed_trade_days": result.replayed_trade_days,
        "substep_runtime": result.substep_runtime or {},
        "equity_curve": {str(k): float(v) for k, v in curve.items()},
        "decision_date": result.decision_date,
        "exit_date": result.exit_date,
        "per_stock": per_stock,
        "broker_events": broker.events,
    }
