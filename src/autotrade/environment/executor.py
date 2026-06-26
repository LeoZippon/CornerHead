"""Command executors: run sandbox-side commands locally or inside Docker.

The Environment's trusted services (NL Sub Agent, Broker, manifests) always run
on the host; only Agent-facing execution — shell commands and the strategy
entrypoint — goes through an executor. ``DockerExecutor`` maps host sandbox
paths to the fixed ``/mnt`` container layout and runs commands as the non-root
``agent`` user via ``docker exec``.
"""

from __future__ import annotations

import os
import selectors
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from autotrade.environment.runtime import SandboxPaths


@dataclass(frozen=True)
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str
    stdout_truncated: bool = False
    stderr_truncated: bool = False


class ExecutorError(RuntimeError):
    pass


class LocalExecutor:
    """Host-process execution; used for tests and non-Docker development."""

    name = "local"

    def __init__(self, paths: SandboxPaths, *, python: str | None = None) -> None:
        self.paths = paths
        self.python = python or sys.executable

    def map_path(self, host_path: Path | str) -> str:
        return str(host_path)

    def run(
        self,
        argv: list[str],
        *,
        env: dict[str, str] | None = None,
        cwd: Path | None = None,
        timeout_seconds: float = 120.0,
        user: str = "agent",
        max_output_chars: int | None = None,
    ) -> ExecResult:
        base_env = {
            "PATH": f"{self.paths.workspace}/.local/bin:{self.paths.workspace}/.npm-global/bin:/usr/local/bin:/usr/bin:/bin",
            "HOME": str(self.paths.workspace),
            "PYTHONUSERBASE": str(self.paths.workspace / ".local"),
            "PIP_USER": "1",
            "npm_config_prefix": str(self.paths.workspace / ".npm-global"),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        if "PYTHONPATH" in os.environ:
            base_env["PYTHONPATH"] = os.environ["PYTHONPATH"]
        base_env.update(env or {})
        if max_output_chars is not None:
            return _run_limited_capture(
                argv,
                cwd=cwd or self.paths.agent,
                env=base_env,
                timeout_seconds=timeout_seconds,
                max_output_chars=max_output_chars,
            )
        try:
            completed = subprocess.run(
                argv,
                cwd=str(cwd or self.paths.agent),
                env=base_env,
                capture_output=True,
                text=True,
                errors="replace",  # agent commands may emit binary output
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            return ExecResult(exit_code=124, stdout=str(exc.stdout or ""), stderr=f"timeout after {timeout_seconds}s")
        return ExecResult(exit_code=completed.returncode, stdout=completed.stdout, stderr=completed.stderr)

    def popen(
        self,
        argv: list[str],
        *,
        env: dict[str, str] | None = None,
        cwd: Path | None = None,
        user: str = "agent",
    ) -> subprocess.Popen[str]:
        base_env = {
            "PATH": f"{self.paths.workspace}/.local/bin:{self.paths.workspace}/.npm-global/bin:/usr/local/bin:/usr/bin:/bin",
            "HOME": str(self.paths.workspace),
            "PYTHONUSERBASE": str(self.paths.workspace / ".local"),
            "PIP_USER": "1",
            "npm_config_prefix": str(self.paths.workspace / ".npm-global"),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        if "PYTHONPATH" in os.environ:
            base_env["PYTHONPATH"] = os.environ["PYTHONPATH"]
        base_env.update(env or {})
        return subprocess.Popen(
            argv,
            cwd=str(cwd or self.paths.agent),
            env=base_env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
        )


class DockerExecutor:
    """Execution inside a running sandbox container via ``docker exec``."""

    name = "docker"

    def __init__(self, container: str, host_paths: SandboxPaths, *, python: str = "python3") -> None:
        self.container = container
        self.host_paths = host_paths
        self.python = python

    def map_path(self, host_path: Path | str) -> str:
        """Translate a host sandbox path to the container /mnt layout."""
        host_path = Path(host_path)
        for base in (self.host_paths.snapshot, self.host_paths.current_snapshot):
            try:
                relative_to_snapshot = host_path.resolve().relative_to(base.resolve())
                # The formal entrypoint always sees the documented /mnt/snapshot link.
                return str(Path("/mnt/snapshot") / relative_to_snapshot)
            except ValueError:
                pass
        root = self.host_paths.root
        try:
            relative = host_path.resolve().relative_to(root.resolve())
        except ValueError as exc:
            raise ExecutorError(f"path is outside the sandbox root and cannot be mapped: {host_path}") from exc
        return str(Path("/mnt") / relative)

    def run(
        self,
        argv: list[str],
        *,
        env: dict[str, str] | None = None,
        cwd: Path | None = None,
        timeout_seconds: float = 120.0,
        user: str = "agent",
        max_output_chars: int | None = None,
    ) -> ExecResult:
        command = ["docker", "exec", "-i", "--user", user]
        merged_env = {
            "PATH": "/mnt/agent/workspace/.local/bin:/mnt/agent/workspace/.npm-global/bin:/usr/local/bin:/usr/bin:/bin",
            "HOME": "/mnt/agent/workspace",
            "PYTHONUSERBASE": "/mnt/agent/workspace/.local",
            "PIP_USER": "1",
            "npm_config_prefix": "/mnt/agent/workspace/.npm-global",
            "PYTHONDONTWRITEBYTECODE": "1",
            **(env or {}),
        }
        for key, value in merged_env.items():
            command.extend(["--env", f"{key}={value}"])
        command.extend(["--workdir", self.map_path(cwd) if cwd else "/mnt/agent"])
        command.append(self.container)
        command.extend(argv)
        if max_output_chars is not None:
            return _run_limited_capture(
                command,
                cwd=None,
                env=None,
                timeout_seconds=timeout_seconds,
                max_output_chars=max_output_chars,
            )
        try:
            completed = subprocess.run(
                command, capture_output=True, text=True, errors="replace", timeout=timeout_seconds
            )
        except subprocess.TimeoutExpired as exc:
            return ExecResult(exit_code=124, stdout=str(exc.stdout or ""), stderr=f"timeout after {timeout_seconds}s")
        return ExecResult(exit_code=completed.returncode, stdout=completed.stdout, stderr=completed.stderr)

    def popen(
        self,
        argv: list[str],
        *,
        env: dict[str, str] | None = None,
        cwd: Path | None = None,
        user: str = "agent",
    ) -> subprocess.Popen[str]:
        # ``-i`` keeps stdin open so the persistent strategy-policy runner can
        # serve per-bar JSONL requests; without it the container sees EOF and the
        # driver's stdin loop exits immediately.
        command = ["docker", "exec", "-i", "--user", user]
        merged_env = {
            "PATH": "/mnt/agent/workspace/.local/bin:/mnt/agent/workspace/.npm-global/bin:/usr/local/bin:/usr/bin:/bin",
            "HOME": "/mnt/agent/workspace",
            "PYTHONUSERBASE": "/mnt/agent/workspace/.local",
            "PIP_USER": "1",
            "npm_config_prefix": "/mnt/agent/workspace/.npm-global",
            "PYTHONDONTWRITEBYTECODE": "1",
            **(env or {}),
        }
        for key, value in merged_env.items():
            command.extend(["--env", f"{key}={value}"])
        command.extend(["--workdir", self.map_path(cwd) if cwd else "/mnt/agent"])
        command.append(self.container)
        command.extend(argv)
        return subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
        )


def docker_available() -> bool:
    try:
        return subprocess.run(["docker", "ps"], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _run_limited_capture(
    argv: list[str],
    *,
    cwd: Path | None,
    env: dict[str, str] | None,
    timeout_seconds: float,
    max_output_chars: int,
) -> ExecResult:
    """Run a command while bounding stdout/stderr retained in memory."""
    max_bytes = max(max_output_chars, 0)
    proc = subprocess.Popen(
        argv,
        cwd=str(cwd) if cwd is not None else None,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    selector = selectors.DefaultSelector()
    assert proc.stdout is not None
    assert proc.stderr is not None
    selector.register(proc.stdout, selectors.EVENT_READ, "stdout")
    selector.register(proc.stderr, selectors.EVENT_READ, "stderr")
    stdout = bytearray()
    stderr = bytearray()
    stdout_truncated = False
    stderr_truncated = False
    timed_out = False
    deadline = time.monotonic() + timeout_seconds

    while selector.get_map():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            proc.kill()
            break
        events = selector.select(timeout=min(0.1, remaining))
        if not events and proc.poll() is not None:
            # Process exited; loop once more to drain EOF-ready pipes.
            events = selector.select(timeout=0)
        for key, _ in events:
            chunk = os.read(key.fileobj.fileno(), 8192)
            if not chunk:
                selector.unregister(key.fileobj)
                continue
            if key.data == "stdout":
                remaining_bytes = max(0, max_bytes - len(stdout))
                if remaining_bytes:
                    stdout.extend(chunk[:remaining_bytes])
                if len(chunk) > remaining_bytes:
                    stdout_truncated = True
            else:
                remaining_bytes = max(0, max_bytes - len(stderr))
                if remaining_bytes:
                    stderr.extend(chunk[:remaining_bytes])
                if len(chunk) > remaining_bytes:
                    stderr_truncated = True

    for key in list(selector.get_map().values()):
        try:
            selector.unregister(key.fileobj)
        except Exception:
            pass
    selector.close()
    if timed_out:
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        _close_process_pipes(proc)
        timeout_msg = f"\ntimeout after {timeout_seconds}s".encode()
        if len(stderr) + len(timeout_msg) <= max_bytes:
            stderr.extend(timeout_msg)
        else:
            stderr_truncated = True
        return ExecResult(
            exit_code=124,
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
        )
    return_code = proc.wait()
    _close_process_pipes(proc)
    return ExecResult(
        exit_code=return_code,
        stdout=stdout.decode("utf-8", errors="replace"),
        stderr=stderr.decode("utf-8", errors="replace"),
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
    )


def _close_process_pipes(proc: subprocess.Popen[bytes]) -> None:
    for pipe in (proc.stdout, proc.stderr):
        if pipe is not None and not pipe.closed:
            pipe.close()
