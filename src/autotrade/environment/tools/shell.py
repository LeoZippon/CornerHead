"""sandbox_shell_tool: the managed non-root shell inside the Sandbox.

docs/environment_design.md §2.2. Inside Docker the hard isolation comes from
the container (non-root user, configured network policy, read-only mounts). This module
adds logged execution, output limits, phase/write-lock checks, and an audit
reminder for hidden stderr. Path and mount permissions are enforced by the
Sandbox filesystem, not by static shell parsing.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass

from autotrade.environment.runtime import utc_now_iso

from .base import ActionField, ActionSpec, PHASE_TRAIN_VALID, ToolContext, ToolError

OUTPUT_LIMIT_CHARS = 20_000
OUTPUT_CAPTURE_LIMIT_CHARS = 200_000
DEFAULT_TIMEOUT_SECONDS = 120.0
# Hard upper bound the Agent may request for a single command. The default stays
# 120s for quick probes, but a legitimate heavy probe (e.g. a long DuckDB scan)
# may opt up to this cap — granting more exploration freedom without unbounding it.
MAX_TIMEOUT_SECONDS = 600.0
# Advisory (not enforced): nudge the Agent away from hiding stderr, which breaks audit.
STDERR_SUPPRESSION_RE = re.compile(r"2\s*>\s*/dev/null|&>\s*/dev/null|/dev/null\s+2\s*>\s*&\s*1")
STDERR_SUPPRESSION_REMINDER = (
    "stderr 被重定向到 /dev/null：错误输出对审计与调试很重要，请保留 stderr（去掉 2>/dev/null 等）。"
)
READ_ONLY_COMMANDS = {
    "awk",
    "cat",
    "cut",
    "du",
    "file",
    "find",
    "grep",
    "head",
    "jq",
    "less",
    "ls",
    "nl",
    "pwd",
    "rg",
    "sort",
    "stat",
    "tail",
    "wc",
}
SEARCH_COMMANDS = {"ag", "ack", "find", "grep", "locate", "rg", "which", "whereis"}
LIST_COMMANDS = {"du", "ls", "tree"}
SHELL_NEUTRAL_COMMANDS = {"echo", "printf", "true", "false", ":"}
WRITE_COMMANDS = {
    "apply_patch",
    "chmod",
    "chown",
    "cp",
    "dd",
    "install",
    "ln",
    "mkdir",
    "mv",
    "rm",
    "rmdir",
    "tee",
    "touch",
    "truncate",
}


@dataclass(frozen=True)
class ShellResult:
    call_id: str
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    stdout_capture_truncated: bool = False
    stderr_capture_truncated: bool = False
    stdout_path: str | None = None
    stderr_path: str | None = None
    host_stdout_path: str | None = None
    host_stderr_path: str | None = None
    command_kind: str = "unknown"
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    stderr_suppression_reminder: str | None = None

    def to_record(self) -> dict[str, object]:
        record = {
            "call_id": self.call_id,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "timed_out": self.timed_out,
            "stdout_truncated": self.stdout_truncated,
            "stderr_truncated": self.stderr_truncated,
            "stdout_capture_truncated": self.stdout_capture_truncated,
            "stderr_capture_truncated": self.stderr_capture_truncated,
            "stdout_path": self.stdout_path,
            "stderr_path": self.stderr_path,
            "host_stdout_path": self.host_stdout_path,
            "host_stderr_path": self.host_stderr_path,
            "command_kind": self.command_kind,
            "timeout_seconds": self.timeout_seconds,
        }
        if self.stderr_suppression_reminder:
            record["stderr_suppression_reminder"] = self.stderr_suppression_reminder
        return record


class SandboxShellTool:
    name = "sandbox_shell_tool"
    spec = ActionSpec(
        action="shell",
        tool_name=name,
        description=(
            "Run one managed bash command inside the Agent sandbox. Use for data inspection, "
            "debugging, and controlled writes to workspace/output/models. For large parquet "
            "tables (events, text_index, intraday_1min), prefer DuckDB count/limit, Parquet "
            "metadata, or column-select reads over full pd.read_parquet(). Keep stderr visible: do "
            "NOT use `2>/dev/null` / `2>&1 >/dev/null` to hide errors — suppressed stderr breaks audit."
        ),
        fields=(
            ActionField(
                "command",
                "string",
                required=True,
                description=(
                    "Bash command executed in the sandbox. Prefer explicit read/list/search commands; "
                    "write only under workspace/output/models. Keep stderr visible — never redirect it to "
                    "/dev/null (`2>/dev/null`)."
                ),
            ),
            ActionField(
                "max_output_chars",
                "integer",
                required=False,
                default=OUTPUT_LIMIT_CHARS,
                min_value=1,
                max_value=OUTPUT_LIMIT_CHARS,
                description=(
                    "Maximum stdout/stderr characters returned inline to the Agent context. "
                    "Large captured output is persisted to trace/log paths."
                ),
            ),
            ActionField(
                "timeout_seconds",
                "integer",
                required=False,
                default=int(DEFAULT_TIMEOUT_SECONDS),
                min_value=1,
                max_value=MAX_TIMEOUT_SECONDS,
                description=(
                    f"Per-command timeout in seconds (default {int(DEFAULT_TIMEOUT_SECONDS)}, "
                    f"max {int(MAX_TIMEOUT_SECONDS)}); use smaller values for quick probes and "
                    "raise it only for a genuinely heavy scan."
                ),
            ),
        ),
        read_only=False,
        destructive=True,
        concurrency_safe=False,
        max_result_chars=OUTPUT_LIMIT_CHARS,
        result_policy="bounded_inline_with_persisted_captured_output",
        allowed_modes=("fold", "meta_learning"),
    )

    def __init__(
        self,
        ctx: ToolContext,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_timeout_seconds: float = MAX_TIMEOUT_SECONDS,
    ) -> None:
        self.ctx = ctx
        # ``timeout_seconds`` is the default used when a command omits one;
        # ``max_timeout_seconds`` is the hard cap a command may opt up to.
        self.timeout_seconds = timeout_seconds
        self.max_timeout_seconds = max_timeout_seconds

    def run(
        self,
        command: str,
        *,
        max_output_chars: int | None = OUTPUT_LIMIT_CHARS,
        timeout_seconds: int | None = None,
    ) -> ShellResult:
        if self.ctx.phase != PHASE_TRAIN_VALID:
            raise ToolError("sandbox_shell_tool is closed in test/held-out phases")
        self.ctx.require_writable(tool=self.name)
        inline_limit = _validate_output_limit(max_output_chars)
        timeout_limit = _validate_timeout(
            timeout_seconds, default_seconds=self.timeout_seconds, max_seconds=self.max_timeout_seconds
        )
        command_kind = _classify_command(_safe_split(command))
        started = utc_now_iso()
        result = self.ctx.executor.run(
            ["/bin/bash", "-lc", command],
            cwd=self.ctx.paths.agent,
            timeout_seconds=timeout_limit,
            user="agent",
            max_output_chars=OUTPUT_CAPTURE_LIMIT_CHARS,
        )
        exit_code = result.exit_code
        stdout_truncated = len(result.stdout) > inline_limit or result.stdout_truncated
        stderr_truncated = len(result.stderr) > inline_limit or result.stderr_truncated
        stdout = result.stdout[:inline_limit]
        stderr = result.stderr[:inline_limit]
        stdout_ref = (
            self.ctx.store_tool_result(tool=self.name, kind="stdout", content=result.stdout)
            if stdout_truncated
            else {}
        )
        stderr_ref = (
            self.ctx.store_tool_result(tool=self.name, kind="stderr", content=result.stderr)
            if stderr_truncated
            else {}
        )
        timed_out = exit_code == 124 or "timeout after" in result.stderr.lower()
        call_id = self.ctx.trace.emit(
            "shell",
            {
                "tool": self.name,
                "tool_spec": self.spec.to_record(),
                "command": command,
                "command_kind": command_kind,
                "max_output_chars": inline_limit,
                "timeout_seconds": timeout_limit,
                "exit_code": exit_code,
                "stdout": stdout,
                "stderr": stderr,
                "timed_out": timed_out,
                "stdout_truncated": stdout_truncated,
                "stderr_truncated": stderr_truncated,
                "stdout_capture_truncated": result.stdout_truncated,
                "stderr_capture_truncated": result.stderr_truncated,
                **stdout_ref,
                **stderr_ref,
                "started_at": started,
            },
            step_id=self.ctx.current_step_id,
        )
        return ShellResult(
            call_id=call_id,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
            stdout_capture_truncated=result.stdout_truncated,
            stderr_capture_truncated=result.stderr_truncated,
            stdout_path=stdout_ref.get("stdout_path"),
            stderr_path=stderr_ref.get("stderr_path"),
            host_stdout_path=stdout_ref.get("host_stdout_path"),
            host_stderr_path=stderr_ref.get("host_stderr_path"),
            command_kind=command_kind,
            timeout_seconds=timeout_limit,
            stderr_suppression_reminder=(
                STDERR_SUPPRESSION_REMINDER if STDERR_SUPPRESSION_RE.search(command) else None
            ),
        )

def _validate_output_limit(value: int | None) -> int:
    if value is None:
        return OUTPUT_LIMIT_CHARS
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolError("max_output_chars must be an integer")
    if value < 1 or value > OUTPUT_LIMIT_CHARS:
        raise ToolError(f"max_output_chars must be between 1 and {OUTPUT_LIMIT_CHARS}")
    return value


def _validate_timeout(value: int | None, *, default_seconds: float, max_seconds: float) -> float:
    if value is None:
        return float(default_seconds)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolError("timeout_seconds must be an integer")
    if value < 1 or value > max_seconds:
        raise ToolError(f"timeout_seconds must be between 1 and {int(max_seconds)}")
    return float(value)


def _classify_command(tokens: list[str]) -> str:
    """Best-effort audit label only; permissions are enforced by Docker/filesystem."""
    words = [_basename(token) for token in tokens if token and not token.startswith("-")]
    if not words:
        return "unknown"
    meaningful = [word for word in words if word not in SHELL_NEUTRAL_COMMANDS]
    if not meaningful:
        return "neutral"
    first = meaningful[0]
    if first in WRITE_COMMANDS:
        return "write"
    if first in SEARCH_COMMANDS:
        return "search"
    if first in LIST_COMMANDS:
        return "list"
    if first in READ_ONLY_COMMANDS:
        return "read"
    return "unknown"


def _safe_split(command: str) -> list[str]:
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        return []


def _basename(token: str) -> str:
    return token.rstrip("/").rsplit("/", 1)[-1]
