from __future__ import annotations

from dataclasses import dataclass
from itertools import product
from typing import Any

import pandas as pd


@dataclass(frozen=True)
class FormulaicScoreRule:
    column: str
    ascending: bool
    weight: float = 1.0


@dataclass(frozen=True)
class FormulaicParameters:
    top_n: int = 50
    max_pe_ttm_quantile: float = 0.4
    max_pb_quantile: float = 0.5
    min_amount_quantile: float = 0.3


def parameter_grid(space: dict[str, Any]) -> list[FormulaicParameters]:
    top_n = space.get("top_n", [50])
    pe = space.get("max_pe_ttm_quantile", [0.4])
    pb = space.get("max_pb_quantile", [0.5])
    amount = space.get("min_amount_quantile", space.get("min_turnover_quantile", [0.3]))
    return [
        FormulaicParameters(int(n), float(p), float(b), float(a))
        for n, p, b, a in product(top_n, pe, pb, amount)
    ]


def score_cross_section(frame: pd.DataFrame, rules: list[FormulaicScoreRule]) -> pd.DataFrame:
    if "ts_code" not in frame.columns:
        raise ValueError("cross section frame must include ts_code")
    if not rules:
        raise ValueError("at least one scoring rule is required")
    scored = frame.copy()
    score = pd.Series(0.0, index=scored.index)
    for rule in rules:
        if rule.column not in scored.columns:
            raise ValueError(f"missing scoring column: {rule.column}")
        rank = scored[rule.column].rank(ascending=rule.ascending, pct=True, na_option="bottom")
        score += rank.fillna(1.0) * rule.weight
    scored["score"] = score
    return scored.sort_values(["score", "ts_code"], ascending=[True, True]).reset_index(drop=True)


def select_formulaic_candidates(cross_section: pd.DataFrame, params: FormulaicParameters) -> list[str]:
    frame = cross_section.copy()
    if "is_suspended" in frame.columns:
        frame = frame[~frame["is_suspended"].fillna(False)]
    for column in ("pe_ttm", "pb", "amount_ma20", "ret_20d"):
        if column not in frame.columns:
            raise ValueError(f"missing required feature column: {column}")
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame[(frame["pe_ttm"] > 0) & (frame["pb"] > 0) & frame["amount_ma20"].notna()]
    if frame.empty:
        return []
    frame = frame[frame["pe_ttm"] <= frame["pe_ttm"].quantile(params.max_pe_ttm_quantile)]
    frame = frame[frame["pb"] <= frame["pb"].quantile(params.max_pb_quantile)]
    frame = frame[frame["amount_ma20"] >= frame["amount_ma20"].quantile(params.min_amount_quantile)]
    if frame.empty:
        return []
    scored = score_cross_section(frame, [
        FormulaicScoreRule("pe_ttm", ascending=True, weight=1.0),
        FormulaicScoreRule("pb", ascending=True, weight=0.8),
        FormulaicScoreRule("ret_20d", ascending=False, weight=0.4),
        FormulaicScoreRule("amount_ma20", ascending=False, weight=0.2),
    ])
    return scored["ts_code"].astype(str).head(params.top_n).tolist()
