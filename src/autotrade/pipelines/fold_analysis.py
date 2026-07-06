"""Post-fold strategy analysis for the HITL console (docs/pipeline_design.md).

After a fold completes, a predefined template asks the LLM to review the frozen
strategy in natural language. The analysis is researcher-facing only: it is
never fed back into any Agent prompt, and its evidence is deliberately limited
to validation-period results — test-period metrics are excluded so the analysis
cannot become a test-leakage channel through the researcher's next-fold
directive (guarded test view).

Provider calls go through the existing LLMProxy, so the mandatory conversation
log JSONL is written by the client layer automatically.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping

from autotrade.environment.runtime import utc_now_iso

ANALYSIS_SCHEMA_VERSION = 1
DEFAULT_TIMEOUT_SECONDS = 900.0
MAX_FILE_CHARS = 20_000
MAX_TOTAL_CHARS = 60_000
_TEXT_SUFFIXES = {".py", ".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".cfg", ".ini", ".csv"}

# Validation-only projection of the fold ledger record. test_result and the
# test schedule are intentionally absent (guarded test view).
_RECORD_FIELDS = (
    "epoch_id",
    "fold_id",
    "input_window",
    "validation_period",
    "fold_status",
    "finish_reason",
    "accept_reasons",
    "selected_step_id",
    "steps",
    "validation_result",
    "fold_directive",
    "frozen_strategy_artifact_id",
)

FOLD_ANALYSIS_SYSTEM_PROMPT = """\
你是一名资深量化策略审阅人，负责向研究者解读一个由自主 Agent 在滚动 Fold 内产出的 A 股策略。
你只掌握验证期证据：Fold 元信息、验证回测摘要、Step 历史与冻结策略代码。测试期结果对你不可见，\
不要猜测或臆造任何测试期表现。

输出要求：
- 用简体中文撰写，Markdown 格式，面向人类研究者，语言精炼、可直接阅读。
- 依次给出以下小节（使用 `##` 标题）：
  1. `策略逻辑概述` — 策略在做什么，信号、组合构建与执行节奏。
  2. `数据与信号使用` — 用到了哪些数据域/特征，是否合理，有无可疑的硬编码或数据窥探迹象。
  3. `风险与过拟合迹象` — 参数敏感性、样本依赖、复杂度、死代码或与验证期特定行情耦合的规则。
  4. `验证表现解读` — 结合验证摘要与 Step 历史，说明表现来源与稳健性，注意区分运气与结构性收益。
  5. `下一 Fold 可探索方向` — 2-4 条具体、可检验的改进假设，供研究者写入下一个 Fold 的指令；\
不要包含具体日历日期或特定月份行情经验。
- 所有结论必须能从给定材料推出；材料不足时明确说不确定，不要脑补。\
"""


def analysis_key(epoch_id: str, fold_id: str) -> str:
    return f"{str(epoch_id)}__{str(fold_id)}"


def analysis_paths(out_dir: Path, epoch_id: str, fold_id: str) -> tuple[Path, Path]:
    key = analysis_key(epoch_id, fold_id)
    return out_dir / f"{key}.md", out_dir / f"{key}.json"


def guarded_record_view(record: Mapping[str, object]) -> dict[str, object]:
    """Project a fold ledger record down to validation-only evidence."""
    return {key: record.get(key) for key in _RECORD_FIELDS if record.get(key) is not None}


def read_strategy_files(
    strategy_dir: Path,
    *,
    max_file_chars: int = MAX_FILE_CHARS,
    max_total_chars: int = MAX_TOTAL_CHARS,
) -> list[dict[str, object]]:
    """Read the frozen strategy files with per-file and total char budgets.

    main.py always comes first; the freeze manifest.json is metadata, not
    strategy content, and is skipped. Non-text or over-budget content degrades
    to an explicit marker instead of silently vanishing.
    """
    strategy_dir = Path(strategy_dir)
    files = sorted(
        (path for path in strategy_dir.rglob("*") if path.is_file() and path.name != "manifest.json"),
        key=lambda path: (path.relative_to(strategy_dir) != Path("main.py"), str(path.relative_to(strategy_dir))),
    )
    entries: list[dict[str, object]] = []
    remaining = max_total_chars
    for path in files:
        rel = str(path.relative_to(strategy_dir))
        if path.suffix.lower() not in _TEXT_SUFFIXES:
            entries.append({"path": rel, "skipped": f"non-text file ({path.stat().st_size} bytes)"})
            continue
        if remaining <= 0:
            entries.append({"path": rel, "skipped": "total content budget exhausted"})
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            entries.append({"path": rel, "skipped": f"unreadable: {exc}"})
            continue
        budget = min(max_file_chars, remaining)
        truncated = len(content) > budget
        if truncated:
            content = content[:budget]
        remaining -= len(content)
        entries.append({"path": rel, "content": content, "truncated": truncated})
    return entries


def build_fold_analysis_messages(
    record: Mapping[str, object],
    strategy_files: list[dict[str, object]],
    *,
    model_files: list[str] | None = None,
) -> list[dict[str, str]]:
    guarded = guarded_record_view(record)
    parts = [
        "# Fold 元信息与验证证据（JSON）",
        json.dumps(guarded, ensure_ascii=False, indent=2, sort_keys=True, default=str),
        "",
        "# 冻结策略产物 output/",
    ]
    for entry in strategy_files:
        parts.append(f"\n## 文件 {entry['path']}")
        if "content" in entry:
            suffix = "\n...[截断]" if entry.get("truncated") else ""
            parts.append(f"```\n{entry['content']}{suffix}\n```")
        else:
            parts.append(f"（未内联：{entry.get('skipped')}）")
    if model_files:
        parts.append("\n# 模型参数产物 models/（仅文件清单）")
        parts.extend(f"- {name}" for name in model_files)
    return [
        {"role": "system", "content": FOLD_ANALYSIS_SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(parts)},
    ]


def analyze_fold(
    proxy,
    *,
    ledger_record: Mapping[str, object],
    strategy_dir: Path,
    model_dir: Path | None,
    out_dir: Path,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> Path:
    """Run the analysis template against one completed fold and persist it.

    Writes ``<epoch>__<fold>.md`` plus a sidecar ``.json`` with provenance. A
    provider failure writes an error sidecar and re-raises so the caller can
    record it (the interactive runner treats analysis as advisory).
    """
    epoch_id = str(ledger_record.get("epoch_id") or "epoch_unknown")
    fold_id = str(ledger_record.get("fold_id") or "fold_unknown")
    md_path, meta_path = analysis_paths(Path(out_dir), epoch_id, fold_id)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    strategy_files = read_strategy_files(Path(strategy_dir))
    model_files = (
        sorted(str(path.relative_to(model_dir)) for path in Path(model_dir).rglob("*") if path.is_file())
        if model_dir is not None and Path(model_dir).is_dir()
        else []
    )
    messages = build_fold_analysis_messages(ledger_record, strategy_files, model_files=model_files)
    meta: dict[str, object] = {
        "schema_version": ANALYSIS_SCHEMA_VERSION,
        "epoch_id": epoch_id,
        "fold_id": fold_id,
        "provider": getattr(proxy, "provider", "unknown"),
        "model": getattr(proxy, "model", "unknown"),
        "created_at": utc_now_iso(),
        "guarded_view": "validation_only",
    }
    try:
        response = proxy.complete(messages, json_mode=False, timeout_seconds=timeout_seconds)
    except Exception as exc:
        meta.update(status="error", error=f"{type(exc).__name__}: {exc}")
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        raise
    content = str(response.content or "").strip()
    if not content:
        meta.update(status="error", error="provider returned empty analysis content")
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        raise RuntimeError("fold analysis returned empty content")
    md_path.write_text(content + "\n", encoding="utf-8")
    meta.update(status="ok", usage=dict(response.usage or {}), analysis_path=str(md_path))
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return md_path
