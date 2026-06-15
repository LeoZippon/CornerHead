"""sandbox_shell_tool: the managed non-root shell inside the Sandbox.

docs/environment_design.md 4.2. Inside Docker the hard isolation comes from
the container (non-root user, --network none, read-only mounts). This module
adds the logged execution, output limits, and the textual path guard, and is
also what the local (non-Docker) driver uses, where the guard is best-effort.
"""

from __future__ import annotations

from dataclasses import dataclass

from hl_trader.environment.runtime import utc_now_iso

from .base import PHASE_TRAIN_VALID, ToolContext, ToolError

OUTPUT_LIMIT_CHARS = 20_000
DEFAULT_TIMEOUT_SECONDS = 120.0


@dataclass(frozen=True)
class ShellResult:
    call_id: str
    exit_code: int
    stdout: str
    stderr: str

    def to_record(self) -> dict[str, object]:
        return {
            "call_id": self.call_id,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
        }


class SandboxShellTool:
    name = "sandbox_shell_tool"

    def __init__(self, ctx: ToolContext, *, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        self.ctx = ctx
        self.timeout_seconds = timeout_seconds

    def run(self, command: str, *, readonly_review: bool = False) -> ShellResult:
        if self.ctx.phase != PHASE_TRAIN_VALID and not readonly_review:
            raise ToolError("sandbox_shell_tool is closed in test/held-out phases")
        if self.ctx.phase == PHASE_TRAIN_VALID:
            self.ctx.require_writable(tool=self.name)
        self._guard_paths(command)
        started = utc_now_iso()
        result = self.ctx.executor.run(
            ["/bin/bash", "-lc", command],
            cwd=self.ctx.paths.agent,
            timeout_seconds=self.timeout_seconds,
            user="agent",
        )
        exit_code = result.exit_code
        stdout = result.stdout[:OUTPUT_LIMIT_CHARS]
        stderr = result.stderr[:OUTPUT_LIMIT_CHARS]
        call_id = self.ctx.trace.emit(
            "shell",
            {
                "tool": self.name,
                "command": command,
                "exit_code": exit_code,
                "stdout": stdout,
                "stderr": stderr,
                "started_at": started,
                "readonly_review": readonly_review,
            },
            step_id=self.ctx.current_step_id,
        )
        return ShellResult(call_id=call_id, exit_code=exit_code, stdout=stdout, stderr=stderr)

    def _guard_paths(self, command: str) -> None:
        forbidden = [str(self.ctx.paths.test), "/mnt/snapshots/test", "/var/run/docker.sock"]
        for needle in forbidden:
            if needle and needle in command:
                raise ToolError(f"command references a forbidden path: {needle}")
