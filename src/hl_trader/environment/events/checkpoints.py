from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd


@dataclass(frozen=True)
class EventCheckpoint:
    checkpoint_id: str
    event_type: str
    feature_date: str
    tradable_date: str
    ts_code: str
    severity: str
    payload: dict[str, Any]


class CheckpointDetector:
    def __init__(
        self,
        *,
        price_move_threshold_pct: float = 9.5,
        amount_to_ma20_threshold: float = 3.0,
    ) -> None:
        if price_move_threshold_pct <= 0:
            raise ValueError("price_move_threshold_pct must be positive")
        if amount_to_ma20_threshold <= 0:
            raise ValueError("amount_to_ma20_threshold must be positive")
        self.price_move_threshold_pct = price_move_threshold_pct
        self.amount_to_ma20_threshold = amount_to_ma20_threshold

    def detect(self, features: pd.DataFrame) -> list[EventCheckpoint]:
        required = {"feature_date", "tradable_date", "ts_code"}
        missing = required - set(features.columns)
        if missing:
            raise ValueError(f"features missing checkpoint columns: {sorted(missing)}")
        frame = features.copy()
        frame["feature_date"] = frame["feature_date"].astype(str)
        frame["tradable_date"] = frame["tradable_date"].astype(str)
        frame["ts_code"] = frame["ts_code"].astype(str)
        duplicated = frame.duplicated(["feature_date", "tradable_date", "ts_code"], keep=False)
        if duplicated.any():
            sample = frame.loc[duplicated, ["feature_date", "tradable_date", "ts_code"]].head(5).to_dict("records")
            raise ValueError(f"duplicate checkpoint feature rows: {sample}")

        checkpoints: list[EventCheckpoint] = []
        checkpoints.extend(self._price_move_checkpoints(frame))
        checkpoints.extend(self._amount_spike_checkpoints(frame))
        checkpoints.extend(self._limit_checkpoints(frame))
        return sorted(checkpoints, key=lambda item: (item.feature_date, item.ts_code, item.event_type))

    def _price_move_checkpoints(self, frame: pd.DataFrame) -> list[EventCheckpoint]:
        if "pct_chg" not in frame.columns:
            return []
        pct_chg = pd.to_numeric(frame["pct_chg"], errors="coerce")
        mask = (pct_chg.abs() >= self.price_move_threshold_pct).fillna(False)
        checkpoints = []
        for row, move_pct in zip(frame[mask].itertuples(index=False), pct_chg[mask], strict=False):
            checkpoints.append(self._make_checkpoint(row, "large_price_move", "medium", {
                "pct_chg": float(move_pct),
                "pct_chg_unit": "percent",
                "threshold_pct": self.price_move_threshold_pct,
            }))
        return checkpoints

    def _amount_spike_checkpoints(self, frame: pd.DataFrame) -> list[EventCheckpoint]:
        if not {"amount", "amount_ma20"}.issubset(frame.columns):
            return []
        amount = pd.to_numeric(frame["amount"], errors="coerce")
        baseline = pd.to_numeric(frame["amount_ma20"], errors="coerce")
        ratio = amount / baseline.where(baseline > 0)
        mask = (ratio >= self.amount_to_ma20_threshold).fillna(False)
        checkpoints = []
        for row, value in zip(frame[mask].itertuples(index=False), ratio[mask], strict=False):
            checkpoints.append(self._make_checkpoint(row, "large_amount_spike", "medium", {
                "amount_to_ma20": float(value),
                "amount_to_ma20_threshold": self.amount_to_ma20_threshold,
                "amount_unit": "thousand_cny",
                "amount_ma20_unit": "thousand_cny",
            }))
        return checkpoints

    @staticmethod
    def _limit_checkpoints(frame: pd.DataFrame) -> list[EventCheckpoint]:
        column = "limit_status" if "limit_status" in frame.columns else "limit" if "limit" in frame.columns else None
        if column is None:
            return []
        values = frame[column]
        mask = values.notna() & (values.astype(str) != "")
        checkpoints = []
        for row in frame[mask].itertuples(index=False):
            status = str(getattr(row, column))
            checkpoints.append(CheckpointDetector._make_checkpoint(row, "price_limit_status", "high", {
                "limit_status": status,
                "source_column": column,
            }))
        return checkpoints

    @staticmethod
    def _make_checkpoint(row: Any, event_type: str, severity: str, payload: dict[str, Any]) -> EventCheckpoint:
        feature_date = str(getattr(row, "feature_date"))
        tradable_date = str(getattr(row, "tradable_date"))
        ts_code = str(getattr(row, "ts_code"))
        checkpoint_id = f"{feature_date}:{tradable_date}:{ts_code}:{event_type}"
        return EventCheckpoint(checkpoint_id, event_type, feature_date, tradable_date, ts_code, severity, payload)
