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

# Stamped on every appended record; bump when the record shape changes.
LEDGER_RECORD_SCHEMA_VERSION = 1
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
        # Stamps come after the spread so a caller-supplied schema_version or
        # recorded_at can never override the ledger's own.
        payload = {
            **sanitize_for_log(record),
            "schema_version": LEDGER_RECORD_SCHEMA_VERSION,
            "recorded_at": utc_now_iso(),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str) + "\n")

    def rewrite(self, records: list[dict[str, object]]) -> None:
        """Maintenance-only atomic full rewrite (migrations); never a runtime path.

        The rolling-upgrade write-isolation guard is part of the primitive,
        not a procedural convention: the rewrite refuses while the owning
        experiment worker is alive, and refuses records that do not already
        carry the current schema stamp (a migration must hand over fully
        migrated records). Migrations must go through this method instead of
        editing the file by hand.
        """
        # Local import: hitl_state pulls in the config/session stack, which
        # this module must not load for its plain append/read paths.
        from autotrade.pipelines.hitl_state import assert_no_live_writer

        assert_no_live_writer(self.path.parent.parent)
        for record in records:
            version = record.get("schema_version")
            if type(version) is not int or version != LEDGER_RECORD_SCHEMA_VERSION:
                raise ValueError(
                    f"rewrite requires fully migrated records; got schema_version {version!r}"
                )
        tmp = self.path.with_suffix(".jsonl.tmp")
        tmp.write_text(
            "".join(
                json.dumps(record, ensure_ascii=False, sort_keys=True, default=str) + "\n"
                for record in records
            ),
            encoding="utf-8",
        )
        tmp.replace(self.path)

    def read(self, record_type: str | None = None) -> list[dict[str, object]]:
        if not self.path.exists():
            return []
        records = [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()]
        for record in records:
            version = record.get("schema_version")
            # type() check: JSON true/1.0 must not pass as 1 (bool subclasses
            # int and floats compare equal), and "1" must not pass either.
            if type(version) is not int or version != LEDGER_RECORD_SCHEMA_VERSION:
                # Fail-fast, no legacy tolerance: a missing or unknown version
                # means a foreign/newer format that older code must not
                # silently misinterpret — migrate the ledger, don't guess.
                raise ValueError(
                    f"ledger record schema_version {version!r} != "
                    f"{LEDGER_RECORD_SCHEMA_VERSION} in {self.path}; migrate the ledger before reading"
                )
        if record_type is None:
            return records
        return [record for record in records if record.get("record_type") == record_type]
