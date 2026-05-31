from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from hl_trader.environment.storage.ledger import TrialLedger, to_jsonable

if TYPE_CHECKING:
    from hl_trader.environment.protocols import FreezeSpec


@dataclass(frozen=True)
class ExperimentLedgerContext:
    experiment_id: str
    track_id: str
    template_id: str
    protocol_id: str
    trade_policy_id: str
    horizon_months: int
    track_hash: str
    template_hash: str
    protocol_hash: str
    trade_policy_hash: str
    freeze_hash: str
    model_id: str = ""
    prompt_id: str = ""
    data_contract_id: str = "tushare_daily_pit_v1"
    heldout_start: str | None = None

    @classmethod
    def from_freeze_spec(cls, freeze_spec: FreezeSpec, *, heldout_start: str | None = None) -> ExperimentLedgerContext:
        return cls(
            experiment_id=freeze_spec.experiment_id,
            track_id=freeze_spec.track_id,
            template_id=freeze_spec.template_id,
            protocol_id=freeze_spec.protocol_id,
            trade_policy_id=freeze_spec.trade_policy_id,
            horizon_months=freeze_spec.horizon_months,
            track_hash=freeze_spec.track_hash,
            template_hash=freeze_spec.template_hash,
            protocol_hash=freeze_spec.protocol_hash,
            trade_policy_hash=freeze_spec.trade_policy_hash,
            freeze_hash=freeze_spec.freeze_hash,
            model_id=freeze_spec.model_id,
            prompt_id=freeze_spec.prompt_id,
            data_contract_id=freeze_spec.data_contract_id,
            heldout_start=heldout_start,
        )

    def to_record(self) -> dict[str, Any]:
        return to_jsonable(self)


class ExperimentLedger:
    def __init__(self, path: str | Path, context: ExperimentLedgerContext, *, default_phase: str = "development") -> None:
        if default_phase not in {"development", "heldout"}:
            raise ValueError("default_phase must be development or heldout")
        self.trial_ledger = TrialLedger(path)
        self.context = context
        self.default_phase = default_phase

    @property
    def path(self) -> Path:
        return self.trial_ledger.path

    def append(self, event: dict[str, Any]) -> None:
        record = dict(event)
        if not record.get("event_type"):
            raise ValueError("experiment ledger events require event_type")
        record.setdefault("phase", self.default_phase)
        record.update({
            "experiment_id": self.context.experiment_id,
            "track_id": self.context.track_id,
            "template_id": self.context.template_id,
            "protocol_id": self.context.protocol_id,
            "trade_policy_id": self.context.trade_policy_id,
            "horizon_months": self.context.horizon_months,
            "track_hash": self.context.track_hash,
            "template_hash": self.context.template_hash,
            "protocol_hash": self.context.protocol_hash,
            "trade_policy_hash": self.context.trade_policy_hash,
            "freeze_hash": self.context.freeze_hash,
            "model_id": self.context.model_id,
            "prompt_id": self.context.prompt_id,
            "data_contract_id": self.context.data_contract_id,
            "heldout_start": self.context.heldout_start,
        })
        self.trial_ledger.append(record)

    def append_event(
        self,
        event_type: str,
        *,
        fold_id: str | None = None,
        phase: str = "development",
        parameters: dict[str, Any] | None = None,
        metrics: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        event: dict[str, Any] = {"event_type": event_type, "phase": phase}
        if fold_id is not None:
            event["fold_id"] = fold_id
        if parameters is not None:
            event["parameters"] = parameters
        if metrics is not None:
            event["metrics"] = metrics
        if payload is not None:
            event["payload"] = payload
        self.append(event)

    def read_all(self) -> list[dict[str, Any]]:
        return self.trial_ledger.read_all()
