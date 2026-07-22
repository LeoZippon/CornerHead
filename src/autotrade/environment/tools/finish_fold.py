"""finish_fold_tool: the Agent's no-argument way to end the current Fold.

It verifies (1) the current formal artifact passed modification_check_tool and
was not modified afterwards, (2) the current output/models hash has at least one
successful complete-validation backtest (replay_window debug runs do not count —
mirrors the Pipeline freeze filter so a fold cannot be finished unvalidated), and
(3) the light backtest contract check. On success it records the fold end state,
locks writes, and stops further Agent calls. Failures return a fixable reason;
the deadline policy belongs to the Pipeline.
"""

from __future__ import annotations

from autotrade.environment.artifacts import (
    ArtifactError,
    artifact_hash,
    make_formal_artifacts_readonly,
    model_artifact_hash,
    restore_formal_artifacts_writable,
)
from autotrade.environment.runtime import covering_complete_validation, utc_now_iso

from .backtest import BacktestTool
from .base import ActionSpec, PHASE_TRAIN_VALID, ToolContext, ToolError
from .modification_check import ModificationCheckTool


class FinishFoldTool:
    name = "finish_fold_tool"
    spec = ActionSpec(
        action="finish_fold",
        tool_name=name,
        description=(
            "End the current Fold. Requires a successful complete-validation backtest (no "
            "replay_window) for the current output/models hash, a passing modification_check, "
            "and the light strategy contract check; locks further writes in this session."
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

        previous_write_locked = self.ctx.write_locked
        locked = False
        try:
            # Freeze formal files before validation so a lingering sandbox process
            # cannot race modification_check / contract_check. Restore on any
            # rejection; keep read-only on success.
            locked = True
            make_formal_artifacts_readonly(self.ctx.paths)
            self.ctx.write_locked = True

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

            # Same filter the Pipeline freeze applies: without a complete validation of
            # exactly these artifacts the fold can only fall back to its parent, so
            # finishing now would silently waste the session.
            if covering_complete_validation(self.ctx.manifest, current_hash, current_model_hash) is None:
                raise ToolError(
                    "finish_fold rejected: the current output/models hash has no successful complete "
                    "validation backtest (replay_window debug runs do not count). Run backtest without "
                    "replay_window on the current artifacts, or restore the best validated Step first."
                )

            contract = BacktestTool(self.ctx).contract_check()
            cleanup_agent_processes(self.ctx)
            post_hash = artifact_hash(self.ctx.paths.agent_output)
            post_model_hash = model_artifact_hash(self.ctx.paths.model_artifacts)
            if post_hash != current_hash or post_model_hash != current_model_hash:
                raise ToolError(
                    "finish_fold rejected: contract check or background process changed formal artifacts; "
                    "rerun modification_check and a validation backtest"
                )

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
        except Exception:
            self.ctx.write_locked = previous_write_locked
            if locked and not previous_write_locked:
                restore_formal_artifacts_writable(self.ctx.paths)
            raise


def cleanup_agent_processes(ctx: ToolContext) -> None:
    cleanup = getattr(ctx.executor, "cleanup_user_processes", None)
    if callable(cleanup):
        cleanup(user="agent")
