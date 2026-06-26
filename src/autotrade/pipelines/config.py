"""Pipeline configuration records and snapshot providers."""

from __future__ import annotations

from dataclasses import InitVar, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Protocol

from autotrade.environment.artifacts import ModificationConstraints
from autotrade.environment.broker import BrokerProfile
from autotrade.environment.sandbox import SandboxSpec
from autotrade.environment.snapshot import SnapshotBuilder, SnapshotConfig
from autotrade.environment.tools import ToolContext

from .folds import FoldSpec, assert_no_overlap


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
        self.builder = SnapshotBuilder(raw_dir, fundamental_events_root, fundamental_events_status)
        self.config = config or SnapshotConfig()

    def decision_snapshot(self, decision_time: datetime, out_dir: Path) -> dict[str, object]:
        return self.builder.build_decision_snapshot(decision_time, out_dir, self.config)

    def replay_slot(self, start: str, end: str, out_dir: Path, *, label: str) -> dict[str, object]:
        return self.builder.build_replay_slot(start, end, out_dir, label=label, config=self.config)


@dataclass(frozen=True)
class AcceptanceRules:
    """Hard validation acceptance checks (docs/pipeline_design.md 4.1)."""

    min_return: float = 0.0
    min_sharpe: float = 0.0
    max_drawdown: float = 0.25
    require_complete_validation: bool = True

    def evaluate(self, summary: dict[str, object]) -> tuple[bool, list[str]]:
        reasons: list[str] = []
        if float(summary.get("total_return", -1.0)) <= self.min_return:
            reasons.append(f"validation return {summary.get('total_return')} <= {self.min_return}")
        if float(summary.get("sharpe", -1.0)) < self.min_sharpe:
            reasons.append(f"sharpe {summary.get('sharpe')} < {self.min_sharpe}")
        if float(summary.get("max_drawdown", 1.0)) > self.max_drawdown:
            reasons.append(f"max drawdown {summary.get('max_drawdown')} > {self.max_drawdown}")
        if self.require_complete_validation and not summary.get("complete_validation"):
            reasons.append("accepted step requires successful main.py execution and broker replay")
        return (not reasons, reasons)

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
    fold_period: str = "quarter"
    epochs: int = 1
    window_months: int = 21
    max_fold_minutes: int = 60
    finalize_before_deadline_seconds: int = 300
    per_call_timeout_seconds: int = 300
    max_steps_per_fold: int = 10
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
    # If meta-learning writes workspace/sandbox_environment.json, Pipeline can
    # build a derived Docker image and use it for later ordinary Fold runs.
    meta_sandbox_rebuild_enabled: bool = True
    meta_sandbox_rebuild_timeout_seconds: int = 1800
    use_docker: bool = True

    def __post_init__(
        self,
        first_test_quarter: str | None,
        last_test_quarter: str | None,
        heldout_first_quarter: str | None,
        heldout_last_quarter: str | None,
    ) -> None:
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
        # Held-out boundaries are frozen in config before the experiment starts.
        assert_no_overlap(str(last_test_period), str(heldout_first_period), period=self.fold_period)

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
