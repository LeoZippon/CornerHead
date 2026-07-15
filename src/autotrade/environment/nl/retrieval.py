"""Point-in-time text retrieval over the snapshot/replay text libraries.

Pure data access for the NL Sub Agent: DuckDB/RE2 regex over the text index
and per-dataset body shards (column projection + LIMIT; the multi-GB corpus is
never resident in host memory). No LLM dependency — the agent loop lives in
``nl/engine.py``.
"""

from __future__ import annotations

import re
import threading
from datetime import datetime
from pathlib import Path

import duckdb
import pandas as pd

from autotrade.environment.data.contracts import text_dataset_visible_cutoff
from autotrade.environment.snapshot import to_cn_timestamps

MAX_PATTERN_CHARS = 256


class TextRetriever:
    """Grep-style retrieval over the snapshot text index and as-of text library.

    The NL Sub Agent supplies a regex pattern (case-insensitive grep semantics,
    RE2 engine — linear-time matching, so an adversarial or accidental
    catastrophic-backtracking pattern cannot pin the host CPU; unsupported
    constructs are rejected with a fixable error). Titles/codes are matched
    first, then full bodies when more results are needed; results rank
    candidate-related hits before broad background hits, recency second.

    Bodies live in per-dataset parquet shards under ``text_library/`` and are
    scanned in place via DuckDB with column projection and result limits — the
    multi-GB corpus is never resident in host memory; only the returned
    snippets are cached. The frozen research snapshot index/library plus the
    replay-slot index/library are read together. ``as_of`` rolls the corpus on
    the same cron refresh nodes as the agent Timeview: frozen rows are always
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
        self._frozen_mask = (
            self.index["_source"].astype(str) == "frozen"
            if not self.index.empty and "_source" in self.index.columns
            else pd.Series(True, index=self.index.index)
        )
        self._visible_cache_key: str | None = None
        self._visible_cache: pd.DataFrame | None = None
        self._snippets: dict[tuple[str, str], str] = {}
        self._query_lock = threading.Lock()  # DuckDB default connection is not thread-safe

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
    ) -> list[dict[str, object]]:
        """Raises ValueError for patterns outside the RE2/grep contract."""
        index = self._visible_index()
        if index.empty:
            return []
        regex = _validate_pattern(pattern)
        pattern_hit = self._title_code_match(index, regex)
        own_hit = self._candidate_mask(index, ts_code=ts_code, company_terms=company_terms)
        hits = index[pattern_hit].copy()
        hits["_relevance"] = "background"
        hits["_rank"] = 20
        hits.loc[own_hit[own_hit].index.intersection(hits.index), "_rank"] = 40
        hits.loc[own_hit[own_hit].index.intersection(hits.index), "_relevance"] = "candidate"
        if search_bodies and len(hits) < max_results:
            # A stock-scoped NL request searches bodies only for rows already
            # linked to that company by the PIT index (code/name/title). Broad
            # title hits remain available, while market/theme body searches use
            # ctx.nl(prompt=...) without a ts_code. This avoids repeatedly
            # scanning unrelated multi-GB body shards for every candidate.
            body_scope = index[own_hit] if bool(_candidate_terms(ts_code, company_terms)) else index
            body_idx = self._grep_bodies(
                body_scope,
                regex,
                exclude=set(hits["text_id"].astype(str)),
                limit=max_results * 3,
                restrict_to_index=body_scope is not index,
            )
            if body_idx:
                body_rows = index[index["text_id"].astype(str).isin(body_idx)].copy()
                body_own = self._candidate_mask(body_rows, ts_code=ts_code, company_terms=company_terms)
                body_rows["_relevance"] = "background"
                body_rows["_rank"] = 10
                body_rows.loc[body_own[body_own].index.intersection(body_rows.index), "_rank"] = 30
                body_rows.loc[body_own[body_own].index.intersection(body_rows.index), "_relevance"] = "candidate"
                hits = pd.concat([hits, body_rows], ignore_index=False)
        if hits.empty:
            return []
        hits = hits.drop_duplicates(subset=["text_id"], keep="first")
        sort_cols = ["_rank"] + (["available_at"] if "available_at" in hits.columns else [])
        hits = hits.sort_values(sort_cols, ascending=[False] * len(sort_cols))
        records = []
        for row in hits.head(max_results).to_dict("records"):
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

    def _candidate_mask(
        self, frame: pd.DataFrame, *, ts_code: str, company_terms: list[str] | None = None
    ) -> pd.Series:
        terms = _candidate_terms(ts_code, company_terms)
        if not terms:
            return pd.Series(False, index=frame.index)
        codes = frame.get("ts_codes", pd.Series("", index=frame.index)).astype(str)
        titles = frame.get("title", pd.Series("", index=frame.index)).astype(str)
        code = str(ts_code or "").strip()
        mask = (
            codes.str.contains(code, case=False, regex=False, na=False)
            if code
            else pd.Series(False, index=frame.index)
        )
        for term in terms:
            escaped = re.escape(term)
            mask = mask | titles.str.contains(escaped, case=False, regex=True, na=False)
        return mask

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
            con = duckdb.connect()
            try:
                con.register("visible_index", frame)
                rows = con.execute(
                    "SELECT row FROM visible_index "
                    "WHERE regexp_matches(title, ?, 'i') OR regexp_matches(ts_codes, ?, 'i')",
                    [regex, regex],
                ).fetchall()
            except duckdb.Error as exc:
                raise ValueError(f"unsupported regex (RE2/grep semantics): {exc}") from exc
            finally:
                con.close()
        matched = {row[0] for row in rows}
        return pd.Series([idx in matched for idx in index.index], index=index.index)

    def _grep_bodies(
        self,
        index: pd.DataFrame,
        regex: str,
        *,
        exclude: set[str],
        limit: int,
        restrict_to_index: bool = False,
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
            allowed = (
                index.loc[datasets.astype(str) == dataset, "text_id"].astype(str).drop_duplicates().tolist()
                if restrict_to_index
                else []
            )
            if restrict_to_index and not allowed:
                continue
            if allowed:
                placeholders = ",".join("?" for _ in allowed)
                rows = self._body_query(
                    "SELECT text_id, substr(body, 1, ?) FROM read_parquet(?) "
                    f"WHERE CAST(text_id AS VARCHAR) IN ({placeholders}) "
                    "AND regexp_matches(body, ?, 'i') LIMIT ?",
                    [self.snippet_chars, shards, *allowed, regex, limit + len(exclude)],
                )
            else:
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
                return duckdb.execute(query, params).fetchall()
            except duckdb.Error as exc:
                raise ValueError(f"text_retrieve body query failed (RE2/grep semantics): {exc}") from exc

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



def _validate_pattern(pattern: str) -> str:
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
