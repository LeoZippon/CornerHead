"""Shared script bootstrap for direct repo execution."""

from __future__ import annotations

import sys
from pathlib import Path


def add_repo_src(file: str) -> Path:
    """Prepend repo `src/` so scripts work without external PYTHONPATH."""
    root = _repo_root(Path(file).resolve())
    src = root / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    return root


def _repo_root(path: Path) -> Path:
    for parent in (path.parent, *path.parents):
        if (parent / "src" / "hl_trader").is_dir() and (parent / "pyproject.toml").is_file():
            return parent
    raise RuntimeError(f"cannot locate repo root from {path}")
