"""Factor contribution analysis (Shapley values) for backtest results.

Toggled by ``factor_attribution_enabled``. The strategy may emit one column per
registered factor (``factor_<id>``) alongside the combined ``factor_score``;
each coalition's score is the mean of its members' cross-section-normalized
columns, evaluated through the same composition/order/replay pipeline (with the
already-computed NL scores — no extra LLM calls). Exact Shapley enumeration is
used up to ``max_exact_factors``; larger registries fall back to permutation
sampling. Results land in ``results/<phase>_<idx>/factor_attribution.json``
together with each factor's agent-written rationale.
"""

from __future__ import annotations

import math
import random
from itertools import combinations
from typing import Callable

import pandas as pd

from hl_trader.environment.backtest_engine import cross_section_normalize

FACTOR_COLUMN_PREFIX = "factor_"
MAX_EXACT_FACTORS = 8
PERMUTATION_SAMPLES = 32


def factor_column(factor_id: str) -> str:
    return f"{FACTOR_COLUMN_PREFIX}{factor_id}"


def available_factor_columns(candidates: pd.DataFrame, factor_ids: list[str]) -> dict[str, str]:
    """Registered factors that have a per-factor score column in the pool."""
    return {fid: factor_column(fid) for fid in factor_ids if factor_column(fid) in candidates.columns}


def shapley_attribution(
    candidates: pd.DataFrame,
    factor_ids: list[str],
    evaluate: Callable[[pd.Series], float],
    *,
    max_exact_factors: int = MAX_EXACT_FACTORS,
    permutation_samples: int = PERMUTATION_SAMPLES,
    seed: int = 7,
) -> dict[str, dict[str, float]]:
    """Per-factor Shapley value of the replayed total return.

    ``evaluate`` maps a candidate ``factor_score`` series to the replayed total
    return; the empty coalition scores zero everywhere (no trades).
    """
    columns = available_factor_columns(candidates, factor_ids)
    ids = list(columns)
    normalized = {fid: cross_section_normalize(pd.to_numeric(candidates[col], errors="coerce").fillna(0.0)) for fid, col in columns.items()}
    cache: dict[frozenset[str], float] = {}

    def coalition_value(members: frozenset[str]) -> float:
        if members not in cache:
            if not members:
                cache[members] = evaluate(pd.Series(0.0, index=candidates.index))
            else:
                stacked = pd.concat([normalized[fid] for fid in members], axis=1)
                cache[members] = evaluate(stacked.mean(axis=1))
        return cache[members]

    shapley: dict[str, float] = {fid: 0.0 for fid in ids}
    if len(ids) <= max_exact_factors:
        n = len(ids)
        for fid in ids:
            others = [other for other in ids if other != fid]
            for size in range(n):
                for subset in combinations(others, size):
                    weight = math.factorial(size) * math.factorial(n - size - 1) / math.factorial(n)
                    members = frozenset(subset)
                    shapley[fid] += weight * (coalition_value(members | {fid}) - coalition_value(members))
    else:
        rng = random.Random(seed)
        for _ in range(permutation_samples):
            order = ids[:]
            rng.shuffle(order)
            members: frozenset[str] = frozenset()
            for fid in order:
                with_f = frozenset(members | {fid})
                shapley[fid] += (coalition_value(with_f) - coalition_value(members)) / permutation_samples
                members = with_f

    return {
        fid: {
            "shapley_value": shapley[fid],
            "standalone_return": coalition_value(frozenset({fid})),
        }
        for fid in ids
    }


def build_attribution_report(
    candidates: pd.DataFrame,
    factors: list[dict[str, object]],
    evaluate: Callable[[pd.Series], float],
    *,
    full_return: float,
) -> dict[str, object]:
    """Attribution report joined with the agent's per-factor rationales."""
    factor_ids = [str(entry["id"]) for entry in factors]
    columns = available_factor_columns(candidates, factor_ids)
    report: dict[str, object] = {
        "method": "shapley",
        "evaluated_on": "replayed_total_return",
        "coalition_rule": "mean_of_cross_section_normalized_member_columns",
        "full_total_return": full_return,
        "factors": [],
        "skipped": None,
    }
    if not factor_ids:
        report["skipped"] = "no_registered_factors"
        return report
    if not columns:
        report["skipped"] = (
            "no per-factor columns (factor_<id>) in the candidate pool; "
            "emit them from generate_candidates() to enable attribution"
        )
        report["factors"] = [
            {"id": str(e["id"]), "rationale": str(e.get("rationale", ""))} for e in factors
        ]
        return report
    values = shapley_attribution(candidates, factor_ids, evaluate)
    for entry in factors:
        fid = str(entry["id"])
        row: dict[str, object] = {"id": fid, "rationale": str(entry.get("rationale", ""))}
        if fid in values:
            row.update(values[fid])
        else:
            row["skipped"] = "no_factor_column"
        report["factors"].append(row)
    return report
