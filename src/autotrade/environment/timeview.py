"""Per-tick rolling Timeview (docs/environment_design.md, check.md W3).

Replays the real local-DB refresh cadence so a strategy at any tick sees only the
data its landing cron job has already written. Each agent-readable parquet domain
(daily, events, macro, fundamentals, intraday minute history, text_index) is
exposed under ``ctx.asof_dir/<domain>/`` as a directory of plain parquet parts
that ``pandas.read_parquet`` concatenates into one table:

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

Text bodies live under ``ctx.asof_dir/text_library``. Frozen snapshot body shards
are hardlinked at start; replay body shards are copied only for newly visible
``text_index`` rows, so direct text processing has the same PIT wall as ``ctx.nl``.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from autotrade.environment.data.contracts import (
    domain_visible_cutoff,
    event_dataset_visible_cutoff,
    text_dataset_visible_cutoff,
)
from autotrade.environment.snapshot import to_cn_timestamps

_EMPTY_INDICES = np.array([], dtype=np.int64)


def _utc_ns(values: pd.Series) -> np.ndarray:
    """tz-aware timestamps as UTC-naive datetime64[ns] (searchsorted keys)."""
    return values.dt.tz_convert("UTC").dt.tz_localize(None).to_numpy(dtype="datetime64[ns]")


def _cutoff_ns(cutoff: object) -> np.datetime64:
    ts = pd.Timestamp(cutoff)
    if ts.tzinfo is not None:
        ts = ts.tz_convert("UTC").tz_localize(None)
    return np.datetime64(ts, "ns")


class _SortedCursor:
    """Visibility only grows forward, so each cutoff advance is a binary search
    over available_at-sorted row positions instead of a whole-series boolean
    mask (the mask was O(rows) per node crossing — ~44M rows for a quarter of
    minute bars)."""

    __slots__ = ("indices", "keys", "pos")

    def __init__(self, indices: np.ndarray, keys: np.ndarray) -> None:
        order = np.argsort(keys, kind="stable")
        self.indices = indices[order]
        self.keys = keys[order]
        self.pos = 0

    def advance(self, cutoff: np.datetime64) -> np.ndarray:
        new_pos = int(np.searchsorted(self.keys, cutoff, side="right"))
        if new_pos <= self.pos:
            return _EMPTY_INDICES
        newly = self.indices[self.pos:new_pos]
        self.pos = new_pos
        return newly


def _dataset_cursors(datasets: np.ndarray, valid_indices: np.ndarray, keys_all: np.ndarray) -> dict[str, _SortedCursor]:
    cursors: dict[str, _SortedCursor] = {}
    valid_datasets = datasets[valid_indices]
    for name in np.unique(valid_datasets):
        selection = valid_indices[valid_datasets == name]
        cursors[str(name)] = _SortedCursor(selection, keys_all[selection])
    return cursors

# Agent-readable parquet domains: (view name, snapshot/replay file, whole-domain
# cutoff key or None for the per-dataset events domain).
_DOMAINS: tuple[tuple[str, str, str | None], ...] = (
    ("daily", "daily.parquet", "daily"),
    ("events", "events.parquet", None),
    ("macro", "macro.parquet", "macro"),
    ("fundamentals", "fundamentals.parquet", "fundamentals"),
    ("intraday_1min", "intraday_1min.parquet", "intraday_1min"),
    ("auction", "auction.parquet", "auction"),
)


class Timeview:
    """Builds and rolls the per-tick as-of view for one replay."""

    def __init__(
        self,
        *,
        host_dir: Path,
        executor,
        snapshot_dir: Path,
        replay_frames: dict[str, pd.DataFrame],
        replay_text_library_dir: Path | None = None,
    ) -> None:
        self.host_dir = Path(host_dir)
        self.executor = executor
        self.snapshot_dir = Path(snapshot_dir)
        # Fresh per backtest: no stale parts from an earlier run leak in.
        shutil.rmtree(self.host_dir, ignore_errors=True)
        self.host_dir.mkdir(parents=True, exist_ok=True)
        self._version = 0
        # The container mapping of host_dir is invariant for the whole replay;
        # resolving it per tick costs several filesystem walks (map_path does
        # multiple Path.resolve() calls), so it is computed once, lazily.
        self._mapped_dir: str | None = None
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
        self._text = _TextView(
            out_index_dir=self.host_dir / "text_index",
            out_library_dir=self.host_dir / "text_library",
            frozen_index_file=self.snapshot_dir / "text_index.parquet",
            frozen_library_dir=self.snapshot_dir / "text_library",
            replay_index=replay_frames.get("text_index", pd.DataFrame()),
            replay_library_dir=Path(replay_text_library_dir) if replay_text_library_dir is not None else None,
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
        if self._text.roll(when):
            self._version += 1
        if self._mapped_dir is None:
            self._mapped_dir = self.executor.map_path(self.host_dir)
        return self._mapped_dir, str(self._version)


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
            replay = pd.DataFrame()
        self.replay = replay
        self._part_seq = 0
        self._last_signature: object = object()  # sentinel: force the first roll
        self._cursor: _SortedCursor | None = None
        self._cursors: dict[str, _SortedCursor] = {}
        if not replay.empty:
            available_at = to_cn_timestamps(replay["available_at"])
            valid = np.flatnonzero(available_at.notna().to_numpy())  # NaT rows never become visible
            keys = _utc_ns(available_at)
            if cutoff_key is not None:
                self._cursor = _SortedCursor(valid, keys[valid])
            elif "dataset" in replay.columns:
                self._cursors = _dataset_cursors(replay["dataset"].astype(str).to_numpy(), valid, keys)
        self._dataset_names: list[str] = sorted(self._cursors)
        self._columns = self._init_frozen_part(frozen_file)

    def _init_frozen_part(self, frozen_file: Path) -> list[str]:
        """Seed part 0 from the frozen snapshot domain and fix the canonical schema.

        A non-empty frozen file is hardlinked unchanged and its columns become the
        canonical schema. An empty/absent frozen writes NO part: an empty parquet
        always lands null-typed columns that can no longer unify with the typed
        replay parts appended later, whereas an empty directory simply reads back as
        an empty frame. The domain stays empty until its first replay part rolls in.

        Emptiness and schema come from the parquet FOOTER (num_rows + arrow
        schema): the frozen daily/minute domains run to gigabytes and reading
        them whole here dominated Timeview init."""
        frozen_columns: list[str] = []
        if frozen_file.exists():
            footer = pq.ParquetFile(frozen_file)
            frozen_columns = list(footer.schema_arrow.names)
            if footer.metadata.num_rows > 0:
                _link_or_copy(frozen_file, self.out_dir / "part_0000.parquet")
                self._part_seq = 1
                return frozen_columns
        # The agent-facing schema drops the gating-only available_at unless the frozen
        # domain already carries it (events/macro/fundamentals do; daily does not).
        columns = frozen_columns or list(self.replay.columns)
        if "available_at" not in frozen_columns and "available_at" in columns:
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
        newly = self._newly_visible(when)
        if newly.size == 0:
            return False
        newly.sort()  # original frame order: parts read back exactly as the frame slice
        part = self.replay.iloc[newly].reindex(columns=self._columns)
        part.to_parquet(self.out_dir / f"part_{self._part_seq:04d}.parquet", index=False)
        self._part_seq += 1
        return True

    def _newly_visible(self, when: pd.Timestamp) -> np.ndarray:
        if self._cursor is not None:
            cutoff = domain_visible_cutoff(self.cutoff_key, when)
            return self._cursor.advance(_cutoff_ns(cutoff)) if cutoff is not None else _EMPTY_INDICES
        parts = [
            indices
            for name, cursor in self._cursors.items()
            if (cutoff := event_dataset_visible_cutoff(name, when)) is not None
            and (indices := cursor.advance(_cutoff_ns(cutoff))).size
        ]
        return np.concatenate(parts) if parts else _EMPTY_INDICES

    def _signature(self, when: pd.Timestamp) -> object:
        if self.cutoff_key is not None:
            return str(domain_visible_cutoff(self.cutoff_key, when))
        return tuple((d, str(event_dataset_visible_cutoff(d, when))) for d in self._dataset_names)


class _TextView:
    """Rolling text index plus visible body shards.

    The frozen research library is fully visible at replay start, but replay-slot
    bodies are only copied into the view once their matching text_index rows pass
    the text refresh-node gate. This lets strategies do their own NLP from
    ``ctx.asof_dir`` without exposing future text bodies.
    """

    def __init__(
        self,
        *,
        out_index_dir: Path,
        out_library_dir: Path,
        frozen_index_file: Path,
        frozen_library_dir: Path,
        replay_index: pd.DataFrame,
        replay_library_dir: Path | None,
    ) -> None:
        self.out_index_dir = out_index_dir
        self.out_library_dir = out_library_dir
        self.out_index_dir.mkdir(parents=True, exist_ok=True)
        self.out_library_dir.mkdir(parents=True, exist_ok=True)
        self.replay_index = replay_index.reset_index(drop=True) if replay_index is not None else pd.DataFrame()
        self.replay_library_dir = replay_library_dir
        self._part_seq = 0
        self._last_signature: object = object()
        required = {"available_at", "dataset", "text_id"}
        if self.replay_index.empty or not required.issubset(self.replay_index.columns):
            self.replay_index = pd.DataFrame()
        self._cursors: dict[str, _SortedCursor] = {}
        if not self.replay_index.empty:
            available_at = to_cn_timestamps(self.replay_index["available_at"])
            valid = np.flatnonzero(available_at.notna().to_numpy())
            self._cursors = _dataset_cursors(
                self.replay_index["dataset"].astype(str).to_numpy(), valid, _utc_ns(available_at)
            )
        self._dataset_names = sorted(self._cursors)
        self._init_frozen(frozen_index_file, frozen_library_dir)

    def _init_frozen(self, frozen_index_file: Path, frozen_library_dir: Path) -> None:
        if frozen_index_file.exists():
            _link_or_copy(frozen_index_file, self.out_index_dir / "part_0000.parquet")
            self._part_seq = 1
        if frozen_library_dir.exists():
            for src in sorted(frozen_library_dir.glob("*.parquet")):
                _link_or_copy(src, self.out_library_dir / src.name)

    def roll(self, when: pd.Timestamp) -> bool:
        if self.replay_index.empty:
            return False
        signature = tuple((d, str(text_dataset_visible_cutoff(d, when))) for d in self._dataset_names)
        if signature == self._last_signature:
            return False
        self._last_signature = signature
        parts = [
            indices
            for name, cursor in self._cursors.items()
            if (cutoff := text_dataset_visible_cutoff(name, when)) is not None
            and (indices := cursor.advance(_cutoff_ns(cutoff))).size
        ]
        if not parts:
            return False
        newly = np.concatenate(parts)
        newly.sort()  # original frame order: parts read back exactly as the frame slice
        rows = self.replay_index.iloc[newly].copy()
        if "library_file" not in rows.columns:
            rows["library_file"] = rows["dataset"].astype(str) + ".parquet"
        library_files: dict[tuple[str, str], str] = {}
        for dataset, group in rows.groupby(rows["dataset"].astype(str), sort=True):
            part_name = f"{dataset}__part_{self._part_seq:04d}.parquet"
            self._write_body_part(dataset, set(group["text_id"].astype(str)), part_name)
            for source_file in group["library_file"].astype(str).unique():
                library_files[(dataset, source_file)] = part_name
        rows["library_file"] = [
            library_files.get(
                (str(row.get("dataset", "")), str(row.get("library_file", ""))),
                str(row.get("library_file", "")),
            )
            for _, row in rows.iterrows()
        ]
        rows.to_parquet(self.out_index_dir / f"part_{self._part_seq:04d}.parquet", index=False)
        self._part_seq += 1
        return True

    def _write_body_part(self, dataset: str, text_ids: set[str], part_name: str) -> None:
        body = self._read_body_rows(dataset, text_ids)
        if body.empty or "text_id" not in body.columns:
            pd.DataFrame(columns=["text_id", "body"]).to_parquet(self.out_library_dir / part_name, index=False)
            return
        part = body[[c for c in ("text_id", "body") if c in body.columns]]
        part.to_parquet(self.out_library_dir / part_name, index=False)

    def _read_body_rows(self, dataset: str, text_ids: set[str]) -> pd.DataFrame:
        path = self.replay_library_dir / f"{dataset}.parquet" if self.replay_library_dir is not None else None
        if path is None or not path.exists() or not text_ids:
            return pd.DataFrame(columns=["text_id", "body"])
        try:
            return pd.read_parquet(path, filters=[("text_id", "in", sorted(text_ids))])
        except Exception:
            body = pd.read_parquet(path)
            if body.empty or "text_id" not in body.columns:
                return pd.DataFrame(columns=["text_id", "body"])
            return body.loc[body["text_id"].astype(str).isin(text_ids)]


def _link_or_copy(src: Path, dst: Path) -> None:
    """Hardlink ``src`` to ``dst`` (zero-copy on the same filesystem); copy on a
    cross-device or already-linked failure."""
    if dst.exists():
        dst.unlink()
    try:
        os.link(src, dst)
    except OSError:
        shutil.copyfile(src, dst)
