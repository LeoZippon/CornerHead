"""finish_fold_tool: the Agent's no-argument way to end the current Fold.

docs/environment_design.md 4.5: it verifies (1) the current formal artifact
passed modification_check_tool and was not modified afterwards, and (2) the
light backtest contract check. On success it records the fold end state, locks
writes, and stops further Agent calls. Failures return a fixable reason; the
deadline policy belongs to the Pipeline.
"""

from __future__ import annotations

from hl_trader.environment.artifacts import artifact_hash
from hl_trader.environment.runtime import utc_now_iso

from .backtest import BacktestTool
from .base import PHASE_TRAIN_VALID, ToolContext, ToolError
from .modification_check import ModificationCheckTool


class FinishFoldTool:
    name = "finish_fold_tool"

    def __init__(self, ctx: ToolContext) -> None:
        self.ctx = ctx

    def run(self) -> dict[str, object]:
        self.ctx.require_phase(PHASE_TRAIN_VALID, tool=self.name)
        self.ctx.require_writable(tool=self.name)

        last = self.ctx.manifest.get("last_modification_check")
        current_hash = artifact_hash(self.ctx.paths.agent_output)
        if not last or str(last.get("artifact_hash")) != current_hash:
            last = ModificationCheckTool(self.ctx).run()
        if not last.get("allowed_to_backtest"):
            raise ToolError(f"finish_fold rejected: modification check failed: {last.get('reasons')}")

        contract = BacktestTool(self.ctx).contract_check()

        self.ctx.write_locked = True
        fold_end = {
            "status": "fold_finished",
            "finish_reason": "finish_fold_tool",
            "artifact_hash": current_hash,
            "contract_check": contract,
            "finished_at": utc_now_iso(),
        }
        self.ctx.manifest.update(fold_end_status=fold_end, write_locked=True)
        self.ctx.trace.emit("finish_fold", fold_end, step_id=self.ctx.current_step_id)
        return {"status": "fold_finished", "fold_status": "pending_pipeline_review", "write_locked": True}
