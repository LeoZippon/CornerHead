"""Shared tool context and errors (docs/environment_design.md chapter 4).

Every entrypoint resolves paths, decision times, fold info, and run settings
from the run manifest. Tools reject agent-supplied absolute paths, future
times, or anything outside the permission boundary by simply not accepting
such parameters.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from hl_trader.environment.executor import LocalExecutor
from hl_trader.environment.llm.proxy import LLMProxy
from hl_trader.environment.runtime import AgentTraceWriter, RunManifest, SandboxPaths, new_id, sanitize_for_log

PHASE_TRAIN_VALID = "train_valid"
PHASE_FROZEN = "frozen"


class ToolError(RuntimeError):
    """Explicit, agent-visible tool failure with a fixable reason."""

    def __init__(
        self,
        message: str,
        *,
        error_type: str = "tool_error",
        reason: str | None = None,
        retry_hint: str | None = None,
        blocked_target: str | None = None,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.reason = reason
        self.retry_hint = retry_hint
        self.blocked_target = blocked_target
        self.details = details or {}

    def to_record(self) -> dict[str, object]:
        return {
            key: value
            for key, value in {
                "error_type": self.error_type,
                "reason": self.reason,
                "retry_hint": self.retry_hint,
                "blocked_target": self.blocked_target,
                "details": self.details or None,
            }.items()
            if value is not None
        }


class ToolSchemaError(ToolError):
    """Action payload failed the Runner-side tool schema."""


@dataclass(frozen=True)
class ActionField:
    """Small native-tool schema field used by the provider-portable Runner.

    This intentionally stays lighter than Pydantic/JSON Schema: provider-native
    tool calls carry one JSON object per call, while the Runner owns hard
    validation.
    """

    name: str
    type_name: str
    required: bool = False
    default: object = None
    choices: tuple[object, ...] = ()
    min_value: float | None = None
    max_value: float | None = None
    description: str = ""

    def validate(self, payload: dict[str, object]) -> object:
        if self.name not in payload or payload.get(self.name) is None:
            if self.required:
                raise ToolSchemaError(f"missing required field: {self.name}")
            return self.default
        value = payload[self.name]
        if self.type_name == "string":
            if not isinstance(value, str):
                raise ToolSchemaError(f"{self.name} must be a string")
        elif self.type_name == "integer":
            if isinstance(value, bool) or not isinstance(value, int):
                raise ToolSchemaError(f"{self.name} must be an integer")
        elif self.type_name == "number":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ToolSchemaError(f"{self.name} must be a number")
        elif self.type_name == "boolean":
            if not isinstance(value, bool):
                raise ToolSchemaError(f"{self.name} must be a boolean")
        else:
            raise ToolSchemaError(f"unsupported schema type for {self.name}: {self.type_name}")
        if self.choices and value not in self.choices:
            raise ToolSchemaError(f"{self.name} must be one of {list(self.choices)}")
        if self.min_value is not None and isinstance(value, (int, float)) and value < self.min_value:
            raise ToolSchemaError(f"{self.name} must be >= {self.min_value}")
        if self.max_value is not None and isinstance(value, (int, float)) and value > self.max_value:
            raise ToolSchemaError(f"{self.name} must be <= {self.max_value}")
        return value

    def to_record(self) -> dict[str, object]:
        return {
            "name": self.name,
            "type": self.type_name,
            "required": self.required,
            "default": self.default,
            "choices": list(self.choices),
            "min_value": self.min_value,
            "max_value": self.max_value,
            "description": self.description,
        }

    def to_json_schema(self) -> dict[str, object]:
        """Render this field as a JSON Schema property for native tool calling."""
        schema: dict[str, object] = {"type": self.type_name}
        if self.choices:
            schema["enum"] = list(self.choices)
        if self.min_value is not None:
            schema["minimum"] = self.min_value
        if self.max_value is not None:
            schema["maximum"] = self.max_value
        descriptions: list[str] = []
        if self.description:
            descriptions.append(self.description)
        if not self.required and self.default is not None:
            descriptions.append(f"Defaults to {self.default!r}.")
        if descriptions:
            schema["description"] = " ".join(descriptions)
        return schema


@dataclass(frozen=True)
class ActionSpec:
    """Runner-facing tool metadata, inspired by typed tool protocols.

    Each action carries enough metadata for validation, audit, result budgeting,
    provider-native tool schema rendering, and safe read-only parallelism.
    """

    action: str
    tool_name: str
    description: str
    fields: tuple[ActionField, ...] = ()
    read_only: bool = False
    destructive: bool = False
    concurrency_safe: bool = False
    max_result_chars: int | None = None
    result_policy: str = "inline"
    allowed_modes: tuple[str, ...] = ("fold",)
    schema_version: int = 1

    def validate(self, payload: dict[str, object], *, mode: str) -> dict[str, object]:
        if mode not in self.allowed_modes:
            raise ToolSchemaError(f"{self.action} is not available in {mode} mode")
        allowed_fields = {field.name for field in self.fields} | {"action"}
        unknown = sorted(set(payload) - allowed_fields)
        if unknown:
            raise ToolSchemaError(f"unknown field(s) for {self.action}: {unknown}")
        args = {field.name: field.validate(payload) for field in self.fields}
        return args

    def to_record(self) -> dict[str, object]:
        return {
            "action": self.action,
            "tool_name": self.tool_name,
            "schema_version": self.schema_version,
            "description": self.description,
            "fields": [field.to_record() for field in self.fields],
            "read_only": self.read_only,
            "destructive": self.destructive,
            "concurrency_safe": self.concurrency_safe,
            "max_result_chars": self.max_result_chars,
            "result_policy": self.result_policy,
            "allowed_modes": list(self.allowed_modes),
        }

    def to_tool_schema(self) -> dict[str, object]:
        """Render this action as an OpenAI-compatible function tool definition.

        The function name is the ``action`` token, so the provider's tool-call
        name maps straight back to the Runner dispatch; the Runner still owns
        hard validation via :meth:`validate`.
        """
        return {
            "type": "function",
            "function": {
                "name": self.action,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {field.name: field.to_json_schema() for field in self.fields},
                    "required": [field.name for field in self.fields if field.required],
                    "additionalProperties": False,
                },
            },
        }


@dataclass
class ToolContext:
    paths: SandboxPaths
    manifest: RunManifest
    trace: AgentTraceWriter
    proxy: LLMProxy | None = None
    # Dedicated provider for NL Sub Agent calls; falls back to the main-conversation proxy.
    nl_proxy: LLMProxy | None = None
    executor: object | None = None
    phase: str = PHASE_TRAIN_VALID
    write_locked: bool = False
    current_step_id: str | None = None
    extra: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.executor is None:
            self.executor = LocalExecutor(self.paths)

    @property
    def effective_nl_proxy(self) -> LLMProxy | None:
        return self.nl_proxy or self.proxy

    def require_phase(self, phase: str, *, tool: str) -> None:
        if self.phase != phase:
            raise ToolError(f"{tool} is not available in phase {self.phase}")

    def require_writable(self, *, tool: str) -> None:
        if self.write_locked:
            raise ToolError(f"{tool} rejected: fold writes are locked")

    def store_tool_result(self, *, tool: str, kind: str, content: str) -> dict[str, object]:
        """Persist an oversized tool result outside the model context budget."""
        result_id = new_id("tool_result")
        result_dir = self.paths.logs / "tool_results" / result_id
        result_dir.mkdir(parents=True, exist_ok=True)
        path = result_dir / f"{kind}.txt"
        path.write_text(str(sanitize_for_log(content)), encoding="utf-8", errors="replace")
        mapped_path: str | None
        try:
            mapped_path = self.executor.map_path(path) if self.executor is not None else str(path)
        except Exception:  # noqa: BLE001 - storage path is still useful on host
            mapped_path = None
        return {
            "result_id": result_id,
            f"{kind}_path": mapped_path,
            f"host_{kind}_path": str(path),
        }
