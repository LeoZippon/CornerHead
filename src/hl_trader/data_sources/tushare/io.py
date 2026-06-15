"""TuShare raw-file IO helpers."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow.parquet as pq


def write_parquet(
    path: Path,
    df: pd.DataFrame,
    *,
    api_name: str,
    params: dict[str, Any],
    fields: list[str],
    source_hash: str,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    df.to_parquet(tmp, index=False)
    os.replace(tmp, path)
    meta = {
        "api_name": api_name,
        "params": params,
        "fields": fields,
        "row_count": int(len(df)),
        "source_hash": source_hash,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "format": "parquet",
    }
    path.with_suffix(path.suffix + ".meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def parquet_meta(path: Path) -> dict[str, Any]:
    meta_path = path.with_suffix(path.suffix + ".meta.json")
    if not meta_path.exists():
        return {}
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def read_many(files: list[Path], columns: list[str] | None = None) -> pd.DataFrame:
    frames = [pd.read_parquet(path, columns=columns) for path in files]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def parquet_rows(path: Path) -> int:
    return pq.ParquetFile(path).metadata.num_rows


def has_pagination_probe(path: Path) -> bool:
    pagination = (parquet_meta(path).get("params") or {}).get("pagination") or {}
    return int(pagination.get("pages") or 0) > 1
