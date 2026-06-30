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
from collections.abc import Callable
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from autotrade.environment.backtest_engine import (
    _STRATEGY_PATH_GUARD,
    BacktestError,
    MarketData,
    MinuteMarketData,
    ReplayResult,
    _empty_minute_rows,
    _executor_pathsep_join,
    _jsonable,
    _minute_rows_with_daily_fallback,
    _minute_sort,
    _serve_nl_requests,
    hide_snapshot_slots_from_agent,
)
from autotrade.environment.broker import _ACTION_TO_ORDER_TYPE, SimBroker, xtconstant
from autotrade.environment.data.contracts import sim_datetime
from autotrade.environment.runtime import sanitize_for_log
from autotrade.environment.state_staging import StateStager
from autotrade.environment.timeview import Timeview

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


_RESP_STATE = {"offset": 0, "responses": {}}


def _read_responses(path):
    state = _RESP_STATE
    try:
        with open(path, "rb") as handle:
            handle.seek(state["offset"])
            chunk = handle.read()
    except FileNotFoundError:
        return state["responses"]
    head, sep, _partial = chunk.rpartition(b"\\n")
    if sep:
        state["offset"] += len(head) + len(sep)
        for raw in head.splitlines():
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            state["responses"][str(record.get("request_id"))] = record
    return state["responses"]


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
        self._working = state.get("pending") or {}
        self._pos = {}
        for item in self.positions:
            qty = int(item.get("quantity", 0) or 0)
            self._pos[str(item.get("ts_code"))] = qty if str(item.get("side", "long")) == "long" else -qty
        self._actions = []
        self._substeps = []      # [{name, budget_minutes, real_wall_s}] declared this tick
        self._substep_names = set()
        self._cur_substep = None  # name of the open ctx.substep, tagged onto each order
        self._staged = []        # [{staging_rel, state_rel, substep, budget_minutes}] this tick

    @property
    def cash(self):
        return self._cash

    @property
    def money(self):
        return self._cash

    def position(self, ts_code):
        return self._pos.get(str(ts_code), 0)

    def pending(self, ts_code):
        \"\"\"Still-working orders for ``ts_code`` — those queued on earlier ticks and
        not yet filled, plus any submitted this tick. Mirrors the live order query
        so re-entry/exit logic can skip codes with an order already in flight.\"\"\"
        code = str(ts_code)
        working = list(self._working.get(code, []))
        working.extend(action for action in self._actions if str(action.get("ts_code")) == code)
        return working

    def buy(self, ts_code, amount=None, weight=None, limit=None, valid_bars=None, reason=None, **kwargs):
        self._order("buy", ts_code, amount, weight, limit, valid_bars, reason, +1)

    def sell(self, ts_code, amount=None, limit=None, valid_bars=None, reason=None, **kwargs):
        self._order("sell", ts_code, amount, None, limit, valid_bars, reason, -1)

    def short(self, ts_code, amount=None, weight=None, limit=None, valid_bars=None, reason=None, **kwargs):
        self._order("short", ts_code, amount, weight, limit, valid_bars, reason, -1)

    def cover(self, ts_code, amount=None, limit=None, valid_bars=None, reason=None, **kwargs):
        self._order("cover", ts_code, amount, None, limit, valid_bars, reason, +1)

    def close(self, ts_code, reason=None, **kwargs):
        code = str(ts_code)
        self._actions.append({"action": "close", "ts_code": code, "reason": reason, "_substep": self._cur_substep})
        price = self._prices.get(code)
        held = self._pos.get(code, 0)
        if price is not None:
            self._cash += held * price
        self._pos[code] = 0

    def _order(self, action, ts_code, amount, weight, limit, valid_bars, reason, sign):
        code = str(ts_code)
        record = {"action": action, "ts_code": code}
        if amount is not None:
            record["amount"] = amount
        if weight is not None:
            record["weight"] = weight
        if limit is not None:
            record["limit"] = limit
        if valid_bars is not None:
            record["valid_bars"] = valid_bars
        if reason is not None:
            record["reason"] = reason
        record["_substep"] = self._cur_substep
        self._actions.append(record)
        if limit is not None:
            return  # a resting limit order may not fill; leave the optimistic view unchanged
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


class _Ctx(types.SimpleNamespace):
    \"\"\"main(ctx) view. ``state_dir`` is a property so that, inside ctx.substep(),
    it resolves to a hidden staging directory; outside it resolves to the managed,
    visible state directory. Writing via ctx.state_dir therefore stages a heavy
    block's output regardless of the write mechanism (json, parquet, native), and
    the host merges it into the visible directory only once the block's declared
    duration has elapsed (ready_at = tick + B).\"\"\"

    @property
    def state_dir(self):
        holder = self._state_holder
        return holder[\"active\"] or holder[\"visible\"]


def _safe_name(text):
    return re.sub(r\"[^0-9A-Za-z._-]\", \"_\", str(text or \"tick\"))


def _build_ctx(state, snapshot_dir, model_dir, state_dir, staging_root):
    bars = {str(b.get("ts_code", "")): dict(b) for b in (state.get("bars") or [])}
    prices = {code: _bar_price(bar) for code, bar in bars.items()}
    broker = _Broker(state, prices)
    state_holder = {"active": None, "visible": state_dir}
    tick_key = _safe_name(state.get("cur_datetime") or state.get("cur_time") or "tick")

    def price(ts_code):
        return prices.get(str(ts_code))

    def bar(ts_code):
        return bars.get(str(ts_code))

    @contextlib.contextmanager
    def substep(name, budget_minutes=None):
        # Declared compute duration (minutes) for a heavy block. B is the block's
        # real-time ceiling (the host aborts the backtest if real wall-time exceeds B),
        # it is bounded by decision_max_sim_minutes, and it gates when in-block writes
        # to ctx.state_dir become visible (ready_at = tick + B). It does NOT move the
        # order fill bar: orders fill at the normal decision-bar lag regardless of B.
        # A wrapped block MUST declare B > 0; wrapping with 0 is identical to not
        # wrapping, so it is rejected. Leave trivial per-tick code unwrapped.
        try:
            budget = float(budget_minutes) if budget_minutes is not None else 0.0
        except (TypeError, ValueError):
            budget = 0.0
        if budget <= 0:
            raise ValueError(
                "ctx.substep(name, budget_minutes=B) requires B > 0 minutes (the time this "
                "block may take, which is also its real-time ceiling); use a small value such "
                "as 0.5 for light work. Leave trivial per-tick code unwrapped for the default lag."
            )
        _cap = os.environ.get("AT_DECISION_MAX_SIM_MINUTES", "")
        if _cap:
            try:
                _cap_val = float(_cap)
            except ValueError:
                _cap_val = None
            if _cap_val is not None and budget > _cap_val:
                raise ValueError(
                    "ctx.substep budget_minutes=%.4g exceeds the decision_max_sim_minutes cap "
                    "(%.4g); split the work or declare a smaller budget" % (budget, _cap_val)
                )
        step_name = str(name)
        if step_name in broker._substep_names:
            raise ValueError(
                f"ctx.substep name {step_name!r} was already used in this tick; use a unique name "
                "for each decision block so its latency budget maps unambiguously to orders."
            )
        broker._substep_names.add(step_name)
        prev = broker._cur_substep
        prev_active = state_holder["active"]
        # Writes via ctx.state_dir inside the block land here; the host merges them
        # into the visible state dir once ready_at = this tick + budget elapses.
        staging_subdir = os.path.join(staging_root, tick_key, _safe_name(step_name))
        os.makedirs(staging_subdir, exist_ok=True)
        state_holder["active"] = staging_subdir
        broker._cur_substep = step_name
        start = time.monotonic()
        try:
            yield
        finally:
            broker._substeps.append({
                "name": step_name,
                "budget_minutes": budget,
                "real_wall_s": time.monotonic() - start,
            })
            for _root, _dirs, _files in os.walk(staging_subdir):
                for _fn in _files:
                    _abs = os.path.join(_root, _fn)
                    broker._staged.append({
                        "staging_rel": os.path.relpath(_abs, staging_root),
                        "state_rel": os.path.relpath(_abs, staging_subdir),
                        "substep": step_name,
                        "budget_minutes": budget,
                    })
            state_holder["active"] = prev_active
            broker._cur_substep = prev

    return _Ctx(
        broker=broker,
        account=broker.account,
        positions=broker.positions,
        bars=bars,
        bar=bar,
        substep=substep,
        price=price,
        cur_price=price,
        cur_date=str(state.get("cur_date", "") or ""),
        cur_time=str(state.get("cur_time", "") or ""),
        cur_datetime=str(state.get("cur_datetime", "") or ""),
        params=dict(state.get("params") or {}),
        nl=_nl,
        snapshot_dir=snapshot_dir,
        asof_dir=(state.get("asof_dir") or snapshot_dir),
        asof_version=str(state.get("asof_version") or ""),
        model_dir=model_dir,
        _state_holder=state_holder,
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
state_dir = os.environ.get("AT_STATE_DIR", "/mnt/agent/workspace/.state")
staging_root = os.environ.get("AT_STATE_STAGING_DIR", "/mnt/agent/workspace/.state_staging")
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
            ctx, broker = _build_ctx(request.get("state") or {}, snapshot_dir, model_dir, state_dir, staging_root)
            with contextlib.redirect_stdout(sys.stderr):
                main_fn(ctx)
            response = {
                "request_id": request_id,
                "status": "ok",
                "actions": broker._actions,
                "substeps": broker._substeps,
                "staged": broker._staged,
            }
    except Exception as exc:
        response = {"request_id": request_id, "status": "error", "error": _sanitize_error("%s: %s" % (type(exc).__name__, exc))}
    print(json.dumps(response, ensure_ascii=False, default=str), file=_PROTOCOL_STDOUT, flush=True)
"""


@dataclass(frozen=True)
class _TickResult:
    """One ``main(ctx)`` tick: its orders, declared latency sub-steps, and the
    files staged via ctx.state_dir inside a sub-step (host-merged at ready_at)."""

    actions: list[dict[str, object]]
    substeps: list[dict[str, object]]
    staged: list[dict[str, object]]


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
        decision_max_sim_minutes: float | None = None,
    ) -> None:
        self.executor = executor
        self.paths = paths
        self.timeout_seconds = timeout_seconds
        self.decision_time = decision_time
        self.replay_granularity = replay_granularity
        self.decision_max_sim_minutes = decision_max_sim_minutes
        self.nl_service = nl_service
        self.requests_path = requests_path
        self.responses_path = responses_path
        self.proc = None
        # Unique cmdline marker so the in-container driver tree can be reaped on
        # timeout/teardown (killing the host docker exec client does not signal it).
        self._run_marker = "at_driver_" + uuid.uuid4().hex
        self._hide_cm = None
        self._hide_entered = False
        self._served: set[str] = set()
        self._nl_offset = 0
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
                "AT_STATE_DIR": self.executor.map_path(self.paths.workspace / ".state"),
                "AT_STATE_STAGING_DIR": self.executor.map_path(self.paths.workspace / ".state_staging"),
                "AT_DECISION_TIME": self.decision_time,
                "AT_REPLAY_GRANULARITY": self.replay_granularity,
                "AT_FORBIDDEN_PATHS": _executor_pathsep_join(
                    self.executor, [self.paths.train, self.paths.valid, self.paths.test, self.paths.artifacts]
                ),
                "AT_WRITE_FORBIDDEN_PATHS": self.executor.map_path(self.paths.agent_output),
                "AT_DISABLE_LINKS": "1",
            }
            if self.decision_max_sim_minutes is not None:
                env["AT_DECISION_MAX_SIM_MINUTES"] = str(self.decision_max_sim_minutes)
            if self.requests_path is not None and self.responses_path is not None:
                env["AT_NL_REQUESTS_PATH"] = self.executor.map_path(self.requests_path)
                env["AT_NL_RESPONSES_PATH"] = self.executor.map_path(self.responses_path)
                env["AT_NL_TOOL_TIMEOUT_SECONDS"] = str(self.timeout_seconds)
            self._hide_cm = hide_snapshot_slots_from_agent(self.paths)
            self._hide_cm.__enter__()
            self._hide_entered = True
            self.proc = self.executor.popen(
                [self.executor.python, "-c", _MAIN_DRIVER, self.executor.map_path(main_py), self._run_marker],
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

    def step(self, state: dict[str, object]) -> "_TickResult":
        response = self._request({"op": "call", "state": state})
        actions = response.get("actions") or []
        if not isinstance(actions, list):
            raise BacktestError("main(ctx) returned non-list actions")
        substeps = response.get("substeps") or []
        staged = response.get("staged") or []
        return _TickResult(
            actions=[dict(action) for action in actions if isinstance(action, dict)],
            substeps=[dict(s) for s in substeps if isinstance(s, dict)],
            staged=[dict(s) for s in staged if isinstance(s, dict)],
        )

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
            # Killing the host client may leave the in-container driver alive; reap it.
            self.executor.kill_marker(self._run_marker)
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
        # Hard per-decision wall cap: the whole main(ctx) tick — its compute AND any
        # nl() calls it makes — must finish within timeout_seconds, else the decision is
        # killed immediately and the backtest fails. No inactivity reset: a decision that
        # leans on slow/serial NL is what the cap is meant to catch.
        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            self._pump_nl()
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
        self.executor.kill_marker(self._run_marker)
        raise BacktestError(f"main(ctx) decision exceeded its {self.timeout_seconds:.0f}s wall-clock cap")

    def _pump_nl(self) -> int:
        if self.nl_service is None or self.requests_path is None or self.responses_path is None:
            return 0
        before = len(self._served)
        self._nl_offset = _serve_nl_requests(
            self.requests_path, self.responses_path, self._served, self.nl_service, self._nl_offset
        )
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
    shortable_codes: frozenset[str],
    shortable_by_date: dict[str, frozenset[str]] | None = None,
    main_policy: MainPolicyRunner,
    replay_intraday_1min: pd.DataFrame | None = None,
    auction_enabled: bool = True,
    auction_preopen_time: str | None = "09:15",
    auction_decision_time: str = "09:25",
    auction_close_time: str | None = None,
    execution_lag_bars: int = 2,
    offsession_tick_minutes: int = 15,
    max_seconds_per_trading_day: float | None = None,
    timeview_enabled: bool = False,
    snapshot_dir: Path | None = None,
    replay_dir: Path | None = None,
    on_progress: "Callable[[str, int, int, float, int], None] | None" = None,
) -> ReplayResult:
    """Replay the region tick by tick, calling ``main(ctx)`` per tick.

    Market and FIX_PRICE limit orders go through the Broker's day order book:
    the Agent decides on the bar it can see, then each order reaches a LATER bar
    for matching. ``execution_lag_bars`` (default 2) sets the gap from the
    decision bar to the activation bar — 1 = the immediate next bar, 2 = one bar
    of submit latency then matching on the following bar — which removes
    within-bar look-ahead. A sub-step's declared budget no longer moves the fill bar
    (orders fill at this lag regardless of ``budget_minutes``). A decision with no bar
    ``execution_lag_bars`` ahead (near the close) cannot fill and is recorded
    ``main_actions_unfilled``. The final trade date is reserved for mandatory
    liquidation; minute bars drive the replay when present, else a daily 09:30/15:00
    fallback is synthesized.

    The tick grid spans the whole day on the same ``main(ctx)`` entry, so the loop
    drives both backtest and live. ``auction_enabled`` leads each day with a 09:15
    info tick (no price, fills at the 09:30 opening auction) and a 09:25 tick (on the
    matched open, fills at the first continuous bar); ``auction_close_time`` (e.g.
    14:57) makes that bar's decision fill at the day's final bar (the 15:00 close
    auction). These batch-auction fills are labelled ``price_label="auction"``.
    ``offsession_tick_minutes`` (0 = off) adds a research-only tick grid outside the
    session: pre-open off-session orders fill at the opening auction, post-close ones
    do not fill. ``ctx.broker.pending(ts_code)`` exposes still-working orders so the
    Agent can avoid re-submitting before a fill (parity with the live order query).
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
    broker = SimBroker(profile, market, shortable_codes=shortable_codes, shortable_by_date=shortable_by_date)
    timeview = (
        Timeview(
            host_dir=main_policy.paths.workspace / ".asof",
            executor=main_policy.executor,
            snapshot_dir=snapshot_dir,
            replay_frames=_timeview_replay_frames(replay_dir, replay_daily, replay_intraday_1min),
        )
        if timeview_enabled and snapshot_dir is not None and getattr(main_policy, "paths", None) is not None
        else None
    )
    # Managed ctx.state_dir: sub-step writes stage to a hidden dir and merge into the
    # visible dir at ready_at = tick + B. Reset per backtest for reproducibility.
    stager = (
        StateStager(
            visible_dir=main_policy.paths.workspace / ".state",
            staging_dir=main_policy.paths.workspace / ".state_staging",
        )
        if getattr(main_policy, "paths", None) is not None
        else None
    )
    equity_by_date: dict[str, float] = {}
    replay_start = time.monotonic()  # wall-clock start, reported as replay_wall_seconds
    substep_runtime: dict[str, dict[str, float]] = {}
    # Per-phase wall-time so the 24h replay's added cost is visible (W9): the agent
    # main(ctx) step, the Timeview rebuilds, the state-staging merges, and the Broker
    # matching. The NL-service share of step wall is split out from strategy compute.
    phase_wall = {"strategy_step": 0.0, "timeview_build": 0.0, "state_merge": 0.0, "broker_match": 0.0}
    tick_counts = {"total": 0, "intraday": 0, "offsession": 0}
    total_days = len(market.trade_dates)
    last_progress_idx = 0
    last_progress_time = replay_start

    for day_idx, trade_date in enumerate(market.trade_dates):
        day_compute_wall = 0.0  # cumulative real main(ctx) wall-time for this trade day
        now = time.monotonic()
        # Throttled heartbeat so a long replay is auditable: at most every N days or M seconds.
        if on_progress is not None and (
            day_idx - last_progress_idx >= 30 or now - last_progress_time >= 30.0
        ):
            on_progress(str(trade_date), day_idx, total_days, now - replay_start, len(broker.query_stock_orders()))
            last_progress_idx, last_progress_time = day_idx, now
        if trade_date != exit_date:
            minute_seed = minute_market.rows_for_date(trade_date) if minute_market is not None else _empty_minute_rows()
            minute_rows = _minute_rows_with_daily_fallback(replay_daily, trade_date, minute_seed)
            if not minute_rows.empty:
                plan = _day_tick_plan(
                    minute_rows, auction_enabled, auction_preopen_time, auction_decision_time,
                    execution_lag_bars, offsession_tick_minutes=offsession_tick_minutes,
                    close_auction_time=auction_close_time,
                )
                real_index = {tick.minute_key: i for i, tick in enumerate(t for t in plan if t.is_real)}
                n_real = len(real_index)
                # Decisions wait out the submit lag, then enter the Broker's order book
                # (order_stock) at their activation bar. The sub-step budget no longer
                # shifts that bar: orders fill at the decision's activation bar.
                incoming: dict[int, list[tuple[dict[str, object], bool]]] = {}
                for tick in plan:
                    tick_counts["total"] += 1
                    tick_counts["offsession" if tick.is_offsession else "intraday"] += 1
                    if tick.is_real:
                        _match_t0 = time.monotonic()
                        index = real_index[tick.minute_key]
                        for action, is_auction in incoming.pop(index, []):
                            if not _submit_order(broker, action, is_auction):
                                broker.record_event(
                                    "main_action_ignored", trade_date=trade_date,
                                    action=_jsonable(action), reason="unsupported_or_missing_ts_code",
                                )
                        broker.match_bar(trade_date, tick.minute_key, tick.group, granularity)
                        phase_wall["broker_match"] += time.monotonic() - _match_t0
                    when = sim_datetime(trade_date, tick.minute_key)
                    if timeview is not None:
                        _tv_t0 = time.monotonic()
                        asof_dir, asof_version = timeview.refresh(when)
                        phase_wall["timeview_build"] += time.monotonic() - _tv_t0
                    else:
                        asof_dir, asof_version = None, None
                    if timeview is not None and main_policy.nl_service is not None:
                        main_policy.nl_service.current_when = when  # roll ctx.nl() text on the same clock
                    if stager is not None:
                        _merge_t0 = time.monotonic()
                        stager.merge_ready(when)  # surface staged writes whose ready_at has arrived
                        phase_wall["state_merge"] += time.monotonic() - _merge_t0
                    state = _market_state(
                        broker,
                        trade_date=trade_date,
                        minute_key=tick.minute_key,
                        minute_group=tick.group,
                        asof_dir=asof_dir,
                        asof_version=asof_version,
                        pending=_pending_view(broker, incoming),
                        cur_datetime=when.isoformat(),
                    )
                    # A single decision (one main(ctx) tick) over the per-decision real
                    # wall cap is killed inside MainPolicyRunner.step. Here we accumulate
                    # the day's compute and fail-fast when it exceeds the per-day budget
                    # (scales with replay length, unlike a fixed total cap).
                    _tick_t0 = time.monotonic()
                    actions, substeps, staged = _normalize_tick(main_policy.step(state))
                    _tick_wall = time.monotonic() - _tick_t0
                    day_compute_wall += _tick_wall
                    phase_wall["strategy_step"] += _tick_wall
                    if stager is not None and staged:
                        stager.register(staged, when=when)
                    if max_seconds_per_trading_day is not None and day_compute_wall > max_seconds_per_trading_day:
                        raise BacktestError(
                            f"trade day {trade_date} exceeded its compute budget "
                            f"({max_seconds_per_trading_day:.0f}s) at {tick.minute_key}; cache heavy "
                            "recompute and bound rebalance/graph cost"
                        )
                    # Fail-fast: ctx.substep enforces a positive declared budget, so real
                    # wall-time over the claimed B invalidates the rest of the replay
                    # (under-declaring is non-exploitable). Unwrapped orders carry no
                    # sub-step and fill at the default lag with no per-block ceiling.
                    for sub in substeps:
                        budget_min = float(sub.get("budget_minutes", 0.0) or 0.0)
                        if float(sub.get("real_wall_s", 0.0) or 0.0) > budget_min * 60.0:
                            raise BacktestError(
                                f"sub-step {str(sub.get('name'))!r} at {trade_date} {tick.minute_key} exceeded its "
                                f"declared budget: real {float(sub.get('real_wall_s', 0.0)):.1f}s > {budget_min:.1f}min"
                            )
                        agg = substep_runtime.setdefault(
                            str(sub.get("name")),
                            {"count": 0.0, "total_real_wall_s": 0.0, "max_real_wall_s": 0.0, "budget_minutes": budget_min},
                        )
                        real_wall = float(sub.get("real_wall_s", 0.0) or 0.0)
                        agg["count"] += 1
                        agg["total_real_wall_s"] += real_wall
                        agg["max_real_wall_s"] = max(agg["max_real_wall_s"], real_wall)
                        agg["budget_minutes"] = budget_min
                    placed = 0
                    for action in actions:
                        # _substep is an internal routing tag; drop it so the recorded
                        # order stays clean (its budget is enforced above, not here).
                        action.pop("_substep", None)
                        fill_index = tick.activate_index
                        if fill_index is None or fill_index >= n_real:
                            broker.record_event(
                                "main_actions_unfilled", trade_date=trade_date, minute_key=tick.minute_key,
                                action=_jsonable(action), reason="no_fill_bar_ahead",
                            )
                            continue
                        incoming.setdefault(fill_index, []).append((action, tick.is_auction))
                        placed += 1
                    if placed:
                        broker.record_event(
                            "main_actions", trade_date=trade_date, minute_key=tick.minute_key,
                            action_count=placed, actions=_jsonable(actions),
                        )
                for order in broker.query_stock_orders(cancelable_only=True):  # day order auto-voids at the close
                    broker.cancel_order_stock(
                        str(order["order_id"]), reason="day_end_unfilled", trade_date=trade_date
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
        substep_runtime=substep_runtime or None,
        replay_wall_seconds=time.monotonic() - replay_start,
        replayed_trade_days=total_days,
        total_ticks=tick_counts["total"],
        intraday_ticks=tick_counts["intraday"],
        offsession_ticks=tick_counts["offsession"],
        state_staging_audit=(stager.audit() if stager is not None else None),
        phase_seconds=_phase_seconds(
            phase_wall, getattr(getattr(main_policy, "nl_service", None), "nl_wall_seconds", 0.0)
        ),
    )


@dataclass(frozen=True)
class _Tick:
    """One decision tick and the bar its orders fill at under next-bar execution."""

    minute_key: str
    group: pd.DataFrame
    activate_index: int | None
    is_real: bool
    is_auction: bool
    is_offsession: bool = False


def _offsession_keys(start_min: int, end_min: int, step_minutes: int) -> list[str]:
    """``HH:MM`` keys at ``step_minutes`` spacing over ``[start_min, end_min)``;
    empty when off-session ticks are disabled (``step_minutes <= 0``)."""
    if step_minutes <= 0:
        return []
    keys: list[str] = []
    minute = max(0, int(start_min))
    while minute < end_min:
        keys.append(f"{minute // 60:02d}:{minute % 60:02d}")
        minute += step_minutes
    return keys


def _day_tick_plan(
    minute_rows: pd.DataFrame,
    auction_enabled: bool,
    preopen_time: str | None,
    decision_time: str,
    execution_lag_bars: int,
    *,
    offsession_tick_minutes: int = 15,
    close_auction_time: str | None = None,
) -> list[_Tick]:
    """Ordered decision ticks for one day, each tagged with the real-bar index its
    orders reach the book at (``activate_index``).

    A decision on real bar *i* activates at *i + execution_lag_bars*; a bar with no
    such later bar (near the close) yields ``activate_index=None``. With
    ``auction_enabled`` two pre-open ticks lead the day: a ``preopen_time`` (09:15)
    info tick with no bars (``ctx.price`` is None) activating at the first real bar
    (the 09:30 opening auction), and a ``decision_time`` (09:25) tick on the matched
    open activating at the first continuous bar (09:31). ``close_auction_time`` (e.g.
    14:57) makes that bar's decision activate at the day's final bar (the 15:00 close
    auction) instead of the default lag. ``offsession_tick_minutes`` (0 = off) adds a
    research-only grid outside the session: pre-open off-session ticks activate at the
    opening auction (no price), post-close ones never fill. Pre-open and close-auction
    activations are fixed and independent of ``execution_lag_bars``.
    """
    real_keys = sorted({str(key) for key in minute_rows["minute_key"]}, key=_minute_sort)
    if not real_keys:
        return []
    groups = {str(key): group for key, group in minute_rows.groupby(minute_rows["minute_key"].astype(str), sort=False)}
    n = len(real_keys)
    plan: list[_Tick] = []
    # Off-session grid frame: the session starts at the earliest pre-open tick and
    # ends at the last real bar; ticks outside [open, close] are research-only.
    session_open = preopen_time if (auction_enabled and preopen_time) else (decision_time if auction_enabled else real_keys[0])
    open_min, close_min = _minute_sort(session_open), _minute_sort(real_keys[-1])
    # Pre-open off-session ticks: no price, orders fill at the first real bar (the
    # opening auction when auctions are on, else a plain market-on-open).
    for key in _offsession_keys(0, open_min, offsession_tick_minutes):
        plan.append(_Tick(key, _empty_minute_rows(), 0, False, auction_enabled, True))
    if auction_enabled:
        first = minute_rows.sort_values("minute_sort", kind="stable").drop_duplicates("ts_code", keep="first").copy()
        open_group = first.assign(high=first["open"], low=first["open"], close=first["open"])
        for column in ("vol", "amount"):
            if column in open_group.columns:
                open_group[column] = float("nan")  # intraday volume is unknown pre-open
        if preopen_time:
            plan.append(_Tick(preopen_time, _empty_minute_rows(), 0, False, True))
        plan.append(_Tick(decision_time, open_group, min(1, n - 1), False, True))
    # Clamp the lag to the day's bar count so short/daily-fallback days (e.g. the
    # 09:30+15:00 synthesis) still trade even with auctions disabled: >=1 preserves
    # next-bar execution; <=n-1 lets the first decision reach the last bar.
    lag = max(1, min(execution_lag_bars, n - 1))
    for index, key in enumerate(real_keys):
        if close_auction_time and key == str(close_auction_time) and index < n - 1:
            # Close call auction: this bar's decision fills at the day's final bar.
            plan.append(_Tick(key, groups[key], n - 1, True, True))
        else:
            activate_index = index + lag
            plan.append(_Tick(key, groups[key], activate_index if activate_index < n else None, True, False))
    # Post-close off-session ticks: research/state only, orders never fill.
    after_start = ((close_min // offsession_tick_minutes) + 1) * offsession_tick_minutes if offsession_tick_minutes > 0 else 0
    for key in _offsession_keys(after_start, 24 * 60, offsession_tick_minutes):
        plan.append(_Tick(key, _empty_minute_rows(), None, False, False, True))
    return plan


def _phase_seconds(phase_wall: dict[str, float], nl_wall: float) -> dict[str, float]:
    """Per-phase replay wall-time. The NL-service share of the agent step is split
    out of strategy compute so the four host phases plus the LLM service sum to the
    replay's active work."""
    nl = float(nl_wall or 0.0)
    return {
        "strategy_compute": round(max(0.0, phase_wall["strategy_step"] - nl), 3),
        "nl_service": round(nl, 3),
        "timeview_build": round(phase_wall["timeview_build"], 3),
        "state_merge": round(phase_wall["state_merge"], 3),
        "broker_match": round(phase_wall["broker_match"], 3),
    }


def _normalize_tick(result: object) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]]]:
    """A ``MainPolicyRunner`` step returns a ``_TickResult``; test fakes return a
    plain action list (no sub-steps or staged writes = current next-bar behavior)."""
    if isinstance(result, _TickResult):
        return result.actions, result.substeps, result.staged
    actions = [dict(a) for a in (result or []) if isinstance(a, dict)]
    return actions, [], []


def _submit_order(broker: SimBroker, action: dict[str, object], is_auction: bool) -> bool:
    """Translate a ``main()`` action into a Broker ``order_stock`` submission.

    ``limit`` (a fixed price) routes to a ``FIX_PRICE`` order resting ``valid_bars``
    bars (default 1); otherwise a ``MARKET_PEER_PRICE_FIRST`` order valid that bar.
    ``close`` is always a market exit. Returns False if the action is unsupported."""
    name = _ACTION_ALIASES.get(str(action.get("action", "")).lower().strip(), str(action.get("action", "")).lower().strip())
    ts_code = str(action.get("ts_code", "")).strip()
    if name not in _SUPPORTED_ACTIONS or not ts_code:
        return False
    limit = _float_or_none(action.get("limit")) if name != "close" else None
    if limit is not None and limit <= 0:
        limit = None
    order_type = "CLOSE_POSITION" if name == "close" else _ACTION_TO_ORDER_TYPE[name]
    price_type = xtconstant.FIX_PRICE if limit is not None else xtconstant.MARKET_PEER_PRICE_FIRST
    valid_bars = max(1, _int_or_none(action.get("valid_bars")) or 1) if limit is not None else 1
    broker.order_stock(
        order_type,
        ts_code,
        _int_or_none(action.get("amount")),
        price_type,
        limit or 0,
        weight=_float_or_none(action.get("weight")),
        valid_bars=valid_bars,
        is_auction=is_auction,
        reason=str(action.get("reason") or name),
    )
    return True


def _pending_view(broker: SimBroker, incoming: dict[int, list[tuple[dict[str, object], bool]]]) -> dict[str, list[dict[str, object]]]:
    """Working orders the Agent can see via ``ctx.broker.pending(ts_code)``: the
    Broker's cancelable book plus decisions still inside the submit lag, so de-dup
    holds across the whole decision-to-fill window (mirrors ``query_stock_orders``)."""
    records = list(broker.query_stock_orders(cancelable_only=True))
    for items in incoming.values():
        for action, _is_auction in items:
            code = str(action.get("ts_code", ""))
            if code:
                records.append(
                    {"ts_code": code, "action": str(action.get("action", "")), "order_volume": action.get("amount"),
                     "weight": action.get("weight"), "price": action.get("limit"), "status": "pending"}
                )
    grouped: dict[str, list[dict[str, object]]] = {}
    for record in records:
        grouped.setdefault(str(record.get("ts_code", "")), []).append(record)
    return grouped


def _timeview_replay_frames(
    replay_dir: Path | None,
    replay_daily: pd.DataFrame,
    replay_intraday_1min: pd.DataFrame | None,
) -> dict[str, pd.DataFrame]:
    """Replay-slot frames the Timeview rolls in. daily/intraday reuse the frames
    already loaded for the replay; events/macro/fundamentals are read from the
    slot directory when present (each carries a row-level ``available_at``)."""
    frames: dict[str, pd.DataFrame] = {"daily": replay_daily}
    if replay_intraday_1min is not None:
        frames["intraday_1min"] = replay_intraday_1min
    if replay_dir is not None:
        for name in ("events", "macro", "fundamentals"):
            path = Path(replay_dir) / f"{name}.parquet"
            if path.exists():
                frames[name] = pd.read_parquet(path)
    return frames


def _market_state(
    broker: SimBroker,
    *,
    trade_date: str,
    minute_key: str,
    minute_group: pd.DataFrame,
    asof_dir: str | None = None,
    asof_version: str | None = None,
    pending: dict[str, list[dict[str, object]]] | None = None,
    cur_datetime: str = "",
) -> dict[str, object]:
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
        "cur_datetime": str(cur_datetime or ""),
        "account": _jsonable(broker.query_stock_asset()),
        "positions": _jsonable(broker.query_stock_positions()),
        "cash": float(broker.cash),
        "initial_equity": float(broker.initial_equity),
        "bars": bars,
        "asof_dir": asof_dir,
        "asof_version": asof_version,
        "pending": pending or {},
        "params": {},
    }


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
