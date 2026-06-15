"""Strict JSON extraction from provider responses for NL scoring.

Implements docs/environment_design.md 4.4: only tool-call arguments, JSON-mode
content, or a single JSON object in plain text are accepted. One ```json fence
may be removed. Closed <think> blocks are stripped (and preserved for logging);
an unclosed <think>, multiple JSON objects, or any string-searching for score
fields is a failure. Thinking text never supplies formal fields.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

THINK_OPEN = "<think>"
THINK_CLOSE = "</think>"
FENCE_PATTERN = re.compile(r"^```(?:json)?\s*\n(.*)\n```\s*$", re.DOTALL)

NL_SCORE_REQUIRED_FIELDS = ("ts_code", "nl_score", "confidence", "risk_tags", "applied_prior_ids", "evidence_ids")


class ExtractionError(ValueError):
    """The provider response does not contain exactly one valid JSON object."""


@dataclass(frozen=True)
class ExtractedJSON:
    payload: dict[str, object]
    stripped_think: str = ""


def extract_json_object(content: str, *, tool_call_arguments: str | None = None) -> ExtractedJSON:
    """Extract exactly one JSON object from a provider response.

    ``tool_call_arguments`` takes precedence when the provider returned a
    tool/function call; otherwise the content must itself be one JSON object,
    optionally wrapped in a single json code fence and/or preceded by a closed
    think block.
    """
    if tool_call_arguments is not None:
        return ExtractedJSON(payload=_loads_object(tool_call_arguments))
    text = content.strip()
    stripped_think = ""
    if THINK_OPEN in text:
        open_index = text.index(THINK_OPEN)
        close_index = text.find(THINK_CLOSE, open_index)
        if close_index < 0:
            raise ExtractionError("unclosed <think> block in provider response")
        stripped_think = text[open_index + len(THINK_OPEN) : close_index]
        text = (text[:open_index] + text[close_index + len(THINK_CLOSE) :]).strip()
        if THINK_OPEN in text:
            raise ExtractionError("multiple <think> blocks in provider response")
    fence = FENCE_PATTERN.match(text)
    if fence:
        text = fence.group(1).strip()
    return ExtractedJSON(payload=_loads_object(text), stripped_think=stripped_think)


def validate_score_payload(
    payload: dict[str, object],
    *,
    expected_ts_code: str,
    seen_evidence_ids: set[str],
    valid_prior_ids: set[str] | None = None,
    require_prior_id: bool = False,
) -> dict[str, object]:
    """Validate the final NL score JSON against the fixed schema."""
    missing = [key for key in NL_SCORE_REQUIRED_FIELDS if key not in payload]
    if missing:
        raise ExtractionError(f"score JSON missing fields: {missing}")
    if str(payload["ts_code"]) != expected_ts_code:
        raise ExtractionError(f"score ts_code {payload['ts_code']!r} does not match task {expected_ts_code}")
    nl_score = _as_float(payload["nl_score"], "nl_score")
    confidence = _as_float(payload["confidence"], "confidence")
    if not -1.0 <= nl_score <= 1.0:
        raise ExtractionError(f"nl_score out of range [-1, 1]: {nl_score}")
    if not 0.0 <= confidence <= 1.0:
        raise ExtractionError(f"confidence out of range [0, 1]: {confidence}")
    for key in ("risk_tags", "applied_prior_ids", "evidence_ids"):
        if not isinstance(payload[key], list) or not all(isinstance(item, str) for item in payload[key]):
            raise ExtractionError(f"{key} must be a list of strings")
    invalid = [eid for eid in payload["evidence_ids"] if eid not in seen_evidence_ids]
    if invalid:
        raise ExtractionError(f"score cites unknown evidence ids: {invalid}")
    applied = list(payload["applied_prior_ids"])
    if valid_prior_ids is not None:
        unknown = [pid for pid in applied if pid not in valid_prior_ids]
        if unknown:
            raise ExtractionError(f"score cites unknown prior ids: {unknown}")
    if require_prior_id and valid_prior_ids and not applied and _needs_prior_reference(nl_score, payload):
        raise ExtractionError("non-neutral or evidence-backed score must cite at least one applicable prior id")
    return {
        "ts_code": expected_ts_code,
        "nl_score": nl_score,
        "confidence": confidence,
        "risk_tags": list(payload["risk_tags"]),
        "applied_prior_ids": applied,
        "evidence_ids": list(payload["evidence_ids"]),
    }


def _loads_object(text: str) -> dict[str, object]:
    try:
        decoded, end = json.JSONDecoder().raw_decode(text.strip())
    except json.JSONDecodeError as exc:
        raise ExtractionError(f"response is not valid JSON: {exc}") from exc
    if text.strip()[end:].strip():
        raise ExtractionError("response contains content beyond a single JSON object")
    if not isinstance(decoded, dict):
        raise ExtractionError("response JSON must be a single object")
    return decoded


def _as_float(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ExtractionError(f"{name} must be a number")
    return float(value)


def _needs_prior_reference(nl_score: float, payload: dict[str, object]) -> bool:
    if abs(nl_score) > 0.1:
        return True
    if payload.get("evidence_ids"):
        return True
    tags = [str(tag) for tag in payload.get("risk_tags", [])]
    return any(tag not in {"", "neutral", "insufficient_evidence", "no_evidence"} for tag in tags)
