"""Daily equity / benchmark series assembly for console charts.

Pure read-model over frozen run artifacts — no raw-lake access and no math in
the browser: strategy daily returns come from each result window's
``detailed_return.json`` ``equity_curve``, the CSI 300 series from the run's
``style_<prefix>.json`` rollups (written by the pipeline at replay time from
snapshot-frozen data), and cumulative curves + running drawdowns are computed
HERE so the SPA only plots the arrays it receives.

Read-model semantics match registry.py: missing artifacts yield empty
series (the chart shows a hint), they never raise.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from autotrade.environment.style_analysis import BENCHMARK_LABEL, daily_returns_from_curve


# ---------------------------------------------------------------------------
# artifact readers
# ---------------------------------------------------------------------------
@lru_cache(maxsize=512)
def _window_returns_cached(detailed_path: str, mtime_ns: int) -> tuple[tuple[str, float], ...]:
    del mtime_ns  # cache key component only
    payload = json.loads(Path(detailed_path).read_text(encoding="utf-8"))
    curve = payload.get("equity_curve")
    if not isinstance(curve, dict) or not curve:
        return ()
    return tuple(daily_returns_from_curve(curve, float(payload.get("initial_cash") or 0.0)))


def _window_returns(window_dir: Path) -> tuple[tuple[str, float], ...]:
    path = window_dir / "detailed_return.json"
    try:
        return _window_returns_cached(str(path), path.stat().st_mtime_ns)
    except (OSError, ValueError):
        return ()


def run_series(experiment_dir: Path, run_id: str | None, prefix: str) -> list[tuple[str, float]]:
    """Daily returns chained across a run's result windows named ``prefix*``."""
    if not run_id:
        return []
    results_root = experiment_dir / "artifacts" / str(run_id) / "results"
    if not results_root.is_dir():
        return []
    merged: list[tuple[str, float]] = []
    for window_dir in sorted(results_root.iterdir()):
        if window_dir.is_dir() and window_dir.name.startswith(prefix):
            merged.extend(_window_returns(window_dir))
    return _chain([merged])


def _rollup_benchmark(experiment_dir: Path, run_id: str | None, prefix: str) -> dict[str, float]:
    """Benchmark daily returns persisted in the run's style rollup."""
    if not run_id:
        return {}
    path = experiment_dir / "artifacts" / str(run_id) / "results" / f"style_{prefix}.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(date): float(value) for date, value in payload.get("benchmark_daily", [])}


SERIES_LABELS = {"valid": "策略（验证）", "test": "策略（测试）", "heldout": "策略（Held-out）"}


def _chain(parts: list[list[tuple[str, float]]]) -> list[tuple[str, float]]:
    """Date-sorted union of daily-return parts; the first value per date wins."""
    seen: set[str] = set()
    out: list[tuple[str, float]] = []
    for part in parts:
        for date, value in sorted(part):
            if date not in seen:
                seen.add(date)
                out.append((date, value))
    return sorted(out)


# ---------------------------------------------------------------------------
# curve math (server-side; the SPA only renders)
# ---------------------------------------------------------------------------
def _curve_entry(key: str, label: str, series: list[tuple[str, float]]) -> dict[str, object]:
    dates: list[str] = []
    cum: list[float] = []
    drawdown: list[float] = []
    equity = 1.0
    peak = 1.0
    for date, value in series:
        equity *= 1.0 + value
        peak = max(peak, equity)
        dates.append(date)
        cum.append(round(equity - 1.0, 6))
        drawdown.append(round(equity / peak - 1.0, 6))
    return {
        "key": key,
        "label": label,
        "dates": dates,
        "cum": cum,
        "drawdown": drawdown,
        "final": cum[-1] if cum else None,
    }


def _benchmark_entry(bench: dict[str, float], dates: list[str]) -> dict[str, object]:
    series = [(date, bench[date]) for date in sorted(set(dates)) if date in bench]
    return _curve_entry("benchmark", BENCHMARK_LABEL, series)


# ---------------------------------------------------------------------------
# payload assembly
# ---------------------------------------------------------------------------
def fold_equity_payload(
    experiments_root: Path, experiment_id: str, epoch_id: str, fold_id: str
) -> dict[str, object]:
    from .registry import _read_ledger_records, latest_fold_records, resolve_experiment_dir

    experiment_dir = resolve_experiment_dir(experiments_root, experiment_id)
    record = latest_fold_records(_read_ledger_records(experiment_dir)).get((epoch_id, fold_id))
    if record is None:
        raise KeyError(f"fold {epoch_id}/{fold_id} has no ledger record")
    run_id = str(record.get("run_id") or "")
    payload: dict[str, object] = {}
    for prefix in ("valid", "test"):
        strategy = run_series(experiment_dir, run_id, prefix)
        bench = _rollup_benchmark(experiment_dir, run_id, prefix)
        payload[prefix] = {
            "series": [_curve_entry(prefix, SERIES_LABELS[prefix], strategy)],
            "benchmark": _benchmark_entry(bench, [date for date, _ in strategy]),
        }
    return payload


def experiment_equity_payload(experiments_root: Path, experiment_id: str) -> dict[str, object]:
    from .registry import (
        _read_ledger_records,
        latest_fold_records,
        latest_heldout_records,
        resolve_experiment_dir,
    )

    experiment_dir = resolve_experiment_dir(experiments_root, experiment_id)
    records = _read_ledger_records(experiment_dir)
    folds = list(latest_fold_records(records).values())
    folds.sort(key=lambda r: (str(r.get("epoch_id")), str(r.get("test_period") or r.get("fold_id"))))
    heldout = latest_heldout_records(records)
    heldout_runs = sorted({str(r.get("run_id") or "") for r in heldout if r.get("run_id")})

    chains = {
        "valid": _chain([run_series(experiment_dir, str(r.get("run_id") or ""), "valid") for r in folds]),
        "test": _chain([run_series(experiment_dir, str(r.get("run_id") or ""), "test") for r in folds]),
        "heldout": _chain([run_series(experiment_dir, run_id, "heldout") for run_id in heldout_runs]),
    }
    series = [
        _curve_entry(key, SERIES_LABELS[key], chain) for key, chain in chains.items() if chain
    ]

    bench: dict[str, float] = {}
    for record in folds:
        run_id = str(record.get("run_id") or "")
        for prefix in ("valid", "test"):
            bench.update(_rollup_benchmark(experiment_dir, run_id, prefix))
    for run_id in heldout_runs:
        bench.update(_rollup_benchmark(experiment_dir, run_id, "heldout"))
    all_dates = [date for chain in chains.values() for date, _ in chain]
    return {"series": series, "benchmark": _benchmark_entry(bench, all_dates)}
