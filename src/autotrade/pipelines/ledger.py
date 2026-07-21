"""The single experiment ledger (docs/pipeline_design.md §4.1).

One JSONL file per experiment. Records are distinguished by ``record_type``
(``fold`` / ``meta_learning`` / ``heldout``); Steps are lightweight
summaries inside the fold record's ``steps[]``, never separate files.
``attempt_failed`` records are appended when a run throws before its success
record — they carry the error evidence and are ignored by every reader that
selects the success types, so a failed attempt is re-runnable but auditable.
"""

from __future__ import annotations

import json
from pathlib import Path

from autotrade.environment.runtime import sanitize_for_log, utc_now_iso

RECORD_TYPES = ("fold", "meta_learning", "heldout", "attempt_failed")
LINK_KEYS = ("experiment_id", "epoch_id", "fold_id", "run_id")


def latest_fold_records(records: list[dict[str, object]]) -> dict[tuple[str, str], dict[str, object]]:
    """Latest fold record per (epoch, fold): the ledger is append-only, so a
    re-run appends a superseding record. Formal consumers (reporting, console)
    must never double-count earlier attempts."""
    latest: dict[tuple[str, str], dict[str, object]] = {}
    for record in records:
        if record.get("record_type") == "fold":
            latest[(str(record.get("epoch_id")), str(record.get("fold_id")))] = record
    return latest


def latest_heldout_records(records: list[dict[str, object]]) -> list[dict[str, object]]:
    """Latest record per held-out period (a fold re-run replays held-out, so
    earlier period records are superseded, not removed)."""
    latest: dict[str, dict[str, object]] = {}
    for record in records:
        if record.get("record_type") == "heldout":
            latest[str(record.get("fold_id"))] = record
    return [latest[key] for key in sorted(latest)]


class ExperimentLedger:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def append(self, record: dict[str, object]) -> None:
        record_type = record.get("record_type")
        if record_type not in RECORD_TYPES:
            raise ValueError(f"unsupported record_type: {record_type!r}")
        missing = [key for key in LINK_KEYS if not record.get(key)]
        if missing:
            raise ValueError(f"ledger record missing link keys: {missing}")
        payload = {"recorded_at": utc_now_iso(), **sanitize_for_log(record)}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str) + "\n")

    def read(self, record_type: str | None = None) -> list[dict[str, object]]:
        if not self.path.exists():
            return []
        records = [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()]
        if record_type is None:
            return records
        return [record for record in records if record.get("record_type") == record_type]
