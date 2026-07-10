"""Step-tree console view: de-opaqued lineage, node metrics, and source export.

The agent-visible tree stores fold ids as opaque ``fold_ref_*`` tokens (the raw
label encodes the calendar quarter). The console is the researcher's trusted
surface, so this module recomputes the ref for every known fold id (schedule +
ledger) and maps the tokens back for display. Frozen markers come from the
ledger's fold records: the node whose artifact/model hashes match a fold's
frozen hashes is the artifact that fold shipped.
"""

from __future__ import annotations

from pathlib import Path

from autotrade.environment.identity import agent_visible_ref
from autotrade.environment.step_tree import NODE_OUTPUT_DIR, StepTree
from autotrade.pipelines.hitl_state import HITL_DIR_NAME, SCHEDULE_NAME, read_json

from .registry import latest_fold_records, read_ledger_records


def node_layout(steps_root: Path, node_id: str) -> str | None:
    """Snapshot layout of a node dir: "split" (output/+models/), "flat" (legacy
    pre-2026-07-10: artifact files at the node root), or None (no snapshot).

    The console stays readable for legacy trees — download works for both
    layouts; only rollback (parent override / step_rollback) requires "split"."""
    node_dir = steps_root / node_id
    if (node_dir / NODE_OUTPUT_DIR).is_dir():
        return "split"
    if (node_dir / "main.py").is_file():
        return "flat"
    return None


def fold_sessions(experiment_dir: Path) -> list[dict[str, str]]:
    """Ordered fold sessions ``{key, epoch_id, fold_id}`` from the HITL schedule."""
    schedule = read_json(experiment_dir / HITL_DIR_NAME / SCHEDULE_NAME)
    sessions = schedule.get("sessions") if isinstance(schedule.get("sessions"), list) else []
    out: list[dict[str, str]] = []
    for session in sessions:
        if not isinstance(session, dict) or session.get("kind") != "fold":
            continue
        key = str(session.get("key"))
        epoch_id, _, fold_id = key.partition("/")
        out.append({"key": key, "epoch_id": epoch_id, "fold_id": fold_id})
    return out


def _fold_ref_map(experiment_dir: Path, records: list[dict[str, object]]) -> dict[str, str]:
    fold_ids: dict[str, None] = {}
    for session in fold_sessions(experiment_dir):
        fold_ids.setdefault(session["fold_id"])
    for record in records:
        if record.get("record_type") == "fold" and record.get("fold_id"):
            fold_ids.setdefault(str(record["fold_id"]))
    return {agent_visible_ref(fold_id, prefix="fold_ref"): fold_id for fold_id in fold_ids}


def step_tree_view(experiment_dir: Path) -> dict[str, object]:
    tree = StepTree(experiment_dir / "steps")
    records = read_ledger_records(experiment_dir)
    refs = _fold_ref_map(experiment_dir, records)
    frozen_hashes: dict[tuple[str, str | None], list[str]] = {}
    for (epoch_id, fold_id), record in latest_fold_records(records).items():
        strategy_hash = record.get("frozen_strategy_artifact_hash")
        if not strategy_hash:
            continue
        model_hash = record.get("frozen_model_artifact_hash") or None
        frozen_hashes.setdefault((str(strategy_hash), model_hash), []).append(f"{epoch_id}/{fold_id}")

    nodes: list[dict[str, object]] = []
    for node in tree.nodes():
        node_id = str(node["node_id"])
        fold_ref = str(node.get("fold_id") or "")
        node_hash = node.get("artifact_hash")
        model_hash = node.get("model_artifact_hash")
        frozen_for = (
            frozen_hashes.get((str(node_hash), model_hash or None), []) if node.get("complete_validation") else []
        )
        layout = node_layout(tree.root, node_id)
        nodes.append(
            {
                "node_id": node_id,
                "parent_node_id": node.get("parent_node_id"),
                "epoch_id": node.get("epoch_id"),
                "fold_ref": fold_ref,
                "fold_id": refs.get(fold_ref),
                "result_name": node.get("result_name"),
                "complete_validation": bool(node.get("complete_validation")),
                "status": node.get("status"),
                "error": node.get("error"),
                "metrics": dict(node.get("metrics") or {}),
                "artifact_hash": node_hash,
                "model_artifact_hash": model_hash,
                "created_at": node.get("created_at"),
                "attachments": sorted(dict(node.get("attachments") or {})),
                "has_snapshot": layout is not None,
                "restorable": layout == "split",
                "frozen_for": sorted(frozen_for),
                "is_current": node_id == tree.current_node_id,
            }
        )
    return {
        "current_node_id": tree.current_node_id,
        "nodes": nodes,
        "fold_sessions": fold_sessions(experiment_dir),
    }


def node_export_dir(experiment_dir: Path, node_id: str) -> Path:
    """Validated node directory for the source.zip download (never a raw path join)."""
    tree = StepTree(experiment_dir / "steps")
    node = tree.get_node(node_id)  # raises ValueError for unknown ids
    if node.get("status") == "failed" or not node.get("complete_validation"):
        raise ValueError(f"step node {node_id} is a failed attempt without a snapshot")
    if node_layout(tree.root, str(node["node_id"])) is None:
        raise ValueError(f"step node snapshot is missing on disk: {node_id}")
    return tree.root / str(node["node_id"])
