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
from datetime import datetime, timedelta
from collections.abc import Callable
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from autotrade.environment.backtest_engine import (
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
from autotrade.environment.broker import SimBroker, optype, prtype
from autotrade.environment.data.contracts import sim_datetime
from autotrade.environment.runtime import sanitize_for_log
from autotrade.environment.state_staging import StateStager
from autotrade.environment.timeview import Timeview

_ACTION_ALIASES = {
    "long": "buy",
    "sell_short": "short",
    "close_long": "sell",
    "close_short": "cover",
    "exit": "close",
    "margin_buy": "fin_buy",
    "cancel_order": "cancel",
}
_ORDER_ACTIONS = {
    "buy", "sell", "credit_buy", "credit_sell", "short", "cover", "close",
    "fin_buy", "sell_repay", "direct_repay", "transfer",
}
_SUPPORTED_ACTIONS = _ORDER_ACTIONS | {"cancel"}


# The persistent per-tick driver is a real module (main_ctx_driver.py) shipped into
# the sandbox image; it is launched by file (executor.runtime_path) rather than as a
# -c string so it stays typed and testable. It is standard-library only.
_DRIVER_PATH = Path(__file__).with_name("main_ctx_driver.py")


@dataclass(frozen=True)
class _TickResult:
    """One ``main(ctx)`` tick: its orders, declared latency sub-steps, and the
    files staged via ctx.state_dir inside a sub-step (host-merged at ready_at)."""

    actions: list[dict[str, object]]
    substeps: list[dict[str, object]]
    staged: list[dict[str, object]]
    main_wall_s: float | None = None


@dataclass(frozen=True)
class _DelayedAction:
    seq: int
    ready_at: datetime
    action: dict[str, object]
    substep: str
    generated_at: str


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
                "AT_WRITE_FORBIDDEN_PATHS": _executor_pathsep_join(
                    self.executor, [self.paths.agent_output, self.paths.model_artifacts]
                ),
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
                [
                    self.executor.python,
                    self.executor.runtime_path(_DRIVER_PATH),
                    self.executor.map_path(main_py),
                    self._run_marker,
                ],
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
        main_wall_raw = response.get("main_wall_s")
        try:
            main_wall_s = float(main_wall_raw) if main_wall_raw is not None else None
        except (TypeError, ValueError):
            main_wall_s = None
        return _TickResult(
            actions=[dict(action) for action in actions if isinstance(action, dict)],
            substeps=[dict(s) for s in substeps if isinstance(s, dict)],
            staged=[dict(s) for s in staged if isinstance(s, dict)],
            main_wall_s=main_wall_s,
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
    auction_close_time: str | None = "14:57",
    execution_lag_bars: int = 2,
    offsession_tick_minutes: int = 15,
    max_seconds_per_trading_day: float | None = None,
    enforce_substep_timeout: bool = True,
    enforce_substep_coverage: bool = True,
    max_untracked_substep_wall_s: float = 0.05,
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
    within-bar look-ahead. Broker actions issued inside ``ctx.substep`` with a
    sub-minute budget are treated as submitted in the current decision minute while
    retaining ``ready_at`` metadata for audit; actions with ``B>=1`` are first held
    until the block's ``ready_at`` and submitted on the first later orderable tick.
    From that submit tick they use the same ``execution_lag_bars`` mapping. A
    decision with no bar ``execution_lag_bars`` ahead (near the close) cannot fill
    and is recorded ``main_actions_unfilled``. The final trade date is reserved for
    mandatory liquidation; minute bars drive the replay when present, else a daily
    09:30/15:00 fallback is synthesized.

    The tick grid spans the whole day on the same ``main(ctx)`` entry, so the loop
    drives both backtest and live. ``auction_enabled`` leads each day with a 09:15
    info tick (no price, fills at the 09:30 opening auction) and a 09:25 tick (on the
    matched open, fills at the first continuous bar); ``auction_close_time`` (e.g.
    14:57) makes that bar's decision fill at the day's final bar (the 15:00 close
    auction). These batch-auction fills are labelled ``price_label="auction"``.
    ``offsession_tick_minutes`` (0 = off) adds a research-only tick grid outside the
    session; off-session orders do not fill. ``ctx.broker.pending(ts_code)`` exposes
    still-working orders so the Agent can avoid re-submitting before a fill (parity
    with the live order query).

    ``enforce_substep_timeout`` (default True) keeps the per-substep wall fail-fast
    that aborts the replay when a declared ``ctx.substep`` block runs over its budget
    B; the final/frozen (held-out) eval passes False so a reproducible eval of an
    already-accepted strategy is not aborted by transient load. The per-substep
    runtime statistics are aggregated either way; only the raise is skipped.
    ``enforce_substep_coverage`` rejects substantive Python-side ``main(ctx)`` time
    outside declared substeps (with a small overhead grace), so heavy unwrapped work
    cannot hide in the tick's aggregate wall time.
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
    delayed_actions: list[_DelayedAction] = []
    delayed_seq = 0
    total_days = len(market.trade_dates)
    last_progress_idx = 0
    last_progress_time = replay_start

    for day_idx, trade_date in enumerate(market.trade_dates):
        # Roll the sim-date before the day's first tick so overnight holds report
        # their full sellable_quantity (T+1 unlocked) from the first off-session tick,
        # not only after the day's first fill. execute()/mark_to_market() keep their
        # own _advance_date as an idempotent safety net.
        broker.roll_to_date(trade_date)
        day_compute_wall = 0.0  # cumulative real main(ctx) wall-time for this trade day
        now = time.monotonic()
        # Throttled heartbeat so a long replay is auditable: at most every N days or M seconds.
        if on_progress is not None and (
            day_idx - last_progress_idx >= 30 or now - last_progress_time >= 30.0
        ):
            on_progress(str(trade_date), day_idx, total_days, now - replay_start, len(broker.orders))
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
                # (passorder) at their activation bar. Substep-wrapped broker actions
                # are first delayed until ready_at, then treated as submitted at the
                # ready/orderable tick and mapped through this same activation logic.
                incoming: dict[int, list[tuple[dict[str, object], bool, bool]]] = {}
                for tick in plan:
                    tick_counts["total"] += 1
                    tick_counts["offsession" if tick.is_offsession else "intraday"] += 1
                    if tick.is_real:
                        _match_t0 = time.monotonic()
                        index = real_index[tick.minute_key]
                        for action, is_auction, is_close_auction in incoming.pop(index, []):
                            if not _submit_order(broker, action, is_auction, is_close_auction):
                                broker.record_event(
                                    "main_action_ignored", trade_date=trade_date,
                                    action=_jsonable(action), reason="unsupported_or_missing_ts_code",
                                )
                        broker.match_bar(trade_date, tick.minute_key, tick.group, granularity)
                        if index == n_real - 1:
                            _cancel_day_end_orders(broker, trade_date=trade_date, minute_key=tick.minute_key)
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
                    released_actions = _release_delayed_actions(
                        delayed_actions,
                        broker=broker,
                        incoming=incoming,
                        tick=tick,
                        trade_date=trade_date,
                        when=when,
                        n_real=n_real,
                    )
                    if released_actions:
                        broker.record_event(
                            "main_actions",
                            trade_date=trade_date,
                            minute_key=tick.minute_key,
                            action_count=len(released_actions),
                            actions=_jsonable(released_actions),
                            delayed_from_substep=True,
                        )
                    state = _market_state(
                        broker,
                        trade_date=trade_date,
                        minute_key=tick.minute_key,
                        minute_group=tick.group,
                        asof_dir=asof_dir,
                        asof_version=asof_version,
                        pending=_pending_view(broker, incoming, delayed_actions=delayed_actions, now=when),
                        cur_datetime=when.isoformat(),
                    )
                    # A single decision (one main(ctx) tick) over the per-decision real
                    # wall cap is killed inside MainPolicyRunner.step. Here we accumulate
                    # the day's compute and fail-fast when it exceeds the per-day budget
                    # (scales with replay length, unlike a fixed total cap).
                    _tick_t0 = time.monotonic()
                    actions, substeps, staged, main_wall_s = _normalize_tick(main_policy.step(state))
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
                        real_wall = float(sub.get("real_wall_s", 0.0) or 0.0)
                        # Final/frozen (held-out) eval skips the raise so a transient
                        # overrun under load cannot abort an already-accepted strategy's
                        # reproducible eval; the runtime is still aggregated below.
                        if enforce_substep_timeout and real_wall > budget_min * 60.0:
                            raise BacktestError(
                                f"sub-step {str(sub.get('name'))!r} at {trade_date} {tick.minute_key} exceeded its "
                                f"declared budget: real {real_wall:.1f}s > {budget_min:.1f}min"
                            )
                        agg = substep_runtime.setdefault(
                            str(sub.get("name")),
                            {"count": 0.0, "total_real_wall_s": 0.0, "max_real_wall_s": 0.0, "budget_minutes": budget_min},
                        )
                        agg["count"] += 1
                        agg["total_real_wall_s"] += real_wall
                        agg["max_real_wall_s"] = max(agg["max_real_wall_s"], real_wall)
                        agg["budget_minutes"] = budget_min
                    strategy_wall = float(main_wall_s if main_wall_s is not None else _tick_wall)
                    # Only top-level substeps count toward coverage: a nested substep's
                    # wall-time is already inside its parent's real_wall_s, so summing all
                    # of them would over-count and let genuine unwrapped compute hide.
                    covered_wall = sum(
                        float(sub.get("real_wall_s", 0.0) or 0.0)
                        for sub in substeps
                        if not sub.get("nested")
                    )
                    untracked_wall = max(0.0, strategy_wall - covered_wall)
                    if enforce_substep_coverage and untracked_wall > max_untracked_substep_wall_s:
                        raise BacktestError(
                            f"main(ctx) at {trade_date} {tick.minute_key} spent {untracked_wall:.3f}s outside "
                            "ctx.substep; wrap every strategy step in ctx.substep(name, budget_minutes=B)"
                        )
                    substep_budgets = {
                        str(sub.get("name")): float(sub.get("budget_minutes", 0.0) or 0.0)
                        for sub in substeps
                    }
                    immediate_actions: list[dict[str, object]] = []
                    for action in actions:
                        substep_name = action.get("_substep")
                        if substep_name is None:
                            immediate_actions.append(action)
                            continue
                        substep_key = str(substep_name)
                        if substep_key not in substep_budgets:
                            raise BacktestError(
                                f"broker action referenced unknown ctx.substep {substep_key!r}"
                            )
                        budget_minutes = substep_budgets[substep_key]
                        ready_at = when + timedelta(minutes=budget_minutes)
                        delayed_action = dict(action)
                        delayed_action.pop("_substep", None)
                        delayed_action.setdefault("decision_at", delayed_action.get("submitted_at", ""))
                        delayed_action.setdefault("decision_time", delayed_action.get("submitted_time", ""))
                        if 0.0 < budget_minutes < 1.0:
                            # Sub-minute work completes inside the current decision
                            # minute. Treat it as submitted on this tick while still
                            # auditing the substep and preserving ready_at metadata.
                            delayed_action["substep"] = substep_key
                            delayed_action["substep_generated_at"] = when.isoformat()
                            delayed_action["substep_ready_at"] = ready_at.isoformat()
                            immediate_actions.append(delayed_action)
                            continue
                        delayed_actions.append(
                            _DelayedAction(
                                seq=delayed_seq,
                                ready_at=ready_at,
                                action=delayed_action,
                                substep=substep_key,
                                generated_at=when.isoformat(),
                            )
                        )
                        delayed_seq += 1
                    placed_actions = _place_actions_at_tick(
                        immediate_actions,
                        broker=broker,
                        incoming=incoming,
                        delayed_actions=delayed_actions,
                        tick=tick,
                        trade_date=trade_date,
                        n_real=n_real,
                    )
                    if placed_actions:
                        broker.record_event(
                            "main_actions", trade_date=trade_date, minute_key=tick.minute_key,
                            action_count=len(placed_actions), actions=_jsonable(placed_actions),
                        )
                _cancel_day_end_orders(broker, trade_date=trade_date)

        equity = broker.mark_to_market(trade_date)
        if trade_date == exit_date and any(state.positions for state in broker.accounts.values()):
            broker.close_all(trade_date)
            equity = broker.equity()
        equity_by_date[trade_date] = equity

    for delayed in delayed_actions:
        broker.record_event(
            "main_actions_unfilled",
            trade_date=market.trade_dates[-1],
            minute_key="",
            action=_jsonable(delayed.action),
            reason="substep_delayed_action_not_released",
            substep=delayed.substep,
            generated_at=delayed.generated_at,
            ready_at=delayed.ready_at.isoformat(),
        )

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
    # A close (15:00) call-auction tick: its order fills at the final bar's CLOSE.
    is_close_auction: bool = False


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
    close_auction_time: str | None = "14:57",
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
    research-only grid outside the session: off-session ticks never fill orders. The
    explicit pre-open auction ticks and close-auction tick have fixed activations
    independent of ``execution_lag_bars``.
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
    # Pre-open off-session ticks are research/state only. Actual auction order
    # entry starts at the explicit pre-open auction tick below.
    for key in _offsession_keys(0, open_min, offsession_tick_minutes):
        plan.append(_Tick(key, _empty_minute_rows(), None, False, False, True))
    if auction_enabled:
        first = minute_rows.sort_values("minute_sort", kind="stable").drop_duplicates("ts_code", keep="first").copy()
        open_group = first.assign(high=first["open"], low=first["open"], close=first["open"])
        for column in ("vol", "amount"):
            if column in open_group.columns:
                open_group[column] = float("nan")  # intraday volume is unknown pre-open
        if preopen_time:
            # 09:15 blind pre-open order clears in the 09:30 opening call auction (single
            # price, no slippage): is_auction=True.
            plan.append(_Tick(preopen_time, _empty_minute_rows(), 0, False, True))
        # 09:25 sees the matched open and fills at the first CONTINUOUS bar (09:31), so it
        # is a continuous taker fill (slippage applies): is_auction=False. Only the open/
        # close call auctions are slippage-free.
        plan.append(_Tick(decision_time, open_group, min(1, n - 1), False, False))
    # Clamp the lag to the day's bar count so short/daily-fallback days (e.g. the
    # 09:30+15:00 synthesis) still trade even with auctions disabled: >=1 preserves
    # next-bar execution; <=n-1 lets the first decision reach the last bar.
    lag = max(1, min(execution_lag_bars, n - 1))
    for index, key in enumerate(real_keys):
        if close_auction_time and key == str(close_auction_time) and index < n - 1:
            # Close call auction: this bar's decision fills at the day's final bar's
            # CLOSE (the 15:00 print), not its open.
            plan.append(_Tick(key, groups[key], n - 1, True, True, is_close_auction=True))
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


def _normalize_tick(result: object) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]], float | None]:
    """A ``MainPolicyRunner`` step returns a ``_TickResult``; test fakes return a
    plain action list (no sub-steps or staged writes = current next-bar behavior)."""
    if isinstance(result, _TickResult):
        return result.actions, result.substeps, result.staged, result.main_wall_s
    actions = [dict(a) for a in (result or []) if isinstance(a, dict)]
    return actions, [], [], None


def _release_delayed_actions(
    delayed_actions: list[_DelayedAction],
    *,
    broker: SimBroker,
    incoming: dict[int, list[tuple[dict[str, object], bool, bool]]],
    tick: "_Tick",
    trade_date: str,
    when: datetime,
    n_real: int,
) -> list[dict[str, object]]:
    """Submit substep-produced broker actions once their declared compute is ready.

    A substep action is not a broker order until this release point. If ready_at
    falls between replay ticks, it is submitted on the first later orderable tick.
    Off-session ticks are research/state only, so they keep ready actions queued.
    Real market ticks are orderable even when no fill bar remains; those submissions
    follow the normal no-fill path instead of silently rolling forward.
    """
    if not _is_orderable_tick(tick):
        return []
    ready = sorted((item for item in delayed_actions if item.ready_at <= when), key=lambda item: item.seq)
    if not ready:
        return []
    ready_ids = {item.seq for item in ready}
    delayed_actions[:] = [item for item in delayed_actions if item.seq not in ready_ids]
    actions: list[dict[str, object]] = []
    for item in ready:
        action = dict(item.action)
        original_submitted_at = str(action.get("submitted_at") or "")
        original_submitted_time = str(action.get("submitted_time") or "")
        if original_submitted_at:
            action.setdefault("decision_at", original_submitted_at)
        if original_submitted_time:
            action.setdefault("decision_time", original_submitted_time)
        action["submitted_at"] = when.isoformat()
        action["submitted_time"] = tick.minute_key
        action["substep"] = item.substep
        action["substep_generated_at"] = item.generated_at
        action["substep_ready_at"] = item.ready_at.isoformat()
        actions.append(action)
    return _place_actions_at_tick(
        actions,
        broker=broker,
        incoming=incoming,
        delayed_actions=delayed_actions,
        tick=tick,
        trade_date=trade_date,
        n_real=n_real,
    )


def _is_orderable_tick(tick: "_Tick") -> bool:
    return not tick.is_offsession and (tick.is_real or tick.activate_index is not None)


def _place_actions_at_tick(
    actions: list[dict[str, object]],
    *,
    broker: SimBroker,
    incoming: dict[int, list[tuple[dict[str, object], bool, bool]]],
    delayed_actions: list[_DelayedAction],
    tick: "_Tick",
    trade_date: str,
    n_real: int,
) -> list[dict[str, object]]:
    """Route broker actions submitted at this tick into cancel/order queues."""
    placed_actions: list[dict[str, object]] = []
    for raw_action in actions:
        action = dict(raw_action)
        action.pop("_substep", None)
        name = _action_name(action)
        if name == "cancel":
            order_id = str(action.get("order_id") or "").strip()
            if order_id and _cancel_pending_order(
                broker,
                incoming,
                order_id,
                trade_date=trade_date,
                minute_key=tick.minute_key,
                reason=str(action.get("reason") or "agent_cancel"),
                delayed_actions=delayed_actions,
            ):
                placed_actions = [
                    placed for placed in placed_actions
                    if str(placed.get("order_id") or "") != order_id
                ]
                continue
            broker.record_event(
                "main_action_ignored",
                trade_date=trade_date,
                minute_key=tick.minute_key,
                action=_jsonable(action),
                reason="cancel_order_not_found" if order_id else "cancel_missing_order_id",
            )
            continue
        fill_index = tick.activate_index
        if fill_index is None or fill_index >= n_real:
            broker.record_event(
                "main_actions_unfilled", trade_date=trade_date, minute_key=tick.minute_key,
                action=_jsonable(action), reason="no_fill_bar_ahead",
            )
            continue
        incoming.setdefault(fill_index, []).append((action, tick.is_auction, tick.is_close_auction))
        placed_actions.append(dict(action))
    return placed_actions


def _submit_order(broker: SimBroker, action: dict[str, object], is_auction: bool, is_close_auction: bool = False) -> bool:
    """Translate a ``main()`` action into a Broker ``passorder`` submission.

    ``limit`` (a fixed price) routes to a 指定价 order resting ``valid_bars`` bars
    (default 1); otherwise a 对手价 market order valid that bar. ``close`` has no
    official op: it resolves to the holding account's market exit at submission
    (the activation tick is also the match tick, so there is no drift window) —
    an explicit ``account`` wins, else the unique holder; ambiguous closes are
    ignored (the driver already rejects them at call time). ``direct_repay``
    follows the official 1102 (amount in CNY) convention and ``transfer`` moves
    cash between the accounts; neither needs a bar. ``is_close_auction`` marks a
    15:00 close-auction order so it fills at the activation bar's close.
    Returns False if unsupported."""
    name = _action_name(action)
    order_kwargs = {
        "user_order_id": str(action.get("order_id") or ""),
        "reason": str(action.get("reason") or name),
        "submitted_at": str(action.get("submitted_at") or ""),
    }
    if name == "direct_repay":
        amount = _float_or_none(action.get("amount"))
        if amount is None or amount <= 0:
            return False
        broker.passorder(optype.DIRECT_REPAY, 1102, "", "", prtype.PEER, 0, amount, **order_kwargs)
        return True
    if name == "transfer":
        amount = _float_or_none(action.get("amount"))
        if amount is None or amount <= 0:
            return False
        broker.transfer(
            amount,
            str(action.get("from_account", "")),
            str(action.get("to_account", "")),
            reason=order_kwargs["reason"],
            order_id=order_kwargs["user_order_id"] or None,
            submitted_at=order_kwargs["submitted_at"],
        )
        return True
    ts_code = str(action.get("ts_code", "")).strip()
    if name not in _ORDER_ACTIONS or not ts_code:
        return False
    limit = _float_or_none(action.get("limit")) if name != "close" else None
    if limit is not None and limit <= 0:
        limit = None
    if name == "close":
        account = str(action.get("account") or "").strip().lower()
        if not account:
            holders = [
                acct for acct in ("stock", "credit")
                if broker.position_quantity(ts_code, account=acct) != 0
            ]
            if len(holders) != 1:
                return False  # no holder, or ambiguous (both accounts hold the code)
            account = holders[0]
        if account == "stock":
            name = "sell"
        else:
            name = "cover" if broker.position_quantity(ts_code, account="credit") < 0 else "credit_sell"
    try:
        _, op_type = broker.account_op_for_action(name)
    except ValueError:
        return False
    broker.passorder(
        op_type,
        1101,
        "",
        ts_code,
        prtype.FIX if limit is not None else prtype.PEER,
        limit or 0,
        _int_or_none(action.get("amount")),
        weight=_float_or_none(action.get("weight")),
        valid_bars=max(1, _int_or_none(action.get("valid_bars")) or 1) if limit is not None else 1,
        is_auction=is_auction,
        auction_close=is_close_auction,
        **order_kwargs,
    )
    return True


def _cancel_day_end_orders(broker: SimBroker, *, trade_date: str, minute_key: str | None = None) -> None:
    """Auto-void any still-working day orders after the final matchable bar."""
    for order in broker.working_orders():
        broker.cancel(
            str(order["order_id"]),
            reason="day_end_unfilled",
            trade_date=trade_date,
            minute_key=minute_key,
        )


def _action_name(action: dict[str, object]) -> str:
    raw = str(action.get("action", "")).lower().strip()
    return _ACTION_ALIASES.get(raw, raw)


def _cancel_pending_order(
    broker: SimBroker,
    incoming: dict[int, list[tuple[dict[str, object], bool, bool]]],
    order_id: str,
    *,
    trade_date: str,
    minute_key: str,
    reason: str,
    delayed_actions: list[_DelayedAction] | None = None,
) -> bool:
    """Cancel a live working order, submit-lag order, or unreleased substep action."""
    if broker.cancel(order_id, reason=reason, trade_date=trade_date, minute_key=minute_key):
        return True
    for index, items in list(incoming.items()):
        kept: list[tuple[dict[str, object], bool, bool]] = []
        removed: list[dict[str, object]] = []
        for item in items:
            action, is_auction, is_close_auction = item
            if str(action.get("order_id") or "") == order_id:
                removed.append(action)
            else:
                kept.append((action, is_auction, is_close_auction))
        if removed:
            if kept:
                incoming[index] = kept
            else:
                incoming.pop(index, None)
            for action in removed:
                broker.record_event(
                    "order_cancelled",
                    trade_date=trade_date,
                    minute_key=minute_key,
                    ts_code=str(action.get("ts_code") or ""),
                    order_id=order_id,
                    reason=reason,
                    pending_stage="submit_lag",
                )
            return True
    if delayed_actions is not None:
        kept_delayed: list[_DelayedAction] = []
        removed_delayed: list[_DelayedAction] = []
        for item in delayed_actions:
            if str(item.action.get("order_id") or "") == order_id:
                removed_delayed.append(item)
            else:
                kept_delayed.append(item)
        if removed_delayed:
            delayed_actions[:] = kept_delayed
            for item in removed_delayed:
                broker.record_event(
                    "order_cancelled",
                    trade_date=trade_date,
                    minute_key=minute_key,
                    ts_code=str(item.action.get("ts_code") or ""),
                    order_id=order_id,
                    reason=reason,
                    pending_stage="substep_delay",
                    substep=item.substep,
                    generated_at=item.generated_at,
                    ready_at=item.ready_at.isoformat(),
                )
            return True
    return False


def _pending_view(
    broker: SimBroker,
    incoming: dict[int, list[tuple[dict[str, object], bool, bool]]],
    *,
    delayed_actions: list[_DelayedAction] | None = None,
    now: datetime | None = None,
) -> dict[str, list[dict[str, object]]]:
    """Working orders the Agent can see via ``ctx.broker.pending(ts_code)``: the
    Broker's cancelable book plus decisions still inside the submit lag or delayed
    by a substep, so de-dup holds across the whole decision-to-fill window."""
    records = [_pending_record(record, now=now) for record in broker.working_orders()]
    for item in delayed_actions or []:
        action = item.action
        records.append(
            _pending_record(
                {
                    "order_id": action.get("order_id"),
                    # direct_repay carries no ts_code; it still shows as pending ("").
                    "ts_code": str(action.get("ts_code", "")),
                    "action": str(action.get("action", "")),
                    "order_volume": action.get("amount"),
                    "weight": action.get("weight"),
                    "price": action.get("limit"),
                    "status": "pending",
                    "submitted_at": action.get("submitted_at"),
                    "reason": action.get("reason"),
                    "pending_stage": "substep_delay",
                    "substep": item.substep,
                    "ready_at": item.ready_at.isoformat(),
                },
                now=now,
            )
        )
    for items in incoming.values():
        for action, _is_auction, _is_close_auction in items:
            records.append(
                _pending_record(
                    {
                        "order_id": action.get("order_id"),
                        "ts_code": str(action.get("ts_code", "")),
                        "action": str(action.get("action", "")),
                        "order_volume": action.get("amount"),
                        "weight": action.get("weight"),
                        "price": action.get("limit"),
                        "status": "pending",
                        "submitted_at": action.get("submitted_at"),
                        "reason": action.get("reason"),
                        "pending_stage": "submit_lag",
                    },
                    now=now,
                )
            )
    grouped: dict[str, list[dict[str, object]]] = {}
    for record in records:
        grouped.setdefault(str(record.get("ts_code", "")), []).append(record)
    return grouped


def _pending_record(record: dict[str, object], *, now: datetime | None) -> dict[str, object]:
    out = dict(record)
    submitted_at = str(out.get("submitted_at") or "")
    out.setdefault("submitted_at", submitted_at)
    if now is not None and submitted_at:
        try:
            submitted = datetime.fromisoformat(submitted_at)
        except ValueError:
            submitted = None
        if submitted is not None:
            out["age_minutes"] = max(0.0, (now - submitted).total_seconds() / 60.0)
    out.setdefault("age_minutes", 0.0)
    out.setdefault("status", "pending")
    return out


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
        "account": {
            "stock": _jsonable(broker.get_trade_detail_data(account_type="STOCK", data_type="ACCOUNT")[0]),
            "credit": _jsonable(broker.get_trade_detail_data(account_type="CREDIT", data_type="ACCOUNT")[0]),
            "total_assets": float(broker.equity()),
            "risk_limits": {
                "max_total_holdings": broker.profile.max_total_holdings,
                "max_single_name_weight": broker.profile.max_single_name_weight,
                "maintenance_closeout_ratio": broker.profile.maintenance_closeout_ratio,
                "maintenance_withdraw_ratio": broker.profile.maintenance_withdraw_ratio,
            },
        },
        "positions": _jsonable(
            broker.get_trade_detail_data(account_type="STOCK", data_type="POSITION")
            + broker.get_trade_detail_data(account_type="CREDIT", data_type="POSITION")
        ),
        "debt_contracts": _jsonable(broker.get_debt_contract()),
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
