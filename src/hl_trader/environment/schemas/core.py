from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class HorizonTrack:
    track_id: str
    target_holding_months: int
    train_length_months: int
    test_length_months: int
    step_months: int
    template_bank: str

    def validate(self) -> None:
        if self.target_holding_months <= 0:
            raise ValueError("target_holding_months must be positive")
        if self.train_length_months < self.target_holding_months:
            raise ValueError("train_length_months should cover at least one holding horizon")
        if self.test_length_months <= 0 or self.step_months <= 0:
            raise ValueError("test_length_months and step_months must be positive")


@dataclass(frozen=True)
class CostModel:
    commission_bps: float = 3.0
    stamp_tax_bps: float = 5.0
    slippage_bps: float = 5.0

    def estimate_buy_cost(self, notional: float) -> float:
        return notional * (self.commission_bps + self.slippage_bps) / 10_000.0

    def estimate_sell_cost(self, notional: float) -> float:
        return notional * (self.commission_bps + self.stamp_tax_bps + self.slippage_bps) / 10_000.0


@dataclass(frozen=True)
class Protocol:
    protocol_id: str
    start_date: date
    end_date: date
    decision_anchor: str = "month_end"
    rebalance_frequency: str = "monthly"
    nl_weight: float = 0.0
    heldout_start: date | None = None
    cost_model: CostModel = field(default_factory=CostModel)

    def validate(self) -> None:
        if self.start_date >= self.end_date:
            raise ValueError("protocol start_date must be before end_date")
        if self.heldout_start and not (self.start_date < self.heldout_start <= self.end_date):
            raise ValueError("heldout_start must be inside the protocol date range")
        if self.nl_weight != 0.0 and (not self.heldout_start or self.start_date < self.heldout_start):
            raise ValueError("development WFO should keep nl_weight at 0.0")


@dataclass(frozen=True)
class TradeStrategyPolicy:
    policy_id: str
    data_granularity: str = "daily"
    settlement_mode: str = "t_plus_1"
    max_daily_turnover_pct: float = 0.2
    max_position_deviation_pct: float = 0.05
    min_expected_edge_after_cost: float = 0.0
    event_de_risk_pct: float = 0.5
    event_exit_loss_pct: float = 18.0
    allowed_actions: tuple[str, ...] = ("hold", "enter", "exit", "trim", "add", "rebalance", "event_de_risk")

    def allows(self, action: str) -> bool:
        return action in self.allowed_actions

    def validate(self) -> None:
        if self.data_granularity not in {"daily", "minute", "tick"}:
            raise ValueError(f"unsupported data_granularity={self.data_granularity}")
        if not 0 < self.max_daily_turnover_pct <= 1:
            raise ValueError("max_daily_turnover_pct must be in (0, 1]")
        if not 0 <= self.event_de_risk_pct <= 1:
            raise ValueError("event_de_risk_pct must be in [0, 1]")
        if self.event_exit_loss_pct <= 0:
            raise ValueError("event_exit_loss_pct must be positive")
        if not self.allowed_actions:
            raise ValueError("allowed_actions cannot be empty")


@dataclass(frozen=True)
class HeuristicTemplate:
    template_id: str
    strategy_family: str
    variable_families: tuple[str, ...]
    parameter_space: dict[str, Any] = field(default_factory=dict)
    objective: str = "excess_return_after_cost"

    def validate(self) -> None:
        if not self.variable_families:
            raise ValueError("template variable_families cannot be empty")


@dataclass(frozen=True)
class ExperimentConfig:
    experiment_id: str
    raw_dir: Path
    feature_dir: Path
    ledger_path: Path
    track: HorizonTrack
    protocol: Protocol
    trade_policy: TradeStrategyPolicy
    template: HeuristicTemplate
    universe: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        self.track.validate()
        self.protocol.validate()
        self.trade_policy.validate()
        self.template.validate()
        if self.protocol.start_date < date(2020, 1, 1):
            # The current data can support earlier daily experiments, but the first integrated pilot is 2020+.
            raise ValueError("initial experiment configs should start at or after 2020-01-01")


def _parse_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _read_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return data


def load_experiment_config(path: str | Path) -> ExperimentConfig:
    base = Path(path).resolve().parent
    data = _read_yaml(Path(path))
    track = HorizonTrack(**data["track"])
    protocol_data = dict(data["protocol"])
    protocol_data["start_date"] = _parse_date(protocol_data["start_date"])
    protocol_data["end_date"] = _parse_date(protocol_data["end_date"])
    if protocol_data.get("heldout_start"):
        protocol_data["heldout_start"] = _parse_date(protocol_data["heldout_start"])
    if "cost_model" in protocol_data:
        protocol_data["cost_model"] = CostModel(**protocol_data["cost_model"])
    protocol = Protocol(**protocol_data)
    trade_policy = TradeStrategyPolicy(**data["trade_policy"])
    template = HeuristicTemplate(
        variable_families=tuple(data["template"].get("variable_families", ())),
        **{key: value for key, value in data["template"].items() if key != "variable_families"},
    )
    cfg = ExperimentConfig(
        experiment_id=data["experiment_id"],
        raw_dir=(base / data.get("raw_dir", "../../data/raw")).resolve(),
        feature_dir=(base / data.get("feature_dir", "../../data/features")).resolve(),
        ledger_path=(base / data.get("ledger_path", "../../experiments/trial_ledger/pilot.jsonl")).resolve(),
        track=track,
        protocol=protocol,
        trade_policy=trade_policy,
        template=template,
        universe=dict(data.get("universe") or {}),
    )
    cfg.validate()
    return cfg
