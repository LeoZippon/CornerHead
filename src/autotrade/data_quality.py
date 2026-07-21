"""Small, shared contract for machine-readable data-quality reports."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping


QUALITY_REPORT_SCHEMA_VERSION = 1
QUALITY_REPORT_KEYS = frozenset(
    {
        "schema_version",
        "report_type",
        "created_at",
        "status",
        "scope",
        "finding_counts",
        "datasets",
        "findings",
        "metadata",
    }
)
FINDING_KEYS = frozenset({"severity", "check", "message", "details"})
DATASET_SUMMARY_KEYS = frozenset({"status", "finding_counts", "checks"})
SCOPE_KEYS = frozenset({"data_root", "start_date", "end_date", "datasets"})
SEVERITIES = ("error", "warning", "info")


def count_findings(findings: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    counts = {severity: 0 for severity in SEVERITIES}
    for finding in findings:
        severity = finding.get("severity")
        if severity not in counts:
            raise ValueError(f"unknown data-quality severity: {severity!r}")
        counts[str(severity)] += 1
    return counts


def quality_status(counts: Mapping[str, int]) -> str:
    if int(counts.get("error", 0)):
        return "error"
    if int(counts.get("warning", 0)):
        return "warning"
    return "ok"


def summarize_datasets(
    findings: Iterable[Mapping[str, Any]], dataset_names: Iterable[str]
) -> dict[str, dict[str, Any]]:
    """Map check-name prefixes to a compact, uniform per-dataset summary."""
    names = sorted(set(dataset_names), key=len, reverse=True)
    summary: dict[str, dict[str, Any]] = {}
    for finding in findings:
        check = str(finding.get("check") or "")
        dataset = next(
            (
                name
                for name in names
                if check == name
                or check.startswith(f"{name}_")
                or check.startswith(f"source_{name}")
            ),
            None,
        )
        if dataset is None:
            continue
        item = summary.setdefault(
            dataset,
            {
                "status": "ok",
                "finding_counts": {severity: 0 for severity in SEVERITIES},
                "checks": [],
            },
        )
        severity = str(finding.get("severity") or "info")
        if severity not in SEVERITIES:
            raise ValueError(f"unknown data-quality severity: {severity!r}")
        item["finding_counts"][severity] += 1
        item["checks"].append(check)
    for item in summary.values():
        item["status"] = quality_status(item["finding_counts"])
        item["checks"] = sorted(set(item["checks"]))
    return dict(sorted(summary.items()))


def build_quality_report(
    *,
    report_type: str,
    scope: Mapping[str, Any],
    findings: Iterable[Mapping[str, Any]],
    datasets: Mapping[str, Mapping[str, Any]] | None = None,
    metadata: Mapping[str, Any] | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Build the exact envelope shared by every retained quality report.

    ``scope`` and ``metadata`` are intentionally report-specific. Finding
    payloads are discriminated by ``check``; their four-key outer record stays
    fixed while ``details`` carries only the fields needed by that check.
    """
    records = [dict(finding) for finding in findings]
    scope_record = dict(scope)
    missing_scope = SCOPE_KEYS - set(scope_record)
    if missing_scope:
        raise ValueError(f"data-quality scope is missing {sorted(missing_scope)}")
    for finding in records:
        if set(finding) != FINDING_KEYS:
            raise ValueError(
                "data-quality findings require exactly "
                f"{sorted(FINDING_KEYS)}, got {sorted(finding)}"
            )
        if not isinstance(finding["details"], dict):
            raise TypeError("data-quality finding details must be an object")
        if not str(finding["check"]).strip() or not str(finding["message"]).strip():
            raise ValueError("data-quality finding check and message must be non-empty")
    counts = count_findings(records)
    dataset_records = {name: dict(value) for name, value in (datasets or {}).items()}
    for name, summary in dataset_records.items():
        if set(summary) != DATASET_SUMMARY_KEYS:
            raise ValueError(
                f"dataset summary {name!r} requires exactly {sorted(DATASET_SUMMARY_KEYS)}"
            )
        if set(summary["finding_counts"]) != set(SEVERITIES):
            raise ValueError(f"dataset summary {name!r} has invalid finding counts")
    return {
        "schema_version": QUALITY_REPORT_SCHEMA_VERSION,
        "report_type": str(report_type),
        "created_at": created_at or datetime.now(timezone.utc).isoformat(),
        "status": quality_status(counts),
        "scope": scope_record,
        "finding_counts": counts,
        "datasets": dataset_records,
        "findings": records,
        "metadata": dict(metadata or {}),
    }


def write_quality_report(path: str | Path, report: Mapping[str, Any]) -> None:
    """Atomically publish one validated quality report."""
    if set(report) != QUALITY_REPORT_KEYS:
        raise ValueError(
            f"quality report requires exactly {sorted(QUALITY_REPORT_KEYS)}, got {sorted(report)}"
        )
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False),
        encoding="utf-8",
    )
    os.replace(temporary, output)
