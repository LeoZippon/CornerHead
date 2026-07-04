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

from autotrade.agent.prompts import (
    DEFAULT_ANTI_OVERFIT_PROMPT,
    DEFAULT_CONVERGENCE_PROMPT,
    PROTOCOL_INSTRUCTION,
    WRAP_UP_PROMPT,
    build_experiment_facts,
    build_meta_learning_prompt,
    build_system_prompt,
)
from autotrade.environment.nl.engine import FINAL_AFTER_TOOL_BUDGET, SUB_AGENT_SYSTEM_PROMPT

SAMPLE_FOLD = {
    "fold_id": "fold_2022Q1",
    "input_window": "20200101..20210930",
    "validation_period": "20211001..20211231",
    "test_period": "20220101..20220331",
    "valid_decision_time": "2021-09-30T23:59:59+08:00",
}
SAMPLE_ACCEPTANCE = {"min_return": 0.0, "min_sharpe": 0.0, "max_drawdown": 0.25, "require_complete_validation": True}
SAMPLE_MANIFEST = {
    "experiment_id": "exp_prompt_audit",
    "run_id": "run_sample",
    "epoch_id": "epoch_001",
    "fold_id": "fold_2022Q1",
    "kind": "fold",
    "fold": SAMPLE_FOLD,
    "runtime_env_ref": "/mnt/artifacts/runtime_env.json",
    "data_summary_ref": "/mnt/artifacts/data_summary.json",
    "fold_period": "quarter",
    "snapshot_config": {
        "decision_windows": {
            "daily_months": 21,
            "fundamentals_months": 21,
            "events_months": 21,
            "macro_months": 21,
            "text_months": 21,
            "intraday_trade_days": 21,
        }
    },
    "valid_decision_time": "2021-09-30T23:59:59+08:00",
    "is_initial_artifact": True,
    "initial_template_hash": "sha256:template",
    "modification_constraints": {"max_changed_lines": 500, "max_model_artifact_bytes": 104857600},
    "acceptance_rules": SAMPLE_ACCEPTANCE,
    "broker_profile": {
        "profile_id": "citic_default_v3",
        "initial_cash": 1000000.0,
        "commission_bps": 1.0,
        "min_commission_cny": 5.0,
        "stamp_duty_sell_bps_before_cutover": 10.0,
        "stamp_duty_sell_bps_from_cutover": 5.0,
        "stamp_duty_cutover_date": "20230828",
        "slippage_bps": 5.0,
        "short_inventory_mode": "proxy_margin_secs",
        "short_margin_ratio": 1.0,
        "short_borrow_fee_annual": 0.085,
        "short_borrow_fee_is_assumed": True,
    },
    "nl_failure_policy": "return_error_with_audit",
    "step_tree_enabled": True,
    "record_failed_attempts": True,
    "phase": "exploration",
    "max_steps": 10,
    "fold_deadline_at": "2026-06-26T21:40:00+00:00",
    "finalize_before_deadline_seconds": 300,
    "per_call_timeout_seconds": 300,
}
SAMPLE_RUNTIME_ENV = {
    "python": {"version": "3.11", "executable": "/usr/local/bin/python"},
    "network": "none",
    "python_packages": {
        "pandas": {"version": "2.2.3", "available": True},
        "pyarrow": {"version": "18.1.0", "available": True},
        "duckdb": {"version": "1.1.3", "available": True},
    },
    "tools": {
        "rg": {"available": True},
        "git": {"available": True},
        "npm": {"available": True},
        "pip": {"available": True},
        "hf": {"available": False},
    },
    "sandbox_spec": {"network": "none", "env_aliases": []},
}
SAMPLE_DATA_SUMMARY = {
    "kind": "fold",
    "large_table_guidance": [
        "events.parquet、text_index.parquet、intraday_1min.parquet 优先用 DuckDB count/limit、metadata 或按列读取。"
    ],
    "views": {
        "snapshot": {
            "mount_path": "/mnt/snapshot",
            "decision_time": "2021-09-30T23:59:59+08:00",
            "domain_windows": {"daily": {"window_months": 21}, "intraday_1min": {"trade_days": 21}},
            "files": [
                {
                    "path": "daily.parquet",
                    "mount_path": "/mnt/snapshot/daily.parquet",
                    "rows": 100000,
                    "size_bytes": 12000000,
                    "date_ranges": {"trade_date": {"min": "20200102", "max": "20210930"}},
                    "large_table": False,
                    "column_count": 14,
                    "key_columns": ["ts_code", "trade_date", "open", "close", "amount"],
                    "metadata_null_counts": {"ts_code": 0, "trade_date": 0},
                },
                {
                    "path": "intraday_1min.parquet",
                    "mount_path": "/mnt/snapshot/intraday_1min.parquet",
                    "rows": 2500000,
                    "size_bytes": 420000000,
                    "date_ranges": {"trade_time": {"min": "20210901 09:30:00", "max": "20210930 15:00:00"}},
                    "large_table": True,
                    "column_count": 8,
                    "key_columns": ["ts_code", "trade_time", "close", "amount"],
                },
            ],
            "large_tables": ["intraday_1min.parquet"],
        },
        "train": {
            "mount_path": "/mnt/snapshots/train",
            "decision_time": "2021-09-30T23:59:59+08:00",
            "files": [{"path": "daily.parquet", "mount_path": "/mnt/snapshots/train/daily.parquet", "rows": 100000}],
        },
        "valid": {
            "mount_path": "/mnt/snapshots/valid",
            "period_start": "20211001",
            "period_end": "20211231",
            "files": [{"path": "daily.parquet", "mount_path": "/mnt/snapshots/valid/daily.parquet", "rows": 12000}],
        },
    },
}
SAMPLE_EXPERIMENT_FACTS = build_experiment_facts(
    manifest=SAMPLE_MANIFEST,
    runtime_env=SAMPLE_RUNTIME_ENV,
    data_summary=SAMPLE_DATA_SUMMARY,
    max_llm_calls=80,
    context_compaction={"enabled": True, "token_threshold": 200000, "max_calls": 8},
    model_artifacts_empty=True,
)
SAMPLE_META_MANIFEST = {
    **SAMPLE_MANIFEST,
    "kind": "meta_learning",
    "fold_id": "epoch_001_meta_learning",
    "experiment_parameters": {
        "fold_period": "quarter",
        "snapshot_config": SAMPLE_MANIFEST["snapshot_config"],
        "broker_profile": SAMPLE_MANIFEST["broker_profile"],
    },
    "development_inputs": {
        "development_history": "/mnt/agent/workspace/development_history.json",
        "experiment_ledger_full": "/mnt/agent/workspace/experiment_ledger_full.jsonl",
        "meta_learning_memory": "/mnt/agent/workspace/meta_learning_memory.jsonl",
        "previous_taste": False,
    },
    "taste_output": "/mnt/agent/workspace/taste.md",
    "web_search_engines": ["tavily", "semantic_scholar"],
    "meta_learning_directive": "",
    "sandbox_runtime": {
        "active_env_passthrough": ["GITHUB_TOKEN", "HF_TOKEN"],
        "active_env_aliases": [
            {"container_env": "AT_PROXY_HTTP", "host_env": "HTTP_PROXY"},
            {"container_env": "AT_PROXY_HTTPS", "host_env": "HTTPS_PROXY"},
            {"container_env": "AT_PROXY_ALL", "host_env": "ALL_PROXY"},
            {"container_env": "AT_PROXY_NO_PROXY", "host_env": "NO_PROXY"},
        ]
    },
}
SAMPLE_META_FACTS = build_experiment_facts(
    manifest=SAMPLE_META_MANIFEST,
    runtime_env={
        **SAMPLE_RUNTIME_ENV,
        "network": "bridge",
        "sandbox_spec": {
            "network": "bridge",
            "env_aliases": [
                {"container_env": "AT_PROXY_HTTP", "host_env": "HTTP_PROXY"},
                {"container_env": "AT_PROXY_HTTPS", "host_env": "HTTPS_PROXY"},
                {"container_env": "AT_PROXY_ALL", "host_env": "ALL_PROXY"},
                {"container_env": "AT_PROXY_NO_PROXY", "host_env": "NO_PROXY"},
            ],
        },
    },
    data_summary=SAMPLE_DATA_SUMMARY,
    max_llm_calls=80,
    context_compaction={"enabled": True, "token_threshold": 200000, "max_calls": 8},
    model_artifacts_empty=True,
)


def _section_anchor(index: int) -> str:
    return f"prompt-section-{index}"


def _render_prompt_section(index: int, title: str, body: str) -> list[str]:
    text = body.rstrip()
    fence = "````"
    return [
        f'<a id="{_section_anchor(index)}"></a>',
        f"## {index}. {title}",
        "",
        f"<details{' open' if index == 1 else ''}>",
        f"<summary>完整文本，{len(text):,} 字符</summary>",
        "",
        f"{fence}text",
        text,
        fence,
        "",
        "</details>",
        "",
    ]


def render() -> str:
    sections = [
        (
            "Fold Agent 系统提示词（完整渲染示例）",
            build_system_prompt(
                fold_info=SAMPLE_FOLD,
                acceptance_rules=SAMPLE_ACCEPTANCE,
                experiment_facts=SAMPLE_EXPERIMENT_FACTS,
                step_tree_enabled=True,
                taste_prompt="优先探索可迁移的价格-成交量结构；谨慎处理单一题材经验。",
            ),
        ),
        ("Fold Agent 协议模板（PROTOCOL_INSTRUCTION）", PROTOCOL_INSTRUCTION),
        ("收尾提示（WRAP_UP_PROMPT，T-5 分钟最多一次）", WRAP_UP_PROMPT),
        ("防过拟合构件（DEFAULT_ANTI_OVERFIT_PROMPT，注入“阶段策略与防过拟合”，两阶段都生效）", DEFAULT_ANTI_OVERFIT_PROMPT),
        ("收敛构件（DEFAULT_CONVERGENCE_PROMPT，仅收敛期注入“阶段策略与防过拟合”）", DEFAULT_CONVERGENCE_PROMPT),
        (
            "元学习 Agent System Prompt（基础模板）",
            build_meta_learning_prompt(experiment_facts=SAMPLE_META_FACTS),
        ),
        (
            "元学习 Agent System Prompt（含实验级探索方向示例）",
            build_meta_learning_prompt(
                experiment_facts=SAMPLE_META_FACTS,
                experiment_directive="示例：优先评估分钟级流动性冲击后的反转假设，并说明是否值得进入后续 Fold。",
            ),
        ),
        ("NL Sub Agent 系统提示词（SUB_AGENT_SYSTEM_PROMPT）", SUB_AGENT_SYSTEM_PROMPT),
        ("NL Sub Agent 工具预算耗尽提示（FINAL_AFTER_TOOL_BUDGET）", FINAL_AFTER_TOOL_BUDGET),
    ]
    lines = [
        "# Prompt 模板审计快照",
        "",
        "由 `scripts/dev/export_prompts.py` 从代码渲染；代码是唯一事实来源：",
        "",
        "- `src/autotrade/agent/prompts.py`",
        "- `src/autotrade/environment/nl/engine.py`",
        "",
        "阅读说明：每个 Prompt 块都按模型实际接收的文本原样放入 `text` 代码块；为减少页面噪声，除第一节外默认折叠。NL Sub Agent 的用户消息为 JSON object：`{request: {ts_code, prompt, kwargs}, company_context}`；最终回答不限定格式，只有 `text_retrieve` 工具调用需要使用约定 JSON。",
        "",
        "## 导航",
        "",
    ]
    for index, (title, _body) in enumerate(sections, start=1):
        lines.append(f"- [{index}. {title}](#{_section_anchor(index)})")
    lines.append("")
    for index, (title, body) in enumerate(sections, start=1):
        lines.extend(_render_prompt_section(index, title, body))
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
