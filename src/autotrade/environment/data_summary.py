"""Agent-visible snapshot summaries built without full-table reads."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Mapping

from autotrade.environment.runtime import utc_now_iso

LARGE_TABLE_ROW_THRESHOLD = 1_000_000
LARGE_TABLE_SIZE_THRESHOLD_BYTES = 100 * 1024 * 1024
LARGE_TABLE_NAMES = {"events.parquet", "text_index.parquet", "intraday_1min.parquet"}
DATE_COLUMNS = ("trade_date", "date", "available_at", "trade_time", "ann_date", "end_date")
NULL_COUNT_COLUMNS = (
    "ts_code",
    "trade_date",
    "available_at",
    "open",
    "high",
    "low",
    "close",
    "amount",
    "vol",
    "dataset",
    "text_id",
)
KEY_COLUMN_NAMES = {
    *NULL_COUNT_COLUMNS,
    "pre_close",
    "change",
    "pct_chg",
    "turnover_rate",
    "turnover_rate_f",
    "pe",
    "pe_ttm",
    "pb",
    "ps",
    "ps_ttm",
    "total_mv",
    "circ_mv",
    "adj_factor",
    "up_limit",
    "down_limit",
    "is_suspended",
    "ann_date",
    "f_ann_date",
    "end_date",
    "title",
    "content",
    "summary",
    "source",
    "name",
    "rzye",
    "rzmre",
    "rqye",
    "rqmcl",
    "buy_lg_amount",
    "sell_lg_amount",
    "buy_elg_amount",
    "sell_elg_amount",
}
LARGE_TABLE_GUIDANCE = (
    "本 data_summary.json 是轻量索引，只保留文件规模、行数、关键列和日期覆盖；可整文件 cat 读取。",
    "只有主决策视图 snapshot 给出关键列与空值；train/valid 仅给规模与日期覆盖（schema 与 snapshot 一致）。",
    "需要完整 schema、行组、空值或更细字段时，先查 snapshot manifest 或 Parquet metadata。",
    "events.parquet、text_index.parquet、intraday_1min.parquet 通常是大表；需要抽样或聚合时，用 DuckDB count/limit、pyarrow 或 pandas 按列/日期过滤读取。",
    "不要对未知规模大表直接 pd.read_parquet() 全量读取；需要 pandas 时先限制列、过滤日期或抽样。",
)
PRIMARY_VIEW_NAME = "snapshot"
HOST_PATH_RE = re.compile(r"(?<![\w.:\-/])/(?!mnt/)(?:[^\s'\";|,)]*)")


def write_agent_data_summary(
    output_path: str | Path,
    *,
    kind: str,
    fold_id: str | None,
    views: Mapping[str, tuple[Path, str]],
) -> dict[str, object]:
    """Write `/mnt/artifacts/data_summary.json` before Agent starts.

    `views` maps a logical name such as `snapshot` or `valid` to
    `(host_snapshot_dir, sandbox_mount_path)`. The summary intentionally
    exposes only sandbox mount paths, not host filesystem paths.
    """
    summary: dict[str, object] = {
        "generated_at": utc_now_iso(),
        "kind": kind,
        "fold_id": fold_id,
        "large_table_guidance": list(LARGE_TABLE_GUIDANCE),
        "views": {},
    }
    primary = PRIMARY_VIEW_NAME if PRIMARY_VIEW_NAME in views else next(iter(views), None)
    for name, (view_dir, mount_path) in views.items():
        summary["views"][name] = _snapshot_view_summary(
            Path(view_dir), mount_path, detailed=(name == primary)
        )
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # Compact (un-indented) JSON keeps this index a single cat-able, low-token
    # read; use jq for ad-hoc human formatting.
    output_path.write_text(
        json.dumps(summary, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    return summary


def _snapshot_view_summary(view_dir: Path, mount_path: str, *, detailed: bool = True) -> dict[str, object]:
    manifest = _read_json(view_dir / "manifest.json")
    files = [
        _parquet_file_summary(path, view_dir, mount_path, detailed=detailed)
        for path in sorted(view_dir.rglob("*.parquet"))
    ]
    large_files = [
        item
        for item in files
        if item.get("large_table") or Path(str(item.get("path", ""))).name in LARGE_TABLE_NAMES
    ]
    return {
        "mount_path": mount_path,
        "kind": manifest.get("kind"),
        "decision_time": manifest.get("decision_time") or manifest.get("decision_date"),
        "period_start": manifest.get("period_start"),
        "period_end": manifest.get("period_end"),
        "window_config": manifest.get("window_config"),
        "domain_windows": manifest.get("domain_windows"),
        "domains": _compact_domains(manifest.get("domains")),
        "files": files,
        "large_tables": [str(item.get("path")) for item in large_files],
    }


def _parquet_file_summary(path: Path, root: Path, mount_root: str, *, detailed: bool = True) -> dict[str, object]:
    relpath = path.relative_to(root).as_posix()
    record: dict[str, object] = {
        "path": f"{relpath}",
        "mount_path": f"{mount_root.rstrip('/')}/{relpath}",
        "size_bytes": int(path.stat().st_size),
    }
    try:
        import pyarrow.parquet as pq

        parquet = pq.ParquetFile(path)
        metadata = parquet.metadata
        columns = [str(name) for name in parquet.schema_arrow.names]
        rows = int(metadata.num_rows)
        record["rows"] = rows
        # Schema highlights and null counts are emitted only for the primary
        # (decision-input) view; train/valid share the schema, so they stay a
        # compact size/date-coverage index that fits a single cat read.
        if detailed:
            record["column_count"] = len(columns)
            record["key_columns"] = [name for name in columns if name in KEY_COLUMN_NAMES]
        ranges = _metadata_ranges(metadata, columns)
        if ranges:
            record["date_ranges"] = ranges
        if detailed:
            null_counts = _metadata_null_counts(metadata, columns)
            if null_counts:
                record["metadata_null_counts"] = null_counts
    except Exception as exc:  # pragma: no cover - defensive for malformed local artifacts
        record["metadata_error"] = _metadata_error_message(exc, path=path, root=root, mount_path=record["mount_path"])
        rows = None
    size = int(record["size_bytes"])
    row_count = int(record["rows"]) if isinstance(record.get("rows"), int) else 0
    record["large_table"] = (
        path.name in LARGE_TABLE_NAMES
        or row_count >= LARGE_TABLE_ROW_THRESHOLD
        or size >= LARGE_TABLE_SIZE_THRESHOLD_BYTES
    )
    return record


def _metadata_ranges(metadata, columns: list[str]) -> dict[str, dict[str, str]]:
    ranges: dict[str, dict[str, str]] = {}
    column_index = {name: index for index, name in enumerate(columns)}
    for name in DATE_COLUMNS:
        if name not in column_index:
            continue
        mins: list[str] = []
        maxs: list[str] = []
        index = column_index[name]
        for group_index in range(metadata.num_row_groups):
            stats = metadata.row_group(group_index).column(index).statistics
            if stats is None or not stats.has_min_max:
                continue
            mins.append(_scalar_to_text(stats.min))
            maxs.append(_scalar_to_text(stats.max))
        if mins and maxs:
            ranges[name] = {"min": min(mins), "max": max(maxs)}
    return ranges


def _metadata_null_counts(metadata, columns: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    column_index = {name: index for index, name in enumerate(columns)}
    for name in NULL_COUNT_COLUMNS:
        if name not in column_index:
            continue
        total = 0
        seen = False
        index = column_index[name]
        for group_index in range(metadata.num_row_groups):
            stats = metadata.row_group(group_index).column(index).statistics
            if stats is None or stats.null_count is None:
                continue
            seen = True
            total += int(stats.null_count)
        if seen:
            counts[name] = total
    return counts


def _scalar_to_text(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _metadata_error_message(exc: Exception, *, path: Path, root: Path, mount_path: object) -> str:
    message = f"{type(exc).__name__}: {exc}"
    try:
        message = message.replace(str(path.resolve()), str(mount_path))
        message = message.replace(str(root.resolve()), str(Path(str(mount_path)).parent))
    except OSError:
        pass
    return HOST_PATH_RE.sub("[host_path]", message)


def _compact_domains(domains: object) -> dict[str, dict[str, object]]:
    if not isinstance(domains, dict):
        return {}
    compact: dict[str, dict[str, object]] = {}
    for name, value in domains.items():
        if not isinstance(value, dict):
            continue
        compact[str(name)] = {
            key: value.get(key)
            for key in ("rows", "datasets", "coverage_start", "coverage_end", "files", "skipped")
            if key in value
        }
    return compact


def _read_json(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
