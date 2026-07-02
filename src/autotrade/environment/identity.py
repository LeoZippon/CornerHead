"""Agent-visible opaque identifiers.

The Agent must never see a raw fold id such as ``fold_2022Q1``, because it encodes
the held-out test quarter. Every agent-readable surface (run manifest, data summary,
ledger view, and the step-tree node names) projects ids through this single
deterministic helper, so the same source id always maps to the same opaque ref and
the host can still correlate by recomputing it. Kept dependency-free so any layer
(pipelines, environment, tools) can import it without a cycle.
"""

from __future__ import annotations

import hashlib


def agent_visible_ref(value: object, *, prefix: str) -> str:
    """Deterministic opaque ref for an id that must not leak calendar meaning."""
    digest = hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:10]
    return f"{prefix}_{digest}"
