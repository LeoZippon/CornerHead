"""Conversation compaction for long Agent sessions.

The runner keeps deterministic observation summaries as a free fallback, but
long sessions need a semantic summary before expensive main-model calls.  This
module provides a small Claude-Code-inspired compaction layer: estimate the
current context window, call a cheap no-thinking model when the window is large,
replace old messages with a structured continuation state, and let the runner
record the audit event.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any

from hl_trader.environment.llm.proxy import LLMProxy, ProviderResponse
from hl_trader.environment.nl.extraction import ExtractionError, extract_json_object
from hl_trader.environment.runtime import sanitize_for_log


@dataclass(frozen=True)
class ContextCompactionConfig:
    token_threshold: int = 200_000
    min_messages: int = 20
    keep_recent_messages: int = 12
    max_response_tokens: int = 1600
    max_failures: int = 3
    max_calls: int = 8
    timeout_seconds: float = 90.0
    min_remaining_seconds: float = 60.0

    def __post_init__(self) -> None:
        if self.token_threshold <= 0:
            raise ValueError("token_threshold must be positive")
        if self.min_messages < 2:
            raise ValueError("min_messages must be at least 2")
        if self.keep_recent_messages < 1:
            raise ValueError("keep_recent_messages must be positive")
        if self.max_response_tokens <= 0:
            raise ValueError("max_response_tokens must be positive")
        if self.max_failures < 0:
            raise ValueError("max_failures cannot be negative")
        if self.max_calls < 0:
            raise ValueError("max_calls cannot be negative")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.min_remaining_seconds < 0:
            raise ValueError("min_remaining_seconds cannot be negative")


@dataclass(frozen=True)
class ContextCompactionResult:
    messages: list[dict[str, str]]
    event: dict[str, object]


class ContextCompactor:
    """Semantic compactor that uses a dedicated low-cost LLM proxy."""

    def __init__(self, proxy: LLMProxy, config: ContextCompactionConfig | None = None) -> None:
        self.proxy = proxy
        self.config = config or ContextCompactionConfig()
        self._consecutive_failures = 0
        self.compaction_count = 0
        self.compaction_attempts = 0

    def should_compact(self, messages: list[dict[str, str]], *, remaining_seconds: float) -> tuple[bool, dict[str, object]]:
        estimated_tokens = estimate_messages_tokens(messages)
        non_summary_count = len([message for message in messages[1:] if not is_compaction_message(message)])
        reason = {
            "estimated_tokens": estimated_tokens,
            "token_threshold": self.config.token_threshold,
            "message_count": len(messages),
            "non_summary_message_count": non_summary_count,
            "keep_recent_messages": self.config.keep_recent_messages,
            "consecutive_failures": self._consecutive_failures,
            "compaction_attempts": self.compaction_attempts,
            "max_calls": self.config.max_calls,
        }
        if self.compaction_attempts >= self.config.max_calls:
            return False, {**reason, "skip_reason": "call_limit_reached"}
        if self.config.max_failures and self._consecutive_failures >= self.config.max_failures:
            return False, {**reason, "skip_reason": "failure_circuit_open"}
        if remaining_seconds < self.config.min_remaining_seconds:
            return False, {**reason, "skip_reason": "insufficient_remaining_time"}
        if len(messages) < self.config.min_messages:
            return False, {**reason, "skip_reason": "not_enough_messages"}
        if non_summary_count <= self.config.keep_recent_messages:
            return False, {**reason, "skip_reason": "nothing_to_compact"}
        if estimated_tokens < self.config.token_threshold:
            return False, {**reason, "skip_reason": "below_token_threshold"}
        return True, {**reason, "trigger_reason": "estimated_tokens"}

    def compact(
        self,
        messages: list[dict[str, str]],
        *,
        remaining_seconds: float,
        step_id: str | None,
    ) -> ContextCompactionResult | None:
        should_compact, decision = self.should_compact(messages, remaining_seconds=remaining_seconds)
        if not should_compact:
            return None

        started_at = datetime.now(timezone.utc).isoformat()
        compact_budget = max(remaining_seconds - self.config.min_remaining_seconds, 0.0)
        timeout = min(self.config.timeout_seconds, compact_budget)
        if timeout <= 0:
            return None
        request_messages = self._build_compact_request(messages)
        self.compaction_attempts += 1
        try:
            response = self.proxy.complete(
                request_messages,
                json_mode=True,
                timeout_seconds=timeout,
                max_tokens=self.config.max_response_tokens,
            )
            summary_payload = _extract_summary_payload(response)
        except Exception as exc:  # noqa: BLE001 - compaction failure should not kill a Fold
            self._consecutive_failures += 1
            return ContextCompactionResult(
                messages=messages,
                event={
                    **decision,
                    "status": "error",
                    "provider": self.proxy.provider,
                    "model": self.proxy.model,
                    "started_at": started_at,
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                    "error": safe_error_summary(exc),
                    "step_id_at_compaction": step_id,
                },
            )

        self._consecutive_failures = 0
        self.compaction_count += 1
        summary_message = _build_compaction_summary_message(summary_payload, self.compaction_count)
        keep = self.config.keep_recent_messages
        recent_messages = _drop_leading_orphan_tools(
            [message for message in messages[1:] if not is_compaction_message(message)][-keep:]
        )
        compacted_messages = [messages[0], summary_message, *recent_messages]
        summary_text = summary_message["content"]
        event = {
            **decision,
            "status": "ok",
            "provider": response.provider,
            "model": response.model,
            "started_at": started_at,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "usage": response.usage,
            "response_id": response.response_id,
            "messages_before": len(messages),
            "messages_after": len(compacted_messages),
            "dropped_messages": max(len(messages) - len(compacted_messages), 0),
            "summary_chars": len(summary_text),
            "summary_hash": sha256(summary_text.encode("utf-8")).hexdigest(),
            "compaction_index": self.compaction_count,
            "step_id_at_compaction": step_id,
        }
        return ContextCompactionResult(messages=compacted_messages, event=event)

    def _build_compact_request(self, messages: list[dict[str, str]]) -> list[dict[str, str]]:
        previous_summary = _latest_compaction_summary(messages)
        messages_since_summary = [message for message in messages[1:] if not is_compaction_message(message)]
        compact_input = {
            "instructions": (
                "Update the continuation state for a coding/trading Agent. Treat "
                "previous_summary as the current anchor when present, merge in only "
                "new information from messages_since_previous_summary, remove stale "
                "or superseded details, and do not invent facts."
            ),
            "previous_summary": sanitize_for_log(previous_summary),
            "output_schema": {
                "goal": "string",
                "constraints_and_preferences": ["string"],
                "progress": {"done": ["string"], "in_progress": ["string"], "blocked": ["string"]},
                "key_decisions": ["string"],
                "errors_and_fixes": ["string"],
                "next_steps": ["string"],
                "critical_context": ["string"],
                "relevant_files": ["string"],
                "recent_user_feedback": ["string"],
            },
            "messages_since_previous_summary": sanitize_for_log(_strip_internal_fields(messages_since_summary)),
        }
        return [
            {
                "role": "system",
                "content": (
                    "You are an anchored context compaction sub-agent. Return exactly one JSON "
                    "object matching the requested schema. Do not call tools. Do not use "
                    "markdown or commentary. Preserve exact file paths, commands, error "
                    "strings, artifact ids, user constraints, and next steps. Avoid vague "
                    "phrases and omit obsolete details. Do not mention that messages were "
                    "compacted."
                ),
            },
            {"role": "user", "content": json.dumps(compact_input, ensure_ascii=False, default=str)},
        ]


def _drop_leading_orphan_tools(seq: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop leading ``tool`` messages left without their ``assistant`` turn."""
    index = 0
    while index < len(seq) and isinstance(seq[index], dict) and seq[index].get("role") == "tool":
        index += 1
    return list(seq[index:])


def _strip_internal_fields(value: Any) -> Any:
    """Remove runner-local bookkeeping fields before compact-model prompts."""
    if isinstance(value, dict):
        return {
            key: _strip_internal_fields(item)
            for key, item in value.items()
            if not str(key).startswith("_")
        }
    if isinstance(value, list):
        return [_strip_internal_fields(item) for item in value]
    if isinstance(value, tuple):
        return [_strip_internal_fields(item) for item in value]
    return value


def estimate_messages_tokens(messages: list[dict[str, Any]]) -> int:
    """Conservative rough token estimate based on serialized message content."""

    total_chars = 0
    for message in messages:
        total_chars += len(str(message.get("role", "")))
        total_chars += len(str(message.get("content", "")))
        tool_calls = message.get("tool_calls")
        if tool_calls:
            total_chars += len(json.dumps(tool_calls, ensure_ascii=False, default=str))
        total_chars += 8
    return max(1, int((total_chars / 4.0) * (4.0 / 3.0)))


def is_compaction_message(message: dict[str, str]) -> bool:
    payload = _compaction_payload(message)
    return payload is not None and payload.get("observation") in {"context_summary", "context_compaction"}


def is_llm_compaction_message(message: dict[str, str]) -> bool:
    payload = _compaction_payload(message)
    return payload is not None and payload.get("observation") == "context_compaction"


def _compaction_payload(message: dict[str, str]) -> dict[str, Any] | None:
    if message.get("role") != "user":
        return None
    try:
        payload = json.loads(message.get("content", ""))
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _latest_compaction_summary(messages: list[dict[str, Any]]) -> object | None:
    for message in reversed(messages[1:]):
        payload = _compaction_payload(message)
        if payload is None or payload.get("observation") not in {"context_summary", "context_compaction"}:
            continue
        return payload.get("summary", payload)
    return None


def _extract_summary_payload(response: ProviderResponse) -> dict[str, object]:
    try:
        payload = json.loads(response.content)
    except json.JSONDecodeError:
        try:
            payload = extract_json_object(response.content).payload
        except ExtractionError as exc:
            raise ValueError("compaction response is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("compaction response must be a JSON object")
    return _normalize_summary_payload(payload)


def _normalize_summary_payload(payload: dict[str, Any]) -> dict[str, object]:
    if any(field in payload for field in ("goal", "progress", "key_decisions", "next_steps", "critical_context")):
        progress = payload.get("progress") if isinstance(payload.get("progress"), dict) else {}
        return {
            "goal": _as_text(payload.get("goal")),
            "constraints_and_preferences": _as_list(payload.get("constraints_and_preferences")),
            "progress": {
                "done": _as_list(progress.get("done")),
                "in_progress": _as_list(progress.get("in_progress")),
                "blocked": _as_list(progress.get("blocked")),
            },
            "key_decisions": _as_list(payload.get("key_decisions")),
            "errors_and_fixes": _as_list(payload.get("errors_and_fixes")),
            "next_steps": _as_list(payload.get("next_steps")),
            "critical_context": _as_list(payload.get("critical_context")),
            "relevant_files": _as_list(payload.get("relevant_files")),
            "recent_user_feedback": _as_list(payload.get("recent_user_feedback")),
        }

    current_state = _as_text(payload.get("current_state"))
    pending_tasks = _as_list(payload.get("pending_tasks"))
    next_action = _as_text(payload.get("next_action"))
    next_steps = [*pending_tasks, *([next_action] if next_action else [])]
    return {
        "goal": _as_text(payload.get("primary_request")),
        "constraints_and_preferences": _as_list(payload.get("user_constraints")),
        "progress": {
            "done": _as_list(payload.get("validated_results")),
            "in_progress": [current_state] if current_state else [],
            "blocked": [],
        },
        "key_decisions": _as_list(payload.get("decisions")),
        "errors_and_fixes": _as_list(payload.get("errors_and_fixes")),
        "next_steps": next_steps,
        "critical_context": [current_state] if current_state else [],
        "relevant_files": _as_list(payload.get("files_and_artifacts")),
        "recent_user_feedback": [],
    }


def _build_compaction_summary_message(summary_payload: dict[str, object], compaction_index: int) -> dict[str, str]:
    payload = {
        "observation": "context_compaction",
        "summary_kind": "llm_compact_summary",
        "compaction_index": compaction_index,
        "note": "Older raw messages were compacted. Full trusted trace remains in /mnt/artifacts/agent_trace.jsonl.",
        "summary": summary_payload,
    }
    return {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)}


def _as_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def _as_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [_as_text(item) for item in value]
    return [_as_text(value)]


def safe_error_summary(exc: Exception, max_chars: int = 500) -> str:
    text = f"{type(exc).__name__}: {exc}"
    text = re.sub(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [redacted]", text)
    text = re.sub(r"(?i)(authorization\s*[:=]\s*)[^\s,;]+", r"\1[redacted]", text)
    sanitized = sanitize_for_log(text)
    if not isinstance(sanitized, str):
        sanitized = str(sanitized)
    if len(sanitized) <= max_chars:
        return sanitized
    return sanitized[: max_chars - 3] + "..."
