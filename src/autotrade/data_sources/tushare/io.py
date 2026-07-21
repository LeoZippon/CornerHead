"""TuShare raw-file IO helpers."""

from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from fcntl import LOCK_EX, LOCK_UN, flock
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow.parquet as pq

# The sidecar read contract is owned by the environment's PIT layer.
from autotrade.environment.data.pit import concat_rows, parquet_meta


_unique_jsonl_lock = threading.Lock()
_unique_jsonl_state: dict[tuple[Path, str], tuple[int, int, int, set[str]]] = {}


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
    extra_metadata: dict[str, Any] | None = None,
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
    meta_path = path.with_suffix(path.suffix + ".meta.json")
    previous_meta: dict[str, Any] = {}
    if meta_path.exists():
        try:
            previous_meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            previous_meta = {}
    fetched_at = datetime.now(timezone.utc).isoformat()
    parquet_sha256 = file_sha256(tmp)
    meta = {
        "api_name": api_name,
        "params": params,
        "fields": fields,
        "row_count": int(len(df)),
        "source_hash": source_hash,
        "parquet_sha256": parquet_sha256,
        "fetched_at": fetched_at,
        "format": "parquet",
    }
    if extra_metadata:
        meta.update(extra_metadata)
    # Preserve first-landing evidence only while the payload is byte-identical.
    # A source revision was not knowable at the old timestamp, so a caller that
    # does not provide fresh evidence is conservatively visible from this fetch.
    previous_availability = previous_meta.get("availability")
    if previous_availability and previous_meta.get("parquet_sha256") == parquet_sha256:
        meta["availability"] = previous_availability
    elif previous_availability and "availability" not in (extra_metadata or {}):
        revised = dict(previous_availability)
        revised.update(
            {
                "available_at": fetched_at,
                "rule": "observed:content_revision_fetch",
                "row_count": int(len(df)),
                "content_hash": parquet_sha256,
            }
        )
        meta["availability"] = revised
    os.replace(tmp, path)
    meta_tmp = meta_path.with_suffix(meta_path.suffix + ".tmp")
    meta_tmp.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False),
        encoding="utf-8",
    )
    os.replace(meta_tmp, meta_path)


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def append_jsonl_unique(path: Path, payload: dict[str, Any], *, key: str) -> bool:
    """Append once per stable record key, without rescanning on every write."""
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"unique JSONL record requires a non-empty string {key!r}")
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    cache_key = (path, key)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n"
    with _unique_jsonl_lock, path.open("a+", encoding="utf-8") as handle:
        flock(handle.fileno(), LOCK_EX)
        try:
            stat = os.fstat(handle.fileno())
            state = _unique_jsonl_state.get(cache_key)
            if state is None or state[:2] != (stat.st_dev, stat.st_ino) or stat.st_size < state[2]:
                offset, values = 0, set()
            else:
                offset, values = state[2], state[3]
            handle.seek(offset)
            while line := handle.readline():
                try:
                    existing = json.loads(line).get(key)
                except (json.JSONDecodeError, AttributeError):
                    continue
                if isinstance(existing, str) and existing:
                    values.add(existing)
            offset = os.fstat(handle.fileno()).st_size
            if value in values:
                _unique_jsonl_state[cache_key] = (stat.st_dev, stat.st_ino, offset, values)
                return False
            handle.write(encoded)
            handle.flush()
            values.add(value)
            offset = os.fstat(handle.fileno()).st_size
            _unique_jsonl_state[cache_key] = (stat.st_dev, stat.st_ino, offset, values)
            return True
        finally:
            flock(handle.fileno(), LOCK_UN)


def read_many(files: list[Path], columns: list[str] | None = None) -> pd.DataFrame:
    frames = [pd.read_parquet(path, columns=columns) for path in files]
    return concat_rows(frames, ignore_index=True) if frames else pd.DataFrame()


def parquet_rows(path: Path) -> int:
    return pq.ParquetFile(path).metadata.num_rows


def has_pagination_probe(path: Path) -> bool:
    pagination = (parquet_meta(path).get("params") or {}).get("pagination") or {}
    return int(pagination.get("pages") or 0) > 1
