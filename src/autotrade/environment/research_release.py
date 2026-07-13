"""Immutable, on-demand research inputs over the mutable live data lake.

The cron writer keeps ``data/raw`` as its live tree.  An experiment pins a
committed generation into a shared hardlink checkpoint, then stores a durable
experiment-local manifest.  Resume always reuses that manifest; a new
experiment started during ``updating``/``dirty`` uses the last complete release.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


DOMAIN_STATUS_FILES: dict[str, str] = {
    "daily": "base_research_status.json",
    "intraday_1min": "intraday_minutes_status.json",
    "events": "event_flow_status.json",
    "board_trading": "board_trading_status.json",
    "macro": "macro_context_status.json",
    "text": "text_evidence_status.json",
    "fundamentals": "fundamental_events_status.json",
}

_SCHEMA_VERSION = 2
_PIN_DIR_NAME = "research_release"
_MANIFEST_NAME = "manifest.json"
_RAW_GENERATION_NAME = ".raw_generation.json"
_LIVE_EXCLUDED_ROOTS = frozenset({"rt_min_live"})
_SAFE_GENERATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


@dataclass(frozen=True)
class ResearchRelease:
    raw_dir: Path
    fundamental_events_root: Path
    fundamental_events_status: Path
    generation_id: str


@dataclass(frozen=True)
class _GlobalRelease:
    root: Path
    raw_dir: Path
    fundamental_events_root: Path
    baseline_quality_dir: Path
    generation_id: str
    fundamental_status_name: str
    source_raw_dir: Path
    source_fundamental_events_root: Path
    source_quality_dir: Path


def pin_research_release(
    *,
    experiment_dir: str | Path,
    raw_dir: str | Path,
    fundamental_events_root: str | Path,
    fundamental_events_status: str | Path,
) -> ResearchRelease:
    """Pin immutable data inputs for one experiment.

    Production mode requires both the cron flock and generation marker.  Data
    roots without that pair retain live-path behavior for local/synthetic tests.
    """

    experiment_dir = Path(experiment_dir).resolve()
    raw_dir = Path(raw_dir).resolve()
    fundamental_events_root = Path(fundamental_events_root).resolve()
    fundamental_events_status = Path(fundamental_events_status).resolve()
    pin_dir = experiment_dir / _PIN_DIR_NAME
    pin_manifest = pin_dir / _MANIFEST_NAME
    experiment_dir.mkdir(parents=True, exist_ok=True)

    with _exclusive_flock(experiment_dir / f".{_PIN_DIR_NAME}.lock"):
        if pin_manifest.exists():
            return _load_experiment_pin(
                pin_manifest,
                raw_dir.parent / "research_releases",
                raw_dir=raw_dir,
                fundamental_events_root=fundamental_events_root,
                fundamental_events_status=fundamental_events_status,
            )

        update_lock = raw_dir.parent.parent / ".runtime" / "tushare" / "locks" / "tushare_update.lock"
        generation_path = raw_dir / _RAW_GENERATION_NAME
        if not update_lock.exists() and not generation_path.exists():
            return ResearchRelease(
                raw_dir=raw_dir,
                fundamental_events_root=fundamental_events_root,
                fundamental_events_status=fundamental_events_status,
                generation_id="",
            )
        if not update_lock.exists() or not generation_path.exists():
            raise RuntimeError(
                "production research inputs require both the updater lock and raw generation marker: "
                f"lock={update_lock.exists()} marker={generation_path.exists()}"
            )

        _reject_unpinned_legacy_experiment(experiment_dir)
        release_root = raw_dir.parent / "research_releases"
        release_root.mkdir(parents=True, exist_ok=True)

        try:
            observed = _read_generation(generation_path)
        except RuntimeError:
            observed = {}

        # Cron holds LOCK_EX before changing raw or PIT. Never queue behind a
        # long update: publish current committed data only when LOCK_SH wins.
        with _try_shared_flock(update_lock) as lock_acquired:
            if lock_acquired:
                generation = _read_generation(generation_path)
                try:
                    generation_id = _committed_generation_id(generation)
                except RuntimeError:
                    pass
                else:
                    with _exclusive_flock(release_root / ".registry.lock"):
                        shared = _load_global_release(release_root / generation_id)
                        if shared is None:
                            shared = _create_global_release(
                                release_root=release_root,
                                generation=generation,
                                raw_dir=raw_dir,
                                fundamental_events_root=fundamental_events_root,
                                quality_dir=fundamental_events_status.parent,
                                fundamental_status_name=fundamental_events_status.name,
                            )
                        else:
                            _assert_source_contract(
                                shared,
                                raw_dir,
                                fundamental_events_root,
                                fundamental_events_status,
                            )
                    return _publish_experiment_pin(
                        experiment_dir, shared, quality_source=fundamental_events_status.parent
                    )

        shared = _select_existing_release(
            release_root,
            preferred_generation=str(observed.get("generation_id") or ""),
            raw_dir=raw_dir,
            fundamental_events_root=fundamental_events_root,
            fundamental_events_status=fundamental_events_status,
        )
        if shared is None:
            raise RuntimeError(
                "live research data is unavailable and no immutable release exists; "
                "bootstrap one release while the generation is committed"
            )
        return _publish_experiment_pin(
            experiment_dir, shared, quality_source=shared.baseline_quality_dir
        )


def _reject_unpinned_legacy_experiment(experiment_dir: Path) -> None:
    ledger = experiment_dir / "ledgers" / "experiment_ledger.jsonl"
    if not ledger.exists():
        return
    try:
        populated = bool(ledger.read_text(encoding="utf-8").strip())
    except OSError as exc:
        raise RuntimeError(f"cannot inspect legacy experiment ledger: {ledger}: {exc}") from exc
    if populated:
        raise RuntimeError(
            "experiment has ledger records but no research-release pin; refusing to resume with a different "
            f"data generation: {ledger}"
        )


def _create_global_release(
    *,
    release_root: Path,
    generation: dict[str, object],
    raw_dir: Path,
    fundamental_events_root: Path,
    quality_dir: Path,
    fundamental_status_name: str,
) -> _GlobalRelease:
    generation_id = _committed_generation_id(generation)
    target = release_root / generation_id
    staging = release_root / f".{generation_id}.{uuid.uuid4().hex[:10]}.tmp"
    if target.exists():
        loaded = _load_global_release(target)
        if loaded is None:
            raise RuntimeError(f"research release target exists but is incomplete: {target}")
        return loaded
    try:
        staging.mkdir()
        _clone_tree(raw_dir, staging / "raw", kind="raw")
        _clone_tree(fundamental_events_root, staging / "fundamental_events", kind="pit")
        quality_hashes = _copy_quality_files(
            quality_dir,
            staging / "baseline_quality",
            fundamental_status_name=fundamental_status_name,
        )
        manifest = {
            "schema_version": _SCHEMA_VERSION,
            "kind": "research_release",
            "generation_id": generation_id,
            "created_at": _utc_now(),
            "raw_generation": generation,
            "fundamental_status_name": fundamental_status_name,
            "quality_sha256": quality_hashes,
            "source": {
                "raw_dir": str(raw_dir),
                "fundamental_events_root": str(fundamental_events_root),
                "quality_dir": str(quality_dir),
            },
        }
        _write_json(staging / _MANIFEST_NAME, manifest)
        staging.rename(target)
        published = _load_global_release(target)
        if published is None:  # Defensive: staging was fully validated before rename.
            raise RuntimeError(f"published research release failed validation: {target}")
        return published
    except Exception:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        raise


def _publish_experiment_pin(
    experiment_dir: Path,
    shared: _GlobalRelease,
    *,
    quality_source: Path,
) -> ResearchRelease:
    target = experiment_dir / _PIN_DIR_NAME
    if target.exists():
        raise RuntimeError(f"research-release pin directory already exists unexpectedly: {target}")

    staging = experiment_dir / f".{_PIN_DIR_NAME}.{uuid.uuid4().hex[:10]}.tmp"
    try:
        staging.mkdir()
        quality_hashes = _copy_quality_files(
            quality_source,
            staging / "quality",
            fundamental_status_name=shared.fundamental_status_name,
        )
        _write_json(
            staging / _MANIFEST_NAME,
            {
                "schema_version": _SCHEMA_VERSION,
                "kind": "experiment_research_release",
                "generation_id": shared.generation_id,
                "created_at": _utc_now(),
                "quality_sha256": quality_hashes,
            },
        )
        staging.rename(target)
        return _load_experiment_pin(
            target / _MANIFEST_NAME,
            shared.root.parent,
            raw_dir=shared.source_raw_dir,
            fundamental_events_root=shared.source_fundamental_events_root,
            fundamental_events_status=shared.source_quality_dir / shared.fundamental_status_name,
        )
    except Exception:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        raise


def _load_experiment_pin(
    manifest_path: Path,
    release_root: Path,
    *,
    raw_dir: Path,
    fundamental_events_root: Path,
    fundamental_events_status: Path,
) -> ResearchRelease:
    if manifest_path.is_symlink() or manifest_path.parent.is_symlink():
        raise RuntimeError(f"research-release pin must not use symlinks: {manifest_path}")
    payload = _read_json(manifest_path, description="experiment research-release manifest")
    if (
        payload.get("kind") != "experiment_research_release"
        or payload.get("schema_version") != _SCHEMA_VERSION
    ):
        raise RuntimeError(f"invalid research-release pin kind: {manifest_path}")
    generation_id = str(payload.get("generation_id") or "")
    _validate_generation_text(generation_id)
    expected_global = (release_root / generation_id / _MANIFEST_NAME).resolve()
    shared = _load_global_release(expected_global.parent)
    if shared is None:
        raise RuntimeError(f"pinned global research release is missing or incomplete: {expected_global.parent}")
    _assert_source_contract(
        shared,
        raw_dir,
        fundamental_events_root,
        fundamental_events_status,
    )

    quality_dir = manifest_path.parent / "quality"
    if quality_dir.is_symlink() or not quality_dir.is_dir():
        raise RuntimeError(f"pinned quality directory is missing or invalid: {quality_dir}")
    hashes = payload.get("quality_sha256")
    if not isinstance(hashes, dict):
        raise RuntimeError(f"research-release pin has no quality hashes: {manifest_path}")
    _verify_quality_hashes(quality_dir, hashes, shared.fundamental_status_name)
    status_path = quality_dir / shared.fundamental_status_name
    return ResearchRelease(
        raw_dir=shared.raw_dir,
        fundamental_events_root=shared.fundamental_events_root,
        fundamental_events_status=status_path,
        generation_id=generation_id,
    )


def _select_existing_release(
    release_root: Path,
    *,
    preferred_generation: str,
    raw_dir: Path,
    fundamental_events_root: Path,
    fundamental_events_status: Path,
) -> _GlobalRelease | None:
    if preferred_generation and _SAFE_GENERATION_ID.fullmatch(preferred_generation):
        preferred = _load_global_release(release_root / preferred_generation)
        if preferred is not None:
            _assert_source_contract(
                preferred,
                raw_dir,
                fundamental_events_root,
                fundamental_events_status,
            )
            return preferred
    candidates: list[tuple[str, Path]] = []
    for path in release_root.iterdir() if release_root.exists() else ():
        if not path.is_dir() or path.name.startswith("."):
            continue
        try:
            payload = _read_json(path / _MANIFEST_NAME, description="research-release manifest")
        except RuntimeError:
            continue
        if payload.get("kind") == "research_release":
            candidates.append((str(payload.get("created_at") or ""), path))
    for _, path in sorted(candidates, reverse=True):
        loaded = _load_global_release(path)
        if loaded is not None and _source_contract_matches(
            loaded,
            raw_dir,
            fundamental_events_root,
            fundamental_events_status,
        ):
            return loaded
    return None


def _load_global_release(path: Path) -> _GlobalRelease | None:
    manifest_path = path / _MANIFEST_NAME
    if (
        path.is_symlink()
        or not path.is_dir()
        or not manifest_path.is_file()
        or manifest_path.is_symlink()
    ):
        return None
    try:
        payload = _read_json(manifest_path, description="research-release manifest")
        if (
            payload.get("kind") != "research_release"
            or payload.get("schema_version") != _SCHEMA_VERSION
        ):
            return None
        generation_id = str(payload.get("generation_id") or "")
        _validate_generation_text(generation_id)
        if path.name != generation_id:
            return None
        raw_dir = path / "raw"
        fundamental_root = path / "fundamental_events"
        baseline_quality = path / "baseline_quality"
        roots = (raw_dir, fundamental_root, baseline_quality)
        if any(root.is_symlink() or not root.is_dir() for root in roots):
            return None
        generation = _read_generation(raw_dir / _RAW_GENERATION_NAME)
        if _committed_generation_id(generation) != generation_id:
            return None
        fundamental_status_name = str(
            payload.get("fundamental_status_name") or DOMAIN_STATUS_FILES["fundamentals"]
        )
        if Path(fundamental_status_name).name != fundamental_status_name:
            return None
        hashes = payload.get("quality_sha256")
        if not isinstance(hashes, dict):
            return None
        _verify_quality_hashes(baseline_quality, hashes, fundamental_status_name)
        source = payload.get("source")
        if not isinstance(source, dict):
            return None
        source_paths = tuple(
            Path(str(source.get(key) or ""))
            for key in ("raw_dir", "fundamental_events_root", "quality_dir")
        )
        if any(not source_path.is_absolute() for source_path in source_paths):
            return None
        return _GlobalRelease(
            root=path,
            raw_dir=raw_dir,
            fundamental_events_root=fundamental_root,
            baseline_quality_dir=baseline_quality,
            generation_id=generation_id,
            fundamental_status_name=fundamental_status_name,
            source_raw_dir=source_paths[0],
            source_fundamental_events_root=source_paths[1],
            source_quality_dir=source_paths[2],
        )
    except (OSError, RuntimeError, ValueError, TypeError):
        return None


def _source_contract_matches(
    shared: _GlobalRelease,
    raw_dir: Path,
    fundamental_events_root: Path,
    fundamental_events_status: Path,
) -> bool:
    return (
        shared.source_raw_dir == raw_dir.resolve()
        and shared.source_fundamental_events_root == fundamental_events_root.resolve()
        and shared.source_quality_dir == fundamental_events_status.resolve().parent
        and shared.fundamental_status_name == fundamental_events_status.name
    )


def _assert_source_contract(
    shared: _GlobalRelease,
    raw_dir: Path,
    fundamental_events_root: Path,
    fundamental_events_status: Path,
) -> None:
    if not _source_contract_matches(
        shared,
        raw_dir,
        fundamental_events_root,
        fundamental_events_status,
    ):
        raise RuntimeError(
            f"research release {shared.generation_id} was published for a different raw/PIT/status contract"
        )


def _clone_tree(source: Path, destination: Path, *, kind: str) -> None:
    if not source.is_dir() or source.is_symlink():
        raise RuntimeError(f"research-release source must be a real directory: {source}")
    destination.mkdir(parents=True)
    _clone_directory(source, destination, kind=kind, top_level=True)


def _clone_directory(
    source: Path,
    destination: Path,
    *,
    kind: str,
    top_level: bool,
) -> None:
    parquets: set[str] = set()
    metadata_targets: set[str] = set()
    with os.scandir(source) as entries:
        for entry in entries:
            if kind == "raw" and top_level and entry.name in _LIVE_EXCLUDED_ROOTS:
                continue
            if _is_temporary_name(entry.name):
                raise RuntimeError(
                    f"temporary file/directory found while creating research release: {source / entry.name}"
                )
            mode = entry.stat(follow_symlinks=False).st_mode
            src = source / entry.name
            dst = destination / entry.name
            if stat.S_ISLNK(mode):
                raise RuntimeError(f"symbolic link is forbidden in a research release: {src}")
            if stat.S_ISDIR(mode):
                dst.mkdir()
                _clone_directory(src, dst, kind=kind, top_level=False)
            elif stat.S_ISREG(mode):
                if kind == "raw" and entry.name.endswith(".parquet.meta.json"):
                    metadata_targets.add(entry.name[: -len(".meta.json")])
                elif kind == "raw" and entry.name.endswith(".parquet"):
                    parquets.add(entry.name)
                if _should_hardlink(entry.name, kind=kind):
                    os.link(src, dst)
                else:
                    shutil.copy2(src, dst, follow_symlinks=False)
            else:
                raise RuntimeError(f"special file is forbidden in a research release: {src}")
    if kind == "raw":
        _validate_raw_pairs(source, parquets, metadata_targets)


def _should_hardlink(name: str, *, kind: str) -> bool:
    if kind == "raw":
        return name.endswith(".parquet") or name.endswith(".parquet.meta.json")
    if kind == "pit":
        return name.endswith(".parquet")
    raise ValueError(f"unknown research-release tree kind: {kind}")


def _is_temporary_name(name: str) -> bool:
    return ".tmp" in name.lower() or name.lower() == "tmp"


def _validate_raw_pairs(source: Path, parquets: set[str], metadata_targets: set[str]) -> None:
    missing_meta = sorted(parquets - metadata_targets, key=str)
    orphan_meta = sorted(metadata_targets - parquets, key=str)
    if missing_meta or orphan_meta:
        raise RuntimeError(
            f"raw parquet/metadata pairing failed under {source}: "
            f"missing_meta={missing_meta[:10]} orphan_meta={orphan_meta[:10]}"
        )


def _copy_quality_files(
    source: Path,
    destination: Path,
    *,
    fundamental_status_name: str,
) -> dict[str, str | None]:
    destination.mkdir(parents=True, exist_ok=True)
    hashes: dict[str, str | None] = {}
    for name in _quality_names(fundamental_status_name):
        src = source / name
        dst = destination / name
        if src.is_symlink():
            raise RuntimeError(f"quality status must not be a symlink: {src}")
        if not src.exists():
            hashes[name] = None
            continue
        mode = src.lstat().st_mode
        if not stat.S_ISREG(mode):
            raise RuntimeError(f"quality status must be a regular non-symlink file: {src}")
        shutil.copy2(src, dst, follow_symlinks=False)
        hashes[name] = _file_sha256(dst)
    return hashes


def _verify_quality_hashes(
    directory: Path,
    hashes: dict[object, object],
    fundamental_status_name: str,
) -> None:
    if {str(name) for name in hashes} != set(_quality_names(fundamental_status_name)):
        raise RuntimeError(f"research-release quality file set is invalid: {directory}")
    for raw_name, raw_expected in hashes.items():
        name = str(raw_name)
        if Path(name).name != name:
            raise RuntimeError(f"invalid quality filename in research-release manifest: {name!r}")
        expected = None if raw_expected is None else str(raw_expected)
        path = directory / name
        if path.is_symlink():
            raise RuntimeError(f"pinned quality file must not be a symlink: {path}")
        if expected is None:
            if path.exists():
                raise RuntimeError(f"unexpected quality file in research release: {path}")
        elif not path.is_file() or path.is_symlink():
            raise RuntimeError(f"pinned quality file is missing or invalid: {path}")
        elif _file_sha256(path) != expected:
            raise RuntimeError(f"pinned quality file hash mismatch: {path}")


def _quality_names(fundamental_status_name: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys((*DOMAIN_STATUS_FILES.values(), fundamental_status_name)))


def _committed_generation_id(generation: dict[str, object]) -> str:
    if generation.get("schema_version") != 2 or generation.get("state") != "committed":
        raise RuntimeError("raw generation is not an explicit schema-v2 committed record")
    generation_id = str(generation.get("generation_id") or "")
    _validate_generation_text(generation_id)
    return generation_id


def _validate_generation_text(generation_id: str) -> None:
    if not _SAFE_GENERATION_ID.fullmatch(generation_id):
        raise RuntimeError(f"invalid raw generation id for research release: {generation_id!r}")


def _read_generation(path: Path) -> dict[str, object]:
    return _read_json(path, description="raw generation record")


def _read_json(path: Path, *, description: str) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid {description}: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"invalid {description}: expected object at {path}")
    return payload


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def _exclusive_flock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def _try_shared_flock(path: Path) -> Iterator[bool]:
    with path.open("rb") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
