from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

import pandas as pd

from hl_trader.environment.schemas import ExperimentConfig, Protocol
from hl_trader.environment.storage.ledger import stable_hash, to_jsonable

if TYPE_CHECKING:
    from hl_trader.environment.wfo.splitter import Fold


@dataclass(frozen=True)
class FreezeSpec:
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
    model_id: str = ""
    prompt_id: str = ""
    data_contract_id: str = "tushare_daily_pit_v1"

    @classmethod
    def from_config(
        cls,
        config: ExperimentConfig,
        *,
        model_id: str = "",
        prompt_id: str = "",
        data_contract_id: str = "tushare_daily_pit_v1",
    ) -> FreezeSpec:
        return cls(
            experiment_id=config.experiment_id,
            track_id=config.track.track_id,
            template_id=config.template.template_id,
            protocol_id=config.protocol.protocol_id,
            trade_policy_id=config.trade_policy.policy_id,
            horizon_months=config.track.target_holding_months,
            track_hash=stable_hash(config.track),
            template_hash=stable_hash(config.template),
            protocol_hash=stable_hash(config.protocol),
            trade_policy_hash=stable_hash(config.trade_policy),
            model_id=model_id,
            prompt_id=prompt_id,
            data_contract_id=data_contract_id,
        )

    def to_record(self) -> dict[str, Any]:
        record = to_jsonable(self)
        record["freeze_hash"] = self.freeze_hash
        return record

    @property
    def freeze_hash(self) -> str:
        return stable_hash({
            "experiment_id": self.experiment_id,
            "track_id": self.track_id,
            "template_id": self.template_id,
            "protocol_id": self.protocol_id,
            "trade_policy_id": self.trade_policy_id,
            "horizon_months": self.horizon_months,
            "track_hash": self.track_hash,
            "template_hash": self.template_hash,
            "protocol_hash": self.protocol_hash,
            "trade_policy_hash": self.trade_policy_hash,
            "model_id": self.model_id,
            "prompt_id": self.prompt_id,
            "data_contract_id": self.data_contract_id,
        })


def development_end(protocol: Protocol) -> date:
    if protocol.heldout_start is None:
        return protocol.end_date
    return protocol.heldout_start - timedelta(days=1)


def development_folds(config: ExperimentConfig) -> list[Fold]:
    from hl_trader.environment.wfo.splitter import generate_rolling_folds

    end = development_end(config.protocol)
    if end <= config.protocol.start_date:
        return []
    folds = generate_rolling_folds(
        start_date=config.protocol.start_date,
        end_date=end,
        train_length_months=config.track.train_length_months,
        test_length_months=config.track.test_length_months,
        step_months=config.track.step_months,
    )
    for fold in folds:
        assert_fold_before_heldout(fold, config.protocol)
    return folds


def assert_fold_before_heldout(fold: Fold, protocol: Protocol) -> None:
    if protocol.heldout_start is None:
        return
    if fold.test_end >= protocol.heldout_start or fold.train_end >= protocol.heldout_start:
        raise ValueError(
            f"fold {fold.fold_id} crosses held-out boundary {protocol.heldout_start.isoformat()}"
        )


def assert_freeze_match(expected: FreezeSpec, observed: FreezeSpec) -> None:
    if expected.freeze_hash != observed.freeze_hash:
        raise ValueError("frozen experiment spec changed inside a protected window")


def assert_result_available(
    frame: pd.DataFrame,
    *,
    train_end: date,
    result_available_column: str = "result_available_time",
    require_column: bool = False,
) -> None:
    if result_available_column not in frame.columns:
        if require_column:
            raise ValueError(f"training data missing required {result_available_column} column")
        return
    cutoff = pd.Timestamp(train_end).tz_localize("Asia/Shanghai") + pd.Timedelta(hours=23, minutes=59, seconds=59)
    raw_values = frame[result_available_column]
    present = raw_values.notna()
    if require_column and not present.all():
        missing = frame.loc[~present].head(3).to_dict("records")
        raise ValueError(f"training data has missing {result_available_column} values: {missing}")
    if not present.any():
        return
    values = raw_values[present].map(_parse_result_available_time)
    violating = values > cutoff
    if violating.fillna(False).any():
        sample = frame.loc[values.index[violating], [result_available_column]].head(3).to_dict("records")
        raise ValueError(f"training data contains unavailable results after train_end={train_end}: {sample}")


def _parse_result_available_time(value: Any) -> pd.Timestamp:
    if isinstance(value, pd.Timestamp):
        parsed = value
    else:
        text = str(value).strip()
        if len(text) == 8 and text.isdigit():
            parsed = pd.Timestamp(f"{text[:4]}-{text[4:6]}-{text[6:8]}")
        else:
            try:
                parsed = pd.Timestamp(text)
            except Exception as exc:
                raise ValueError(f"invalid result_available_time={value!r}") from exc
    if pd.isna(parsed):
        raise ValueError(f"invalid result_available_time={value!r}")
    if parsed.tzinfo is None:
        return parsed.tz_localize("Asia/Shanghai")
    return parsed.tz_convert("Asia/Shanghai")
