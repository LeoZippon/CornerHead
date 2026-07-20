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

import base64
import json
import os
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

import numpy as np
import pandas as pd
import pyarrow as pa

from autotrade.environment.broker import MarketData, SimBroker, optype, prtype
from autotrade.environment.broker_core import afterhours_available
from autotrade.environment.data.contracts import sim_datetime
from autotrade.environment.replay_market import (
    MinuteMarketData,
    ParquetMinuteReplaySource,
    empty_minute_rows,
    minute_rows_with_daily_fallback,
    minute_sort,
)
from autotrade.environment.replay_stats import ReplayResult
from autotrade.environment.runtime import sanitize_for_log
from autotrade.environment.sandbox import hide_snapshot_slots_from_agent
from autotrade.environment.state_staging import StateStager
from autotrade.environment.timeview import Timeview

_AUCTION_PREOPEN_TIME = "09:15"
_AUCTION_DECISION_TIME = "09:25"
_AUCTION_CLOSE_TIME = "14:57"


class BacktestError(RuntimeError):
    """A formal backtest step failed; the error is explicit, never silent."""

    def __init__(
        self,
        message: str,
        *,
        error_type: str | None = None,
        reason: str | None = None,
        retry_hint: str | None = None,
    ) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.reason = reason
        self.retry_hint = retry_hint


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


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


def _executor_pathsep_join(executor, paths: list[Path]) -> str:
    return os.pathsep.join(executor.map_path(path) for path in paths if path.exists())

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
    agent_peak_rss_bytes: int | None = None


@dataclass(frozen=True)
class _DelayedAction:
    seq: int
    ready_at: datetime
    action: dict[str, object]
    substep: str
    generated_at: str


@dataclass(frozen=True)
class _PendingTransfer:
    seq: int
    action: dict[str, object]
    requested_at: str


_PREOPEN_TRANSFER_TIME = "09:14"


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
        runtime_dir: Path | None = None,
        snapshot_path: Path | None = None,
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
        self.snapshot_path = Path(snapshot_path) if snapshot_path is not None else self.paths.snapshot
        self.formal_isolation = bool(getattr(executor, "formal_isolation", False))
        if runtime_dir is None:
            self.state_dir = self.paths.workspace / ".state"
            self.staging_dir = self.paths.workspace / ".state_staging"
            self.asof_dir = self.paths.workspace / ".asof"
        else:
            runtime_dir = Path(runtime_dir)
            self.state_dir = runtime_dir / "state"
            self.staging_dir = runtime_dir / "state_staging"
            self.asof_dir = runtime_dir / "asof"
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
                "AT_SNAPSHOT_DIR": self.executor.map_path(self.snapshot_path),
                "AT_AGENT_OUTPUT_DIR": self.executor.map_path(self.paths.agent_output),
                "AT_MODEL_DIR": self.executor.map_path(self.paths.model_artifacts),
                "AT_STATE_DIR": self.executor.map_path(self.state_dir),
                "AT_STATE_STAGING_DIR": self.executor.map_path(self.staging_dir),
                "AT_DECISION_TIME": self.decision_time,
                "AT_REPLAY_GRANULARITY": self.replay_granularity,
                "AT_FORBIDDEN_PATHS": (
                    "/mnt/agent/workspace:/mnt/artifacts:/mnt/snapshots"
                    if self.formal_isolation
                    else _executor_pathsep_join(
                        self.executor, [self.paths.train, self.paths.valid, self.paths.test, self.paths.artifacts]
                    )
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
            if not self.formal_isolation:
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
                cwd=self.paths.agent_output if self.formal_isolation else self.paths.agent,
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
        peak_rss_raw = response.get("agent_peak_rss_bytes")
        try:
            agent_peak_rss_bytes = int(peak_rss_raw) if peak_rss_raw is not None else None
        except (TypeError, ValueError):
            agent_peak_rss_bytes = None
        return _TickResult(
            actions=[dict(action) for action in actions if isinstance(action, dict)],
            substeps=[dict(s) for s in substeps if isinstance(s, dict)],
            staged=[dict(s) for s in staged if isinstance(s, dict)],
            main_wall_s=main_wall_s,
            agent_peak_rss_bytes=agent_peak_rss_bytes,
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
        # The in-container driver can outlive the host client either way (a
        # crashed client never signalled it), so the marker sweep is
        # unconditional, not just on the terminate path.
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
            # The columnar bars arrays are JSON-safe by construction (str codes,
            # _float_or_none values) and dominate the record size; deep-walking
            # them through _jsonable every tick is pure overhead.
            state = record.get("state")
            bars = state.pop("bars", None) if isinstance(state, dict) else None
            encoded = _jsonable(record)
            if bars is not None:
                state["bars"] = bars  # restore the caller's record
                encoded_state = encoded.get("state")
                if isinstance(encoded_state, dict):
                    encoded_state["bars"] = bars
            proc.stdin.write(
                json.dumps(encoded, ensure_ascii=False, default=str, separators=(",", ":")) + "\n"
            )
            proc.stdin.flush()
        except BrokenPipeError as exc:
            raise BacktestError(f"main policy runner exited early: {self._drain_stderr()}") from exc
        # Hard per-decision wall cap: the whole main(ctx) tick — its compute AND any
        # nl() calls it makes — must finish within timeout_seconds, else the decision is
        # killed immediately and the backtest fails. No inactivity reset: a decision that
        # leans on slow/serial NL is what the cap is meant to catch.
        deadline = time.monotonic() + self.timeout_seconds
        if self.nl_service is not None:
            # NL requests served inside this decision share the same absolute
            # deadline: each provider round's timeout is clamped to the remaining
            # time (retries disabled once clamped), so an in-flight synchronous
            # NL task cannot stretch the decision far past its cap — the kill
            # below only fires between pump iterations.
            self.nl_service.deadline_at = deadline
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
                raise BacktestError(
                    str(sanitize_for_log(str(response.get("error", "main(ctx) failed")))),
                    error_type=_optional_text(response.get("public_error_type")),
                    reason=_optional_text(response.get("public_reason")),
                    retry_hint=_optional_text(response.get("public_retry_hint")),
                )
            return response
        proc.kill()
        self.executor.kill_marker(self._run_marker)
        raise BacktestError(
            f"main(ctx) decision exceeded its {self.timeout_seconds:.0f}s wall-clock cap",
            error_type="strategy_runtime_error",
            reason="strategy_decision_timeout",
            retry_hint="Cache data by asof_version and reduce repeated work inside each decision tick.",
        )

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
    corporate_actions_by_date: dict[str, list[dict[str, object]]] | None = None,
    auction_prints_by_date: dict[tuple[str, str], dict[str, float]] | None = None,
    main_policy: MainPolicyRunner,
    replay_intraday_1min: pd.DataFrame | None = None,
    replay_minute_source: ParquetMinuteReplaySource | None = None,
    replay_auction_results: pd.DataFrame | None = None,
    afterhours_decision_time: str | None = None,
    execution_lag_bars: int = 2,
    offsession_tick_minutes: int = 30,
    intraday_decision_minutes: int = 1,
    max_seconds_per_trading_day: float | None = None,
    enforce_substep_timeout: bool = True,
    enforce_substep_coverage: bool = True,
    max_untracked_substep_wall_s: float = 0.25,
    timeview_enabled: bool = False,
    snapshot_dir: Path | None = None,
    replay_dir: Path | None = None,
    timeview_stash_dir: Path | None = None,
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
    until the block's ``ready_at`` and submitted only if the generating tick,
    ``ready_at``, and release tick are in an exchange order-submission window.
    From that submit tick they use the same ``execution_lag_bars`` mapping. A
    decision with no bar ``execution_lag_bars`` ahead (near the close) cannot fill
    and is recorded ``main_actions_unfilled``. The final trade date is reserved for
    mandatory liquidation; minute bars drive the replay when present, else a daily
    09:30/15:00 fallback is synthesized.

    The tick grid spans the whole day on the same ``main(ctx)`` entry, so the loop
    drives both backtest and live. Every day starts with a fixed 09:15 info tick
    (no price, fills at the 09:30 opening auction) and a blind 09:25 submission
    tick (no price, fills at the first continuous bar). A captured
    ``stk_auction`` result wakes ``main(ctx)`` only at its observed availability;
    pre-open result ticks are research-only, while arrivals after 09:30 wake the
    corresponding real bar. The fixed 14:57 decision fills at the day's final
    bar (the 15:00 close auction). These batch-auction fills are labelled
    ``price_label="auction"``.
    ``afterhours_decision_time`` (None = off) appends the after-hours fixed-price
    tick (盘后固定价格交易, e.g. 15:05): the strategy sees the day's close prints
    and its orders settle immediately at the official close for board-eligible
    codes, labelled ``price_label="afterhours_fixed"`` with no slippage.
    ``offsession_tick_minutes`` (0 = off) adds a research-only tick grid outside the
    session; off-session orders do not fill. ``ctx.broker.pending(ts_code)`` exposes
    still-working orders so the Agent can avoid re-submitting before a fill (parity
    with the live order query).

    ``intraday_decision_minutes`` (default 1 = every bar) coarsens only the
    ``main(ctx)`` decision cadence on plain intraday bars: the Broker still
    matches every minute bar (pending orders, execution lag, auction fills are
    unchanged), auction and off-session ticks always decide, and the Timeview /
    staged-state clocks advance every tick. Decisions land on wall-clock minutes
    divisible by the grid.

    ``enforce_substep_timeout`` (default True) keeps the per-substep wall fail-fast
    that aborts the replay when a declared ``ctx.substep`` block runs over its budget
    B. Declared budgets advance sim time, so formal evaluation and production-path
    benchmarks enforce it. Targeted diagnostics may disable the raise while still
    collecting the same per-substep runtime statistics.
    ``enforce_substep_coverage`` rejects substantive Python-side ``main(ctx)`` time
    outside declared substeps (with a small overhead grace), so heavy unwrapped work
    cannot hide in the tick's aggregate wall time.
    """
    # Wall clock covers the FULL replay lifecycle (market/broker/Timeview
    # construction included): a quarter of minute data makes init a first-class
    # cost, and the probe extrapolation must not hide it.
    replay_start = time.monotonic()
    if replay_intraday_1min is not None and replay_minute_source is not None:
        raise ValueError("pass either replay_intraday_1min or replay_minute_source, not both")
    market = MarketData(replay_daily)
    if len(market.trade_dates) < 2:
        raise BacktestError("replay region needs at least two trade dates for entry/exit")
    minute_market = (
        MinuteMarketData(replay_intraday_1min)
        if replay_intraday_1min is not None and not replay_intraday_1min.empty
        else None
    )
    replay_days = market.trade_dates[:-1]
    if replay_minute_source is not None and replay_days:
        # Overlap the first daily read/normalization with Broker and Timeview
        # construction. Subsequent days are prefetched during the prior day.
        replay_minute_source.prefetch(replay_days[0])
    auction_results_by_date = {
        str(day): group.reset_index(drop=True)
        for day, group in (
            replay_auction_results.groupby(replay_auction_results["trade_date"].astype(str), sort=False)
            if replay_auction_results is not None and not replay_auction_results.empty
            else []
        )
    }
    granularity = "minute" if minute_market is not None or replay_minute_source is not None else "daily"
    entry_date, exit_date = market.trade_dates[0], market.trade_dates[-1]
    broker = SimBroker(
        profile,
        market,
        shortable_codes=shortable_codes,
        shortable_by_date=shortable_by_date,
        corporate_actions_by_date=corporate_actions_by_date,
        auction_prints_by_date=auction_prints_by_date,
    )
    _tv_init_t0 = time.monotonic()
    timeview = (
        Timeview(
            host_dir=main_policy.asof_dir,
            executor=main_policy.executor,
            snapshot_dir=snapshot_dir,
            replay_frames=_timeview_replay_frames(
                replay_dir, replay_daily, replay_intraday_1min, replay_auction_results
            ),
            replay_text_library_dir=(Path(replay_dir) / "text_library") if replay_dir is not None else None,
            incremental_domains={"intraday_1min"} if replay_minute_source is not None else None,
            stash_dir=timeview_stash_dir,
        )
        if timeview_enabled and snapshot_dir is not None and getattr(main_policy, "paths", None) is not None
        else None
    )
    timeview_init_seconds = time.monotonic() - _tv_init_t0 if timeview is not None else 0.0
    # Managed ctx.state_dir: sub-step writes stage to a hidden dir and merge into the
    # visible dir at ready_at = tick + B. Reset per backtest for reproducibility.
    stager = (
        StateStager(
            visible_dir=main_policy.state_dir,
            staging_dir=main_policy.staging_dir,
        )
        if getattr(main_policy, "paths", None) is not None
        else None
    )
    equity_by_date: dict[str, float] = {}
    substep_runtime: dict[str, dict[str, float]] = {}
    # Per-phase wall-time so the 24h replay's added cost is visible (W9): the agent
    # main(ctx) step, the Timeview rebuilds, the state-staging merges, and the Broker
    # matching. The NL-service share of step wall is split out from strategy compute.
    phase_wall = {
        "strategy_step": 0.0, "agent_main": 0.0, "timeview_init": timeview_init_seconds,
        "timeview_roll": 0.0, "state_merge": 0.0, "broker_match": 0.0,
    }
    agent_peak_rss_bytes = 0
    tick_counts = {"total": 0, "intraday": 0, "offsession": 0, "decisions": 0, "actions": 0}
    delayed_actions: list[_DelayedAction] = []
    delayed_seq = 0
    total_days = len(market.trade_dates)
    last_progress_idx = 0
    last_progress_time = replay_start

    for day_idx, trade_date in enumerate(market.trade_dates):
        preopen_transfers: list[_PendingTransfer] = []
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
            if replay_minute_source is not None:
                next_trade_date = replay_days[day_idx + 1] if day_idx + 1 < len(replay_days) else None
                partition = replay_minute_source.rows_for_date(
                    trade_date,
                    next_trade_date=next_trade_date,
                )
                minute_seed = partition.market_rows
                if timeview is not None and partition.timeview_rows is not None:
                    timeview.append_replay_partition("intraday_1min", partition.timeview_rows)
            else:
                minute_seed = (
                    minute_market.rows_for_date(trade_date)
                    if minute_market is not None
                    else empty_minute_rows()
                )
            minute_rows = minute_rows_with_daily_fallback(replay_daily, trade_date, minute_seed)
            if not minute_rows.empty:
                plan = _day_tick_plan(
                    minute_rows, execution_lag_bars,
                    offsession_tick_minutes=offsession_tick_minutes,
                    afterhours_time=afterhours_decision_time,
                    auction_results=auction_results_by_date.get(str(trade_date)),
                )
                real_index = {tick.minute_key: i for i, tick in enumerate(t for t in plan if t.is_real)}
                n_real = len(real_index)
                # Decisions wait out the submit lag, then enter the Broker's order book
                # (passorder) at their activation bar. Substep-wrapped broker actions
                # are first delayed until ready_at; if ready_at is not an exchange
                # order-submission time, the action is recorded unfilled instead of
                # being auto-scheduled into a later session.
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
                        phase_wall["timeview_roll"] += time.monotonic() - _tv_t0
                    else:
                        asof_dir, asof_version = None, None
                    if timeview is not None and main_policy.nl_service is not None:
                        main_policy.nl_service.current_when = when  # roll ctx.nl() text on the same clock
                    if stager is not None:
                        _merge_t0 = time.monotonic()
                        stager.merge_ready(when)  # surface staged writes whose ready_at has arrived
                        phase_wall["state_merge"] += time.monotonic() - _merge_t0
                    _confirm_preopen_transfers(preopen_transfers, broker=broker, trade_date=trade_date, when=when)
                    released_actions = _release_delayed_actions(
                        delayed_actions,
                        broker=broker,
                        incoming=incoming,
                        preopen_transfers=preopen_transfers,
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
                    # Coarser decision grid: skip main(ctx) (and its state assembly)
                    # on non-decision bars; matching, timeview, staged-state merges,
                    # transfers, and delayed-action release above all still ran.
                    if not _is_decision_tick(tick, intraday_decision_minutes):
                        continue
                    tick_counts["decisions"] += 1
                    state = _market_state(
                        broker,
                        trade_date=trade_date,
                        minute_key=tick.minute_key,
                        minute_group=tick.group,
                        asof_dir=asof_dir,
                        asof_version=asof_version,
                        pending=_pending_view(broker, incoming, now=when),
                        pending_orders=_incoming_reservation_records(broker, incoming),
                        cur_datetime=when.isoformat(),
                    )
                    # A single decision (one main(ctx) tick) over the per-decision real
                    # wall cap is killed inside MainPolicyRunner.step. Here we accumulate
                    # the day's compute and fail-fast when it exceeds the per-day budget
                    # (scales with replay length, unlike a fixed total cap).
                    _tick_t0 = time.monotonic()
                    actions, substeps, staged, main_wall_s, tick_peak_rss = _normalize_tick(main_policy.step(state))
                    tick_counts["actions"] += len(actions)
                    _tick_wall = time.monotonic() - _tick_t0
                    day_compute_wall += _tick_wall
                    phase_wall["strategy_step"] += _tick_wall
                    strategy_wall = float(main_wall_s if main_wall_s is not None else _tick_wall)
                    phase_wall["agent_main"] += strategy_wall
                    if tick_peak_rss is not None:
                        agent_peak_rss_bytes = max(agent_peak_rss_bytes, int(tick_peak_rss))
                    if stager is not None and staged:
                        try:
                            stager.register(staged, when=when)
                        except ValueError as exc:
                            # Agent-controlled rel path escaped its root — a
                            # protocol violation, not an internal error.
                            raise BacktestError(str(exc)) from exc
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
                            f"ctx.substep (allowed: {max_untracked_substep_wall_s:.2f}s per tick). Move imports, "
                            "data loading and any per-tick computation INSIDE a ctx.substep(name, "
                            "budget_minutes=B) block; only trivial glue may run outside"
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
                        preopen_transfers=preopen_transfers,
                        tick=tick,
                        trade_date=trade_date,
                        when=when,
                        n_real=n_real,
                    )
                    if placed_actions:
                        broker.record_event(
                            "main_actions", trade_date=trade_date, minute_key=tick.minute_key,
                            action_count=len(placed_actions), actions=_jsonable(placed_actions),
                        )
                _cancel_day_end_orders(broker, trade_date=trade_date)

        _reject_unconfirmed_transfers(preopen_transfers, broker=broker, trade_date=trade_date)

        equity = broker.mark_to_market(trade_date)
        if trade_date == exit_date and any(state.positions for state in broker.accounts.values()):
            broker.close_all(trade_date)
            equity = broker.equity()
            broker.record_positions_eod(trade_date)  # refresh: only unsellable leftovers remain
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

    replay_wall_seconds = time.monotonic() - replay_start
    return ReplayResult(
        equity_curve=pd.Series(equity_by_date).sort_index(),
        broker=broker,
        decision_date=entry_date,
        exit_date=exit_date,
        granularity=granularity,
        substep_runtime=substep_runtime or None,
        replay_wall_seconds=replay_wall_seconds,
        replayed_trade_days=len(replay_days),
        replayed_exit_days=1,
        total_ticks=tick_counts["total"],
        intraday_ticks=tick_counts["intraday"],
        offsession_ticks=tick_counts["offsession"],
        decision_calls=tick_counts["decisions"],
        strategy_action_count=tick_counts["actions"],
        state_staging_audit=(stager.audit() if stager is not None else None),
        phase_seconds=_phase_seconds(
            phase_wall,
            getattr(getattr(main_policy, "nl_service", None), "nl_wall_seconds", 0.0),
            replay_wall_seconds,
        ),
        agent_peak_rss_bytes=agent_peak_rss_bytes or None,
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
    # An after-hours fixed-price tick (盘后固定价格交易): its orders settle
    # immediately at the day's official close for board-eligible codes.
    is_afterhours: bool = False
    # Data-driven market event (for example a newly-landed auction result) that
    # always wakes main(ctx) without changing how its orders are matched.
    always_decide: bool = False


def _is_decision_tick(tick: "_Tick", intraday_decision_minutes: int) -> bool:
    """Whether ``main(ctx)`` runs on this tick under the configured decision grid.

    Auction ticks (pre-open info, matched open, close auction) and off-session
    research ticks always decide; plain intraday bars decide only on wall-clock
    minutes divisible by the grid. 1 = every bar (the default, exact legacy
    behavior)."""
    if intraday_decision_minutes <= 1:
        return True
    if (
        tick.is_offsession
        or tick.is_auction
        or tick.is_close_auction
        or tick.is_afterhours
        or getattr(tick, "always_decide", False)
    ):
        return True
    hour_text, _, minute_text = str(tick.minute_key).partition(":")
    try:
        total_minutes = int(hour_text) * 60 + int(minute_text)
    except ValueError:
        return True
    return total_minutes % int(intraday_decision_minutes) == 0


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
    execution_lag_bars: int,
    *,
    offsession_tick_minutes: int = 15,
    afterhours_time: str | None = None,
    auction_results: pd.DataFrame | None = None,
) -> list[_Tick]:
    """Ordered decision ticks for one day, each tagged with the real-bar index its
    orders reach the book at (``activate_index``).

    A decision on real bar *i* activates at *i + execution_lag_bars*; a bar with no
    such later bar (near the close) yields ``activate_index=None``. Two fixed
    pre-open ticks lead the day: a 09:15 info tick with no bars (``ctx.price`` is
    None) activating at the first real bar (the 09:30 opening auction), and a blind
    09:25 tick. An
    observed ``stk_auction`` result can add a source-backed pre-open tick or wake
    the corresponding real minute after continuous trading begins. The fixed 14:57
    decision activates at the day's final bar (the 15:00 close auction) instead of
    the default lag. ``offsession_tick_minutes`` (0 = off) adds a
    research-only grid outside the session: off-session ticks never fill orders. The
    explicit pre-open auction ticks and close-auction tick have fixed activations
    independent of ``execution_lag_bars``. ``afterhours_time`` (e.g. 15:05) appends
    the after-hours fixed-price tick after the last real bar: it sees the day's
    close prints and its orders settle immediately at the official close for
    board-eligible codes (no activation bar; see ``_execute_afterhours_action``).
    """
    real_keys = sorted({str(key) for key in minute_rows["minute_key"]}, key=minute_sort)
    if not real_keys:
        return []
    groups = {str(key): group for key, group in minute_rows.groupby(minute_rows["minute_key"].astype(str), sort=False)}
    n = len(real_keys)
    plan: list[_Tick] = []
    # Off-session grid frame: the session starts at the earliest pre-open tick and
    # ends at the last real bar; ticks outside [open, close] are research-only.
    session_open = _AUCTION_PREOPEN_TIME
    open_min, close_min = minute_sort(session_open), minute_sort(real_keys[-1])
    # Pre-open off-session ticks are research/state only. Actual auction order
    # entry starts at the explicit pre-open auction tick below.
    for key in _offsession_keys(0, open_min, offsession_tick_minutes):
        plan.append(_Tick(key, empty_minute_rows(), None, False, False, True))
    # 09:15 blind pre-open orders clear in the 09:30 opening call auction (single
    # price, no slippage): is_auction=True.
    plan.append(_Tick(_AUCTION_PREOPEN_TIME, empty_minute_rows(), 0, False, True))
    # The exchange has matched by 09:25, but this project's TuShare source does
    # not publish the full result until 09:27-09:29. Keep this order-entry tick
    # blind; never reconstruct its price from the future 09:30/09:31 minute bar.
    plan.append(_Tick(_AUCTION_DECISION_TIME, empty_minute_rows(), min(1, n - 1), False, False))
    auction_ticks, auction_wake_keys = _auction_result_ticks(
        auction_results,
        real_keys=real_keys,
    )
    plan.extend(auction_ticks)
    # Clamp the lag to the day's bar count: >=1 preserves next-bar execution;
    # <=n-1 lets the first decision reach the last bar on short/fallback days.
    lag = max(1, min(execution_lag_bars, n - 1))
    for index, key in enumerate(real_keys):
        if key == _AUCTION_CLOSE_TIME and index < n - 1:
            # Close call auction: this bar's decision fills at the day's final bar's
            # CLOSE (the 15:00 print), not its open.
            plan.append(_Tick(key, groups[key], n - 1, True, True, is_close_auction=True))
        else:
            activate_index = index + lag
            plan.append(
                _Tick(
                    key,
                    groups[key],
                    activate_index if activate_index < n else None,
                    True,
                    False,
                    always_decide=key in auction_wake_keys,
                )
            )
    session_end = close_min
    if afterhours_time and minute_sort(str(afterhours_time)) > close_min:
        # After-hours fixed-price tick: the close prints are visible (ctx.bars =
        # the final bar group) and orders settle at the close, immediately.
        plan.append(_Tick(str(afterhours_time), groups[real_keys[-1]], None, False, False, is_afterhours=True))
        session_end = minute_sort(str(afterhours_time))
    # Post-close off-session ticks: research/state only, orders never fill.
    after_start = ((session_end // offsession_tick_minutes) + 1) * offsession_tick_minutes if offsession_tick_minutes > 0 else 0
    for key in _offsession_keys(after_start, 24 * 60, offsession_tick_minutes):
        plan.append(_Tick(key, empty_minute_rows(), None, False, False, True))
    return sorted(plan, key=lambda tick: minute_sort(tick.minute_key))


def _auction_result_ticks(
    rows: pd.DataFrame | None,
    *,
    real_keys: list[str],
) -> tuple[list[_Tick], set[str]]:
    """Return source-backed pre-open ticks and real bars that an arrival wakes."""
    if rows is None or rows.empty or "available_at" not in rows.columns or "price" not in rows.columns:
        return [], set()
    frame = rows.copy()
    available = pd.to_datetime(frame["available_at"], errors="coerce", utc=True).dt.tz_convert("Asia/Shanghai")
    trade_days = frame["trade_date"].astype(str) if "trade_date" in frame.columns else pd.Series("", index=frame.index)
    same_day = available.dt.strftime("%Y%m%d").eq(trade_days)
    visible_mask = available.notna() & same_day
    frame = frame[visible_mask].copy()
    available = available[visible_mask]
    if frame.empty:
        return [], set()
    frame["result_minute"] = available.dt.ceil("min").dt.strftime("%H:%M").to_numpy()
    ticks: list[_Tick] = []
    wake_keys: set[str] = set()
    first_real_key = real_keys[0]
    for minute_key, group in frame.groupby("result_minute", sort=True):
        if minute_sort(str(minute_key)) >= minute_sort(first_real_key):
            wake_key = next(
                (key for key in real_keys if minute_sort(key) >= minute_sort(str(minute_key))),
                None,
            )
            if wake_key is not None:
                wake_keys.add(wake_key)
            continue
        price = pd.to_numeric(group.get("price"), errors="coerce")
        visible = group[price.gt(0)].copy()
        visible["open"] = pd.to_numeric(visible["price"], errors="coerce")
        visible["high"] = visible["open"]
        visible["low"] = visible["open"]
        visible["close"] = visible["open"]
        visible["minute_key"] = str(minute_key)
        visible["minute_sort"] = minute_sort(str(minute_key))
        ticks.append(
            _Tick(
                str(minute_key),
                visible,
                None,
                False,
                False,
                always_decide=True,
            )
        )
    return ticks, wake_keys


def _phase_seconds(phase_wall: dict[str, float], nl_wall: float, replay_wall: float) -> dict[str, float]:
    """Per-phase replay wall-time. The NL-service share of the agent step is split
    out of strategy compute so the four host phases plus the LLM service sum to the
    replay's active work."""
    nl = float(nl_wall or 0.0)
    main_wall = max(0.0, phase_wall["agent_main"])
    result = {
        "strategy_compute": round(max(0.0, main_wall - nl), 3),
        # State serialization, JSONL transport, driver ctx construction and
        # response decoding around main(ctx); measured separately from Agent code.
        "strategy_ipc": round(max(0.0, phase_wall["strategy_step"] - main_wall), 3),
        "nl_service": round(nl, 3),
        "timeview_init": round(phase_wall["timeview_init"], 3),
        "timeview_roll": round(phase_wall["timeview_roll"], 3),
        "state_merge": round(phase_wall["state_merge"], 3),
        "broker_match": round(phase_wall["broker_match"], 3),
    }
    accounted = sum(result.values())
    # Remaining host loop: day slicing/planning, state assembly, broker book-
    # keeping and result construction. This closes the previous opaque timing gap.
    result["host_replay_overhead"] = round(max(0.0, replay_wall - accounted), 3)
    return result


def _normalize_tick(
    result: object,
) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]], float | None, int | None]:
    """A ``MainPolicyRunner`` step returns a ``_TickResult``; test fakes return a
    plain action list (no sub-steps or staged writes = current next-bar behavior)."""
    if isinstance(result, _TickResult):
        return (
            result.actions,
            result.substeps,
            result.staged,
            result.main_wall_s,
            result.agent_peak_rss_bytes,
        )
    actions = [dict(a) for a in (result or []) if isinstance(a, dict)]
    return actions, [], [], None, None


def _release_delayed_actions(
    delayed_actions: list[_DelayedAction],
    *,
    broker: SimBroker,
    incoming: dict[int, list[tuple[dict[str, object], bool, bool]]],
    preopen_transfers: list[_PendingTransfer],
    tick: "_Tick",
    trade_date: str,
    when: datetime,
    n_real: int,
) -> list[dict[str, object]]:
    """Submit substep-produced broker actions once their declared compute is ready.

    A substep action is not a broker order until this release point. If its
    generating tick or ready_at is outside the exchange's accepted order-submission
    windows, the action is recorded unfilled instead of being auto-scheduled into
    a later session. Real market ticks are orderable even when no fill bar remains;
    those submissions follow the normal no-fill path instead of silently rolling
    forward.
    """
    ready = sorted((item for item in delayed_actions if item.ready_at <= when), key=lambda item: item.seq)
    if not ready:
        return []
    current_window = _orderable_window_id(when) if _is_orderable_tick(tick) else None
    ready_ids: set[int] = set()
    releasable: list[_DelayedAction] = []
    for item in ready:
        if _action_name(item.action) in ("transfer", "direct_repay"):
            # Cash operations are not exchange orders: transfer is a pre-09:14
            # request its queue gates itself, direct_repay settles immediately —
            # neither needs an orderable window or a later fill bar.
            releasable.append(item)
            ready_ids.add(item.seq)
            continue
        generated_at = _parse_datetime(item.generated_at)
        generated_window = _orderable_window_id(generated_at) if generated_at is not None else None
        ready_window = _orderable_window_id(item.ready_at)
        reason = ""
        if generated_window is None:
            reason = "substep_generated_at_not_orderable"
        elif ready_window is None:
            reason = "substep_ready_at_not_orderable"
        elif _orderable_window_has_passed(item.ready_at, when):
            reason = "substep_order_window_missed"
        elif current_window == ready_window:
            releasable.append(item)
            ready_ids.add(item.seq)
            continue
        if reason:
            ready_ids.add(item.seq)
            broker.record_event(
                "main_actions_unfilled",
                trade_date=trade_date,
                minute_key=tick.minute_key,
                action=_jsonable(item.action),
                reason=reason,
                substep=item.substep,
                generated_at=item.generated_at,
                ready_at=item.ready_at.isoformat(),
            )
    delayed_actions[:] = [item for item in delayed_actions if item.seq not in ready_ids]
    if not releasable:
        return []
    actions: list[dict[str, object]] = []
    for item in releasable:
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
        preopen_transfers=preopen_transfers,
        tick=tick,
        trade_date=trade_date,
        when=when,
        n_real=n_real,
    )


def _is_orderable_tick(tick: "_Tick") -> bool:
    if tick.is_afterhours:
        return True
    return not tick.is_offsession and (tick.is_real or tick.activate_index is not None)


def _parse_datetime(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _seconds_since_midnight(when: datetime) -> float:
    return (
        when.hour * 3600
        + when.minute * 60
        + when.second
        + when.microsecond / 1_000_000.0
    )


def _orderable_window_id(when: datetime) -> str | None:
    seconds = _seconds_since_midnight(when)
    windows = (
        ("open_auction", 9 * 3600 + 15 * 60, 9 * 3600 + 25 * 60),
        ("morning", 9 * 3600 + 30 * 60, 11 * 3600 + 30 * 60),
        ("afternoon", 13 * 3600, 15 * 3600),
    )
    for name, start, end in windows:
        if start <= seconds <= end:
            return name
    return None


def _orderable_window_has_passed(ready_at: datetime, now: datetime) -> bool:
    if now.date() > ready_at.date():
        return True
    if now.date() < ready_at.date():
        return False
    window = _orderable_window_id(ready_at)
    if window == "open_auction":
        end = 9 * 3600 + 25 * 60
    elif window == "morning":
        end = 11 * 3600 + 30 * 60
    elif window == "afternoon":
        end = 15 * 3600
    else:
        return False
    return _seconds_since_midnight(now) > end


def _place_actions_at_tick(
    actions: list[dict[str, object]],
    *,
    broker: SimBroker,
    incoming: dict[int, list[tuple[dict[str, object], bool, bool]]],
    preopen_transfers: list[_PendingTransfer],
    tick: "_Tick",
    trade_date: str,
    when: datetime,
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
            if not _is_orderable_tick(tick):
                broker.record_event(
                    "main_action_ignored",
                    trade_date=trade_date,
                    minute_key=tick.minute_key,
                    action=_jsonable(action),
                    reason="cancel_not_orderable_tick",
                )
                continue
            cancel_block_reason = _incoming_cancel_block_reason(incoming, order_id, when=when)
            if cancel_block_reason:
                broker.record_event(
                    "main_action_ignored",
                    trade_date=trade_date,
                    minute_key=tick.minute_key,
                    action=_jsonable(action),
                    reason=cancel_block_reason,
                )
                continue
            if order_id and _cancel_pending_order(
                broker,
                incoming,
                order_id,
                trade_date=trade_date,
                minute_key=tick.minute_key,
                reason=str(action.get("reason") or "agent_cancel"),
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
        if name == "transfer":
            if _queue_preopen_transfer(
                action,
                preopen_transfers,
                broker=broker,
                trade_date=trade_date,
                minute_key=tick.minute_key,
                when=when,
            ):
                placed_actions.append(dict(action))
            continue
        if tick.is_afterhours:
            # After-hours fixed-price: no activation bar — settle at the close now.
            if _execute_afterhours_action(broker, action, trade_date=trade_date, minute_key=tick.minute_key):
                placed_actions.append(dict(action))
            else:
                broker.record_event(
                    "main_action_ignored", trade_date=trade_date, minute_key=tick.minute_key,
                    action=_jsonable(action), reason="unsupported_or_missing_ts_code",
                )
            continue
        if not _is_orderable_tick(tick):
            broker.record_event(
                "main_actions_unfilled", trade_date=trade_date, minute_key=tick.minute_key,
                action=_jsonable(action), reason="not_orderable_tick",
            )
            continue
        if name == "direct_repay":
            # Immediate cash settlement (直接还款不经过撮合): needs an orderable
            # tick like any counter instruction, but no activation/fill bar.
            if _submit_order(broker, action, False):
                placed_actions.append(dict(action))
            continue
        fill_index = tick.activate_index
        if fill_index is None or fill_index >= n_real:
            broker.record_event(
                "main_actions_unfilled", trade_date=trade_date, minute_key=tick.minute_key,
                action=_jsonable(action), reason="no_fill_bar_ahead",
            )
            continue
        if action.get("limit") in (None, ""):
            reserve_price = _tick_price_for_code(tick.group, str(action.get("ts_code") or ""))
            if reserve_price is not None:
                action["_reserve_price"] = reserve_price
        incoming.setdefault(fill_index, []).append((action, tick.is_auction, tick.is_close_auction))
        placed_actions.append(dict(action))
    return placed_actions


def _queue_preopen_transfer(
    action: dict[str, object],
    pending: list[_PendingTransfer],
    *,
    broker: SimBroker,
    trade_date: str,
    minute_key: str,
    when: datetime,
) -> bool:
    """Store a same-day account transfer request for the user's 09:14 batch."""
    cutoff = sim_datetime(trade_date, _PREOPEN_TRANSFER_TIME)
    if when >= cutoff:
        broker.record_event(
            "main_action_ignored",
            trade_date=trade_date,
            minute_key=minute_key,
            action=_jsonable(action),
            reason="transfer_after_preopen_cutoff",
            transfer_cutoff=_PREOPEN_TRANSFER_TIME,
        )
        return False
    amount = _float_or_none(action.get("amount"))
    if amount is None or amount <= 0:
        broker.record_event(
            "main_action_ignored",
            trade_date=trade_date,
            minute_key=minute_key,
            action=_jsonable(action),
            reason="transfer_amount_not_positive",
            transfer_cutoff=_PREOPEN_TRANSFER_TIME,
        )
        return False
    pending.append(_PendingTransfer(seq=len(pending), action=dict(action), requested_at=when.isoformat()))
    broker.record_event(
        "transfer_requested",
        trade_date=trade_date,
        minute_key=minute_key,
        action=_jsonable(action),
        requested_at=when.isoformat(),
        confirm_time=_PREOPEN_TRANSFER_TIME,
    )
    return True


def _confirm_preopen_transfers(
    pending: list[_PendingTransfer],
    *,
    broker: SimBroker,
    trade_date: str,
    when: datetime,
) -> None:
    """Confirm queued same-day transfers once the simulated clock reaches 09:14."""
    if not pending or when < sim_datetime(trade_date, _PREOPEN_TRANSFER_TIME):
        return
    ready = sorted(pending, key=lambda item: item.seq)
    pending.clear()
    for item in ready:
        action = dict(item.action)
        amount = _float_or_none(action.get("amount"))
        if amount is None or amount <= 0:
            continue
        try:
            broker.transfer(
                amount,
                str(action.get("from_account", "")),
                str(action.get("to_account", "")),
                reason=str(action.get("reason") or "preopen_transfer"),
                order_id=str(action.get("order_id") or "") or None,
                submitted_at=sim_datetime(trade_date, _PREOPEN_TRANSFER_TIME).isoformat(),
            )
        except ValueError as exc:
            broker.record_event(
                "transfer_rejected",
                trade_date=trade_date,
                minute_key=_PREOPEN_TRANSFER_TIME,
                action=_jsonable(action),
                reason=str(exc),
                requested_at=item.requested_at,
            )


def _reject_unconfirmed_transfers(
    pending: list[_PendingTransfer],
    *,
    broker: SimBroker,
    trade_date: str,
) -> None:
    if not pending:
        return
    ready = sorted(pending, key=lambda item: item.seq)
    pending.clear()
    for item in ready:
        broker.record_event(
            "transfer_rejected",
            trade_date=trade_date,
            minute_key="",
            action=_jsonable(item.action),
            reason="preopen_transfer_not_confirmed",
            requested_at=item.requested_at,
            confirm_time=_PREOPEN_TRANSFER_TIME,
        )


def _submit_order(broker: SimBroker, action: dict[str, object], is_auction: bool, is_close_auction: bool = False) -> bool:
    """Translate a ``main()`` action into a Broker ``passorder`` submission.

    ``limit`` (a fixed price) routes to a 指定价 day order; otherwise a 对手价 market
    order. ``close`` has no official op: it resolves to the holding account's market exit at submission
    (the activation tick is also the match tick, so there is no drift window) —
    an explicit ``account`` wins, else the unique holder; ambiguous closes are
    ignored (the driver already rejects them at call time). ``direct_repay``
    follows the official 1102 (amount in CNY) convention and needs no bar.
    ``transfer`` is handled by the pre-open batch path before this order-submission
    translator. ``is_close_auction`` marks a 15:00 close-auction order so it fills
    at the activation bar's close.
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
    ts_code = str(action.get("ts_code", "")).strip()
    if name not in _ORDER_ACTIONS or not ts_code:
        return False
    limit = _float_or_none(action.get("limit")) if name != "close" else None
    if limit is not None and limit <= 0:
        broker.reject_submission(
            ts_code=ts_code,
            action=name,
            reason="invalid_limit_price",
            amount=action.get("amount"),
            submitted_at=str(action.get("submitted_at") or ""),
            order_id=str(action.get("order_id") or ""),
            strategy_reason=str(action.get("reason") or ""),
        )
        return True
    if name == "close":
        resolved = _resolve_close(broker, action, ts_code)
        if resolved is None:
            return False  # no holder, or ambiguous (both accounts hold the code)
        name, amount = resolved
        action = dict(action)
        action["amount"] = amount
    try:
        _, op_type = broker.account_op_for_action(name)
    except ValueError:
        return False
    shares, amount_reject = broker.validate_order_amount(name, ts_code, action.get("amount"))
    if amount_reject is not None:
        broker.reject_submission(
            ts_code=ts_code,
            action=name,
            reason=amount_reject,
            amount=action.get("amount"),
            submitted_at=str(action.get("submitted_at") or ""),
            order_id=str(action.get("order_id") or ""),
            strategy_reason=str(action.get("reason") or ""),
        )
        return True
    broker.passorder(
        op_type,
        1101,
        "",
        ts_code,
        prtype.FIX if limit is not None else prtype.PEER,
        limit or 0,
        shares,
        is_auction=is_auction,
        auction_close=is_close_auction,
        # A market order may now rest across printless bars: keep it reserving
        # buying power at its decision-time price estimate.
        reserve_price=_float_or_none(action.get("_reserve_price")),
        **order_kwargs,
    )
    return True


def _resolve_close(broker: SimBroker, action: dict[str, object], ts_code: str) -> tuple[str, int] | None:
    """Resolve a ``close`` verb to the holding account's exit op and full sellable
    amount: an explicit ``account`` wins, else the unique holder; None when no
    account holds the code or both do (the driver already rejects ambiguity)."""
    account = str(action.get("account") or "").strip().lower()
    if not account:
        holders = [
            acct for acct in ("stock", "credit")
            if broker.position_quantity(ts_code, account=acct) != 0
        ]
        if len(holders) != 1:
            return None
        account = holders[0]
    if account == "stock":
        name = "sell"
    elif broker.position_quantity(ts_code, account="credit") < 0:
        name = "cover"
    elif broker.financed_shares_outstanding(ts_code) > 0:
        name = "sell_repay"
    else:
        name = "credit_sell"
    return name, broker.sellable_quantity(account, ts_code)


# Buy-side vs sell-side order verbs for the after-hours price-validity rule
# (收盘价高于申报买价或低于申报卖价的申报无效).
_AFTERHOURS_BUY_ACTIONS = {"buy", "credit_buy", "cover"}


def _execute_afterhours_action(broker: SimBroker, action: dict[str, object], *, trade_date: str, minute_key: str) -> bool:
    """Settle one after-hours fixed-price action immediately at the day's close.

    盘后固定价格交易 (15:05-15:30) matches at the official closing price only, so
    there is no order book passage: eligible orders settle at once via
    ``broker.execute`` under the full constraint set (suspension — a stock still
    suspended at 15:00 has no after-hours session, price limits as a conservative
    counterparty assumption at a limit-locked close, T+1, cash/margin, lots). A
    limit worse than the close is an invalid submission per the rule. Board/date
    eligibility follows ``afterhours_available``; opening new leveraged positions
    (``short``/``fin_buy``) is conservatively unsupported — real availability of
    融资/融券开仓 through the after-hours session is unverified. Returns False for
    unsupported/malformed actions (caller records ``main_action_ignored``)."""
    name = _action_name(action)
    if name == "direct_repay":
        return _submit_order(broker, action, False)  # cash op, settles immediately
    ts_code = str(action.get("ts_code", "")).strip()
    if name not in _ORDER_ACTIONS or not ts_code:
        return False
    reject_kwargs = {
        "ts_code": ts_code,
        "amount": action.get("amount"),
        "submitted_at": str(action.get("submitted_at") or ""),
        "order_id": str(action.get("order_id") or "") or None,
        "strategy_reason": str(action.get("reason") or ""),
    }
    limit = _float_or_none(action.get("limit")) if name != "close" else None
    if limit is not None and limit <= 0:
        broker.reject_submission(action=name, reason="invalid_limit_price", **reject_kwargs)
        return True
    amount = action.get("amount")
    resolved_close = False
    if name == "close":
        resolved = _resolve_close(broker, action, ts_code)
        if resolved is None:
            return False
        name, amount = resolved
        resolved_close = True
        reject_kwargs["amount"] = amount
    if not afterhours_available(ts_code, trade_date):
        broker.reject_submission(action=name, reason="afterhours_not_available", **reject_kwargs)
        return True
    if name in {"short", "fin_buy"}:
        broker.reject_submission(action=name, reason="afterhours_op_unsupported", **reject_kwargs)
        return True
    if not resolved_close:
        # Same amount contract as every other submission path: a missing/invalid
        # amount is a reject, never an implicit full liquidation at the close.
        # A broker-resolved close amount skips this gate so _reduce can report
        # the precise blocker (e.g. t_plus_one_no_sellable) instead.
        shares, amount_reject = broker.validate_order_amount(name, ts_code, amount)
        if amount_reject is not None:
            broker.reject_submission(action=name, reason=amount_reject, **reject_kwargs)
            return True
        amount = shares
    bar = broker.market.bar(trade_date, ts_code)
    close = None
    if bar is not None and pd.notna(bar.get("close")):
        close = float(bar["close"])
    if limit is not None and close is not None and (
        close > limit if name in _AFTERHOURS_BUY_ACTIONS else close < limit
    ):
        broker.reject_submission(action=name, reason="afterhours_price_invalid", **reject_kwargs)
        return True
    broker.execute(
        ts_code,
        name,
        trade_date=trade_date,
        raw_price=close,  # None -> missing_price reject inside execute
        amount=amount,
        time=minute_key,
        reason=str(action.get("reason") or name),
        price_label="afterhours_fixed",
        apply_slippage=False,
        order_id=str(action.get("order_id") or "") or None,
        submitted_at=str(action.get("submitted_at") or ""),
        limit_price=limit,
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
    # The driver emits canonical verbs only; unknown names fall through to the
    # _ORDER_ACTIONS membership checks and are recorded as ignored, not rewritten.
    return str(action.get("action", "")).lower().strip()


def _cancel_pending_order(
    broker: SimBroker,
    incoming: dict[int, list[tuple[dict[str, object], bool, bool]]],
    order_id: str,
    *,
    trade_date: str,
    minute_key: str,
    reason: str,
) -> bool:
    """Cancel a live working order or submit-lag order."""
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
    return False


def _incoming_cancel_block_reason(
    incoming: dict[int, list[tuple[dict[str, object], bool, bool]]],
    order_id: str,
    *,
    when: datetime,
) -> str | None:
    """Return the exchange phase that makes an in-flight auction order final.

    The submit-lag queue already carries the open-/close-auction flags needed for
    matching. Reuse those flags to enforce the corresponding no-cancel phases,
    without introducing a second order-state model.
    """
    if not order_id:
        return None
    open_auction_cancel_cutoff = 9 * 3600 + 25 * 60
    for items in incoming.values():
        for action, is_auction, is_close_auction in items:
            if str(action.get("order_id") or "") != order_id:
                continue
            if is_close_auction:
                return "close_auction_cancel_window_closed"
            if is_auction and _seconds_since_midnight(when) >= open_auction_cancel_cutoff:
                return "open_auction_cancel_window_closed"
            return None
    return None


def _pending_view(
    broker: SimBroker,
    incoming: dict[int, list[tuple[dict[str, object], bool, bool]]],
    *,
    now: datetime | None = None,
) -> dict[str, list[dict[str, object]]]:
    """Working orders the Agent can see via ``ctx.broker.pending(ts_code)``: the
    Broker's cancelable book plus decisions already submitted into the regular
    submit lag. Broker actions whose substep compute is not ready are not orders
    yet and are intentionally hidden."""
    records = [_pending_record(record, now=now) for record in broker.working_orders()]
    for items in incoming.values():
        for action, _is_auction, _is_close_auction in items:
            account = str(action.get("account") or "")
            op_type = action.get("op_type")
            action_name = _action_name(action)
            if action_name == "close":
                # Single authority: the same _resolve_close that submission uses
                # decides the pending view's account/op (the driver sends no hint).
                resolved = _resolve_close(broker, action, str(action.get("ts_code", "")))
                if resolved is not None:
                    account, op_type = broker.account_op_for_action(resolved[0])
            elif account not in {"stock", "credit"} or op_type is None:
                try:
                    account, op_type = broker.account_op_for_action(action_name)
                except ValueError:
                    pass
            records.append(
                _pending_record(
                    {
                        "order_id": action.get("order_id"),
                        "ts_code": str(action.get("ts_code", "")),
                        "action": action_name,
                        "account": account,
                        "op_type": op_type,
                        "order_volume": action.get("amount"),
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


def _incoming_reservation_records(
    broker: SimBroker,
    incoming: dict[int, list[tuple[dict[str, object], bool, bool]]]
) -> list[dict[str, object]]:
    """Submit-lag actions already accepted by the host but not yet in Broker._book."""
    records: list[dict[str, object]] = []
    for items in incoming.values():
        for action, _is_auction, _is_close_auction in items:
            record = dict(action)
            record.setdefault("status", "pending")
            record.setdefault("pending_stage", "submit_lag")
            action_name = _action_name(record)
            try:
                account, op_type = broker.account_op_for_action(action_name)
                record.setdefault("account", account)
                record.setdefault("op_type", op_type)
            except ValueError:
                pass
            records.append(record)
    return records


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


def _tick_price_for_code(minute_group: pd.DataFrame, ts_code: str) -> float | None:
    if minute_group.empty or not ts_code:
        return None
    rows = minute_group[minute_group["ts_code"].astype(str) == str(ts_code)]
    if rows.empty:
        return None
    row = rows.iloc[-1]
    for field in ("close", "open"):
        value = row.get(field)
        if value is not None and pd.notna(value):
            return float(value)
    return None


def _timeview_replay_frames(
    replay_dir: Path | None,
    replay_daily: pd.DataFrame,
    replay_intraday_1min: pd.DataFrame | None,
    replay_auction_results: pd.DataFrame | None,
) -> dict[str, pd.DataFrame]:
    """Replay-slot frames the Timeview rolls in. daily/intraday reuse the frames
    already loaded for the replay; events/macro/fundamentals are read from the
    slot directory when present (each carries a row-level ``available_at``)."""
    frames: dict[str, pd.DataFrame] = {"daily": replay_daily}
    if replay_intraday_1min is not None:
        frames["intraday_1min"] = replay_intraday_1min
    if replay_auction_results is not None:
        frames["auction"] = replay_auction_results
    if replay_dir is not None:
        end_date = str(replay_daily["trade_date"].astype(str).max())
        cutoff = f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:]}T23:59:59+08:00"
        for name, filename in (
            ("events", "events.parquet"),
            ("macro", "macro.parquet"),
            ("fundamentals", "fundamentals.parquet"),
            ("text_index", "text_index.parquet"),
        ):
            path = Path(replay_dir) / filename
            if path.exists():
                try:
                    frames[name] = pd.read_parquet(
                        path,
                        filters=[("available_at", "<=", cutoff)],
                    )
                except (KeyError, TypeError, ValueError, pa.ArrowNotImplementedError):
                    frame = pd.read_parquet(path)
                    if "available_at" in frame.columns:
                        available = pd.to_datetime(frame["available_at"], errors="coerce", utc=True)
                        frame = frame[available <= pd.Timestamp(cutoff).tz_convert("UTC")]
                    frames[name] = frame.reset_index(drop=True)
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
    pending_orders: list[dict[str, object]] | None = None,
    cur_datetime: str = "",
) -> dict[str, object]:
    # Columnar payload: full-universe list-of-dicts JSON dominated the per-tick
    # RPC cost (~1MB/tick at ~5.4k codes). The driver's _LazyBars keeps the
    # ctx.bars dict semantics while materializing only the codes a strategy
    # actually touches. Numeric columns ship as ONE little-endian float64
    # buffer each (base64 inside the JSON line, NaN encodes None): json floats
    # cost ~10ms/tick to serialize+parse at full universe, the packed buffer
    # ~3.5ms — about two minutes per full-quarter replay on every surface.
    if minute_group.empty:
        bars: dict[str, object] = {"ts_code": []}
    else:
        codes = [str(code) for code in minute_group["ts_code"].tolist()]
        bars = {"ts_code": codes}
        packed: dict[str, str] = {}
        for column in ("open", "high", "low", "close", "vol", "amount"):
            if column not in minute_group.columns:
                packed[column] = _PACKED_NAN_CACHE.buffer(len(codes))
                continue
            blob = _columnar_pack(minute_group[column])
            if blob is not None:
                packed[column] = blob
            else:
                # Legacy object/string columns keep the per-value JSON path.
                bars[column] = _columnar_float_values(minute_group[column])
        if packed:
            bars["packed_f64"] = packed
    return {
        "cur_date": str(trade_date),
        "cur_time": str(minute_key or ""),
        "cur_datetime": str(cur_datetime or ""),
        "account": {
            # Raw records: MainPolicyRunner._request already runs one _jsonable
            # walk over the whole state, so walking them here would double the work.
            "stock": broker.account_record("stock", pending_orders=pending_orders or ()),
            "credit": broker.account_record("credit", pending_orders=pending_orders or ()),
            "total_assets": float(broker.equity()),
            "risk_limits": {
                "max_total_holdings": broker.profile.max_total_holdings,
                "max_single_name_weight": broker.profile.max_single_name_weight,
                "maintenance_closeout_ratio": broker.profile.maintenance_closeout_ratio,
                "maintenance_withdraw_ratio": broker.profile.maintenance_withdraw_ratio,
            },
        },
        "positions": (
            broker.position_records("stock", pending_orders=pending_orders or ())
            + broker.position_records("credit", pending_orders=pending_orders or ())
        ),
        "debt_contracts": broker.get_debt_contract(),
        "bars": bars,
        "asof_dir": asof_dir,
        "asof_version": asof_version,
        "pending": pending or {},
    }


class _PackedNanCache:
    """Base64 all-NaN float64 buffers by length (missing bar columns reuse one
    encode per universe size instead of re-encoding every tick)."""

    def __init__(self) -> None:
        self._by_length: dict[int, str] = {}

    def buffer(self, length: int) -> str:
        cached = self._by_length.get(length)
        if cached is None:
            cached = base64.b64encode(np.full(length, np.nan, dtype="<f8").tobytes()).decode("ascii")
            if len(self._by_length) > 8:
                self._by_length.clear()
            self._by_length[length] = cached
        return cached


_PACKED_NAN_CACHE = _PackedNanCache()


def _columnar_pack(values: pd.Series) -> str | None:
    """One bar column as a base64 little-endian float64 buffer (NaN = None),
    or None when the column is not purely numeric (legacy fallback)."""
    if (
        pd.api.types.is_numeric_dtype(values.dtype)
        and not pd.api.types.is_complex_dtype(values.dtype)
    ):
        numeric = values.to_numpy(dtype="float64", na_value=float("nan"), copy=False)
        return base64.b64encode(numeric.astype("<f8", copy=False).tobytes()).decode("ascii")
    return None


def _columnar_float_values(values: pd.Series) -> list[float | None]:
    """Encode one bar column with a numeric fast path and legacy fallbacks."""
    if (
        pd.api.types.is_numeric_dtype(values.dtype)
        and not pd.api.types.is_complex_dtype(values.dtype)
    ):
        numeric = values.to_numpy(dtype="float64", na_value=float("nan"), copy=False)
        encoded = numeric.astype(object)
        encoded[pd.isna(numeric)] = None
        return encoded.tolist()
    return [_float_or_none(value) for value in values.tolist()]


def _float_or_none(value: object) -> float | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(result) else result
