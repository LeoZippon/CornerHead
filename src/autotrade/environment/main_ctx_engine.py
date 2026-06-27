"""Unified per-minute ``main(ctx)`` execution engine (docs/environment_design.md).

The Environment replays the region minute by minute and calls the Agent's single
``main(ctx)`` entrypoint once per minute with a market-level ``ctx``. ``main``
owns all timing, screening, and position management; it drives the Broker's
strategy-agnostic primitives (``ctx.broker.buy/sell/short/cover/close`` by
``ts_code``). The host Broker applies every market constraint and records fills.

This replaces the previous two-path model (one-shot ``run_strategy`` returning a
fixed ``trade_intents`` mapping plus a per-stock ``trade_strategy(ctx)`` driver):
one persistent sandbox process serves a per-minute RPC, so the Agent can open new
positions at any minute, not only at the fold decision time.
"""

from __future__ import annotations

import json
import select
import sys
import threading
import time
import uuid
from collections import deque
from pathlib import Path

import pandas as pd

from autotrade.environment.backtest_engine import (
    _STRATEGY_PATH_GUARD,
    BacktestError,
    MarketData,
    MinuteMarketData,
    ReplayResult,
    _bar_execution_price,
    _empty_minute_rows,
    _executor_pathsep_join,
    _jsonable,
    _minute_bar_for_code,
    _minute_rows_with_daily_fallback,
    _minute_sort,
    _serve_nl_requests,
    hide_snapshot_slots_from_agent,
)
from autotrade.environment.broker import SimBroker
from autotrade.environment.runtime import sanitize_for_log

_ACTION_ALIASES = {"long": "buy", "sell_short": "short", "close_long": "sell", "close_short": "cover", "exit": "close"}
_SUPPORTED_ACTIONS = {"buy", "sell", "short", "cover", "close"}


_MAIN_DRIVER = """\
import builtins, contextlib, importlib.util, json, os, re, sys, time, types, uuid
from pathlib import Path

import pandas as pd

_PROTOCOL_STDOUT = sys.stdout

""" + _STRATEGY_PATH_GUARD + """\
_SECRET_PATTERNS = (
    (re.compile(r"sk-[A-Za-z0-9_-]{8,}"), "sk-***"),
    (re.compile(r"Bearer\\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE), "Bearer [REDACTED]"),
)


def _sanitize_error(value):
    text = str(value)
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _append_jsonl(path, record):
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\\n")


def _read_responses(path):
    responses = {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                record = json.loads(line)
                responses[str(record.get("request_id"))] = record
    except FileNotFoundError:
        return responses
    return responses


def _nl(ts_code="", prompt="", *, timeout_seconds=None, content_only=False, **kwargs):
    request_path = os.environ.get("AT_NL_REQUESTS_PATH", "")
    response_path = os.environ.get("AT_NL_RESPONSES_PATH", "")
    if not request_path or not response_path:
        raise RuntimeError("nl tool is not configured for this backtest")
    request_id = uuid.uuid4().hex
    _append_jsonl(request_path, {"request_id": request_id, "ts_code": str(ts_code), "prompt": str(prompt or ""), "kwargs": kwargs})
    timeout = float(timeout_seconds or os.environ.get("AT_NL_TOOL_TIMEOUT_SECONDS", "300"))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = _read_responses(response_path).get(request_id)
        if response is not None:
            if response.get("status") != "ok":
                raise RuntimeError(str(response.get("error", "nl tool failed")))
            result = response.get("result") or {}
            return str(result.get("content", "")) if content_only else result
        time.sleep(0.05)
    raise TimeoutError("nl tool timed out after %ss for %s" % (timeout, ts_code))


tools_module = types.ModuleType("at_tools")
tools_module.nl = _nl
sys.modules["at_tools"] = tools_module


def _bar_price(bar):
    if not bar:
        return None
    for field in ("close", "open"):
        value = bar.get(field)
        if value not in (None, ""):
            try:
                return float(value)
            except (TypeError, ValueError):
                return None
    return None


class _Broker:
    \"\"\"Market-wide, ts_code-keyed view of the host Broker primitives.

    Calls are recorded as deferred actions applied by the trusted host Broker
    with full market constraints. An optimistic intra-minute view of cash and
    per-code position keeps several actions in one minute self-consistent.\"\"\"

    def __init__(self, state, prices):
        account = state.get("account") or {}
        self.account = account
        self.positions = state.get("positions") or []
        self._cash = float(state.get("cash", account.get("cash", 0.0)) or 0.0)
        self._initial_equity = float(state.get("initial_equity", 0.0) or 0.0)
        self._prices = prices
        self._pos = {}
        for item in self.positions:
            qty = int(item.get("quantity", 0) or 0)
            self._pos[str(item.get("ts_code"))] = qty if str(item.get("side", "long")) == "long" else -qty
        self._actions = []

    @property
    def cash(self):
        return self._cash

    @property
    def money(self):
        return self._cash

    def position(self, ts_code):
        return self._pos.get(str(ts_code), 0)

    def buy(self, ts_code, amount=None, weight=None, reason=None, **kwargs):
        self._order("buy", ts_code, amount, weight, reason, +1)

    def sell(self, ts_code, amount=None, reason=None, **kwargs):
        self._order("sell", ts_code, amount, None, reason, -1)

    def short(self, ts_code, amount=None, weight=None, reason=None, **kwargs):
        self._order("short", ts_code, amount, weight, reason, -1)

    def cover(self, ts_code, amount=None, reason=None, **kwargs):
        self._order("cover", ts_code, amount, None, reason, +1)

    def close(self, ts_code, reason=None, **kwargs):
        code = str(ts_code)
        self._actions.append({"action": "close", "ts_code": code, "reason": reason})
        price = self._prices.get(code)
        held = self._pos.get(code, 0)
        if price is not None:
            self._cash += held * price
        self._pos[code] = 0

    def _order(self, action, ts_code, amount, weight, reason, sign):
        code = str(ts_code)
        record = {"action": action, "ts_code": code}
        if amount is not None:
            record["amount"] = amount
        if weight is not None:
            record["weight"] = weight
        if reason is not None:
            record["reason"] = reason
        self._actions.append(record)
        price = self._prices.get(code)
        shares = self._resolve_shares(amount, weight, price)
        if shares > 0 and price is not None:
            self._pos[code] = self._pos.get(code, 0) + sign * shares
            self._cash -= sign * shares * price

    def _resolve_shares(self, amount, weight, price):
        if price is None or price <= 0:
            return 0
        try:
            if amount is not None and str(amount).strip() != "":
                raw = int(float(amount))
            elif weight is not None and str(weight).strip() != "":
                raw = int(abs(float(weight)) * self._initial_equity / price)
            else:
                return 0
        except (TypeError, ValueError):
            return 0
        return (raw // 100) * 100


def _build_ctx(state, snapshot_dir, model_dir, state_dir):
    bars = {str(b.get("ts_code", "")): dict(b) for b in (state.get("bars") or [])}
    prices = {code: _bar_price(bar) for code, bar in bars.items()}
    broker = _Broker(state, prices)

    def price(ts_code):
        return prices.get(str(ts_code))

    def bar(ts_code):
        return bars.get(str(ts_code))

    return types.SimpleNamespace(
        broker=broker,
        account=broker.account,
        positions=broker.positions,
        bars=bars,
        bar=bar,
        price=price,
        cur_price=price,
        cur_date=str(state.get("cur_date", "") or ""),
        cur_time=str(state.get("cur_time", "") or ""),
        params=dict(state.get("params") or {}),
        nl=_nl,
        snapshot_dir=snapshot_dir,
        model_dir=model_dir,
        state_dir=state_dir,
    ), broker


def _load_module(path, name):
    if not path.exists():
        return None
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


main_path = Path(sys.argv[1])
snapshot_dir = os.environ.get("AT_SNAPSHOT_DIR", "/mnt/snapshot")
model_dir = os.environ.get("AT_MODEL_DIR", "/mnt/agent/models")
state_dir = os.environ.get("AT_STATE_DIR", "/mnt/agent/workspace")
main_module = None
main_load_error = None
with contextlib.redirect_stdout(sys.stderr):
    try:
        main_module = _load_module(main_path, "agent_strategy_main")
    except Exception as exc:
        main_load_error = _sanitize_error("%s: %s" % (type(exc).__name__, exc))

main_fn = getattr(main_module, "main", None) if main_module is not None else None

for line in sys.stdin:
    if not line.strip():
        continue
    request = json.loads(line)
    request_id = str(request.get("request_id", ""))
    try:
        if main_load_error is not None:
            raise RuntimeError("main.py failed to import: " + main_load_error)
        if request.get("op") == "validate":
            if not callable(main_fn):
                raise AttributeError("main.py must define main(ctx)")
            response = {"request_id": request_id, "status": "ok"}
        else:
            if not callable(main_fn):
                raise AttributeError("main.py must define main(ctx)")
            ctx, broker = _build_ctx(request.get("state") or {}, snapshot_dir, model_dir, state_dir)
            with contextlib.redirect_stdout(sys.stderr):
                main_fn(ctx)
            response = {"request_id": request_id, "status": "ok", "actions": broker._actions}
    except Exception as exc:
        response = {"request_id": request_id, "status": "error", "error": _sanitize_error("%s: %s" % (type(exc).__name__, exc))}
    print(json.dumps(response, ensure_ascii=False, default=str), file=_PROTOCOL_STDOUT, flush=True)
"""


class MainPolicyRunner:
    """Persistent sandbox process serving per-minute ``main(ctx)`` calls."""

    def __init__(
        self,
        executor,
        paths,
        *,
        timeout_seconds: float,
        decision_time: str,
        replay_granularity: str,
        nl_service=None,
        requests_path: Path | None = None,
        responses_path: Path | None = None,
    ) -> None:
        self.executor = executor
        self.paths = paths
        self.timeout_seconds = timeout_seconds
        self.decision_time = decision_time
        self.replay_granularity = replay_granularity
        self.nl_service = nl_service
        self.requests_path = requests_path
        self.responses_path = responses_path
        self.proc = None
        self._hide_cm = None
        self._hide_entered = False
        self._served: set[str] = set()
        # Continuously drained so the persistent driver never blocks on a full
        # stderr pipe (the driver redirects the Agent's stdout to stderr).
        self._stderr_chunks: deque[str] = deque(maxlen=400)
        self._stderr_thread: threading.Thread | None = None

    def __enter__(self) -> "MainPolicyRunner":
        try:
            main_py = self.paths.agent_output / "main.py"
            env = {
                "AT_SNAPSHOT_DIR": self.executor.map_path(self.paths.snapshot),
                "AT_AGENT_OUTPUT_DIR": self.executor.map_path(self.paths.agent_output),
                "AT_MODEL_DIR": self.executor.map_path(self.paths.model_artifacts),
                "AT_STATE_DIR": self.executor.map_path(self.paths.workspace),
                "AT_DECISION_TIME": self.decision_time,
                "AT_REPLAY_GRANULARITY": self.replay_granularity,
                "AT_FORBIDDEN_PATHS": _executor_pathsep_join(
                    self.executor, [self.paths.train, self.paths.valid, self.paths.test, self.paths.artifacts]
                ),
                "AT_WRITE_FORBIDDEN_PATHS": self.executor.map_path(self.paths.agent_output),
                "AT_DISABLE_LINKS": "1",
            }
            if self.requests_path is not None and self.responses_path is not None:
                env["AT_NL_REQUESTS_PATH"] = self.executor.map_path(self.requests_path)
                env["AT_NL_RESPONSES_PATH"] = self.executor.map_path(self.responses_path)
                env["AT_NL_TOOL_TIMEOUT_SECONDS"] = str(self.timeout_seconds)
            self._hide_cm = hide_snapshot_slots_from_agent(self.paths)
            self._hide_cm.__enter__()
            self._hide_entered = True
            self.proc = self.executor.popen(
                [self.executor.python, "-c", _MAIN_DRIVER, self.executor.map_path(main_py)],
                env=env,
                cwd=self.paths.agent,
                user="agent",
            )
            self._start_stderr_drainer()
        except Exception:
            self.__exit__(*sys.exc_info())
            raise
        return self

    def _start_stderr_drainer(self) -> None:
        proc = self.proc
        if proc is None or proc.stderr is None:
            return

        def _drain() -> None:
            try:
                for line in proc.stderr:  # blocks until each line / EOF on process exit
                    self._stderr_chunks.append(line)
            except Exception:  # noqa: BLE001 - the pipe closes when the process exits
                pass

        self._stderr_thread = threading.Thread(target=_drain, daemon=True)
        self._stderr_thread.start()

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            self.close()
        finally:
            if self._hide_cm is not None and self._hide_entered:
                self._hide_cm.__exit__(exc_type, exc, tb)
                self._hide_entered = False

    def validate_main(self) -> None:
        self._request({"op": "validate"})

    def step(self, state: dict[str, object]) -> list[dict[str, object]]:
        response = self._request({"op": "call", "state": state})
        actions = response.get("actions") or []
        if not isinstance(actions, list):
            raise BacktestError("main(ctx) returned non-list actions")
        return [dict(action) for action in actions if isinstance(action, dict)]

    def close(self) -> None:
        proc = self.proc
        if proc is None:
            return
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except Exception:  # noqa: BLE001 - best effort cleanup
                proc.kill()
                proc.wait()
        # The process is dead, so proc.stderr hits EOF and the daemon drainer
        # finishes; join it before closing the fd so we never race it.
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=1)
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            try:
                if pipe is not None:
                    pipe.close()
            except Exception:  # noqa: BLE001 - best effort cleanup
                pass
        self.proc = None

    def _request(self, payload: dict[str, object]) -> dict[str, object]:
        proc = self.proc
        if proc is None or proc.stdin is None or proc.stdout is None:
            raise BacktestError("main policy runner is not running")
        request_id = uuid.uuid4().hex
        record = {"request_id": request_id, **payload}
        try:
            proc.stdin.write(json.dumps(_jsonable(record), ensure_ascii=False, default=str) + "\n")
            proc.stdin.flush()
        except BrokenPipeError as exc:
            raise BacktestError(f"main policy runner exited early: {self._drain_stderr()}") from exc
        # Inactivity deadline: a single slow nl() (up to its own timeout) must not
        # exhaust the per-minute budget, so serving an NL request resets the clock.
        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            if self._pump_nl():
                deadline = time.monotonic() + self.timeout_seconds
            if proc.poll() is not None:
                raise BacktestError(f"main policy runner failed: {self._drain_stderr()}")
            ready, _, _ = select.select([proc.stdout], [], [], 0.05)
            if not ready:
                continue
            line = proc.stdout.readline()
            if not line:
                continue
            try:
                response = json.loads(line)
            except json.JSONDecodeError:
                continue
            if str(response.get("request_id", "")) != request_id:
                continue
            if response.get("status") != "ok":
                raise BacktestError(str(sanitize_for_log(str(response.get("error", "main(ctx) failed")))))
            return response
        proc.kill()
        raise BacktestError(f"main policy runner timed out after {self.timeout_seconds}s")

    def _pump_nl(self) -> int:
        if self.nl_service is None or self.requests_path is None or self.responses_path is None:
            return 0
        before = len(self._served)
        _serve_nl_requests(self.requests_path, self.responses_path, self._served, self.nl_service)
        return len(self._served) - before

    def _drain_stderr(self) -> str:
        thread = self._stderr_thread
        if thread is not None:
            thread.join(timeout=0.5)  # let the drainer flush the dying process's final stderr
        return str(sanitize_for_log("".join(self._stderr_chunks)))[-2000:]


def run_main_ctx_replay(
    replay_daily: pd.DataFrame,
    profile,
    *,
    decision_time_iso: str,
    shortable_codes: frozenset[str],
    main_policy: MainPolicyRunner,
    replay_intraday_1min: pd.DataFrame | None = None,
    auction_enabled: bool = True,
    auction_decision_time: str = "09:25",
) -> ReplayResult:
    """Replay the region minute by minute, calling ``main(ctx)`` once per minute.

    Every minute the Agent's ``main`` receives a market-level ``ctx`` and may
    open or adjust positions on any ``ts_code`` that trades that minute. The
    final trade date is reserved for mandatory liquidation of remaining
    holdings. Minute bars are used when present; otherwise a daily-synthesized
    09:30/15:00 fallback is generated per code.

    When ``auction_enabled``, each day prepends a pre-open call-auction tick at
    ``auction_decision_time`` (default 09:25, when the open is matched), priced
    at the day's open; entries placed there fill at the open under the Broker's
    daily limit rules and are labelled ``price_label="auction"``.
    """
    market = MarketData(replay_daily)
    if len(market.trade_dates) < 2:
        raise BacktestError("replay region needs at least two trade dates for entry/exit")
    minute_market = (
        MinuteMarketData(replay_intraday_1min)
        if replay_intraday_1min is not None and not replay_intraday_1min.empty
        else None
    )
    granularity = "minute" if minute_market is not None else "daily"
    entry_date, exit_date = market.trade_dates[0], market.trade_dates[-1]
    broker = SimBroker(profile, market, shortable_codes=shortable_codes)
    equity_by_date: dict[str, float] = {}

    for trade_date in market.trade_dates:
        if trade_date != exit_date:
            minute_seed = minute_market.rows_for_date(trade_date) if minute_market is not None else _empty_minute_rows()
            minute_rows = _minute_rows_with_daily_fallback(replay_daily, trade_date, minute_seed)
            if auction_enabled and not minute_rows.empty:
                minute_rows = pd.concat(
                    [_auction_rows(minute_rows, auction_decision_time), minute_rows], ignore_index=True
                ).sort_values(["minute_sort", "ts_code"], kind="stable").reset_index(drop=True)
            for minute_key, minute_group in minute_rows.groupby("minute_key", sort=True):
                state = _market_state(broker, trade_date=trade_date, minute_key=str(minute_key), minute_group=minute_group)
                actions = main_policy.step(state)
                if not actions:
                    continue
                price_label = "auction" if str(minute_key) == auction_decision_time else f"{granularity}:{minute_key}"
                broker.record_event(
                    "main_actions",
                    trade_date=trade_date,
                    minute_key=str(minute_key),
                    action_count=len(actions),
                    actions=_jsonable(actions),
                )
                for action in actions:
                    _execute_main_action(
                        action,
                        minute_group,
                        broker,
                        trade_date=trade_date,
                        minute_key=str(minute_key),
                        price_label=price_label,
                    )

        equity = broker.mark_to_market(trade_date)
        if trade_date == exit_date and broker.positions:
            broker.close_all(trade_date)
            equity = broker.equity()
        equity_by_date[trade_date] = equity

    return ReplayResult(
        equity_curve=pd.Series(equity_by_date).sort_index(),
        broker=broker,
        decision_date=entry_date,
        exit_date=exit_date,
        granularity=granularity,
    )


def _auction_rows(minute_rows: pd.DataFrame, auction_time: str) -> pd.DataFrame:
    """Pre-open call-auction tick: each code's opening price only.

    Built from the day's first bar per code, with high/low set to the open and
    vol/amount cleared (the matched open price is all that is known at 09:25).
    The Broker fills entries placed here at the open under the daily limit rules.
    """
    if minute_rows.empty:
        return _empty_minute_rows()
    first = minute_rows.sort_values("minute_sort", kind="stable").drop_duplicates("ts_code", keep="first").copy()
    for column in ("high", "low", "close"):
        first[column] = first["open"]
    for column in ("vol", "amount"):
        if column in first.columns:
            first[column] = float("nan")
    first["minute_key"] = auction_time
    first["minute_sort"] = _minute_sort(auction_time)
    return first.reset_index(drop=True)


def _market_state(broker: SimBroker, *, trade_date: str, minute_key: str, minute_group: pd.DataFrame) -> dict[str, object]:
    bars = [
        {
            "ts_code": str(row.ts_code),
            "open": _float_or_none(getattr(row, "open", None)),
            "high": _float_or_none(getattr(row, "high", None)),
            "low": _float_or_none(getattr(row, "low", None)),
            "close": _float_or_none(getattr(row, "close", None)),
            "vol": _float_or_none(getattr(row, "vol", None)),
            "amount": _float_or_none(getattr(row, "amount", None)),
        }
        for row in minute_group.itertuples()
    ]
    return {
        "cur_date": str(trade_date),
        "cur_time": str(minute_key or ""),
        "account": _jsonable(broker.get_account()),
        "positions": _jsonable(broker.get_positions()),
        "cash": float(broker.cash),
        "initial_equity": float(broker.initial_equity),
        "bars": bars,
        "params": {},
    }


def _execute_main_action(
    action: dict[str, object],
    minute_group: pd.DataFrame,
    broker: SimBroker,
    *,
    trade_date: str,
    minute_key: str,
    price_label: str,
) -> None:
    name = _ACTION_ALIASES.get(str(action.get("action", "")).lower().strip(), str(action.get("action", "")).lower().strip())
    ts_code = str(action.get("ts_code", "")).strip()
    if name not in _SUPPORTED_ACTIONS or not ts_code:
        broker.record_event(
            "main_action_ignored", trade_date=trade_date, action=_jsonable(action), reason="unsupported_or_missing_ts_code"
        )
        return
    bar = _minute_bar_for_code(minute_group, ts_code)
    if bar is None:
        broker.record_event(
            "main_action_ignored", trade_date=trade_date, ts_code=ts_code, action=_jsonable(action), reason="no_bar_this_minute"
        )
        return
    broker.execute(
        ts_code,
        name,
        trade_date=trade_date,
        raw_price=_bar_execution_price(bar),
        amount=_int_or_none(action.get("amount")),
        weight=_float_or_none(action.get("weight")),
        time=minute_key,
        reason=str(action.get("reason") or name),
        price_label=price_label,
    )


def _int_or_none(value: object) -> int | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _float_or_none(value: object) -> float | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(result) else result
