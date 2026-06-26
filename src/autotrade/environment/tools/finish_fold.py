"""finish_fold_tool: the Agent's no-argument way to end the current Fold.

docs/environment_design.md 4.5: it verifies (1) the current formal artifact
passed modification_check_tool and was not modified afterwards, and (2) the
light backtest contract check. On success it records the fold end state, locks
writes, and stops further Agent calls. Failures return a fixable reason; the
deadline policy belongs to the Pipeline.
"""

from __future__ import annotations

from autotrade.environment.artifacts import ArtifactError, artifact_hash, model_artifact_hash
from autotrade.environment.runtime import utc_now_iso

from .backtest import BacktestTool
from .base import ActionSpec, PHASE_TRAIN_VALID, ToolContext, ToolError
from .modification_check import ModificationCheckTool


class FinishFoldTool:
    name = "finish_fold_tool"
    spec = ActionSpec(
        action="finish_fold",
        tool_name=name,
        description=(
            "End the current Fold after artifact hash, modification_check, and light strategy "
            "contract checks pass; locks further writes in this session."
        ),
        read_only=False,
        destructive=False,
        concurrency_safe=False,
        allowed_modes=("fold",),
    )

    def __init__(self, ctx: ToolContext) -> None:
        self.ctx = ctx

    def run(self) -> dict[str, object]:
        self.ctx.require_phase(PHASE_TRAIN_VALID, tool=self.name)
        self.ctx.require_writable(tool=self.name)

        last = self.ctx.manifest.get("last_modification_check")
        current_hash = artifact_hash(self.ctx.paths.agent_output)
        try:
            current_model_hash = model_artifact_hash(self.ctx.paths.model_artifacts)
        except ArtifactError:
            current_model_hash = None
        if (
            not last
            or str(last.get("artifact_hash")) != current_hash
            or str(last.get("model_artifact_hash")) != str(current_model_hash)
        ):
            last = ModificationCheckTool(self.ctx).run()
        if not last.get("allowed_to_backtest"):
            raise ToolError(f"finish_fold rejected: modification check failed: {last.get('reasons')}")
        current_model_hash = str(last.get("model_artifact_hash"))

        contract = BacktestTool(self.ctx).contract_check()
        post_hash = artifact_hash(self.ctx.paths.agent_output)
        post_model_hash = model_artifact_hash(self.ctx.paths.model_artifacts)
        if post_hash != current_hash or post_model_hash != current_model_hash:
            raise ToolError(
                "finish_fold rejected: contract check changed formal artifacts; "
                "rerun modification_check and a validation backtest"
            )

        self.ctx.write_locked = True
        fold_end = {
            "status": "fold_finished",
            "finish_reason": "finish_fold_tool",
            "tool_spec": self.spec.to_record(),
            "artifact_hash": current_hash,
            "model_artifact_hash": current_model_hash,
            "contract_check": contract,
            "finished_at": utc_now_iso(),
        }
        self.ctx.manifest.update(fold_end_status=fold_end, write_locked=True)
        self.ctx.trace.emit("finish_fold", fold_end, step_id=self.ctx.current_step_id)
        return {"status": "fold_finished", "fold_status": "pending_pipeline_review", "write_locked": True}
