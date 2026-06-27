"""GPU selection for sandbox containers.

Default policy: allocate the requested number of L20 GPUs with the most free
video memory at container start. A one-GPU sandbox is the normal case, but the
same selector supports wider ML experiments without changing Docker plumbing.
"""

from __future__ import annotations

import subprocess


class GpuUnavailableError(RuntimeError):
    pass


def list_gpus() -> list[dict[str, object]]:
    """[{index, name, memory_free_mib, memory_total_mib}] from nvidia-smi."""
    try:
        completed = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,name,memory.free,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GpuUnavailableError(f"nvidia-smi not available: {exc}") from exc
    if completed.returncode != 0:
        raise GpuUnavailableError(f"nvidia-smi failed: {completed.stderr.strip()[:200]}")
    gpus = []
    for line in completed.stdout.strip().splitlines():
        index, name, free, total = (part.strip() for part in line.split(",", 3))
        gpus.append(
            {"index": int(index), "name": name, "memory_free_mib": int(free), "memory_total_mib": int(total)}
        )
    if not gpus:
        raise GpuUnavailableError("nvidia-smi reported no GPUs")
    return gpus


def select_gpus(count: int = 1, *, require_name: str | None = "L20") -> list[int]:
    """GPU indexes sorted by descending free memory.

    ``require_name`` keeps the default tied to the local L20 pool. Passing
    ``None`` allows any visible NVIDIA GPU.
    """
    if count <= 0:
        raise ValueError(f"count must be positive: {count}")
    gpus = list_gpus()
    if require_name:
        gpus = [gpu for gpu in gpus if require_name.lower() in str(gpu["name"]).lower()]
    if len(gpus) < count:
        names = ", ".join(str(gpu["name"]) for gpu in gpus) or "none"
        raise GpuUnavailableError(f"requested {count} GPU(s), available matching GPUs: {names}")
    selected = sorted(gpus, key=lambda gpu: int(gpu["memory_free_mib"]), reverse=True)[:count]
    return [int(gpu["index"]) for gpu in selected]
