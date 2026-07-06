"""Experiment discovery and read-model assembly for the HITL console.

Everything here is read-only over ``experiments/<id>/``: the append-only
ledger, the hitl/ control-plane files, and frozen artifacts. Legacy
experiments (no hitl/ directory, e.g. pre-console CLI runs) are listed
best-effort and marked read-only; unparseable ones still appear so they can be
deleted, they just carry an error note instead of metrics.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Mapping

from autotrade.pipelines.fold_analysis import analysis_paths
from autotrade.pipelines.interactive import (
    ANALYSIS_DIR_NAME,
    CONTROL_NAME,
    HITL_DIR_NAME,
    PARAMS_NAME,
    SCHEDULE_NAME,
    STATUS_NAME,
    read_control,
    read_json,
    read_status,
    status_pid_alive,
)

ACTIVE_STATES = ("starting", "running_session", "waiting_user", "paused")
# Fold-record fields whose content is test-period evidence (guarded view).
TEST_FIELDS = ("test_result",)


def _read_ledger_records(experiment_dir: Path) -> list[dict[str, object]]:
    path = experiment_dir / "ledgers" / "experiment_ledger.jsonl"
    if not path.exists():
        return []
    records: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def latest_fold_records(records: list[dict[str, object]]) -> dict[tuple[str, str], dict[str, object]]:
    latest: dict[tuple[str, str], dict[str, object]] = {}
    for record in records:
        if record.get("record_type") == "fold":
            latest[(str(record.get("epoch_id")), str(record.get("fold_id")))] = record
    return latest


def _compound(returns: list[float]) -> float | None:
    if not returns:
        return None
    total = 1.0
    for value in returns:
        total *= 1.0 + value
    return total - 1.0


def _metric_series(records: list[dict[str, object]], result_key: str, metric: str) -> list[float]:
    values: list[float] = []
    for record in records:
        result = record.get(result_key)
        if isinstance(result, Mapping):
            value = result.get(metric)
            if isinstance(value, (int, float)) and math.isfinite(float(value)):
                values.append(float(value))
    return values


def experiment_state(experiment_dir: Path) -> dict[str, object]:
    """Effective lifecycle state combining status.json and pid liveness."""
    hitl_dir = experiment_dir / HITL_DIR_NAME
    if not hitl_dir.is_dir():
        return {"kind": "legacy", "state": "legacy", "worker_alive": False}
    status = read_status(hitl_dir / STATUS_NAME)
    if not status:
        return {"kind": "hitl", "state": "created", "worker_alive": False}
    state = str(status.get("state") or "unknown")
    alive = status_pid_alive(status)
    if state in ACTIVE_STATES and not alive:
        state = "interrupted"
    return {"kind": "hitl", "state": state, "worker_alive": alive, "status": status}


def summarize_experiment(experiment_dir: Path) -> dict[str, object]:
    summary: dict[str, object] = {
        "experiment_id": experiment_dir.name,
        "path": str(experiment_dir),
    }
    try:
        state = experiment_state(experiment_dir)
        summary.update(state)
        records = _read_ledger_records(experiment_dir)
        folds = list(latest_fold_records(records).values())
        folds.sort(key=lambda record: (str(record.get("epoch_id")), str(record.get("test_period") or record.get("fold_id"))))
        heldout = [record for record in records if record.get("record_type") == "heldout"]
        meta = [record for record in records if record.get("record_type") == "meta_learning"]
        schedule = read_json(experiment_dir / HITL_DIR_NAME / SCHEDULE_NAME)
        sessions = schedule.get("sessions") if isinstance(schedule.get("sessions"), list) else None
        completed_sessions = len(folds) + len({str(m.get("epoch_id")) for m in meta}) + (1 if heldout else 0)
        status = state.get("status") or {}
        summary.update(
            {
                "folds_recorded": len(folds),
                "meta_recorded": len(meta),
                "heldout_recorded": len(heldout),
                "total_sessions": (status.get("total_sessions") if isinstance(status, Mapping) else None)
                or (len(sessions) if sessions else None),
                "completed_sessions": (status.get("completed_sessions") if isinstance(status, Mapping) else None)
                or completed_sessions,
                "current_session": status.get("session_key") if isinstance(status, Mapping) else None,
                "metrics": {
                    "cum_valid_return": _compound(_metric_series(folds, "validation_result", "total_return")),
                    "cum_test_return": _compound(_metric_series(folds, "test_result", "total_return")),
                    "mean_test_sharpe": _mean(_metric_series(folds, "test_result", "sharpe")),
                    "cum_heldout_return": _compound(_metric_series(heldout, "test_result", "total_return")),
                },
                "fold_returns": [
                    {
                        "epoch_id": record.get("epoch_id"),
                        "fold_id": record.get("fold_id"),
                        "fold_status": record.get("fold_status"),
                        "valid_return": _first_metric(record, "validation_result", "total_return"),
                        "test_return": _first_metric(record, "test_result", "total_return"),
                    }
                    for record in folds
                ],
                "created_at": _created_at(experiment_dir),
            }
        )
    except Exception as exc:  # noqa: BLE001 - unreadable experiments stay listed for deletion
        summary.update({"kind": summary.get("kind", "legacy"), "state": "unreadable", "error": f"{type(exc).__name__}: {exc}"})
    return summary


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _first_metric(record: Mapping[str, object], result_key: str, metric: str) -> float | None:
    result = record.get(result_key)
    if isinstance(result, Mapping):
        value = result.get(metric)
        if isinstance(value, (int, float)) and math.isfinite(float(value)):
            return float(value)
    return None


def _created_at(experiment_dir: Path) -> str | None:
    params = read_json(experiment_dir / HITL_DIR_NAME / PARAMS_NAME)
    created = params.get("_created_at")
    if created:
        return str(created)
    try:
        import datetime

        return datetime.datetime.fromtimestamp(experiment_dir.stat().st_mtime).isoformat(timespec="seconds")
    except OSError:
        return None


def list_experiments(experiments_root: Path) -> list[dict[str, object]]:
    root = Path(experiments_root)
    if not root.is_dir():
        return []
    entries = [entry for entry in root.iterdir() if entry.is_dir() and not entry.name.startswith(".")]
    summaries = [summarize_experiment(entry) for entry in sorted(entries)]
    summaries.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return summaries


def experiment_detail(experiments_root: Path, experiment_id: str) -> dict[str, object]:
    experiment_dir = resolve_experiment_dir(experiments_root, experiment_id)
    detail = summarize_experiment(experiment_dir)
    hitl_dir = experiment_dir / HITL_DIR_NAME
    records = _read_ledger_records(experiment_dir)
    fold_map = latest_fold_records(records)
    meta_map = {
        str(record.get("epoch_id")): record for record in records if record.get("record_type") == "meta_learning"
    }
    heldout_records = [record for record in records if record.get("record_type") == "heldout"]
    schedule = read_json(hitl_dir / SCHEDULE_NAME)
    sessions_plan = schedule.get("sessions") if isinstance(schedule.get("sessions"), list) else []
    control = read_control(hitl_dir / CONTROL_NAME) if (hitl_dir / CONTROL_NAME).exists() else None
    sessions: list[dict[str, object]] = []
    if sessions_plan:
        for planned in sessions_plan:
            entry = dict(planned)
            kind = str(entry.get("kind"))
            epoch_id = str(entry.get("epoch_id"))
            if kind == "fold":
                record = fold_map.get((epoch_id, str(entry.get("fold_id"))))
                if record is not None:
                    entry["record"] = guarded_fold_view(record)
                    entry["analysis_available"] = analysis_available(hitl_dir, epoch_id, str(entry.get("fold_id")))
            elif kind == "meta_learning":
                record = meta_map.get(epoch_id)
                if record is not None:
                    entry["record"] = _public_meta_view(record)
            elif kind == "heldout":
                if heldout_records:
                    entry["records"] = [_public_heldout_view(record) for record in heldout_records]
            sessions.append(entry)
    else:
        # Legacy experiment: synthesize session rows from ledger records only.
        for record in records:
            kind = str(record.get("record_type"))
            entry: dict[str, object] = {
                "key": f"{record.get('epoch_id')}/{record.get('fold_id')}",
                "kind": kind,
                "epoch_id": record.get("epoch_id"),
                "fold_id": record.get("fold_id"),
            }
            if kind == "fold":
                entry["record"] = guarded_fold_view(record)
                for field in ("validation_period", "test_period", "input_window"):
                    entry[field] = record.get(field)
            elif kind == "meta_learning":
                entry["record"] = _public_meta_view(record)
            else:
                entry["record"] = _public_heldout_view(record)
            sessions.append(entry)
    detail.update(
        {
            "sessions": sessions,
            "control": control.to_record() if control is not None else None,
            "params": _public_params(read_json(hitl_dir / PARAMS_NAME)),
            "heldout_records": [_public_heldout_view(record) for record in heldout_records],
        }
    )
    return detail


def fold_detail(experiments_root: Path, experiment_id: str, epoch_id: str, fold_id: str) -> dict[str, object]:
    experiment_dir = resolve_experiment_dir(experiments_root, experiment_id)
    records = _read_ledger_records(experiment_dir)
    record = latest_fold_records(records).get((epoch_id, fold_id))
    if record is None:
        raise KeyError(f"no fold record for {epoch_id}/{fold_id}")
    hitl_dir = experiment_dir / HITL_DIR_NAME
    strategy_dir = record.get("frozen_strategy_artifact_path")
    files: list[dict[str, object]] = []
    if strategy_dir and Path(str(strategy_dir)).is_dir():
        root = Path(str(strategy_dir))
        for path in sorted(root.rglob("*")):
            if path.is_file():
                files.append({"path": str(path.relative_to(root)), "bytes": path.stat().st_size})
    md_path, meta_path = analysis_paths(hitl_dir / ANALYSIS_DIR_NAME, epoch_id, fold_id)
    return {
        "experiment_id": experiment_id,
        "epoch_id": epoch_id,
        "fold_id": fold_id,
        "record": guarded_fold_view(record),
        # Guarded test view: test-period evidence rides in a separate, clearly
        # labelled block the UI keeps collapsed with a leakage warning.
        "test_audit": {field: record.get(field) for field in TEST_FIELDS},
        "strategy_files": files,
        "strategy_dir": str(strategy_dir) if strategy_dir else None,
        "analysis": {
            "available": md_path.exists(),
            "meta": read_json(meta_path) if meta_path.exists() else None,
        },
        "run_id": record.get("run_id"),
    }


def guarded_fold_view(record: Mapping[str, object]) -> dict[str, object]:
    """Fold record minus test-period evidence (shown separately, labelled)."""
    return {key: value for key, value in record.items() if key not in TEST_FIELDS}


def _public_meta_view(record: Mapping[str, object]) -> dict[str, object]:
    view = dict(record)
    taste_path = record.get("taste_path")
    if taste_path and Path(str(taste_path)).exists():
        view["taste"] = Path(str(taste_path)).read_text(encoding="utf-8")
    return view


def _public_heldout_view(record: Mapping[str, object]) -> dict[str, object]:
    return dict(record)


def _public_params(params: Mapping[str, object]) -> dict[str, object]:
    return {key: value for key, value in params.items()}


def analysis_available(hitl_dir: Path, epoch_id: str, fold_id: str) -> bool:
    md_path, _meta = analysis_paths(hitl_dir / ANALYSIS_DIR_NAME, epoch_id, fold_id)
    return md_path.exists()


def resolve_experiment_dir(experiments_root: Path, experiment_id: str) -> Path:
    root = Path(experiments_root).resolve()
    candidate = (root / experiment_id).resolve()
    if candidate.parent != root or not experiment_id or experiment_id.startswith("."):
        raise ValueError(f"invalid experiment id: {experiment_id!r}")
    if not candidate.is_dir():
        raise KeyError(f"unknown experiment: {experiment_id}")
    return candidate
