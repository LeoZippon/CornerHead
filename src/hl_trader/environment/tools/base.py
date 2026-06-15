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
from hl_trader.environment.runtime import AgentTraceWriter, RunManifest, SandboxPaths

PHASE_TRAIN_VALID = "train_valid"
PHASE_FROZEN = "frozen"


class ToolError(RuntimeError):
    """Explicit, agent-visible tool failure with a fixable reason."""


@dataclass
class ToolContext:
    paths: SandboxPaths
    manifest: RunManifest
    trace: AgentTraceWriter
    proxy: LLMProxy | None = None
    # Dedicated provider for NL scoring; falls back to the main-conversation proxy.
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
