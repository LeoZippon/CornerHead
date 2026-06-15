"""modification_check_tool: the deterministic gate before formal backtests.

docs/environment_design.md 4.3: it checks only factor/ and nl_prior/, takes no
business parameters, verifies the parent copy hash against the run manifest
before diffing, and writes the same result to agent_trace.jsonl and the run
manifest's latest-check summary.
"""

from __future__ import annotations

from pathlib import Path

from hl_trader.environment.artifacts import (
    ArtifactError,
    ModificationConstraints,
    artifact_hash,
    load_strategy_artifact,
    modification_delta,
)
from hl_trader.environment.runtime import utc_now_iso

from .base import ToolContext, ToolError


class ModificationCheckTool:
    name = "modification_check_tool"

    def __init__(self, ctx: ToolContext) -> None:
        self.ctx = ctx

    def run(self) -> dict[str, object]:
        manifest = self.ctx.manifest
        constraints = ModificationConstraints.from_record(dict(manifest.require("modification_constraints")))
        work_root = self.ctx.paths.agent_output
        reasons: list[str] = []

        if constraints.is_initial_artifact:
            base_root = Path(str(manifest.require("template_dir")))
        else:
            base_root = self.ctx.paths.parent_output
            expected = str(manifest.require("parent_strategy_artifact_hash"))
            actual = artifact_hash(base_root)
            if actual != expected:
                raise ToolError(
                    f"parent_output hash mismatch: manifest={expected} actual={actual}; diff base is not trusted"
                )

        delta = None
        current_hash: str | None = None
        try:
            load_strategy_artifact(work_root)
            current_hash = artifact_hash(work_root)
            delta = modification_delta(base_root, work_root)
            allowed, reasons = constraints.evaluate(delta)
        except ArtifactError as exc:
            allowed = False
            reasons = [f"artifact format invalid: {exc}"]

        summary: dict[str, object] = {
            "tool": self.name,
            "checked_at": utc_now_iso(),
            "allowed_to_backtest": allowed,
            "artifact_hash": current_hash,
            "constraints": constraints.to_record(),
            "delta": delta.to_record() if delta is not None else None,
            "reasons": reasons,
        }
        manifest.record_modification_check(summary)
        self.ctx.trace.emit("tool", {**summary}, step_id=self.ctx.current_step_id)
        return summary
