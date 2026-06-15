"""Sandbox preparation: mount layout, snapshot-view binding, Docker rendering.

docs/environment_design.md chapter 3: the isolation boundary is a non-root
user, no network, read-only snapshots, a writable artifacts tree, a fold
deadline, and basic resource guards. ``LocalSandbox`` reproduces the directory
layout and binding semantics on the host for orchestration and tests; the
Docker arguments are rendered from the same spec.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from hl_trader.environment.artifacts import READONLY_FILES, copy_artifact, init_from_template
from hl_trader.environment.runtime import AGENT_TOP_LEVEL, ARTIFACT_TOP_LEVEL, SandboxPaths, new_id

DEFAULT_IMAGE = "macroquant-sandbox:latest"
DEFAULT_HOST_FRACTION = 0.10


@dataclass(frozen=True)
class SandboxSpec:
    image: str = DEFAULT_IMAGE
    user: str = "agent"
    network: str = "none"
    cpus: float = 4.0
    memory: str = "8g"
    pids_limit: int = 512
    max_fold_minutes: int = 30
    # "auto" allocates gpu_count matching GPUs with the most free memory at
    # container start; an integer or list pins devices; None runs CPU-only.
    gpu: str | int | Sequence[int] | None = "auto"
    gpu_count: int = 1
    gpu_name_filter: str | None = "L20"

    def __post_init__(self) -> None:
        if self.gpu_count <= 0:
            raise ValueError(f"gpu_count must be positive: {self.gpu_count}")

    @classmethod
    def from_host_fraction(cls, fraction: float = DEFAULT_HOST_FRACTION, **overrides) -> "SandboxSpec":
        """Limit one container to a fraction (default 10%) of host CPU/RAM."""
        if not 0.0 < fraction <= 1.0:
            raise ValueError(f"fraction must be in (0, 1]: {fraction}")
        host_cpus = os.cpu_count() or 4
        with open("/proc/meminfo", encoding="ascii") as handle:
            mem_total_kb = int(next(line for line in handle if line.startswith("MemTotal")).split()[1])
        cpus = max(1.0, round(host_cpus * fraction, 1))
        memory_gib = max(1, int(mem_total_kb / 1024 / 1024 * fraction))
        return cls(cpus=cpus, memory=f"{memory_gib}g", **overrides)

    def to_record(self) -> dict[str, object]:
        return {
            "image": self.image,
            "user": self.user,
            "network": self.network,
            "cpus": self.cpus,
            "memory": self.memory,
            "pids_limit": self.pids_limit,
            "max_fold_minutes": self.max_fold_minutes,
            "gpu": self.gpu,
            "gpu_count": self.gpu_count,
            "gpu_name_filter": self.gpu_name_filter,
        }


class LocalSandbox:
    """Host-side sandbox layout with the documented /mnt-relative structure."""

    def __init__(self, root: str | Path) -> None:
        # Resolved root keeps symlink targets and Docker mounts absolute.
        self.paths = SandboxPaths(Path(root).resolve())

    def prepare_layout(self) -> SandboxPaths:
        for path in (
            self.paths.train,
            self.paths.valid,
            self.paths.test,
            self.paths.snapshot_views,
            self.paths.current_snapshot,
            self.paths.parent_output,
            self.paths.results,
            self.paths.steps,
            self.paths.logs,
            self.paths.workspace,
            self.paths.agent_output,
        ):
            path.mkdir(parents=True, exist_ok=True)
        # Rootless Docker maps the container agent user to a subuid; the
        # agent-writable surface must be world-writable on the host.
        self.paths.agent.chmod(0o755)
        self.paths.workspace.chmod(0o777)
        self.paths.snapshot_views.chmod(0o700)
        self.paths.current_snapshot.chmod(0o755)
        # The test slot is owner-only from the start (re-applied on install).
        self.paths.test.chmod(0o700)
        return self.paths

    def install_strategy_artifact(self, source_root: Path | None, template_dir: Path) -> bool:
        """Copy the parent artifact into parent_output/ and agent_output/.

        Returns ``is_initial_artifact``: when there is no parent, agent_output/
        is initialized from the template and parent_output/ stays empty.
        Read-only files get filesystem enforcement on top of the checks.
        """
        if source_root is None:
            init_from_template(template_dir, self.paths.agent_output)
            is_initial = True
        else:
            copy_artifact(source_root, self.paths.parent_output)
            copy_artifact(source_root, self.paths.agent_output)
            _chmod_tree(self.paths.parent_output, file_mode=0o444, dir_mode=0o555)
            is_initial = False
        self.unlock_agent_output()
        return is_initial

    def bind_snapshot_view(self, view_dir: Path) -> None:
        """Refresh the current decision-input mirror and bind /mnt/snapshot to it."""
        _replace_dir_contents(view_dir, self.paths.current_snapshot)
        link = self.paths.snapshot
        if link.is_symlink() or link.exists():
            if link.is_dir() and not link.is_symlink():
                raise ValueError(f"/mnt/snapshot must be a symlink, found directory: {link}")
            link.unlink()
        os.symlink(self.paths.current_snapshot.resolve(), link)

    def install_replay_slot(self, slot: str, source_dir: Path) -> Path:
        """Install replay/exploration data; hardlinked when possible.

        The test slot is restricted to the owning (Runner) user so a non-root
        container agent cannot read it.
        """
        if slot not in {"train", "valid", "test"}:
            raise ValueError(f"unknown snapshot slot: {slot}")
        target = getattr(self.paths, slot)
        if target.exists():
            _chmod_tree(target, file_mode=0o644, dir_mode=0o755)
            shutil.rmtree(target)
        shutil.copytree(source_dir, target, copy_function=_link_or_copy)
        if slot == "test":
            target.chmod(0o700)
        return target

    def lock_agent_output(self) -> None:
        """Filesystem write lock after finish_fold / during frozen phases."""
        _chmod_tree(self.paths.agent_output, file_mode=0o444, dir_mode=0o555)

    def unlock_agent_output(self) -> None:
        # World-writable so the container agent (subuid in rootless Docker) can
        # edit the formal files; READMEs stay read-only.
        _chmod_tree(self.paths.agent_output, file_mode=0o666, dir_mode=0o777)
        for relpath in READONLY_FILES:
            target = self.paths.agent_output / relpath
            if target.exists():
                target.chmod(0o444)

    def collect_artifacts(self, dest_dir: Path) -> Path:
        """Collect runtime outputs into the host experiment run directory.

        Runtime separates trusted `/mnt/artifacts` from agent-writable
        `/mnt/agent`; the collected experiment directory keeps the historical
        flat layout for reports and ledgers.
        """
        dest_dir.parent.mkdir(parents=True, exist_ok=True)
        if dest_dir.exists():
            raise FileExistsError(f"artifact collection target already exists: {dest_dir}")
        dest_dir.mkdir()
        for name in ARTIFACT_TOP_LEVEL:
            source = self.paths.artifacts / name
            if source.exists():
                _copy_path(source, dest_dir / name)
        for name in AGENT_TOP_LEVEL:
            source = self.paths.agent / name
            if source.exists():
                _copy_path(source, dest_dir / name)
        _chmod_tree(dest_dir, file_mode=0o644, dir_mode=0o755)
        return dest_dir


def _link_or_copy(src: str, dst: str) -> None:
    """Hardlink within one filesystem; fall back to a real copy."""
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def _copy_path(source: Path, dest: Path) -> None:
    if source.is_dir():
        shutil.copytree(source, dest, symlinks=True)
    else:
        shutil.copy2(source, dest)


def link_copytree(source: str | Path, dest: str | Path) -> Path:
    """Replace ``dest`` with a hardlinked copy of ``source``."""
    source, dest = Path(source), Path(dest)
    if dest.exists():
        _chmod_tree(dest, file_mode=0o644, dir_mode=0o755)
        shutil.rmtree(dest)
    shutil.copytree(source, dest, copy_function=_link_or_copy)
    return dest


def _replace_dir_contents(source: Path, dest: Path) -> None:
    """Replace directory contents without replacing the directory inode."""
    source = Path(source)
    dest = Path(dest)
    if not source.is_dir():
        raise FileNotFoundError(f"snapshot view not found: {source}")
    dest.mkdir(parents=True, exist_ok=True)
    _chmod_tree(dest, file_mode=0o644, dir_mode=0o755)
    for child in list(dest.iterdir()):
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()
    for child in source.iterdir():
        target = dest / child.name
        if child.is_dir() and not child.is_symlink():
            shutil.copytree(child, target, copy_function=_link_or_copy)
        else:
            _link_or_copy(str(child), str(target))
    _chmod_tree(dest, file_mode=0o644, dir_mode=0o755)


def _chmod_tree(root: Path, *, file_mode: int, dir_mode: int) -> None:
    if not root.exists():
        return
    for path in sorted(root.rglob("*"), reverse=True):
        try:
            path.chmod(dir_mode if path.is_dir() else file_mode)
        except OSError:
            pass
    root.chmod(dir_mode if root.is_dir() else file_mode)


class DockerSandbox:
    """Container lifecycle for one Fold run (docs/environment_design.md 3.1).

    The container runs detached with the documented isolation flags; Agent
    commands execute inside via ``docker exec --user agent`` and the frozen
    test phase reuses the same container after writes are locked.
    """

    def __init__(self, local: LocalSandbox, spec: SandboxSpec) -> None:
        self.local = local
        self.spec = spec
        self.container = new_id("mqsbx")
        self.gpu_indices: list[int] = []

    def start(self) -> str:
        paths = self.local.paths
        gpu_args: list[str] = []
        if self.spec.gpu is not None:
            from hl_trader.environment.gpu import GpuUnavailableError

            try:
                self.gpu_indices = self._resolve_gpu_indices()
                if self.gpu_indices:
                    gpu_args = [f"--gpus=device={','.join(str(index) for index in self.gpu_indices)}"]
            except GpuUnavailableError as exc:
                message = str(exc)
                if self.spec.gpu == "auto" and (
                    "nvidia-smi not available" in message or "reported no GPUs" in message
                ):
                    self.gpu_indices = []  # CPU-only host: run without a GPU
                else:
                    raise
        command = [
            "docker",
            "run",
            "--detach",
            "--name",
            self.container,
            *gpu_args,
            f"--network={self.spec.network}",
            f"--cpus={self.spec.cpus}",
            f"--memory={self.spec.memory}",
            f"--pids-limit={self.spec.pids_limit}",
            "-v",
            f"{paths.train}:/mnt/snapshots/train:ro",
            "-v",
            f"{paths.valid}:/mnt/snapshots/valid:ro",
            "-v",
            f"{paths.test}:/mnt/snapshots/test:ro",
            "-v",
            f"{paths.current_snapshot}:/mnt/snapshot:ro",
            "-v",
            f"{paths.artifacts}:/mnt/artifacts:ro",
            "-v",
            f"{paths.agent}:/mnt/agent:rw",
            self.spec.image,
            "sleep",
            "infinity",
        ]
        completed = subprocess.run(command, capture_output=True, text=True, timeout=120)
        if completed.returncode != 0:
            raise RuntimeError(f"failed to start sandbox container: {completed.stderr.strip()}")
        return self.container

    def _resolve_gpu_indices(self) -> list[int]:
        request = self.spec.gpu
        if request is None:
            return []
        if request == "auto":
            from hl_trader.environment.gpu import select_gpus

            return select_gpus(self.spec.gpu_count, require_name=self.spec.gpu_name_filter)
        if isinstance(request, int):
            return [request]
        if isinstance(request, str):
            return [int(part.strip()) for part in request.split(",") if part.strip()]
        return [int(index) for index in request]

    def allocation_record(self) -> dict[str, object]:
        return {
            "container": self.container,
            "image": self.spec.image,
            "requested_gpu": self.spec.gpu,
            "gpu_count": self.spec.gpu_count,
            "gpu_name_filter": self.spec.gpu_name_filter,
            "allocated_gpu_indices": list(self.gpu_indices),
        }

    def bind_snapshot_view(self, view_name: str) -> None:
        """Refresh the host current snapshot mounted at container /mnt/snapshot."""
        self.local.bind_snapshot_view(self.local.paths.snapshot_views / view_name)

    def stop(self) -> None:
        subprocess.run(["docker", "rm", "-f", self.container], capture_output=True, text=True, timeout=60)
