"""Daily equity / benchmark series assembly for console charts.

Every replay result window stores ``detailed_return.json`` with an
``equity_curve`` mapping (``YYYYMMDD`` -> EOD account value). This module
turns those into daily simple-return series — chained across a fold's
validation sub-windows, across folds (test months), and across held-out
periods — and aligns a CSI 300 (000300.SH) benchmark series from
``data/raw/index_daily``. The SPA compounds returns into cumulative curves
and drawdowns client-side, so payloads stay small.

Read-model semantics match registry.py: missing artifacts yield empty
series (the chart shows a hint), they never raise.
"""

from __future__ import annotations

import json
import math
from functools import lru_cache
from pathlib import Path

BENCHMARK_TS_CODE = "000300.SH"
BENCHMARK_LABEL = "沪深300"


# ---------------------------------------------------------------------------
# benchmark (CSI 300 daily returns)
# ---------------------------------------------------------------------------
@lru_cache(maxsize=32)
def _benchmark_year(repo_root: str, year: str) -> tuple[tuple[str, float], ...]:
    path = Path(repo_root) / "data" / "raw" / "index_daily" / f"ts_code={BENCHMARK_TS_CODE}" / f"year={year}.parquet"
    if not path.exists():
        return ()
    import pandas as pd

    df = pd.read_parquet(path, columns=["trade_date", "pct_chg"])
    rows = []
    for date, pct in zip(df["trade_date"], df["pct_chg"]):
        value = float(pct)
        if math.isfinite(value):
            rows.append((str(date), value / 100.0))
    return tuple(rows)


def benchmark_returns(repo_root: Path, dates: list[str]) -> list[list[object]]:
    """CSI 300 daily returns restricted to ``dates`` (as [date, r] pairs)."""
    if not dates:
        return []
    table: dict[str, float] = {}
    for year in sorted({date[:4] for date in dates}):
        table.update(dict(_benchmark_year(str(repo_root), year)))
    return [[date, table[date]] for date in dates if date in table]


# ---------------------------------------------------------------------------
# strategy daily returns from replay result windows
# ---------------------------------------------------------------------------
@lru_cache(maxsize=512)
def _window_returns_cached(detailed_path: str, mtime_ns: int) -> tuple[tuple[str, float], ...]:
    del mtime_ns  # cache key component only
    payload = json.loads(Path(detailed_path).read_text(encoding="utf-8"))
    curve = payload.get("equity_curve")
    if not isinstance(curve, dict) or not curve:
        return ()
    previous = float(payload.get("initial_cash") or 0.0)
    out: list[tuple[str, float]] = []
    for date in sorted(curve):
        value = float(curve[date])
        if previous > 0:
            out.append((str(date), value / previous - 1.0))
        previous = value
    return tuple(out)


def _window_returns(window_dir: Path) -> tuple[tuple[str, float], ...]:
    path = window_dir / "detailed_return.json"
    try:
        return _window_returns_cached(str(path), path.stat().st_mtime_ns)
    except (OSError, ValueError, json.JSONDecodeError):
        return ()


def run_series(experiment_dir: Path, run_id: str | None, prefix: str) -> list[list[object]]:
    """Daily returns chained across a run's result windows named ``prefix*``.

    Validation replays in disjoint sub-windows (valid_000..N); chaining their
    daily returns in date order reconstructs the period's daily series.
    Duplicate dates (defensive) keep the first occurrence.
    """
    if not run_id:
        return []
    results_root = experiment_dir / "artifacts" / str(run_id) / "results"
    if not results_root.is_dir():
        return []
    merged: list[tuple[str, float]] = []
    for window_dir in sorted(results_root.iterdir()):
        if window_dir.is_dir() and window_dir.name.startswith(prefix):
            merged.extend(_window_returns(window_dir))
    seen: set[str] = set()
    series: list[list[object]] = []
    for date, value in sorted(merged):
        if date not in seen:
            seen.add(date)
            series.append([date, value])
    return series


def _chain(parts: list[list[list[object]]]) -> list[list[object]]:
    """Concatenate per-session series in order, dropping repeated dates."""
    seen: set[str] = set()
    out: list[list[object]] = []
    for part in parts:
        for date, value in part:
            if date not in seen:
                seen.add(date)
                out.append([date, value])
    return out


# ---------------------------------------------------------------------------
# payload assembly
# ---------------------------------------------------------------------------
def _series_entry(key: str, label: str, points: list[list[object]]) -> dict[str, object]:
    return {"key": key, "label": label, "points": points}


def fold_equity(experiment_dir: Path, record: dict[str, object], repo_root: Path) -> dict[str, object]:
    """Per-fold daily series, validation and test kept separate (guarded view)."""
    run_id = record.get("run_id")
    valid = run_series(experiment_dir, str(run_id) if run_id else None, "valid")
    test = run_series(experiment_dir, str(run_id) if run_id else None, "test")
    return {
        "valid": {
            "series": [_series_entry("valid", "策略（验证）", valid)],
            "benchmark": _series_entry("benchmark", BENCHMARK_LABEL, benchmark_returns(repo_root, [d for d, _ in valid])),
        },
        "test": {
            "series": [_series_entry("test", "策略（测试）", test)],
            "benchmark": _series_entry("benchmark", BENCHMARK_LABEL, benchmark_returns(repo_root, [d for d, _ in test])),
        },
    }


def fold_equity_payload(
    experiments_root: Path, experiment_id: str, epoch_id: str, fold_id: str, repo_root: Path
) -> dict[str, object]:
    from .registry import _read_ledger_records, latest_fold_records, resolve_experiment_dir

    experiment_dir = resolve_experiment_dir(experiments_root, experiment_id)
    record = latest_fold_records(_read_ledger_records(experiment_dir)).get((epoch_id, fold_id))
    if record is None:
        raise KeyError(f"fold {epoch_id}/{fold_id} has no ledger record")
    return fold_equity(experiment_dir, record, repo_root)


def experiment_equity_payload(experiments_root: Path, experiment_id: str, repo_root: Path) -> dict[str, object]:
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
    return experiment_equity(experiment_dir, folds, heldout, repo_root)


def experiment_equity(
    experiment_dir: Path,
    fold_records: list[dict[str, object]],
    heldout_records: list[dict[str, object]],
    repo_root: Path,
) -> dict[str, object]:
    """Experiment-level chained daily series: valid chain, test chain, held-out
    chain, plus one benchmark aligned to the union of all strategy dates."""
    valid = _chain([
        run_series(experiment_dir, str(r.get("run_id") or ""), "valid") for r in fold_records
    ])
    test = _chain([
        run_series(experiment_dir, str(r.get("run_id") or ""), "test") for r in fold_records
    ])
    heldout = _chain([
        run_series(experiment_dir, str(r.get("run_id") or ""), "heldout") for r in heldout_records
    ])
    series = [
        entry
        for entry in (
            _series_entry("valid", "策略（验证）", valid),
            _series_entry("test", "策略（测试）", test),
            _series_entry("heldout", "策略（Held-out）", heldout),
        )
        if entry["points"]
    ]
    all_dates = sorted({date for entry in series for date, _ in entry["points"]})
    return {
        "series": series,
        "benchmark": _series_entry("benchmark", BENCHMARK_LABEL, benchmark_returns(repo_root, all_dates)),
    }
