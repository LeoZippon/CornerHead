"""Sandbox preparation: mount layout, snapshot-view binding, Docker rendering.

docs/environment_design.md §2.1: the isolation boundary is a non-root
user, no network, read-only snapshots, a writable artifacts tree, a fold
deadline, and basic resource guards. ``LocalSandbox`` reproduces the directory
layout and binding semantics on the host for orchestration and tests; the
Docker arguments are rendered from the same spec.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from autotrade.environment.artifacts import (
    copy_artifact,
    copy_model_artifacts,
    init_from_template,
    make_formal_artifacts_readonly,
    restore_formal_artifacts_writable,
)
from autotrade.environment.runtime import (
    chmod_tree,
    AGENT_TOP_LEVEL,
    ARTIFACT_TOP_LEVEL,
    RUNTIME_CACHE_DIR_NAMES,
    RUNTIME_CACHE_SUFFIXES,
    SandboxPaths,
    new_id,
    utc_now_iso,
)

DEFAULT_IMAGE = "autotrade-sandbox:latest"
DEFAULT_HOST_FRACTION = 0.10

RUNTIME_ENV_SCHEMA_VERSION = 2

# Curated CLI tools the Agent may rely on; their availability is PROBED (from
# the session image in docker mode, from the host in local mode), never
# statically asserted. Python packages have no static list at all — the
# session runtime itself is the single source of truth (see
# probe_image_runtime), so base-image bumps and meta-learning derived images
# can never drift from the published contract.
IMPORTANT_TOOLS = ("rg", "git", "npm", "pip", "hf", "huggingface-cli", "duckdb", "nvcc")


class SandboxLifecycleFatal(BaseException):
    """Unsafe container lifecycle state; must abort the Agent session."""


@dataclass(frozen=True)
class SandboxSpec:
    image: str = DEFAULT_IMAGE
    user: str = "agent"
    network: str = "none"
    cpus: float = 4.0
    memory: str = "8g"
    pids_limit: int = 512
    # "auto" allocates gpu_count matching GPUs with the most free memory at
    # container start; an integer or list pins devices; None runs CPU-only.
    gpu: str | int | Sequence[int] | None = "auto"
    gpu_count: int = 1
    gpu_name_filter: str | None = "L20"
    env_passthrough: tuple[str, ...] = ()
    env_aliases: tuple[tuple[str, str], ...] = ()
    add_host_gateway: bool = False
    host_gateway_ip: str | None = None

    def __post_init__(self) -> None:
        if self.gpu_count <= 0:
            raise ValueError(f"gpu_count must be positive: {self.gpu_count}")
        if self.host_gateway_ip is not None and not self.host_gateway_ip.strip():
            raise ValueError("host_gateway_ip must be non-empty when set")

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
            "gpu": self.gpu,
            "gpu_count": self.gpu_count,
            "gpu_name_filter": self.gpu_name_filter,
            "env_passthrough": list(self.env_passthrough),
            "requested_env_aliases": [
                {"container_env": container_env, "host_env": host_env}
                for container_env, host_env in self.env_aliases
            ],
            "add_host_gateway": self.add_host_gateway,
            "host_gateway_ip": self.host_gateway_ip,
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
            self.paths.parent_model_artifacts,
            self.paths.results,
            self.paths.steps,
            self.paths.logs,
            self.paths.workspace,
            self.paths.agent_output,
            self.paths.model_artifacts,
            self.paths.agent / ".runtime",
        ):
            path.mkdir(parents=True, exist_ok=True)
        # Rootless Docker maps the container agent user to a subuid; the
        # agent-writable surface must be world-writable on the host.
        # Keep the Agent mount root itself clean; only the documented child
        # directories below are writable by sandbox commands.
        self.paths.agent.chmod(0o555)
        (self.paths.agent / ".runtime").chmod(0o555)
        self.paths.workspace.chmod(0o777)
        self.paths.model_artifacts.chmod(0o777)
        self.paths.snapshot_views.chmod(0o700)
        self.paths.current_snapshot.chmod(0o755)
        # The test slot is owner-only from the start (re-applied on install).
        self.paths.test.chmod(0o700)
        self.write_runtime_env(mode="local")
        return self.paths

    def write_runtime_env(
        self,
        *,
        mode: str,
        sandbox_spec: SandboxSpec | None = None,
        image_probe: dict[str, object] | None = None,
    ) -> Path:
        """Write the read-only runtime contract visible at /mnt/artifacts/runtime_env.json.

        Docker mode requires ``image_probe`` (from :func:`probe_image_runtime`)
        so the contract reflects the actual session image."""
        if mode not in {"local", "docker"}:
            raise ValueError(f"unsupported runtime env mode: {mode}")
        record = _runtime_env_record(mode=mode, sandbox_spec=sandbox_spec, image_probe=image_probe)
        path = self.paths.runtime_env
        if path.exists():
            try:
                path.chmod(0o644)
            except OSError:
                pass
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        path.chmod(0o444)
        return path

    def install_strategy_artifact(
        self,
        source_root: Path | None,
        template_dir: Path,
        *,
        source_model_root: Path | None = None,
    ) -> bool:
        """Copy the parent artifact into parent_output/, output/, and models/.

        Returns ``is_initial_artifact``: when there is no parent, the initial
        template is copied to both parent_output/ and output/ so later checks
        can diff against a sandbox-local read-only baseline.
        Read-only files get filesystem enforcement on top of the checks.
        """
        if source_root is None:
            init_from_template(template_dir, self.paths.parent_output)
            init_from_template(template_dir, self.paths.agent_output)
            copy_model_artifacts(None, self.paths.parent_model_artifacts)
            copy_model_artifacts(None, self.paths.model_artifacts)
            chmod_tree(self.paths.parent_output, file_mode=0o444, dir_mode=0o555)
            chmod_tree(self.paths.parent_model_artifacts, file_mode=0o444, dir_mode=0o555)
            is_initial = True
        else:
            copy_artifact(source_root, self.paths.parent_output)
            copy_artifact(source_root, self.paths.agent_output)
            copy_model_artifacts(source_model_root, self.paths.parent_model_artifacts)
            copy_model_artifacts(source_model_root, self.paths.model_artifacts)
            chmod_tree(self.paths.parent_output, file_mode=0o444, dir_mode=0o555)
            chmod_tree(self.paths.parent_model_artifacts, file_mode=0o444, dir_mode=0o555)
            is_initial = False
        self.unlock_agent_output()
        return is_initial

    def bind_snapshot_view(self, view_dir: Path) -> None:
        """Refresh the current decision-input mirror and bind /mnt/snapshot to it."""
        _replace_dir_contents(view_dir, self.paths.current_snapshot)
        self._bind_snapshot_selector(self.paths.snapshot, self.paths.current_snapshot)
        self.bind_formal_snapshot_view(self.paths.current_snapshot)

    def bind_formal_snapshot_view(self, view_dir: Path) -> None:
        """Point host/formal replay at a view without changing the dev mount.

        Frozen Test/Held-out views use this path: the development container
        keeps its last Agent-visible PIT mirror while the one-shot formal
        container mounts the referenced hidden view directly.
        """
        source = Path(view_dir)
        if not source.exists() or not source.is_dir():
            raise ValueError(f"formal snapshot view must be an existing directory: {source}")
        source = source.resolve()
        allowed_roots = (self.paths.snapshot_views.resolve(), self.paths.current_snapshot.resolve())
        if not any(source == root or source.is_relative_to(root) for root in allowed_roots):
            raise ValueError(f"formal snapshot view is outside the sandbox snapshot roots: {source}")
        self._bind_snapshot_selector(self.paths.formal_snapshot, source)

    @staticmethod
    def _bind_snapshot_selector(link: Path, source: Path) -> None:
        """Replace one host-only snapshot selector symlink."""
        if link.is_symlink() or link.exists():
            if link.is_dir() and not link.is_symlink():
                raise ValueError(f"snapshot selector must be a symlink, found directory: {link}")
            link.unlink()
        os.symlink(Path(source).resolve(), link)

    def install_replay_slot(self, slot: str, source_dir: Path) -> Path:
        """Install replay/exploration data; hardlinked when possible.

        The test slot is restricted to the owning (Runner) user so a non-root
        container agent cannot read it.
        """
        if slot not in {"train", "valid", "test"}:
            raise ValueError(f"unknown snapshot slot: {slot}")
        target = getattr(self.paths, slot)
        if target.exists():
            chmod_tree(target, file_mode=0o644, dir_mode=0o755)
            shutil.rmtree(target)
        shutil.copytree(source_dir, target, copy_function=_link_or_copy)
        if slot == "test":
            target.chmod(0o700)
        return target

    def lock_agent_output(self) -> None:
        """Filesystem write lock after finish_fold / during frozen phases."""
        make_formal_artifacts_readonly(self.paths)

    def unlock_agent_output(self) -> None:
        # World-writable so the container agent (subuid in rootless Docker) can
        # edit the formal files; READMEs stay read-only.
        restore_formal_artifacts_writable(self.paths)

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
        if self.paths.host_run_manifest.exists():
            _copy_path(self.paths.host_run_manifest, dest_dir / "host_run_manifest.json")
        # Collect the frozen artifacts (output/, models/) FIRST: they are
        # controlled, chmod-locked outputs and must never be pre-empted by an
        # uncollectable file in the adversarial agent workspace. ``workspace`` is
        # agent-writable scratch, so it is collected LAST and best-effort — a
        # single unreadable/special file there (e.g. a subuid-owned core dump)
        # is skipped and logged instead of aborting the whole collection.
        for name in AGENT_TOP_LEVEL:
            if name == _AGENT_WORKSPACE:
                continue
            source = self.paths.agent / name
            if source.exists():
                _copy_path(source, dest_dir / name)
        workspace_source = self.paths.agent / _AGENT_WORKSPACE
        if workspace_source.exists():
            try:
                _copy_path(workspace_source, dest_dir / _AGENT_WORKSPACE)
            except (OSError, shutil.Error) as exc:
                _record_collect_skip(dest_dir, _AGENT_WORKSPACE, exc)
        chmod_tree(dest_dir, file_mode=0o644, dir_mode=0o755)
        return dest_dir


def _link_or_copy(src: str, dst: str) -> None:
    """Hardlink within one filesystem; fall back to a real copy."""
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


# Transient caches/tooling dirs are scratch, not experiment artifacts. They are
# also often written by the container user with restrictive perms (e.g. pip's
# 0600 cache), which the host collector cannot read; archiving them is both
# wrong and a copy failure. Excluded from artifact collection.
_COLLECT_IGNORE = shutil.ignore_patterns(
    ".cache",
    ".nv",
    ".asof",  # host-built per-tick Timeview domain views; not Agent artifacts
    ".state",  # host-managed visible ctx.state_dir; per-backtest scratch, not artifacts
    ".state_staging",  # host-managed staged ctx.state_dir writes; per-backtest scratch
    "core.[0-9]*",  # PID-suffixed core dumps (RLIMIT_CORE=0 prevents these; belt-and-suspenders)
    *RUNTIME_CACHE_DIR_NAMES,  # __pycache__ (shared with artifacts._is_runtime_cache)
    *(f"*{_suffix}" for _suffix in RUNTIME_CACHE_SUFFIXES),  # *.pyc, *.pyo
    ".git",
    ".hg",
    ".svn",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".ipynb_checkpoints",
    "node_modules",
    ".venv",
    ".conda",
    ".npm",
)


# The single agent-writable top-level tree; everything else under /mnt/agent
# (output/, models/) is a controlled, chmod-locked artifact.
_AGENT_WORKSPACE = "workspace"


def _copy_path(source: Path, dest: Path) -> None:
    if source.is_dir():
        shutil.copytree(source, dest, symlinks=True, ignore=_COLLECT_IGNORE)
    else:
        shutil.copy2(source, dest)


def _record_collect_skip(dest_dir: Path, name: str, exc: Exception) -> None:
    """Record a best-effort collection skip so a partially-collected workspace is
    visible in the run directory rather than silently dropped."""
    try:
        (dest_dir / f"{name}.collect_error.txt").write_text(
            f"{type(exc).__name__}: {exc}\n", encoding="utf-8"
        )
    except OSError:
        pass


def probe_image_runtime(image: str, *, timeout_seconds: float = 120.0) -> dict[str, object]:
    """Probe the session image itself for its Python/package/tool inventory.

    The runtime that will actually execute is the single source of truth: a
    static list drifts on base-image bumps and cannot see meta-learning
    derived images. One offline container run (~1s) per session keeps the
    published contract honest; failures raise (fail-fast, no stale claims)."""
    script = (
        "import json, importlib.metadata, platform, shutil\n"
        f"tools = {list(IMPORTANT_TOOLS)!r}\n"
        "packages = {}\n"
        "for dist in importlib.metadata.distributions():\n"
        "    name = dist.metadata['Name']\n"
        "    if name:\n"
        "        packages[name] = dist.version\n"
        "print(json.dumps({'python_version': platform.python_version(),"
        " 'packages': packages, 'tools': {t: shutil.which(t) for t in tools}}))\n"
    )
    result = subprocess.run(
        ["docker", "run", "--rm", "--network=none", "--entrypoint", "python", image, "-c", script],
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )
    if result.returncode != 0:
        raise RuntimeError(f"image runtime probe failed for {image}: {result.stderr.strip()[:500]}")
    return json.loads(result.stdout)


def _runtime_env_record(
    *,
    mode: str,
    sandbox_spec: SandboxSpec | None,
    image_probe: dict[str, object] | None,
) -> dict[str, object]:
    if mode == "docker":
        if image_probe is None:
            raise ValueError("docker runtime env requires an image probe (probe_image_runtime)")
        return _assemble_runtime_env(
            mode="docker",
            probe_mode="image_probe",
            python={"version": str(image_probe.get("python_version"))},
            network=sandbox_spec.network if sandbox_spec is not None else "none",
            sandbox_spec=sandbox_spec,
            packages=dict(image_probe.get("packages") or {}),
            tool_paths=dict(image_probe.get("tools") or {}),
            notes=[
                "Probed from the session image itself; meta-learning derived-image packages appear here automatically.",
                "python_packages maps distribution name to version; import names may differ (e.g. scikit-learn -> sklearn).",
                "Host writes this contract before Docker starts because /mnt/artifacts is mounted read-only.",
            ],
        )
    return _assemble_runtime_env(
        mode="local",
        probe_mode="host_python",
        python={
            "version": platform.python_version(),
            "executable": sys.executable,
            "implementation": platform.python_implementation(),
            "platform": platform.platform(),
        },
        network="host",
        sandbox_spec=sandbox_spec,
        packages=_local_package_records(),
        tool_paths={tool: shutil.which(tool) for tool in IMPORTANT_TOOLS},
        notes=[
            "Local mode is for development and tests only; formal experiments should use Docker.",
            "python_packages maps distribution name to version; import names may differ (e.g. scikit-learn -> sklearn).",
            "If a dependency is uncertain, use a read-only shell import probe before relying on it.",
        ],
    )


def _assemble_runtime_env(
    *,
    mode: str,
    probe_mode: str,
    python: dict[str, object],
    network: object,
    sandbox_spec: SandboxSpec | None,
    packages: dict[str, object],
    tool_paths: dict[str, object],
    notes: list[str],
) -> dict[str, object]:
    """Single runtime_env.json schema assembly shared by local and docker modes."""
    return {
        "schema_version": RUNTIME_ENV_SCHEMA_VERSION,
        "generated_at": utc_now_iso(),
        "mode": mode,
        "probe_mode": probe_mode,
        "python": python,
        "network": network,
        "sandbox_spec": sandbox_spec.to_record() if sandbox_spec is not None else None,
        "python_packages": dict(sorted(packages.items(), key=lambda kv: str(kv[0]).lower())),
        "tools": {
            str(tool): {"available": path is not None, "path": path}
            for tool, path in tool_paths.items()
        },
        "policy": _runtime_policy(),
        "notes": notes,
    }


def _local_package_records() -> dict[str, str]:
    records: dict[str, str] = {}
    for dist in importlib.metadata.distributions():
        name = dist.metadata["Name"]
        if name:
            records[str(name)] = str(dist.version)
    return records


def _runtime_policy() -> dict[str, object]:
    return {
        "install_packages_during_fold": False,
        "meta_learning_package_installs": (
            "allowed only when experiment config enables network; persistent dependencies belong in the sandbox image"
        ),
        "ordinary_fold_network": "disabled",
        "meta_learning_network": (
            "web_search/web_fetch host-side tools only unless a meta-learning sandbox spec explicitly enables "
            "Docker network access"
        ),
        "formal_strategy_read_roots": ["/mnt/snapshot", "/mnt/agent/output", "/mnt/agent/models"],
    }


# Redirect tool caches out of /mnt/agent (the collected workspace) into the
# container's ephemeral /tmp, so pip/HF/torch/CUDA caches never land as root-owned
# directories in the workspace and crash host-side collect_artifacts (forensics I2;
# the .nv denylist was a narrower patch). /tmp is not collected.
_CACHE_REDIRECT_ENV: tuple[tuple[str, str], ...] = (
    ("XDG_CACHE_HOME", "/tmp/sandbox-cache"),
    ("PIP_CACHE_DIR", "/tmp/sandbox-cache/pip"),
    ("HF_HOME", "/tmp/sandbox-cache/hf"),
    ("CUDA_CACHE_PATH", "/tmp/sandbox-cache/cuda"),
    ("NUMBA_CACHE_DIR", "/tmp/sandbox-cache/numba"),
    ("MPLCONFIGDIR", "/tmp/sandbox-cache/mpl"),
)
_CACHE_REDIRECT_ENV_ARGS: list[str] = [
    arg for key, value in _CACHE_REDIRECT_ENV for arg in ("--env", f"{key}={value}")
]


def _docker_env_args(
    names: Sequence[str],
    aliases: Sequence[tuple[str, str]] = (),
    *,
    rewrite_localhost: bool = False,
) -> tuple[list[str], dict[str, str], list[str], list[dict[str, str]]]:
    args: list[str] = []
    env: dict[str, str] = {}
    active_names: list[str] = []
    active_aliases: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw_name in names:
        name = str(raw_name).strip()
        if not name or name in seen:
            continue
        seen.add(name)
        if os.environ.get(name) is None:
            continue
        active_names.append(name)
        args.extend(["--env", name])
    for raw_container_env, raw_host_env in aliases:
        container_env = str(raw_container_env).strip()
        host_env = str(raw_host_env).strip()
        if not container_env or not host_env or container_env in seen:
            continue
        seen.add(container_env)
        value = os.environ.get(host_env)
        if value is None:
            continue
        env[container_env] = _rewrite_localhost_proxy(value) if rewrite_localhost else value
        active_aliases.append({"container_env": container_env, "host_env": host_env})
        args.extend(["--env", container_env])
    return args, env, active_names, active_aliases


def _rewrite_localhost_proxy(value: str) -> str:
    rewritten = value
    for local in ("127.0.0.1", "localhost", "[::1]"):
        rewritten = rewritten.replace(f"://{local}", "://host.docker.internal")
    if rewritten.startswith("127.0.0.1:"):
        rewritten = "host.docker.internal:" + rewritten.split(":", 1)[1]
    if rewritten.startswith("localhost:"):
        rewritten = "host.docker.internal:" + rewritten.split(":", 1)[1]
    return rewritten


def _host_gateway_args(spec: SandboxSpec) -> list[str]:
    if not spec.add_host_gateway:
        return []
    target = spec.host_gateway_ip or "host-gateway"
    return ["--add-host", f"host.docker.internal:{target}"]


def _docker_resource_args(
    spec: SandboxSpec,
    gpu_indices: Sequence[int],
    *,
    network: str | None = None,
) -> list[str]:
    args: list[str] = []
    if gpu_indices:
        devices = ",".join(str(index) for index in gpu_indices)
        args.extend(["--gpus", f'"device={devices}"'])
    args.extend(
        [
            f"--network={network or spec.network}",
            f"--cpus={spec.cpus}",
            f"--memory={spec.memory}",
            f"--pids-limit={spec.pids_limit}",
            "--ulimit",
            "core=0:0",
        ]
    )
    return args


def link_copytree(source: str | Path, dest: str | Path) -> Path:
    """Replace ``dest`` with a hardlinked copy of ``source``."""
    source, dest = Path(source), Path(dest)
    if dest.exists():
        chmod_tree(dest, file_mode=0o644, dir_mode=0o755)
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
    chmod_tree(dest, file_mode=0o644, dir_mode=0o755)
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
    chmod_tree(dest, file_mode=0o644, dir_mode=0o755)


@contextmanager
def hide_snapshot_slots_from_agent(paths: SandboxPaths):
    """Temporarily hide replay/exploration/artifact slots from strategy code.

    Docker runs candidate code as the non-root ``agent`` user. Making the slot
    roots owner-only is enough to prevent traversal while keeping the current
    `/mnt/snapshot` view and staged workspace inputs available.
    """
    slots: list[tuple[Path, int]] = []
    for path in (paths.train, paths.valid, paths.test, paths.artifacts):
        if path.exists():
            slots.append((path, stat.S_IMODE(path.stat().st_mode)))
    try:
        for path, _mode in slots:
            path.chmod(0o700)
        yield
    finally:
        for path, mode in slots:
            path.chmod(mode)


def resolve_image_identity(image: str) -> tuple[str, list[str]]:
    """(image id, repo digests) for a tag — the content-addressable identity of
    the bits that actually run. Fails fast: an uninspectable image right after
    a successful run/build is an environment inconsistency worth surfacing."""
    completed = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}|{{join .RepoDigests \",\"}}", image],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"docker image inspect failed for {image!r}: {completed.stderr.strip()}")
    image_id, _, digests = completed.stdout.strip().partition("|")
    return image_id, [digest for digest in digests.split(",") if digest]


class DockerSandbox:
    """Container lifecycle for one Fold run (docs/environment_design.md §2.1).

    The development container runs detached with the documented isolation
    flags; Agent commands use ``docker exec --user agent`` while formal replay
    runs in a separate ephemeral container.
    """

    def __init__(self, local: LocalSandbox, spec: SandboxSpec, labels: dict[str, str] | None = None) -> None:
        self.local = local
        self.spec = spec
        # Ownership labels: a SIGKILLed worker cannot run its finally-stop, so
        # the console reclaims leaked containers by label (docker rm -f).
        self.labels = dict(labels or {})
        self.container = new_id("mqsbx")
        self.gpu_indices: list[int] = []
        self.active_env_passthrough: list[str] = []
        self.active_env_aliases: list[dict[str, str]] = []
        self.image_id = ""
        self.image_repo_digests: list[str] = []
        self._retain_formal_pause = False
        self._formal_pause_active = False

    def start(self) -> str:
        paths = self.local.paths
        if self.spec.gpu is not None:
            from autotrade.environment.gpu import GpuUnavailableError

            try:
                self.gpu_indices = self._resolve_gpu_indices()
            except GpuUnavailableError as exc:
                message = str(exc)
                if self.spec.gpu == "auto" and (
                    "nvidia-smi not available" in message or "reported no GPUs" in message
                ):
                    self.gpu_indices = []  # CPU-only host: run without a GPU
                else:
                    raise
        env_args, command_env, active_names, active_aliases = _docker_env_args(
            self.spec.env_passthrough,
            self.spec.env_aliases,
            rewrite_localhost=self.spec.add_host_gateway,
        )
        self.active_env_passthrough = active_names
        self.active_env_aliases = active_aliases
        command = [
            "docker",
            "run",
            "--detach",
            # tini as PID 1 reaps orphaned/zombie processes (e.g. a driver or training
            # child left after a timeout kill), protecting --pids-limit.
            "--init",
            "--name",
            self.container,
            *(arg for key, value in sorted(self.labels.items()) for arg in ("--label", f"{key}={value}")),
            *_docker_resource_args(self.spec, self.gpu_indices),
            *_host_gateway_args(self.spec),
            *_CACHE_REDIRECT_ENV_ARGS,
            *env_args,
            "-v",
            f"{paths.train}:/mnt/snapshots/train:ro",
            "-v",
            f"{paths.valid}:/mnt/snapshots/valid:ro",
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
        run_env = {**os.environ, **command_env} if command_env else None
        completed = subprocess.run(command, capture_output=True, text=True, timeout=120, env=run_env)
        if completed.returncode != 0:
            raise RuntimeError(f"failed to start sandbox container: {completed.stderr.strip()}")
        self.image_id, self.image_repo_digests = resolve_image_identity(self.spec.image)
        return self.container

    def _resolve_gpu_indices(self) -> list[int]:
        request = self.spec.gpu
        if request is None:
            return []
        if request == "auto":
            from autotrade.environment.gpu import select_gpus

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
            # Content-addressable identity of what actually ran: a tag is
            # mutable, so the manifest also records the resolved image id and
            # any repo digests (empty for local-only builds).
            "image_id": getattr(self, "image_id", ""),
            "image_repo_digests": list(getattr(self, "image_repo_digests", ())),
            "network": self.spec.network,
            "requested_gpu": self.spec.gpu,
            "gpu_count": self.spec.gpu_count,
            "gpu_name_filter": self.spec.gpu_name_filter,
            "allocated_gpu_indices": list(self.gpu_indices),
            "requested_env_passthrough": list(self.spec.env_passthrough),
            "active_env_passthrough": list(self.active_env_passthrough),
            "requested_env_aliases": [
                {"container_env": container_env, "host_env": host_env}
                for container_env, host_env in self.spec.env_aliases
            ],
            "active_env_aliases": list(self.active_env_aliases),
            "add_host_gateway": self.spec.add_host_gateway,
        }

    def bind_snapshot_view(self, view_name: str) -> None:
        """Refresh the host current snapshot mounted at container /mnt/snapshot."""
        self.local.bind_snapshot_view(self.local.paths.snapshot_views / view_name)

    @contextmanager
    def formal_guard(self):
        """Freeze the development container for one complete formal tool call.

        A read-only bind in the formal container does not stop a background
        process in the development container from writing the same host inode.
        Pausing the development namespace before artifact validation and keeping
        it paused through result publication closes that TOCTOU without copying
        potentially large model artifacts for every Probe.
        """
        if self._formal_pause_active:
            # A final hidden evaluation deliberately keeps this namespace
            # paused until stop(). Later formal calls share that same seal.
            yield
            return
        try:
            paused = subprocess.run(
                ["docker", "pause", self.container],
                capture_output=True,
                text=True,
                timeout=30,
            )
            pause_error = (
                None
                if paused.returncode == 0
                else RuntimeError(f"failed to pause development container: {paused.stderr.strip()}")
            )
        except (OSError, subprocess.SubprocessError) as exc:
            pause_error = RuntimeError(f"failed to pause development container: {type(exc).__name__}: {exc}")
        if pause_error is not None:
            self._retain_formal_pause = False
            self._formal_pause_active = False
            resume_error = self._ensure_development_container_unpaused()
            if resume_error is not None:
                raise SandboxLifecycleFatal(
                    f"development container pause state is unsafe: {resume_error}"
                ) from pause_error
            raise pause_error
        self._formal_pause_active = True
        body_error: BaseException | None = None
        try:
            yield
        except BaseException as exc:
            body_error = exc
            raise
        finally:
            if not self._retain_formal_pause:
                resume_error = self._ensure_development_container_unpaused()
                if resume_error is not None:
                    fatal = SandboxLifecycleFatal(str(resume_error))
                    if body_error is not None:
                        raise fatal from body_error
                    raise fatal
                self._formal_pause_active = False

    def retain_pause_until_stop(self) -> None:
        """Keep the development namespace sealed after final hidden replay."""
        self._retain_formal_pause = True

    def _ensure_development_container_unpaused(self) -> RuntimeError | None:
        """Retry unpause and verify state before declaring the session unsafe."""
        details: list[str] = []
        for _ in range(2):
            try:
                resumed = subprocess.run(
                    ["docker", "unpause", self.container],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                details.append(f"{type(exc).__name__}: {exc}")
                continue
            if resumed.returncode == 0:
                return None
            details.append(resumed.stderr.strip() or f"exit {resumed.returncode}")
        try:
            inspected = subprocess.run(
                ["docker", "inspect", "--format", "{{.State.Paused}}", self.container],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if inspected.returncode == 0 and inspected.stdout.strip().lower() == "false":
                return None
            details.append(
                inspected.stderr.strip()
                or inspected.stdout.strip()
                or f"inspect exit {inspected.returncode}"
            )
        except (OSError, subprocess.SubprocessError) as exc:
            details.append(f"inspect {type(exc).__name__}: {exc}")
        return RuntimeError(
            "failed to unpause development container after retry: "
            + "; ".join(filter(None, details))
        )

    @contextmanager
    def formal_executor(self, runtime_root: Path):
        """Start one throw-away strategy container with only formal mounts."""
        from autotrade.environment.executor import FormalDockerExecutor

        paths = self.local.paths
        if not paths.formal_snapshot.exists():
            raise RuntimeError("formal snapshot is not bound")
        snapshot_source = paths.formal_snapshot.resolve()
        runtime_root = Path(runtime_root).resolve()
        state = runtime_root / "state"
        staging = runtime_root / "state_staging"
        asof = runtime_root / "asof"
        rpc_agent = runtime_root / "rpc_agent"
        for path in (state, staging, asof, rpc_agent / ".runtime"):
            path.mkdir(parents=True, exist_ok=True)
            path.chmod(0o777)
        container = new_id("mqformal")
        command = [
            "docker",
            "run",
            "--detach",
            "--init",
            "--name",
            container,
            *(arg for key, value in sorted(self.labels.items()) for arg in ("--label", f"{key}={value}")),
            "--label",
            "mq.role=formal-replay",
            *_docker_resource_args(self.spec, self.gpu_indices, network="none"),
            "--read-only",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=4g",
            *_CACHE_REDIRECT_ENV_ARGS,
            "-v",
            f"{snapshot_source}:/mnt/snapshot:ro",
            "-v",
            f"{paths.agent_output}:/mnt/agent/output:ro",
            "-v",
            f"{paths.model_artifacts}:/mnt/agent/models:ro",
            "-v",
            f"{state}:/mnt/runtime/state:ro",
            "-v",
            f"{staging}:/mnt/runtime/state_staging:rw",
            "-v",
            f"{asof}:/mnt/runtime/asof:ro",
            "-v",
            f"{rpc_agent}:/mnt/runtime/rpc_agent:rw",
            self.image_id or self.spec.image,
            "sleep",
            "infinity",
        ]
        completed = subprocess.run(command, capture_output=True, text=True, timeout=120)
        if completed.returncode != 0:
            raise RuntimeError(f"failed to start formal replay container: {completed.stderr.strip()}")
        mappings = (
            (snapshot_source, "/mnt/snapshot"),
            (paths.agent_output, "/mnt/agent/output"),
            (paths.model_artifacts, "/mnt/agent/models"),
            (state, "/mnt/runtime/state"),
            (staging, "/mnt/runtime/state_staging"),
            (asof, "/mnt/runtime/asof"),
            (rpc_agent, "/mnt/runtime/rpc_agent"),
        )
        try:
            yield FormalDockerExecutor(container, mappings)
        finally:
            subprocess.run(["docker", "rm", "-f", container], capture_output=True, text=True, timeout=60)

    def stop(self) -> None:
        strict_cleanup = self._formal_pause_active or self._retain_formal_pause
        try:
            removed = subprocess.run(
                ["docker", "rm", "-f", self.container],
                capture_output=True,
                text=True,
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            if strict_cleanup:
                raise SandboxLifecycleFatal(
                    f"failed to destroy sealed development container: {type(exc).__name__}: {exc}"
                ) from exc
            raise
        if removed.returncode != 0 and strict_cleanup:
            raise SandboxLifecycleFatal(
                f"failed to destroy sealed development container: {removed.stderr.strip()}"
            )
        if removed.returncode != 0:
            return  # best-effort cleanup of a container that may never have started
        self._formal_pause_active = False
        self._retain_formal_pause = False
