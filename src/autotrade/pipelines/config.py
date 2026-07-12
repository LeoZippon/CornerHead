"""Pipeline configuration records and snapshot providers."""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import uuid
from dataclasses import InitVar, asdict, dataclass, field, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Protocol

from autotrade.environment.artifacts import ModificationConstraints
from autotrade.environment.broker import BrokerProfile
from autotrade.environment.managed_proxy import ManagedProxySpec
from autotrade.environment.sandbox import SandboxSpec, link_copytree
from autotrade.environment.snapshot import SnapshotBuilder, SnapshotConfig, read_raw_generation
from autotrade.environment.tools import ToolContext

from .folds import FoldSpec, assert_no_overlap

FINAL_EVAL_WALL_CAP_MULTIPLIER = 3.0


class SnapshotProvider(Protocol):
    """Builds decision-input snapshots and replay slots for one Fold."""

    def decision_snapshot(self, decision_time: datetime, out_dir: Path) -> dict[str, object]: ...

    def replay_slot(self, start: str, end: str, out_dir: Path, *, label: str) -> dict[str, object]: ...


class RawSnapshotProvider:
    """Default provider over the local raw data and PIT event indexes."""

    def __init__(
        self,
        raw_dir: str | Path,
        fundamental_events_root: str | Path,
        config: SnapshotConfig | None = None,
        fundamental_events_status: str | Path | None = Path("results/data_quality/fundamental_events_status.json"),
    ) -> None:
        self.raw_dir = Path(raw_dir)
        self.builder = SnapshotBuilder(raw_dir, fundamental_events_root, fundamental_events_status)
        self.config = config or SnapshotConfig()

    def decision_snapshot(self, decision_time: datetime, out_dir: Path) -> dict[str, object]:
        return self.builder.build_decision_snapshot(decision_time, out_dir, self.config)

    def replay_slot(self, start: str, end: str, out_dir: Path, *, label: str) -> dict[str, object]:
        return self.builder.build_replay_slot(start, end, out_dir, label=label, config=self.config)


class CachingSnapshotProvider:
    """Reuse identical snapshot builds within one experiment.

    Adjacent folds share the expensive decision snapshot (fold N+1's validation
    anchor equals fold N's test anchor) and multi-epoch reruns rebuild every
    view identically; each build is a pure function of (anchor/range, label,
    provider config) over a raw lake that must not change mid-experiment.
    Replay slots are label-specific — the label lands inside the built
    manifest — so same-range valid/test slots build once per label rather than
    sharing. Entries are built once under the experiment dir and hardlinked
    into each run's sandbox; snapshot parquet views are write-once, so shared
    inodes are safe (the same pattern as the step tree's link_copytree).
    """

    def __init__(self, provider: SnapshotProvider, cache_root: Path) -> None:
        self._provider = provider
        self._root = Path(cache_root)
        config = getattr(provider, "config", None)
        if is_dataclass(config):
            self._config_key = json.dumps(asdict(config), sort_keys=True, default=str)
        else:
            self._config_key = "" if config is None else repr(config)

    def decision_snapshot(self, decision_time: datetime, out_dir: Path) -> dict[str, object]:
        return self._cached(
            ("decision", decision_time.isoformat()),
            lambda view: self._provider.decision_snapshot(decision_time, view),
            out_dir,
        )

    def replay_slot(self, start: str, end: str, out_dir: Path, *, label: str) -> dict[str, object]:
        # label lands inside the built manifest, so it is part of the key.
        return self._cached(
            ("replay", start, end, label),
            lambda view: self._provider.replay_slot(start, end, view, label=label),
            out_dir,
        )

    def _cached(
        self,
        parts: tuple[str, ...],
        build: Callable[[Path], dict[str, object]],
        out_dir: Path,
    ) -> dict[str, object]:
        # Raw-lake generation in the key: a cron mutation between folds must
        # rebuild, never resurface a view of the previous lake.
        generation = read_raw_generation(getattr(self._provider, "raw_dir", None))
        generation_key = str((generation or {}).get("generation_id", ""))
        key = hashlib.sha256("|".join((*parts, self._config_key, generation_key)).encode("utf-8")).hexdigest()[:16]
        entry = self._root / f"{parts[0]}_{key}"
        manifest_path = entry / "cache_manifest.json"
        if not manifest_path.exists():
            staging = self._root / f".{parts[0]}_{key}.{uuid.uuid4().hex[:8]}"
            manifest = build(staging / "view")
            (staging / "cache_manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, sort_keys=True, default=str, allow_nan=False),
                encoding="utf-8",
            )
            try:
                staging.rename(entry)  # atomic publish; a concurrent builder may have won
            except OSError:
                shutil.rmtree(staging, ignore_errors=True)
        link_copytree(entry / "view", out_dir)
        return json.loads(manifest_path.read_text(encoding="utf-8"))


@dataclass(frozen=True)
class AcceptanceRules:
    """Validation acceptance checks (docs/pipeline_design.md 4.1): drawdown,
    finiteness and completeness are HARD rejects; min_return/min_sharpe are
    warn-only targets — a shortfall records a warning and never resets the fold."""

    min_return: float = 0.0
    min_sharpe: float = 0.0
    max_drawdown: float = 0.25
    require_complete_validation: bool = True

    def __post_init__(self) -> None:
        for name in ("min_return", "min_sharpe", "max_drawdown"):
            if not math.isfinite(float(getattr(self, name))):
                raise ValueError(f"acceptance rule {name} must be finite, got {getattr(self, name)!r}")
        if not 0.0 <= float(self.max_drawdown) <= 1.0:
            raise ValueError(f"max_drawdown must be within [0, 1], got {self.max_drawdown!r}")

    def evaluate(self, summary: dict[str, object]) -> tuple[list[str], list[str]]:
        """(hard_reasons, warnings). Integrity failures are hard rejects:
        non-finite metrics (every IEEE comparison against NaN is False, so a NaN
        metric would otherwise pass all thresholds) and incomplete validation.
        The max_drawdown cap stays a hard risk limit. Return/Sharpe shortfalls
        are WARNINGS only — the fold still freezes its validated update; a weak
        step recorded with a warning beats silently resetting the fold chain."""
        reasons: list[str] = []
        warnings: list[str] = []
        total_return = float(summary.get("total_return", -1.0))
        sharpe = float(summary.get("sharpe", -1.0))
        max_drawdown = float(summary.get("max_drawdown", 1.0))
        non_finite = [
            name
            for name, value in (("total_return", total_return), ("sharpe", sharpe), ("max_drawdown", max_drawdown))
            if not math.isfinite(value)
        ]
        if non_finite:
            reasons.append(f"non-finite validation metrics: {non_finite}")
        else:
            if total_return < self.min_return:
                warnings.append(f"validation return {summary.get('total_return')} < {self.min_return}")
            if sharpe < self.min_sharpe:
                warnings.append(f"sharpe {summary.get('sharpe')} < {self.min_sharpe}")
            if max_drawdown > self.max_drawdown:
                reasons.append(f"max drawdown {summary.get('max_drawdown')} > {self.max_drawdown}")
        if self.require_complete_validation and not summary.get("complete_validation"):
            reasons.append("accepted step requires successful main.py execution and broker replay")
        return reasons, warnings

    def to_record(self) -> dict[str, object]:
        return {
            "min_return": self.min_return,
            "min_sharpe": self.min_sharpe,
            "max_drawdown": self.max_drawdown,
            "require_complete_validation": self.require_complete_validation,
        }


@dataclass(frozen=True)
class ExperimentConfig:
    experiment_id: str
    experiments_root: Path
    work_root: Path
    template_dir: Path
    first_test_period: str | None = None
    last_test_period: str | None = None
    heldout_first_period: str | None = None
    heldout_last_period: str | None = None
    first_test_quarter: InitVar[str | None] = None
    last_test_quarter: InitVar[str | None] = None
    heldout_first_quarter: InitVar[str | None] = None
    heldout_last_quarter: InitVar[str | None] = None
    # Deprecated alias for timeview_enabled, kept so older callers/manifests still work.
    rolling_asof_enabled: InitVar[bool | None] = None
    fold_period: str = "quarter"
    epochs: int = 1
    window_months: int = 21
    max_fold_minutes: int = 60
    finalize_before_deadline_seconds: int = 300
    per_call_timeout_seconds: int = 300
    max_steps_per_fold: int = 10
    # Backtests are timed independently of the fold reasoning deadline; this caps
    # how many a fold may run (so the deadline-exclusion can't be abused).
    max_backtests_per_fold: int = 30
    # Pre-open auction: replay injects pre-open decision ticks per day. The 09:15
    # info tick exposes no price and gives a ~10-minute decision window; 09:25 is
    # also blind because the data source lands later. Orders placed at 09:15 fill
    # at 09:30; 09:25 orders fill at the first continuous bar.
    # Set auction_preopen_time=None to drop 09:15.
    auction_enabled: bool = True
    auction_preopen_time: str | None = "09:15"
    auction_decision_time: str = "09:25"
    # Close call-auction decision tick: a decision at this time fills at the day's
    # final bar (15:00 close auction). Set None to drop it.
    auction_close_time: str | None = "14:57"
    # After-hours fixed-price tick (盘后固定价格交易, 15:05-15:30 at the closing
    # price): the strategy sees the confirmed close and its orders settle
    # immediately at that price for board-eligible codes (STAR since 2019-07,
    # ChiNext since 2020-08, all remaining A-shares since 2026-07-06 per the 2026
    # exchange rule revision). Set None to drop the tick. Old manifests without
    # this key replay with it disabled, preserving frozen-eval reproducibility.
    afterhours_decision_time: str | None = "15:05"
    # 24h tick grid: outside the 09:15-15:00 session the replay still calls main(ctx)
    # on this minute spacing (research/state only — off-session ticks place no fills),
    # so the same loop drives backtest and live. 0 disables off-session ticks.
    offsession_tick_minutes: int = 30
    # main(ctx) cadence on plain intraday bars (1 = every minute bar). The Broker
    # still matches every bar (pending orders, execution lag, auction fills are
    # unchanged); auction and off-session ticks always decide. Coarser grids trade
    # intraday reaction granularity for replay wall-time.
    intraday_decision_minutes: int = 1
    # Bars between an order's decision tick and its fill bar (market orders fill at
    # the fill bar's open), modelling the live submit latency: 1 = the immediate
    # next bar, 2 = one bar to compute/submit then fill on the following bar.
    execution_lag_bars: int = 2
    # Latency budgets. A ctx.substep(name, budget_minutes=B) is the block's real-wall
    # ceiling (the backtest aborts if real wall-time exceeds B) and the ctx.state_dir
    # write-visibility gate (ready_at = tick + B). Broker actions inside a sub-minute
    # block are submitted in the current decision minute; B>=1 must still be ready
    # inside the exchange's accepted order-submission window before using the normal
    # execution_lag_bars fill mapping. A declared B over decision_max_sim_minutes is
    # rejected at ctx.substep() init (BacktestError).
    # The two real-wall caps below are SYSTEM fail-fasts that scale with the replay
    # length instead of a fixed total: any single decision (one main(ctx) tick) over
    # backtest_max_seconds_per_decision is killed immediately, and a trade day whose
    # cumulative compute exceeds backtest_max_seconds_per_trading_day aborts the replay
    # (BacktestError, not accept-eligible) — forcing the Agent to cache heavy recompute
    # and bound rebalance/graph cost.
    decision_max_sim_minutes: float | None = 30.0
    backtest_max_seconds_per_decision: float = 1800.0
    backtest_max_seconds_per_trading_day: float = 3600.0
    # The two caps above are real wall-clock, hence load-dependent. To keep
    # acceptance reproducible (H2), they bound ONLY agent-iteration validation
    # backtests. The final evals (the per-fold frozen test_000 and held-out) must
    # complete and be reproducible — a strategy that already fit the tight caps
    # during validation must be allowed to finish its final eval — so they are not
    # subject to the tight caps. They keep a GENEROUS wall-clock backstop whose only
    # job is to kill a true hang (a sim-time budget cannot: an infinite loop in one
    # tick burns zero sim minutes but unbounded wall-clock). None derives from the
    # validation cap by FINAL_EVAL_WALL_CAP_MULTIPLIER; explicit values override.
    backtest_final_eval_max_seconds_per_decision: float | None = None
    backtest_final_eval_max_seconds_per_trading_day: float | None = None
    # Per-tick Timeview: ctx.asof_dir exposes the parquet domains plus text_index
    # and visible text_library shards, rolled in on their real refresh nodes
    # (REFRESH_NODES), so a tick sees only data the landing cron job has already
    # written. Off by replacing with the frozen snapshot view.
    timeview_enabled: bool = True
    # System NL call quota, default-on. The effective per-backtest cap is
    # nl_max_calls_per_decision_day * decision_days (a daily-average budget). An
    # optional nl_max_calls_per_backtest tightens it further (the min wins).
    nl_max_calls_per_decision_day: int = 10
    nl_max_calls_per_backtest: int | None = None
    snapshot_config: SnapshotConfig | None = None
    # Individual NL Sub Agent failures return audited error results by default
    # so Agent code can decide whether to ignore, retry, or fail closed.
    nl_failure_policy: str = "return_error_with_audit"
    # Step artifact tree (lineage across folds); toggleable for ablations.
    step_tree_enabled: bool = True
    # Also record failed validation attempts as lightweight dead-end nodes
    # (no output snapshot) so later folds can see what was already tried.
    record_failed_attempts: bool = True
    # Epoch index (1-based) from which folds enter the convergence phase
    # (fewer modifications while holding returns, down to zero changes).
    convergence_start_epoch: int = 3
    # Optional experiment-level research direction injected only into the
    # Epoch-start meta-learning prompt.
    meta_learning_directive: str = ""
    # Raw prior meta-learning traces handed to the next meta session are bounded
    # to the most recent N epochs (0 disables raw memory). Unbounded concatenation
    # grows O(epochs^2); older sessions persist via the Taste chain and the
    # compact fold history instead.
    meta_memory_max_epochs: int = 3
    step_constraints: ModificationConstraints = field(default_factory=ModificationConstraints)
    regularization_constraints: ModificationConstraints = field(default_factory=ModificationConstraints)
    acceptance: AcceptanceRules = field(default_factory=AcceptanceRules)
    broker_profile: BrokerProfile = field(default_factory=BrokerProfile)
    # Each sandbox container is limited to ~10% of host CPU/RAM by default.
    sandbox_spec: SandboxSpec = field(default_factory=SandboxSpec.from_host_fraction)
    # Optional override used only by Epoch-start meta-learning runs. Ordinary
    # Fold and held-out runs keep ``sandbox_spec`` so production backtests stay
    # offline unless explicitly configured otherwise.
    meta_learning_sandbox_spec: SandboxSpec | None = None
    # Host-side XRay lifecycle for meta-learning only. The spec records only
    # env-var names and process policy; raw proxy configs stay in the host env.
    meta_learning_managed_proxy: ManagedProxySpec = field(default_factory=ManagedProxySpec)
    # If meta-learning writes workspace/sandbox_environment.json, Pipeline can
    # build a derived Docker image and use it for later ordinary Fold runs.
    meta_sandbox_rebuild_enabled: bool = True
    meta_sandbox_rebuild_timeout_seconds: int = 1800
    # Keep at most this many derived sandbox images for this experiment; older ones
    # are best-effort pruned after a successful rebuild (0 disables GC).
    meta_sandbox_image_keep: int = 3
    use_docker: bool = True

    def __post_init__(
        self,
        first_test_quarter: str | None,
        last_test_quarter: str | None,
        heldout_first_quarter: str | None,
        heldout_last_quarter: str | None,
        rolling_asof_enabled: bool | None,
    ) -> None:
        if rolling_asof_enabled is not None:
            object.__setattr__(self, "timeview_enabled", bool(rolling_asof_enabled))
        first_test_period = self.first_test_period or first_test_quarter
        last_test_period = self.last_test_period or last_test_quarter
        heldout_first_period = self.heldout_first_period or heldout_first_quarter
        heldout_last_period = self.heldout_last_period or heldout_last_quarter
        conflicts = [
            name
            for name, current, legacy in (
                ("first_test_period", self.first_test_period, first_test_quarter),
                ("last_test_period", self.last_test_period, last_test_quarter),
                ("heldout_first_period", self.heldout_first_period, heldout_first_quarter),
                ("heldout_last_period", self.heldout_last_period, heldout_last_quarter),
            )
            if current is not None and legacy is not None and str(current) != str(legacy)
        ]
        if conflicts:
            raise ValueError(f"conflicting period aliases: {conflicts}")
        missing = [
            name
            for name, value in (
                ("first_test_period", first_test_period),
                ("last_test_period", last_test_period),
                ("heldout_first_period", heldout_first_period),
                ("heldout_last_period", heldout_last_period),
            )
            if not value
        ]
        if missing:
            raise ValueError(f"missing required experiment period fields: {missing}")
        object.__setattr__(self, "first_test_period", str(first_test_period))
        object.__setattr__(self, "last_test_period", str(last_test_period))
        object.__setattr__(self, "heldout_first_period", str(heldout_first_period))
        object.__setattr__(self, "heldout_last_period", str(heldout_last_period))
        if self.snapshot_config is None:
            object.__setattr__(self, "snapshot_config", SnapshotConfig(window_months=self.window_months))
        # Budget/limit knobs must be positive finite numbers: CLI/HITL inputs are
        # coerced with bare float()/int(), so NaN/inf/zero would otherwise flow
        # silently into deadlines and caps.
        for name in (
            "epochs", "window_months", "max_fold_minutes", "per_call_timeout_seconds",
            "max_steps_per_fold", "max_backtests_per_fold", "execution_lag_bars",
            "intraday_decision_minutes", "backtest_max_seconds_per_decision",
            "backtest_max_seconds_per_trading_day", "nl_max_calls_per_decision_day",
        ):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be a positive finite number, got {getattr(self, name)!r}")
        for name in ("finalize_before_deadline_seconds", "offsession_tick_minutes", "meta_memory_max_epochs"):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be a non-negative finite number, got {getattr(self, name)!r}")
        for name in (
            "decision_max_sim_minutes", "backtest_final_eval_max_seconds_per_decision",
            "backtest_final_eval_max_seconds_per_trading_day", "nl_max_calls_per_backtest",
        ):
            optional = getattr(self, name)
            if optional is not None and (not math.isfinite(float(optional)) or float(optional) <= 0):
                raise ValueError(f"{name} must be a positive finite number when set, got {optional!r}")
        # Held-out boundaries are frozen in config before the experiment starts.
        assert_no_overlap(str(last_test_period), str(heldout_first_period), period=self.fold_period)

    def final_eval_max_seconds_per_decision(self) -> float:
        if self.backtest_final_eval_max_seconds_per_decision is not None:
            return float(self.backtest_final_eval_max_seconds_per_decision)
        return float(self.backtest_max_seconds_per_decision) * FINAL_EVAL_WALL_CAP_MULTIPLIER

    def final_eval_max_seconds_per_trading_day(self) -> float:
        if self.backtest_final_eval_max_seconds_per_trading_day is not None:
            return float(self.backtest_final_eval_max_seconds_per_trading_day)
        return float(self.backtest_max_seconds_per_trading_day) * FINAL_EVAL_WALL_CAP_MULTIPLIER

    @property
    def experiment_dir(self) -> Path:
        return Path(self.experiments_root) / self.experiment_id

    @property
    def ledger_path(self) -> Path:
        return self.experiment_dir / "ledgers" / "experiment_ledger.jsonl"


@dataclass(frozen=True)
class FrozenArtifact:
    artifact_id: str
    path: Path
    artifact_hash: str
    model_path: Path | None
    model_artifact_hash: str


@dataclass
class FoldOutcome:
    fold_id: str
    run_id: str
    fold_status: str
    frozen: FrozenArtifact
    validation_summary: dict[str, object] | None
    test_summary: dict[str, object] | None


AgentFactory = Callable[[ToolContext, FoldSpec, dict[str, object]], object]
MetaLearner = Callable[[ToolContext], dict[str, object] | None]
