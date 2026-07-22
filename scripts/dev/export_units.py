#!/usr/bin/env python3
"""Render the unit registry to docs/units_reference.md; refresh the schema inventory.

``FIELD_RULES`` in src/autotrade/environment/data/units.py stays the single
source of truth; this exporter renders it so the reference document cannot
drift from the code (a freshness test regenerates and compares).

``--refresh-inventory`` rescans the raw lake and rewrites
configs/data/snapshot_columns.json (the committed per-dataset column
inventory that tests resolve against). Run it when vendor schemas change.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from _bootstrap import add_repo_src

add_repo_src(__file__)

from autotrade.environment.data.units import (
    COMMON_FIELD_SEMANTICS,
    FIELD_RULES,
    NO_NUMERIC_DATASETS,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DOC_PATH = REPO_ROOT / "docs" / "units_reference.md"
INVENTORY_PATH = REPO_ROOT / "configs" / "data" / "snapshot_columns.json"

FILE_TITLES = {
    "daily.parquet": "daily.parquet（日频归一化文件）",
    "intraday_1min.parquet": "intraday_1min.parquet（历史分钟线）",
    "auction.parquet": "auction.parquet（开盘竞价）",
    "corporate_actions.parquet": "corporate_actions.parquet（回放分红送转）",
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
        "`FIELD_RULES` 生成，禁止手工编辑；回归测试会重新生成并与本文件比对。",
        "单位口径的查找规则与使用纪律见 `docs/data_documentation.md` §1.2。",
        "",
        "本表是注册表的**规则视图**（按列名/通配符定位的规则行）。逐列展开后的完整字段级",
        "单位表随每次快照生成为 `/mnt/artifacts/unit_reference.json`，只包含当前快照实际可见",
        "的 file/dataset/column；完备性由两道校验保证——快照构建对每一列强制解析（缺规则即",
        "失败），回归测试对 `configs/data/snapshot_columns.json` 的全量供应商列清单强制解析。",
        "",
        "状态含义：`verified` 已与另一数据源或已知外部事实对账（依据见 evidence 列）；",
        "`official` 依据供应商官方字段合同；`inferred` 仅由本地量级合理性推断；`unknown`",
        "诚实未解决——此类字段不得进入绝对阈值或跨数据集算术。`factor` 为快照载入时的乘数",
        "（归一化文件存换算后的值）。",
    ]
    files = list(dict.fromkeys(rule.file for rule in FIELD_RULES))
    for file in files:
        lines += ["", f"## {FILE_TITLES[file]}", ""]
        lines.append("| dataset | 列（名/通配） | 语义 | 源单位 | factor | 状态 | 依据/说明 |")
        lines.append("|---|---|---|---|---|---|---|")
        for rule in FIELD_RULES:
            if rule.file != file:
                continue
            factor = f"×{rule.factor:g} → {rule.normalized_unit}" if rule.factor is not None else ""
            basis = "；".join(part for part in (rule.evidence, rule.note) if part)
            lines.append(
                "| " + " | ".join([
                    _cell(f"`{rule.dataset}`" if rule.dataset else ""),
                    _cell("/".join(rule.columns)),
                    rule.semantic,
                    _cell(rule.source_unit or ""),
                    _cell(factor),
                    rule.status,
                    _cell(basis),
                ]) + " |"
            )
    lines += [
        "",
        "## 无数值字段的数据集",
        "",
        "以下数据集全部字段均为标识/日期/文本，由通用分类器解析，不携带单位规则：",
        "`" + "`、`".join(sorted(NO_NUMERIC_DATASETS)) + "`。",
        "",
        "## 通用列分类器（按序首个匹配；数据集规则优先）",
        "",
        "| 模式 | 语义 |",
        "|---|---|",
    ]
    for pattern, semantic in COMMON_FIELD_SEMANTICS:
        lines.append(f"| `{pattern}` | {semantic} |")
    lines.append("")
    return "\n".join(lines)


def refresh_inventory() -> None:
    import pyarrow.parquet as pq

    from autotrade.environment.data.fundamental_events import FUNDAMENTAL_EVENT_DATASETS
    from autotrade.environment.data.snapshot import SnapshotConfig

    raw = REPO_ROOT / "data" / "raw"
    if not raw.exists():
        raise FileNotFoundError(f"raw lake not available at {raw}; run on the data host")
    config = SnapshotConfig()
    fund_sidecars = ["dataset", "source_path", "source_hash", "source_row_id", "business_key", "available_month"]
    plans = [
        ("events.parquet", tuple(config.events_datasets), ["dataset"]),
        ("macro.parquet", tuple(config.macro_datasets), ["dataset"]),
        ("fundamentals.parquet", tuple(FUNDAMENTAL_EVENT_DATASETS), fund_sidecars),
    ]
    files: dict[str, dict[str, list[str]]] = {}
    for file, datasets, extra in plans:
        for dataset in datasets:
            parquets = sorted((raw / dataset).rglob("*.parquet"))
            if not parquets:
                raise FileNotFoundError(f"no raw parquet partitions for dataset {dataset}")
            indexes = sorted({0, len(parquets) // 4, len(parquets) // 2, 3 * len(parquets) // 4, len(parquets) - 1})
            columns: set[str] = set(extra)
            for index in indexes:
                columns.update(pq.read_schema(parquets[index]).names)
            files.setdefault(file, {})[dataset] = sorted(columns)
    inventory = {
        "note": (
            "Sampled union of raw vendor parquet schemas per snapshot dataset, plus snapshot-builder "
            "provenance columns. Regenerate with scripts/dev/export_units.py --refresh-inventory. "
            "Tests resolve every column against the unit registry; the snapshot build re-validates live."
        ),
        "files": files,
    }
    INVENTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    INVENTORY_PATH.write_text(json.dumps(inventory, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {INVENTORY_PATH}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if the committed document is stale")
    parser.add_argument("--refresh-inventory", action="store_true",
                        help="rescan the raw lake and rewrite configs/data/snapshot_columns.json")
    args = parser.parse_args()
    if args.refresh_inventory:
        refresh_inventory()
    rendered = render_units_markdown()
    if args.check:
        if not DOC_PATH.exists() or DOC_PATH.read_text(encoding="utf-8") != rendered:
            print("docs/units_reference.md is stale; run scripts/dev/export_units.py", file=sys.stderr)
            return 1
        return 0
    DOC_PATH.write_text(rendered, encoding="utf-8")
    print(f"wrote {DOC_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
