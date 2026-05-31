from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any, Protocol

from hl_trader.agent.shadow.nl_shadow import DEFAULT_NL_SHADOW_ACTIONS, NLShadowDecision, sanitize_provider_metadata
from hl_trader.agent.evidence import verify_pack_record
from hl_trader.agent.llm import ChatMessage
from hl_trader.environment.storage.ledger import stable_hash, to_jsonable

from .prompts import LLM_SHADOW_SYSTEM_PROMPT


DEFAULT_LLM_SHADOW_ACTIONS = DEFAULT_NL_SHADOW_ACTIONS


class JSONChatResponse(Protocol):
    content: str
    model: str
    usage: dict[str, Any]
    response_id: str

    def json_content(self) -> dict[str, Any]:
        ...


class JSONChatClient(Protocol):
    def chat_json(self, messages: list[ChatMessage], *, max_tokens: int | None = None) -> JSONChatResponse:
        ...


@dataclass(frozen=True)
class ShadowAdviceResult:
    decisions: tuple[NLShadowDecision, ...]
    prompt_hash: str
    response_hash: str
    provider_metadata: dict[str, Any] = field(default_factory=dict)
    raw_response: dict[str, Any] = field(default_factory=dict)


class LLMShadowAdvisor:
    def __init__(
        self,
        client: JSONChatClient,
        *,
        provider_name: str = "unknown",
        allowed_actions: tuple[str, ...] = DEFAULT_LLM_SHADOW_ACTIONS,
        max_evidence_rows: int = 20,
        max_tokens: int = 1200,
    ) -> None:
        if not allowed_actions:
            raise ValueError("allowed_actions cannot be empty")
        unsupported = sorted(set(allowed_actions) - set(DEFAULT_NL_SHADOW_ACTIONS))
        if unsupported:
            raise ValueError(f"unsupported shadow actions: {unsupported}")
        if max_evidence_rows <= 0:
            raise ValueError("max_evidence_rows must be positive")
        if not provider_name:
            raise ValueError("provider_name cannot be empty")
        self.client = client
        self.provider_name = provider_name
        self.allowed_actions = allowed_actions
        self.max_evidence_rows = max_evidence_rows
        self.max_tokens = max_tokens

    def advise(
        self,
        evidence_pack_record: dict[str, Any],
        *,
        checkpoints: list[dict[str, Any]] | None = None,
    ) -> ShadowAdviceResult:
        verify_pack_record(evidence_pack_record)
        _pack_ts_codes(evidence_pack_record)
        prompt_payload = self._prompt_payload(evidence_pack_record, checkpoints or [])
        messages = self._messages(prompt_payload)
        response = self.client.chat_json(messages, max_tokens=self.max_tokens)
        response_payload = response.json_content()
        prompt_hash = stable_hash([message.to_record() for message in messages])
        response_hash = stable_hash(response_payload)
        decisions = self._decisions_from_response(evidence_pack_record, response_payload, prompt_hash, response_hash)
        provider_metadata = sanitize_provider_metadata({
            "provider": self.provider_name,
            "model": response.model,
            "response_id": response.response_id,
            "usage": response.usage,
        })
        return ShadowAdviceResult(decisions, prompt_hash, response_hash, provider_metadata, response_payload)

    def _prompt_payload(self, evidence_pack_record: dict[str, Any], checkpoints: list[dict[str, Any]]) -> dict[str, Any]:
        evidence = _compact_evidence_pack(evidence_pack_record, max_rows=self.max_evidence_rows)
        return {
            "task": "Produce shadow-only point-in-time JSON decisions for the supplied ts_codes.",
            "allowed_actions": list(self.allowed_actions),
            "cannot_affect_trading": True,
            "evidence_pack": evidence,
            "event_checkpoints": to_jsonable(checkpoints),
        }

    @staticmethod
    def _messages(prompt_payload: dict[str, Any]) -> list[ChatMessage]:
        return [
            ChatMessage("system", LLM_SHADOW_SYSTEM_PROMPT),
            ChatMessage(
                "user",
                "Return JSON only for this point-in-time shadow review:\n"
                + json.dumps(to_jsonable(prompt_payload), ensure_ascii=False, sort_keys=True),
            ),
        ]

    def _decisions_from_response(
        self,
        evidence_pack_record: dict[str, Any],
        response_payload: dict[str, Any],
        prompt_hash: str,
        response_hash: str,
    ) -> tuple[NLShadowDecision, ...]:
        ts_codes = _pack_ts_codes(evidence_pack_record)
        rows = response_payload.get("decisions")
        if not isinstance(rows, list):
            raise ValueError("LLM shadow response missing decisions list")
        if len(rows) != len(ts_codes):
            raise ValueError("LLM shadow response must include exactly one decision per pack ts_code")
        seen: set[str] = set()
        decisions: list[NLShadowDecision] = []
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("each LLM shadow decision must be an object")
            ts_code = str(row.get("ts_code", ""))
            if ts_code not in ts_codes:
                raise ValueError(f"LLM shadow response contains unknown ts_code={ts_code}")
            if ts_code in seen:
                raise ValueError(f"LLM shadow response contains duplicate ts_code={ts_code}")
            action = str(row.get("action", "human_review"))
            if action not in self.allowed_actions:
                action = "human_review"
            confidence = _float_in_unit_interval(row.get("confidence", 0.0), label=f"{ts_code}.confidence")
            rationale = _rationale(row)
            decision_id = stable_hash({
                "evidence_pack_id": evidence_pack_record.get("pack_id"),
                "ts_code": ts_code,
                "prompt_hash": prompt_hash,
                "response_hash": response_hash,
            })
            decisions.append(NLShadowDecision(
                decision_id=decision_id,
                decision_date=str(evidence_pack_record["decision_date"]),
                tradable_date=str(evidence_pack_record["tradable_date"]),
                ts_code=ts_code,
                prompt_hash=prompt_hash,
                response_hash=response_hash,
                rationale=rationale,
                action=action,
                confidence=confidence,
            ))
            seen.add(ts_code)
        missing = sorted(set(ts_codes) - seen)
        if missing:
            raise ValueError(f"LLM shadow response missing ts_codes: {missing}")
        return tuple(decisions)


def _compact_evidence_pack(record: dict[str, Any], *, max_rows: int) -> dict[str, Any]:
    compact = {
        "schema_version": record.get("schema_version"),
        "pack_id": record.get("pack_id"),
        "pack_hash": record.get("pack_hash"),
        "decision_date": record.get("decision_date"),
        "tradable_date": record.get("tradable_date"),
        "ts_codes": record.get("ts_codes", []),
        "items": [],
    }
    for item in record.get("items", []):
        payload = dict(item.get("payload") or {})
        rows = list(payload.get("rows") or [])
        payload["rows"] = rows[:max_rows]
        payload["truncated_rows"] = max(0, len(rows) - max_rows)
        compact["items"].append({
            "name": item.get("name"),
            "source": item.get("source"),
            "as_of": item.get("as_of"),
            "payload_hash": item.get("payload_hash"),
            "payload": payload,
        })
    return to_jsonable(compact)


def _float_in_unit_interval(value: Any, *, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be numeric") from exc
    if not 0.0 <= result <= 1.0:
        raise ValueError(f"{label} must be in [0, 1]")
    return result


def _rationale(row: dict[str, Any]) -> str:
    rationale = str(row.get("rationale", "")).strip()
    risk_flags = row.get("risk_flags", [])
    if isinstance(risk_flags, list) and risk_flags:
        flags = ", ".join(str(flag) for flag in risk_flags[:8])
        rationale = f"{rationale} | risk_flags={flags}" if rationale else f"risk_flags={flags}"
    return rationale or "No rationale supplied by shadow model."


def _pack_ts_codes(evidence_pack_record: dict[str, Any]) -> tuple[str, ...]:
    ts_codes = tuple(str(code) for code in evidence_pack_record.get("ts_codes", ()))
    if not ts_codes:
        raise ValueError("evidence pack has no ts_codes")
    if len(ts_codes) != len(set(ts_codes)):
        raise ValueError("evidence pack ts_codes must be unique")
    return ts_codes
