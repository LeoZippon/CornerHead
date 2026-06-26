"""sandbox_shell_tool: the managed non-root shell inside the Sandbox.

docs/environment_design.md 4.2. Inside Docker the hard isolation comes from
the container (non-root user, --network none, read-only mounts). This module
adds logged execution, output limits, and a light contract guard. The guard is
not a full Bash parser; it gives fast, actionable feedback for clear policy
violations and leaves hard isolation to the Sandbox.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path

from hl_trader.environment.runtime import utc_now_iso

from .base import ActionField, ActionSpec, PHASE_TRAIN_VALID, ToolContext, ToolError

OUTPUT_LIMIT_CHARS = 20_000
OUTPUT_CAPTURE_LIMIT_CHARS = 200_000
DEFAULT_TIMEOUT_SECONDS = 120.0
ABSOLUTE_PATH_RE = re.compile(r"(?<![\w.:\-/])/(?:[^\s'\";|&<>`$(){}]+)")
CONTROL_OPERATOR_RE = re.compile(r"(&&|\|\||[;&|\n])")
CONTROL_OPERATORS = {"&&", "||", ";", "&", "|"}
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
TARGET_ONLY_WRITE_COMMANDS = {"cp", "dd", "tee"}
IN_PLACE_WRITE_COMMANDS = {"sed", "perl"}
WRITE_REDIRECT_OPERATORS = {">", ">>", ">|", "<>", "1>", "1>>", "2>", "2>>", "&>", "&>>"}
COMBINED_WRITE_REDIRECT_RE = re.compile(r"^(?:(?:\d)?(?:>>?|>\||<>)|&>>?)(?P<target>.+)$")
EMBEDDED_WRITE_REDIRECT_RE = re.compile(
    r"(?:^|[\s({;])(?:(?:\d)?(?:>>?|>\||<>)|&>>?)\s*['\"]?"
    r"(?P<target>(?:/|\./|workspace/|output/|agent_output/|models/|model_artifacts/)"
    r"[^\s'\";|&<>`$(){}]+)"
)
COMMAND_SUBSTITUTION_NETWORK_RE = re.compile(
    r"(?:\$\([^)]*|`[^`]*)(?:\b(?:curl|wget|pip3?|pipx|conda|mamba|micromamba|npm|npx|pnpm|yarn|poetry|pipenv|uv|hf|huggingface-cli|git)\b)",
    re.IGNORECASE,
)
FD_DUP_REDIRECT_RE = re.compile(r"^\d?>&\d+;?$")
# Heredoc body = pure data, never shell syntax. Stripping it keeps the guard from
# reading interpreter code (e.g. ``python3 << 'EOF' ... x > 150 ... EOF``) as
# shell redirections/paths. The opener line (and any real redirect on it) is kept.
HEREDOC_RE = re.compile(
    r"<<-?\s*(?P<q>['\"]?)(?P<delim>[A-Za-z_][A-Za-z0-9_]*)(?P=q)"
    r"(?P<rest>[^\n]*)\n(?:.*?\n)??[ \t]*(?P=delim)[ \t]*(?=\n|$)",
    re.DOTALL,
)
DEV_NULL = "/dev/null"
PYTHON_BINARIES = {"python", "python3", "python3.11"}
SHELL_BINARIES = {"bash", "sh", "zsh"}
ENV_BINARIES = {"env"}
INSTALL_OR_NETWORK_COMMANDS = {
    "curl",
    "wget",
    "pip",
    "pip3",
    "pipx",
    "conda",
    "mamba",
    "micromamba",
    "npm",
    "npx",
    "pnpm",
    "yarn",
    "poetry",
    "pipenv",
    "uv",
    "hf",
    "huggingface-cli",
}
INSTALL_OR_NETWORK_SUBCOMMANDS = {
    "clone",
    "fetch",
    "pull",
    "submodule",
    "lfs",
    "install",
    "download",
    "add",
    "update",
    "sync",
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

    def to_record(self) -> dict[str, object]:
        return {
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


class SandboxShellTool:
    name = "sandbox_shell_tool"
    spec = ActionSpec(
        action="shell",
        tool_name=name,
        description=(
            "Run one managed bash command inside the Agent sandbox. Use for data inspection, "
            "debugging, and controlled writes to workspace/output/models. For large parquet "
            "tables (events, text_index, intraday_1min), prefer DuckDB count/limit, Parquet "
            "metadata, or column-select reads over full pd.read_parquet(); do not hide stderr."
        ),
        fields=(
            ActionField(
                "command",
                "string",
                required=True,
                description=(
                    "Bash command executed in the sandbox. Prefer explicit read/list/search commands; "
                    "write only under workspace/output/models and keep stderr visible."
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
                max_value=DEFAULT_TIMEOUT_SECONDS,
                description="Per-command timeout in seconds; use smaller values for quick probes.",
            ),
        ),
        read_only=False,
        destructive=True,
        concurrency_safe=False,
        max_result_chars=OUTPUT_LIMIT_CHARS,
        result_policy="bounded_inline_with_persisted_captured_output",
        allowed_modes=("fold", "meta_learning"),
    )

    def __init__(self, ctx: ToolContext, *, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        self.ctx = ctx
        self.timeout_seconds = timeout_seconds

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
        timeout_limit = _validate_timeout(timeout_seconds, max_timeout_seconds=self.timeout_seconds)
        # Guards inspect the shell skeleton (heredoc bodies stripped) so interpreter
        # code is never parsed as shell syntax; the original command still executes.
        guard_command = _strip_heredoc_bodies(command)
        guard_tokens = _safe_split(guard_command)
        command_kind = _classify_command(guard_tokens, guard_command)
        self._guard_install_or_network_command(guard_command, guard_tokens)
        self._guard_paths(guard_command)
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
        )

    def _guard_paths(self, command: str) -> None:
        command = _strip_heredoc_bodies(command)
        tokens = _safe_split(command)
        segments = _command_segments(tokens) if tokens else [tokens]
        if not segments:
            segments = [[]]
        single_segment = len(segments) == 1
        for segment in segments:
            segment_command = command if single_segment else shlex.join(segment)
            path_refs = _path_references(segment_command, segment)
            command_write_like = _looks_command_write_like(segment, segment_command)
            write_targets = _write_targets(segment)
            write_targets.update(_unquoted_shell_write_targets(segment_command))
            for ref in path_refs:
                if _is_dev_null(ref):
                    continue
                self._guard_one_path(ref, write_like=command_write_like or _is_write_target(ref, write_targets))
            for ref in write_targets:
                if _is_dev_null(ref) or ref in {_normalize_ref(path) for path in path_refs}:
                    continue
                self._guard_one_path(ref, write_like=True)

    def _guard_one_path(self, raw: str, *, write_like: bool) -> None:
        raw = _normalize_ref(raw)
        if raw in {"", "/", DEV_NULL}:
            return
        if raw in {"/mnt", "/mnt/"}:
            self._guard_host_path(self.ctx.paths.root, write_like=write_like)
            return
        if raw.startswith("/mnt/"):
            host_path = _container_to_host_path(raw, self.ctx.paths.root)
            self._guard_host_path(host_path, write_like=write_like, allow_snapshot_binding=raw.startswith("/mnt/snapshot"))
            return
        path = Path(raw)
        if not path.is_absolute():
            path = (self.ctx.paths.agent / raw).resolve()
        else:
            path = path.resolve()
        self._guard_host_path(path, write_like=write_like)

    def _guard_host_path(self, path: Path, *, write_like: bool, allow_snapshot_binding: bool = False) -> None:
        if _is_relative_to(path, self.ctx.paths.test):
            raise ToolError(
                f"command references a forbidden path: {path}",
                error_type="path_guard",
                reason="test/held-out snapshot data is not visible to the Agent",
                retry_hint="Use /mnt/snapshot, /mnt/snapshots/train, or /mnt/snapshots/valid when they are visible in this phase.",
                blocked_target=str(path),
            )
        if str(path) == "/var/run/docker.sock":
            raise ToolError(
                "command references a forbidden path: /var/run/docker.sock",
                error_type="path_guard",
                reason="the Docker socket is outside the Agent sandbox contract",
                retry_hint="Use the provided sandbox tools instead of controlling Docker from inside the Agent session.",
                blocked_target="/var/run/docker.sock",
            )
        if _is_relative_to(path, self.ctx.paths.root / "runtime"):
            if allow_snapshot_binding and _is_relative_to(path, self.ctx.paths.current_snapshot):
                if write_like:
                    raise _read_only_path_error(path)
                return
            raise ToolError(
                f"command references a forbidden runtime path: {path}",
                error_type="path_guard",
                reason="runtime internals are managed by the Environment",
                retry_hint="Read the mounted /mnt/snapshot view or /mnt/artifacts/run_manifest.json instead of runtime internals.",
                blocked_target=str(path),
            )
        writable_roots = (self.ctx.paths.workspace, self.ctx.paths.agent_output, self.ctx.paths.model_artifacts)
        if any(_is_relative_to(path, root) for root in writable_roots):
            return
        read_only_roots = (
            self.ctx.paths.artifacts,
            self.ctx.paths.snapshot,
            self.ctx.paths.current_snapshot,
            self.ctx.paths.train,
            self.ctx.paths.valid,
        )
        if any(_is_relative_to(path, root) for root in read_only_roots):
            if write_like:
                raise _read_only_path_error(path)
            return
        if _is_relative_to(path, self.ctx.paths.root):
            if write_like:
                raise ToolError(
                    f"write-like command references an unmanaged sandbox path: {path}",
                    error_type="path_guard",
                    reason="writes are only part of the experiment contract under workspace, output, or models",
                    retry_hint=(
                        "Write scratch files to /mnt/agent/workspace/..., strategy code to "
                        "/mnt/agent/output/..., or model parameters to /mnt/agent/models/..."
                    ),
                    blocked_target=str(path),
                )
            return
        raise ToolError(
            f"command references a path outside the sandbox boundary: {path}",
            error_type="path_guard",
            reason="absolute host/system paths are outside the Agent sandbox",
            retry_hint="Use the documented /mnt/... sandbox paths.",
            blocked_target=str(path),
        )

    def _guard_install_or_network_command(self, command: str, tokens: list[str]) -> None:
        if _is_meta_learning_run(self.ctx.manifest.get("kind")):
            return
        blocked = _blocked_install_or_network_command(tokens, command=command)
        if blocked:
            raise ToolError(
                "ordinary Fold shell cannot install packages or download from the network; "
                f"blocked command: {blocked}",
                error_type="network_or_install_blocked",
                reason="ordinary Fold sessions must use the prepared sandbox environment",
                retry_hint=(
                    "Use installed packages listed in /mnt/artifacts/runtime_env.json, or move external research/downloads "
                    "to the meta-learning phase where network access is explicitly configured."
                ),
                blocked_target=blocked,
            )


def _validate_output_limit(value: int | None) -> int:
    if value is None:
        return OUTPUT_LIMIT_CHARS
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolError("max_output_chars must be an integer")
    if value < 1 or value > OUTPUT_LIMIT_CHARS:
        raise ToolError(f"max_output_chars must be between 1 and {OUTPUT_LIMIT_CHARS}")
    return value


def _validate_timeout(value: int | None, *, max_timeout_seconds: float) -> float:
    if value is None:
        return float(max_timeout_seconds)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolError("timeout_seconds must be an integer")
    if value < 1 or value > max_timeout_seconds:
        raise ToolError(f"timeout_seconds must be between 1 and {int(max_timeout_seconds)}")
    return float(value)


def _read_only_path_error(path: Path) -> ToolError:
    return ToolError(
        f"write-like command references a read-only path: {path}",
        error_type="path_guard",
        reason="the target is mounted or managed as read-only",
        retry_hint="Copy the file into /mnt/agent/workspace before editing, or write formal changes under /mnt/agent/output or /mnt/agent/models.",
        blocked_target=str(path),
    )


def _classify_command(tokens: list[str], command: str) -> str:
    """Best-effort audit label; enforcement remains in the guards below."""
    words = [Path(token).name for token in tokens if token and not token.startswith("-")]
    if not words:
        return "unknown"
    blocked = _blocked_install_or_network_command(tokens)
    if blocked:
        if any(word in blocked for word in ("install", "add", "update", "sync", "env")):
            return "install"
        return "network"
    if _write_targets(tokens):
        return "write"
    if _looks_command_write_like(tokens, command):
        return "write"
    meaningful = [word for word in words if word not in SHELL_NEUTRAL_COMMANDS]
    if not meaningful:
        return "neutral"
    first = meaningful[0]
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


def _strip_heredoc_bodies(command: str) -> str:
    """Remove heredoc bodies, keeping each opener line (and any redirect on it)."""
    if "<<" not in command:
        return command
    out = command
    for _ in range(8):  # bounded passes for multiple heredocs
        new = HEREDOC_RE.sub(
            lambda m: f"<<{m.group('q')}{m.group('delim')}{m.group('q')}{m.group('rest')}\n",
            out,
            count=1,
        )
        if new == out:
            break
        out = new
    return out


def _mask_quoted(command: str) -> str:
    """Blank out quoted-region contents (length preserved) for path scanning.

    Absolute-path detection must not reach inside quoted interpreter payloads
    such as ``python3 -c "... '/x' ..."``; standalone quoted path arguments are
    still caught by the token loop in ``_path_references``.
    """
    chars = list(command)
    single = double = False
    for index, char in enumerate(chars):
        if char == "'" and not double:
            single = not single
        elif char == '"' and not single:
            double = not double
        elif single or double:
            chars[index] = " "
    return "".join(chars)


def _expanded_control_tokens(tokens: list[str]) -> list[str]:
    expanded: list[str] = []
    for token in tokens:
        if re.search(r"\s", token):
            expanded.append(token)
            continue
        parts = [part for part in CONTROL_OPERATOR_RE.split(token) if part]
        expanded.extend(parts or [token])
    return expanded


def _command_segments(tokens: list[str]) -> list[list[str]]:
    segments: list[list[str]] = []
    current: list[str] = []
    for token in _expanded_control_tokens(tokens):
        if token in CONTROL_OPERATORS:
            if current:
                segments.append(current)
                current = []
            continue
        current.append(token)
    if current:
        segments.append(current)
    return segments


def _is_meta_learning_run(kind: object) -> bool:
    return str(kind or "").strip() == "meta_learning"


def _blocked_install_or_network_command(tokens: list[str], *, command: str = "") -> str:
    if command and COMMAND_SUBSTITUTION_NETWORK_RE.search(command):
        return "command substitution network/install"
    if not tokens:
        return ""
    for segment in _command_segments(tokens):
        blocked = _blocked_install_or_network_segment(segment)
        if blocked:
            return blocked
    return ""


def _blocked_install_or_network_segment(tokens: list[str]) -> str:
    tokens = _strip_shell_prefix(tokens)
    if not tokens:
        return ""
    nested = _nested_shell_command(tokens)
    if nested:
        return _blocked_install_or_network_command(_safe_split(nested))
    words = [Path(token).name for token in tokens if token and not token.startswith("-")]
    if not words:
        return ""
    first = words[0]
    if first == "find":
        for command_tokens in _find_exec_commands(tokens):
            blocked = _blocked_install_or_network_command(command_tokens)
            if blocked:
                return blocked
    if first in PYTHON_BINARIES and len(words) >= 3 and words[1] == "pip" and "install" in words[2:]:
        return "python -m pip install"
    if first in {"git"} and any(word in INSTALL_OR_NETWORK_SUBCOMMANDS for word in words[1:]):
        return "git " + next(word for word in words[1:] if word in INSTALL_OR_NETWORK_SUBCOMMANDS)
    if first in INSTALL_OR_NETWORK_COMMANDS:
        if first in {"pip", "pip3", "pipx"} and "install" in words[1:]:
            return first + " install"
        if first in {"conda", "mamba", "micromamba"} and any(
            word in {"install", "update", "env"} for word in words[1:]
        ):
            return first + " " + next(word for word in words[1:] if word in {"install", "update", "env"})
        if first in {"npm", "pnpm", "yarn", "poetry", "pipenv", "uv", "npx"} and any(
            word in INSTALL_OR_NETWORK_SUBCOMMANDS for word in words[1:]
        ):
            return first + " " + next(word for word in words[1:] if word in INSTALL_OR_NETWORK_SUBCOMMANDS)
        if first in {"hf", "huggingface-cli"} and any(word in {"download", "repo"} for word in words[1:]):
            return first + " " + next(word for word in words[1:] if word in {"download", "repo"})
        if first in {"curl", "wget"}:
            return first
    return ""


def _path_references(command: str, tokens: list[str]) -> set[str]:
    refs: set[str] = set()
    for match in ABSOLUTE_PATH_RE.finditer(_mask_quoted(command)):
        ref = match.group(0)
        if _is_dev_null(ref) or _is_shell_variable_suffix(command, match.start()):
            continue
        refs.add(ref)
    for token in tokens:
        if token.startswith("-"):
            continue
        token = token.replace("$PWD", ".").replace("${PWD}", ".")
        first_part = token.split("/", 1)[0]
        if (
            (
                token.startswith(("/", "."))
                or first_part in {"workspace", "output", "agent_output", "models", "model_artifacts"}
            )
            and not re.search(r"[()'\"]", token)
        ):
            if not _is_dev_null(token):
                refs.add(token)
    return refs


def _is_shell_variable_suffix(command: str, start: int) -> bool:
    prefix = command[max(0, start - 32) : start]
    return bool(re.search(r"\$[A-Za-z0-9_{}]+['\"]?$", prefix))


def _looks_command_write_like(tokens: list[str], command: str) -> bool:
    segments = _command_segments(tokens)
    if len(segments) > 1:
        return any(_looks_command_write_like(segment, shlex.join(segment)) for segment in segments)
    tokens = _strip_shell_prefix(tokens)
    command_tokens = [Path(token).name for token in tokens if token and not token.startswith("-")]
    if not command_tokens:
        return False
    command_name = command_tokens[0]
    if command_name in IN_PLACE_WRITE_COMMANDS and _has_in_place_write_flag(tokens):
        return True
    if command_name in READ_ONLY_COMMANDS:
        return False
    return command_name in WRITE_COMMANDS and command_name not in TARGET_ONLY_WRITE_COMMANDS


def _write_targets(tokens: list[str]) -> set[str]:
    segments = _command_segments(tokens)
    if len(segments) > 1:
        targets: set[str] = set()
        for segment in segments:
            targets.update(_write_targets(segment))
        return targets
    targets = _write_redirection_targets(tokens)
    targets.update(_write_command_targets(tokens))
    return targets


def _write_redirection_targets(tokens: list[str]) -> set[str]:
    targets: set[str] = set()
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if FD_DUP_REDIRECT_RE.match(token):
            index += 1
            continue
        if token in WRITE_REDIRECT_OPERATORS:
            if index + 1 < len(tokens):
                _add_write_target(targets, tokens[index + 1])
            index += 2
            continue
        match = COMBINED_WRITE_REDIRECT_RE.match(token)
        if match:
            _add_write_target(targets, match.group("target"))
        if "$" not in token:
            for embedded in EMBEDDED_WRITE_REDIRECT_RE.finditer(token):
                _add_write_target(targets, embedded.group("target"))
        index += 1
    return targets


def _unquoted_shell_write_targets(command: str) -> set[str]:
    targets: set[str] = set()
    single = False
    double = False
    index = 0
    length = len(command)
    while index < length:
        char = command[index]
        if char == "'" and not double:
            single = not single
            index += 1
            continue
        if char == '"' and not single:
            double = not double
            index += 1
            continue
        if single or double:
            index += 1
            continue
        op_start = index
        if char.isdigit() and index + 1 < length and command[index + 1] == ">":
            index += 1
            char = command[index]
        if char == "&" and index + 1 < length and command[index + 1] == ">":
            index += 1
            if index + 1 < length and command[index + 1] == ">":
                index += 1
        elif char == ">":
            if index + 1 < length and command[index + 1] in {">", "|"}:
                index += 1
        elif char == "<" and index + 1 < length and command[index + 1] == ">":
            index += 1
        else:
            index += 1
            continue
        index += 1
        while index < length and command[index].isspace():
            index += 1
        quote = command[index] if index < length and command[index] in {"'", '"'} else ""
        if quote:
            index += 1
        target_start = index
        while index < length:
            if quote and command[index] == quote:
                break
            if not quote and (command[index].isspace() or command[index] in ";|&<>`$(){}"):
                break
            index += 1
        if index > target_start:
            _add_write_target(targets, command[target_start:index])
        elif index == op_start + 1:
            index += 1
    return targets


def _write_command_targets(tokens: list[str]) -> set[str]:
    words = _strip_shell_prefix([token for token in tokens if token and not token.startswith("-")])
    if not words:
        return set()
    command = Path(words[0]).name
    operands = [_normalize_ref(token) for token in words[1:] if _is_write_operand_token(token)]
    if command == "cp":
        return {operands[-1]} if operands else set()
    if command == "tee":
        return set(operands)
    if command == "dd":
        targets: set[str] = set()
        for token in tokens:
            if token.startswith("of="):
                _add_write_target(targets, token[3:])
        return targets
    if command in WRITE_COMMANDS:
        return set(operands)
    return set()


def _strip_shell_prefix(tokens: list[str]) -> list[str]:
    stripped = list(tokens)
    while stripped:
        first = Path(stripped[0]).name
        if _is_env_assignment(stripped[0]):
            stripped = stripped[1:]
            continue
        if first in ENV_BINARIES:
            stripped = _strip_env_command(stripped[1:])
            continue
        break
    return stripped


def _strip_env_command(tokens: list[str]) -> list[str]:
    stripped = list(tokens)
    while stripped:
        token = stripped[0]
        if _is_env_assignment(token):
            stripped = stripped[1:]
            continue
        if token in {"-i", "--ignore-environment", "-0", "--null"}:
            stripped = stripped[1:]
            continue
        if token in {"-u", "--unset"}:
            stripped = stripped[2:] if len(stripped) >= 2 else []
            continue
        if token.startswith("-u") or token.startswith("--unset="):
            stripped = stripped[1:]
            continue
        if token.startswith("-"):
            stripped = stripped[1:]
            continue
        break
    return stripped


def _nested_shell_command(tokens: list[str]) -> str:
    if not tokens:
        return ""
    if Path(tokens[0]).name not in SHELL_BINARIES:
        return ""
    for index, token in enumerate(tokens[1:], start=1):
        if token == "-c" or (token.startswith("-") and token.endswith("c")):
            return tokens[index + 1] if index + 1 < len(tokens) else ""
    return ""


def _is_env_assignment(token: str) -> bool:
    return bool(re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", token))


def _has_in_place_write_flag(tokens: list[str]) -> bool:
    for token in tokens[1:]:
        if token == "--in-place" or token.startswith("--in-place="):
            return True
        if token == "-i" or token.startswith("-i."):
            return True
    return False


def _is_write_operand_token(token: str) -> bool:
    normalized = _normalize_ref(token)
    if not normalized or normalized.startswith("-") or normalized in {"{}", "+"}:
        return False
    return not any(char in normalized for char in "\"'(){}")


def _find_exec_commands(tokens: list[str]) -> list[list[str]]:
    commands: list[list[str]] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token not in {"-exec", "-execdir", "-ok", "-okdir"}:
            index += 1
            continue
        index += 1
        command: list[str] = []
        while index < len(tokens) and tokens[index] not in {";", "+"}:
            command.append(tokens[index])
            index += 1
        if command:
            commands.append(command)
        index += 1
    return commands


def _add_write_target(targets: set[str], raw: str) -> None:
    target = _normalize_ref(raw)
    if not target or target.startswith("&") or _is_dev_null(target):
        return
    targets.add(target)


def _is_write_target(raw: str, targets: set[str]) -> bool:
    return _normalize_ref(raw) in targets


def _normalize_ref(raw: str) -> str:
    return raw.rstrip(".,:);")


def _is_dev_null(raw: str) -> bool:
    return _normalize_ref(raw) == DEV_NULL


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _container_to_host_path(raw: str, sandbox_root: Path) -> Path:
    path = Path(raw)
    try:
        relative = path.relative_to("/mnt")
    except ValueError:
        raise ToolError(f"command references a path outside the sandbox boundary: {raw}")
    return (sandbox_root / relative).resolve()
