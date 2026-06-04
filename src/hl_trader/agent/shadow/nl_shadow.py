from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
import re
from typing import Any

from hl_trader.environment.storage import TrialLedger
from hl_trader.environment.storage.ledger import stable_hash, utc_now_iso


MARGIN_SHORT_SELL_ACTION = "margin_short_sell"
DEFAULT_NL_SHADOW_ACTIONS = (
    "hold",
    "enter",
    "exit",
    "trim",
    "add",
    "rebalance",
    MARGIN_SHORT_SELL_ACTION,
    "human_review",
)
_ALLOWED_NL_SHADOW_ACTIONS = frozenset(DEFAULT_NL_SHADOW_ACTIONS)
_SECRET_VALUE_PATTERN = re.compile(r"sk-[A-Za-z0-9_-]{8,}")
_SECRET_KEY_NAMES = {
    "api_key",
    "apikey",
    "authorization",
    "bearer",
    "access_token",
    "refresh_token",
    "token",
    "secret",
    "password",
    "credential",
    "credentials",
}


@dataclass(frozen=True)
class NLShadowDecision:
    decision_id: str
    decision_date: str
    tradable_date: str
    ts_code: str
    prompt_hash: str
    response_hash: str
    rationale: str
    action: str = "hold"
    confidence: float = 0.0
    nl_weight: float = 0.0
    action_impact: str = "shadow_only"
    created_at: str = ""

    def __post_init__(self) -> None:
        if not self.decision_id:
            raise ValueError("decision_id cannot be empty")
        if not self.prompt_hash or not self.response_hash:
            raise ValueError("prompt_hash and response_hash are required for NL shadow auditability")
        if self.action not in _ALLOWED_NL_SHADOW_ACTIONS:
            raise ValueError(f"unsupported NL shadow action={self.action}")
        if self.action_impact != "shadow_only":
            raise ValueError("NL shadow decisions must not affect orders")
        if not math.isfinite(float(self.nl_weight)) or float(self.nl_weight) != 0.0:
            raise ValueError("NL shadow decisions must keep nl_weight at 0.0 before API integration")
        if not math.isfinite(float(self.confidence)) or not 0.0 <= float(self.confidence) <= 1.0:
            raise ValueError("confidence must be in [0, 1]")
        if not self.created_at:
            object.__setattr__(self, "created_at", utc_now_iso())

    def to_record(self) -> dict[str, Any]:
        record = {
            "decision_id": self.decision_id,
            "decision_date": self.decision_date,
            "tradable_date": self.tradable_date,
            "ts_code": self.ts_code,
            "prompt_hash": self.prompt_hash,
            "response_hash": self.response_hash,
            "rationale": self.rationale,
            "action": self.action,
            "confidence": float(self.confidence),
            "nl_weight": 0.0,
            "action_impact": "shadow_only",
            "can_affect_trading": False,
            "created_at": self.created_at,
        }
        record["decision_hash"] = stable_hash({
            key: value for key, value in record.items()
            if key not in {"decision_hash", "created_at"}
        })
        return record


class NLShadowRecorder:
    def __init__(self, path: str | Path) -> None:
        self.ledger = TrialLedger(path)

    def append(
        self,
        decision: NLShadowDecision,
        *,
        evidence_pack_id: str | None = None,
        provider_metadata: dict[str, Any] | None = None,
    ) -> None:
        decision_record = decision.to_record()
        if decision_record["can_affect_trading"]:
            raise ValueError("NL shadow recorder cannot write trading-impact decisions")
        self.ledger.append({
            "event_type": "nl_shadow_decision",
            "decision": decision_record,
            "decision_hash": decision_record["decision_hash"],
            "evidence_pack_id": evidence_pack_id,
            "provider_metadata": sanitize_provider_metadata(provider_metadata or {}),
            "action_impact": "shadow_only",
            "can_affect_trading": False,
        })

    def read_all(self) -> list[dict[str, Any]]:
        return self.ledger.read_all()


def sanitize_provider_metadata(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if _is_secret_key(key_text):
                sanitized[key_text] = "[REDACTED]"
            else:
                sanitized[key_text] = sanitize_provider_metadata(item)
        return sanitized
    if isinstance(value, (list, tuple)):
        return [sanitize_provider_metadata(item) for item in value]
    if isinstance(value, set):
        return sorted((sanitize_provider_metadata(item) for item in value), key=repr)
    if isinstance(value, str):
        return _SECRET_VALUE_PATTERN.sub("sk-***", value)
    return value


def _is_secret_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    if normalized in _SECRET_KEY_NAMES:
        return True
    return normalized.endswith(("_api_key", "_secret", "_password", "_credential", "_token"))
