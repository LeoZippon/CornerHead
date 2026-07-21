#!/usr/bin/env python3
"""TuShare data-quality audit CLI for AutoTrade."""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow.parquet as pq

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from autotrade.data_sources.tushare import common as core
    from autotrade.data_sources.tushare.common import *  # noqa: F401,F403
else:
    from . import common as core
    from .common import *  # noqa: F401,F403

from autotrade.data_quality import build_quality_report, summarize_datasets, write_quality_report

def audit_trade_date_dataset(raw_dir: Path, spec: TradeDateDataset, expected_dates: set[str], add) -> None:
    files = sorted((raw_dir / spec.api_name).glob("trade_date=*.parquet"))
    file_dates = {partition_date(path): path for path in files}
    row_counts = {trade_date: parquet_rows(path) for trade_date, path in file_dates.items()}
    zero_dates = sorted(d for d, count in row_counts.items() if count == 0)
    nonzero_dates = sorted(d for d, count in row_counts.items() if count > 0)
    missing = sorted(expected_dates - set(file_dates))
    extra = sorted(set(file_dates) - expected_dates)
    exact_limit_dates = sorted(d for d, count in row_counts.items() if count in {5000, 6000, 7000, 8000, 10000} and not has_pagination_probe(file_dates[d]))
    details = {
        "files": len(files),
        "rows": int(sum(row_counts.values())),
        "expected_files": len(expected_dates),
        "missing_expected_files": len(missing),
        "extra_files": len(extra),
        "zero_row_partitions": len(zero_dates),
        "first_file_date": min(file_dates) if file_dates else None,
        "last_file_date": max(file_dates) if file_dates else None,
        "first_nonzero_date": nonzero_dates[0] if nonzero_dates else None,
        "last_nonzero_date": nonzero_dates[-1] if nonzero_dates else None,
        "missing_sample": missing[:20],
        "extra_sample": extra[:20],
        "zero_sample": zero_dates[:20],
        "exact_common_limit_row_count_dates": exact_limit_dates[:20],
    }
    has_partition_error = not files or bool(missing) or (bool(zero_dates) and not spec.zero_rows_ok)
    severity = "error" if has_partition_error else "warning" if exact_limit_dates else "info"
    add(severity, f"{spec.api_name}_partitions", f"{spec.api_name} trade-date partition checks", details)

    key_details = audit_partition_keys(files, spec)
    has_key_error = any(key_details[name] for name in ("blank_trade_date", "blank_ts_code", "duplicate_key_rows", "filename_trade_date_mismatches", "missing_key_column_files"))
    add("error" if has_key_error else "info", f"{spec.api_name}_keys", f"{spec.api_name} key checks", key_details)

def audit_partition_keys(files: list[Path], spec: TradeDateDataset) -> dict[str, Any]:
    duplicate_rows = 0
    blank_trade_date = 0
    blank_ts_code = 0
    filename_mismatches = 0
    missing_key_column_files: list[str] = []
    key_columns = list(spec.key_columns)
    for path in files:
        if parquet_rows(path) == 0:
            continue
        schema = pq.ParquetFile(path).schema_arrow.names
        missing = [col for col in key_columns if col not in schema]
        if missing:
            missing_key_column_files.append(str(path))
            continue
        df = pd.read_parquet(path, columns=key_columns)
        if "trade_date" in df:
            trade_dates = df["trade_date"].astype(str).str.strip()
            blank_trade_date += int(df["trade_date"].isna().sum() + (trade_dates == "").sum())
            filename_mismatches += int((trade_dates != partition_date(path)).sum())
        if "ts_code" in df:
            ts_codes = df["ts_code"].astype(str).str.strip()
            blank_ts_code += int(df["ts_code"].isna().sum() + (ts_codes == "").sum())
        duplicate_rows += int(df.duplicated(key_columns).sum())
    return {
        "files_checked": len(files),
        "key_columns": key_columns,
        "blank_trade_date": blank_trade_date,
        "blank_ts_code": blank_ts_code,
        "duplicate_key_rows": duplicate_rows,
        "filename_trade_date_mismatches": filename_mismatches,
        "missing_key_column_files": len(missing_key_column_files),
        "missing_key_column_sample": missing_key_column_files[:10],
    }

def select_revision_sentinel_dates(trade_dates: list[str], sample_size: int, seed: str) -> list[str]:
    if sample_size <= 0 or len(trade_dates) <= sample_size:
        return sorted(trade_dates)
    ranked = sorted(trade_dates, key=lambda item: stable_hash({"seed": seed, "trade_date": item}))
    return sorted(ranked[:sample_size])

REVISION_HISTORY_SAMPLE_STATUS_PATH = "results/data_quality/process/revision_history_sample_status.json"
REVISION_HISTORY_SAMPLE_EVENTS_PATH = "results/data_quality/process/revision_history_sample_events.jsonl"

def select_revision_history_dates_by_year(trade_dates: list[str], sample_per_year: int, seed: str) -> dict[str, list[str]]:
    by_year: dict[str, list[str]] = defaultdict(list)
    for trade_date in sorted(trade_dates):
        by_year[trade_date[:4]].append(trade_date)
    selected: dict[str, list[str]] = {}
    for year, dates in sorted(by_year.items()):
        if sample_per_year <= 0 or len(dates) <= sample_per_year:
            selected[year] = sorted(dates)
            continue
        ranked = sorted(dates, key=lambda item: stable_hash({"seed": seed, "year": year, "trade_date": item}))
        selected[year] = sorted(ranked[:sample_per_year])
    return selected

def revision_numeric_deltas(old_df: pd.DataFrame, new_df: pd.DataFrame, key_columns: list[str]) -> dict[str, Any]:
    keys = list(key_columns)
    if old_df.empty or new_df.empty:
        return {"issue": "empty_side", "columns": {}}
    missing_old = [column for column in keys if column not in old_df.columns]
    missing_new = [column for column in keys if column not in new_df.columns]
    if missing_old or missing_new:
        return {"issue": "missing_key_columns", "columns": {}}
    if old_df.duplicated(keys).any() or new_df.duplicated(keys).any():
        return {"issue": "duplicate_key_rows", "columns": {}}

    def keyed(df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        out["__revision_key"] = [
            tuple(canonical_revision_value(record[column]) for column in keys)
            for record in out[keys].to_dict("records")
        ]
        return out.set_index("__revision_key", drop=True)

    old_keyed = keyed(old_df)
    new_keyed = keyed(new_df)
    common_keys = old_keyed.index.intersection(new_keyed.index)
    common_columns = sorted((set(old_keyed.columns) & set(new_keyed.columns)) - set(keys))
    columns: dict[str, dict[str, Any]] = {}
    for column in common_columns:
        old_values = pd.to_numeric(old_keyed.loc[common_keys, column], errors="coerce")
        new_values = pd.to_numeric(new_keyed.loc[common_keys, column], errors="coerce")
        mask = old_values.notna() & new_values.notna() & (old_values != new_values)
        if not bool(mask.any()):
            continue
        deltas = (new_values[mask] - old_values[mask]).astype(float)
        abs_deltas = deltas.abs()
        samples = []
        for key, old_value, new_value, delta in zip(deltas.index[:5], old_values[mask].iloc[:5], new_values[mask].iloc[:5], deltas.iloc[:5]):
            samples.append({
                "key": list(key) if isinstance(key, tuple) else [str(key)],
                "old": float(old_value),
                "new": float(new_value),
                "delta": float(delta),
            })
        columns[column] = {
            "changed_cells": int(mask.sum()),
            "sum_abs_delta": float(abs_deltas.sum()),
            "sum_signed_delta": float(deltas.sum()),
            "max_abs_delta": float(abs_deltas.max()),
            "abs_deltas": [float(value) for value in abs_deltas.tolist()],
            "samples": samples,
        }
    return {"issue": "", "columns": columns}

def keyed_revision_rows(df: pd.DataFrame, key_columns: list[str], value_columns: list[str]) -> dict[tuple[str, ...], dict[str, str]]:
    if df.empty:
        return {}
    normalized = df.copy()
    for column in key_columns + value_columns:
        if column not in normalized.columns:
            normalized[column] = ""
    rows: dict[tuple[str, ...], dict[str, str]] = {}
    for record in normalized[key_columns + value_columns].to_dict("records"):
        key = tuple(canonical_revision_value(record[column]) for column in key_columns)
        rows[key] = {column: canonical_revision_value(record[column]) for column in value_columns}
    return rows

def revision_number(value: str) -> float | None:
    if value == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None

def revision_value_transitions(old_df: pd.DataFrame, new_df: pd.DataFrame, key_columns: list[str]) -> dict[str, Any]:
    keys = list(key_columns)
    missing_old = [column for column in keys if column not in old_df.columns]
    missing_new = [column for column in keys if column not in new_df.columns]
    if missing_old or missing_new:
        return {"issue": "missing_key_columns", "columns": {}}
    if old_df.duplicated(keys).any() or new_df.duplicated(keys).any():
        return {"issue": "duplicate_key_rows", "columns": {}}
    value_columns = sorted((set(old_df.columns) | set(new_df.columns)) - set(keys))
    old_rows = keyed_revision_rows(old_df, keys, value_columns)
    new_rows = keyed_revision_rows(new_df, keys, value_columns)
    columns: dict[str, dict[str, Any]] = {}
    for key in sorted(set(old_rows) & set(new_rows)):
        for column in value_columns:
            old_value = old_rows[key].get(column, "")
            new_value = new_rows[key].get(column, "")
            if old_value == new_value:
                continue
            old_number = revision_number(old_value)
            new_number = revision_number(new_value)
            transition = ""
            magnitude: float | None = None
            if old_value and not new_value and old_number is not None:
                transition = "numeric_to_blank"
                magnitude = abs(old_number)
            elif not old_value and new_value and new_number is not None:
                transition = "blank_to_numeric"
                magnitude = abs(new_number)
            if not transition or magnitude is None:
                continue
            stats = columns.setdefault(column, {}).setdefault(transition, {"count": 0, "sum_abs_value": 0.0, "max_abs_value": 0.0, "abs_values": [], "samples": []})
            stats["count"] += 1
            stats["sum_abs_value"] += magnitude
            stats["max_abs_value"] = max(float(stats["max_abs_value"]), magnitude)
            stats["abs_values"].append(magnitude)
            if len(stats["samples"]) < 8:
                stats["samples"].append({"key": list(key), "old": old_value, "new": new_value})
    return {"issue": "", "columns": columns}

def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = int(round((len(ordered) - 1) * pct))
    return float(ordered[index])

def merge_numeric_deltas(summary: dict[str, Any], numeric: dict[str, Any]) -> None:
    if numeric.get("issue"):
        issue = str(numeric["issue"])
        summary["numeric_delta_issues"][issue] = summary["numeric_delta_issues"].get(issue, 0) + 1
        return
    for column, stats in numeric.get("columns", {}).items():
        aggregate = summary["numeric_deltas"].setdefault(column, {
            "changed_cells": 0,
            "sum_abs_delta": 0.0,
            "sum_signed_delta": 0.0,
            "max_abs_delta": 0.0,
            "abs_deltas": [],
            "samples": [],
        })
        aggregate["changed_cells"] += int(stats["changed_cells"])
        aggregate["sum_abs_delta"] += float(stats["sum_abs_delta"])
        aggregate["sum_signed_delta"] += float(stats["sum_signed_delta"])
        aggregate["max_abs_delta"] = max(float(aggregate["max_abs_delta"]), float(stats["max_abs_delta"]))
        aggregate["abs_deltas"].extend(stats.get("abs_deltas", []))
        if len(aggregate["samples"]) < 8:
            aggregate["samples"].extend(stats.get("samples", [])[: 8 - len(aggregate["samples"])])

def merge_value_transitions(summary: dict[str, Any], transitions: dict[str, Any]) -> None:
    if transitions.get("issue"):
        issue = str(transitions["issue"])
        summary["value_transition_issues"][issue] = summary["value_transition_issues"].get(issue, 0) + 1
        return
    for column, by_transition in transitions.get("columns", {}).items():
        column_stats = summary["value_transitions"].setdefault(column, {})
        for transition_name, stats in by_transition.items():
            aggregate = column_stats.setdefault(transition_name, {"count": 0, "sum_abs_value": 0.0, "max_abs_value": 0.0, "abs_values": [], "samples": []})
            aggregate["count"] += int(stats["count"])
            aggregate["sum_abs_value"] += float(stats["sum_abs_value"])
            aggregate["max_abs_value"] = max(float(aggregate["max_abs_value"]), float(stats["max_abs_value"]))
            aggregate["abs_values"].extend(stats.get("abs_values", []))
            if len(aggregate["samples"]) < 8:
                aggregate["samples"].extend(stats.get("samples", [])[: 8 - len(aggregate["samples"])])

def finalize_numeric_deltas(numeric_deltas: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    finalized: dict[str, dict[str, Any]] = {}
    for column, stats in sorted(numeric_deltas.items()):
        changed_cells = int(stats["changed_cells"])
        abs_deltas = list(stats.get("abs_deltas", []))
        finalized[column] = {
            "changed_cells": changed_cells,
            "mean_abs_delta": float(stats["sum_abs_delta"] / changed_cells) if changed_cells else 0.0,
            "mean_signed_delta": float(stats["sum_signed_delta"] / changed_cells) if changed_cells else 0.0,
            "max_abs_delta": float(stats["max_abs_delta"]),
            "p50_abs_delta": percentile(abs_deltas, 0.50),
            "p95_abs_delta": percentile(abs_deltas, 0.95),
            "samples": stats.get("samples", [])[:8],
        }
    return finalized

def finalize_value_transitions(value_transitions: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    finalized: dict[str, dict[str, Any]] = {}
    for column, by_transition in sorted(value_transitions.items()):
        finalized[column] = {}
        for transition_name, stats in sorted(by_transition.items()):
            count = int(stats["count"])
            abs_values = list(stats.get("abs_values", []))
            finalized[column][transition_name] = {
                "count": count,
                "mean_abs_value": float(stats["sum_abs_value"] / count) if count else 0.0,
                "max_abs_value": float(stats["max_abs_value"]),
                "p50_abs_value": percentile(abs_values, 0.50),
                "p95_abs_value": percentile(abs_values, 0.95),
                "samples": stats.get("samples", [])[:8],
            }
    return finalized

def make_revision_history_summary(dataset: str, group: str) -> dict[str, Any]:
    return {
        "dataset": dataset,
        "group": group,
        "planned_partitions": 0,
        "queried_partitions": 0,
        "compared_partitions": 0,
        "stable_partitions": 0,
        "revision_partitions": 0,
        "structural_issue_partitions": 0,
        "missing_local_partitions": 0,
        "remote_zero_partitions": 0,
        "remote_empty_local_missing_partitions": 0,
        "errors": 0,
        "old_rows": 0,
        "new_rows": 0,
        "changed_keys": 0,
        "added_keys": 0,
        "removed_keys": 0,
        "changed_columns": {},
        "numeric_deltas": {},
        "numeric_delta_issues": {},
        "value_transitions": {},
        "value_transition_issues": {},
        "revision_samples": [],
        "missing_local_sample": [],
        "remote_zero_sample": [],
        "error_sample": [],
    }

def record_revision_history_check(
    *,
    client: TuShareClient,
    raw_dir: Path,
    events_output: Path,
    summary: dict[str, Any],
    by_year: dict[str, dict[str, Any]],
    dataset: str,
    group: str,
    trade_date: str,
    path: Path,
    params: dict[str, Any],
    fields: str,
    key_columns: list[str],
    page_limit: int,
    zero_rows_ok: bool,
    augment_kind: str = "",
    spec: Any = None,
) -> None:
    dataset_summary = summary.setdefault(dataset, make_revision_history_summary(dataset, group))
    year_summary = by_year.setdefault(trade_date[:4], {"planned_partitions": 0, "revision_partitions": 0, "missing_local_partitions": 0, "remote_zero_partitions": 0, "errors": 0})
    dataset_summary["planned_partitions"] += 1
    year_summary["planned_partitions"] += 1
    try:
        result, pages = query_paged(client, dataset, params, fields, page_limit)
        new_df = frame(result)
        if augment_kind == "event":
            new_df = augment_event_frame(new_df, spec)
        elif augment_kind == "board":
            new_df = augment_board_frame(new_df, spec, params)
    except Exception as exc:  # pragma: no cover - runtime API failures are summarized
        dataset_summary["errors"] += 1
        year_summary["errors"] += 1
        if len(dataset_summary["error_sample"]) < 10:
            dataset_summary["error_sample"].append({"trade_date": trade_date, "path": str(path), "params": params, "error": str(exc)})
        return

    dataset_summary["queried_partitions"] += 1
    if new_df.empty and not zero_rows_ok:
        dataset_summary["remote_zero_partitions"] += 1
        year_summary["remote_zero_partitions"] += 1
        if len(dataset_summary["remote_zero_sample"]) < 10:
            dataset_summary["remote_zero_sample"].append({"trade_date": trade_date, "path": str(path), "params": params})
        return

    if not path.exists():
        if new_df.empty:
            dataset_summary["remote_empty_local_missing_partitions"] += 1
            return
        dataset_summary["missing_local_partitions"] += 1
        year_summary["missing_local_partitions"] += 1
        if len(dataset_summary["missing_local_sample"]) < 10:
            dataset_summary["missing_local_sample"].append({"trade_date": trade_date, "path": str(path), "params": params, "remote_rows": int(len(new_df))})
        return

    old_df = pd.read_parquet(path)
    dataset_summary["compared_partitions"] += 1
    dataset_summary["old_rows"] += int(len(old_df))
    dataset_summary["new_rows"] += int(len(new_df))
    event = build_revision_event(
        dataset=dataset,
        partition=path.with_suffix("").name if path.parent.name == dataset else f"trade_date={trade_date}",
        path=path,
        old_df=old_df,
        new_df=new_df,
        key_columns=key_columns,
        source="history_sample_probe",
    )
    if not event:
        dataset_summary["stable_partitions"] += 1
        return

    event["group"] = group
    event["params"] = params
    event["pages"] = pages
    append_jsonl(events_output, event)
    if event.get("comparison_issue"):
        dataset_summary["structural_issue_partitions"] += 1
        if len(dataset_summary["revision_samples"]) < 8:
            dataset_summary["revision_samples"].append({
                "trade_date": trade_date,
                "path": str(path),
                "params": params,
                "comparison_issue": event.get("comparison_issue"),
                "duplicate_key_rows_old": event.get("duplicate_key_rows_old", 0),
                "duplicate_key_rows_new": event.get("duplicate_key_rows_new", 0),
                "missing_key_columns_old": event.get("missing_key_columns_old", []),
                "missing_key_columns_new": event.get("missing_key_columns_new", []),
            })
        return
    dataset_summary["revision_partitions"] += 1
    year_summary["revision_partitions"] += 1
    for key in ("changed_keys", "added_keys", "removed_keys"):
        dataset_summary[key] += int(event.get(key, 0))
    for column, count in event.get("changed_columns", {}).items():
        dataset_summary["changed_columns"][column] = dataset_summary["changed_columns"].get(column, 0) + int(count)
    merge_numeric_deltas(dataset_summary, revision_numeric_deltas(old_df, new_df, key_columns))
    merge_value_transitions(dataset_summary, revision_value_transitions(old_df, new_df, key_columns))
    if len(dataset_summary["revision_samples"]) < 8:
        dataset_summary["revision_samples"].append({
            "trade_date": trade_date,
            "path": str(path),
            "params": params,
            "changed_keys": event.get("changed_keys", 0),
            "added_keys": event.get("added_keys", 0),
            "removed_keys": event.get("removed_keys", 0),
            "changed_columns_sample": event.get("changed_columns_sample", [])[:3],
        })

def audit_revision_history_sample(args: argparse.Namespace) -> int:
    repo_root = Path.cwd().resolve()
    raw_dir = (repo_root / args.raw_dir).resolve()
    output = Path(args.output or REVISION_HISTORY_SAMPLE_STATUS_PATH)
    events_output = Path(args.events_output or REVISION_HISTORY_SAMPLE_EVENTS_PATH)
    if not output.is_absolute():
        output = repo_root / output
    if not events_output.is_absolute():
        events_output = repo_root / events_output
    output.parent.mkdir(parents=True, exist_ok=True)
    events_output.parent.mkdir(parents=True, exist_ok=True)
    if events_output.exists():
        events_output.unlink()

    seed = args.seed or args.end_date
    trade_dates = load_sse_open_dates(raw_dir, args.start_date, args.end_date)
    selected_by_year = select_revision_history_dates_by_year(trade_dates, args.sample_per_year, seed)
    sampled_dates = [trade_date for dates in selected_by_year.values() for trade_date in dates]
    client = TuShareClient(load_token(repo_root), args.min_interval_seconds, args.timeout_seconds)
    groups = set(args.groups or ["daily", "reference", "event_flow", "board_trading"])
    summaries: dict[str, dict[str, Any]] = {}
    by_year: dict[str, dict[str, Any]] = {}

    daily_datasets = list(args.daily_datasets or (DAILY_REQUIRED_DATASETS + DAILY_OPTIONAL_DATASETS))
    event_datasets = [
        dataset for dataset in list(args.event_datasets or EVENT_FLOW_DATASETS)
        if EVENT_FLOW_SPECS[dataset].strategy == "trade_date"
    ]
    board_datasets = [
        dataset for dataset in list(args.board_datasets or BOARD_TRADING_DEFAULT_DATASETS)
        if BOARD_TRADING_SPECS[dataset].strategy != "static_once"
    ]

    for trade_date in sampled_dates:
        if "daily" in groups:
            for dataset in daily_datasets:
                spec = DAILY_SPECS[dataset]
                if trade_date < max(args.start_date, spec.start_date):
                    continue
                record_revision_history_check(
                    client=client,
                    raw_dir=raw_dir,
                    events_output=events_output,
                    summary=summaries,
                    by_year=by_year,
                    dataset=dataset,
                    group="daily",
                    trade_date=trade_date,
                    path=raw_dir / spec.api_name / f"trade_date={trade_date}.parquet",
                    params={"trade_date": trade_date},
                    fields=spec.fields,
                    key_columns=list(spec.key_columns),
                    page_limit=args.page_limit or TRADE_DATE_PAGE_LIMIT,
                    zero_rows_ok=spec.zero_rows_ok,
                )
        if "reference" in groups and args.include_bak_basic and trade_date >= max(args.start_date, BAK_BASIC_SPEC.start_date):
            record_revision_history_check(
                client=client,
                raw_dir=raw_dir,
                events_output=events_output,
                summary=summaries,
                by_year=by_year,
                dataset="bak_basic",
                group="reference",
                trade_date=trade_date,
                path=raw_dir / "bak_basic" / f"trade_date={trade_date}.parquet",
                params={"trade_date": trade_date},
                fields=BAK_BASIC_SPEC.fields,
                key_columns=list(BAK_BASIC_SPEC.key_columns),
                page_limit=args.page_limit or TRADE_DATE_PAGE_LIMIT,
                zero_rows_ok=BAK_BASIC_SPEC.zero_rows_ok,
            )
        if "event_flow" in groups:
            for dataset in event_datasets:
                spec = EVENT_FLOW_SPECS[dataset]
                if trade_date < max(args.start_date, spec.start_date):
                    continue
                record_revision_history_check(
                    client=client,
                    raw_dir=raw_dir,
                    events_output=events_output,
                    summary=summaries,
                    by_year=by_year,
                    dataset=dataset,
                    group="event_flow",
                    trade_date=trade_date,
                    path=raw_dir / spec.api_name / f"trade_date={trade_date}.parquet",
                    params={"trade_date": trade_date},
                    fields=spec.fields,
                    key_columns=list(spec.key_columns),
                    page_limit=event_page_limit(spec, args.page_limit),
                    zero_rows_ok=spec.zero_rows_ok,
                    augment_kind="event",
                    spec=spec,
                )
        if "board_trading" in groups:
            board_args = argparse.Namespace(
                kpl_tag=args.kpl_tag,
                ths_limit_type=args.ths_limit_type,
                ths_hot_market=args.ths_hot_market,
                dc_hot_market=args.dc_hot_market,
                dc_hot_type=args.dc_hot_type,
                hot_is_new=args.hot_is_new,
            )
            for dataset in board_datasets:
                spec = BOARD_TRADING_SPECS[dataset]
                if trade_date < max(args.start_date, spec.start_date):
                    continue
                page_limit = board_page_limit(spec, args.page_limit)
                tasks: list[tuple[Path, dict[str, Any]]] = []
                if spec.strategy == "trade_date":
                    tasks.append((raw_dir / spec.api_name / f"trade_date={trade_date}.parquet", {"trade_date": trade_date}))
                elif spec.strategy == "trade_date_by_tag":
                    tasks.extend(
                        (
                            raw_dir / spec.api_name / f"tag={safe_partition_value(tag)}" / f"trade_date={trade_date}.parquet",
                            {"trade_date": trade_date, "tag": tag},
                        )
                        for tag in selected_board_kpl_tags(board_args)
                    )
                elif spec.strategy == "trade_date_by_limit_type":
                    tasks.extend(
                        (
                            raw_dir / spec.api_name / f"limit_type={safe_partition_value(limit_type)}" / f"trade_date={trade_date}.parquet",
                            {"trade_date": trade_date, "limit_type": limit_type},
                        )
                        for limit_type in selected_board_ths_limit_types(board_args)
                    )
                elif spec.strategy == "trade_date_by_market":
                    tasks.extend(
                        (
                            raw_dir / spec.api_name / f"market={safe_partition_value(market)}" / f"is_new={is_new}" / f"trade_date={trade_date}.parquet",
                            {"trade_date": trade_date, "market": market, "is_new": is_new},
                        )
                        for market in selected_board_ths_hot_markets(board_args)
                        for is_new in selected_board_hot_is_new(board_args)
                    )
                elif spec.strategy == "trade_date_by_market_hot_type":
                    tasks.extend(
                        (
                            raw_dir / spec.api_name / f"market={safe_partition_value(market)}" / f"hot_type={safe_partition_value(hot_type)}" / f"is_new={is_new}" / f"trade_date={trade_date}.parquet",
                            {"trade_date": trade_date, "market": market, "hot_type": hot_type, "is_new": is_new},
                        )
                        for market in selected_board_dc_hot_markets(board_args)
                        for hot_type in selected_board_dc_hot_types(board_args)
                        for is_new in selected_board_hot_is_new(board_args)
                    )
                for path, params in tasks:
                    record_revision_history_check(
                        client=client,
                        raw_dir=raw_dir,
                        events_output=events_output,
                        summary=summaries,
                        by_year=by_year,
                        dataset=dataset,
                        group="board_trading",
                        trade_date=trade_date,
                        path=path,
                        params=params,
                        fields=spec.fields,
                        key_columns=list(spec.key_columns),
                        page_limit=page_limit,
                        zero_rows_ok=spec.zero_rows_ok,
                        augment_kind="board",
                        spec=spec,
                    )

    dataset_reports = []
    for dataset, details in sorted(summaries.items()):
        numeric = finalize_numeric_deltas(details["numeric_deltas"])
        transitions = finalize_value_transitions(details["value_transitions"])
        report = {key: value for key, value in details.items() if key not in {"numeric_deltas", "value_transitions"}}
        report["numeric_deltas"] = numeric
        report["value_transitions"] = transitions
        report["revision_rate"] = (
            float(details["revision_partitions"] / details["compared_partitions"])
            if details["compared_partitions"]
            else None
        )
        dataset_reports.append(report)
    most_changed = sorted(
        dataset_reports,
        key=lambda item: (item["revision_partitions"], item["changed_keys"] + item["added_keys"] + item["removed_keys"]),
        reverse=True,
    )
    stable = [
        item["dataset"]
        for item in dataset_reports
        if item["compared_partitions"]
        and item["revision_partitions"] == 0
        and item["structural_issue_partitions"] == 0
        and item["missing_local_partitions"] == 0
        and item["remote_zero_partitions"] == 0
    ]
    findings = []
    for item in dataset_reports:
        severity = (
            "error"
            if item["errors"] or item["remote_zero_partitions"]
            else "warning"
            if item["revision_partitions"]
            or item["structural_issue_partitions"]
            or item["missing_local_partitions"]
            else "info"
        )
        findings.append(
            {
                "severity": severity,
                "check": f"{item['dataset']}_revision_history",
                "message": f"{item['dataset']} stratified source-revision checks",
                "details": item,
            }
        )
    report = build_quality_report(
        report_type="revision_history_sample",
        scope={
            "data_root": str(raw_dir),
            "start_date": args.start_date,
            "end_date": args.end_date,
            "datasets": [item["dataset"] for item in dataset_reports],
            "groups": sorted(groups),
        },
        findings=findings,
        datasets=summarize_datasets(
            findings, (item["dataset"] for item in dataset_reports)
        ),
        metadata={
            "raw_mutation": "none",
            "sample_per_year": args.sample_per_year,
            "seed": seed,
            "sampled_trade_dates_by_year": selected_by_year,
            "sampled_trade_dates": sampled_dates,
            "events_output": str(events_output),
            "most_changed_interfaces": [
                {
                    "dataset": item["dataset"],
                    "group": item["group"],
                    "revision_partitions": item["revision_partitions"],
                    "revision_rate": item["revision_rate"],
                    "changed_keys": item["changed_keys"],
                    "added_keys": item["added_keys"],
                    "removed_keys": item["removed_keys"],
                    "changed_columns_top": sorted(
                        item["changed_columns"].items(),
                        key=lambda pair: pair[1],
                        reverse=True,
                    )[:8],
                }
                for item in most_changed[:12]
                if item["revision_partitions"]
                or item["changed_keys"]
                or item["added_keys"]
                or item["removed_keys"]
            ],
            "stable_interfaces": stable,
            "structural_issue_interfaces": [
                {
                    "dataset": item["dataset"],
                    "group": item["group"],
                    "structural_issue_partitions": item["structural_issue_partitions"],
                    "sample": item["revision_samples"][:3],
                }
                for item in dataset_reports
                if item["structural_issue_partitions"]
            ],
            "by_year": by_year,
            "caveats": [
                "This command checks active trade-date partitioned interfaces only. Macro, fundamental, text month/day source, and share_float union need their own month/period/code sampling plans.",
                "Required trade-date interfaces returning zero rows are counted as remote_zero instead of source revisions.",
                "Missing local partition with non-empty remote response is reported as a local gap, not a source revision.",
            ],
        },
    )
    status = report["status"]
    write_quality_report(output, report)
    print(f"revision history sample status={status} datasets={len(dataset_reports)} dates={len(sampled_dates)} output={output} events={events_output}")
    return 1 if status == "error" and args.fail_on_error else 0

def audit_revision_sentinel(args: argparse.Namespace) -> int:
    repo_root = Path.cwd().resolve()
    raw_dir = (repo_root / args.raw_dir).resolve()
    output = Path(args.output or REVISION_SUMMARY_PATH)
    if not output.is_absolute():
        output = repo_root / output
    ledger = core.resolve_revision_ledger(raw_dir, args.revision_ledger, repo_root=repo_root)

    client = TuShareClient(load_token(repo_root), args.min_interval_seconds, args.timeout_seconds)
    datasets = list(args.datasets or (DAILY_REQUIRED_DATASETS + DAILY_OPTIONAL_DATASETS))
    trade_dates = load_sse_open_dates(raw_dir, args.start_date, args.end_date)
    events: list[dict[str, Any]] = []
    dataset_reports: list[dict[str, Any]] = []
    page_limit = args.page_limit or TRADE_DATE_PAGE_LIMIT
    total_missing_local = 0
    total_remote_zero = 0
    total_errors = 0
    total_no_effective_checks = 0

    for dataset in datasets:
        spec = DAILY_SPECS[dataset]
        candidate_dates = [date_value for date_value in trade_dates if max(args.start_date, spec.start_date) <= date_value <= args.end_date]
        sample_dates = select_revision_sentinel_dates(candidate_dates, args.sample_size, f"{args.seed or args.end_date}:{dataset}")
        checked = 0
        missing_local: list[str] = []
        remote_zero: list[str] = []
        errors: list[dict[str, str]] = []
        dataset_events = 0
        for trade_date in sample_dates:
            path = raw_dir / spec.api_name / f"trade_date={trade_date}.parquet"
            if not path.exists():
                missing_local.append(trade_date)
                continue
            try:
                result, _pages = query_paged(client, spec.api_name, {"trade_date": trade_date}, spec.fields, page_limit)
            except Exception as exc:  # pragma: no cover - defensive runtime path
                errors.append({"trade_date": trade_date, "error": str(exc)})
                continue
            new_df = frame(result)
            if new_df.empty and not spec.zero_rows_ok:
                remote_zero.append(trade_date)
                continue
            checked += 1
            event = build_revision_event(
                dataset=spec.api_name,
                partition=f"trade_date={trade_date}",
                path=path,
                old_df=pd.read_parquet(path),
                new_df=new_df,
                key_columns=list(spec.key_columns),
                source="sentinel_probe",
            )
            if event:
                append_jsonl_unique(ledger, event, key="event_id")
                print("REVISION_ALERT " + json.dumps(event, ensure_ascii=False, sort_keys=True))
                events.append(event)
                dataset_events += 1
        no_effective_checks = int(bool(sample_dates) and checked == 0)
        total_missing_local += len(missing_local)
        total_remote_zero += len(remote_zero)
        total_errors += len(errors)
        total_no_effective_checks += no_effective_checks
        dataset_reports.append({
            "dataset": dataset,
            "candidate_dates": len(candidate_dates),
            "sampled_dates": len(sample_dates),
            "checked_dates": checked,
            "revision_events": dataset_events,
            "missing_local_dates": len(missing_local),
            "remote_zero_dates": len(remote_zero),
            "errors": len(errors),
            "no_effective_checks": no_effective_checks,
            "sample_dates": sample_dates[:20],
            "missing_local_sample": missing_local[:20],
            "remote_zero_sample": remote_zero[:20],
            "error_sample": errors[:10],
        })

    findings = []
    for item in dataset_reports:
        severity = (
            "error"
            if item["errors"] or item["remote_zero_dates"]
            else "warning"
            if item["revision_events"] or item["missing_local_dates"] or item["no_effective_checks"]
            else "info"
        )
        findings.append(
            {
                "severity": severity,
                "check": f"{item['dataset']}_revision_sentinel",
                "message": f"{item['dataset']} sampled source-revision checks",
                "details": item,
            }
        )
    report = build_quality_report(
        report_type="revision_sentinel",
        scope={
            "data_root": str(raw_dir),
            "start_date": args.start_date,
            "end_date": args.end_date,
            "datasets": datasets,
        },
        findings=findings,
        datasets=summarize_datasets(findings, datasets),
        metadata={
            "revision_ledger": str(ledger),
            "sample_size": args.sample_size,
            "seed": args.seed or args.end_date,
            "totals": {
                "revision_events": len(events),
                "missing_local_dates": total_missing_local,
                "remote_zero_dates": total_remote_zero,
                "api_errors": total_errors,
                "datasets_without_effective_checks": total_no_effective_checks,
            },
            "revision_event_sample": events[:20],
        },
    )
    status = report["status"]
    has_error = status == "error"
    write_quality_report(output, report)
    print(f"revision sentinel status={status} events={len(events)} errors={total_errors} remote_zero={total_remote_zero} no_effective_checks={total_no_effective_checks} output={output} ledger={ledger}")
    return 1 if has_error or (events and args.fail_on_revision) else 0

def audit_intraday_by_date(args: argparse.Namespace) -> int:
    repo_root = Path.cwd().resolve()
    raw_dir = (repo_root / args.raw_dir).resolve()
    output = (repo_root / args.output).resolve() if args.output else (repo_root / INTRADAY_MINUTES_STATUS_PATH).resolve()
    trade_dates = load_sse_open_dates(raw_dir, args.start_date, args.end_date)
    dataset_dir = raw_dir / args.output_dataset
    findings: list[dict[str, Any]] = []

    def add(severity: str, check: str, message: str, details: dict[str, Any] | None = None) -> None:
        findings.append({"severity": severity, "check": check, "message": message, "details": details or {}})

    paths = {trade_date: stk_mins_by_date_path(raw_dir, args.output_dataset, trade_date) for trade_date in trade_dates}
    missing = [str(path) for trade_date, path in paths.items() if not path.exists()]
    files = [path for path in paths.values() if path.exists()]
    meta_files = [path.with_suffix(path.suffix + ".meta.json") for path in files]
    missing_meta = [str(path) for path in meta_files if not path.exists()]
    all_meta = sorted(dataset_dir.glob("*.parquet.meta.json"))
    expected_meta = {str(path) for path in meta_files}
    orphan_meta = [str(path) for path in all_meta if str(path) not in expected_meta and not Path(str(path).removesuffix(".meta.json")).exists()]
    row_counts = {path.name: parquet_rows(path) for path in files}
    zero_files = [str(path) for path in files if row_counts.get(path.name, 0) == 0]
    schema_missing: list[str] = []
    for path in files:
        schema = set(pq.ParquetFile(path).schema_arrow.names)
        if not set(STK_MINS_REQUIRED_COLUMNS).issubset(schema):
            schema_missing.append(str(path))
    add("error" if missing or missing_meta or orphan_meta or zero_files or schema_missing else "info", f"{args.output_dataset}_inventory", "date-organized intraday minute inventory", {
        "dataset_dir": str(dataset_dir),
        "expected_trade_dates": len(trade_dates),
        "files": len(files),
        "missing_files": len(missing),
        "missing_meta": len(missing_meta),
        "orphan_meta": len(orphan_meta),
        "schema_missing_required_columns": len(schema_missing),
        "rows": int(sum(row_counts.values())),
        "zero_row_files": len(zero_files),
        "missing_sample": missing[:20],
        "missing_meta_sample": missing_meta[:20],
        "orphan_meta_sample": orphan_meta[:20],
        "zero_file_sample": zero_files[:20],
        "schema_missing_sample": schema_missing[:10],
    })

    deep_paths = files if args.full_scan else files[: max(0, args.sample_limit)]
    bad_days: list[dict[str, Any]] = []
    for path in deep_paths:
        trade_date = path.stem.split("=", 1)[-1]
        df = pd.read_parquet(path)
        expected_codes = intraday_expected_codes_for_day(raw_dir, args, trade_date)
        ok, details = validate_stk_mins_by_date_frame(
            df,
            trade_date,
            expected_codes=expected_codes,
            min_rows=args.min_rows_per_day,
            allow_missing_codes=args.allow_missing_codes,
        )
        if not ok:
            bad_days.append(details)
    add("warning" if bad_days else "info", f"{args.output_dataset}_deep_checks", "date-organized intraday minute row/key/PIT/time checks", {
        "full_scan": bool(args.full_scan),
        "files_checked": len(deep_paths),
        "bad_days": len(bad_days),
        "bad_day_sample": bad_days[:10],
        "expected_codes_source": args.expected_codes_source,
        "min_rows_per_day": args.min_rows_per_day,
        "allow_missing_codes": args.allow_missing_codes,
    })

    report = build_quality_report(
        report_type="intraday_minutes",
        scope={
            "data_root": str(raw_dir),
            "start_date": args.start_date,
            "end_date": args.end_date,
            "datasets": [args.output_dataset],
            "expected_codes_source": args.expected_codes_source,
        },
        findings=findings,
        datasets=summarize_dataset_status(findings),
        metadata={
            "unit_rules": {
                args.output_dataset: {
                    "source": f"derived from {STK_MINS_DATASET} or daily incremental {STK_MINS_API_NAME}",
                    "partition": "one full-market parquet per trade_date",
                    "vol": "shares",
                    "amount": "CNY",
                    "available_at": "bar close time from trade_time",
                }
            },
            "conclusions": [
                "The date-organized minute store is the preferred research/live-update layout for PIT daily replay.",
                "The stock-year source store remains the historical download and traceability layer.",
                "Rows must still be filtered by available_at <= decision_time inside PIT snapshot construction.",
            ],
        },
    )
    counts = report["finding_counts"]
    status = report["status"]
    write_quality_report(output, report)
    print(f"intraday by-date audit status={status} errors={counts['error']} warnings={counts['warning']} output={output}")
    return 1 if counts["error"] else 0

def auction_alignment_bucket(ts_code: object) -> str:
    text = str(ts_code or "").strip().upper()
    if text.endswith(".SZ") and text.startswith("00"):
        return "sz_main_00"
    if text.endswith(".SZ") and text.startswith("30"):
        return "sz_gem_30"
    if text.endswith(".SH") and text.startswith("60"):
        return "sh_main_60"
    if text.endswith(".SH") and text.startswith("68"):
        return "sh_star_68"
    if text.endswith(".BJ"):
        return "bj"
    return "other"

def numeric_ratio(numerator: pd.Series, denominator: pd.Series) -> pd.Series:
    left = pd.to_numeric(numerator, errors="coerce")
    right = pd.to_numeric(denominator, errors="coerce")
    return left.where(right.ne(0)) / right.where(right.ne(0))

def grouped_ratio_stats(df: pd.DataFrame, columns: list[str]) -> dict[str, dict[str, float | int | None]]:
    if df.empty:
        return {}
    result: dict[str, dict[str, float | int | None]] = {}
    for bucket, group in df.groupby("bucket", dropna=False):
        item: dict[str, float | int | None] = {"rows": int(len(group))}
        for column in columns:
            values = pd.to_numeric(group[column], errors="coerce").dropna()
            if values.empty:
                item[column] = None
                continue
            item[f"{column}_median"] = float(values.median())
            item[f"{column}_p10"] = float(values.quantile(0.1))
            item[f"{column}_p90"] = float(values.quantile(0.9))
        result[str(bucket)] = item
    return result

def factor_for_auction_bucket(bucket: str) -> float:
    return {"sz_main_00": 0.76, "sz_gem_30": 0.58}.get(bucket, 1.0)

def audit_auction_alignment(args: argparse.Namespace) -> int:
    repo_root = Path.cwd().resolve()
    raw_dir = (repo_root / args.raw_dir).resolve()
    output = (repo_root / (args.output or "results/data_quality/process/auction_alignment_status.json")).resolve()
    trade_dates = load_sse_open_dates(raw_dir, args.start_date, args.end_date)
    if args.max_trade_dates > 0:
        trade_dates = trade_dates[-args.max_trade_dates :]
    findings: list[dict[str, Any]] = []

    def add(severity: str, check: str, message: str, details: dict[str, Any] | None = None) -> None:
        findings.append({"severity": severity, "check": check, "message": message, "details": details or {}})

    client = TuShareClient(load_token(repo_root), args.min_interval_seconds, args.timeout_seconds)
    auction_day_stats: list[dict[str, Any]] = []
    daily_day_stats: list[dict[str, Any]] = []
    missing_minute_dates: list[str] = []
    missing_daily_dates: list[str] = []

    for trade_date in trade_dates:
        minute_path = stk_mins_by_date_path(raw_dir, args.output_dataset, trade_date)
        if not minute_path.exists():
            missing_minute_dates.append(trade_date)
            continue
        minutes = pd.read_parquet(minute_path, columns=["ts_code", "trade_time", "vol", "amount"])
        hhmm = minutes["trade_time"].astype(str).str.slice(11, 16)
        open_bar = minutes[hhmm.eq("09:30")].copy()
        auction = api_frame(client, "stk_auction", {"trade_date": trade_date}, "ts_code,trade_date,vol,amount")
        merged = open_bar.merge(auction, on="ts_code", suffixes=("_minute", "_auction"))
        merged["bucket"] = merged["ts_code"].map(auction_alignment_bucket)
        merged["vol_ratio"] = numeric_ratio(merged["vol_minute"], merged["vol_auction"])
        merged["amount_ratio"] = numeric_ratio(merged["amount_minute"], merged["amount_auction"])
        merged["factor"] = merged["bucket"].map(factor_for_auction_bucket)
        merged["vol_ratio_after_factor"] = merged["vol_ratio"] * merged["factor"]
        merged["amount_ratio_after_factor"] = merged["amount_ratio"] * merged["factor"]
        auction_day_stats.append({
            "trade_date": trade_date,
            "minute_open_rows": int(len(open_bar)),
            "stk_auction_rows": int(len(auction)),
            "matched_rows": int(len(merged)),
            "bucket_stats": grouped_ratio_stats(merged, ["vol_ratio", "amount_ratio", "vol_ratio_after_factor", "amount_ratio_after_factor"]),
        })

        daily_path = raw_dir / "daily" / f"trade_date={trade_date}.parquet"
        if not daily_path.exists():
            missing_daily_dates.append(trade_date)
            continue
        daily = pd.read_parquet(daily_path, columns=["ts_code", "vol", "amount"])
        minute_sum = minutes.groupby("ts_code", as_index=False)[["vol", "amount"]].sum()
        daily_merge = minute_sum.merge(daily, on="ts_code", suffixes=("_minute_sum", "_daily"))
        daily_merge["bucket"] = daily_merge["ts_code"].map(auction_alignment_bucket)
        daily_merge["minute_to_daily_vol_ratio"] = numeric_ratio(daily_merge["vol_minute_sum"], daily_merge["vol_daily"])
        daily_merge["minute_to_daily_amount_ratio"] = numeric_ratio(daily_merge["amount_minute_sum"], daily_merge["amount_daily"])
        daily_day_stats.append({
            "trade_date": trade_date,
            "matched_rows": int(len(daily_merge)),
            "bucket_stats": grouped_ratio_stats(daily_merge, ["minute_to_daily_vol_ratio", "minute_to_daily_amount_ratio"]),
        })

    add("error" if missing_minute_dates else "info", "auction_alignment_inputs", "input partition availability for auction alignment", {
        "trade_dates_checked": trade_dates,
        "missing_minute_dates": missing_minute_dates,
        "missing_daily_dates": missing_daily_dates,
    })
    add("warning" if not auction_day_stats else "info", "minute_0930_vs_stk_auction", "09:30 minute bar against live stk_auction ratios by market bucket", {
        "days": auction_day_stats,
        "expected_pattern": "SH/BJ buckets should stay near 1.0; historical SZ 09:30 minute bars are adjusted in PIT snapshot construction before comparing with live stk_auction.",
        "correction_factors": {"sz_main_00": 0.76, "sz_gem_30": 0.58, "others": 1.0},
    })
    add("warning" if not daily_day_stats else "info", "minute_sum_vs_daily_units", "full-day minute sums against daily unit ratios", {
        "days": daily_day_stats,
        "expected_ratios": {"vol": "minute shares / daily hands ~= 100", "amount": "minute CNY / daily thousand CNY ~= 1000"},
    })

    report = build_quality_report(
        report_type="auction_alignment",
        scope={
            "data_root": str(raw_dir),
            "start_date": args.start_date,
            "end_date": args.end_date,
            "datasets": [args.output_dataset, "stk_auction", "daily"],
            "trade_dates_checked": trade_dates,
            "output_dataset": args.output_dataset,
        },
        findings=findings,
        datasets=summarize_dataset_status(findings),
        metadata={
            "conclusions": [
                "Raw minute files are not modified by this audit.",
                "Historical 09:30 minute auction bars should be corrected in the Environment snapshot layer when they are used as a proxy for live stk_auction.",
                "Full-day minute sums should still align with daily units after the documented share/hand and CNY/thousand-CNY conversions.",
            ]
        },
    )
    counts = report["finding_counts"]
    status = report["status"]
    write_quality_report(output, report)
    print(f"auction alignment audit status={status} errors={counts['error']} warnings={counts['warning']} output={output}")
    return 1 if counts["error"] else 0

def expected_stk_mins_paths(raw_dir: Path, args: argparse.Namespace) -> set[Path]:
    universe_args = argparse.Namespace(codes=getattr(args, "intraday_codes", None), max_codes=getattr(args, "intraday_max_codes", None))
    universe = load_minute_universe(raw_dir, universe_args)
    expected: set[Path] = set()
    for _, row in universe.iterrows():
        ts_code = str(row["ts_code"])
        for year, _, _ in active_year_windows(row, args.intraday_start_date, args.intraday_end_date):
            expected.add(raw_dir / STK_MINS_DATASET / f"ts_code={safe_partition_value(ts_code)}" / f"year={year}.parquet")
    return expected

def audit_stk_mins_completeness(raw_dir: Path, args: argparse.Namespace, add) -> None:
    all_files = sorted((raw_dir / STK_MINS_DATASET).glob("ts_code=*/year=*.parquet"))
    all_meta_files = sorted((raw_dir / STK_MINS_DATASET).glob("ts_code=*/year=*.parquet.meta.json"))
    expected = expected_stk_mins_paths(raw_dir, args)
    scoped = bool(getattr(args, "intraday_codes", None) or getattr(args, "intraday_max_codes", None))
    if scoped:
        expected_parent_dirs = {path.parent.resolve() for path in expected}
        files = [path for path in all_files if path.parent.resolve() in expected_parent_dirs]
        meta_files = [path for path in all_meta_files if path.parent.resolve() in expected_parent_dirs]
    else:
        files = all_files
        meta_files = all_meta_files
    file_set = {path.resolve() for path in files}
    expected_set = {path.resolve() for path in expected}
    missing = sorted(str(path) for path in expected_set - file_set)
    extra = sorted(str(path) for path in file_set - expected_set)
    parquet_meta = {str(path.with_suffix(path.suffix + ".meta.json")) for path in files}
    meta_set = {str(path) for path in meta_files}
    missing_meta = sorted(parquet_meta - meta_set)
    orphan_meta = sorted(meta_set - parquet_meta)
    row_counts = {str(path): parquet_rows(path) for path in files}
    zero_files = sorted(path for path, rows in row_counts.items() if rows == 0)
    exact_limit_files = sorted(
        path
        for path, rows in row_counts.items()
        if rows in {STK_MINS_PAGE_LIMIT, 5000, 6000, 7000, 8000, 10000} and not has_pagination_probe(Path(path))
    )
    schema_missing: list[str] = []
    for path in files:
        schema = set(pq.ParquetFile(path).schema_arrow.names)
        if not set(STK_MINS_REQUIRED_COLUMNS).issubset(schema):
            schema_missing.append(str(path))
    has_error = bool(missing or missing_meta or orphan_meta or schema_missing)
    add("error" if has_error else "warning" if zero_files or exact_limit_files else "info", f"{STK_MINS_DATASET}_partitions", "stk_mins 1min stock-year partition inventory", {
        "files": len(files),
        "expected_files": len(expected),
        "missing_expected_files": len(missing),
        "extra_files": len(extra),
        "meta_files": len(meta_files),
        "rows": int(sum(row_counts.values())),
        "zero_row_partitions": len(zero_files),
        "exact_common_limit_row_count_partitions": len(exact_limit_files),
        "missing_meta": len(missing_meta),
        "orphan_meta": len(orphan_meta),
        "schema_missing_required_columns": len(schema_missing),
        "missing_sample": missing[:20],
        "extra_sample": extra[:20],
        "zero_sample": zero_files[:20],
        "exact_limit_sample": exact_limit_files[:20],
        "schema_missing_sample": schema_missing[:10],
        "unit_rules": {"vol": "shares", "amount": "CNY", "official_page_limit": STK_MINS_PAGE_LIMIT, "doc_ref": INTEGRATED_DOC_REFS[STK_MINS_DATASET]},
    })
    audit_stk_mins_sample(files, args.sample_limit, add)

def audit_stk_mins_sample(files: list[Path], sample_limit: int, add) -> None:
    sample = [path for path in files if parquet_rows(path) > 0][: max(0, sample_limit)]
    duplicate_key_rows = 0
    unparseable_trade_time_rows = 0
    unparseable_available_at_rows = 0
    missing_0930_files: list[str] = []
    missing_1500_files: list[str] = []
    code_partition_mismatch = 0
    year_partition_mismatch = 0
    for path in sample:
        df = pd.read_parquet(path, columns=["ts_code", "trade_time", "trade_date", "available_at"])
        duplicate_key_rows += int(df.duplicated(["ts_code", "trade_time"]).sum())
        trade_time = df["trade_time"].astype(str).str.strip()
        parsed_trade = pd.to_datetime(trade_time, errors="coerce")
        unparseable_trade_time_rows += int(parsed_trade.isna().sum())
        available = df["available_at"].astype(str).str.strip()
        parsed_available = pd.to_datetime(available[available.ne("")], errors="coerce", utc=True, format="mixed")
        unparseable_available_at_rows += int(parsed_available.isna().sum())
        times = set(trade_time.str.extract(r"(\d{2}:\d{2})", expand=False).dropna().tolist())
        if "09:30" not in times:
            missing_0930_files.append(str(path))
        if "15:00" not in times:
            missing_1500_files.append(str(path))
        expected_code = path.parent.name.split("=", 1)[1]
        expected_year = path.stem.split("=", 1)[1]
        code_partition_mismatch += int((df["ts_code"].astype(str) != expected_code).sum())
        year_partition_mismatch += int((df["trade_date"].astype(str).str[:4] != expected_year).sum())
    has_issue = any([duplicate_key_rows, unparseable_trade_time_rows, unparseable_available_at_rows, missing_0930_files, missing_1500_files, code_partition_mismatch, year_partition_mismatch])
    add("warning" if has_issue else "info", f"{STK_MINS_DATASET}_sample_keys", "stk_mins 1min sampled key/PIT/auction-bar checks", {
        "files_sampled": len(sample),
        "duplicate_key_rows": duplicate_key_rows,
        "unparseable_trade_time_rows": unparseable_trade_time_rows,
        "unparseable_available_at_rows": unparseable_available_at_rows,
        "missing_0930_files": len(missing_0930_files),
        "missing_1500_files": len(missing_1500_files),
        "code_partition_mismatch_rows": code_partition_mismatch,
        "year_partition_mismatch_rows": year_partition_mismatch,
        "missing_0930_sample": missing_0930_files[:10],
        "missing_1500_sample": missing_1500_files[:10],
    })

def fundamental_partition_value(path: Path, prefix: str) -> str:
    stem = path.stem
    expected = f"{prefix}="
    return stem[len(expected):] if stem.startswith(expected) else ""

def audit_fundamental_dataset(raw_dir: Path, spec: FundamentalDataset, expected: set[str], prefix: str, add) -> None:
    files = sorted((raw_dir / spec.api_name).glob(f"{prefix}=*.parquet"))
    file_values = {fundamental_partition_value(path, prefix): path for path in files}
    row_counts = {value: parquet_rows(path) for value, path in file_values.items()}
    zero_values = sorted(value for value, count in row_counts.items() if count == 0)
    nonzero_values = sorted(value for value, count in row_counts.items() if count > 0)
    missing = sorted(expected - set(file_values))
    extra = sorted(set(file_values) - expected)
    exact_limit_values = sorted(value for value, count in row_counts.items() if count in {5000, 6000, 7000, 8000, 10000})
    details = {
        "strategy": spec.strategy,
        "partition_prefix": prefix,
        "files": len(files),
        "rows": int(sum(row_counts.values())),
        "expected_files": len(expected),
        "missing_expected_files": len(missing),
        "extra_files": len(extra),
        "zero_row_partitions": len(zero_values),
        "nonzero_partitions": len(nonzero_values),
        "first_partition": min(file_values) if file_values else None,
        "last_partition": max(file_values) if file_values else None,
        "first_nonzero_partition": nonzero_values[0] if nonzero_values else None,
        "last_nonzero_partition": nonzero_values[-1] if nonzero_values else None,
        "missing_sample": missing[:20],
        "extra_sample": extra[:20],
        "zero_sample": zero_values[:20],
        "exact_common_limit_row_count_partitions": len(exact_limit_values),
        "exact_limit_sample": exact_limit_values[:20],
    }
    add("error" if not files or missing else "warning" if exact_limit_values else "info", f"{spec.api_name}_partitions", f"{spec.api_name} fundamental partition checks", details)
    key_details = audit_fundamental_keys(files, spec)
    has_key_error = key_details["blank_ts_code"] or key_details["missing_key_column_files"]
    has_key_warning = key_details["duplicate_key_rows"] or key_details["duplicate_full_rows"]
    severity = "error" if has_key_error else "warning" if has_key_warning else "info"
    add(severity, f"{spec.api_name}_keys", f"{spec.api_name} fundamental key checks", key_details)

def audit_fundamental_keys(files: list[Path], spec: FundamentalDataset) -> dict[str, Any]:
    duplicate_key_rows = 0
    duplicate_full_rows = 0
    blank_ts_code = 0
    blank_date_fields: dict[str, int] = {}
    missing_key_column_files: list[str] = []
    date_fields = [field for field in ("ann_date", "f_ann_date", "end_date", "actual_date", "pre_date") if field in spec.key_columns]
    for path in files:
        rows = parquet_rows(path)
        if rows == 0:
            continue
        parquet = pq.ParquetFile(path)
        schema = parquet.schema_arrow.names
        missing = [col for col in spec.key_columns if col not in schema]
        if missing:
            missing_key_column_files.append(str(path))
            continue
        key_df = pd.read_parquet(path, columns=list(spec.key_columns))
        if "ts_code" in key_df:
            ts_codes = key_df["ts_code"].astype(str).str.strip()
            blank_ts_code += int(key_df["ts_code"].isna().sum() + (ts_codes == "").sum())
        for field in date_fields:
            values = key_df[field].astype(str).str.strip()
            blank_date_fields[field] = blank_date_fields.get(field, 0) + int(key_df[field].isna().sum() + (values == "").sum())
        duplicate_key_rows += int(key_df.duplicated(list(spec.key_columns)).sum())
        if rows <= 20000:
            full_df = pd.read_parquet(path)
            duplicate_full_rows += int(full_df.duplicated().sum())
    return {
        "files_checked": len(files),
        "key_columns": list(spec.key_columns),
        "blank_ts_code": blank_ts_code,
        "blank_date_fields": blank_date_fields,
        "duplicate_key_rows": duplicate_key_rows,
        "duplicate_full_rows": duplicate_full_rows,
        "missing_key_column_files": len(missing_key_column_files),
        "missing_key_column_sample": missing_key_column_files[:10],
    }

def blank_count(series: pd.Series) -> int:
    return int(series.isna().sum() + (series.astype(str).str.strip() == "").sum())

def audit_stock_basic(raw_dir: Path, add) -> pd.DataFrame:
    files = sorted((raw_dir / "stock_basic").glob("list_status=*.parquet"))
    df = read_many(files)
    details = {"files": len(files), "rows": len(df)}
    if df.empty:
        add("error", "stock_basic", "stock_basic is empty or missing", details)
        return df
    details.update({
        "unique_ts_code": int(df["ts_code"].nunique()),
        "duplicate_ts_code_rows": int(df.duplicated(["ts_code"]).sum()),
        "blank_required": {col: blank_count(df[col]) for col in ["ts_code", "symbol", "name", "list_status", "list_date"]},
        "status_counts": df["list_status"].value_counts(dropna=False).to_dict(),
    })
    has_error = details["duplicate_ts_code_rows"] or any(details["blank_required"].values())
    add("error" if has_error else "info", "stock_basic", "stock_basic key and required-field checks", details)
    return df

def audit_stock_company(raw_dir: Path, stock_basic: pd.DataFrame, add) -> None:
    df = read_many(sorted((raw_dir / "stock_company").glob("exchange=*.parquet")))
    if df.empty:
        add("warning", "stock_company", "stock_company is empty or missing")
        return
    details = {
        "rows": len(df), "unique_ts_code": int(df["ts_code"].nunique()),
        "duplicate_ts_code_rows": int(df.duplicated(["ts_code"]).sum()),
        "blank_ts_code": blank_count(df["ts_code"]), "blank_com_name": blank_count(df["com_name"]),
    }
    add("warning" if details["blank_com_name"] else "info", "stock_company", "stock_company key and name checks", details)
    if not stock_basic.empty:
        basic_codes = set(stock_basic["ts_code"].dropna().astype(str))
        company_codes = set(df["ts_code"].dropna().astype(str))
        add("warning", "stock_company_vs_stock_basic", "stock_company and stock_basic coverage differs", {
            "stock_basic_missing_in_company": len(basic_codes - company_codes),
            "stock_company_missing_in_basic": len(company_codes - basic_codes),
            "stock_basic_missing_sample": sorted(basic_codes - company_codes)[:20],
            "stock_company_missing_sample": sorted(company_codes - basic_codes)[:20],
        })

def audit_trade_cal(raw_dir: Path, add) -> set[str]:
    calendars: dict[str, pd.DataFrame] = {}
    for exchange in ("SSE", "SZSE", "BSE"):
        files = sorted((raw_dir / "trade_cal" / f"exchange={exchange}").glob("year=*.parquet"))
        df = read_many(files)
        calendars[exchange] = df
        add("warning" if exchange == "BSE" and df.empty else "info", f"trade_cal_{exchange}", f"{exchange} trade calendar checks", {
            "files": len(files), "rows": len(df),
            "open_days": int((df["is_open"].astype(str) == "1").sum()) if not df.empty else 0,
            "duplicate_cal_date_rows": int(df.duplicated(["cal_date"]).sum()) if not df.empty else 0,
        })
    sse_open = set(calendars["SSE"].loc[calendars["SSE"]["is_open"].astype(str) == "1", "cal_date"].astype(str)) if not calendars["SSE"].empty else set()
    szse_open = set(calendars["SZSE"].loc[calendars["SZSE"]["is_open"].astype(str) == "1", "cal_date"].astype(str)) if not calendars["SZSE"].empty else set()
    add("error" if sse_open != szse_open else "info", "trade_cal_sse_szse", "SSE/SZSE open-day alignment", {"sse_not_szse": len(sse_open - szse_open), "szse_not_sse": len(szse_open - sse_open)})
    return sse_open

def audit_bak_basic(raw_dir: Path, sse_open: set[str], end_date: str, add) -> None:
    files = sorted((raw_dir / "bak_basic").glob("trade_date=*.parquet"))
    rows = {path.stem.split("=", 1)[1]: parquet_rows(path) for path in files}
    expected = {d for d in sse_open if "20160101" <= d <= end_date}
    scoped_rows = {d: count for d, count in rows.items() if d <= end_date}
    missing_dates = sorted(expected - set(scoped_rows))
    extra_dates = sorted(set(scoped_rows) - expected)
    zero_dates = sorted(d for d in expected if scoped_rows.get(d, 0) == 0)
    nonzero_dates = sorted(d for d in expected if scoped_rows.get(d, 0) > 0)
    details = {
        "files": len(files), "rows": int(sum(rows.values())), "end_date": end_date,
        "missing_expected_files": len(missing_dates), "extra_files": len(extra_dates),
        "zero_row_partitions": len(zero_dates), "first_nonzero_date": nonzero_dates[0] if nonzero_dates else None,
        "last_nonzero_date": nonzero_dates[-1] if nonzero_dates else None,
        "zero_after_first_nonzero": len([d for d in zero_dates if nonzero_dates and d > nonzero_dates[0]]),
        "missing_sample": missing_dates[:20], "extra_sample": extra_dates[:20], "zero_sample": zero_dates[:20],
    }
    severity = "error" if details["missing_expected_files"] else "warning" if zero_dates else "info"
    add(severity, "bak_basic_partitions", "bak_basic partition and source-empty checks", details)
    key_df = read_many(files, columns=["trade_date", "ts_code"])
    add("error" if key_df.duplicated(["trade_date", "ts_code"]).any() else "info", "bak_basic_keys", "bak_basic key checks", {
        "blank_trade_date": blank_count(key_df["trade_date"]), "blank_ts_code": blank_count(key_df["ts_code"]),
        "duplicate_trade_date_ts_code_rows": int(key_df.duplicated(["trade_date", "ts_code"]).sum()),
    })

def audit_namechange(raw_dir: Path, add) -> None:
    path = raw_dir / "namechange" / "namechange.parquet"
    if not path.exists():
        add("error", "namechange", "namechange final table is missing")
        return
    df = pd.read_parquet(path)
    details = {
        "rows": len(df), "unique_ts_code": int(df["ts_code"].nunique()),
        "blank_ts_code": blank_count(df["ts_code"]), "duplicate_full_rows": int(df.duplicated().sum()),
        "start_date_min": str(df["start_date"].dropna().astype(str).replace("", pd.NA).dropna().min()),
        "start_date_max": str(df["start_date"].dropna().astype(str).replace("", pd.NA).dropna().max()),
    }
    has_error = details["blank_ts_code"] or details["duplicate_full_rows"]
    add("error" if has_error else "info", "namechange", "final namechange table checks", details)

def audit_index_classify(raw_dir: Path, add) -> pd.DataFrame:
    path = raw_dir / "index_classify" / "src=SW2021.parquet"
    if not path.exists():
        add("error", "index_classify", "index_classify is missing")
        return pd.DataFrame()
    df = pd.read_parquet(path)
    details = {"rows": len(df), "level_counts": df["level"].value_counts(dropna=False).to_dict(), "duplicate_index_code_rows": int(df.duplicated(["index_code"]).sum())}
    add("error" if details["duplicate_index_code_rows"] else "info", "index_classify", "SW2021 industry classification checks", details)
    return df

def audit_index_member_all(raw_dir: Path, classify: pd.DataFrame, stock_basic: pd.DataFrame, add) -> None:
    files = sorted((raw_dir / "index_member_all").glob("l1_code=*.parquet"))
    df = read_many(files)
    l1_codes = set(classify.loc[classify["level"].astype(str) == "L1", "index_code"].astype(str)) if not classify.empty else set()
    file_codes = set(path.stem.split("=", 1)[1] for path in files)
    missing_in_basic: set[str] = set()
    if not df.empty and not stock_basic.empty:
        missing_in_basic = set(df["ts_code"].dropna().astype(str)) - set(stock_basic["ts_code"].dropna().astype(str))
    details = {
        "files": len(files), "rows": len(df), "missing_l1_partitions": len(l1_codes - file_codes),
        "extra_l1_partitions": len(file_codes - l1_codes), "blank_ts_code": blank_count(df["ts_code"]) if not df.empty else 0,
        "duplicate_full_rows": int(df.duplicated().sum()) if not df.empty else 0,
        "member_codes_missing_in_stock_basic": len(missing_in_basic), "missing_code_sample": sorted(missing_in_basic)[:20],
    }
    severity = "error" if details["missing_l1_partitions"] or details["blank_ts_code"] else "warning" if missing_in_basic else "info"
    add(severity, "index_member_all", "SW2021 member table checks", details)

def status_severity(counts: dict[str, int]) -> str:
    return "error" if counts.get("error", 0) else "warning" if counts.get("warning", 0) else "ok"

def json_count_dict(series: pd.Series) -> dict[str, int]:
    return {str(key): int(value) for key, value in series.value_counts(dropna=False).items()}

def first_parquet_schema(raw_dir: Path, dataset: str) -> list[str]:
    files = sorted((raw_dir / dataset).rglob("*.parquet"))
    if not files:
        return []
    return list(pq.ParquetFile(files[0]).schema_arrow.names)

def read_partition_codes(raw_dir: Path, dataset: str, trade_date: str) -> set[str]:
    path = raw_dir / dataset / f"trade_date={trade_date}.parquet"
    if not path.exists() or parquet_rows(path) == 0:
        return set()
    schema = pq.ParquetFile(path).schema_arrow.names
    if "ts_code" not in schema:
        return set()
    df = pd.read_parquet(path, columns=["ts_code"])
    return set(df["ts_code"].dropna().astype(str).str.strip()) - {""}

def code_type_counts(codes: set[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for code in codes:
        symbol, _, suffix = code.partition(".")
        if suffix == "BJ":
            key = "BJ"
        elif symbol.startswith(("900", "200")):
            key = "B_share_like"
        elif symbol.startswith(("510", "511", "512", "513", "515", "516", "517", "518", "519", "520", "560", "561", "562", "563", "588", "159", "160", "161", "162", "163", "164", "165", "166", "167", "168", "169")):
            key = "fund_or_etf_like"
        elif suffix in {"SH", "SZ"}:
            key = "A_share_like"
        else:
            key = suffix or "unknown"
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))

def new_coverage_acc(left_name: str, right_name: str) -> dict[str, Any]:
    return {
        "left_dataset": left_name,
        "right_dataset": right_name,
        "dates_checked": 0,
        "left_only_rows": 0,
        "right_only_rows": 0,
        "dates_with_left_only": 0,
        "dates_with_right_only": 0,
        "max_left_only_on_date": {"trade_date": None, "rows": 0},
        "max_right_only_on_date": {"trade_date": None, "rows": 0},
        "samples": [],
        "_left_only_codes": set(),
        "_right_only_codes": set(),
    }

def update_coverage_acc(acc: dict[str, Any], trade_date: str, left_codes: set[str], right_codes: set[str], sample_limit: int, extra: dict[str, Any] | None = None) -> None:
    left_only = left_codes - right_codes
    right_only = right_codes - left_codes
    acc["dates_checked"] += 1
    acc["left_only_rows"] += len(left_only)
    acc["right_only_rows"] += len(right_only)
    acc["_left_only_codes"].update(left_only)
    acc["_right_only_codes"].update(right_only)
    if left_only:
        acc["dates_with_left_only"] += 1
        if len(left_only) > acc["max_left_only_on_date"]["rows"]:
            acc["max_left_only_on_date"] = {"trade_date": trade_date, "rows": len(left_only)}
    if right_only:
        acc["dates_with_right_only"] += 1
        if len(right_only) > acc["max_right_only_on_date"]["rows"]:
            acc["max_right_only_on_date"] = {"trade_date": trade_date, "rows": len(right_only)}
    if (left_only or right_only) and len(acc["samples"]) < sample_limit:
        sample = {
            "trade_date": trade_date,
            "left_only_count": len(left_only),
            "right_only_count": len(right_only),
            "left_only_sample": sorted(left_only)[:10],
            "right_only_sample": sorted(right_only)[:10],
        }
        if extra:
            sample.update(extra)
        acc["samples"].append(sample)

def finish_coverage_acc(acc: dict[str, Any]) -> dict[str, Any]:
    left_only_codes = acc.pop("_left_only_codes")
    right_only_codes = acc.pop("_right_only_codes")
    acc["unique_left_only_codes"] = len(left_only_codes)
    acc["unique_right_only_codes"] = len(right_only_codes)
    acc["unique_left_only_sample"] = sorted(left_only_codes)[:20]
    acc["unique_right_only_sample"] = sorted(right_only_codes)[:20]
    acc["unique_left_only_code_types"] = code_type_counts(left_only_codes)
    acc["unique_right_only_code_types"] = code_type_counts(right_only_codes)
    return acc

def audit_daily_cross_coverage(raw_dir: Path, trade_dates: set[str], args: argparse.Namespace, add) -> dict[str, set[str]]:
    dates = sorted(d for d in trade_dates if args.start_date <= d <= args.end_date)
    acc_daily_basic = new_coverage_acc("daily", "daily_basic")
    acc_adj = new_coverage_acc("adj_factor", "daily")
    acc_stk_limit = new_coverage_acc("stk_limit", "daily")
    all_codes = {"daily": set(), "daily_basic": set(), "adj_factor": set(), "stk_limit": set()}
    for trade_date in dates:
        daily_codes = read_partition_codes(raw_dir, "daily", trade_date)
        daily_basic_codes = read_partition_codes(raw_dir, "daily_basic", trade_date)
        adj_codes = read_partition_codes(raw_dir, "adj_factor", trade_date)
        stk_limit_codes = read_partition_codes(raw_dir, "stk_limit", trade_date)
        all_codes["daily"].update(daily_codes)
        all_codes["daily_basic"].update(daily_basic_codes)
        all_codes["adj_factor"].update(adj_codes)
        all_codes["stk_limit"].update(stk_limit_codes)
        update_coverage_acc(acc_daily_basic, trade_date, daily_codes, daily_basic_codes, args.sample_limit)
        adj_only = adj_codes - daily_codes
        extra = None
        if adj_only and len(acc_adj["samples"]) < args.sample_limit:
            suspend_codes = read_partition_codes(raw_dir, "suspend_d", trade_date)
            extra = {"left_only_in_suspend_d": len(adj_only & suspend_codes), "suspend_d_codes": len(suspend_codes)}
        update_coverage_acc(acc_adj, trade_date, adj_codes, daily_codes, args.sample_limit, extra)
        update_coverage_acc(acc_stk_limit, trade_date, stk_limit_codes, daily_codes, args.sample_limit)
    daily_basic_details = finish_coverage_acc(acc_daily_basic)
    adj_details = finish_coverage_acc(acc_adj)
    stk_limit_details = finish_coverage_acc(acc_stk_limit)
    add("warning" if daily_basic_details["left_only_rows"] or daily_basic_details["right_only_rows"] else "info", "daily_vs_daily_basic_coverage", "daily and daily_basic same-day code coverage", daily_basic_details)
    add("warning" if adj_details["right_only_rows"] else "info", "adj_factor_vs_daily_coverage", "adj_factor can validly exceed daily because factors may exist for non-trading/suspended names", adj_details)
    add("warning" if stk_limit_details["right_only_rows"] else "info", "stk_limit_vs_daily_coverage", "stk_limit covers A/B shares and funds, so rows can exceed daily", stk_limit_details)
    return all_codes

def audit_unit_schema(raw_dir: Path, add) -> None:
    daily_schema = first_parquet_schema(raw_dir, "daily")
    daily_basic_schema = first_parquet_schema(raw_dir, "daily_basic")
    bak_basic_schema = first_parquet_schema(raw_dir, "bak_basic")
    add("info", "unit_schema_reference", "local schemas and official unit references", {
        "daily_has_vol_amount": {"vol": "vol" in daily_schema, "amount": "amount" in daily_schema},
        "daily_basic_has_vol_amount": {"vol": "vol" in daily_basic_schema, "amount": "amount" in daily_basic_schema},
        "bak_basic_has_vol_amount": {"vol": "vol" in bak_basic_schema, "amount": "amount" in bak_basic_schema},
        "unit_rules": {
            "daily.vol": "hands; multiply by 100 for shares",
            "daily.amount": "thousand CNY",
            "daily_basic.total_share/float_share/free_share": "10k shares",
            "daily_basic.total_mv/circ_mv": "10k CNY",
            "bak_basic.float_share/total_share": "100m shares per official document; no volume or amount fields",
            "bak_daily.vol": "inferred directly comparable to daily.vol from sample probes",
            "bak_daily.amount": "inferred 10k CNY from sample probes; multiply by 10 to compare with daily.amount in thousand CNY",
            "bak_daily.total_share/float_share": "100m shares per official document; multiply by 10000 to compare with daily_basic share fields",
            "bak_daily.total_mv/float_mv": "inferred 100m CNY from sample probes; multiply by 10000 to compare with daily_basic market value fields",
        },
        "doc_refs": SEMANTIC_DOC_REFS,
    })
    if "vol" not in bak_basic_schema and "amount" not in bak_basic_schema:
        add("info", "bak_basic_no_turnover_fields", "bak_basic does not contain volume or amount and must not be used for turnover-unit alignment")
    else:
        add("warning", "bak_basic_no_turnover_fields", "unexpected bak_basic volume/amount fields found; inspect schema before using it for unit alignment", {"schema": bak_basic_schema})

def api_frame(client: TuShareClient, api_name: str, params: dict[str, Any], fields: str) -> pd.DataFrame:
    return frame(client.query(api_name, params, fields))

def numeric_value(df: pd.DataFrame, column: str) -> float | None:
    if df.empty or column not in df or pd.isna(df[column].iloc[0]):
        return None
    return float(df[column].iloc[0])

def audit_stock_universe_semantics(raw_dir: Path, all_codes: dict[str, set[str]], add) -> None:
    stock_basic = read_many(sorted((raw_dir / "stock_basic").glob("list_status=*.parquet")), columns=["ts_code", "name", "market", "exchange", "list_status", "list_date", "delist_date"])
    stock_company = read_many(sorted((raw_dir / "stock_company").glob("exchange=*.parquet")), columns=["ts_code", "exchange", "com_name"])
    index_member = read_many(sorted((raw_dir / "index_member_all").glob("l1_code=*.parquet")), columns=["ts_code", "l1_code", "l1_name", "in_date", "out_date"])
    basic_codes = set(stock_basic["ts_code"].dropna().astype(str)) if not stock_basic.empty else set()
    company_codes = set(stock_company["ts_code"].dropna().astype(str)) if not stock_company.empty else set()
    member_codes = set(index_member["ts_code"].dropna().astype(str)) if not index_member.empty else set()
    daily_codes = all_codes.get("daily", set())
    listed_codes = set(stock_basic.loc[stock_basic["list_status"].astype(str) == "L", "ts_code"].astype(str)) if not stock_basic.empty else set()
    delisted_codes = set(stock_basic.loc[stock_basic["list_status"].astype(str) == "D", "ts_code"].astype(str)) if not stock_basic.empty else set()
    bse_basic_codes = set(stock_basic.loc[stock_basic["exchange"].astype(str) == "BSE", "ts_code"].astype(str)) if not stock_basic.empty else set()
    bj_daily_codes = {code for code in daily_codes if code.endswith(".BJ")}
    details = {
        "stock_basic_rows": int(len(stock_basic)),
        "stock_basic_unique_codes": len(basic_codes),
        "stock_basic_status_counts": json_count_dict(stock_basic["list_status"]) if not stock_basic.empty else {},
        "stock_basic_exchange_counts": json_count_dict(stock_basic["exchange"]) if not stock_basic.empty else {},
        "stock_basic_market_counts": json_count_dict(stock_basic["market"]) if not stock_basic.empty else {},
        "stock_company_rows": int(len(stock_company)),
        "stock_company_missing_stock_basic_codes": len(basic_codes - company_codes),
        "stock_company_extra_codes_vs_stock_basic": len(company_codes - basic_codes),
        "stock_company_missing_sample": sorted(basic_codes - company_codes)[:20],
        "stock_company_extra_sample": sorted(company_codes - basic_codes)[:20],
        "daily_unique_codes": len(daily_codes),
        "daily_codes_missing_in_stock_basic": len(daily_codes - basic_codes),
        "daily_missing_in_stock_basic_sample": sorted(daily_codes - basic_codes)[:20],
        "stock_basic_codes_missing_in_daily": len(basic_codes - daily_codes),
        "listed_stock_basic_codes_missing_in_daily": len(listed_codes - daily_codes),
        "listed_missing_in_daily_sample": sorted(listed_codes - daily_codes)[:20],
        "delisted_stock_basic_codes": len(delisted_codes),
        "delisted_codes_with_daily": len(delisted_codes & daily_codes),
        "delisted_with_daily_sample": sorted(delisted_codes & daily_codes)[:20],
        "bse_stock_basic_codes": len(bse_basic_codes),
        "bj_daily_unique_codes": len(bj_daily_codes),
        "bj_daily_codes_missing_in_stock_basic": len(bj_daily_codes - basic_codes),
        "bse_stock_basic_codes_missing_in_daily": len(bse_basic_codes - daily_codes),
        "bj_daily_missing_in_stock_basic_sample": sorted(bj_daily_codes - basic_codes)[:20],
        "industry_member_codes": len(member_codes),
        "industry_member_codes_missing_in_stock_basic": len(member_codes - basic_codes),
        "industry_member_missing_sample": sorted(member_codes - basic_codes)[:20],
    }
    has_diff = any(details[key] for key in ("stock_company_missing_stock_basic_codes", "stock_company_extra_codes_vs_stock_basic", "daily_codes_missing_in_stock_basic", "listed_stock_basic_codes_missing_in_daily", "bj_daily_codes_missing_in_stock_basic", "industry_member_codes_missing_in_stock_basic"))
    add("warning" if has_diff else "info", "stock_universe_semantics", "North-board, delisted, stock_company, daily, and industry-member coverage differences", details)

def audit_pit_availability(raw_dir: Path, add) -> None:
    dataset_columns = {dataset: first_parquet_schema(raw_dir, dataset) for dataset in ("daily", "daily_basic", "adj_factor", "stk_limit", "bak_basic", "namechange")}
    row_available_at = {dataset: "available_at" in columns for dataset, columns in dataset_columns.items()}
    sidecar_has_fetched_at: dict[str, bool] = {}
    for dataset in dataset_columns:
        meta_files = sorted((raw_dir / dataset).rglob("*.meta.json"))
        if not meta_files:
            sidecar_has_fetched_at[dataset] = False
            continue
        meta = json.loads(meta_files[0].read_text(encoding="utf-8"))
        sidecar_has_fetched_at[dataset] = bool(meta.get("fetched_at"))
    add("warning", "pit_available_at", "raw files carry fetch metadata but no row-level available_at; snapshot construction must enforce PIT rules", {
        "row_level_available_at_present": row_available_at,
        "sample_sidecar_has_fetched_at": sidecar_has_fetched_at,
        "rules": {
            "daily": "officially loaded after market close around 15:00-16:00; do not use same-day values for 09:25 decisions",
            "daily_basic": "officially updated around 15:00-17:00; do not use same-day values for 09:25 decisions",
            "stk_limit": "officially around 08:40 and covers A/B shares and funds; keep explicit available_at in PIT layer",
            "adj_factor": "officially around 09:15-09:20, but raw trade_date alone is not enough for PIT-safe joins",
            "namechange": "use ann_date or a derived available_at; start_date can be a future effective date",
        },
    })

def json_scalar(value: Any) -> Any:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(value, "item"):
        return value.item()
    return value

def records_for_json(df: pd.DataFrame, columns: list[str] | None = None, limit: int = 8) -> list[dict[str, Any]]:
    if df.empty:
        return []
    view = df[[col for col in (columns or list(df.columns)) if col in df.columns]].head(limit)
    records: list[dict[str, Any]] = []
    for row in view.to_dict("records"):
        records.append({str(key): json_scalar(value) for key, value in row.items()})
    return records

def existing_partition_values(raw_dir: Path, datasets: list[str], prefix: str) -> dict[str, list[str]]:
    values: dict[str, list[str]] = {}
    for dataset in datasets:
        values[dataset] = sorted(fundamental_partition_value(path, prefix) for path in (raw_dir / dataset).glob(f"{prefix}=*.parquet"))
    return values

def integrated_unit_rules() -> dict[str, str]:
    return {
        "daily.vol": "hands; multiply by 100 for shares",
        "daily.amount": "thousand CNY",
        "bak_daily.vol": "same empirical scale as daily.vol in API case studies",
        "bak_daily.amount": "10k CNY inferred by API case studies; multiply by 10 before comparing with daily.amount",
        "daily_basic.total_share/float_share/free_share": "10k shares",
        "daily_basic.total_mv/circ_mv": "10k CNY",
        "bak_daily.total_share/float_share": "100m shares; multiply by 10000 before comparing with daily_basic share fields",
        "bak_daily.total_mv/float_mv": "100m CNY inferred by case studies; multiply by 10000 before comparing with daily_basic market value fields",
        "bak_basic.float_share/total_share": "100m shares; bak_basic has no volume or amount fields",
        "bak_basic.total_assets/liquid_assets/fixed_assets": "100m CNY style snapshot fields; use only as a supplemental coarse snapshot",
        "fundamental.statement_amount_fields": "income_vip/balancesheet_vip/cashflow_vip amount fields are CNY/yuan unless the field is explicitly per-share or ratio",
        "fundamental.forecast_profit_fields": "forecast_vip net_profit_min/net_profit_max are 10k CNY and must not be mixed directly with statement net profit in CNY",
        "fundamental.dividend_cash_fields": "dividend cash_div/cash_div_tax are per-share cash dividend fields; base_share is 10k shares when present",
        "fundamental.fina_indicator_vip": "mixed table: per-share fields, ratios/percent fields, and CNY amount fields; handle by field family",
    }

def audit_integrated_filesystem(raw_dir: Path, datasets: list[str], add) -> None:
    parquet_files: list[Path] = []
    meta_files: list[Path] = []
    tmp_files: list[Path] = []
    missing_dirs: list[str] = []
    per_dataset: dict[str, dict[str, int]] = {}
    for dataset in datasets:
        dataset_dir = raw_dir / dataset
        if not dataset_dir.exists():
            missing_dirs.append(dataset)
            per_dataset[dataset] = {"parquet_files": 0, "meta_files": 0, "tmp_files": 0}
            continue
        ds_parquet = sorted(dataset_dir.rglob("*.parquet"))
        ds_meta = sorted(dataset_dir.rglob("*.meta.json"))
        ds_tmp = sorted(path for path in dataset_dir.rglob("*") if ".tmp" in path.name)
        parquet_files.extend(ds_parquet)
        meta_files.extend(ds_meta)
        tmp_files.extend(ds_tmp)
        per_dataset[dataset] = {"parquet_files": len(ds_parquet), "meta_files": len(ds_meta), "tmp_files": len(ds_tmp)}
    parquet_meta_paths = {str(path.with_suffix(path.suffix + ".meta.json")) for path in parquet_files}
    meta_set = {str(path) for path in meta_files}
    missing_meta = sorted(parquet_meta_paths - meta_set)
    orphan_meta = sorted(meta_set - parquet_meta_paths)
    # Content-hash spot check: a sidecar carrying parquet_sha256 must match the
    # parquet bytes (a torn write_parquet pair surfaces here). Sidecars written
    # before the field existed are counted as legacy, not failed; the newest
    # pairs per dataset are verified so fresh writes are always covered.
    hash_mismatches: list[str] = []
    hashes_checked = 0
    legacy_meta_without_hash = 0
    verifiable: list[tuple[float, Path, str]] = []
    for path in parquet_files:
        expected_sha = str(parquet_meta(path).get("parquet_sha256") or "")
        if not expected_sha:
            legacy_meta_without_hash += 1
            continue
        try:
            verifiable.append((path.stat().st_mtime, path, expected_sha))
        except OSError:
            continue
    for _mtime, path, expected_sha in sorted(verifiable, reverse=True)[:5]:
        hashes_checked += 1
        if file_sha256(path) != expected_sha:
            hash_mismatches.append(str(path))
    add("error" if missing_dirs or missing_meta or orphan_meta or tmp_files or hash_mismatches else "info", "integrated_filesystem", "base research parquet sidecar inventory", {
        "datasets": datasets,
        "per_dataset": per_dataset,
        "parquet_files": len(parquet_files),
        "meta_files": len(meta_files),
        "missing_meta": len(missing_meta),
        "orphan_meta": len(orphan_meta),
        "tmp_files": len(tmp_files),
        "missing_dataset_dirs": missing_dirs,
        "missing_meta_sample": missing_meta[:10],
        "orphan_meta_sample": orphan_meta[:10],
        "parquet_sha256_checked": hashes_checked,
        "parquet_sha256_mismatches": hash_mismatches[:10],
        "legacy_meta_without_parquet_sha256": legacy_meta_without_hash,
    })

def audit_fundamental_completeness(raw_dir: Path, args: argparse.Namespace, add) -> None:
    stock_codes = load_stock_codes(raw_dir)
    period_datasets = [name for name, spec in FUNDAMENTAL_SPECS.items() if spec.strategy == "period"]
    ann_month_datasets = [name for name, spec in FUNDAMENTAL_SPECS.items() if spec.strategy == "ann_month"]
    period_values = existing_partition_values(raw_dir, period_datasets, "period")
    ann_values = existing_partition_values(raw_dir, ann_month_datasets, "ann_month")
    all_periods = sorted({value for values in period_values.values() for value in values if value})
    all_months = sorted({value for values in ann_values.values() for value in values if value})
    period_end = args.fundamental_end_date or (all_periods[-1] if all_periods else args.fundamental_start_date)
    ann_end = args.fundamental_end_date or (month_end_from_yyyymm(all_months[-1]) if all_months else args.fundamental_start_date)
    periods = set(quarter_periods(args.fundamental_start_date, period_end))
    months = {month for _, _, month in month_windows(args.fundamental_start_date, ann_end)}
    add("info", "fundamental_expected_ranges", "fundamental expected partition ranges inferred from local data or explicit args", {
        "fundamental_start_date": args.fundamental_start_date,
        "fundamental_end_date_arg": args.fundamental_end_date,
        "period_datasets": period_datasets,
        "ann_month_datasets": ann_month_datasets,
        "period_expected_end": period_end,
        "ann_month_expected_end": ann_end,
        "expected_period_partitions": len(periods),
        "expected_ann_month_partitions": len(months),
        "expected_ts_code_partitions": len(stock_codes),
    })
    for dataset in selected_integrated_fundamental_datasets(args):
        spec = FUNDAMENTAL_SPECS[dataset]
        if spec.strategy == "period":
            audit_fundamental_dataset(raw_dir, spec, periods, "period", add)
        elif spec.strategy == "ann_month":
            audit_fundamental_dataset(raw_dir, spec, months, "ann_month", add)
        else:
            audit_fundamental_dataset(raw_dir, spec, set(stock_codes), "ts_code", add)

def audit_fundamental_unit_and_pit_semantics(raw_dir: Path, add) -> None:
    schemas = {dataset: first_parquet_schema(raw_dir, dataset) for dataset in FUNDAMENTAL_DATASETS}
    tables_with_f_ann_date = sorted(dataset for dataset, columns in schemas.items() if "f_ann_date" in columns)
    tables_without_f_ann_date = sorted(dataset for dataset, columns in schemas.items() if columns and "f_ann_date" not in columns)
    add("warning", "fundamental_unit_and_pit_semantics", "fundamental units and PIT fields are mixed by interface and field family", {
        "tables_with_f_ann_date": tables_with_f_ann_date,
        "tables_without_f_ann_date": tables_without_f_ann_date,
        "unit_rules": integrated_unit_rules(),
        "pit_rules": {
            "income_vip/balancesheet_vip/cashflow_vip": "use f_ann_date first, then ann_date; choose the latest visible version at decision time",
            "fina_indicator_vip": "no f_ann_date in local schema; use ann_date conservatively and expect duplicate same-period rows",
            "forecast_vip": "PIT visibility = each version's own ann_date (first_ann_date is a series attribute, never an availability floor); keep update_flag/type; it is an event table, not a final statement table",
            "express_vip": "use ann_date; it is a preliminary result table and may differ from later statements",
            "dividend": "use imp_ann_date, ex_date, record_date, and pay_date according to field meaning; ann_date can be blank",
            "disclosure_date": "calendar/planned disclosure table; do not treat it as a fundamental value",
        },
        "doc_refs": {key: INTEGRATED_DOC_REFS[key] for key in sorted(INTEGRATED_DOC_REFS)},
    })

def selected_integrated_text_datasets(args: argparse.Namespace) -> list[str]:
    datasets = list(getattr(args, "text_datasets", None) or TEXT_DEFAULT_DATASETS)
    invalid = sorted(set(datasets) - set(TEXT_SPECS))
    if invalid:
        raise RuntimeError(f"unknown text datasets: {invalid}")
    return datasets

from dataclasses import dataclass as _dataclass


@_dataclass(frozen=True)
class DomainAuditProfile:
    """Shared partition-inventory + key/PIT auditor knobs for one raw domain.

    The four domain auditors (text/macro/event/board) were hand-written copies
    of the same shape and had drifted (uneven pagination-probe exclusion,
    text-only empty early-return, event-only zero_rows_ok). The profile encodes
    exactly what genuinely differs; the emitted check names and details keys
    stay the per-domain status-JSON contract the nightly consumers read.
    """

    domain: str                    # check-name suffix: <api>_<domain>_partitions/_keys
    exact_limit_rows: frozenset    # row counts that look like a page-limit truncation
    exact_limit_key: str           # details key name for that count
    apply_pagination_probe: bool   # exclude partitions with a recorded pagination probe
    empty_error_mode: str          # "with_expected" | "always" | "early_return"
    include_strategy: bool
    key_columns: tuple
    key_extra_columns: tuple       # extra columns joined into the keys read
    keys_intersect_available: bool  # text: audit only key columns present in schema
    blank_mode: str                # "per_key_field" | "available_at_total" | "available_at_rows"
    pit_rules: object
    partitions_message: str
    keys_message: str
    partition_prefix: str | None = None  # event: strategy partition glob + ignored tracking
    zero_rows_ok: bool | None = None     # event: emitted and gates the zero-rows warning
    availability_required: bool = True   # board static_once: availability is not an error


def audit_domain_dataset(raw_dir: Path, spec, expected_paths: set[Path], add, profile: DomainAuditProfile) -> None:
    dataset_dir = raw_dir / spec.api_name
    ignored_parquet: list[str] = []
    ignored_meta: list[str] = []
    if profile.partition_prefix is not None:
        files = sorted(dataset_dir.rglob(f"{profile.partition_prefix}=*.parquet"))
        meta_files = sorted(dataset_dir.rglob(f"{profile.partition_prefix}=*.parquet.meta.json"))
        ignored_parquet = sorted(str(path.resolve()) for path in dataset_dir.rglob("*.parquet") if path not in files)
        ignored_meta = sorted(str(path.resolve()) for path in dataset_dir.rglob("*.meta.json") if path not in meta_files)
    else:
        files = sorted(dataset_dir.rglob("*.parquet"))
        meta_files = sorted(dataset_dir.rglob("*.meta.json"))
    file_set = {path.resolve() for path in files}
    expected_set = {path.resolve() for path in expected_paths}
    missing_expected = sorted(str(path) for path in expected_set - file_set)
    extra_files = sorted(str(path) for path in file_set - expected_set)
    if profile.empty_error_mode == "early_return" and not files:
        add("error", f"{spec.api_name}_{profile.domain}_partitions", f"{spec.api_name} {profile.domain} dataset is missing", {
            "strategy": spec.strategy,
            "expected_files": len(expected_set),
            "missing_expected_files": len(missing_expected),
            "missing_sample": missing_expected[:10],
        })
        return
    parquet_meta = {str(path.with_suffix(path.suffix + ".meta.json")) for path in files}
    meta_set = {str(path) for path in meta_files}
    missing_meta = sorted(parquet_meta - meta_set)
    orphan_meta = sorted(meta_set - parquet_meta)
    row_counts = {str(path): parquet_rows(path) for path in files}
    zero_files = [path for path, rows in row_counts.items() if rows == 0]
    exact_limit_files = [
        path
        for path, rows in row_counts.items()
        if rows in profile.exact_limit_rows
        and not (profile.apply_pagination_probe and has_pagination_probe(Path(path)))
    ]
    has_error = bool(missing_expected or missing_meta or orphan_meta)
    if profile.empty_error_mode == "always":
        has_error = has_error or not files
    elif profile.empty_error_mode == "with_expected":
        has_error = has_error or (not files and expected_set)
    has_warning = bool(exact_limit_files)
    if profile.zero_rows_ok is not None:
        has_warning = has_warning or bool(zero_files and not profile.zero_rows_ok) or bool(ignored_parquet or ignored_meta)
    details: dict[str, Any] = {}
    if profile.include_strategy:
        details["strategy"] = spec.strategy
    if profile.partition_prefix is not None:
        details["partition_prefix"] = profile.partition_prefix
    details.update({
        "files": len(files),
        "expected_files": len(expected_set),
        "missing_expected_files": len(missing_expected),
        "extra_files": len(extra_files),
    })
    if profile.partition_prefix is not None:
        details["ignored_non_strategy_parquet_files"] = len(ignored_parquet)
        details["ignored_non_strategy_meta_files"] = len(ignored_meta)
    details.update({
        "meta_files": len(meta_files),
        "rows": int(sum(row_counts.values())),
        "zero_row_partitions": len(zero_files),
    })
    if profile.zero_rows_ok is not None:
        details["zero_rows_ok"] = profile.zero_rows_ok
    details.update({
        "missing_meta": len(missing_meta),
        "orphan_meta": len(orphan_meta),
        profile.exact_limit_key: len(exact_limit_files),
        "missing_sample": missing_expected[:10],
        "extra_sample": extra_files[:10],
    })
    if profile.partition_prefix is not None:
        details["ignored_non_strategy_parquet_sample"] = ignored_parquet[:10]
        details["ignored_non_strategy_meta_sample"] = ignored_meta[:10]
    details.update({
        "zero_sample": zero_files[:10],
        "exact_limit_sample": exact_limit_files[:10],
    })
    add(
        "error" if has_error else "warning" if has_warning else "info",
        f"{spec.api_name}_{profile.domain}_partitions",
        f"{spec.api_name} {profile.partitions_message}",
        details,
    )
    key_details = audit_domain_keys(files, profile)
    if profile.blank_mode == "per_key_field":
        has_blank = any(int(value) for value in key_details["blank_key_fields"].values())
    elif profile.blank_mode == "available_at_total":
        has_blank = bool(key_details["blank_time_fields"])
    else:
        has_blank = bool(key_details["blank_available_at_rows"])
    availability_error = bool(key_details["missing_available_at_files"] or key_details["unparseable_available_at_rows"])
    has_key_error = bool(key_details["missing_key_column_files"]) or (profile.availability_required and availability_error)
    has_key_warning = bool(key_details["duplicate_key_rows"]) or (
        has_blank if profile.availability_required or profile.blank_mode != "available_at_rows" else False
    )
    add(
        "error" if has_key_error else "warning" if has_key_warning else "info",
        f"{spec.api_name}_{profile.domain}_keys",
        f"{spec.api_name} {profile.keys_message}",
        key_details,
    )


def audit_domain_keys(files: list[Path], profile: DomainAuditProfile) -> dict[str, Any]:
    duplicate_key_rows = 0
    blank_key_fields: dict[str, int] = {}
    blank_available_at = 0
    unparseable_available_at_rows = 0
    missing_key_column_files: list[str] = []
    missing_available_at_files: list[str] = []
    for path in files:
        if parquet_rows(path) == 0:
            continue
        schema = pq.ParquetFile(path).schema_arrow.names
        missing = [col for col in profile.key_columns if col not in schema]
        if missing:
            missing_key_column_files.append(str(path))
            continue
        keys = (
            [col for col in profile.key_columns if col in schema]
            if profile.keys_intersect_available
            else list(profile.key_columns)
        )
        columns = list(dict.fromkeys(keys + list(profile.key_extra_columns)))
        df = pd.read_parquet(path, columns=[col for col in columns if col and col in schema])
        if keys:
            duplicate_key_rows += int(df.duplicated(keys).sum())
            if profile.blank_mode == "per_key_field":
                for col in keys:
                    blank_key_fields[col] = blank_key_fields.get(col, 0) + blank_count(df[col])
        if "available_at" not in df.columns:
            missing_available_at_files.append(str(path))
            continue
        available = df["available_at"].astype(str).str.strip()
        if profile.blank_mode == "available_at_total":
            blank_available_at += blank_count(df["available_at"])
        blank = available.eq("") | available.eq("nan") | available.eq("None")
        if profile.blank_mode == "available_at_rows":
            blank_available_at += int(blank.sum())
        nonblank = available[~blank]
        if not nonblank.empty:
            parsed = pd.to_datetime(nonblank, errors="coerce", utc=True, format="mixed")
            unparseable_available_at_rows += int(parsed.isna().sum())
    details: dict[str, Any] = {
        "files_checked": len(files),
        "key_columns": list(profile.key_columns),
        "duplicate_key_rows": duplicate_key_rows,
    }
    if profile.blank_mode == "per_key_field":
        details["blank_key_fields"] = blank_key_fields
    elif profile.blank_mode == "available_at_total":
        details["blank_time_fields"] = blank_available_at
    else:
        details["blank_available_at_rows"] = blank_available_at
    details.update({
        "unparseable_available_at_rows": unparseable_available_at_rows,
        "missing_key_column_files": len(missing_key_column_files),
        "missing_available_at_files": len(missing_available_at_files),
        "missing_key_column_sample": missing_key_column_files[:10],
        "missing_available_at_sample": missing_available_at_files[:10],
        "pit_rules": profile.pit_rules,
    })
    return details


def audit_text_dataset(raw_dir: Path, spec: TextDataset, expected_paths: set[Path], add) -> None:
    audit_domain_dataset(raw_dir, spec, expected_paths, add, DomainAuditProfile(
        domain="text",
        exact_limit_rows=frozenset({5000, 6000, 7000, 8000, 10000}),
        exact_limit_key="exact_common_limit_row_count_partitions",
        apply_pagination_probe=False,
        empty_error_mode="early_return",
        include_strategy=False,
        key_columns=tuple(spec.key_columns),
        key_extra_columns=("available_at", spec.time_column, spec.date_column),
        keys_intersect_available=True,
        blank_mode="available_at_total",
        pit_rules=text_pit_rules().get(spec.api_name, {}),
        partitions_message="text partition inventory",
        keys_message="text key and PIT checks",
    ))


def text_pit_rules() -> dict[str, dict[str, str]]:
    return {
        "anns_d": {"available_at": "rec_time; if missing, treat ann_date as visible only after close or next session", "unit": "text/url, no numeric unit"},
        "major_news": {"available_at": "pub_time", "unit": "text"},
        "news": {"available_at": "datetime", "unit": "text"},
        "cctv_news": {"available_at": "date at 23:59:59+08:00 conservative fallback", "unit": "text"},
        "npr": {"available_at": "pubtime", "unit": "HTML/text"},
        "irm_qa_sh": {"available_at": "pub_time; trade_date end-of-day fallback", "unit": "Q&A text"},
        "irm_qa_sz": {"available_at": "pub_time; trade_date end-of-day fallback", "unit": "Q&A text"},
        "research_report": {"available_at": "trade_date conservative end-of-day unless a more precise time is available", "unit": "text/summary/url"},
        "report_rc": {"available_at": "create_time if present, otherwise report_date 22:00+08 based on documented nightly update", "unit": "mixed forecast fields; do not mix directly with P2 actual statements"},
    }

def expected_text_paths(raw_dir: Path, spec: TextDataset, start_date: str, end_date: str, args: argparse.Namespace) -> set[Path]:
    start = max(start_date, spec.start_date)
    if spec.strategy in {"range_month", "time_range_month"}:
        months = [month for _, _, month in month_windows(start, end_date)]
        if spec.strategy == "time_range_month":
            sources = args.major_news_src or [""]
            return {raw_dir / spec.api_name / (f"src={safe_partition_value(source)}" if source else "src=all") / f"month={month}.parquet" for source in sources for month in months}
        return {raw_dir / spec.api_name / f"month={month}.parquet" for month in months}
    if spec.strategy == "news_src_month":
        sources = selected_news_sources(getattr(args, "news_src", []))
        months = [month for _, _, month in month_windows(start, end_date)]
        return {raw_dir / spec.api_name / f"src={safe_partition_value(source)}" / f"month={month}.parquet" for source in sources for month in months}
    if spec.strategy == "news_src_day":
        sources = selected_news_sources(getattr(args, "news_src", []))
        days = date_range_days(start, end_date)
        return {raw_dir / spec.api_name / f"src={safe_partition_value(source)}" / f"date={day}.parquet" for source in sources for day in days}
    if spec.strategy == "day":
        return {raw_dir / spec.api_name / f"date={day}.parquet" for day in date_range_days(start, end_date)}
    raise RuntimeError(f"unsupported text strategy {spec.strategy} for {spec.api_name}")

def audit_text_completeness(raw_dir: Path, args: argparse.Namespace, add) -> None:
    datasets = selected_integrated_text_datasets(args)
    text_end = args.text_end_date or args.end_date
    add("info", "text_expected_scope", "optional TuShare text/NL datasets included in this audit", {
        "datasets": datasets,
        "start_date": args.text_start_date,
        "end_date": text_end,
        "dataset_pit_rules": text_pit_rules(),
    })
    for dataset in datasets:
        spec = TEXT_SPECS[dataset]
        expected = expected_text_paths(raw_dir, spec, args.text_start_date, text_end, args)
        audit_text_dataset(raw_dir, spec, expected, add)


def audit_text_only(args: argparse.Namespace) -> int:
    repo_root = Path.cwd().resolve()
    raw_dir = (repo_root / args.raw_dir).resolve()
    output = (repo_root / (args.output or TEXT_EVIDENCE_STATUS_PATH)).resolve()
    findings: list[dict[str, Any]] = []

    def add(severity: str, check: str, message: str, details: dict[str, Any] | None = None) -> None:
        findings.append({"severity": severity, "check": check, "message": message, "details": details or {}})

    datasets = selected_integrated_text_datasets(args)
    audit_integrated_filesystem(raw_dir, datasets, add)
    audit_text_completeness(raw_dir, args, add)
    report = build_quality_report(
        report_type="text_evidence",
        scope={
            "data_root": str(raw_dir),
            "start_date": args.text_start_date,
            "end_date": args.text_end_date,
            "datasets": datasets,
        },
        findings=findings,
        datasets=summarize_dataset_status(findings),
        metadata={
            "pit_rules": text_pit_rules(),
            "doc_refs": {
                dataset: INTEGRATED_DOC_REFS[dataset]
                for dataset in sorted(set(datasets) & set(INTEGRATED_DOC_REFS))
            },
            "conclusions": [
                "Text rows remain raw evidence; snapshot construction must apply each source's recorded availability rule.",
                "Repeated delivery and republication are retained in raw data and deduplicated deterministically in the snapshot layer.",
            ],
        },
    )
    counts = report["finding_counts"]
    status = report["status"]
    write_quality_report(output, report)
    print(f"text audit status={status} errors={counts['error']} warnings={counts['warning']} output={output}")
    return 1 if counts["error"] else 0

def selected_audit_macro_datasets(args: argparse.Namespace) -> list[str]:
    datasets = list(getattr(args, "datasets", None) or MACRO_DATASETS)
    invalid = sorted(set(datasets) - set(MACRO_SPECS))
    if invalid:
        raise RuntimeError(f"unknown macro/global datasets: {invalid}; supported={sorted(MACRO_SPECS)}")
    return datasets

def expected_macro_paths(raw_dir: Path, spec: MacroDataset, start_date: str, end_date: str, args: argparse.Namespace) -> set[Path]:
    start = max(start_date, spec.start_date)
    if spec.strategy in {"quarter_once", "month_once"}:
        # Mirrors the downloader's retained floor: range pulls always cover
        # [floor, latest] and land in ONE canonical file regardless of the
        # audit window, so the expectation must not follow start/end_date.
        retained = max(min(start_date, MACRO_RETAINED_FLOOR), spec.start_date)
        if spec.strategy == "quarter_once":
            start_q = max(yyyymmdd_to_quarter(retained), spec.start_quarter)
            return {raw_dir / spec.api_name / f"range={start_q}_latest.parquet"}
        start_m = max(yyyymmdd_to_month(retained), spec.start_month)
        return {raw_dir / spec.api_name / f"range={start_m}_latest.parquet"}
    if spec.strategy == "month_loop":
        return {raw_dir / spec.api_name / f"month={month}.parquet" for _, _, month in month_windows(start, end_date)}
    if spec.strategy == "date_year":
        return {raw_dir / spec.api_name / f"year={year}.parquet" for year in range(int(start[:4]), int(end_date[:4]) + 1)}
    if spec.strategy == "date_year_by_curr_type":
        currencies = selected_libor_currencies(args)
        return {
            raw_dir / spec.api_name / f"curr_type={safe_partition_value(curr_type)}" / f"year={year}.parquet"
            for curr_type in currencies
            for year in range(int(start[:4]), int(end_date[:4]) + 1)
        }
    if spec.strategy == "trade_date":
        open_dates = load_sse_open_dates(raw_dir, start, end_date)
        if spec.loop_values:
            return {
                raw_dir / spec.api_name / f"{spec.loop_param}={safe_partition_value(value)}" / f"trade_date={d}.parquet"
                for value in spec.loop_values
                for d in open_dates
                if d >= spec.loop_start_date(value)
            }
        return {raw_dir / spec.api_name / f"trade_date={d}.parquet" for d in open_dates}
    if spec.strategy == "static_full":
        directory = raw_dir / spec.api_name
        if spec.loop_values:
            return {directory / f"{spec.loop_param}={safe_partition_value(value)}.parquet" for value in spec.loop_values}
        return {directory / "full.parquet"}
    if spec.strategy == "date_year_by_ts_code":
        if spec.api_name == "index_global":
            codes = selected_index_codes(args)
        elif spec.api_name in ("index_daily", "index_dailybasic"):
            codes = selected_cn_index_codes(args)
        else:
            codes = selected_fx_codes(args)
        return {
            raw_dir / spec.api_name / f"ts_code={safe_partition_value(ts_code)}" / f"year={year}.parquet"
            for ts_code in codes
            for year in range(int(start[:4]), int(end_date[:4]) + 1)
        }
    if spec.strategy == "eco_cal_month":
        countries = selected_eco_filter_values(args, "eco_country")
        currencies = selected_eco_filter_values(args, "eco_currency")
        events = selected_eco_filter_values(args, "eco_event")
        return {
            raw_dir / spec.api_name / f"country={safe_partition_value(country) if country else 'all'}" / f"currency={safe_partition_value(currency) if currency else 'all'}" / f"event={safe_partition_value(event) if event else 'all'}" / f"month={month}.parquet"
            for country in countries
            for currency in currencies
            for event in events
            for _, _, month in month_windows(start, end_date)
        }
    raise RuntimeError(f"unsupported macro strategy {spec.strategy} for {spec.api_name}")

def macro_pit_rules() -> dict[str, str]:
    return {
        "cn_schedule": "publish_date is the intended release date and should refine monthly/quarterly macro visibility when snapshot construction maps data_api to realized releases.",
        "monthly_macro": "raw month-only indicators are stamped conservatively as month-end plus 31 days until cn_schedule or another release timestamp is applied.",
        "quarterly_macro": "raw quarter-only indicators are stamped conservatively as quarter-end plus 45 days until a release schedule is applied.",
        "daily_rates": "date-only rates and cross-market daily series are stamped at local end-of-day; do not use them for same-day open decisions without an explicit release time.",
        "eco_cal": "date+time events use source time when parseable; all-day or missing-time events fall back to date end-of-day.",
        "monetary_policy": "pub_date is used as conservative end-of-day availability; content_html/PDF are text evidence and should be hashed before LLM use.",
        "derivatives_daily": "fut_daily/fut_mapping/opt_daily/cb_daily/yc_cb rows are stamped at trade_date end-of-day and roll on the evening node: usable from the NEXT trading morning, never for same-day open decisions.",
        "derivatives_registry": "fut_basic/opt_basic/cb_basic rows become visible at their list_date; cb_call announcements at ann_date end-of-day (redemption events are evening disclosures). WARNING: cb_basic is a nightly CURRENT-STATE refresh — conv_price/remain_size/newest_rating/delist_date must never feed historical backtests; derive the as-of conversion price from cb_daily (100 * stock close / cb_value), use cb_over_rate for as-of premium and cb_call for redemption outcomes.",
    }

def macro_unit_rules() -> dict[str, str]:
    return {
        "cn_gdp": "GDP and industry value fields are 100m CNY style macro levels; *_yoy fields are percent.",
        "cn_cpi/cn_ppi": "index and inflation fields are official CPI/PPI values, month-on-month/year-on-year/accumulated percent fields by column suffix.",
        "cn_pmi": "PMI fields are diffusion-index levels.",
        "cn_m": "m0/m1/m2 are 100m CNY; *_yoy and *_mom are percent.",
        "sf_month": "social financing flow/stock fields are official 100m CNY style macro levels.",
        "shibor/shibor_lpr/libor/hibor/us_rates": "rate columns are percent levels unless a field name/document explicitly states otherwise.",
        "repo_daily": "bond repo price/rate/amount fields are preserved in official raw units; normalize before cross-asset factor use.",
        "index_global": "global index OHLC fields are index points; vol/amount availability varies by market and source.",
        "fx_daily": "FX quote fields are bid/ask prices; tick_qty is quote/tick count, not stock volume.",
        "eco_cal": "economic-calendar actual/previous/forecast values are heterogeneous by event and must not be pooled without event-specific parsing.",
        "monetary_policy": "text/PDF evidence; no numeric unit.",
        "fut_daily": "futures prices are contract quote units (index points for CFFEX); vol/oi are lots (手); amount is 万元; multiplier from fut_basic converts to notional.",
        "opt_daily": "option prices are premium quote units; vol/oi are contracts (手); amount is 万元; exercise_price from opt_basic shares the underlying quote unit.",
        "cb_daily": "CB prices are per-100-par CNY; vol is lots (手), amount is 万元; bond_over_rate/cb_over_rate are percent.",
        "yc_cb": "yield is percent per annum; curve_term is years; curve_type 0=YTM, 1=spot.",
    }

def audit_macro_dataset(raw_dir: Path, spec: MacroDataset, expected_paths: set[Path], add) -> None:
    audit_domain_dataset(raw_dir, spec, expected_paths, add, DomainAuditProfile(
        domain="macro",
        exact_limit_rows=frozenset({1000, 3000, 5000, 8000, 10000}),
        exact_limit_key="exact_common_limit_row_count_partitions",
        apply_pagination_probe=False,
        empty_error_mode="with_expected",
        include_strategy=True,
        key_columns=tuple(spec.key_columns),
        key_extra_columns=("available_at", "available_at_rule"),
        keys_intersect_available=False,
        blank_mode="per_key_field",
        pit_rules=macro_pit_rules(),
        partitions_message="macro/global partition inventory",
        keys_message="key, PIT, and duplicate checks",
    ))


def audit_macro_completeness(raw_dir: Path, args: argparse.Namespace, add) -> None:
    datasets = selected_audit_macro_datasets(args)
    add("info", "macro_expected_scope", "TuShare macro, policy, and global-context datasets included in this audit", {
        "datasets": datasets,
        "start_date": args.start_date,
        "end_date": args.end_date,
        "index_codes": selected_index_codes(args),
        "fx_codes": selected_fx_codes(args),
        "libor_currencies": selected_libor_currencies(args),
        "eco_country": selected_eco_filter_values(args, "eco_country"),
        "eco_currency": selected_eco_filter_values(args, "eco_currency"),
        "eco_event": selected_eco_filter_values(args, "eco_event"),
        "dataset_pit_rules": macro_pit_rules(),
        "dataset_unit_rules": macro_unit_rules(),
    })
    for dataset in datasets:
        spec = MACRO_SPECS[dataset]
        expected = expected_macro_paths(raw_dir, spec, args.start_date, args.end_date, args)
        if spec.strategy in {"quarter_once", "month_once"}:
            # Extra range files duplicate the whole series in snapshot domain
            # unions; the downloader prunes them, so any survivor is an error.
            stale = sorted(
                str(path)
                for path in (raw_dir / spec.api_name).glob("range=*.parquet")
                if path.resolve() not in {p.resolve() for p in expected}
            )
            if stale:
                add("error", f"{spec.api_name}_stale_range_partitions", "non-canonical range partitions duplicate the series", {
                    "strategy": spec.strategy,
                    "stale_files": len(stale),
                    "stale_sample": stale[:10],
                })
        audit_macro_dataset(raw_dir, spec, expected, add)

def audit_macro_only(args: argparse.Namespace) -> int:
    repo_root = Path.cwd().resolve()
    raw_dir = (repo_root / args.raw_dir).resolve()
    output = (repo_root / (args.output or MACRO_CONTEXT_STATUS_PATH)).resolve()
    findings: list[dict[str, Any]] = []

    def add(severity: str, check: str, message: str, details: dict[str, Any] | None = None) -> None:
        findings.append({"severity": severity, "check": check, "message": message, "details": details or {}})

    datasets = selected_audit_macro_datasets(args)
    audit_integrated_filesystem(raw_dir, datasets, add)
    audit_macro_completeness(raw_dir, args, add)
    report = build_quality_report(
        report_type="macro_context",
        scope={
            "data_root": str(raw_dir),
            "start_date": args.start_date,
            "end_date": args.end_date,
            "datasets": datasets,
            "index_codes": selected_index_codes(args),
            "fx_codes": selected_fx_codes(args),
            "libor_currencies": selected_libor_currencies(args),
            "eco_country": selected_eco_filter_values(args, "eco_country"),
            "eco_currency": selected_eco_filter_values(args, "eco_currency"),
            "eco_event": selected_eco_filter_values(args, "eco_event"),
        },
        findings=findings,
        datasets=summarize_dataset_status(findings),
        metadata={
            "unit_rules": macro_unit_rules(),
            "pit_rules": macro_pit_rules(),
            "doc_refs": {
                dataset: INTEGRATED_DOC_REFS[dataset]
                for dataset in sorted(set(datasets) & set(INTEGRATED_DOC_REFS))
            },
            "conclusions": [
                "Macro/global context is stored as raw evidence and regime context; snapshot construction must still apply release-time and event-specific PIT rules.",
                "Monthly and quarterly macro tables use conservative availability fallbacks until cn_schedule or a more precise source release time is joined.",
                "Economic-calendar values are heterogeneous by event and should not be turned into numeric signals without event-specific parsing.",
                "monetary_policy is text/PDF evidence; hash and truncate content before LLM prompts and keep it shadow-only until the trading policy explicitly allows text impact.",
            ],
        },
    )
    counts = report["finding_counts"]
    status = report["status"]
    write_quality_report(output, report)
    print(f"macro audit status={status} errors={counts['error']} warnings={counts['warning']} output={output}")
    return 1 if counts["error"] else 0

def expected_event_paths(raw_dir: Path, spec: EventDataset, start_date: str, end_date: str) -> set[Path]:
    start = max(start_date, spec.start_date)
    if spec.strategy == "trade_date":
        trade_end_date = min(end_date, latest_sse_calendar_date(raw_dir))
        trade_dates = load_sse_open_dates(raw_dir, start, trade_end_date)
        return {raw_dir / spec.api_name / f"trade_date={trade_date}.parquet" for trade_date in trade_dates}
    if spec.strategy == "range_month":
        return {raw_dir / spec.api_name / f"month={month}.parquet" for _, _, month in month_windows(start, end_date)}
    if spec.strategy == "day":
        return {raw_dir / spec.api_name / f"date={day}.parquet" for day in date_range_days(start, end_date)}
    raise RuntimeError(f"unsupported event/flow strategy {spec.strategy} for {spec.api_name}")

def event_unit_rules() -> dict[str, str]:
    return {
        "margin/margin_detail": "financing/margin balance and amount fields are preserved in TuShare raw units; rqyl is securities-lending quantity.",
        "margin_secs": "margin eligibility table has no numeric market amount; exchange is SSE/SZSE/BSE and does not guarantee broker-level borrow inventory.",
        "moneyflow": "volume fields are raw TuShare moneyflow volume units and amount fields are raw TuShare amount units; normalize before mixing with daily/stk_mins.",
        "stk_holdernumber": "holder_num is shareholder account count.",
        "stk_holdertrade": "change_vol/after_share/total_share are raw share fields; ratios are percent-style raw fields.",
        "repurchase": "vol/amount/high_limit/low_limit are raw TuShare repurchase units; treat as event evidence until normalized.",
        "share_float": "float_share and float_ratio are raw unlock-share and ratio fields; field availability should be based on ann_date, not float_date, when ann_date exists.",
        "block_trade": "price/vol/amount are raw block-trade fields; block_trade is sparse and zero-row trade dates are expected.",
    }

def event_pit_rules() -> dict[str, str]:
    return {
        "margin": "available_at uses next-day 09:00+08 from trade_date.",
        "margin_detail": "available_at uses next-day 09:00+08 from trade_date.",
        "margin_secs": "available_at uses same-day 09:00+08 from trade_date because this is a pre-open eligibility table.",
        "moneyflow": "available_at uses 19:00+08 from trade_date.",
        "moneyflow_dc": "available_at uses 19:00+08 from trade_date.",
        "moneyflow_ths": "available_at uses 19:00+08 from trade_date.",
        "moneyflow_ind_dc": "available_at uses 19:00+08 from trade_date.",
        "moneyflow_ind_ths": "available_at uses 19:00+08 from trade_date.",
        "moneyflow_cnt_ths": "available_at uses 19:00+08 from trade_date.",
        "cyq_perf": "available_at uses 19:00+08 from trade_date.",
        "bak_daily": "available_at uses 19:00+08 from trade_date.",
        "stk_premarket": "available_at uses same-day 09:00+08 from trade_date (pre-open static table).",
        "slb_len": "available_at uses 19:00+08 from trade_date.",
        "slb_len_mm": "available_at uses 19:00+08 from trade_date.",
        "block_trade": "available_at uses 21:00+08 from trade_date.",
        "top10_holders": "available_at uses conservative end-of-day from ann_date.",
        "top10_floatholders": "available_at uses conservative end-of-day from ann_date.",
        "pledge_detail": "available_at uses conservative end-of-day from ann_date.",
        "stk_surv": "available_at uses conservative end-of-day from surv_date.",
        "new_share": "available_at uses conservative end-of-day from ipo_date (calendar known earlier; stamped late, never leaks).",
        "stk_holdernumber": "available_at uses ann_date end-of-day.",
        "stk_holdertrade": "available_at uses ann_date 19:00+08.",
        "repurchase": "available_at uses ann_date end-of-day.",
        "share_float": "available_at uses ann_date end-of-day; if ann_date is blank, raw layer falls back to float_date and snapshot layer must treat that as conservative event-date availability, not pre-event knowledge.",
    }

def event_partition_prefix(spec: EventDataset) -> str:
    if spec.strategy == "trade_date":
        return "trade_date"
    if spec.strategy == "range_month":
        return "month"
    if spec.strategy == "day":
        return "date"
    raise RuntimeError(f"unsupported event/flow strategy {spec.strategy} for {spec.api_name}")

def audit_margin_exchange_completeness(raw_dir: Path, add) -> None:
    # Exchange-level margin days must carry every publishing exchange
    # (SSE+SZSE, plus BSE from 20230213): partial days poison market-wide
    # aggregates and the downloader refuses to commit them, so any committed
    # partial partition is a data-integrity error needing manual repair.
    incomplete: list[dict[str, Any]] = []
    for path in sorted((raw_dir / "margin").glob("trade_date=*.parquet")):
        trade_date = path.stem.split("=", 1)[1]
        present = pd.read_parquet(path, columns=["exchange_id"])["exchange_id"]
        missing = margin_missing_exchanges(trade_date, present)
        if missing:
            incomplete.append({"trade_date": trade_date, "missing_exchanges": missing})
    if incomplete:
        add(
            "error",
            "margin_exchange_completeness",
            f"margin partitions missing required exchange rows: {len(incomplete)} day(s)",
            {"incomplete_days": incomplete[:20], "incomplete_day_count": len(incomplete)},
        )


def audit_event_dataset(raw_dir: Path, spec: EventDataset, expected_paths: set[Path], add) -> None:
    if spec.api_name == "margin":
        audit_margin_exchange_completeness(raw_dir, add)
    audit_domain_dataset(raw_dir, spec, expected_paths, add, DomainAuditProfile(
        domain="event",
        exact_limit_rows=frozenset({spec.page_limit, 5000, 6000, 10000}),
        exact_limit_key="exact_common_limit_row_count_partitions",
        apply_pagination_probe=True,
        empty_error_mode="always",
        include_strategy=True,
        key_columns=tuple(spec.key_columns),
        key_extra_columns=("available_at", "available_at_rule"),
        keys_intersect_available=False,
        blank_mode="per_key_field",
        pit_rules=event_pit_rules().get(spec.api_name, ""),
        partitions_message="event/flow partition inventory",
        keys_message="key, duplicate, and PIT checks",
        partition_prefix=event_partition_prefix(spec),
        zero_rows_ok=spec.zero_rows_ok,
    ))


def audit_share_float_complete_union(raw_dir: Path, add) -> None:
    union_path = raw_dir / "share_float_complete" / "share_float_complete.parquet"
    meta_path = union_path.with_suffix(union_path.suffix + ".meta.json")
    ann_rescue_files = sorted((raw_dir / "share_float_ann_date_ts_code").rglob("*.parquet"))
    float_rescue_files = sorted((raw_dir / "share_float_float_date_ts_code").rglob("*.parquet"))
    rescue_limit_files: list[str] = []
    rescue_zero_files = 0
    for path in ann_rescue_files + float_rescue_files:
        rows = parquet_rows(path)
        if rows == 0:
            rescue_zero_files += 1
        if rows >= SHARE_FLOAT_ROW_LIMIT:
            rescue_limit_files.append(str(path))

    base_details: dict[str, Any] = {
        "path": str(union_path),
        "ann_date_ts_code_files": len(ann_rescue_files),
        "float_date_ts_code_files": len(float_rescue_files),
        "rescue_zero_files": rescue_zero_files,
        "rescue_limit_files": len(rescue_limit_files),
        "rescue_limit_file_sample": rescue_limit_files[:10],
    }
    if not union_path.exists():
        add("warning", "share_float_complete_union", "share_float complete union file is missing; event_flow audit falls back to raw share_float partitions only", base_details)
        return

    rows = parquet_rows(union_path)
    schema = pq.ParquetFile(union_path).schema_arrow.names
    required_columns = ["ts_code", "ann_date", "float_date", "download_path", "source_file", "source_cap_risk"]
    missing_columns = [column for column in required_columns if column not in schema]
    meta_row_count = None
    meta_error = ""
    if meta_path.exists():
        try:
            meta_row_count = json.loads(meta_path.read_text(encoding="utf-8")).get("row_count")
        except Exception as exc:
            meta_error = str(exc)

    details: dict[str, Any] = {
        **base_details,
        "rows": rows,
        "meta_exists": meta_path.exists(),
        "meta_row_count": meta_row_count,
        "meta_error": meta_error,
        "missing_columns": missing_columns,
    }
    try:
        columns = [column for column in ("download_path", "source_cap_risk", "source_file") if column in schema]
        df = pd.read_parquet(union_path, columns=columns) if columns else pd.DataFrame()
        if "download_path" in df.columns:
            details["download_path_counts"] = {str(key): int(value) for key, value in df["download_path"].value_counts(dropna=False).sort_index().items()}
        if "source_file" in df.columns:
            details["input_files_seen"] = int(df["source_file"].nunique(dropna=True))
        if "source_cap_risk" in df.columns:
            risk = df["source_cap_risk"].fillna(False)
            if risk.dtype != bool:
                risk = risk.astype(str).str.lower().isin({"true", "1", "yes"})
            details["source_cap_risk_rows"] = int(risk.sum())
    except Exception as exc:
        details["read_error"] = str(exc)

    severity = "warning" if (
        rows == 0
        or missing_columns
        or not meta_path.exists()
        or meta_error
        or details.get("source_cap_risk_rows", 0)
        or rescue_limit_files
    ) else "info"
    add(severity, "share_float_complete_union", "share_float complete union and rescue coverage", details)

def share_float_complete_union_exists(raw_dir: Path) -> bool:
    return (raw_dir / "share_float_complete" / "share_float_complete.parquet").exists()

def audit_event_flow_only(args: argparse.Namespace) -> int:
    repo_root = Path.cwd().resolve()
    raw_dir = (repo_root / args.raw_dir).resolve()
    output = (repo_root / (args.output or EVENT_FLOW_STATUS_PATH)).resolve()
    findings: list[dict[str, Any]] = []

    def add(severity: str, check: str, message: str, details: dict[str, Any] | None = None) -> None:
        findings.append({"severity": severity, "check": check, "message": message, "details": details or {}})

    datasets = selected_event_flow_datasets(args)
    filesystem_datasets = [dataset for dataset in datasets if dataset != "share_float" or not share_float_complete_union_exists(raw_dir)]
    audit_integrated_filesystem(raw_dir, filesystem_datasets, add)
    add("info", "event_flow_expected_scope", "TuShare event/flow datasets included in this audit", {
        "datasets": datasets,
        "filesystem_datasets": filesystem_datasets,
        "share_float_retained_as_union": bool("share_float" in datasets and share_float_complete_union_exists(raw_dir)),
        "start_date": args.start_date,
        "end_date": args.end_date,
        "dataset_pit_rules": event_pit_rules(),
        "dataset_unit_rules": event_unit_rules(),
    })
    for dataset in datasets:
        if dataset == "share_float" and share_float_complete_union_exists(raw_dir):
            continue
        spec = EVENT_FLOW_SPECS[dataset]
        expected = expected_event_paths(raw_dir, spec, args.start_date, args.end_date)
        audit_event_dataset(raw_dir, spec, expected, add)
    if "share_float" in datasets:
        audit_share_float_complete_union(raw_dir, add)

    report = build_quality_report(
        report_type="event_flow",
        scope={
            "data_root": str(raw_dir),
            "start_date": args.start_date,
            "end_date": args.end_date,
            "datasets": datasets,
        },
        findings=findings,
        datasets=summarize_dataset_status(findings),
        metadata={
            "unit_rules": event_unit_rules(),
            "pit_rules": event_pit_rules(),
            "doc_refs": {
                dataset: INTEGRATED_DOC_REFS[dataset]
                for dataset in sorted(set(datasets) & set(INTEGRATED_DOC_REFS))
            },
            "conclusions": [
                "Event/flow raw data is sparse by design; zero-row event months or block-trade dates are expected for sparse event sources.",
                "Daily flow tables must still be joined with explicit PIT availability; same-day open decisions cannot use post-close or next-day event/flow values.",
                "share_float raw partitions and the optional share_float_complete union are audited together; exact 6000-row partitions remain source-cap risks.",
                "Raw event rows are not deduplicated; downstream evidence/snapshot layers need deterministic event-key and availability rules.",
            ],
        },
    )
    counts = report["finding_counts"]
    status = report["status"]
    write_quality_report(output, report)
    print(f"event_flow audit status={status} errors={counts['error']} warnings={counts['warning']} output={output}")
    return 1 if counts["error"] else 0

def expected_board_paths(raw_dir: Path, spec: BoardTradingDataset, start_date: str, end_date: str, args: argparse.Namespace) -> set[Path]:
    start = max(start_date, spec.start_date)
    if spec.strategy == "static_once":
        return {raw_dir / spec.api_name / f"{spec.api_name}.parquet"}
    if start > end_date:
        return set()
    trade_dates = load_sse_open_dates(raw_dir, start, end_date)
    if spec.strategy == "trade_date":
        return {raw_dir / spec.api_name / f"trade_date={trade_date}.parquet" for trade_date in trade_dates}
    if spec.strategy == "trade_date_by_tag":
        return {
            raw_dir / spec.api_name / f"tag={safe_partition_value(tag)}" / f"trade_date={trade_date}.parquet"
            for tag in selected_board_kpl_tags(args)
            for trade_date in trade_dates
        }
    if spec.strategy == "trade_date_by_limit_type":
        return {
            raw_dir / spec.api_name / f"limit_type={safe_partition_value(limit_type)}" / f"trade_date={trade_date}.parquet"
            for limit_type in selected_board_ths_limit_types(args)
            for trade_date in trade_dates
        }
    if spec.strategy == "trade_date_by_market":
        return {
            raw_dir / spec.api_name / f"market={safe_partition_value(market)}" / f"is_new={is_new}" / f"trade_date={trade_date}.parquet"
            for market in selected_board_ths_hot_markets(args)
            for is_new in selected_board_hot_is_new(args)
            for trade_date in trade_dates
        }
    if spec.strategy == "trade_date_by_market_hot_type":
        return {
            raw_dir / spec.api_name / f"market={safe_partition_value(market)}" / f"hot_type={safe_partition_value(hot_type)}" / f"is_new={is_new}" / f"trade_date={trade_date}.parquet"
            for market in selected_board_dc_hot_markets(args)
            for hot_type in selected_board_dc_hot_types(args)
            for is_new in selected_board_hot_is_new(args)
            for trade_date in trade_dates
        }
    raise RuntimeError(f"unsupported board-trading strategy {spec.strategy} for {spec.api_name}")

def audit_board_dataset(raw_dir: Path, spec: BoardTradingDataset, expected_paths: set[Path], add) -> None:
    audit_domain_dataset(raw_dir, spec, expected_paths, add, DomainAuditProfile(
        domain="board",
        exact_limit_rows=frozenset({spec.page_limit}),
        exact_limit_key="exact_page_limit_row_count_partitions",
        apply_pagination_probe=True,
        empty_error_mode="always",
        include_strategy=True,
        key_columns=tuple(spec.key_columns),
        key_extra_columns=("available_at", "available_at_rule", spec.date_column, spec.time_column),
        keys_intersect_available=False,
        blank_mode="available_at_rows",
        pit_rules=board_pit_rules().get(spec.api_name, {}),
        partitions_message="board-trading partition inventory",
        keys_message="board-trading key and PIT checks",
        availability_required=spec.strategy != "static_once",
    ))


def board_pit_rules() -> dict[str, dict[str, str]]:
    return {
        "kpl_list": {"available_at": "official next-day 08:30 from trade_date", "usage": "next-day board sentiment/evidence; no same-day intraday lookahead"},
        "kpl_concept_cons": {"available_at": "official next-day 08:30 from trade_date", "usage": "next-day concept membership/heat evidence; no same-day intraday lookahead"},
        "dc_index": {"available_at": "official 20:00 from trade_date", "usage": "post-close board-index rotation evidence"},
        "dc_member": {"available_at": "official 20:00 from trade_date", "usage": "post-close board membership map"},
        "limit_step": {"available_at": "conservative trade-date end-of-day", "usage": "market height and limit-up ladder after close"},
        "limit_cpt_list": {"available_at": "conservative trade-date end-of-day", "usage": "topic strength and limit-up board rotation after close"},
        "limit_list_ths": {"available_at": "official around 16:00 from trade_date", "usage": "post-close THS limit-up/down pool evidence; no same-day intraday lookahead"},
        "top_list": {"available_at": "official 20:00 from trade_date", "usage": "next-day Dragon-Tiger list evidence"},
        "top_inst": {"available_at": "official 20:00 from trade_date", "usage": "next-day institutional seat evidence"},
        "hm_list": {"available_at": "static reference list; do not use as historical time-series signal without hm_detail rows"},
        "hm_detail": {"available_at": "conservative trade-date end-of-day", "usage": "next-day hot-money seat evidence"},
        "ths_hot": {"available_at": "rank_time when returned; is_new=Y falls back to 22:30", "usage": "intraday/evening hot-list evidence by observable rank_time"},
        "dc_hot": {"available_at": "rank_time when returned; is_new=Y falls back to 22:30", "usage": "intraday/evening hot-list evidence by observable rank_time"},
    }

def board_unit_rules() -> dict[str, str]:
    return {
        "kpl_list": "amount/free_float/limit_order/lu_limit_order style fields are preserved in official raw units, mostly CNY-level amounts from source.",
        "limit_step": "nums is a consecutive-limit count label; no monetary unit.",
        "limit_cpt_list": "up_nums/cons_nums are counts; pct_chg is percent; rank is source rank.",
        "limit_list_ths": "price/current monetary fields are preserved in official raw units; pct_chg/turnover/rise_rate style fields are percent or source ratios as named.",
        "top_list": "amount and Dragon-Tiger buy/sell/net fields are CNY amounts; rates are percent.",
        "top_inst": "buy/sell/net_buy fields are CNY amounts; buy_rate/sell_rate are percent.",
        "hm_detail": "buy_amount/sell_amount/net_amount are CNY amounts.",
        "ths_hot/dc_hot": "rank/hot are source popularity ranks/scores; pct_change is percent; current_price is CNY price for A-share rows.",
        "hm_list": "static text/reference metadata; no numeric unit.",
    }

def audit_board_trading_only(args: argparse.Namespace) -> int:
    repo_root = Path.cwd().resolve()
    raw_dir = (repo_root / args.raw_dir).resolve()
    output = (repo_root / (args.output or BOARD_TRADING_STATUS_PATH)).resolve()
    findings: list[dict[str, Any]] = []

    def add(severity: str, check: str, message: str, details: dict[str, Any] | None = None) -> None:
        findings.append({"severity": severity, "check": check, "message": message, "details": details or {}})

    datasets = selected_board_trading_datasets(args)
    audit_integrated_filesystem(raw_dir, datasets, add)
    add("info", "board_trading_expected_scope", "TuShare board-trading datasets included in this audit", {
        "datasets": datasets,
        "start_date": args.start_date,
        "end_date": args.end_date,
        "kpl_tags": selected_board_kpl_tags(args),
        "ths_limit_types": selected_board_ths_limit_types(args),
        "ths_hot_markets": selected_board_ths_hot_markets(args),
        "dc_hot_markets": selected_board_dc_hot_markets(args),
        "dc_hot_types": selected_board_dc_hot_types(args),
        "hot_is_new": selected_board_hot_is_new(args),
        "dataset_pit_rules": board_pit_rules(),
        "dataset_unit_rules": board_unit_rules(),
    })
    for dataset in datasets:
        spec = BOARD_TRADING_SPECS[dataset]
        expected = expected_board_paths(raw_dir, spec, args.start_date, args.end_date, args)
        audit_board_dataset(raw_dir, spec, expected, add)

    report = build_quality_report(
        report_type="board_trading",
        scope={
            "data_root": str(raw_dir),
            "start_date": args.start_date,
            "end_date": args.end_date,
            "datasets": datasets,
        },
        findings=findings,
        datasets=summarize_dataset_status(findings),
        metadata={
            "unit_rules": board_unit_rules(),
            "pit_rules": board_pit_rules(),
            "doc_refs": {
                dataset: INTEGRATED_DOC_REFS[dataset]
                for dataset in sorted(set(datasets) & set(INTEGRATED_DOC_REFS))
            },
            "conclusions": [
                "Board-trading raw data is a dedicated sentiment/event evidence domain for limit-up, ladder, topic, Dragon-Tiger, hot-money, and hot-list signals.",
                "Most board-trading datasets are only valid after close or the next morning; intraday usage must rely on rank_time or a documented observable timestamp.",
                "These raw rows complement limit_list_d and minute-derived labels; they do not replace PIT execution constraints built from stk_limit and 1-minute bars.",
            ],
        },
    )
    counts = report["finding_counts"]
    status = report["status"]
    write_quality_report(output, report)
    print(f"board_trading audit status={status} errors={counts['error']} warnings={counts['warning']} output={output}")
    return 1 if counts["error"] else 0

def build_bak_daily_api_case_studies(repo_root: Path, raw_dir: Path, args: argparse.Namespace) -> tuple[list[dict[str, Any]], dict[str, Any], list[str]]:
    case_studies: list[dict[str, Any]] = []
    warnings: list[str] = []
    details: dict[str, Any] = {"row_count_probes": [], "unit_samples": [], "local_api_row_mismatches": []}
    if not args.probe_api:
        warnings.append("API probes skipped; bak_daily is not downloaded locally, so bak_daily unit case studies were not refreshed")
        return case_studies, details, warnings
    try:
        client = TuShareClient(load_token(repo_root), args.min_interval_seconds, args.timeout_seconds)
    except Exception as exc:
        warnings.append(f"API probes could not load token: {exc}")
        return case_studies, details, warnings

    for trade_date in ["20160809", "20170703", "20200102", args.end_date]:
        item: dict[str, Any] = {"trade_date": trade_date, "api_rows": {}, "local_rows": {}}
        for api_name in ("bak_basic", "bak_daily", "daily"):
            fields = "trade_date,ts_code" if api_name == "bak_basic" else "ts_code,trade_date,vol,amount"
            df = api_frame(client, api_name, {"trade_date": trade_date}, fields)
            item["api_rows"][api_name] = int(len(df))
        for dataset in ("bak_basic", "daily"):
            path = raw_dir / dataset / f"trade_date={trade_date}.parquet"
            item["local_rows"][dataset] = int(parquet_rows(path)) if path.exists() else None
            if item["local_rows"][dataset] is not None and item["local_rows"][dataset] != item["api_rows"].get(dataset):
                details["local_api_row_mismatches"].append({
                    "trade_date": trade_date,
                    "dataset": dataset,
                    "local_rows": item["local_rows"][dataset],
                    "api_rows": item["api_rows"].get(dataset),
                })
        details["row_count_probes"].append(item)

    samples: list[dict[str, Any]] = []
    for code in ("000001.SZ", "000716.SZ", "688012.SH"):
        daily = api_frame(client, "daily", {"trade_date": "20200102", "ts_code": code}, "ts_code,trade_date,close,vol,amount")
        bak_daily = api_frame(client, "bak_daily", {"trade_date": "20200102", "ts_code": code}, "ts_code,trade_date,close,vol,amount,total_share,float_share,total_mv,float_mv")
        daily_basic = api_frame(client, "daily_basic", {"trade_date": "20200102", "ts_code": code}, "ts_code,trade_date,total_share,float_share,total_mv,circ_mv")
        if daily.empty or bak_daily.empty or daily_basic.empty:
            samples.append({"ts_code": code, "missing": {"daily": daily.empty, "bak_daily": bak_daily.empty, "daily_basic": daily_basic.empty}})
            continue
        d_vol = numeric_value(daily, "vol")
        b_vol = numeric_value(bak_daily, "vol")
        d_amount = numeric_value(daily, "amount")
        b_amount = numeric_value(bak_daily, "amount")
        db_total_share = numeric_value(daily_basic, "total_share")
        b_total_share = numeric_value(bak_daily, "total_share")
        db_total_mv = numeric_value(daily_basic, "total_mv")
        b_total_mv = numeric_value(bak_daily, "total_mv")
        samples.append({
            "ts_code": code,
            "trade_date": "20200102",
            "raw_values": {
                "daily.vol": d_vol,
                "bak_daily.vol": b_vol,
                "daily.amount": d_amount,
                "bak_daily.amount": b_amount,
                "daily_basic.total_share": db_total_share,
                "bak_daily.total_share": b_total_share,
                "daily_basic.total_mv": db_total_mv,
                "bak_daily.total_mv": b_total_mv,
            },
            "ratios": {
                "daily_vol_div_bak_daily_vol": d_vol / b_vol if b_vol else None,
                "daily_amount_div_bak_daily_amount": d_amount / b_amount if b_amount else None,
                "daily_basic_total_share_div_bak_daily_total_share": db_total_share / b_total_share if b_total_share else None,
                "daily_basic_total_mv_div_bak_daily_total_mv": db_total_mv / b_total_mv if b_total_mv else None,
            },
        })
    details["unit_samples"] = samples
    case_studies.append({
        "case_id": "bak_daily_unit_conversion_api_probe",
        "source": "live_tushare_api_probe; bak_daily is not downloaded locally",
        "samples": samples,
        "interpretation": "bak_daily.vol matches daily.vol scale; daily.amount is about 10x bak_daily.amount; daily_basic share and market-value fields are about 10000x bak_daily fields.",
    })
    case_studies.append({
        "case_id": "bak_basic_bak_daily_coverage_api_probe",
        "source": "live_tushare_api_probe plus local row counts for downloaded bak_basic/daily",
        "samples": details["row_count_probes"],
        "interpretation": "bak_daily cannot replace bak_basic or daily: it is empty before its 2017-mid coverage window but can fill some later bak_basic zero partitions.",
    })
    if details["local_api_row_mismatches"]:
        warnings.append("live API row counts differ from local downloaded partitions for at least one probe")
    return case_studies, details, warnings

def find_financial_pit_case(raw_dir: Path) -> dict[str, Any]:
    dataset = "income_vip"
    files = sorted((raw_dir / dataset).glob("period=*.parquet"))
    sample_cols = ["ts_code", "ann_date", "f_ann_date", "end_date", "report_type", "comp_type", "end_type", "basic_eps", "total_revenue", "revenue", "total_profit", "n_income", "n_income_attr_p"]
    available = [col for col in sample_cols if col in first_parquet_schema(raw_dir, dataset)]
    df = read_many(files, columns=available)
    if df.empty or not {"ts_code", "end_date"}.issubset(df.columns):
        return {"case_id": "fundamental_financial_pit_case", "available": False, "reason": "income_vip is empty or missing keys"}
    stats = df.groupby(["ts_code", "end_date"], dropna=False).agg(
        rows=("ts_code", "size"),
        ann_dates=("ann_date", lambda x: int(x.astype(str).nunique())) if "ann_date" in df.columns else ("ts_code", "size"),
        f_ann_dates=("f_ann_date", lambda x: int(x.astype(str).nunique())) if "f_ann_date" in df.columns else ("ts_code", "size"),
        report_types=("report_type", lambda x: int(x.astype(str).nunique())) if "report_type" in df.columns else ("ts_code", "size"),
    ).reset_index()
    candidates = stats[(stats["rows"] > 1) & ((stats["ann_dates"] > 1) | (stats["f_ann_dates"] > 1) | (stats["report_types"] > 1))]
    if candidates.empty:
        return {"case_id": "fundamental_financial_pit_case", "available": False, "reason": "no multi-version income_vip group found"}
    target = candidates.sort_values(["f_ann_dates", "ann_dates", "report_types", "rows"], ascending=False).iloc[0]
    group = df[(df["ts_code"].astype(str) == str(target["ts_code"])) & (df["end_date"].astype(str) == str(target["end_date"]))].copy()
    sort_cols = [col for col in ["f_ann_date", "ann_date", "report_type", "end_type"] if col in group.columns]
    if sort_cols:
        group = group.sort_values(sort_cols)
    return {
        "case_id": "fundamental_financial_pit_case",
        "dataset": dataset,
        "business_key": {"ts_code": json_scalar(target["ts_code"]), "end_date": json_scalar(target["end_date"])},
        "group_stats": records_for_json(pd.DataFrame([target.to_dict()])),
        "sample_rows": records_for_json(group, limit=10),
        "interpretation": "same stock/report period has multiple announcement or report versions; snapshot construction must select only records visible by f_ann_date/ann_date at decision time.",
    }

def find_financial_duplicate_key_case(raw_dir: Path) -> dict[str, Any]:
    for dataset in ("income_vip", "balancesheet_vip", "cashflow_vip", "fina_indicator_vip"):
        spec = FUNDAMENTAL_SPECS[dataset]
        files = sorted((raw_dir / dataset).glob("period=*.parquet"))
        for path in files:
            if parquet_rows(path) == 0:
                continue
            schema = pq.ParquetFile(path).schema_arrow.names
            if not set(spec.key_columns).issubset(schema):
                continue
            key_df = pd.read_parquet(path, columns=list(spec.key_columns))
            duplicated = key_df.duplicated(list(spec.key_columns), keep=False)
            if not duplicated.any():
                continue
            key = key_df.loc[duplicated, list(spec.key_columns)].iloc[0].to_dict()
            full_df = pd.read_parquet(path)
            mask = pd.Series(True, index=full_df.index)
            for col, value in key.items():
                mask &= full_df[col].astype(str) == str(value)
            group = full_df.loc[mask].copy()
            varying = [col for col in group.columns if col not in spec.key_columns and group[col].astype(str).nunique(dropna=False) > 1]
            columns = list(spec.key_columns) + varying[:10]
            return {
                "case_id": "fundamental_duplicate_business_key_case",
                "dataset": dataset,
                "partition": path.name,
                "business_key": {str(k): json_scalar(v) for k, v in key.items()},
                "rows_in_group": int(len(group)),
                "varying_columns_sample": varying[:20],
                "sample_rows": records_for_json(group, columns=columns, limit=8),
                "interpretation": "duplicate business keys are not always full-row duplicates; raw layer should preserve them, while PIT snapshot layer needs a deterministic version rule.",
            }
    return {"case_id": "fundamental_duplicate_business_key_case", "available": False, "reason": "no duplicate fundamental business key found"}

def find_dividend_issue_case(raw_dir: Path) -> dict[str, Any]:
    cols = ["ts_code", "end_date", "ann_date", "div_proc", "stk_div", "cash_div", "cash_div_tax", "record_date", "ex_date", "pay_date", "imp_ann_date"]
    files = sorted((raw_dir / "dividend").glob("ts_code=*.parquet"))
    for path in files:
        if parquet_rows(path) == 0:
            continue
        schema = pq.ParquetFile(path).schema_arrow.names
        available = [col for col in cols if col in schema]
        df = pd.read_parquet(path, columns=available)
        if "ann_date" in df.columns:
            ann = df["ann_date"].astype(str).str.strip()
            blank = df[df["ann_date"].isna() | (ann == "")]
            if not blank.empty:
                return {
                    "case_id": "dividend_blank_ann_date_case",
                    "partition": path.name,
                    "sample_rows": records_for_json(blank, limit=8),
                    "interpretation": "dividend ann_date can be blank; availability should be derived from imp_ann_date, ex_date, record_date, or pay_date according to field meaning.",
                }
        key_cols = [col for col in FUNDAMENTAL_SPECS["dividend"].key_columns if col in df.columns]
        if key_cols:
            duplicated = df.duplicated(key_cols, keep=False)
            if duplicated.any():
                group = df.loc[duplicated].copy()
                return {
                    "case_id": "dividend_duplicate_key_case",
                    "partition": path.name,
                    "key_columns": key_cols,
                    "sample_rows": records_for_json(group, limit=8),
                    "interpretation": "dividend rows can repeat by business key; snapshot construction should deduplicate after deciding the correct event date.",
                }
    return {"case_id": "dividend_issue_case", "available": False, "reason": "no blank ann_date or duplicate dividend key found"}

def find_daily_basic_coverage_case(raw_dir: Path) -> dict[str, Any]:
    preferred = "20210906"
    dates = [preferred] if (raw_dir / "daily" / f"trade_date={preferred}.parquet").exists() else []
    dates.extend(partition_date(path) for path in sorted((raw_dir / "daily").glob("trade_date=*.parquet")))
    seen: set[str] = set()
    for trade_date in dates:
        if trade_date in seen:
            continue
        seen.add(trade_date)
        daily_codes = read_partition_codes(raw_dir, "daily", trade_date)
        daily_basic_codes = read_partition_codes(raw_dir, "daily_basic", trade_date)
        daily_only = daily_codes - daily_basic_codes
        basic_only = daily_basic_codes - daily_codes
        if daily_only or basic_only:
            return {
                "case_id": "daily_vs_daily_basic_coverage_case",
                "trade_date": trade_date,
                "daily_rows": len(daily_codes),
                "daily_basic_rows": len(daily_basic_codes),
                "daily_only_count": len(daily_only),
                "daily_basic_only_count": len(basic_only),
                "daily_only_sample": sorted(daily_only)[:20],
                "daily_basic_only_sample": sorted(basic_only)[:20],
                "daily_only_code_types": code_type_counts(daily_only),
                "interpretation": "daily and daily_basic are not guaranteed to share identical same-day code coverage; downstream joins need explicit missing-data policy.",
            }
    return {"case_id": "daily_vs_daily_basic_coverage_case", "available": False, "reason": "no coverage difference found"}

def audit_daily_direct(raw_dir: Path, args: argparse.Namespace, add) -> set[str]:
    try:
        trade_dates = set(load_sse_open_dates(raw_dir, args.start_date, args.end_date))
    except Exception as exc:
        add("error", "daily_trade_calendar", str(exc))
        return set()
    for dataset in selected_daily_datasets(args):
        spec = DAILY_SPECS[dataset]
        expected = {d for d in trade_dates if max(args.start_date, spec.start_date) <= d <= args.end_date}
        audit_trade_date_dataset(raw_dir, spec, expected, add)
    return trade_dates

def summarize_dataset_status(findings: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return summarize_datasets(
        findings,
        REFERENCE_DATASETS
        + DAILY_REQUIRED_DATASETS
        + DAILY_OPTIONAL_DATASETS
        + FUNDAMENTAL_DATASETS
        + INTRADAY_DATASETS
        + EVENT_FLOW_DATASETS
        + BOARD_TRADING_DATASETS
        + TEXT_DATASETS
        + MACRO_DATASETS,
    )

def default_audit_output(args: argparse.Namespace) -> str:
    include_text = bool(getattr(args, "include_text", False))
    include_intraday = bool(getattr(args, "include_intraday", False))
    if include_text or include_intraday:
        raise ValueError("combined base audit options require an explicit --output path")
    return BASE_RESEARCH_STATUS_PATH

def audit_unified(args: argparse.Namespace) -> int:
    repo_root = Path.cwd().resolve()
    raw_dir = (repo_root / args.raw_dir).resolve()
    if not args.end_date:
        args.end_date = latest_sse_calendar_date(raw_dir)
    if getattr(args, "intraday_end_date", None) is None:
        args.intraday_end_date = args.end_date
    try:
        output_arg = args.output or default_audit_output(args)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    output = (repo_root / output_arg).resolve()
    findings: list[dict[str, Any]] = []
    case_studies: list[dict[str, Any]] = []

    def add(severity: str, check: str, message: str, details: dict[str, Any] | None = None) -> None:
        findings.append({"severity": severity, "check": check, "message": message, "details": details or {}})

    daily_datasets = selected_daily_datasets(args)
    fundamental_datasets = selected_integrated_fundamental_datasets(args)
    text_datasets = selected_integrated_text_datasets(args) if getattr(args, "include_text", False) else []
    intraday_datasets = selected_integrated_intraday_datasets(args) if getattr(args, "include_intraday", False) else []
    datasets = REFERENCE_DATASETS + daily_datasets + fundamental_datasets + intraday_datasets + text_datasets
    audit_integrated_filesystem(raw_dir, datasets, add)

    stock_basic = audit_stock_basic(raw_dir, add)
    audit_stock_company(raw_dir, stock_basic, add)
    sse_open = audit_trade_cal(raw_dir, add)
    audit_bak_basic(raw_dir, sse_open, args.end_date, add)
    audit_namechange(raw_dir, add)
    classify = audit_index_classify(raw_dir, add)
    audit_index_member_all(raw_dir, classify, stock_basic, add)

    trade_dates = audit_daily_direct(raw_dir, args, add)
    all_codes = audit_daily_cross_coverage(raw_dir, trade_dates, args, add) if trade_dates else {"daily": set(), "daily_basic": set(), "adj_factor": set(), "stk_limit": set()}
    audit_unit_schema(raw_dir, add)
    api_cases, api_details, api_warnings = build_bak_daily_api_case_studies(repo_root, raw_dir, args)
    case_studies.extend(api_cases)
    add("warning" if api_warnings or api_details.get("local_api_row_mismatches") else "info", "bak_daily_api_case_studies", "bak_daily unit and coverage API probes", {
        "warnings": api_warnings,
        **api_details,
    })
    audit_stock_universe_semantics(raw_dir, all_codes, add)
    audit_pit_availability(raw_dir, add)
    audit_fundamental_completeness(raw_dir, args, add)
    audit_fundamental_unit_and_pit_semantics(raw_dir, add)
    if getattr(args, "include_intraday", False):
        if STK_MINS_DATASET in intraday_datasets:
            audit_stk_mins_completeness(raw_dir, args, add)
    if getattr(args, "include_text", False):
        audit_text_completeness(raw_dir, args, add)
    case_studies.extend([
        find_daily_basic_coverage_case(raw_dir),
        find_financial_pit_case(raw_dir),
        find_financial_duplicate_key_case(raw_dir),
        find_dividend_issue_case(raw_dir),
    ])
    add("info", "case_studies", "integrated audit case studies were generated", {
        "case_count": len(case_studies),
        "case_ids": [case.get("case_id") for case in case_studies],
    })

    included_domains = ["base_research"]
    if text_datasets:
        included_domains.append("text_evidence")
    if intraday_datasets:
        included_domains.append("intraday_minutes")
    report = build_quality_report(
        report_type="base_research" if len(included_domains) == 1 else "combined_raw_audit",
        scope={
            "data_root": str(raw_dir),
            "start_date": args.start_date,
            "end_date": args.end_date,
            "datasets": datasets,
            "domains": included_domains,
        },
        findings=findings,
        datasets=summarize_dataset_status(findings),
        metadata={
            "case_studies": case_studies,
            "unit_rules": integrated_unit_rules(),
            "doc_refs": INTEGRATED_DOC_REFS,
            "windows": {
                "fundamental": {
                    "start_date": args.fundamental_start_date,
                    "end_date": args.fundamental_end_date or args.end_date,
                },
                "text": {
                    "start_date": args.text_start_date,
                    "end_date": args.text_end_date or args.end_date,
                }
                if text_datasets
                else None,
                "intraday": {
                    "start_date": args.intraday_start_date,
                    "end_date": args.intraday_end_date,
                }
                if intraday_datasets
                else None,
            },
            "conclusions": [
                "Base research audit covers reference, daily market, and fundamental raw data.",
                "Reference/daily/fundamental raw files are structurally usable when errors are zero, but source and semantic warnings require PIT-aware snapshot construction.",
                "bak_basic and bak_daily are supplemental snapshots; neither should replace daily/daily_basic as the main daily market data source.",
                "Raw financial records are intentionally not deduplicated; choose report versions by availability date in the snapshot layer.",
                "Do not compare amount, market value, or share fields across interfaces until each field is normalized to a common unit.",
            ],
        },
    )
    counts = report["finding_counts"]
    status = report["status"]
    write_quality_report(output, report)
    print(f"audit status={status} errors={counts['error']} warnings={counts['warning']} output={output}")
    return 1 if counts["error"] else 0

def audit_intraday_only(args: argparse.Namespace) -> int:
    repo_root = Path.cwd().resolve()
    raw_dir = (repo_root / args.raw_dir).resolve()
    if getattr(args, "intraday_end_date", None) is None:
        args.intraday_end_date = date.today().strftime("%Y%m%d")
    output = (repo_root / (args.output or INTRADAY_MINUTES_STATUS_PATH)).resolve()
    findings: list[dict[str, Any]] = []

    def add(severity: str, check: str, message: str, details: dict[str, Any] | None = None) -> None:
        findings.append({"severity": severity, "check": check, "message": message, "details": details or {}})

    intraday_datasets = selected_integrated_intraday_datasets(args)
    audit_integrated_filesystem(raw_dir, intraday_datasets, add)
    if STK_MINS_DATASET in intraday_datasets:
        audit_stk_mins_completeness(raw_dir, args, add)

    report = build_quality_report(
        report_type="intraday_minutes",
        scope={
            "data_root": str(raw_dir),
            "start_date": args.intraday_start_date,
            "end_date": args.intraday_end_date,
            "datasets": intraday_datasets,
            "intraday_codes": getattr(args, "intraday_codes", None),
            "intraday_max_codes": getattr(args, "intraday_max_codes", None),
        },
        findings=findings,
        datasets=summarize_dataset_status(findings),
        metadata={
            "unit_rules": {
                STK_MINS_DATASET: {
                    "vol": "shares",
                    "amount": "CNY",
                    "available_at": "source trade_time in Asia/Shanghai; use as bar-close availability",
                    "auction_bars": "opening and closing auction are represented by 09:30 and 15:00 1-minute bars; no separate auction dataset is required for historical intraday minute",
                }
            },
            "doc_refs": {STK_MINS_DATASET: INTEGRATED_DOC_REFS[STK_MINS_DATASET]},
            "conclusions": [
                "Intraday minute data is stored as stock-year Parquet partitions and sidecar metadata under data/raw/stk_mins_1min.",
                "TuShare stk_mins uses shares for vol and CNY for amount; do not mix it with daily.amount or bak_daily.amount without unit conversion.",
                "Before stk_auction coverage begins, 09:30 minute rows remain the explicitly labelled opening-auction proxy; 15:00 close is the closing-auction clearing price.",
            ],
        },
    )
    counts = report["finding_counts"]
    status = report["status"]
    write_quality_report(output, report)
    print(f"intraday audit status={status} errors={counts['error']} warnings={counts['warning']} output={output}")
    return 1 if counts["error"] else 0


def add_base_parser(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser("base", help="audit base research data, optionally with text or source probes")
    core.add_raw_arg(parser)
    parser.add_argument("--start-date", default="20100101")
    parser.add_argument("--bak-start-date", default="20160101")
    parser.add_argument("--end-date")
    parser.add_argument("--fundamental-start-date", default="20100101")
    parser.add_argument("--fundamental-end-date")
    core.add_daily_selection_args(parser, core.DAILY_REQUIRED_DATASETS + core.DAILY_OPTIONAL_DATASETS)
    parser.add_argument("--fundamental-datasets", nargs="+", choices=core.FUNDAMENTAL_DATASETS, dest="fundamental_datasets")
    parser.add_argument("--include-intraday", action="store_true", dest="include_intraday", help="Include optional intraday minute datasets in a unified temporary audit.")
    parser.add_argument("--intraday-start-date", default="20200101")
    parser.add_argument("--intraday-end-date")
    parser.add_argument("--intraday-datasets", nargs="+", choices=core.INTRADAY_DATASETS + ["stk_mins"])
    parser.add_argument("--intraday-codes", nargs="+")
    parser.add_argument("--intraday-max-codes", type=int)
    parser.add_argument("--include-text", action="store_true", help="Include text in an explicitly named temporary combined audit.")
    parser.add_argument("--text-start-date", default="20100101")
    parser.add_argument("--text-end-date")
    parser.add_argument("--text-datasets", nargs="+", choices=core.TEXT_DATASETS, dest="text_datasets")
    parser.add_argument("--news-src", action="append", default=[])
    parser.add_argument("--major-news-src", action="append", default=[])
    parser.add_argument("--probe-api", action="store_true")
    parser.add_argument("--sample-limit", type=int, default=10)
    core.add_runtime_args(parser, min_interval=0.18, timeout=60)
    parser.add_argument("--output", help=f"Defaults to {core.BASE_RESEARCH_STATUS_PATH}; combined options require an explicit path.")

def add_intraday_parser(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser("intraday", help="audit stock-year intraday minute raw data")
    core.add_raw_arg(parser)
    parser.add_argument("--intraday-start-date", default="20200101")
    parser.add_argument("--intraday-end-date")
    parser.add_argument("--intraday-datasets", nargs="+", choices=core.INTRADAY_DATASETS + ["stk_mins"])
    parser.add_argument("--intraday-codes", nargs="+")
    parser.add_argument("--intraday-max-codes", type=int)
    parser.add_argument("--sample-limit", type=int, default=10)
    parser.add_argument("--output", help=f"Defaults to {core.INTRADAY_MINUTES_STATUS_PATH}.")

    by_date = sub.add_parser("intraday-by-date", help="audit final full-market daily minute files")
    core.add_intraday_by_date_common_args(by_date)
    by_date.add_argument("--full-scan", action="store_true")
    by_date.add_argument("--sample-limit", type=int, default=20)
    by_date.add_argument("--output", help=f"Defaults to {core.INTRADAY_MINUTES_STATUS_PATH}.")

    auction = sub.add_parser("auction-alignment", help="compare 09:30 minute auction bars with stk_auction and daily units")
    core.add_raw_arg(auction)
    auction.add_argument("--start-date", required=True)
    auction.add_argument("--end-date", required=True)
    auction.add_argument("--output-dataset", default=core.STK_MINS_BY_DATE_DATASET)
    auction.add_argument("--max-trade-dates", type=int, default=8, help="Use the latest N open dates in the requested window; <=0 means all.")
    auction.add_argument("--output", help="Defaults to results/data_quality/process/auction_alignment_status.json.")
    core.add_runtime_args(auction, min_interval=0.25, timeout=120)

def add_event_macro_parsers(sub: argparse._SubParsersAction) -> None:
    event = sub.add_parser("event-flow", help="audit only event/flow raw data")
    core.add_raw_arg(event)
    event.add_argument("--start-date", default="20200101")
    event.add_argument("--end-date", default=date.today().strftime("%Y%m%d"))
    event.add_argument("--datasets", nargs="+", choices=core.EVENT_FLOW_DATASETS)
    event.add_argument("--output", help=f"Defaults to {core.EVENT_FLOW_STATUS_PATH}.")

    macro = sub.add_parser("macro", help="audit macro, policy, and global-context raw data")
    core.add_raw_arg(macro)
    macro.add_argument("--start-date", default="20100101")
    macro.add_argument("--end-date", default=date.today().strftime("%Y%m%d"))
    macro.add_argument("--datasets", nargs="+", choices=core.MACRO_DATASETS)
    core.add_macro_filter_args(macro)
    macro.add_argument("--output", help=f"Defaults to {core.MACRO_CONTEXT_STATUS_PATH}.")

    text = sub.add_parser("text", help="audit only text-evidence raw data")
    core.add_raw_arg(text)
    text.add_argument("--start-date", dest="text_start_date", default="20100101")
    text.add_argument("--end-date", dest="text_end_date", default=date.today().strftime("%Y%m%d"))
    text.add_argument("--text-datasets", nargs="+", choices=core.TEXT_DATASETS, dest="text_datasets")
    text.add_argument("--news-src", action="append", default=[])
    text.add_argument("--major-news-src", action="append", default=[])
    text.add_argument("--output", help=f"Defaults to {core.TEXT_EVIDENCE_STATUS_PATH}.")

def add_board_parser(sub: argparse._SubParsersAction) -> None:
    board = sub.add_parser("board-trading", help="audit 打板专题 raw data")
    core.add_raw_arg(board)
    board.add_argument("--start-date", default="20200101")
    board.add_argument("--end-date", default=date.today().strftime("%Y%m%d"))
    board.add_argument("--datasets", nargs="+", choices=core.BOARD_TRADING_DATASETS)
    core.add_board_filter_args(board)
    board.add_argument("--output", help=f"Defaults to {core.BOARD_TRADING_STATUS_PATH}.")

def add_revision_parser(sub: argparse._SubParsersAction) -> None:
    revision = sub.add_parser("revision-sentinel", help="sample TuShare source partitions and compare them with local raw data without overwriting raw files")
    core.add_raw_arg(revision)
    revision.add_argument("--start-date", default="20200101")
    revision.add_argument("--end-date", default=date.today().strftime("%Y%m%d"))
    revision.add_argument("--datasets", nargs="+", choices=core.DAILY_REQUIRED_DATASETS + core.DAILY_OPTIONAL_DATASETS)
    revision.add_argument("--sample-size", type=int, default=12, help="Deterministic sample size per dataset; <=0 checks all dates.")
    revision.add_argument("--seed", help="Deterministic sampling seed. Defaults to --end-date.")
    revision.add_argument("--page-limit", type=int, default=core.TRADE_DATE_PAGE_LIMIT)
    revision.add_argument("--revision-ledger", default=core.REVISION_EVENTS_PATH)
    revision.add_argument("--output", default=core.REVISION_SUMMARY_PATH)
    revision.add_argument("--fail-on-revision", action="store_true", help="Return nonzero when source revisions are found.")
    core.add_runtime_args(revision, min_interval=0.22, timeout=120)

    history = sub.add_parser("revision-history-sample", help="yearly stratified source-vs-local checks for active trade-date partitioned TuShare interfaces")
    core.add_raw_arg(history)
    history.add_argument("--start-date", default="20200101")
    history.add_argument("--end-date", default=date.today().strftime("%Y%m%d"))
    history.add_argument("--sample-per-year", type=int, default=3, help="Deterministic trade-date sample count per year; <=0 checks all dates.")
    history.add_argument("--seed", help="Deterministic sampling seed. Defaults to --end-date.")
    history.add_argument("--groups", nargs="+", choices=["daily", "reference", "event_flow", "board_trading"], default=["daily", "reference", "event_flow", "board_trading"])
    history.add_argument("--daily-datasets", nargs="+", choices=core.DAILY_REQUIRED_DATASETS + core.DAILY_OPTIONAL_DATASETS)
    history.add_argument("--event-datasets", nargs="+", choices=core.EVENT_FLOW_DATASETS)
    history.add_argument("--board-datasets", nargs="+", choices=core.BOARD_TRADING_DATASETS)
    history.add_argument("--include-bak-basic", action=argparse.BooleanOptionalAction, default=True)
    history.add_argument("--page-limit", type=int)
    history.add_argument("--events-output", default=REVISION_HISTORY_SAMPLE_EVENTS_PATH)
    history.add_argument("--output", default=REVISION_HISTORY_SAMPLE_STATUS_PATH)
    history.add_argument("--fail-on-error", action="store_true", help="Return nonzero when API errors or required remote-zero responses are found.")
    core.add_board_filter_args(history)
    core.add_runtime_args(history, min_interval=0.22, timeout=120)

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    add_base_parser(sub)
    add_intraday_parser(sub)
    add_event_macro_parsers(sub)
    add_board_parser(sub)
    add_revision_parser(sub)
    return parser.parse_args()

def main() -> int:
    args = parse_args()
    if args.command == "base":
        return audit_unified(args)
    if args.command == "intraday":
        return audit_intraday_only(args)
    if args.command == "intraday-by-date":
        return audit_intraday_by_date(args)
    if args.command == "auction-alignment":
        return audit_auction_alignment(args)
    if args.command == "event-flow":
        return audit_event_flow_only(args)
    if args.command == "macro":
        return audit_macro_only(args)
    if args.command == "text":
        return audit_text_only(args)
    if args.command == "board-trading":
        return audit_board_trading_only(args)
    if args.command == "revision-history-sample":
        return audit_revision_history_sample(args)
    if args.command == "revision-sentinel":
        return audit_revision_sentinel(args)
    raise RuntimeError(f"unknown command {args.command}")

if __name__ == "__main__":
    raise SystemExit(main())
