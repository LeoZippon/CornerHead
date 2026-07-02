"""Host-side managed ctx.state_dir staging (docs/environment_design.md, check.md W4).

A strategy writes cross-tick state to ctx.state_dir. Inside a ctx.substep(name, B),
that path resolves (in the sandbox driver) to a hidden staging directory, so the
write is captured rather than landing in the visible state directory immediately.
The driver reports the staged files each tick; this stager merges each into the
visible directory only once ready_at = generating-tick + B has elapsed, modelling
the latency before a heavy block's output is usable. Later-generated writes win on
conflict. Unmerged-at-region-end writes are kept in the audit ledger.

Both directories are rebuilt empty per backtest, so each replay is independent and
reproducible; durable cross-backtest data belongs in models/, not state_dir.
"""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path


@dataclass
class _Staged:
    seq: int
    state_rel: str
    staging_path: Path
    visible_path: Path
    substep: str
    budget_minutes: float
    gen_at: str
    ready_at: datetime
    status: str = "pending"
    merged_at: str = ""
    file_hash: str = ""


@dataclass
class StateStager:
    visible_dir: Path
    staging_dir: Path
    _pending: list[_Staged] = field(default_factory=list)
    _ledger: list[_Staged] = field(default_factory=list)
    _applied_seq: dict[str, int] = field(default_factory=dict)
    _seq: int = 0

    def __post_init__(self) -> None:
        # Fresh per backtest: never read a prior run's leftover state.
        for path in (self.visible_dir, self.staging_dir):
            shutil.rmtree(path, ignore_errors=True)
            path.mkdir(parents=True, exist_ok=True)
            # Docker sandboxes run strategy code as the container ``agent`` user,
            # which maps to a subuid under rootless Docker. These host-managed
            # scratch directories therefore must be world-writable, like
            # workspace/output/models, or ctx.state_dir writes fail before the
            # strategy can be evaluated.
            path.chmod(0o777)

    def register(self, staged: list[dict[str, object]], *, when: datetime) -> None:
        """Record this tick's staged writes; ready_at = ``when`` + declared budget."""
        for item in staged:
            try:
                budget = float(item.get("budget_minutes", 0.0) or 0.0)
            except (TypeError, ValueError):
                budget = 0.0
            staging_rel = str(item.get("staging_rel", ""))
            state_rel = str(item.get("state_rel", ""))
            if not staging_rel or not state_rel:
                continue
            record = _Staged(
                seq=self._seq,
                state_rel=state_rel,
                staging_path=self.staging_dir / staging_rel,
                visible_path=self.visible_dir / state_rel,
                substep=str(item.get("substep", "")),
                budget_minutes=budget,
                gen_at=when.isoformat(),
                ready_at=when + timedelta(minutes=budget),
                status="pending",
            )
            self._seq += 1
            self._pending.append(record)
            self._ledger.append(record)

    def merge_ready(self, when: datetime) -> int:
        """Merge every staged write whose ready_at has arrived by ``when``.

        Applies in generation order and only when this write is the newest seen for
        its target, so a later-generated write always wins even if an earlier one's
        ready_at is later. Returns the number of files merged."""
        ready = sorted((r for r in self._pending if r.ready_at <= when), key=lambda r: r.seq)
        merged = 0
        for record in ready:
            self._pending.remove(record)
            if not record.staging_path.exists():
                record.status = "missing_staging_file"
                continue
            if self._applied_seq.get(record.state_rel, -1) > record.seq:
                record.status = "superseded"
                continue
            record.file_hash = _sha256(record.staging_path)
            record.visible_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(record.staging_path), str(record.visible_path))
            self._applied_seq[record.state_rel] = record.seq
            record.status = "merged"
            record.merged_at = when.isoformat()
            merged += 1
        return merged

    def audit(self) -> list[dict[str, object]]:
        """Audit record for every staged write, merged or still pending at region end."""
        records: list[dict[str, object]] = []
        for record in self._ledger:
            status = "unmerged_at_region_end" if record.status == "pending" else record.status
            records.append(
                {
                    "state_rel": record.state_rel,
                    "substep": record.substep,
                    "budget_minutes": record.budget_minutes,
                    "generated_at": record.gen_at,
                    "ready_at": record.ready_at.isoformat(),
                    "merged": status == "merged",
                    "status": status,
                    "merged_at": record.merged_at,
                    "file_hash": record.file_hash,
                }
            )
        return records


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return f"sha256:{digest.hexdigest()}"
