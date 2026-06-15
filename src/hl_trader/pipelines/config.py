"""Pipeline configuration records and snapshot providers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Protocol

from hl_trader.environment.artifacts import ModificationConstraints
from hl_trader.environment.broker import BrokerProfile
from hl_trader.environment.sandbox import SandboxSpec
from hl_trader.environment.snapshot import SnapshotBuilder, SnapshotConfig
from hl_trader.environment.tools import ToolContext

from .folds import FoldSpec, assert_no_overlap


class SnapshotProvider(Protocol):
    """Builds decision-input snapshots and replay slots for one Fold."""

    def decision_snapshot(self, decision_time: datetime, out_dir: Path) -> dict[str, object]: ...

    def replay_slot(self, start: str, end: str, out_dir: Path, *, label: str) -> dict[str, object]: ...


class RawSnapshotProvider:
    """Default provider over the local raw data and PIT feature layers."""

    def __init__(
        self, raw_dir: str | Path, fundamental_events_root: str | Path, config: SnapshotConfig | None = None
    ) -> None:
        self.builder = SnapshotBuilder(raw_dir, fundamental_events_root)
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
            reasons.append("accepted step requires full natural-language scoring (nl=on)")
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
    first_test_quarter: str
    last_test_quarter: str
    heldout_first_quarter: str
    heldout_last_quarter: str
    epochs: int = 1
    window_months: int = 21
    max_fold_minutes: int = 30
    finalize_before_deadline_seconds: int = 300
    per_call_timeout_seconds: int = 300
    max_steps_per_fold: int = 10
    max_candidates: int = 10
    # Individual NL task failures become neutral audited scores by default so
    # one malformed provider response does not invalidate a whole Fold.
    nl_failure_policy: str = "neutral_with_audit"
    # Step artifact tree (lineage across folds); toggleable for ablations.
    step_tree_enabled: bool = True
    # Shapley factor attribution after each formal backtest; toggleable.
    factor_attribution_enabled: bool = True
    # Epoch index (1-based) from which folds enter the convergence phase
    # (fewer modifications while holding returns, down to zero changes).
    convergence_start_epoch: int = 3
    step_constraints: ModificationConstraints = field(default_factory=ModificationConstraints)
    regularization_constraints: ModificationConstraints = field(default_factory=ModificationConstraints)
    acceptance: AcceptanceRules = field(default_factory=AcceptanceRules)
    broker_profile: BrokerProfile = field(default_factory=BrokerProfile)
    # Each sandbox container is limited to ~10% of host CPU/RAM by default.
    sandbox_spec: SandboxSpec = field(default_factory=SandboxSpec.from_host_fraction)
    use_docker: bool = True

    def __post_init__(self) -> None:
        # Held-out boundaries are frozen in config before the experiment starts.
        assert_no_overlap(self.last_test_quarter, self.heldout_first_quarter)

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


@dataclass
class FoldOutcome:
    fold_id: str
    run_id: str
    fold_status: str
    frozen: FrozenArtifact
    validation_summary: dict[str, object] | None
    test_summary: dict[str, object] | None


AgentFactory = Callable[[ToolContext, FoldSpec, dict[str, object]], object]
MetaLearner = Callable[[ToolContext], None]
