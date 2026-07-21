"""Decision-time company context for NL Sub Agent calls.

Answers "what is this company known to do at the decision time" from PIT-safe
sources only: as-of names and listing info from the snapshot universe, industry
membership, and main-business composition from visible fina_mainbz_vip events.
The current stock_company.introduction has no historical visibility and is
never used here.

The snapshot is frozen for the whole backtest, so the per-code context is
constant once built. :class:`CompanyContextStore` loads ``universe.parquet`` and
``fundamentals.parquet`` once and memoizes each ts_code's context, so a per-tick
``ctx.nl()`` call does not re-read both parquet files every time.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd


class CompanyContextStore:
    """Lazily load the frozen-snapshot company-context sources once, then build and
    cache each ts_code's PIT context from memory."""

    def __init__(self, snapshot_dir: str | Path) -> None:
        self._snapshot_dir = Path(snapshot_dir)
        self._universe: pd.DataFrame | None = None
        self._mainbz: dict[str, list[str]] | None = None
        self._cache: dict[str, dict[str, object]] = {}

    def _ensure_loaded(self) -> None:
        if self._universe is not None:
            return
        universe = pd.read_parquet(self._snapshot_dir / "universe.parquet")
        universe["ts_code"] = universe["ts_code"].astype(str)
        self._universe = universe.set_index("ts_code")
        self._mainbz = _visible_mainbz(self._snapshot_dir / "fundamentals.parquet")

    def context(self, ts_code: str) -> dict[str, object]:
        code = str(ts_code)
        cached = self._cache.get(code)
        if cached is None:
            self._ensure_loaded()
            assert self._universe is not None and self._mainbz is not None
            cached = _build_one(code, self._universe, self._mainbz)
            self._cache[code] = cached
        return cached


def _build_one(
    code: str, universe: pd.DataFrame, mainbz: dict[str, list[str]]
) -> dict[str, object]:
    context: dict[str, object] = {"ts_code": code, "sources": []}
    if code in universe.index:
        row = universe.loc[code]
        name = row.get("name")
        context["name"] = "" if name is None or pd.isna(name) else str(name)
        context["exchange"] = str(row.get("exchange", ""))
        if pd.notna(row.get("l1_name", None)):
            context["industry_l1"] = str(row.get("l1_name"))
        context["sources"].append("universe_as_of")
    business = mainbz.get(code)
    if business:
        context["main_business"] = business
        context["sources"].append("fina_mainbz_vip_visible_events")
    if len(context["sources"]) == 0:
        context["context"] = "insufficient_company_information"
    return context


def _visible_mainbz(fundamentals_path: Path, *, top_items: int = 5) -> dict[str, list[str]]:
    if not fundamentals_path.exists():
        return {}
    events = pd.read_parquet(fundamentals_path)
    if events.empty or "dataset" not in events.columns:
        return {}
    rows = events[events["dataset"] == "fina_mainbz_vip"]
    if rows.empty or "bz_item" not in rows.columns:
        return {}
    rows = rows.sort_values("available_at")
    out: dict[str, list[str]] = {}
    for code, group in rows.groupby(rows["ts_code"].astype(str)):
        latest_end = group["end_date"].astype(str).max() if "end_date" in group.columns else None
        if latest_end is not None:
            group = group[group["end_date"].astype(str) == latest_end]
        out[code] = [str(item) for item in group["bz_item"].dropna().unique()[:top_items]]
    return out
