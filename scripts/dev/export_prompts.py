#!/usr/bin/env python3
"""Export every Agent/LLM prompt template to configs/prompts/PROMPTS.md for audit.

The code remains the single source of truth; this exporter renders the
templates (with a sample fold context) so reviewers can read exactly what the
models see.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from _bootstrap import add_repo_src

add_repo_src(__file__)

from hl_trader.agent.prompts import (
    DEFAULT_ANTI_OVERFIT_PROMPT,
    DEFAULT_CONVERGENCE_PROMPT,
    META_LEARNING_INSTRUCTION,
    PROTOCOL_INSTRUCTION,
    WRAP_UP_PROMPT,
    build_meta_learning_prompt,
    build_system_prompt,
)
from hl_trader.environment.nl.engine import FINAL_INSTRUCTION, REPAIR_INSTRUCTION, ROUND_INSTRUCTION

SAMPLE_FOLD = {
    "fold_id": "fold_2022Q1",
    "input_window": "20200101..20210930",
    "validation_period": "20211001..20211231",
    "test_period": "20220101..20220331",
    "valid_decision_time": "2021-10-08T09:25:00+08:00",
}
SAMPLE_ACCEPTANCE = {"min_return": 0.0, "min_sharpe": 0.0, "max_drawdown": 0.25, "require_complete_validation": True}


def render() -> str:
    sections = [
        (
            "Fold Agent 系统提示词（完整渲染示例）",
            build_system_prompt(
                fold_info=SAMPLE_FOLD,
                acceptance_rules=SAMPLE_ACCEPTANCE,
                taste_prompt="优先探索可迁移的价格-成交量结构；谨慎处理单一题材经验。",
            ),
        ),
        ("Fold Agent 协议模板（PROTOCOL_INSTRUCTION）", PROTOCOL_INSTRUCTION),
        ("收尾提示（WRAP_UP_PROMPT，T-5 分钟最多一次）", WRAP_UP_PROMPT),
        ("防过拟合约束（DEFAULT_ANTI_OVERFIT_PROMPT）", DEFAULT_ANTI_OVERFIT_PROMPT),
        ("收敛与早停建议（DEFAULT_CONVERGENCE_PROMPT）", DEFAULT_CONVERGENCE_PROMPT),
        ("元学习 + 正则化系统提示词（完整渲染示例）", build_meta_learning_prompt({"experiment_ledger": "experiments/<id>/ledgers/experiment_ledger.jsonl"})),
        ("元学习协议模板（META_LEARNING_INSTRUCTION）", META_LEARNING_INSTRUCTION),
        ("自然语言评分轮次提示（ROUND_INSTRUCTION，system）", ROUND_INSTRUCTION),
        ("自然语言评分收口提示（FINAL_INSTRUCTION）", FINAL_INSTRUCTION),
        ("自然语言评分修复提示（REPAIR_INSTRUCTION，每股票最多一次）", REPAIR_INSTRUCTION),
    ]
    lines = [
        "# Prompt 模板审计快照",
        "",
        "由 `scripts/dev/export_prompts.py` 从代码渲染；代码是唯一事实来源（`src/hl_trader/agent/prompts.py`、`src/hl_trader/environment/nl/engine.py`）。",
        "自然语言评分的用户消息为 JSON object：`{candidate: {ts_code}, company_context, prior_rules, evidence}`，不包含因子分、排名、权重或其他股票结论。",
        "",
    ]
    for title, body in sections:
        lines += [f"## {title}", "", "```text", body.rstrip(), "```", ""]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("configs/prompts/PROMPTS.md"))
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render(), encoding="utf-8")
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
