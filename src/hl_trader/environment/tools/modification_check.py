"""modification_check_tool: the deterministic gate before formal backtests.

It checks the ``output/`` strategy artifact directory plus optional ``models/``
parameters, takes no business parameters, verifies parent
copy hashes against the run manifest before diffing, and writes the same result
to agent_trace.jsonl and the run manifest's latest-check summary.
"""

from __future__ import annotations

from hl_trader.environment.artifacts import (
    ArtifactError,
    ModificationConstraints,
    artifact_hash,
    combined_artifact_hash,
    load_strategy_artifact,
    modification_delta,
    model_artifact_delta,
    model_artifact_hash,
)
from hl_trader.environment.runtime import utc_now_iso

from .base import ActionSpec, ToolContext, ToolError


class ModificationCheckTool:
    name = "modification_check_tool"
    spec = ActionSpec(
        action="modification_check",
        tool_name=name,
        description=(
            "Validate current output/ and models/ artifacts against modification constraints, "
            "parent hashes, size/line limits, and format rules before backtest or finish_fold."
        ),
        read_only=False,
        destructive=False,
        concurrency_safe=False,
        allowed_modes=("fold", "meta_learning"),
    )

    def __init__(self, ctx: ToolContext) -> None:
        self.ctx = ctx

    def run(self) -> dict[str, object]:
        manifest = self.ctx.manifest
        constraints = ModificationConstraints.from_record(dict(manifest.require("modification_constraints")))
        work_root = self.ctx.paths.agent_output
        model_root = self.ctx.paths.model_artifacts
        reasons: list[str] = []

        if constraints.is_initial_artifact:
            base_root = self.ctx.paths.parent_output
            base_model_root = self.ctx.paths.parent_model_artifacts
            expected = manifest.get("initial_template_hash")
            if expected is not None:
                actual = artifact_hash(base_root)
                if actual != str(expected):
                    raise ToolError(
                        f"initial template hash mismatch: manifest={expected} actual={actual}; diff base is not trusted"
                    )
        else:
            base_root = self.ctx.paths.parent_output
            base_model_root = self.ctx.paths.parent_model_artifacts
            expected = str(manifest.require("parent_strategy_artifact_hash"))
            actual = artifact_hash(base_root)
            if actual != expected:
                raise ToolError(
                    f"parent_output hash mismatch: manifest={expected} actual={actual}; diff base is not trusted"
                )
            expected_model = str(manifest.get("parent_model_artifact_hash", model_artifact_hash(base_model_root)))
            actual_model = model_artifact_hash(base_model_root)
            if actual_model != expected_model:
                raise ToolError(
                    "parent_models hash mismatch: "
                    f"manifest={expected_model} actual={actual_model}; model base is not trusted"
                )

        delta = None
        model_delta = None
        current_hash: str | None = None
        current_model_hash: str | None = None
        try:
            load_strategy_artifact(work_root)
            current_hash = artifact_hash(work_root)
            current_model_hash = model_artifact_hash(model_root)
            delta = modification_delta(base_root, work_root)
            model_delta = model_artifact_delta(base_model_root, model_root)
            allowed, reasons = constraints.evaluate(delta, model_delta)
        except ArtifactError as exc:
            allowed = False
            reasons = [f"artifact format invalid: {exc}"]

        summary: dict[str, object] = {
            "tool": self.name,
            "tool_spec": self.spec.to_record(),
            "checked_at": utc_now_iso(),
            "allowed_to_backtest": allowed,
            "artifact_hash": current_hash,
            "model_artifact_hash": current_model_hash,
            "combined_artifact_hash": (
                combined_artifact_hash(current_hash, current_model_hash)
                if current_hash is not None and current_model_hash is not None
                else None
            ),
            "constraints": constraints.to_record(),
            "delta": delta.to_record() if delta is not None else None,
            "model_delta": model_delta.to_record() if model_delta is not None else None,
            "reasons": reasons,
        }
        manifest.record_modification_check(summary)
        self.ctx.trace.emit("tool", {**summary}, step_id=self.ctx.current_step_id)
        return summary
