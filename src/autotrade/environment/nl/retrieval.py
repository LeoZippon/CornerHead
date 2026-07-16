"""Point-in-time text retrieval over the snapshot/replay text libraries.

Pure data access for the NL Sub Agent: DuckDB/RE2 regex over the text index
and per-dataset body shards (column projection + LIMIT; the multi-GB corpus is
never resident in host memory). No LLM dependency — the agent loop lives in
``nl/engine.py``.
"""

from __future__ import annotations

import hashlib
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import duckdb
import pandas as pd

from autotrade.environment.data.contracts import text_dataset_visible_cutoff
from autotrade.environment.snapshot import to_cn_timestamps

MAX_PATTERN_CHARS = 256
_CANDIDATE_CACHE_SIZE = 128


@dataclass
class _CandidateCorpus:
    """Static candidate rows plus incrementally loaded PIT-visible bodies."""

    index: pd.DataFrame
    bodies: pd.DataFrame | None = None
    loaded_body_ids: set[tuple[str, str]] = field(default_factory=set)


@dataclass(frozen=True)
class CandidateEvidenceState:
    """Content identity and cardinality for one PIT candidate evidence scope."""

    revision: str
    match_count: int


class TextRetriever:
    """Grep-style retrieval over the snapshot text index and as-of text library.

    The NL Sub Agent supplies a regex pattern (case-insensitive grep semantics,
    RE2 engine — linear-time matching, so an adversarial or accidental
    catastrophic-backtracking pattern cannot pin the host CPU; unsupported
    constructs are rejected with a fixable error). Titles/codes are matched
    first, then full bodies when more results are needed. Stock-scoped searches
    stay inside code/name-linked candidate rows; calls without a stock code use
    the broad market corpus.

    Bodies live in per-dataset parquet shards under ``text_library/`` and are
    scanned in place via DuckDB with column projection and result limits — the
    multi-GB corpus is never resident in host memory. A bounded LRU retains the
    PIT-visible body subset linked to recently requested candidates and extends
    it as the replay clock advances. The frozen
    research snapshot index/library plus the replay-slot index/library are read
    together. ``as_of`` rolls the corpus on the same cron refresh nodes as the
    agent Timeview: frozen rows are always
    visible; replay rows appear only once their dataset's node has completed
    by ``as_of``. ``as_of`` None (Timeview off) keeps the frozen-only view.
    """

    def __init__(
        self,
        text_index_path: str | Path,
        text_library_dir: str | Path,
        *,
        snippet_chars: int = 4000,
        replay_index_path: str | Path | None = None,
        replay_library_dir: str | Path | None = None,
        as_of: "datetime | None" = None,
    ) -> None:
        self.library_dirs = [Path(text_library_dir)] + (
            [Path(replay_library_dir)] if replay_library_dir is not None else []
        )
        self.snippet_chars = snippet_chars
        self.as_of = as_of
        frozen = _read_index(text_index_path)
        frozen["_source"] = "frozen"
        frames = [frozen]
        if replay_index_path is not None:
            replay = _read_index(replay_index_path)
            if not replay.empty:
                replay["_source"] = "replay"
                frames.append(replay)
        self.index = pd.concat(frames, ignore_index=True) if any(not f.empty for f in frames) else frozen
        self._available_at = (
            to_cn_timestamps(self.index["available_at"])
            if not self.index.empty and "available_at" in self.index.columns
            else pd.Series([], dtype="datetime64[ns, Asia/Shanghai]")
        )
        self._datasets = self.index.get("dataset", pd.Series("", index=self.index.index)).astype(str)
        self._codes = self.index.get("ts_codes", pd.Series("", index=self.index.index)).fillna("").astype(str)
        self._frozen_mask = (
            self.index["_source"].astype(str) == "frozen"
            if not self.index.empty and "_source" in self.index.columns
            else pd.Series(True, index=self.index.index)
        )
        self._visible_cache_key: str | None = None
        self._visible_cache: pd.DataFrame | None = None
        self._snippets: dict[tuple[str, str], str] = {}
        linked = self._codes[self._codes.ne("")]
        self._code_rows = {
            str(code): pd.Index(rows)
            for code, rows in linked.groupby(linked, sort=False).groups.items()
        }
        self._candidate_cache: OrderedDict[tuple[str, ...], _CandidateCorpus] = OrderedDict()
        self._query_lock = threading.Lock()
        self._connection = duckdb.connect()
        self._candidate_titles = pd.DataFrame(
            {
                "row": self.index.index,
                "title": self.index.get("title", pd.Series("", index=self.index.index)).astype(str),
            }
        )
        self._connection.register("candidate_titles", self._candidate_titles)

    def close(self) -> None:
        """Release the persistent DuckDB connection after a replay."""
        with self._query_lock:
            self._connection.close()

    def _visible_index(self) -> pd.DataFrame:
        """Index rows visible at ``self.as_of``: frozen rows always, replay rows only
        once their dataset's refresh node has completed (None = frozen-only view)."""
        if self.index.empty or "_source" not in self.index.columns:
            return self.index
        cache_key = "frozen" if self.as_of is None else pd.Timestamp(self.as_of).isoformat()
        if cache_key == self._visible_cache_key and self._visible_cache is not None:
            return self._visible_cache
        if self.as_of is None:
            visible = self.index[self._frozen_mask]
        else:
            # Convert each dataset's cutoff once, then broadcast by a vectorized
            # dict map. A per-row ``pd.Timestamp()`` lambda over the multi-million
            # row index dominated NL retrieval (tens of seconds rebuilt on every
            # decision tick); mapping the handful of unique datasets is ~150x cheaper.
            cmap = {d: text_dataset_visible_cutoff(d, self.as_of) for d in self._datasets.unique()}
            cutoff_by_dataset = {d: (pd.Timestamp(c) if c is not None else pd.NaT) for d, c in cmap.items()}
            cutoff = self._datasets.map(cutoff_by_dataset)
            replay_visible = (~self._frozen_mask) & (self._available_at <= cutoff)
            visible = self.index[self._frozen_mask | replay_visible]
        # One research tick commonly issues several nl() calls and each NL task
        # may search three rounds. The PIT boundary is identical across them, so
        # retain only the latest materialized visibility slice instead of
        # rebuilding a multi-million-row mask for every tool call.
        self._visible_cache_key = cache_key
        self._visible_cache = visible
        return visible

    def search(
        self,
        pattern: str,
        *,
        ts_code: str = "",
        max_results: int = 5,
        search_bodies: bool = True,
        company_terms: list[str] | None = None,
        lookback_days: int | None = None,
    ) -> list[dict[str, object]]:
        """Raises ValueError for patterns outside the RE2/grep contract."""
        regex = validate_pattern(pattern)
        candidate_key = self._candidate_key(ts_code, company_terms)
        corpus = self._candidate_corpus(candidate_key) if candidate_key else None
        index = (
            self._visible_candidate_index(corpus, lookback_days=lookback_days)
            if corpus is not None
            else self._visible_index()
        )
        if index.empty:
            return []
        pattern_hit = self._title_code_match(index, regex)
        hits = index[pattern_hit].copy()
        hits["_relevance"] = "candidate" if corpus is not None else "background"
        hits["_rank"] = 40 if corpus is not None else 20
        if search_bodies and len(hits) < max_results:
            body_idx = (
                self._grep_candidate_bodies(
                    corpus,
                    index,
                    regex,
                    exclude=set(hits["text_id"].astype(str)),
                    limit=max_results * 3,
                )
                if corpus is not None
                else self._grep_bodies(
                    index,
                    regex,
                    exclude=set(hits["text_id"].astype(str)),
                    limit=max_results * 3,
                )
            )
            if body_idx:
                body_rows = index[index["text_id"].astype(str).isin(body_idx)].copy()
                body_rows["_relevance"] = "candidate" if corpus is not None else "background"
                body_rows["_rank"] = 30 if corpus is not None else 10
                hits = pd.concat([hits, body_rows], ignore_index=False)
        if hits.empty:
            return []
        hits = hits.drop_duplicates(subset=["text_id"], keep="first")
        sort_cols = ["_rank"] + (["available_at"] if "available_at" in hits.columns else [])
        hits = hits.sort_values(sort_cols, ascending=[False] * len(sort_cols))
        selected = hits.head(max_results)
        if corpus is not None and search_bodies:
            # The first stock query commonly hits titles, followed by several body
            # patterns. Prime the currently visible candidate rows once so later
            # regex rounds do not rescan the multi-GB shards; newly visible rows
            # are appended on demand as the replay clock advances.
            self._candidate_bodies(corpus, index)
        self._prime_snippets(selected)
        records = []
        for row in selected.to_dict("records"):
            records.append(
                {
                    "text_id": str(row.get("text_id", "")),
                    "title": str(row.get("title", "")),
                    "available_at": str(row.get("available_at", "")),
                    "source_hash": str(row.get("source_hash", "")),
                    "ts_codes": str(row.get("ts_codes", "")),
                    "relevance": str(row.get("_relevance", "background")),
                    "snippet": self._snippet(str(row.get("dataset", "")), str(row.get("text_id", ""))),
                }
            )
        return records

    def candidate_revision(
        self,
        ts_code: str,
        *,
        company_terms: list[str] | None = None,
        patterns: tuple[str, ...] = (),
        lookback_days: int | None = None,
    ) -> str:
        """Hash visible candidate evidence, optionally limited to event patterns."""
        return self.candidate_evidence_state(
            ts_code,
            company_terms=company_terms,
            patterns=patterns,
            lookback_days=lookback_days,
        ).revision

    def candidate_evidence_state(
        self,
        ts_code: str,
        *,
        company_terms: list[str] | None = None,
        patterns: tuple[str, ...] = (),
        lookback_days: int | None = None,
    ) -> CandidateEvidenceState:
        """Hash and count the matching evidence visible inside a rolling PIT window."""
        if not str(ts_code or "").strip():
            raise ValueError("candidate_evidence_state requires a non-empty ts_code")
        key = self._candidate_key(ts_code, company_terms)
        corpus = self._candidate_corpus(key)
        visible = self._visible_candidate_index(corpus, lookback_days=lookback_days)
        if patterns and not visible.empty:
            visible = self._matching_candidate_rows(corpus, visible, patterns)
        columns = [c for c in ("dataset", "text_id", "source_hash", "available_at", "title") if c in visible]
        digest = hashlib.sha256()
        if columns:
            ordered = visible[columns].fillna("").astype(str).sort_values(columns)
            for row in ordered.itertuples(index=False, name=None):
                digest.update("\x1f".join(row).encode("utf-8"))
                digest.update(b"\n")
        return CandidateEvidenceState(
            revision=f"sha256:{digest.hexdigest()}",
            match_count=int(len(visible)),
        )

    def _matching_candidate_rows(
        self,
        corpus: _CandidateCorpus,
        visible: pd.DataFrame,
        patterns: tuple[str, ...],
    ) -> pd.DataFrame:
        valid = tuple(validate_pattern(pattern) for pattern in patterns)
        combined = "|".join(f"(?:{pattern})" for pattern in valid)
        matched = self._title_code_match(visible, combined)
        bodies = self._candidate_bodies(corpus, visible)
        if not bodies.empty:
            visible_ids = set(visible["text_id"].astype(str))
            body_frame = bodies[bodies["text_id"].isin(visible_ids)]
            if not body_frame.empty:
                with self._query_lock:
                    try:
                        self._connection.register("candidate_bodies", body_frame)
                        body_ids = self._connection.execute(
                            "SELECT DISTINCT text_id FROM candidate_bodies "
                            "WHERE regexp_matches(body, ?, 'i')",
                            [combined],
                        ).fetchall()
                    except duckdb.Error as exc:
                        raise ValueError(
                            f"text_retrieve body query failed (RE2/grep semantics): {exc}"
                        ) from exc
                    finally:
                        self._connection.unregister("candidate_bodies")
                matched = matched | visible["text_id"].astype(str).isin(
                    str(row[0]) for row in body_ids
                )
        return visible[matched]

    def _candidate_key(self, ts_code: str, company_terms: list[str] | None) -> tuple[str, ...]:
        return tuple(_candidate_terms(ts_code, company_terms))

    def _candidate_corpus(self, key: tuple[str, ...]) -> _CandidateCorpus:
        cached = self._candidate_cache.pop(key, None)
        if cached is not None:
            self._candidate_cache[key] = cached
            return cached
        code = key[0]
        code_rows = self._code_rows.get(code, pd.Index([]))
        title_terms = [term for term in key if term]
        if title_terms:
            clauses = " OR ".join("contains(lower(title), lower(?))" for _ in title_terms)
            with self._query_lock:
                title_rows = pd.Index(
                    row[0]
                    for row in self._connection.execute(
                        f"SELECT row FROM candidate_titles WHERE {clauses}",
                        title_terms,
                    ).fetchall()
                )
            rows = code_rows.union(title_rows, sort=False).sort_values()
        else:
            rows = code_rows
        corpus = _CandidateCorpus(self.index.loc[rows].copy())
        self._candidate_cache[key] = corpus
        if len(self._candidate_cache) > _CANDIDATE_CACHE_SIZE:
            _, evicted = self._candidate_cache.popitem(last=False)
            if evicted.bodies is not None:
                for row in evicted.bodies.itertuples(index=False):
                    self._snippets.pop((str(row.dataset), str(row.text_id)), None)
        return corpus

    def _visible_candidate_index(
        self,
        corpus: _CandidateCorpus,
        *,
        lookback_days: int | None = None,
    ) -> pd.DataFrame:
        if lookback_days is not None:
            if isinstance(lookback_days, bool) or not isinstance(lookback_days, int) or lookback_days <= 0:
                raise ValueError("lookback_days must be a positive integer")
            if self.as_of is None:
                raise ValueError("lookback_days requires a simulated decision as_of time")
        index = corpus.index
        if index.empty or "_source" not in index.columns:
            visible = index
        else:
            frozen = self._frozen_mask.loc[index.index]
            if self.as_of is None:
                visible = index[frozen]
            else:
                datasets = self._datasets.loc[index.index]
                cutoffs = {
                    dataset: text_dataset_visible_cutoff(dataset, self.as_of)
                    for dataset in datasets.unique()
                }
                cutoff = datasets.map(
                    {
                        dataset: (pd.Timestamp(value) if value is not None else pd.NaT)
                        for dataset, value in cutoffs.items()
                    }
                )
                available_at = self._available_at.loc[index.index]
                visible = index[frozen | ((~frozen) & (available_at <= cutoff))]
        if lookback_days is None or visible.empty:
            return visible
        anchor = pd.Timestamp(self.as_of)
        anchor = (
            anchor.tz_localize("Asia/Shanghai")
            if anchor.tzinfo is None
            else anchor.tz_convert("Asia/Shanghai")
        )
        earliest = anchor - pd.Timedelta(days=lookback_days)
        return visible[self._available_at.loc[visible.index] >= earliest]

    def _shards(self, dataset: str) -> list[str]:
        return [str(d / f"{dataset}.parquet") for d in self.library_dirs if (d / f"{dataset}.parquet").exists()]

    def _title_code_match(self, index: pd.DataFrame, regex: str) -> pd.Series:
        """RE2 title/code match over the visible index (boolean mask)."""
        frame = pd.DataFrame(
            {
                "row": index.index,
                "title": index.get("title", pd.Series("", index=index.index)).astype(str),
                "ts_codes": index.get("ts_codes", pd.Series("", index=index.index)).astype(str),
            }
        )
        with self._query_lock:
            try:
                self._connection.register("visible_index", frame)
                rows = self._connection.execute(
                    "SELECT row FROM visible_index "
                    "WHERE regexp_matches(title, ?, 'i') OR regexp_matches(ts_codes, ?, 'i')",
                    [regex, regex],
                ).fetchall()
            except duckdb.Error as exc:
                raise ValueError(f"unsupported regex (RE2/grep semantics): {exc}") from exc
            finally:
                self._connection.unregister("visible_index")
        return pd.Series(index.index.isin([row[0] for row in rows]), index=index.index)

    def _grep_candidate_bodies(
        self,
        corpus: _CandidateCorpus,
        visible_index: pd.DataFrame,
        regex: str,
        *,
        exclude: set[str],
        limit: int,
    ) -> set[str]:
        bodies = self._candidate_bodies(corpus, visible_index)
        if bodies.empty:
            return set()
        allowed = set(visible_index["text_id"].astype(str)) - exclude
        frame = bodies[bodies["text_id"].isin(allowed)]
        if frame.empty:
            return set()
        with self._query_lock:
            try:
                self._connection.register("candidate_bodies", frame)
                rows = self._connection.execute(
                    "SELECT text_id FROM candidate_bodies WHERE regexp_matches(body, ?, 'i') LIMIT ?",
                    [regex, limit],
                ).fetchall()
            except duckdb.Error as exc:
                raise ValueError(f"text_retrieve body query failed (RE2/grep semantics): {exc}") from exc
            finally:
                self._connection.unregister("candidate_bodies")
        return {str(row[0]) for row in rows}

    def _candidate_bodies(
        self, corpus: _CandidateCorpus, visible_index: pd.DataFrame
    ) -> pd.DataFrame:
        frames: list[pd.DataFrame] = []
        datasets = visible_index.get("dataset")
        if datasets is not None:
            dataset_values = datasets.astype(str)
            for dataset in dataset_values.unique():
                shards = self._shards(dataset)
                ids = visible_index.loc[dataset_values == dataset, "text_id"].astype(str).drop_duplicates()
                loaded = {
                    text_id
                    for loaded_dataset, text_id in corpus.loaded_body_ids
                    if loaded_dataset == dataset
                }
                ids = ids[~ids.isin(loaded)]
                if not shards or ids.empty:
                    continue
                placeholders = ",".join("?" for _ in ids)
                rows = self._body_query(
                    "SELECT CAST(text_id AS VARCHAR), CAST(body AS VARCHAR) FROM read_parquet(?) "
                    f"WHERE CAST(text_id AS VARCHAR) IN ({placeholders})",
                    [shards, *ids.tolist()],
                )
                if rows:
                    frame = pd.DataFrame(rows, columns=["text_id", "body"])
                    frame["dataset"] = dataset
                    frames.append(frame)
                corpus.loaded_body_ids.update((dataset, text_id) for text_id in ids)
        if corpus.bodies is None:
            corpus.bodies = pd.DataFrame(columns=["text_id", "body", "dataset"])
        if frames:
            added = pd.concat(frames, ignore_index=True)
            corpus.bodies = pd.concat([corpus.bodies, added], ignore_index=True).drop_duplicates(
                ["dataset", "text_id"], keep="first"
            )
            for row in added.itertuples(index=False):
                self._snippets.setdefault(
                    (str(row.dataset), str(row.text_id)),
                    str(row.body or "")[: self.snippet_chars],
                )
        return corpus.bodies

    def _grep_bodies(
        self,
        index: pd.DataFrame,
        regex: str,
        *,
        exclude: set[str],
        limit: int,
    ) -> set[str]:
        """Linear-time full-body grep with column projection and a result cap;
        matched snippets are cached so ranking never re-reads the shards."""
        found: set[str] = set()
        datasets = index.get("dataset")
        if datasets is None:
            return found
        for dataset in datasets.astype(str).unique():
            shards = self._shards(dataset)
            if not shards:
                continue
            rows = self._body_query(
                "SELECT text_id, substr(body, 1, ?) FROM read_parquet(?) "
                "WHERE regexp_matches(body, ?, 'i') LIMIT ?",
                [self.snippet_chars, shards, regex, limit + len(exclude)],
            )
            for text_id, snippet in rows:
                tid = str(text_id)
                self._snippets.setdefault((dataset, tid), str(snippet or ""))
                if tid not in exclude:
                    found.add(tid)
            if len(found) >= limit:
                break
        return found

    def _body_query(self, query: str, params: list[object]) -> list[tuple]:
        with self._query_lock:
            try:
                return self._connection.execute(query, params).fetchall()
            except duckdb.Error as exc:
                raise ValueError(f"text_retrieve body query failed (RE2/grep semantics): {exc}") from exc

    def _prime_snippets(self, rows: pd.DataFrame) -> None:
        if rows.empty:
            return
        datasets = rows.get("dataset")
        if datasets is None:
            return
        dataset_values = datasets.astype(str)
        for dataset in dataset_values.unique():
            ids = rows.loc[dataset_values == dataset, "text_id"].astype(str).drop_duplicates()
            missing = [text_id for text_id in ids if (dataset, text_id) not in self._snippets]
            shards = self._shards(dataset)
            if not missing or not shards:
                continue
            placeholders = ",".join("?" for _ in missing)
            body_rows = self._body_query(
                "SELECT CAST(text_id AS VARCHAR), substr(body, 1, ?) FROM read_parquet(?) "
                f"WHERE CAST(text_id AS VARCHAR) IN ({placeholders})",
                [self.snippet_chars, shards, *missing],
            )
            for text_id, snippet in body_rows:
                self._snippets[(dataset, str(text_id))] = str(snippet or "")

    def _snippet(self, dataset: str, text_id: str) -> str:
        if not dataset or not text_id:
            return ""
        key = (dataset, text_id)
        cached = self._snippets.get(key)
        if cached is None:
            shards = self._shards(dataset)
            rows = (
                self._body_query(
                    "SELECT substr(body, 1, ?) FROM read_parquet(?) WHERE text_id = ? LIMIT 1",
                    [self.snippet_chars, shards, text_id],
                )
                if shards
                else []
            )
            cached = str(rows[0][0]) if rows and rows[0][0] is not None else ""
            self._snippets[key] = cached
        return cached



def _read_index(path: "str | Path | None") -> pd.DataFrame:
    candidate = Path(path) if path is not None else None
    return pd.read_parquet(candidate) if candidate is not None and candidate.exists() else pd.DataFrame()



def validate_pattern(pattern: str) -> str:
    """Gate the sub-agent pattern to the RE2/grep contract before any scan.

    Length is capped and the pattern is compiled by DuckDB's RE2 up front:
    unsupported constructs (backreferences, lookaround) fail here with a
    fixable message instead of silently matching nothing or falling back to a
    backtracking engine."""
    text = str(pattern or "").strip()
    if not text:
        raise ValueError("pattern must be a non-empty grep/regex string")
    if len(text) > MAX_PATTERN_CHARS:
        raise ValueError(f"pattern too long (>{MAX_PATTERN_CHARS} chars); use a shorter grep pattern")
    try:
        duckdb.execute("SELECT regexp_matches('', ?)", [text]).fetchall()
    except duckdb.Error as exc:
        raise ValueError(
            f"unsupported regex (RE2/grep semantics — no backreferences or lookaround): {exc}"
        ) from exc
    return text



def _candidate_terms(ts_code: str, company_terms: list[str] | None = None) -> list[str]:
    code = str(ts_code or "").strip()
    terms = [code] if code else []
    if "." in code:
        terms.append(code.split(".", 1)[0])
    terms.extend(str(term).strip() for term in (company_terms or []) if str(term).strip())
    seen: set[str] = set()
    ordered: list[str] = []
    for term in terms:
        if term and term not in seen:
            seen.add(term)
            ordered.append(term)
    return ordered
