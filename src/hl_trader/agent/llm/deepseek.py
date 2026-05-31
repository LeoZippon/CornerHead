from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_CHAT_COMPLETIONS_PATH = "/chat/completions"
SUPPORTED_MODELS = {"deepseek-v4-flash", "deepseek-v4-pro", "deepseek-chat", "deepseek-reasoner"}
SUPPORTED_REASONING_EFFORTS = {"low", "medium", "high", "max", "xhigh"}
USER_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,512}$")
SECRET_PATTERN = re.compile(r"(sk-[A-Za-z0-9_-]{8,})")


class DeepSeekAPIError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None, retryable: bool = False) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


@dataclass(frozen=True)
class ChatMessage:
    role: str
    content: str

    def to_record(self) -> dict[str, str]:
        if self.role not in {"system", "user", "assistant"}:
            raise ValueError(f"unsupported chat role={self.role}")
        if not self.content:
            raise ValueError("chat message content cannot be empty")
        return {"role": self.role, "content": self.content}


@dataclass(frozen=True)
class DeepSeekConfig:
    api_key: str = field(repr=False)
    model: str = "deepseek-v4-flash"
    base_url: str = DEEPSEEK_BASE_URL
    timeout_seconds: float = 60.0
    max_retries: int = 2
    retry_backoff_seconds: float = 1.0
    max_tokens: int = 1200
    temperature: float = 0.0
    thinking_enabled: bool = False
    reasoning_effort: str | None = None
    user_id: str = "macroquant-hl"
    conversation_log_dir: str | Path | None = "data/llm_conversations"

    def __post_init__(self) -> None:
        if not self.api_key:
            raise ValueError("DeepSeek api_key cannot be empty")
        if not self.base_url.startswith("https://"):
            raise ValueError("DeepSeek base_url must use https")
        if self.model not in SUPPORTED_MODELS:
            raise ValueError(f"unsupported DeepSeek model={self.model}")
        if self.reasoning_effort is not None and self.reasoning_effort not in SUPPORTED_REASONING_EFFORTS:
            raise ValueError("reasoning_effort must be one of low, medium, high, max, xhigh")
        if self.user_id and not USER_ID_PATTERN.fullmatch(self.user_id):
            raise ValueError("user_id must match [A-Za-z0-9_-] and be at most 512 chars")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        if self.max_tokens <= 0:
            raise ValueError("max_tokens must be positive")
        if not 0 <= self.temperature <= 2:
            raise ValueError("temperature must be in [0, 2]")
        if self.conversation_log_dir == "":
            raise ValueError("conversation_log_dir cannot be empty; use None to disable logging")

    @property
    def chat_completions_url(self) -> str:
        return self.base_url.rstrip("/") + DEEPSEEK_CHAT_COMPLETIONS_PATH

    def safe_metadata(self) -> dict[str, Any]:
        return {
            "provider": "deepseek",
            "model": self.model,
            "base_url": self.base_url,
            "timeout_seconds": self.timeout_seconds,
            "max_retries": self.max_retries,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "thinking_enabled": self.thinking_enabled,
            "reasoning_effort": self.reasoning_effort,
            "user_id": self.user_id,
            "conversation_logging_enabled": self.conversation_log_dir is not None,
        }


@dataclass(frozen=True)
class DeepSeekResponse:
    content: str
    model: str
    usage: dict[str, Any] = field(default_factory=dict)
    response_id: str = ""

    def json_content(self) -> dict[str, Any]:
        try:
            parsed = json.loads(self.content)
        except json.JSONDecodeError as exc:
            raise ValueError("DeepSeek response content is not valid JSON") from exc
        if not isinstance(parsed, dict):
            raise ValueError("DeepSeek JSON response must be an object")
        return parsed


def load_deepseek_api_key(env_var: str = "DEEPSEEK_API_KEY", env_file: str | Path = ".env") -> str:
    value = os.environ.get(env_var)
    if value:
        return value.strip()
    path = Path(env_file)
    if not path.exists():
        return ""
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, raw_value = stripped.split("=", 1)
            if key.strip() == env_var:
                return raw_value.strip().strip("'\"")
    return ""


class DeepSeekClient:
    def __init__(self, config: DeepSeekConfig) -> None:
        self.config = config

    @classmethod
    def from_env(cls, *, model: str = "deepseek-v4-flash", env_file: str | Path = ".env") -> DeepSeekClient:
        api_key = load_deepseek_api_key(env_file=env_file)
        return cls(DeepSeekConfig(api_key=api_key, model=model))

    def chat_json(self, messages: list[ChatMessage], *, max_tokens: int | None = None) -> DeepSeekResponse:
        payload = self._payload(messages, json_mode=True, max_tokens=max_tokens)
        response = self._post_with_retries(payload)
        try:
            response.json_content()
        except ValueError as exc:
            raise DeepSeekAPIError(str(exc), retryable=False) from exc
        return response

    def _payload(self, messages: list[ChatMessage], *, json_mode: bool, max_tokens: int | None = None) -> dict[str, Any]:
        if not messages:
            raise ValueError("messages cannot be empty")
        records = [message.to_record() for message in messages]
        if json_mode and not _messages_request_json(records):
            raise ValueError("DeepSeek JSON mode requires a system or user message that explicitly mentions json")
        body: dict[str, Any] = {
            "model": self.config.model,
            "messages": records,
            "stream": False,
            "temperature": self.config.temperature,
            "max_tokens": int(max_tokens or self.config.max_tokens),
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        if self.config.user_id:
            body["user_id"] = self.config.user_id
        if self.config.thinking_enabled:
            body["thinking"] = {"type": "enabled"}
        else:
            body["thinking"] = {"type": "disabled"}
        if self.config.reasoning_effort:
            body["reasoning_effort"] = self.config.reasoning_effort
        return body

    def _post_with_retries(self, payload: dict[str, Any]) -> DeepSeekResponse:
        attempts = self.config.max_retries + 1
        last_error: DeepSeekAPIError | None = None
        for attempt in range(attempts):
            try:
                return self._post_json(payload, attempt=attempt + 1, max_attempts=attempts)
            except DeepSeekAPIError as exc:
                last_error = exc
                if not exc.retryable or attempt == attempts - 1:
                    raise
                time.sleep(self.config.retry_backoff_seconds * (2 ** attempt))
        raise last_error or DeepSeekAPIError("DeepSeek request failed")

    def _post_json(self, payload: dict[str, Any], *, attempt: int, max_attempts: int) -> DeepSeekResponse:
        started_at = _utc_now_iso()
        started_perf = time.perf_counter()
        log_path = self._conversation_log_path(started_at)
        if log_path is not None:
            _ensure_log_parent(log_path)
        request = Request(
            self.config.chat_completions_url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.config.api_key}",
            },
            method="POST",
        )
        raw_body = ""
        data: dict[str, Any] | None = None
        http_status_code: int | None = None
        try:
            with urlopen(request, timeout=self.config.timeout_seconds) as response:
                http_status_code = _response_status_code(response)
                raw_body = response.read().decode("utf-8")
                parsed_body = json.loads(raw_body)
        except HTTPError as exc:
            body = _read_http_error_body(exc)
            error = _http_error(exc, body)
            self._write_conversation_log(
                log_path,
                _conversation_log_record(
                    config=self.config,
                    payload=payload,
                    started_at=started_at,
                    completed_at=_utc_now_iso(),
                    duration_seconds=time.perf_counter() - started_perf,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    status="error",
                    http_status_code=exc.code,
                    response_body=body,
                    error=error,
                ),
            )
            raise error from exc
        except URLError as exc:
            error = DeepSeekAPIError(f"DeepSeek network error: {exc.reason}", retryable=True)
            self._write_conversation_log(
                log_path,
                _conversation_log_record(
                    config=self.config,
                    payload=payload,
                    started_at=started_at,
                    completed_at=_utc_now_iso(),
                    duration_seconds=time.perf_counter() - started_perf,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    status="error",
                    error=error,
                ),
            )
            raise error from exc
        except json.JSONDecodeError as exc:
            error = DeepSeekAPIError("DeepSeek returned invalid JSON response")
            self._write_conversation_log(
                log_path,
                _conversation_log_record(
                    config=self.config,
                    payload=payload,
                    started_at=started_at,
                    completed_at=_utc_now_iso(),
                    duration_seconds=time.perf_counter() - started_perf,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    status="error",
                    http_status_code=http_status_code,
                    response_body=raw_body,
                    error=error,
                ),
            )
            raise error from exc
        if not isinstance(parsed_body, dict):
            error = DeepSeekAPIError("DeepSeek response must be a JSON object")
            self._write_conversation_log(
                log_path,
                _conversation_log_record(
                    config=self.config,
                    payload=payload,
                    started_at=started_at,
                    completed_at=_utc_now_iso(),
                    duration_seconds=time.perf_counter() - started_perf,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    status="error",
                    http_status_code=http_status_code,
                    response_body=raw_body,
                    error=error,
                ),
            )
            raise error
        data = parsed_body
        try:
            parsed_response = _parse_response(data)
        except DeepSeekAPIError as exc:
            self._write_conversation_log(
                log_path,
                _conversation_log_record(
                    config=self.config,
                    payload=payload,
                    started_at=started_at,
                    completed_at=_utc_now_iso(),
                    duration_seconds=time.perf_counter() - started_perf,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    status="error",
                    http_status_code=http_status_code,
                    raw_response=data,
                    error=exc,
                ),
            )
            raise
        self._write_conversation_log(
            log_path,
            _conversation_log_record(
                config=self.config,
                payload=payload,
                started_at=started_at,
                completed_at=_utc_now_iso(),
                duration_seconds=time.perf_counter() - started_perf,
                attempt=attempt,
                max_attempts=max_attempts,
                status="ok",
                http_status_code=http_status_code,
                raw_response=data,
            ),
        )
        return parsed_response

    def _conversation_log_path(self, started_at: str) -> Path | None:
        if self.config.conversation_log_dir is None:
            return None
        date_key = started_at[:10].replace("-", "")
        return Path(self.config.conversation_log_dir) / "deepseek" / self.config.model / f"{date_key}.jsonl"

    def _write_conversation_log(self, path: Path | None, record: dict[str, Any]) -> None:
        if path is None:
            return
        try:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        except OSError as exc:
            raise DeepSeekAPIError(f"failed to write DeepSeek conversation log: {path}") from exc


def _parse_response(data: dict[str, Any]) -> DeepSeekResponse:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise DeepSeekAPIError("DeepSeek response missing choices")
    first_choice = choices[0]
    if not isinstance(first_choice, dict):
        raise DeepSeekAPIError("DeepSeek response choice must be an object")
    finish_reason = first_choice.get("finish_reason")
    if finish_reason in {"length", "content_filter", "insufficient_system_resource"}:
        raise DeepSeekAPIError(f"DeepSeek response stopped with finish_reason={finish_reason}")
    message = first_choice.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or not content.strip():
        raise DeepSeekAPIError("DeepSeek response content is empty")
    return DeepSeekResponse(
        content=content,
        model=str(data.get("model", "")),
        usage=dict(data.get("usage") or {}),
        response_id=str(data.get("id", "")),
    )


def _http_error(exc: HTTPError, body: str | None = None) -> DeepSeekAPIError:
    retryable = exc.code in {429, 500, 503}
    try:
        if body is None:
            body = _read_http_error_body(exc)
        detail = _extract_error_detail(body)
    except Exception:  # noqa: BLE001
        detail = None
    message = f"DeepSeek API error {exc.code}"
    if detail:
        message += f": {detail}"
    return DeepSeekAPIError(message, status_code=exc.code, retryable=retryable)


def _extract_error_detail(body: str) -> str | None:
    if not body:
        return None
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return _redact_secrets(body[:500])
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if isinstance(error, dict):
        detail = error.get("message") or error.get("code") or error.get("type")
    else:
        detail = payload.get("message") or payload.get("detail")
    if detail is None:
        return None
    return _redact_secrets(str(detail))


def _messages_request_json(records: list[dict[str, str]]) -> bool:
    for record in records:
        if record["role"] in {"system", "user"} and "json" in record["content"].lower():
            return True
    return False


def _read_http_error_body(exc: HTTPError) -> str:
    try:
        return exc.read().decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return ""


def _response_status_code(response: Any) -> int | None:
    value = getattr(response, "status", None) or getattr(response, "code", None)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _ensure_log_parent(path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise DeepSeekAPIError(f"failed to prepare DeepSeek conversation log directory: {path.parent}") from exc


def _conversation_log_record(
    *,
    config: DeepSeekConfig,
    payload: dict[str, Any],
    started_at: str,
    completed_at: str,
    duration_seconds: float,
    attempt: int,
    max_attempts: int,
    status: str,
    http_status_code: int | None = None,
    raw_response: dict[str, Any] | None = None,
    response_body: str | None = None,
    error: DeepSeekAPIError | None = None,
) -> dict[str, Any]:
    safe_payload = _redact_secrets_in_obj(payload)
    safe_response = _redact_secrets_in_obj(raw_response) if raw_response is not None else None
    safe_body = _redact_secrets(response_body) if response_body is not None else None
    record: dict[str, Any] = {
        "schema_version": 1,
        "provider": "deepseek",
        "model": config.model,
        "request_started_at": started_at,
        "request_completed_at": completed_at,
        "duration_seconds": round(max(duration_seconds, 0.0), 6),
        "attempt": attempt,
        "max_attempts": max_attempts,
        "status": status,
        "http_status_code": http_status_code,
        "request_hash": _stable_hash(safe_payload),
        "payload": safe_payload,
        "metadata": config.safe_metadata(),
    }
    if safe_response is not None:
        record["raw_response"] = safe_response
        record["response_hash"] = _stable_hash(safe_response)
        record["response_id"] = str(raw_response.get("id", ""))
        record["usage"] = dict(raw_response.get("usage") or {})
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
        return {str(key): _redact_secrets_in_obj(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_secrets_in_obj(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_secrets_in_obj(item) for item in value]
    return value


def _stable_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _redact_secrets(value: str) -> str:
    return SECRET_PATTERN.sub("sk-***", value)
