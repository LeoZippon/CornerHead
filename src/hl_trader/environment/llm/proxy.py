"""Host-side LLM Proxy boundary (docs/environment_design.md chapter 6).

All provider calls go through an :class:`LLMProxy`. API keys live only on the
host side inside provider adapters; sandbox code never sees them. Callers are
responsible for writing call details to the documented locations (main
conversation -> agent_trace.jsonl ``llm_call`` events; NL scoring ->
``nl_output/nl_llm_calls.jsonl``).
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field

from .deepseek import ChatMessage, DeepSeekClient, DeepSeekConfig, load_deepseek_api_key


class LLMProxyError(RuntimeError):
    def __init__(self, message: str, *, timeout: bool = False) -> None:
        super().__init__(message)
        self.timeout = timeout


@dataclass(frozen=True)
class ProviderResponse:
    """Normalized provider response handed back to Environment callers."""

    content: str
    provider: str
    model: str
    reasoning_content: str = ""
    usage: dict[str, object] = field(default_factory=dict)
    response_id: str = ""

    def to_record(self) -> dict[str, object]:
        return {
            "content": self.content,
            "reasoning_content": self.reasoning_content,
            "provider": self.provider,
            "model": self.model,
            "usage": dict(self.usage),
            "response_id": self.response_id,
        }


class LLMProxy(abc.ABC):
    """Single entrypoint for provider requests from Runner and backtest_tool."""

    provider: str = "unknown"
    model: str = "unknown"

    @abc.abstractmethod
    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        json_mode: bool,
        timeout_seconds: float,
        max_tokens: int | None = None,
    ) -> ProviderResponse:
        """Run one provider request with a hard per-call timeout."""


class DeepSeekProxy(LLMProxy):
    provider = "deepseek"

    def __init__(self, client: DeepSeekClient) -> None:
        self.client = client
        self.model = client.config.model

    @classmethod
    def from_env(
        cls, *, model: str = "deepseek-v4-pro", env_file: str = ".env", thinking_enabled: bool = True
    ) -> "DeepSeekProxy":
        """Reasoning is enabled by default; the separated reasoning_content is
        logged while only the final content reaches JSON extraction."""
        api_key = load_deepseek_api_key(env_file=env_file)
        if not api_key:
            raise LLMProxyError("DEEPSEEK_API_KEY is not configured on the host")
        return cls(DeepSeekClient(DeepSeekConfig(api_key=api_key, model=model, thinking_enabled=thinking_enabled)))

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        json_mode: bool,
        timeout_seconds: float,
        max_tokens: int | None = None,
    ) -> ProviderResponse:
        if timeout_seconds <= 0:
            raise LLMProxyError("provider calls require a positive timeout")
        chat_messages = [ChatMessage(role=str(m["role"]), content=str(m["content"])) for m in messages]
        if abs(self.client.config.timeout_seconds - timeout_seconds) > 1e-9:
            config = DeepSeekConfig(
                **{**_config_kwargs(self.client.config), "timeout_seconds": timeout_seconds}
            )
            client = DeepSeekClient(config)
        else:
            client = self.client
        try:
            if json_mode:
                response = client.chat_json(chat_messages, max_tokens=max_tokens)
            else:
                payload = client._payload(chat_messages, json_mode=False, max_tokens=max_tokens)
                response = client._post_with_retries(payload)
        except Exception as exc:  # noqa: BLE001 - normalize provider errors at the boundary
            timeout = "timed out" in str(exc).lower() or "timeout" in str(exc).lower()
            raise LLMProxyError(f"{self.provider} request failed: {exc}", timeout=timeout) from exc
        return ProviderResponse(
            content=response.content,
            provider=self.provider,
            model=response.model or self.model,
            reasoning_content=response.reasoning_content,
            usage=dict(response.usage),
            response_id=response.response_id,
        )


class ScriptedLLM(LLMProxy):
    """Deterministic in-memory proxy for tests and dry runs."""

    provider = "scripted"
    model = "scripted-v0"

    def __init__(self, responses: list[ProviderResponse | str | Exception]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        json_mode: bool,
        timeout_seconds: float,
        max_tokens: int | None = None,
    ) -> ProviderResponse:
        self.calls.append({"messages": messages, "json_mode": json_mode, "timeout_seconds": timeout_seconds})
        if not self._responses:
            raise LLMProxyError("scripted LLM has no responses left")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, str):
            return ProviderResponse(content=item, provider=self.provider, model=self.model)
        return item


def _config_kwargs(config: DeepSeekConfig) -> dict[str, object]:
    return {
        "api_key": config.api_key,
        "model": config.model,
        "base_url": config.base_url,
        "timeout_seconds": config.timeout_seconds,
        "max_retries": config.max_retries,
        "retry_backoff_seconds": config.retry_backoff_seconds,
        "max_tokens": config.max_tokens,
        "temperature": config.temperature,
        "thinking_enabled": config.thinking_enabled,
        "reasoning_effort": config.reasoning_effort,
        "user_id": config.user_id,
        "conversation_log_dir": config.conversation_log_dir,
    }
