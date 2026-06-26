"""Candidate selection helpers for Agent-editable strategies."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

try:
    from mq_tools import nl
except Exception:  # pragma: no cover - only used outside formal execution
    def nl(ts_code: str, prompt: str = "", **kwargs) -> dict[str, object]:
        return {"status": "error", "content": "", "error": "mq_tools.nl is unavailable outside formal execution"}


def select_candidates(context: dict[str, object]) -> pd.DataFrame:
    """Return a candidate table.

    The template starts empty. A strategy may read point-in-time snapshot files
    from context["snapshot_dir"] and may call nl(code, prompt=...). The result
    is a dict; parse result["content"] yourself if you need a score or label.
    """

    snapshot_dir = Path(str(context.get("snapshot_dir", "/mnt/snapshot")))
    if not snapshot_dir.exists():
        raise FileNotFoundError(f"missing snapshot dir: {snapshot_dir}")
    return pd.DataFrame(columns=["ts_code", "reason", "source_artifacts"])
