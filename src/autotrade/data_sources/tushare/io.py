"""TuShare raw-file IO helpers."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow.parquet as pq


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_parquet(
    path: Path,
    df: pd.DataFrame,
    *,
    api_name: str,
    params: dict[str, Any],
    fields: list[str],
    source_hash: str,
) -> None:
    """Write the parquet and its sidecar as a verifiable pair.

    ``parquet_sha256`` is computed over the staged parquet bytes before either
    file is published, and both writes go through tmp + ``os.replace``, so a
    crash between them leaves at worst a hash-mismatched pair that the data
    audit detects — never a silently wrong (new parquet, old sidecar) combo.
    ``source_hash`` stays the API-response content hash; it does not prove the
    on-disk bytes, which is exactly what ``parquet_sha256`` adds."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    df.to_parquet(tmp, index=False)
    meta = {
        "api_name": api_name,
        "params": params,
        "fields": fields,
        "row_count": int(len(df)),
        "source_hash": source_hash,
        "parquet_sha256": file_sha256(tmp),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "format": "parquet",
    }
    os.replace(tmp, path)
    meta_path = path.with_suffix(path.suffix + ".meta.json")
    meta_tmp = meta_path.with_suffix(meta_path.suffix + ".tmp")
    meta_tmp.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False),
        encoding="utf-8",
    )
    os.replace(meta_tmp, meta_path)


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
