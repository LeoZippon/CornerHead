"""backtest_tool: formal replay of the Agent's ``output/main.py``.

The Agent owns all trading logic. The Environment replays the region minute by
minute, calling ``main(ctx)`` each minute (serving optional ``nl()`` calls), and
the Broker applies every market constraint and records fills. The tool writes the
return statistics, the order log, and the NL audit trail.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import re
import shutil
import threading
import time
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

from autotrade.environment.artifacts import (
    ArtifactError,
    READONLY_FILES,
    artifact_hash,
    combined_artifact_hash,
    load_model_artifacts,
    load_strategy_artifact,
    model_artifact_hash,
)
from autotrade.environment.replay_stats import compute_return_stats
from autotrade.environment.broker import (
    BrokerProfile,
    auction_prints_by_date,
    load_corporate_actions_by_date,
    load_shortable_by_date,
    load_shortable_codes,
)
from autotrade.environment.main_ctx_engine import BacktestError, MainPolicyRunner, run_main_ctx_replay
from autotrade.environment.nl.service import StrategyNLService, cleanup_nl_rpc_files, prepare_nl_rpc_files
from autotrade.environment.replay_market import ParquetMinuteReplaySource
from autotrade.environment.identity import agent_visible_ref
from autotrade.environment.runtime import chmod_tree, new_id, sanitize_for_log, utc_now_iso
from autotrade.environment.snapshot import load_snapshot_manifest
from autotrade.environment.step_tree import StepTree
from autotrade.environment.style_analysis import replay_style_analysis

from .base import (
    ActionField,
    ActionSpec,
    PHASE_FROZEN,
    PHASE_TRAIN_VALID,
    SessionInterrupt,
    ToolContext,
    ToolError,
    agent_visible_tool_result,
)
from .modification_check import ModificationCheckTool

_FINAL_EVAL_WALL_CAP_MULTIPLIER = 3.0
_AGENT_MEMORY_ADVISORY_BYTES = 2 * 1024**3


class _ProcessPeakRSS:
    """Sample this host worker's resident set without touching replay semantics."""

    def __init__(self, interval_seconds: float = 0.1) -> None:
        self.interval_seconds = float(interval_seconds)
        self.peak_bytes = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "_ProcessPeakRSS":
        self._sample()
        self._thread = threading.Thread(target=self._run, daemon=True, name="backtest-rss-monitor")
        self._thread.start()
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        self._sample()

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            self._sample()

    def _sample(self) -> None:
        try:
            resident_pages = int(Path("/proc/self/statm").read_text(encoding="ascii").split()[1])
            resident = resident_pages * int(os.sysconf("SC_PAGE_SIZE"))
        except (OSError, ValueError, IndexError):
            return
        self.peak_bytes = max(self.peak_bytes, resident)

MODES = ("valid", "frozen_eval")


def agent_visible_backtest_result(summary: dict[str, object]) -> dict[str, object]:
    """Remove host-only filesystem coordinates from Agent-visible channels."""
    return agent_visible_tool_result(summary)


class BacktestTool:
    name = "backtest_tool"
    spec = ActionSpec(
        action="backtest",
        tool_name=name,
        description=(
            "Run the formal validation backtest by replaying output/main.py minute by minute. The "
            "Runner supplies validation mode; the tool auto-verifies the latest modification check "
            "when needed and writes a new results/valid_<idx>/ artifact. Real-wall caps abort the run: "
            "any single decision over backtest_max_seconds_per_decision is killed immediately, and a trade "
            "day over backtest_max_seconds_per_trading_day aborts (BacktestError, not accept-eligible). So "
            "first run a small replay_window pass, read the returned replay_wall_seconds + replayed_trade_days, "
            "extrapolate to the full validation period, and only run the full backtest once it fits. "
            "replay_window passes are debug-only (complete_validation=false): freezing and finish_fold "
            "both require a successful full-window run of the current artifacts. Probes return ONLY "
            "runtime/lifecycle statistics - no returns, fills or attribution are produced (the probed "
            "window is the strategy's future). Probe NL content is withheld, so its wall time is NOT "
            "representative when nl_outcome_counts contains withheld_probe; nl_cost still reports a "
            "full-window logical-call projection and structural provider-call upper bound."
        ),
        fields=(
            ActionField(
                "replay_window",
                "integer",
                required=False,
                min_value=1,
                description=(
                    "Optional: replay the first N strategy trade days plus one separate liquidation day "
                    "for a fast debug check "
                    "(non-accept-eligible). Use it to measure runtime (replay_wall_seconds / "
                    "replayed_trade_days) only when runtime_representative=true; NL-withheld Probe "
                    "timing cannot be extrapolated, though nl_cost call-count projections remain usable. The "
                    "default full run is the only result eligible for acceptance/freeze."
                ),
            ),
        ),
        read_only=False,
        destructive=False,
        concurrency_safe=False,
        allowed_modes=("fold",),
    )

    def __init__(self, ctx: ToolContext) -> None:
        self.ctx = ctx
        self._backtest_started = False
        self._backtest_started_monotonic: float | None = None
        self._probe_active = False
        self._public_nl_tmp: Path | None = None
        self._tool_started_monotonic: float | None = None
        self._data_load: dict[str, object] = {}

    def run(
        self, *, mode: str, result_name: str | None = None, replay_window: int | None = None
    ) -> dict[str, object]:
        if mode not in MODES:
            raise ToolError(f"unsupported backtest mode: {mode}")
        if not self.ctx.extra.get("allow_backtest", True):
            raise ToolError("formal backtests are not allowed in this meta-learning run")
        if mode == "valid":
            self.ctx.require_phase(PHASE_TRAIN_VALID, tool=self.name)
            self.ctx.require_writable(tool=self.name)
        else:
            self.ctx.require_phase(PHASE_FROZEN, tool=self.name)
            replay_window = None  # frozen_eval always replays the full region
        self._probe_active = replay_window is not None
        self._tool_started_monotonic = time.monotonic()
        self._backtest_started = False
        self._backtest_started_monotonic = None
        self._public_nl_tmp = None
        self._data_load = {}
        if mode == "frozen_eval":
            seal_factory = getattr(self.ctx.executor, "formal_seal_factory", None)
            if callable(seal_factory):
                seal_factory()
        guard_factory = getattr(self.ctx.executor, "formal_guard_factory", None)
        guard = guard_factory() if callable(guard_factory) else contextlib.nullcontext()
        # Docker formal runs freeze the development container for the complete
        # tool call: validation, replay, hashing, manifest publication and Step
        # snapshot all observe one immutable output/models generation.
        with guard:
            try:
                return self._execute(mode=mode, result_name=result_name, replay_window=replay_window)
            except (BacktestError, ArtifactError) as exc:
                public_error = self._public_failure(str(exc), source=exc)
                self._record_failure(
                    mode, str(public_error), error_record=public_error.to_record()
                )
                raise public_error from exc
            except ToolError as exc:
                # A pre-flight rejection happens before backtest_start, so no
                # bracket is open. Post-start failures must terminate the audit;
                # Probe details remain host-only regardless of exception class.
                public_error = self._public_failure(str(exc), source=exc)
                if self._backtest_started:
                    self._record_failure(
                        mode,
                        str(public_error),
                        status="error",
                        error_record=public_error.to_record(),
                    )
                if public_error is not exc:
                    raise public_error from exc
                raise
            except SessionInterrupt as exc:
                # Researcher stop/ask control flow is not a strategy failure and
                # must reach the session loop unchanged. If the replay bracket is
                # already open, still close its host audit record first.
                if self._backtest_started:
                    public_error = self._public_failure(
                        f"{type(exc).__name__}: {exc}", source=exc
                    )
                    self._record_failure(
                        mode,
                        str(public_error),
                        status="aborted",
                        error_record=public_error.to_record(),
                    )
                raise
            except Exception as exc:  # noqa: BLE001 - strategy/runtime failures are normalized
                detail = f"{type(exc).__name__}: {exc}"
                public_error = self._public_failure(detail, source=exc)
                if self._backtest_started:
                    self._record_failure(
                        mode,
                        str(public_error),
                        status="aborted",
                        error_record=public_error.to_record(),
                    )
                raise public_error from exc
            except BaseException as exc:  # noqa: BLE001 - preserve stop/interrupt semantics
                # KeyboardInterrupt/SystemExit must propagate, but their
                # potentially strategy-derived text is never written publicly.
                detail = f"{type(exc).__name__}: {exc}"
                public_error = self._public_failure(detail, source=exc)
                if self._backtest_started:
                    self._record_failure(
                        mode,
                        str(public_error),
                        status="aborted",
                        error_record=public_error.to_record(),
                    )
                raise
            finally:
                if self._public_nl_tmp is not None and self._public_nl_tmp.exists():
                    shutil.rmtree(self._public_nl_tmp, ignore_errors=True)

    def contract_check(self) -> dict[str, object]:
        """finish_fold's light check: artifact loads and main(ctx) is defined."""
        guard_factory = getattr(self.ctx.executor, "formal_guard_factory", None)
        guard = guard_factory() if callable(guard_factory) else contextlib.nullcontext()
        with guard:
            artifact = load_strategy_artifact(self.ctx.paths.agent_output)
            decision_time = str(self.ctx.manifest.require("valid_decision_time"))
            replay_granularity = (
                "minute"
                if _replay_minutes_available(self.ctx.paths.valid / "intraday_1min.parquet")
                else "daily"
            )
            snapshot_dir = self._resolved_snapshot()
            with _formal_artifacts_readonly(self.ctx.paths, restore_writable=not self.ctx.write_locked):
                with _formal_replay_execution(self.ctx) as (executor, runtime_dir, _rpc_agent):
                    with MainPolicyRunner(
                        executor,
                        self.ctx.paths,
                        timeout_seconds=float(self.ctx.manifest.get("per_call_timeout_seconds", 300)),
                        decision_time=decision_time,
                        replay_granularity=replay_granularity,
                        runtime_dir=runtime_dir,
                        snapshot_path=(
                            snapshot_dir
                            if getattr(executor, "formal_isolation", False)
                            else self.ctx.paths.snapshot
                        ),
                    ) as policy:
                        policy.validate_main()
        summary = {
            "tool": self.name,
            "tool_spec": self.spec.to_record(),
            "kind": "contract_check",
            "checked_at": utc_now_iso(),
            "status": "ok",
            "strategy_entry": "main",
            "artifact_files": len(artifact.files),
            "model_artifact_files": len(load_model_artifacts(self.ctx.paths.model_artifacts).files),
        }
        self.ctx.trace.emit("tool", summary, step_id=self.ctx.current_step_id)
        return summary

    def _execute(self, *, mode: str, result_name: str | None, replay_window: int | None = None) -> dict[str, object]:
        manifest = self.ctx.manifest
        modification_check = self._enforce_modification_check(mode)
        artifact = load_strategy_artifact(self.ctx.paths.agent_output)

        snapshot_dir = self._resolved_snapshot()
        self._verify_snapshot_binding(mode, snapshot_dir)
        decision_time = str(manifest.require("valid_decision_time" if mode == "valid" else "test_decision_time"))
        replay_dir = self.ctx.paths.valid if mode == "valid" else self.ctx.paths.test
        # A short debug window replays the first N strategy days plus one distinct
        # liquidation day; such a run is never accept-eligible.
        complete_validation = replay_window is None
        probe = not complete_validation
        data_load: dict[str, object] = {}
        self._data_load = data_load
        timeview_enabled = bool(manifest.get("timeview_enabled", manifest.get("rolling_asof_enabled", True)))
        minute_source: ParquetMinuteReplaySource | None = None
        full_strategy_trade_days: int | None = None
        if replay_window is not None:
            load_t0 = time.monotonic()
            requested_strategy_days = max(1, int(replay_window))
            all_replay_dates = _replay_trade_dates(replay_dir / "daily.parquet")
            full_strategy_trade_days = max(0, len(all_replay_dates) - 1)
            keep = all_replay_dates[: requested_strategy_days + 1]
            if len(keep) < requested_strategy_days + 1:
                raise ToolError(
                    f"replay_window={requested_strategy_days} requires "
                    f"{requested_strategy_days + 1} trade dates including liquidation; "
                    f"the replay region has only {len(keep)}"
                )
            data_load["catalog_seconds"] = round(time.monotonic() - load_t0, 6)
            load_t0 = time.monotonic()
            replay_daily = _read_replay_daily(replay_dir, trade_dates=keep)
            data_load["daily_seconds"] = round(time.monotonic() - load_t0, 6)
            load_t0 = time.monotonic()
            minute_source = _minute_replay_source(
                replay_dir,
                trade_dates=keep,
                include_timeview_rows=timeview_enabled,
            )
            data_load["minute_catalog_seconds"] = round(time.monotonic() - load_t0, 6)
            data_load["minutes_seconds"] = 0.0
            load_t0 = time.monotonic()
            replay_auction = _read_replay_auction(replay_dir, trade_dates=keep)
            data_load["auction_seconds"] = round(time.monotonic() - load_t0, 6)
        else:
            keep = None
            data_load["catalog_seconds"] = 0.0
            load_t0 = time.monotonic()
            replay_daily = _read_replay_daily(replay_dir)
            data_load["daily_seconds"] = round(time.monotonic() - load_t0, 6)
            load_t0 = time.monotonic()
            minute_source = _minute_replay_source(
                replay_dir,
                include_timeview_rows=timeview_enabled,
            )
            data_load["minute_catalog_seconds"] = round(time.monotonic() - load_t0, 6)
            data_load["minutes_seconds"] = 0.0
            load_t0 = time.monotonic()
            replay_auction = _read_replay_auction(replay_dir)
            data_load["auction_seconds"] = round(time.monotonic() - load_t0, 6)
        data_load.update(
            {
                "daily_rows": int(len(replay_daily)),
                "minute_rows": int(minute_source.selected_rows) if minute_source is not None else 0,
                "auction_rows": int(len(replay_auction)) if replay_auction is not None else 0,
                "daily_source_bytes": _path_bytes(replay_dir / "daily.parquet"),
                "minute_source_bytes": _path_bytes(replay_dir / "intraday_1min.parquet"),
                "auction_source_bytes": _path_bytes(replay_dir / "auction.parquet"),
            }
        )
        replay_granularity = "minute" if minute_source is not None else "daily"
        result_dir = self._planned_result_dir(mode, result_name)
        if result_dir.exists():
            raise ToolError(f"result directory already exists: {result_dir}")
        started_at = utc_now_iso()
        total_trade_days = int(replay_daily["trade_date"].nunique())
        strategy_trade_days = max(0, total_trade_days - 1)
        if full_strategy_trade_days is None:
            full_strategy_trade_days = strategy_trade_days
        exit_trade_days = 1 if total_trade_days else 0
        # Open the Agent-visible Valid bracket before synchronous replay so a
        # long run is observable. Frozen evaluation stays on host-only surfaces.
        if mode == "valid":
            self.ctx.trace.emit(
                "backtest_start",
                {
                    "tool": self.name, "mode": mode, "result_name": result_dir.name,
                    "complete_validation": complete_validation, "replay_window": replay_window,
                    "replay_granularity": replay_granularity, "total_trade_days": total_trade_days,
                    "strategy_trade_days": strategy_trade_days, "exit_trade_days": exit_trade_days,
                    "artifact_hash": artifact.artifact_hash, "started_at": started_at,
                },
                step_id=self.ctx.current_step_id,
            )
        self._backtest_started = True  # bracket open: any later failure must emit a terminal event
        self._backtest_started_monotonic = time.monotonic()

        environment_progress_hook = self.ctx.extra.get("environment_progress_hook")
        environment_replay_stage = str(self.ctx.extra.get("environment_replay_stage") or "frozen_test")
        if mode == "frozen_eval" and callable(environment_progress_hook):
            environment_progress_hook(
                environment_replay_stage,
                {"day_index": 0, "total_days": total_trade_days, "percent": 0.0, "elapsed_seconds": 0.0},
            )

        def _on_progress(date: str, idx: int, total: int, elapsed: float, orders: int) -> None:
            if mode == "frozen_eval":
                if callable(environment_progress_hook):
                    # Host status intentionally omits hidden dates, orders, NL
                    # activity and results; only runtime progress reaches the UI.
                    environment_progress_hook(
                        environment_replay_stage,
                        {
                            "day_index": idx,
                            "total_days": total,
                            "percent": round(100.0 * idx / total, 1) if total else 0.0,
                            "elapsed_seconds": round(elapsed, 1),
                        },
                    )
                return
            if probe:
                return  # do not stream progressive future-window/order feedback
            self.ctx.trace.emit(
                "backtest_progress",
                {
                    "tool": self.name, "mode": mode, "result_name": result_dir.name,
                    "trade_date": date, "day_index": idx, "total_days": total,
                    "percent": round(100.0 * idx / total, 1) if total else 0.0,
                    "elapsed_seconds": round(elapsed, 1), "orders_so_far": orders,
                },
                step_id=self.ctx.current_step_id,
            )

        def _on_nl_activity(activity: dict[str, object]) -> None:
            if probe or mode != "valid":
                return
            self.ctx.trace.emit(
                "backtest_activity",
                {
                    "tool": self.name,
                    "mode": mode,
                    "result_name": result_dir.name,
                    **activity,
                },
                step_id=self.ctx.current_step_id,
            )

        # The per-decision wall cap bounds one main(ctx) tick (its compute AND any nl()
        # calls within it); the per-trading-day cap bounds a day's cumulative compute.
        # These are real wall-clock, hence load-dependent, so the TIGHT iteration caps
        # apply only to agent-iteration validation. The final evals (frozen_eval:
        # per-fold test_000 and held-out) must complete and be reproducible — a
        # strategy that already fit the caps during validation must finish its final
        # eval (H2) — so they use a GENEROUS wall-clock backstop whose only job is to
        # kill a true hang, not to gate acceptance.
        # The per-substep budget/coverage checks are DIFFERENT from the coarse caps:
        # declared budgets advance sim time (orders fill at ready_at = tick + B), so
        # compute that misses its own declared budget would fill unrealistically
        # early. Valid, test and held-out enforce the SAME substep contract (engine
        # defaults) — an overrun result is invalid and never scored — otherwise
        # final scores are not comparable to validation. Only the coarse wall caps
        # below stay generous for frozen evals (kill true hangs, don't gate
        # acceptance).
        valid_decision_cap = float(manifest.get("backtest_max_seconds_per_decision", 1800))
        valid_per_day_cap = _optional_float(manifest.get("backtest_max_seconds_per_trading_day", 3600))
        if mode == "valid":
            decision_cap = valid_decision_cap
            per_day_cap = valid_per_day_cap
        else:
            decision_cap = float(
                manifest.get(
                    "backtest_final_eval_max_seconds_per_decision",
                    valid_decision_cap * _FINAL_EVAL_WALL_CAP_MULTIPLIER,
                )
            )
            per_day_cap = _optional_float(manifest.get("backtest_final_eval_max_seconds_per_trading_day"))
            if per_day_cap is None and valid_per_day_cap is not None:
                per_day_cap = valid_per_day_cap * _FINAL_EVAL_WALL_CAP_MULTIPLIER
        public_nl_audit = mode == "valid" and not probe
        nl_log_dir = (
            self.ctx.paths.workspace / f".{new_id('nl_tool')}"
            if public_nl_audit
            else _host_evidence_dir(self.ctx.paths.root, result_dir.name) / "nl_tool"
        )
        self._public_nl_tmp = nl_log_dir if public_nl_audit else None
        rss_monitor = _ProcessPeakRSS()
        with rss_monitor:
            try:
                with _formal_replay_execution(self.ctx) as (formal_executor, runtime_dir, rpc_agent):
                    requests_host, responses_host = prepare_nl_rpc_files(rpc_agent)
                    nl_service = StrategyNLService(
                        proxy=self.ctx.effective_nl_proxy,
                        snapshot_dir=snapshot_dir,
                        log_dir=nl_log_dir,
                        failure_policy=str(manifest.get("nl_failure_policy", "return_error_with_audit")),
                        # A single NL call gets a fraction of the decision cap so there is headroom
                        # for the surrounding compute before the per-decision wall cap kills the tick.
                        per_call_timeout_seconds=decision_cap * 0.8,
                        max_calls=_nl_call_budget(manifest, replay_daily),
                        # When the Timeview is on, ctx.nl() text rolls on the same nodes as the view.
                        replay_dir=replay_dir if timeview_enabled else None,
                        withhold_response=probe,
                        activity_callback=_on_nl_activity if public_nl_audit else None,
                    )
                    try:
                        profile = BrokerProfile(**_profile_kwargs(dict(manifest.require("broker_profile"))))
                        # Frozen decision-day set (agent snapshot view) is the fallback; the per-day
                        # map from the replay slot drives the broker's real same-day short gate (W7).
                        shortable = load_shortable_codes(snapshot_dir, _decision_date(decision_time))
                        shortable_by_date = load_shortable_by_date(replay_dir, trade_dates=keep)
                        with _formal_artifacts_readonly(self.ctx.paths, restore_writable=(mode == "valid")):
                            with MainPolicyRunner(
                                formal_executor,
                                self.ctx.paths,
                                timeout_seconds=decision_cap,
                                decision_time=decision_time,
                                replay_granularity=replay_granularity,
                                nl_service=nl_service,
                                requests_path=requests_host,
                                responses_path=responses_host,
                                decision_max_sim_minutes=_optional_float(manifest.get("decision_max_sim_minutes")),
                                runtime_dir=runtime_dir,
                                snapshot_path=(
                                    snapshot_dir
                                    if getattr(formal_executor, "formal_isolation", False)
                                    else self.ctx.paths.snapshot
                                ),
                            ) as policy:
                                policy.validate_main()
                                replay = run_main_ctx_replay(
                                    replay_daily,
                                    profile,
                                    shortable_codes=shortable,
                                    shortable_by_date=shortable_by_date,
                                    corporate_actions_by_date=load_corporate_actions_by_date(
                                        replay_dir, trade_dates=keep
                                    ),
                                    auction_prints_by_date=auction_prints_by_date(
                                        replay_auction if replay_auction is not None else pd.DataFrame()
                                    ),
                                    main_policy=policy,
                                    replay_minute_source=minute_source,
                                    replay_auction_results=replay_auction,
                                    auction_enabled=bool(manifest.get("auction_enabled", True)),
                                    auction_preopen_time=manifest.get("auction_preopen_time", "09:15"),
                                    auction_decision_time=str(manifest.get("auction_decision_time", "09:25")),
                                    auction_close_time=(manifest.get("auction_close_time", "14:57") or None),
                                    # No fallback default: manifests predating the knob replay
                                    # without the after-hours tick (frozen-eval reproducibility).
                                    afterhours_decision_time=(manifest.get("afterhours_decision_time") or None),
                                    execution_lag_bars=int(manifest.get("execution_lag_bars", 2)),
                                    offsession_tick_minutes=int(manifest.get("offsession_tick_minutes", 30)),
                                    intraday_decision_minutes=int(manifest.get("intraday_decision_minutes", 1)),
                                    max_seconds_per_trading_day=per_day_cap,
                                    timeview_enabled=timeview_enabled,
                                    snapshot_dir=snapshot_dir,
                                    replay_dir=replay_dir,
                                    on_progress=_on_progress,
                                )
                    finally:
                        try:
                            cleanup_nl_rpc_files(requests_host, responses_host)
                        finally:
                            nl_service.close()
            finally:
                if minute_source is not None:
                    minute_source.close()
                    data_load.update(minute_source.stats())
                data_load["host_peak_rss_bytes"] = int(rss_monitor.peak_bytes)
        data_load["host_peak_rss_bytes"] = int(rss_monitor.peak_bytes)
        replay.host_peak_rss_bytes = int(rss_monitor.peak_bytes) or None
        try:
            stats = compute_return_stats(replay)
            artifact = load_strategy_artifact(self.ctx.paths.agent_output)
            model_artifacts = load_model_artifacts(self.ctx.paths.model_artifacts)
            modification_check = self._refresh_modification_check_after_replay(
                mode, modification_check, artifact.artifact_hash, model_artifacts.artifact_hash
            )
            result_dir.mkdir(parents=True)
            nl_tool_dir: Path | None = None
            if public_nl_audit:
                nl_tool_dir = result_dir / "nl_tool"
                if nl_log_dir.exists():
                    shutil.move(str(nl_log_dir), str(nl_tool_dir))
                else:
                    nl_tool_dir.mkdir(parents=True, exist_ok=True)
        finally:
            if public_nl_audit and nl_log_dir.exists():
                shutil.rmtree(nl_log_dir, ignore_errors=True)

        # replay_window probes replay a FUTURE window relative to the decision
        # time: their financial outputs (returns, fill prices, EOD positions)
        # are lookahead for the Agent and were demonstrably used to tune against
        # the first-N-days P&L. Probes keep only run-cost/lifecycle statistics;
        # no financial artifact lands in the agent-readable results dir.
        style_payload: dict[str, object] = {}
        orders_path = None
        if not probe:
            order_records = replay.broker.get_trade_detail_data(
                account_type="STOCK", data_type="ORDER"
            ) + replay.broker.get_trade_detail_data(account_type="CREDIT", data_type="ORDER")
            orders_path = self._write_orders(result_dir, order_records)
            # Broker end-of-day positions per (date, account, ts_code, side): the
            # attribution ground truth (forced closes / bonus shares / hedged legs).
            position_records = replay.broker.positions_eod_records()
            if position_records:
                pd.DataFrame(position_records).to_parquet(result_dir / "positions_eod.parquet", index=False)
            # allow_nan=False: detailed_return.json feeds acceptance and the console;
            # a NaN metric must fail the replay here, not pass thresholds silently.
            (result_dir / "detailed_return.json").write_text(
                json.dumps(sanitize_for_log(stats), ensure_ascii=False, indent=2, sort_keys=True, default=str, allow_nan=False),
                encoding="utf-8",
            )
            # Barra-lite attribution, computed once per replay from frozen run
            # inputs only: replay-slot cross-section + slot index_daily benchmark +
            # snapshot universe industry. Every mode gets a sidecar (test/held-out
            # replays run after the Agent session); the compact block rides in the
            # tool result. Descriptive diagnostics, never an optimization target.
            style_payload = replay_style_analysis(
                replay_daily, position_records, stats, replay_dir=replay_dir, snapshot_dir=snapshot_dir
            )
            (result_dir / "style_analysis.json").write_text(
                json.dumps(sanitize_for_log(style_payload), ensure_ascii=False, indent=2, sort_keys=True, default=str),
                encoding="utf-8",
            )
        staging_audit = replay.state_staging_audit or []
        unmerged = sum(1 for record in staging_audit if not record.get("merged"))
        if staging_audit:
            state_audit_path = (
                result_dir / "state_staging_audit.json"
                if mode == "valid" and not probe
                else nl_log_dir.parent / "state_staging_audit.json"
            )
            state_audit_path.parent.mkdir(parents=True, exist_ok=True)
            state_audit_path.write_text(
                json.dumps(sanitize_for_log(staging_audit), ensure_ascii=False, indent=2, sort_keys=True, default=str),
                encoding="utf-8",
            )

        strategy_reject_category_counts = _strategy_reject_category_counts(stats["reject_counts"])
        nl_outcome_counts = dict(sorted(nl_service.outcome_counts.items()))
        nl_cost = nl_service.cost_summary()
        withheld_probe_nl = int(nl_outcome_counts.get("withheld_probe", 0)) if probe else 0
        runtime_representative = withheld_probe_nl == 0
        if probe:
            observed_days = max(1, int(stats.get("replayed_trade_days") or strategy_trade_days or 1))
            projected_calls = math.ceil(
                int(nl_service.calls) * int(full_strategy_trade_days) / observed_days
            )
            nl_cost.update(
                {
                    "probe_observed_strategy_days": observed_days,
                    "probe_full_strategy_days": int(full_strategy_trade_days),
                    "probe_projected_full_logical_calls": projected_calls,
                    "probe_projected_full_provider_call_upper_bound": (
                        projected_calls * int(nl_service.structural_provider_call_limit)
                    ),
                }
            )
        strategy_advisories = (
            list(modification_check.get("advisories") or [])
            if isinstance(modification_check, dict)
            else []
        )
        diagnostic_warnings = _diagnostic_warnings(stats, strategy_advisories=strategy_advisories)
        if withheld_probe_nl:
            diagnostic_warnings.insert(
                0,
                f"本次 Probe 的 {withheld_probe_nl} 次 ctx.nl() 均未执行真实文本检索/模型调用；"
                "replay_wall_seconds 只代表无 NL 退化路径，不可外推完整 Valid 耗时；"
                "nl_cost 仅给出按调用密度推算的结构性调用上界。",
            )
        summary = {
            "tool": self.name,
            "tool_spec": self.spec.to_record(),
            "mode": mode,
            "status": "ok",
            "artifact_hash": artifact.artifact_hash,
            "model_artifact_hash": model_artifacts.artifact_hash,
            "combined_artifact_hash": combined_artifact_hash(artifact.artifact_hash, model_artifacts.artifact_hash),
            "complete_validation": complete_validation,
            "replay_window": replay_window,
            "result_name": result_dir.name,
            # Probes write no result artifacts; a real path here sends the agent
            # to an empty directory at debugging time (audited failure mode).
            "result_path": None if probe else self.ctx.executor.map_path(result_dir),
            "host_result_path": str(result_dir),
            "decision_time": decision_time,
            "strategy_entry": "main",
            "model_artifact_files": len(model_artifacts.files),
            "model_artifact_bytes": model_artifacts.total_bytes,
            "replay_granularity": replay.granularity,
            "order_count": int(stats["order_count"]),
            "nl_calls": int(nl_service.calls),
            "nl_executed_calls": int(nl_service.executed_calls),
            "nl_cache_hits": int(nl_service.cache_hits),
            "nl_cache_misses": int(nl_service.cache_misses),
            "nl_outcome_counts": nl_outcome_counts,
            "nl_max_calls_per_backtest": nl_service.max_calls,
            "nl_cost": nl_cost,
            "trade_count": int(stats["trade_count"]),
            "unsubmitted_action_count": int(stats["unsubmitted_action_count"]),
            "unsubmitted_action_reason_counts": stats["unsubmitted_action_reason_counts"],
            "strategy_reject_count": sum(strategy_reject_category_counts.values()),
            "strategy_reject_category_counts": strategy_reject_category_counts,
            # Lifecycle signal (also on probes): how many positions the HOST
            # had to liquidate at region end because the strategy never exited.
            "host_exit_liquidation_count": stats["host_exit_liquidation_count"],
            "order_lifecycle": stats["order_lifecycle"],
            "strategy_exit_fill_count": int(stats["strategy_exit_fill_count"]),
            "modification_delta_summary": _modification_delta_summary(modification_check),
            "strategy_advisories": strategy_advisories,
            "probe_note": (
                "replay_window 前 N 个策略交易日 + 1 个独立退出日探针："
                "只返回运行成本与订单生命周期统计，"
                "收益指标与成交明细不生成（对未来窗口的 P&L 属于前视信息）；"
                "ctx.nl() 内容被 withheld 时，探针耗时不可外推完整 Valid，"
                "仅 nl_cost 的调用量结构投影可用于成本预检"
                if probe else None
            ),
            "runtime_representative": runtime_representative,
            "started_at": started_at,
            "finished_at": utc_now_iso(),
            "replay_wall_seconds": stats.get("replay_wall_seconds"),
            "replayed_trade_days": stats.get("replayed_trade_days"),
            "replayed_exit_days": stats.get("replayed_exit_days"),
            "substep_runtime": (
                _probe_substep_runtime(stats.get("substep_runtime"))
                if probe
                else stats.get("substep_runtime")
            ),
            "phase_seconds": stats.get("phase_seconds"),
            "agent_peak_rss_bytes": stats.get("agent_peak_rss_bytes"),
            "host_peak_rss_bytes": stats.get("host_peak_rss_bytes"),
            "data_load": data_load,
            # Measured before manifest/trace publication and optional StepTree
            # snapshot; the name makes that boundary explicit.
            "pre_publish_wall_seconds": round(
                time.monotonic() - self._tool_started_monotonic,
                3,
            ) if self._tool_started_monotonic is not None else None,
            "total_ticks": stats.get("total_ticks"),
            "intraday_ticks": stats.get("intraday_ticks"),
            "offsession_ticks": stats.get("offsession_ticks"),
            "decision_calls": stats.get("decision_calls"),
            "strategy_action_count": stats.get("strategy_action_count"),
            "state_staged_writes": len(staging_audit),
            "state_unmerged_writes": unmerged,
            # Advisory only: strategy defects remain Agent-owned and never
            # affect validation completeness, acceptance or freeze eligibility.
            "diagnostic_warnings": diagnostic_warnings,
        }
        if nl_tool_dir is not None:
            summary.update(
                {
                    "nl_tool_dir": self.ctx.executor.map_path(nl_tool_dir),
                    "host_nl_tool_dir": str(nl_tool_dir),
                }
            )
        if not probe:
            summary.update({
                "margin_secs_reject_count": stats["margin_secs_reject_count"],
                "max_holdings_reject_count": stats["max_holdings_reject_count"],
                "total_return": stats["total_return"],
                "long_return": stats["long_return"],
                "short_return": stats["short_return"],
                "sharpe": stats["sharpe"],
                "max_drawdown": stats["max_drawdown"],
                # Exit-day liquidation completeness: final equity is mark-to-market
                # either way; leftovers (suspension/limit-lock/T+1) are itemized in
                # detailed_return.json's unliquidated_positions.
                "liquidation_complete": stats["liquidation_complete"],
                "unliquidated_position_count": len(stats["unliquidated_positions"]),
                # Descriptive attribution vs 沪深300 (see style_analysis.json) —
                # interpretation aid, NOT an optimization target.
                "benchmark": style_payload.get("compact"),
                "orders_path": self.ctx.executor.map_path(orders_path) if orders_path else None,
                "host_orders_path": str(orders_path) if orders_path else None,
            })
        self.ctx.manifest.append_backtest_summary(summary)
        if mode == "valid":
            self.ctx.trace.emit(
                "backtest", agent_visible_backtest_result(summary), step_id=self.ctx.current_step_id
            )
        if mode == "valid" and complete_validation and self.ctx.manifest.get("step_tree_enabled"):
            StepTree(self.ctx.paths.steps).record_step(
                self.ctx.paths.agent_output,
                epoch_id=str(manifest.get("epoch_id", "")) or None,
                # Opaque the fold id so the step-tree node names the Agent reads
                # (steps/tree.txt|tree.json) never leak the held-out calendar period.
                fold_id=agent_visible_ref(manifest.require("fold_id"), prefix="fold_ref"),
                run_id=str(manifest.require("run_id")),
                result_name=result_dir.name,
                artifact_hash=artifact.artifact_hash,
                model_artifact_hash=model_artifacts.artifact_hash,
                model_artifact_root=self.ctx.paths.model_artifacts,
                metrics={k: stats[k] for k in ("total_return", "long_return", "short_return", "sharpe", "max_drawdown")},
                complete_validation=True,
                attachments={
                    "detailed_return.json": result_dir / "detailed_return.json",
                    "style_analysis.json": result_dir / "style_analysis.json",
                    # Full order log so rollback decisions can compare trades, not just curves.
                    **({"orders.parquet": orders_path} if orders_path is not None else {}),
                },
            )
        return summary

    def _write_orders(self, result_dir: Path, orders: list[dict[str, object]]) -> Path | None:
        if not orders:
            return None
        frame = pd.DataFrame(orders)
        if "source_artifacts" in frame.columns:
            frame["source_artifacts"] = frame["source_artifacts"].map(
                lambda value: json.dumps(list(value) if isinstance(value, (list, tuple)) else [], ensure_ascii=False)
            )
        orders_path = result_dir / "orders.parquet"
        frame.to_parquet(orders_path, index=False)
        return orders_path

    def _enforce_modification_check(self, mode: str) -> dict[str, object] | None:
        manifest = self.ctx.manifest
        current_hash = artifact_hash(self.ctx.paths.agent_output)
        current_model_hash = model_artifact_hash(self.ctx.paths.model_artifacts)
        if mode == "frozen_eval":
            frozen = str(manifest.require("frozen_strategy_artifact_hash"))
            if current_hash != frozen:
                raise ToolError(f"frozen artifact changed before frozen_eval: {current_hash} != {frozen}")
            frozen_model = str(manifest.get("frozen_model_artifact_hash", current_model_hash))
            if current_model_hash != frozen_model:
                raise ToolError(
                    f"frozen model artifacts changed before frozen_eval: {current_model_hash} != {frozen_model}"
                )
            return None
        last = manifest.get("last_modification_check")
        if (
            not isinstance(last, dict)
            or str(last.get("artifact_hash")) != current_hash
            or str(last.get("model_artifact_hash")) != current_model_hash
        ):
            last = ModificationCheckTool(self.ctx).run()
        if not last.get("allowed_to_backtest"):
            raise ToolError(f"modification check rejected the backtest: {last.get('reasons')}")
        return last

    def _refresh_modification_check_after_replay(
        self,
        mode: str,
        check: dict[str, object] | None,
        artifact_hash_value: str,
        model_hash_value: str,
    ) -> dict[str, object] | None:
        """Refresh the summary gate if replay generated inheritable model files."""
        if mode != "valid" or not isinstance(check, dict):
            return check
        if (
            str(check.get("artifact_hash")) == artifact_hash_value
            and str(check.get("model_artifact_hash")) == model_hash_value
        ):
            return check
        refreshed = ModificationCheckTool(self.ctx).run()
        if not refreshed.get("allowed_to_backtest"):
            raise ToolError(f"modification check rejected the replay artifacts: {refreshed.get('reasons')}")
        return refreshed

    def _resolved_snapshot(self) -> Path:
        link = self.ctx.paths.formal_snapshot
        if not link.exists():
            raise ToolError("formal /mnt/snapshot is not bound to a decision-input view")
        return link.resolve()

    def _verify_snapshot_binding(self, mode: str, snapshot_dir: Path) -> None:
        expected_key = "valid_decision_input" if mode == "valid" else "test_decision_input"
        expected = dict(self.ctx.manifest.require("snapshots")).get(expected_key)
        if not expected:
            raise ToolError(f"run manifest has no snapshot record for {expected_key}")
        actual = load_snapshot_manifest(snapshot_dir)
        if actual.get("snapshot_id") != expected.get("snapshot_id") or actual.get("snapshot_hash") != expected.get(
            "snapshot_hash"
        ):
            raise ToolError(
                f"bound snapshot does not match the pipeline record for {expected_key}: "
                f"{actual.get('snapshot_id')} != {expected.get('snapshot_id')}"
            )

    def _planned_result_dir(self, mode: str, result_name: str | None) -> Path:
        results_root = self.ctx.paths.results
        if result_name is None:
            prefix = "valid" if mode == "valid" else "test"
            existing = sorted(p.name for p in results_root.glob(f"{prefix}_*"))
            result_name = f"{prefix}_{len(existing):03d}"
        return results_root / result_name

    def _record_failure(
        self,
        mode: str,
        error: str,
        *,
        status: str = "error",
        error_record: dict[str, object] | None = None,
    ) -> None:
        summary = {
            "tool": self.name,
            "mode": mode,
            "status": status,
            "error": error,
            "finished_at": utc_now_iso(),
            "replay_wall_seconds": (
                time.monotonic() - self._backtest_started_monotonic
                if self._backtest_started_monotonic is not None else None
            ),
        }
        if error_record:
            summary.update(
                {
                    key: error_record[key]
                    for key in ("error_type", "reason", "retry_hint")
                    if error_record.get(key) not in (None, "")
                }
            )
        if self._data_load:
            summary["data_load"] = dict(self._data_load)
        self.ctx.manifest.append_backtest_summary(summary)
        if mode == "valid":
            self.ctx.trace.emit("backtest", summary, step_id=self.ctx.current_step_id)
        manifest = self.ctx.manifest
        if mode == "valid" and manifest.get("step_tree_enabled") and manifest.get("record_failed_attempts"):
            try:
                failed_hash = artifact_hash(self.ctx.paths.agent_output)
            except (OSError, ArtifactError):
                failed_hash = None
            StepTree(self.ctx.paths.steps).record_failed_attempt(
                epoch_id=str(manifest.get("epoch_id", "")) or None,
                fold_id=agent_visible_ref(manifest.require("fold_id"), prefix="fold_ref"),
                result_name=new_id("failed"),
                error=error,
                artifact_hash=failed_hash,
            )

    def _record_probe_failure_detail(self, error: str) -> None:
        try:
            evidence = _host_evidence_dir(self.ctx.paths.root, "failed_probe")
            (evidence / "error.txt").write_text(str(error), encoding="utf-8", errors="replace")
        except OSError:
            # Evidence durability must never reopen the public error channel.
            pass

    def _public_failure(self, detail: str, *, source: BaseException) -> ToolError:
        if not (self._probe_active and self._backtest_started):
            sanitized = detail.replace(str(self.ctx.paths.root), "/mnt")
            if isinstance(source, ToolError) and sanitized == detail:
                return source
            return ToolError(
                sanitized,
                error_type=getattr(source, "error_type", None) or "tool_error",
                reason=getattr(source, "reason", None),
                retry_hint=getattr(source, "retry_hint", None),
            )
        self._record_probe_failure_detail(detail)
        identity = _probe_error_identity(detail, source)
        message = "probe failed inside the isolated replay; raw strategy/runtime error text is host-only."
        if identity:
            # Class name + agent-code line only — the same low-bandwidth class of
            # signal as reject/lifecycle counters; messages stay withheld because
            # they can embed replay-window values.
            message += f" 异常定位（仅类名与策略文件行号）：{identity}。"
        return ToolError(
            message,
            error_type=getattr(source, "error_type", None) or "probe_runtime_error",
            reason=getattr(source, "reason", None),
            retry_hint=(
                getattr(source, "retry_hint", None)
                or "Reduce the strategy to the smallest failing control flow, then rerun a small probe."
            ),
        )


_TRACEBACK_FRAME_RE = re.compile(r'File "([^"]+)", line (\d+)')
_ERROR_CLASS_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*(?:Error|Exception|Interrupt))\b")
_WRAPPER_ERROR_CLASSES = frozenset({"ToolError", "BacktestError", "ArtifactError"})


def _probe_error_identity(detail: str, source: BaseException) -> str:
    """Exception class plus the agent's own strategy file:line, nothing else.

    Enough to aim the next probe (the audited alternative was minutes of blind
    guessing), while the raw message stays host-side: probe error text can embed
    replay-window values the agent must not see."""
    klass = ""
    location = ""
    for line in (detail or "").splitlines():
        match = _ERROR_CLASS_RE.match(line.strip())
        if match:
            klass = match.group(1)
    for filename, lineno in _TRACEBACK_FRAME_RE.findall(detail or ""):
        if "/output/" in filename and filename.endswith(".py"):
            location = f"{Path(filename).name}:{lineno}"
    if not klass:
        fallback = type(source).__name__
        if fallback not in _WRAPPER_ERROR_CLASSES and _ERROR_CLASS_RE.match(fallback):
            klass = fallback
    if not klass:
        return location
    return f"{klass} at {location}" if location else klass


def _profile_kwargs(record: dict[str, object]) -> dict[str, object]:
    fields = set(BrokerProfile.__dataclass_fields__)
    return {key: value for key, value in record.items() if key in fields}


def _replay_trade_dates(path: Path) -> tuple[str, ...]:
    dates = pd.read_parquet(path, columns=["trade_date"])
    if dates.empty:
        raise ToolError(f"replay daily data is empty: {path}")
    return tuple(sorted(dates["trade_date"].astype(str).unique()))


def _first_replay_trade_dates(path: Path, count: int) -> tuple[str, ...]:
    return _replay_trade_dates(path)[: max(1, int(count))]


def _trade_date_filters(trade_dates: tuple[str, ...] | None):
    return [("trade_date", "in", list(trade_dates))] if trade_dates else None


def _path_bytes(path: Path) -> int:
    try:
        return int(path.stat().st_size)
    except OSError:
        return 0


def _probe_substep_runtime(value: object) -> dict[str, dict[str, float]]:
    """Aggregate Probe cost without returning strategy-controlled step names."""
    if not isinstance(value, dict):
        return {}
    records = [record for record in value.values() if isinstance(record, dict)]
    if not records:
        return {}
    return {
        "aggregate": {
            "count": float(sum(float(record.get("count", 0.0) or 0.0) for record in records)),
            "total_real_wall_s": round(
                sum(float(record.get("total_real_wall_s", 0.0) or 0.0) for record in records),
                6,
            ),
            "max_real_wall_s": round(
                max(float(record.get("max_real_wall_s", 0.0) or 0.0) for record in records),
                6,
            ),
        }
    }


_STRATEGY_REJECT_REASONS = {
    "request_contract": frozenset({
        "invalid_amount",
        "amount_below_lot_size",
        "amount_not_lot_aligned",
        "slo_sell_requires_limit_price",
        "transfer_same_account",
        "transfer_amount_not_positive",
    }),
    "position_contract": frozenset({
        "opposite_side_position_open",
        "max_holdings_reached",
        "no_position",
        "no_fin_debt",
        "financed_shares_require_sell_repay",
        "t_plus_one_no_sellable",
        "amount_exceeds_sellable",
        "amount_below_minimum",
        "amount_exceeds_fin_debt",
    }),
    "account_capacity": frozenset({
        "insufficient_cash",
        "insufficient_bail_balance",
        "fin_quota_exceeded",
        "slo_quota_exceeded",
        "single_name_weight_cap",
        "credit_withdraw_blocked_by_maintenance",
    }),
}


def _strategy_reject_category_counts(value: object) -> dict[str, int]:
    """Reduce safe strategy-side rejects without exposing market eligibility."""
    if not isinstance(value, dict):
        return {}
    counts: dict[str, int] = {}
    for raw_reason, raw_count in value.items():
        reason = str(raw_reason)
        category = next(
            (name for name, reasons in _STRATEGY_REJECT_REASONS.items() if reason in reasons),
            None,
        )
        if category is None and reason.startswith(("unsupported_action:", "side_mismatch:")):
            category = "request_contract"
        if category is None:
            continue
        count = int(raw_count or 0)
        if count > 0:
            counts[category] = counts.get(category, 0) + count
    return dict(sorted(counts.items()))


def _diagnostic_warnings(
    stats: dict[str, object], *, strategy_advisories: list[dict[str, object]] | None = None
) -> list[str]:
    """Return non-blocking strategy feedback without exposing private state."""
    warnings: list[str] = []
    if int(stats.get("order_count") or 0) == 0:
        decisions = int(stats.get("decision_calls") or 0)
        actions = int(stats.get("strategy_action_count") or 0)
        warnings.append(
            f"Backtest called main(ctx) {decisions} times, received {actions} broker actions, and completed "
            "with zero orders. This is not rejected; inspect empty/date-stale plans and exceptions caught "
            "by strategy code before accepting the result."
        )
        if any(
            advisory.get("kind") == "blind_auction_price_lookup"
            for advisory in strategy_advisories or []
        ):
            warnings.append(
                "策略在 09:15/09:25 盲竞价分支读取 ctx.price()；这两个 Tick 不暴露价格，"
                "None 检查可能跳过全部下单。请用更早的参考价估算数量，或等 09:30 真实行情后再读取价格。"
            )
        broad_count = sum(
            1
            for advisory in strategy_advisories or []
            if advisory.get("kind") == "suppressed_broad_exception"
        )
        if broad_count:
            warnings.append(
                f"策略含 {broad_count} 处吞掉宽泛异常的分支，可能把运行错误变成空候选；"
                "调试阶段请让异常显式失败，定位后再保留有明确状态的降级处理。"
            )
    elif "trade_count" in stats and int(stats.get("trade_count") or 0) == 0:
        categories = _strategy_reject_category_counts(stats.get("reject_counts"))
        category_note = (
            " Safe strategy-side rejection categories: "
            + ", ".join(f"{name}={count}" for name, count in categories.items())
            + "."
            if categories else ""
        )
        warnings.append(
            f"Backtest created {int(stats.get('order_count') or 0)} orders but completed with zero trades."
            f"{category_note} Inspect order sizing, account/position state, and submission timing before "
            "accepting the result."
        )
    host_liq = int(stats.get("host_exit_liquidation_count") or 0)
    exit_fills = int(stats.get("strategy_exit_fill_count") or 0)
    if host_liq > 0 and exit_fills == 0 and int(stats.get("trade_count") or 0) > 0:
        warnings.append(
            f"退出路径检查：本次回放的 {int(stats.get('trade_count') or 0)} 笔平仓全部来自宿主区间末强制清仓"
            f"（host_exit_liquidation_count={host_liq}，策略自身退出成交=0）。策略从未主动卖出任何持仓——"
            "请核对持仓行键名（quantity/sellable_quantity）与卖出腿是否真正提交；若确为有意的持有到期设计，请在结论中说明理由。"
        )
    if stats.get("liquidation_complete") is False:
        leftovers = stats.get("unliquidated_positions") or []
        warnings.append(
            f"区间末仍有 {len(leftovers)} 个持仓未能清仓（停牌/跌停/T+1 等）；"
            "final_equity 已按市值计入，但实盘中这些头寸要继续承担隔夜风险。"
        )
    peak = int(stats.get("agent_peak_rss_bytes") or 0)
    if peak >= _AGENT_MEMORY_ADVISORY_BYTES:
        warnings.append(
            f"性能参考：本次回测策略进程峰值内存约 {peak / 1024**3:.1f} GiB，不影响验证结果。"
        )
    return warnings


def _read_replay_daily(
    replay_dir: Path,
    *,
    trade_dates: tuple[str, ...] | None = None,
) -> pd.DataFrame:
    return pd.read_parquet(
        replay_dir / "daily.parquet",
        filters=_trade_date_filters(trade_dates),
    )


def _replay_minutes_available(path: Path) -> bool:
    return path.exists() and pq.ParquetFile(path).metadata.num_rows > 0


def _minute_replay_source(
    replay_dir: Path,
    *,
    trade_dates: tuple[str, ...] | None = None,
    include_timeview_rows: bool,
) -> ParquetMinuteReplaySource | None:
    path = replay_dir / "intraday_1min.parquet"
    if not _replay_minutes_available(path):
        return None
    source = ParquetMinuteReplaySource(
        path,
        trade_dates=trade_dates,
        include_timeview_rows=include_timeview_rows,
    )
    if source.selected_rows == 0:
        source.close()
        return None
    return source


def _read_replay_auction(
    replay_dir: Path,
    *,
    trade_dates: tuple[str, ...] | None = None,
) -> pd.DataFrame | None:
    path = replay_dir / "auction.parquet"
    if not path.exists():
        return None
    # Legacy empty auction files have Arrow null-typed columns. Applying a
    # string trade_date predicate to that schema raises before pandas can
    # return the empty frame, so use the footer to take the existing no-data
    # path without scanning or filtering the file.
    if pq.ParquetFile(path).metadata.num_rows == 0:
        return None
    frame = pd.read_parquet(path, filters=_trade_date_filters(trade_dates))
    return None if frame.empty else frame


@contextlib.contextmanager
def _formal_replay_execution(ctx: ToolContext):
    """Yield an executor and isolated runtime for one formal strategy process."""
    factory = getattr(ctx.executor, "formal_factory", None)
    if not callable(factory):
        # LocalExecutor is intentionally a non-secure development/test mode.
        yield ctx.executor, None, ctx.paths.agent
        return
    runtime_dir = ctx.paths.root / "runtime" / "formal" / new_id("replay")
    rpc_agent = runtime_dir / "rpc_agent"
    runtime_dir.mkdir(parents=True, exist_ok=False)
    runtime_dir.chmod(0o755)
    try:
        with factory(runtime_dir) as executor:
            yield executor, runtime_dir, rpc_agent
    finally:
        shutil.rmtree(runtime_dir, ignore_errors=True)


def _host_evidence_dir(sandbox_root: Path, result_name: str) -> Path:
    root = Path(sandbox_root) / "runtime" / "host_evidence" / "backtests"
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    target = root / f"{result_name}_{new_id('audit')}"
    target.mkdir(parents=True, exist_ok=False)
    target.chmod(0o700)
    return target


def _decision_date(decision_time: str) -> str:
    return decision_time[:10].replace("-", "")


def _optional_int(value: object) -> int | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_float(value: object) -> float | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@contextlib.contextmanager
def _formal_artifacts_readonly(paths, *, restore_writable: bool):
    """Make output/models filesystem-read-only while formal replay imports/runs."""
    _make_formal_artifacts_readonly(paths)
    try:
        yield
    finally:
        if restore_writable:
            _restore_formal_artifacts_writable(paths)


def _make_formal_artifacts_readonly(paths) -> None:
    chmod_tree(paths.agent_output, file_mode=0o444, dir_mode=0o555)
    chmod_tree(paths.model_artifacts, file_mode=0o444, dir_mode=0o555)


def _restore_formal_artifacts_writable(paths) -> None:
    chmod_tree(paths.agent_output, file_mode=0o666, dir_mode=0o777)
    chmod_tree(paths.model_artifacts, file_mode=0o666, dir_mode=0o777)
    for relpath in READONLY_FILES:
        target = paths.agent_output / relpath
        if target.exists():
            target.chmod(0o444)


def _nl_call_budget(manifest, replay_daily) -> int | None:
    """System NL call quota for this backtest: a daily-average budget of
    ``nl_max_calls_per_decision_day`` over the replay's decision days (the final
    day is reserved for forced liquidation, not new decisions). An explicit
    ``nl_max_calls_per_backtest`` only tightens it (the min wins)."""
    per_day = _optional_int(manifest.get("nl_max_calls_per_decision_day"))
    decision_days = max(1, len(set(replay_daily["trade_date"].astype(str))) - 1)
    daily_total = per_day * decision_days if per_day is not None else None
    explicit = _optional_int(manifest.get("nl_max_calls_per_backtest"))
    caps = [c for c in (daily_total, explicit) if c is not None]
    return min(caps) if caps else None


def _modification_delta_summary(check: object) -> dict[str, object] | None:
    if not isinstance(check, dict):
        return None
    delta = check.get("delta")
    if not isinstance(delta, dict):
        return None
    summary = {
        "changed_file_count": delta.get("changed_file_count"),
        "diff_lines": delta.get("diff_lines"),
        "code_diff_lines": delta.get("code_diff_lines"),
        "total_files": delta.get("total_files"),
        "total_bytes": delta.get("total_bytes"),
    }
    model_delta = check.get("model_delta")
    if isinstance(model_delta, dict):
        summary.update(
            {
                "model_changed_file_count": model_delta.get("changed_file_count"),
                "model_total_files": model_delta.get("total_files"),
                "model_total_bytes": model_delta.get("total_bytes"),
            }
        )
    return summary
