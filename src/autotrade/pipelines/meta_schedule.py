"""Pure helpers for deterministic within-Epoch meta-learning cadence."""

from __future__ import annotations

from collections.abc import Mapping


def meta_learning_trigger_counts(fold_count: int, interval: int) -> tuple[int, ...]:
    """Completed-Fold counts that trigger Meta before the next Fold.

    Zero is the mandatory Epoch-start session. A positive interval adds
    periodic sessions, but never a useless session after the final Fold.
    """

    if fold_count <= 0:
        return ()
    periodic = range(interval, fold_count, interval) if interval > 0 else ()
    return (0, *periodic)


def meta_learning_id(epoch_id: str, trigger_after_folds: int = 0) -> str:
    """Stable ledger/artifact identity; Epoch-start keeps the legacy name."""

    if trigger_after_folds <= 0:
        return epoch_id
    return f"{epoch_id}_after_fold_{trigger_after_folds:03d}"


def meta_session_key(epoch_id: str, trigger_after_folds: int = 0) -> str:
    """Stable HITL session key; Epoch-start keeps the legacy key."""

    if trigger_after_folds <= 0:
        return f"{epoch_id}/meta_learning"
    return f"{epoch_id}/meta_learning_after_fold_{trigger_after_folds:03d}"


def meta_record_id(record: Mapping[str, object]) -> str:
    """Read new records while retaining old epoch-only ledger compatibility."""

    return str(record.get("meta_learning_id") or record.get("epoch_id") or "")


def meta_record_session_key(record: Mapping[str, object]) -> str:
    return meta_session_key(
        str(record.get("epoch_id") or ""),
        int(record.get("trigger_after_folds") or 0),
    )
