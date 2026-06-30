"""Per-tick rolling Timeview (docs/environment_design.md, check.md W3).

Replays the real local-DB refresh cadence so a strategy at any tick sees only the
data its landing cron job has already written. Each of the agent-readable domains
(daily, events, macro, fundamentals, intraday minute history) is exposed under
``ctx.asof_dir/<domain>/`` as a directory of plain parquet parts that
``pandas.read_parquet`` concatenates into one table:

  * part 0 is the frozen research snapshot for that domain, hardlinked in
    (zero-copy);
  * later parts are write-once replay-slot increments, appended only when the
    simulation clock crosses a refresh node that covers the domain
    (``REFRESH_NODES`` in data/contracts.py).

Visibility only grows forward in time, so each replay row is written exactly once
and unchanged domains cost nothing. During the 09:20 -> next-day 02:05 session no
covering node completes, so the whole view is frozen and ``refresh`` is a no-op.
``ctx.asof_version`` bumps whenever a new part lands, so strategy code can cache a
read and re-run only when the view actually rolls.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pandas as pd

from autotrade.environment.data.contracts import (
    domain_visible_cutoff,
    event_dataset_visible_cutoff,
)
from autotrade.environment.snapshot import to_cn_timestamps

# Agent-readable domains: (view name, snapshot/replay file, whole-domain cutoff key
# or None for the per-dataset events domain). Text rolls separately via the NL view.
_DOMAINS: tuple[tuple[str, str, str | None], ...] = (
    ("daily", "daily.parquet", "daily"),
    ("events", "events.parquet", None),
    ("macro", "macro.parquet", "macro"),
    ("fundamentals", "fundamentals.parquet", "fundamentals"),
    ("intraday_1min", "intraday_1min.parquet", "intraday_1min"),
)


class Timeview:
    """Builds and rolls the per-tick six-domain as-of view for one replay."""

    def __init__(
        self,
        *,
        host_dir: Path,
        executor,
        snapshot_dir: Path,
        replay_frames: dict[str, pd.DataFrame],
    ) -> None:
        self.host_dir = Path(host_dir)
        self.executor = executor
        self.snapshot_dir = Path(snapshot_dir)
        # Fresh per backtest: no stale parts from an earlier run leak in.
        shutil.rmtree(self.host_dir, ignore_errors=True)
        self.host_dir.mkdir(parents=True, exist_ok=True)
        self._version = 0
        self._domains: dict[str, _DomainView] = {}
        for name, filename, cutoff_key in _DOMAINS:
            replay = replay_frames.get(name)
            self._domains[name] = _DomainView(
                name=name,
                cutoff_key=cutoff_key,
                out_dir=self.host_dir / name,
                frozen_file=self.snapshot_dir / filename,
                replay=replay if replay is not None else pd.DataFrame(),
            )
        # The universe never rolls; expose the frozen copy directly.
        universe = self.snapshot_dir / "universe.parquet"
        if universe.exists():
            _link_or_copy(universe, self.host_dir / "universe.parquet")

    def refresh(self, when: pd.Timestamp) -> tuple[str, str]:
        """Append any newly-visible rows at ``when`` and return the container-mapped
        ``asof_dir`` plus the current ``asof_version`` (bumps on each new part)."""
        for view in self._domains.values():
            if view.roll(when):
                self._version += 1
        return self.executor.map_path(self.host_dir), str(self._version)


class _DomainView:
    """One domain's growing directory of parquet parts."""

    def __init__(
        self,
        *,
        name: str,
        cutoff_key: str | None,
        out_dir: Path,
        frozen_file: Path,
        replay: pd.DataFrame,
    ) -> None:
        self.name = name
        self.cutoff_key = cutoff_key
        self.out_dir = out_dir
        self.out_dir.mkdir(parents=True, exist_ok=True)
        replay = replay.reset_index(drop=True)
        # A replay frame can only roll if it carries the row-level available_at the
        # node gate needs; without it the domain stays frozen-only (conservative).
        if replay.empty or "available_at" not in replay.columns:
            self.replay = pd.DataFrame()
            self._available_at = pd.Series([], dtype="datetime64[ns, Asia/Shanghai]")
            self._written = pd.Series([], dtype=bool)
        else:
            self.replay = replay
            self._available_at = to_cn_timestamps(replay["available_at"])
            self._written = pd.Series(False, index=replay.index)
        self._part_seq = 0
        self._last_signature: object = object()  # sentinel: force the first roll
        self._columns = self._init_frozen_part(frozen_file)

    def _init_frozen_part(self, frozen_file: Path) -> list[str]:
        """Seed part 0 from the frozen snapshot domain and fix the canonical schema.

        A non-empty frozen file is hardlinked unchanged and its columns become the
        canonical schema. An empty/absent frozen writes NO part: an empty parquet
        always lands null-typed columns that can no longer unify with the typed
        replay parts appended later, whereas an empty directory simply reads back as
        an empty frame. The domain stays empty until its first replay part rolls in."""
        frozen = pd.read_parquet(frozen_file) if frozen_file.exists() else pd.DataFrame()
        if not frozen.empty:
            _link_or_copy(frozen_file, self.out_dir / "part_0000.parquet")
            self._part_seq = 1
            return list(frozen.columns)
        # The agent-facing schema drops the gating-only available_at unless the frozen
        # domain already carries it (events/macro/fundamentals do; daily does not).
        columns = list(frozen.columns) or list(self.replay.columns)
        if "available_at" not in frozen.columns and "available_at" in columns:
            columns = [c for c in columns if c != "available_at"]
        return columns

    def roll(self, when: pd.Timestamp) -> bool:
        """Append a part for rows newly visible at ``when``; return True if written."""
        if self.replay.empty:
            return False
        signature = self._signature(when)
        if signature == self._last_signature:
            return False  # this domain's covering node(s) have not advanced
        self._last_signature = signature
        cutoffs = self._row_cutoffs(when)
        visible = self._available_at <= cutoffs if cutoffs is not None else pd.Series(False, index=self.replay.index)
        newly = visible & ~self._written
        if not newly.any():
            return False
        self._written = self._written | newly
        part = self.replay.loc[newly].reindex(columns=self._columns)
        part.to_parquet(self.out_dir / f"part_{self._part_seq:04d}.parquet", index=False)
        self._part_seq += 1
        return True

    def _row_cutoffs(self, when: pd.Timestamp):
        """Per-row availability cutoff at ``when`` (a scalar for whole-domain
        nodes, a per-row Series for the per-dataset events domain)."""
        if self.cutoff_key is not None:
            cutoff = domain_visible_cutoff(self.cutoff_key, when)
            return pd.Series(pd.Timestamp(cutoff), index=self.replay.index) if cutoff is not None else None
        datasets = self.replay.get("dataset")
        if datasets is None:
            return None
        datasets = datasets.astype(str)
        cmap = {d: event_dataset_visible_cutoff(d, when) for d in datasets.unique()}
        return datasets.map(lambda d: pd.Timestamp(cmap[d]) if cmap[d] is not None else pd.NaT)

    def _signature(self, when: pd.Timestamp) -> object:
        if self.cutoff_key is not None:
            return str(domain_visible_cutoff(self.cutoff_key, when))
        datasets = self.replay.get("dataset")
        names = sorted(datasets.astype(str).unique()) if datasets is not None else []
        return tuple((d, str(event_dataset_visible_cutoff(d, when))) for d in names)


def _link_or_copy(src: Path, dst: Path) -> None:
    """Hardlink ``src`` to ``dst`` (zero-copy on the same filesystem); copy on a
    cross-device or already-linked failure."""
    if dst.exists():
        dst.unlink()
    try:
        os.link(src, dst)
    except OSError:
        shutil.copyfile(src, dst)
