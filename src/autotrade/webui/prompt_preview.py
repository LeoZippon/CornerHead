"""Session system-prompt preview for pre-approval review.

Rebuilds the exact prompt the worker will assemble (role, environment, taste,
researcher directive, actions, contract, prohibitions). The runtime-generated
当前实验事实 JSON block (built from the live run manifest/runtime_env/
data_summary) cannot exist before the sandbox is prepared, so the preview
renders the documented fallback (fold info + acceptance rules verbatim).
"""

from __future__ import annotations

from pathlib import Path

from autotrade.agent.prompts import build_meta_learning_prompt, build_system_prompt
from autotrade.pipelines.hitl_state import HITL_DIR_NAME, PARAM_DEFAULTS, PARAMS_NAME, SCHEDULE_NAME, read_json
from autotrade.pipelines.meta_schedule import meta_record_id

from .registry import read_ledger_records

PREVIEW_NOTE = (
    "预览包含 Agent 将收到的全部静态段（角色/环境/Taste/研究者指令/动作/提交合同/禁止行为）。"
    "运行时「当前实验事实」JSON 由沙箱准备完成后的 run manifest 等生成，此处以 Fold 信息与验收规则原文代替；"
    "该块只是事实索引，不含额外指令。"
)


def build_prompt_preview(experiment_dir: Path, session_key: str, directive: str) -> dict[str, object]:
    """Raises KeyError for an unknown session and ValueError for held-out keys."""
    hitl_dir = experiment_dir / HITL_DIR_NAME
    schedule = read_json(hitl_dir / SCHEDULE_NAME)
    sessions = schedule.get("sessions") if isinstance(schedule.get("sessions"), list) else []
    entry = next((s for s in sessions if s.get("key") == session_key), None)
    if entry is None:
        raise KeyError(f"unknown session: {session_key}")
    kind = str(entry.get("kind"))
    if kind == "heldout":
        raise ValueError("held-out runs have no agent session or system prompt")
    params = read_json(hitl_dir / PARAMS_NAME)

    def param(key: str):
        return params.get(key, PARAM_DEFAULTS.get(key))

    if kind == "meta_learning":
        prompt = build_meta_learning_prompt(
            experiment_directive=directive.strip() or str(param("meta_learning_directive") or ""),
        )
    else:
        epoch_id = str(entry.get("epoch_id") or "epoch_001")
        try:
            epoch_index = int(epoch_id.rsplit("_", 1)[-1])
        except ValueError:
            epoch_index = 1
        taste = ""
        meta_records = {
            meta_record_id(record): record
            for record in read_ledger_records(experiment_dir)
            if record.get("record_type") == "meta_learning"
        }
        session_position = sessions.index(entry)
        for planned in reversed(sessions[:session_position]):
            if planned.get("kind") != "meta_learning":
                continue
            record = meta_records.get(
                str(planned.get("meta_learning_id") or planned.get("epoch_id") or "")
            )
            taste_path = record.get("taste_path") if record is not None else None
            if not taste_path or not Path(str(taste_path)).exists():
                raise ValueError(
                    "prompt preview unavailable until the nearest preceding Meta session completes"
                )
            taste = Path(str(taste_path)).read_text(encoding="utf-8").strip()
            if not taste:
                raise ValueError(
                    "prompt preview unavailable: the nearest preceding Meta session has no Taste"
                )
            break
        # The runtime facts block redacts the test schedule from the agent;
        # keep the preview's fallback consistent (no test_period).
        fold_info = {
            key: entry.get(key)
            for key in ("fold_id", "input_window", "validation_period", "valid_decision_time")
            if entry.get(key) is not None
        }
        prompt = build_system_prompt(
            fold_info=fold_info,
            acceptance_rules={
                "min_return": param("min_return"),
                "min_sharpe": param("min_sharpe"),
                "max_drawdown": param("max_drawdown"),
                "require_complete_validation": True,
            },
            phase="convergence" if epoch_index >= int(param("convergence_start_epoch") or 3) else "exploration",
            step_tree_enabled=not bool(param("disable_step_tree")),
            taste_prompt=taste,
            fold_exploration_directive=str(param("fold_exploration_directive") or ""),
            fold_directive=directive,
        )
    return {"kind": kind, "prompt": prompt, "note": PREVIEW_NOTE}
