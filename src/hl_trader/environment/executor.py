"""Command executors: run sandbox-side commands locally or inside Docker.

The Environment's trusted services (NL scoring, Broker, manifests) always run
on the host; only Agent-facing execution — shell commands and the strategy
entrypoint — goes through an executor. ``DockerExecutor`` maps host sandbox
paths to the fixed ``/mnt`` container layout and runs commands as the non-root
``agent`` user via ``docker exec``.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from hl_trader.environment.runtime import SandboxPaths


@dataclass(frozen=True)
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str


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
    ) -> ExecResult:
        base_env = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": str(self.paths.workspace),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        if "PYTHONPATH" in os.environ:
            base_env["PYTHONPATH"] = os.environ["PYTHONPATH"]
        base_env.update(env or {})
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
    ) -> ExecResult:
        command = ["docker", "exec", "--user", user]
        merged_env = {"PYTHONDONTWRITEBYTECODE": "1", **(env or {})}
        for key, value in merged_env.items():
            command.extend(["--env", f"{key}={value}"])
        command.extend(["--workdir", self.map_path(cwd) if cwd else "/mnt/agent"])
        command.append(self.container)
        command.extend(argv)
        try:
            completed = subprocess.run(
                command, capture_output=True, text=True, errors="replace", timeout=timeout_seconds
            )
        except subprocess.TimeoutExpired as exc:
            return ExecResult(exit_code=124, stdout=str(exc.stdout or ""), stderr=f"timeout after {timeout_seconds}s")
        return ExecResult(exit_code=completed.returncode, stdout=completed.stdout, stderr=completed.stderr)


def docker_available() -> bool:
    try:
        return subprocess.run(["docker", "ps"], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False
