"""Provider conversation-log records and secret redaction.

Security-sensitive, pure helpers shared by the DeepSeek client: build one
audit record per provider attempt (payload stored once per logical call — see
``DeepSeekClient._post_with_retries``) and redact credentials from every
logged object. No transport logic lives here.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from autotrade.environment.runtime import sanitize_for_log

SENSITIVE_LOG_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "access_token",
    "refresh_token",
    "token",
    "secret",
    "password",
    "access_key",
    "private_key",
}
NON_SECRET_TOKEN_KEYS = {
    "total_tokens",
    "prompt_tokens",
    "completion_tokens",
    "cached_tokens",
    "reasoning_tokens",
}


def _ensure_log_parent(path: Path) -> None:
    # OSError propagates; the client's _write_conversation_log boundary wraps it
    # into the provider error type (importing it here would be a cycle).
    path.parent.mkdir(parents=True, exist_ok=True)


def _conversation_log_record(
    *,
    config: DeepSeekConfig,
    payload: dict[str, Any],
    started_at: str,
    completed_at: str | None,
    duration_seconds: float,
    attempt: int,
    max_attempts: int,
    status: str,
    call_id: str = "",
    include_payload: bool = False,
    http_status_code: int | None = None,
    raw_response: dict[str, Any] | None = None,
    response_body: str | None = None,
    error: DeepSeekAPIError | None = None,
) -> dict[str, Any]:
    """One log line. The full (redacted) payload is stored once per logical
    call — the first attempt's "started" record; every other record joins it
    via ``call_id`` + ``request_hash`` instead of duplicating the history."""
    safe_payload = _redact_secrets_in_obj(payload)
    safe_response = _redact_secrets_in_obj(raw_response) if raw_response is not None else None
    safe_body = _redact_secrets(response_body) if response_body is not None else None
    record: dict[str, Any] = {
        "schema_version": 2,
        "provider": "deepseek",
        "model": config.model,
        "call_id": call_id,
        "request_started_at": started_at,
        "request_completed_at": completed_at,
        "duration_seconds": round(max(duration_seconds, 0.0), 6),
        "attempt": attempt,
        "max_attempts": max_attempts,
        "status": status,
        "http_status_code": http_status_code,
        "request_hash": _stable_hash(safe_payload),
        "metadata": config.safe_metadata(),
    }
    if include_payload:
        record["payload"] = safe_payload
    if safe_response is not None:
        record["raw_response"] = safe_response
        record["response_hash"] = _stable_hash(safe_response)
        record["response_id"] = _redact_secrets(str(raw_response.get("id", "")))
        record["usage"] = _redact_secrets_in_obj(dict(raw_response.get("usage") or {}))
    if safe_body is not None:
        record["response_body"] = safe_body
        record["response_body_hash"] = _stable_hash(safe_body)
    if error is not None:
        record["error"] = {
            "type": type(error).__name__,
            "message": _redact_secrets(str(error)),
            "status_code": error.status_code,
            "retryable": error.retryable,
        }
    return record


def _redact_secrets_in_obj(value: Any) -> Any:
    if isinstance(value, str):
        return _redact_secrets(value)
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if _is_sensitive_log_key(str(key)) else _redact_secrets_in_obj(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_secrets_in_obj(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_secrets_in_obj(item) for item in value]
    return value


def _stable_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _redact_secrets(value: str) -> str:
    return str(sanitize_for_log(value))


def _is_sensitive_log_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
    if normalized in NON_SECRET_TOKEN_KEYS:
        return False
    if normalized in SENSITIVE_LOG_KEYS:
        return True
    compact = normalized.replace("_", "")
    if compact in SENSITIVE_LOG_KEYS:
        return True
    parts = set(normalized.split("_"))
    if {"api", "key"}.issubset(parts):
        return True
    return bool(parts & {"token", "secret", "password"})
