"""Decision-time company context for NL Sub Agent calls.

Answers "what is this company known to do at the decision time" from PIT-safe
sources only: as-of names and listing info from the snapshot universe, industry
membership, and main-business composition from visible fina_mainbz_vip events.
The current stock_company.introduction has no historical visibility and is
never used here.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd


def build_company_contexts(snapshot_dir: str | Path, ts_codes: list[str]) -> dict[str, dict[str, object]]:
    snapshot_dir = Path(snapshot_dir)
    universe = pd.read_parquet(snapshot_dir / "universe.parquet")
    universe["ts_code"] = universe["ts_code"].astype(str)
    universe = universe.set_index("ts_code")
    mainbz = _visible_mainbz(snapshot_dir / "fundamentals.parquet")
    contexts: dict[str, dict[str, object]] = {}
    for code in ts_codes:
        context: dict[str, object] = {"ts_code": code, "sources": []}
        if code in universe.index:
            row = universe.loc[code]
            context["name"] = str(row.get("name_asof", row.get("name", "")))
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
        contexts[code] = context
    return contexts


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
