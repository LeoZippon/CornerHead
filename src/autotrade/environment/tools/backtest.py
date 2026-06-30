"""backtest_tool: formal replay of the Agent's ``output/main.py``.

The Agent owns all trading logic. The Environment replays the region minute by
minute, calling ``main(ctx)`` each minute (serving optional ``nl()`` calls), and
the Broker applies every market constraint and records fills. The tool writes the
return statistics, the order log, and the NL audit trail.
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import pandas as pd

from autotrade.environment.artifacts import (
    ArtifactError,
    artifact_hash,
    combined_artifact_hash,
    load_model_artifacts,
    load_strategy_artifact,
    model_artifact_hash,
)
from autotrade.environment.backtest_engine import BacktestError, compute_return_stats
from autotrade.environment.broker import BrokerProfile, load_shortable_by_date, load_shortable_codes
from autotrade.environment.main_ctx_engine import MainPolicyRunner, run_main_ctx_replay
from autotrade.environment.nl.context import build_company_contexts
from autotrade.environment.nl.engine import NLSubAgentConfig, NLSubAgentEngine, TextRetriever
from autotrade.environment.runtime import new_id, sanitize_for_log, utc_now_iso
from autotrade.environment.snapshot import load_snapshot_manifest
from autotrade.environment.step_tree import StepTree

from .base import ActionField, ActionSpec, PHASE_FROZEN, PHASE_TRAIN_VALID, ToolContext, ToolError
from .modification_check import ModificationCheckTool

MODES = ("valid", "frozen_eval")


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
            "extrapolate to the full validation period, and only run the full backtest once it fits."
        ),
        fields=(
            ActionField(
                "replay_window",
                "integer",
                required=False,
                min_value=2,
                description=(
                    "Optional: replay only the first N trade days of the region for a fast debug check "
                    "(non-accept-eligible). Use it to measure runtime (replay_wall_seconds / "
                    "replayed_trade_days) and extrapolate the full run's cost before launching it. The "
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
        self._backtest_started = False
        try:
            return self._execute(mode=mode, result_name=result_name, replay_window=replay_window)
        except (BacktestError, ArtifactError) as exc:
            self._record_failure(mode, str(exc))
            raise ToolError(str(exc)) from exc
        except ToolError as exc:
            # A pre-flight rejection (dup result dir, modification-check/snapshot mismatch)
            # happens before backtest_start, so no bracket is open and we record nothing.
            # But the post-replay modification refresh can also raise ToolError AFTER
            # backtest_start — that must close the bracket with a terminal event.
            if self._backtest_started:
                self._record_failure(mode, str(exc), status="error")
            raise
        except BaseException as exc:  # noqa: BLE001 - guarantee a terminal audit event on any abort
            # An external/abort path (timeout kill, KeyboardInterrupt) would otherwise
            # leave the trace open on an in-flight backtest with no outcome.
            self._record_failure(mode, f"{type(exc).__name__}: {exc}", status="aborted")
            raise

    def contract_check(self) -> dict[str, object]:
        """finish_fold's light check: artifact loads and main(ctx) is defined."""
        artifact = load_strategy_artifact(self.ctx.paths.agent_output)
        decision_time = str(self.ctx.manifest.require("valid_decision_time"))
        replay_minutes = _read_replay_minutes(self.ctx.paths.valid)
        replay_granularity = "minute" if replay_minutes is not None else "daily"
        with MainPolicyRunner(
            self.ctx.executor,
            self.ctx.paths,
            timeout_seconds=float(self.ctx.manifest.get("per_call_timeout_seconds", 300)),
            decision_time=decision_time,
            replay_granularity=replay_granularity,
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
        replay_daily = pd.read_parquet(replay_dir / "daily.parquet")
        replay_minutes = _read_replay_minutes(replay_dir)
        # A short debug window replays only the first N trade days; such a run is
        # never accept-eligible (complete_validation=False).
        complete_validation = replay_window is None
        if replay_window is not None:
            keep = set(sorted(replay_daily["trade_date"].astype(str).unique())[: max(2, int(replay_window))])
            replay_daily = replay_daily[replay_daily["trade_date"].astype(str).isin(keep)]
            if replay_minutes is not None:
                replay_minutes = replay_minutes[replay_minutes["trade_date"].astype(str).isin(keep)]
                replay_minutes = None if replay_minutes.empty else replay_minutes
        replay_granularity = "minute" if replay_minutes is not None else "daily"
        result_dir = self._planned_result_dir(mode, result_name)
        if result_dir.exists():
            raise ToolError(f"result directory already exists: {result_dir}")
        started_at = utc_now_iso()
        total_trade_days = int(replay_daily["trade_date"].nunique())
        # Open the audit bracket before the synchronous replay; without a start event a
        # long backtest that is later killed leaves no trace of having run.
        self.ctx.trace.emit(
            "backtest_start",
            {
                "tool": self.name, "mode": mode, "result_name": result_dir.name,
                "complete_validation": complete_validation, "replay_window": replay_window,
                "replay_granularity": replay_granularity, "total_trade_days": total_trade_days,
                "artifact_hash": artifact.artifact_hash, "started_at": started_at,
            },
            step_id=self.ctx.current_step_id,
        )
        self._backtest_started = True  # bracket open: any later failure must emit a terminal event

        def _on_progress(date: str, idx: int, total: int, elapsed: float, orders: int) -> None:
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

        # The per-decision wall cap bounds one main(ctx) tick (its compute AND any nl()
        # calls within it); the per-trading-day cap bounds a day's cumulative compute.
        # These are real wall-clock, hence load-dependent, so the TIGHT iteration caps
        # apply only to agent-iteration validation. The final evals (frozen_eval:
        # per-fold test_000 and held-out) must complete and be reproducible — a
        # strategy that already fit the caps during validation must finish its final
        # eval (H2) — so they use a GENEROUS wall-clock backstop whose only job is to
        # kill a true hang, not to gate acceptance.
        if mode == "valid":
            decision_cap = float(manifest.get("backtest_max_seconds_per_decision", 180))
            per_day_cap = _optional_float(manifest.get("backtest_max_seconds_per_trading_day"))
        else:
            decision_cap = float(manifest.get("backtest_final_eval_max_seconds_per_decision", 900))
            per_day_cap = _optional_float(manifest.get("backtest_final_eval_max_seconds_per_trading_day"))
        tmp_nl_dir = self.ctx.paths.workspace / f".{new_id('nl_tool')}"
        requests_host = self.ctx.paths.workspace / f".{new_id('nl_requests')}.jsonl"
        responses_host = self.ctx.paths.workspace / f".{new_id('nl_responses')}.jsonl"
        requests_host.write_text("", encoding="utf-8")
        responses_host.write_text("", encoding="utf-8")

        timeview_enabled = bool(manifest.get("timeview_enabled", manifest.get("rolling_asof_enabled", True)))
        nl_service = _StrategyNLService(
            proxy=self.ctx.effective_nl_proxy,
            snapshot_dir=snapshot_dir,
            log_dir=tmp_nl_dir,
            failure_policy=str(manifest.get("nl_failure_policy", "return_error_with_audit")),
            # A single NL call gets a fraction of the decision cap so there is headroom
            # for the surrounding compute before the per-decision wall cap kills the tick.
            per_call_timeout_seconds=decision_cap * 0.8,
            max_calls=_nl_call_budget(manifest, replay_daily),
            # When the Timeview is on, ctx.nl() text rolls on the same nodes as the view.
            replay_dir=replay_dir if timeview_enabled else None,
        )
        try:
            profile = BrokerProfile(**_profile_kwargs(dict(manifest.require("broker_profile"))))
            # Frozen decision-day set (agent snapshot view) is the fallback; the per-day
            # map from the replay slot drives the broker's real same-day short gate (W7).
            shortable = load_shortable_codes(snapshot_dir, _decision_date(decision_time))
            shortable_by_date = load_shortable_by_date(replay_dir)
            with MainPolicyRunner(
                self.ctx.executor,
                self.ctx.paths,
                timeout_seconds=decision_cap,
                decision_time=decision_time,
                replay_granularity=replay_granularity,
                nl_service=nl_service,
                requests_path=requests_host,
                responses_path=responses_host,
                decision_max_sim_minutes=_optional_float(manifest.get("decision_max_sim_minutes")),
            ) as policy:
                policy.validate_main()
                replay = run_main_ctx_replay(
                    replay_daily,
                    profile,
                    shortable_codes=shortable,
                    shortable_by_date=shortable_by_date,
                    main_policy=policy,
                    replay_intraday_1min=replay_minutes,
                    auction_enabled=bool(manifest.get("auction_enabled", True)),
                    auction_preopen_time=manifest.get("auction_preopen_time", "09:15"),
                    auction_decision_time=str(manifest.get("auction_decision_time", "09:25")),
                    auction_close_time=(manifest.get("auction_close_time") or None),
                    execution_lag_bars=int(manifest.get("execution_lag_bars", 2)),
                    offsession_tick_minutes=int(manifest.get("offsession_tick_minutes", 0) or 0),
                    max_seconds_per_trading_day=per_day_cap,
                    timeview_enabled=timeview_enabled,
                    snapshot_dir=snapshot_dir,
                    replay_dir=replay_dir,
                    on_progress=_on_progress,
                )
            stats = compute_return_stats(replay)
            artifact = load_strategy_artifact(self.ctx.paths.agent_output)
            model_artifacts = load_model_artifacts(self.ctx.paths.model_artifacts)
            modification_check = self._refresh_modification_check_after_replay(
                mode, modification_check, artifact.artifact_hash, model_artifacts.artifact_hash
            )
            result_dir.mkdir(parents=True)
            nl_tool_dir = result_dir / "nl_tool"
            if tmp_nl_dir.exists():
                shutil.move(str(tmp_nl_dir), str(nl_tool_dir))
            else:
                nl_tool_dir.mkdir(parents=True, exist_ok=True)
        finally:
            if tmp_nl_dir.exists():
                shutil.rmtree(tmp_nl_dir, ignore_errors=True)
            requests_host.unlink(missing_ok=True)
            responses_host.unlink(missing_ok=True)

        orders_path = self._write_orders(result_dir, replay.broker.query_stock_orders())
        (result_dir / "detailed_return.json").write_text(
            json.dumps(sanitize_for_log(stats), ensure_ascii=False, indent=2, sort_keys=True, default=str),
            encoding="utf-8",
        )
        staging_audit = replay.state_staging_audit or []
        unmerged = sum(1 for record in staging_audit if not record.get("merged"))
        if staging_audit:
            (result_dir / "state_staging_audit.json").write_text(
                json.dumps(sanitize_for_log(staging_audit), ensure_ascii=False, indent=2, sort_keys=True, default=str),
                encoding="utf-8",
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
            "result_path": self.ctx.executor.map_path(result_dir),
            "host_result_path": str(result_dir),
            "decision_time": decision_time,
            "strategy_entry": "main",
            "model_artifact_files": len(model_artifacts.files),
            "model_artifact_bytes": model_artifacts.total_bytes,
            "replay_granularity": replay.granularity,
            "order_count": int(stats["order_count"]),
            "nl_calls": int(nl_service.calls),
            "nl_max_calls_per_backtest": nl_service.max_calls,
            "trade_count": int(stats["trade_count"]),
            "total_return": stats["total_return"],
            "long_return": stats["long_return"],
            "short_return": stats["short_return"],
            "sharpe": stats["sharpe"],
            "max_drawdown": stats["max_drawdown"],
            "margin_secs_reject_count": stats["margin_secs_reject_count"],
            "max_holdings_reject_count": stats["max_holdings_reject_count"],
            "orders_path": self.ctx.executor.map_path(orders_path) if orders_path else None,
            "host_orders_path": str(orders_path) if orders_path else None,
            "nl_tool_dir": self.ctx.executor.map_path(nl_tool_dir),
            "host_nl_tool_dir": str(nl_tool_dir),
            "modification_delta_summary": _modification_delta_summary(modification_check),
            "started_at": started_at,
            "finished_at": utc_now_iso(),
            "replay_wall_seconds": stats.get("replay_wall_seconds"),
            "replayed_trade_days": stats.get("replayed_trade_days"),
            "substep_runtime": stats.get("substep_runtime"),
            "phase_seconds": stats.get("phase_seconds"),
            "total_ticks": stats.get("total_ticks"),
            "intraday_ticks": stats.get("intraday_ticks"),
            "offsession_ticks": stats.get("offsession_ticks"),
            "state_staged_writes": len(staging_audit),
            "state_unmerged_writes": unmerged,
        }
        self.ctx.manifest.append_backtest_summary(summary)
        self.ctx.trace.emit("backtest", summary, step_id=self.ctx.current_step_id)
        if mode == "valid" and complete_validation and self.ctx.manifest.get("step_tree_enabled"):
            StepTree(self.ctx.paths.steps).record_step(
                self.ctx.paths.agent_output,
                epoch_id=str(manifest.get("epoch_id", "")) or None,
                fold_id=str(manifest.require("fold_id")),
                result_name=result_dir.name,
                artifact_hash=artifact.artifact_hash,
                model_artifact_hash=model_artifacts.artifact_hash,
                model_artifact_root=self.ctx.paths.model_artifacts,
                metrics={k: stats[k] for k in ("total_return", "long_return", "short_return", "sharpe", "max_drawdown")},
                complete_validation=True,
                attachments={"detailed_return.json": result_dir / "detailed_return.json"},
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
        link = self.ctx.paths.snapshot
        if not link.exists():
            raise ToolError("/mnt/snapshot is not bound to a decision-input view")
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

    def _record_failure(self, mode: str, error: str, *, status: str = "error") -> None:
        summary = {
            "tool": self.name,
            "mode": mode,
            "status": status,
            "error": error,
            "finished_at": utc_now_iso(),
        }
        self.ctx.manifest.append_backtest_summary(summary)
        self.ctx.trace.emit("backtest", summary, step_id=self.ctx.current_step_id)
        manifest = self.ctx.manifest
        if mode == "valid" and manifest.get("step_tree_enabled") and manifest.get("record_failed_attempts"):
            try:
                failed_hash = artifact_hash(self.ctx.paths.agent_output)
            except (OSError, ArtifactError):
                failed_hash = None
            StepTree(self.ctx.paths.steps).record_failed_attempt(
                epoch_id=str(manifest.get("epoch_id", "")) or None,
                fold_id=str(manifest.require("fold_id")),
                result_name=new_id("failed"),
                error=error,
                artifact_hash=failed_hash,
            )


class _StrategyNLService:
    def __init__(
        self,
        *,
        proxy,
        snapshot_dir: Path,
        log_dir: Path,
        failure_policy: str,
        per_call_timeout_seconds: float,
        max_calls: int | None = None,
        replay_dir: Path | None = None,
    ) -> None:
        self.proxy = proxy
        self.snapshot_dir = snapshot_dir
        self.log_dir = log_dir
        self.failure_policy = failure_policy
        self.per_call_timeout_seconds = per_call_timeout_seconds
        self.max_calls = max_calls
        self.calls = 0
        self.nl_wall_seconds = 0.0  # cumulative LLM-service wall, reported as a replay phase
        # Set per tick by the replay engine; rolls ctx.nl() text on the same nodes as
        # the Timeview. None (Timeview off / no replay) keeps the frozen PIT corpus.
        self.current_when = None
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.retriever = TextRetriever(
            snapshot_dir / "text_index.parquet",
            snapshot_dir / "text_library",
            replay_index_path=(replay_dir / "text_index.parquet") if replay_dir is not None else None,
            replay_library_dir=(replay_dir / "text_library") if replay_dir is not None else None,
        )

    def run(
        self,
        ts_code: str,
        *,
        prompt: str,
        kwargs: dict[str, object],
        request: dict[str, object],
    ) -> dict[str, object]:
        self.calls += 1
        # Bind the retriever to the requesting tick's sim clock so announcements/news
        # become visible to ctx.nl() only once their refresh node has completed.
        self.retriever.as_of = self.current_when
        if self.max_calls is not None and self.calls > self.max_calls:
            # Hard backstop on API spend; strategy code sees an audited error and
            # degrades (the prompt asks it to keep NL frequency low to begin with).
            result = _error_result(
                ts_code, state="budget_exhausted", error=f"nl call budget exhausted (max {self.max_calls})"
            )
            self._write_result(request, result)
            return result
        if self.proxy is None:
            if self.failure_policy == "return_error_with_audit":
                result = _error_result(ts_code, state="failed_with_policy", error="nl proxy is not configured")
                self._write_result(request, result)
                return result
            raise BacktestError("strategy called nl() but no LLM proxy is configured")
        engine = NLSubAgentEngine(
            self.proxy,
            self.retriever,
            company_contexts=build_company_contexts(self.snapshot_dir, [ts_code]),
        )
        config = NLSubAgentConfig(
            per_call_timeout_seconds=self.per_call_timeout_seconds,
            failure_policy=self.failure_policy,
        )
        _nl_t0 = time.monotonic()
        result = engine.run(ts_code=ts_code, prompt=prompt, request_kwargs=kwargs, config=config)
        self.nl_wall_seconds += time.monotonic() - _nl_t0
        record = result.to_record()
        self._write_result(request, record)
        _append_jsonl(self.log_dir / "search_requests.jsonl", [{"ts_code": ts_code, **r} for r in result.tool_calls])
        _append_jsonl(self.log_dir / "evidence.jsonl", [{"ts_code": ts_code, **e} for e in result.evidence])
        _append_jsonl(self.log_dir / "nl_llm_calls.jsonl", result.llm_calls)
        if result.state in {"failed", "timeout"} and self.failure_policy == "fail":
            raise BacktestError(f"nl() failed for {ts_code}: {result.error}")
        return record

    def _write_result(
        self,
        request: dict[str, object],
        result: dict[str, object],
    ) -> None:
        _append_jsonl(
            self.log_dir / "nl_requests.jsonl",
            {"request": request, "result": result},
        )


def _append_jsonl(path: Path, records: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    items = records if isinstance(records, list) else [records]
    with path.open("a", encoding="utf-8") as handle:
        for record in items:
            handle.write(json.dumps(sanitize_for_log(record), ensure_ascii=False, default=str) + "\n")


def _error_result(ts_code: str, *, state: str, error: str) -> dict[str, object]:
    return {
        "task_id": "",
        "ts_code": ts_code,
        "status": "error",
        "state": state,
        "content": "",
        "error": error,
        "rounds": 0,
        "tool_calls": [],
        "evidence": [],
        "company_context": {},
    }


def _profile_kwargs(record: dict[str, object]) -> dict[str, object]:
    fields = set(BrokerProfile.__dataclass_fields__)
    return {key: value for key, value in record.items() if key in fields}


def _read_replay_minutes(replay_dir: Path) -> pd.DataFrame | None:
    path = replay_dir / "intraday_1min.parquet"
    if not path.exists():
        return None
    minutes = pd.read_parquet(path)
    return None if minutes.empty else minutes


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
