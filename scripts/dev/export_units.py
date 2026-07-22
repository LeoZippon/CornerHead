#!/usr/bin/env python3
"""Render the structured unit registry to docs/units_reference.md.

``UNIT_RULES`` in src/autotrade/environment/data/units.py stays the single
source of truth; this exporter renders it so the reference document can never
drift from the code (a freshness test regenerates and compares).
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

from autotrade.environment.data.units import UNIT_RULES

DOC_RELATIVE_PATH = Path("docs/units_reference.md")

FILE_TITLES = {
    "daily.parquet": "daily.parquet（日频归一化文件）",
    "intraday_1min.parquet": "intraday_1min.parquet（历史分钟线）",
    "auction.parquet": "auction.parquet（开盘竞价）",
    "events.parquet": "events.parquet（事件/资金/打板 source union）",
    "macro.parquet": "macro.parquet（宏观与跨资产 source union）",
    "fundamentals.parquet": "fundamentals.parquet（财务 source union）",
    "raw_only": "仅原始湖数据（不进入快照）",
}


def _cell(text: str) -> str:
    return text.replace("|", "\\|") if text else "—"


def render_units_markdown() -> str:
    lines = [
        "# 单位参考表",
        "",
        "本文档由 `scripts/dev/export_units.py` 从 `src/autotrade/environment/data/units.py` 的",
        "`UNIT_RULES` 生成，禁止手工编辑；回归测试会重新生成并与本文件比对。",
        "单位口径的分层边界与使用纪律见 `docs/data_documentation.md` §1.2。",
        "",
        "状态含义：`verified` 已经真实数据比对核验；`official` 依据供应商官方字段合同；",
        "`inferred` 仅由局部证据推断，使用前应补核验。`snapshot factor` 为快照载入时的乘数",
        "（空表示保留源单位）。`agent` 为否的条目不进入 Agent 合同（非默认快照数据集）。",
    ]
    files = list(dict.fromkeys(rule.file for rule in UNIT_RULES))
    for file in files:
        lines += ["", f"## {FILE_TITLES[file]}", ""]
        lines.append("| dataset | 字段族 | 源单位 | snapshot factor | 状态 | agent | 依据/说明 |")
        lines.append("|---|---|---|---|---|---|---|")
        for rule in UNIT_RULES:
            if rule.file != file:
                continue
            factor = f"×{rule.factor:g} → {rule.normalized_unit}" if rule.factor is not None else ""
            basis = "；".join(part for part in (rule.evidence, rule.note) if part)
            lines.append(
                "| " + " | ".join([
                    _cell(f"`{rule.dataset}`" if rule.dataset else ""),
                    _cell(rule.fields),
                    _cell(rule.source_unit),
                    _cell(factor),
                    rule.status,
                    "是" if rule.agent_visible else "否",
                    _cell(basis),
                ]) + " |"
            )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if the committed document is stale")
    args = parser.parse_args()
    doc_path = Path(__file__).resolve().parents[2] / DOC_RELATIVE_PATH
    rendered = render_units_markdown()
    if args.check:
        if not doc_path.exists() or doc_path.read_text(encoding="utf-8") != rendered:
            print(f"{DOC_RELATIVE_PATH} is stale; run scripts/dev/export_units.py", file=sys.stderr)
            return 1
        return 0
    doc_path.write_text(rendered, encoding="utf-8")
    print(f"wrote {doc_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
