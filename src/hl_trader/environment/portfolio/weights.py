from __future__ import annotations


def normalize_targets(weights: dict[str, float], *, max_weight: float | None = None) -> dict[str, float]:
    cleaned = {code: max(0.0, float(weight)) for code, weight in weights.items() if float(weight) > 0}
    if max_weight is not None and not 0 < max_weight <= 1:
        raise ValueError("max_weight must be in (0, 1]")
    if max_weight is not None and cleaned and max_weight * len(cleaned) < 1.0:
        raise ValueError("max_weight is infeasible for the number of positive weights")
    return _normalize_with_cap(cleaned, max_weight=max_weight)


def _normalize_with_cap(weights: dict[str, float], *, max_weight: float | None) -> dict[str, float]:
    if not weights:
        return {}
    if max_weight is not None:
        capped: dict[str, float] = {}
        remaining_codes = set(weights)
        remaining_target = 1.0
        while remaining_codes:
            raw_total = sum(weights[code] for code in remaining_codes)
            if raw_total <= 0:
                break
            allocation = {code: remaining_target * weights[code] / raw_total for code in remaining_codes}
            over_cap = {code for code, weight in allocation.items() if weight > max_weight}
            if not over_cap:
                capped.update(allocation)
                break
            for code in over_cap:
                capped[code] = max_weight
                remaining_codes.remove(code)
                remaining_target -= max_weight
        return {code: weight for code, weight in sorted(capped.items())}
    total = sum(weights.values())
    if total <= 0:
        return {}
    return {code: weight / total for code, weight in sorted(weights.items())}


def equal_weight_targets(codes: list[str], *, max_names: int | None = None) -> dict[str, float]:
    if max_names is not None and max_names <= 0:
        raise ValueError("max_names must be positive when provided")
    selected = list(dict.fromkeys(codes))
    if max_names is not None:
        selected = selected[:max_names]
    if not selected:
        return {}
    weight = 1.0 / len(selected)
    return {code: weight for code in selected}
