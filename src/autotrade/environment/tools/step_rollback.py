"""step_rollback_tool: restore a validated Step node and branch from it.

Restores ``output/`` (and by default ``models/``) from a step-tree node
snapshot and repositions ``current_node_id`` there, so the next validated
backtest is recorded as a child of the restored node — the lineage in
``steps/tree.json`` stays truthful when the Agent abandons a direction.
Unvalidated working-copy edits are overwritten by design: every validated
state already lives in the tree. Modification constraints keep being measured
against this Fold's parent artifact, so restoring a distant branch may exceed
the diff budget and the next backtest will reject it (reject-don't-clamp).
"""

from __future__ import annotations

from autotrade.environment.artifacts import (
    artifact_hash,
    copy_artifact,
    copy_model_artifacts,
    model_artifact_hash,
)
from autotrade.environment.runtime import utc_now_iso
from autotrade.environment.step_tree import StepTree

from .base import ActionField, ActionSpec, ToolContext, ToolError


class StepRollbackTool:
    name = "step_rollback_tool"
    spec = ActionSpec(
        action="step_rollback",
        tool_name=name,
        description=(
            "Restore output/ (and models/ unless include_models=false) from a validated step-tree node "
            "(steps/<node_id>/) and move the tree position there, so later validated backtests branch from "
            "that node. Overwrites unvalidated working-copy edits; failed nodes carry no snapshot. "
            "Modification constraints are still measured against this fold's parent artifact."
        ),
        fields=(
            ActionField(
                "node_id",
                "string",
                required=True,
                description="Step tree node to restore; see /mnt/artifacts/steps/tree.txt for ids.",
            ),
            ActionField(
                "include_models",
                "boolean",
                default=True,
                description=(
                    "Also restore models/ from the node snapshot (replacing the current models/). "
                    "false keeps the current models/, which may pair the restored code with parameters "
                    "it was never validated with."
                ),
            ),
        ),
        read_only=False,
        destructive=True,
        concurrency_safe=False,
        allowed_modes=("fold",),
    )

    def __init__(self, ctx: ToolContext) -> None:
        self.ctx = ctx

    def run(self, node_id: str, include_models: bool = True) -> dict[str, object]:
        self.ctx.require_writable(tool=self.name)
        if not self.ctx.manifest.get("step_tree_enabled"):
            raise ToolError("step tree is disabled for this experiment; step_rollback is unavailable")
        tree = StepTree(self.ctx.paths.steps)
        try:
            node = tree.get_node(node_id)
        except ValueError as exc:
            raise ToolError(
                str(exc), retry_hint="read /mnt/artifacts/steps/tree.txt for valid node ids"
            ) from exc
        if node.get("status") == "failed" or not node.get("complete_validation"):
            raise ToolError(f"node {node_id} is a failed attempt without a snapshot; it cannot be restored")
        snapshot_output = tree.node_output_dir(node_id)
        if not snapshot_output.is_dir():
            raise ToolError(f"step node snapshot is missing on disk: {node_id}")

        copy_artifact(snapshot_output, self.ctx.paths.agent_output)
        restored_hash = artifact_hash(self.ctx.paths.agent_output)
        if restored_hash != node.get("artifact_hash"):
            raise ToolError(
                f"restored output hash mismatch for {node_id}: snapshot={node.get('artifact_hash')} "
                f"restored={restored_hash}; the snapshot is corrupt and output/ now holds its content"
            )
        restored_model_hash: str | None = None
        if include_models:
            models_dir = tree.node_models_dir(node_id)
            copy_model_artifacts(models_dir if models_dir.is_dir() else None, self.ctx.paths.model_artifacts)
            restored_model_hash = model_artifact_hash(self.ctx.paths.model_artifacts)
            expected_model = node.get("model_artifact_hash")
            if expected_model is not None and restored_model_hash != expected_model:
                raise ToolError(
                    f"restored models hash mismatch for {node_id}: snapshot={expected_model} "
                    f"restored={restored_model_hash}; the snapshot is corrupt"
                )
        tree.set_position(node_id)

        summary: dict[str, object] = {
            "tool": self.name,
            "restored_node_id": node_id,
            "current_node_id": node_id,
            "artifact_hash": restored_hash,
            "model_artifact_hash": restored_model_hash,
            "models_restored": include_models,
            "node_metrics": dict(node.get("metrics") or {}),
            "node_created_at": node.get("created_at"),
            "restored_at": utc_now_iso(),
            "note": (
                "modification constraints are still measured against this fold's parent artifact; "
                "run modification_check before the next backtest"
            ),
        }
        self.ctx.trace.emit("tool", {**summary}, step_id=self.ctx.current_step_id)
        return summary
