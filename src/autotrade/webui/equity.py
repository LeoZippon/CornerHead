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
import math
from functools import lru_cache
from pathlib import Path

from autotrade.environment.replay_stats import TRADING_DAYS_PER_YEAR
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


@lru_cache(maxsize=512)
def _window_exposure_cached(
    window_dir_str: str, positions_mtime_ns: int, detailed_mtime_ns: int
) -> tuple[tuple[str, float, float], ...]:
    """(date, long_weight, short_weight) per EOD: position market value / equity."""
    del positions_mtime_ns, detailed_mtime_ns  # cache key components only
    import pandas as pd

    window_dir = Path(window_dir_str)
    try:
        payload = json.loads((window_dir / "detailed_return.json").read_text(encoding="utf-8"))
        frame = pd.read_parquet(window_dir / "positions_eod.parquet", columns=["date", "side", "market_value"])
    except (OSError, ValueError, KeyError):
        return ()
    curve = payload.get("equity_curve")
    if not isinstance(curve, dict) or not curve:
        return ()
    frame["date"] = frame["date"].astype(str)
    gross = frame.assign(mv=frame["market_value"].abs()).groupby(["date", "side"])["mv"].sum()
    out: list[tuple[str, float, float]] = []
    for date, equity in sorted(curve.items()):
        eq = float(equity)
        if not (eq > 0):
            continue
        out.append((
            str(date),
            round(float(gross.get((str(date), "long"), 0.0)) / eq, 4),
            round(float(gross.get((str(date), "short"), 0.0)) / eq, 4),
        ))
    return tuple(out)


def _window_exposure(window_dir: Path) -> tuple[tuple[str, float, float], ...]:
    positions = window_dir / "positions_eod.parquet"
    detailed = window_dir / "detailed_return.json"
    try:
        return _window_exposure_cached(
            str(window_dir), positions.stat().st_mtime_ns, detailed.stat().st_mtime_ns
        )
    except (OSError, ValueError):
        return ()


def _run_rows(experiment_dir: Path, run_id: str | None, prefix: str, window: str | None, reader):
    if not run_id:
        return []
    results_root = experiment_dir / "artifacts" / str(run_id) / "results"
    if not results_root.is_dir():
        return []
    if window is not None:
        window_dir = results_root / window
        return _chain([list(reader(window_dir))]) if window_dir.is_dir() else []
    merged: list[tuple] = []
    for window_dir in sorted(results_root.iterdir()):
        if window_dir.is_dir() and window_dir.name.startswith(prefix):
            merged.extend(reader(window_dir))
    return _chain([merged])


def run_series(
    experiment_dir: Path, run_id: str | None, prefix: str, window: str | None = None
) -> list[tuple[str, float]]:
    """Daily returns of a run's result windows.

    ``window`` selects ONE named window. Validation callers must pass the
    fold's recorded window (see ``fold_valid_window``): a fold session leaves
    many overlapping ``valid_*`` attempt windows from different strategy
    versions, and merging them yields a curve no real backtest produced.
    Test/held-out runs write one window per run, so the prefix glob is exact.
    """
    return _run_rows(experiment_dir, run_id, prefix, window, _window_returns)


def run_exposure_series(
    experiment_dir: Path, run_id: str | None, prefix: str, window: str | None = None
) -> list[tuple[str, float, float]]:
    """Daily (date, long_weight, short_weight); same window semantics as returns."""
    return _run_rows(experiment_dir, run_id, prefix, window, _window_exposure)


def fold_valid_window(record: dict[str, object]) -> str | None:
    """Result-window name behind the fold's RECORDED validation metrics.

    The selected (frozen) step's window when one exists; otherwise the last
    step that carries a validation result (matches the record's headline
    metrics for no_update folds). None = no complete validation this fold.
    """
    steps = [
        step for step in (record.get("steps") or [])
        if isinstance(step, dict) and step.get("validation_result_ref")
    ]
    if not steps:
        return None
    selected = str(record.get("selected_step_id") or "")
    for step in steps:
        if str(step.get("step_id")) == selected:
            return Path(str(step["validation_result_ref"])).name
    return Path(str(steps[-1]["validation_result_ref"])).name


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


def _chain(parts: list[list[tuple]]) -> list[tuple]:
    """Date-sorted union of daily rows (date-first tuples); first row per date wins."""
    seen: set[str] = set()
    out: list[tuple] = []
    for part in parts:
        for row in sorted(part):
            if row[0] not in seen:
                seen.add(row[0])
                out.append(row)
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


def _exposure_entry(rows: list[tuple[str, float, float]]) -> dict[str, object]:
    return {
        "dates": [row[0] for row in rows],
        "long": [row[1] for row in rows],
        "short": [row[2] for row in rows],
    }


def _cycle_stats(series: list[tuple[str, float]], bench: dict[str, float]) -> dict[str, object] | None:
    """Full-cycle statistics over one chained daily-return series.

    Computed server-side like the curves. Return/vol/Sharpe/drawdown/win-rate
    use every strategy day; the benchmark-relative block (β, excess, tracking
    error, information ratio) uses date-matched days only, and both legs of the
    excess are compounded over that same matched set so they stay comparable.
    Alpha is deliberately not annualized (same rationale as the compact block).
    """
    if not series:
        return None
    values = [value for _, value in series]
    n = len(values)
    equity = 1.0
    peak = 1.0
    max_drawdown = 0.0
    for value in values:
        equity *= 1.0 + value
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, 1.0 - equity / peak)
    cum = equity - 1.0
    mean = sum(values) / n
    variance = sum((value - mean) ** 2 for value in values) / (n - 1) if n > 1 else 0.0
    vol = math.sqrt(variance)
    stats: dict[str, object] = {
        "n_days": n,
        "cum_return": round(cum, 6),
        "annualized_return": round((1.0 + cum) ** (TRADING_DAYS_PER_YEAR / n) - 1.0, 6) if cum > -1.0 else -1.0,
        "annualized_vol": round(vol * math.sqrt(TRADING_DAYS_PER_YEAR), 6),
        "sharpe": round(mean / vol * math.sqrt(TRADING_DAYS_PER_YEAR), 4) if vol > 0 else 0.0,
        "max_drawdown": round(max_drawdown, 6),
        "daily_win_rate": round(sum(1 for value in values if value > 0) / n, 4),
    }
    paired = [(value, bench[date]) for date, value in series if date in bench]
    if len(paired) >= 2:
        strategy_leg = [a for a, _ in paired]
        bench_leg = [b for _, b in paired]
        strategy_cum = math.prod(1.0 + value for value in strategy_leg) - 1.0
        bench_cum = math.prod(1.0 + value for value in bench_leg) - 1.0
        bench_mean = sum(bench_leg) / len(bench_leg)
        strategy_mean = sum(strategy_leg) / len(strategy_leg)
        bench_var = sum((b - bench_mean) ** 2 for b in bench_leg)
        active = [a - b for a, b in paired]
        active_mean = sum(active) / len(active)
        active_var = sum((x - active_mean) ** 2 for x in active) / (len(active) - 1)
        stats.update(
            {
                "benchmark_days": len(paired),
                "benchmark_return": round(bench_cum, 6),
                "excess_return": round(strategy_cum - bench_cum, 6),
                "beta": (
                    round(sum((a - strategy_mean) * (b - bench_mean) for a, b in paired) / bench_var, 4)
                    if bench_var > 0
                    else None
                ),
                "tracking_error": round(math.sqrt(active_var * TRADING_DAYS_PER_YEAR), 6),
                "information_ratio": (
                    round(active_mean / math.sqrt(active_var) * math.sqrt(TRADING_DAYS_PER_YEAR), 4)
                    if active_var > 0
                    else None
                ),
            }
        )
    return stats


# ---------------------------------------------------------------------------
# payload assembly
# ---------------------------------------------------------------------------
def fold_equity_payload(
    experiments_root: Path, experiment_id: str, epoch_id: str, fold_id: str
) -> dict[str, object]:
    from .registry import read_ledger_records, latest_fold_records, resolve_experiment_dir, test_results_revealed

    experiment_dir = resolve_experiment_dir(experiments_root, experiment_id)
    record = latest_fold_records(read_ledger_records(experiment_dir)).get((epoch_id, fold_id))
    if record is None:
        raise KeyError(f"fold {epoch_id}/{fold_id} has no ledger record")
    run_id = str(record.get("run_id") or "")
    # P1-7: test curves stay hidden until the researcher reveals (seals) the
    # experiment; the UI's collapsed test section never renders without them.
    prefixes = ("valid", "test") if test_results_revealed(experiment_dir) else ("valid",)
    payload: dict[str, object] = {}
    for prefix in prefixes:
        window = fold_valid_window(record) if prefix == "valid" else None
        strategy = run_series(experiment_dir, run_id, prefix, window=window)
        exposure = run_exposure_series(experiment_dir, run_id, prefix, window=window)
        if prefix == "valid" and window is None:
            strategy = []  # no recorded validation window: never blend attempts
            exposure = []
        bench = _rollup_benchmark(experiment_dir, run_id, prefix)
        payload[prefix] = {
            "series": [_curve_entry(prefix, SERIES_LABELS[prefix], strategy)],
            "benchmark": _benchmark_entry(bench, [date for date, _ in strategy]),
            "exposure": {prefix: _exposure_entry(exposure)} if exposure else {},
        }
    return payload


def experiment_equity_payload(
    experiments_root: Path, experiment_id: str, epoch_id: str | None = None
) -> dict[str, object]:
    from .registry import (
        read_ledger_records,
        latest_fold_records,
        latest_heldout_records,
        resolve_experiment_dir,
        test_results_revealed,
    )

    experiment_dir = resolve_experiment_dir(experiments_root, experiment_id)
    records = read_ledger_records(experiment_dir)
    all_folds = list(latest_fold_records(records).values())
    all_folds.sort(key=lambda r: (str(r.get("epoch_id")), str(r.get("test_period") or r.get("fold_id"))))
    # Epochs are alternative passes over the SAME fold calendar: chaining folds
    # across epochs would compound each quarter once per epoch and blend curves
    # no strategy produced. One epoch per payload; the SPA switches.
    epochs = sorted({str(r.get("epoch_id")) for r in all_folds})
    selected = str(epoch_id) if epoch_id and str(epoch_id) in epochs else (epochs[-1] if epochs else None)
    folds = [r for r in all_folds if str(r.get("epoch_id")) == selected]
    revealed = test_results_revealed(experiment_dir)
    heldout = latest_heldout_records(records) if revealed else []
    heldout_runs = sorted({str(r.get("run_id") or "") for r in heldout if r.get("run_id")})

    chains = {
        "valid": _chain([
            run_series(experiment_dir, str(r.get("run_id") or ""), "valid", window=window)
            for r in folds
            if (window := fold_valid_window(r)) is not None
        ]),
        "test": _chain([run_series(experiment_dir, str(r.get("run_id") or ""), "test") for r in folds]) if revealed else [],
        "heldout": _chain([run_series(experiment_dir, run_id, "heldout") for run_id in heldout_runs]),
    }
    series = [
        _curve_entry(key, SERIES_LABELS[key], chain) for key, chain in chains.items() if chain
    ]

    bench: dict[str, float] = {}
    for record in folds:
        run_id = str(record.get("run_id") or "")
        for prefix in ("valid", "test") if revealed else ("valid",):
            bench.update(_rollup_benchmark(experiment_dir, run_id, prefix))
    for run_id in heldout_runs:
        bench.update(_rollup_benchmark(experiment_dir, run_id, "heldout"))
    all_dates = [date for chain in chains.values() for date, _ in chain]
    exposure_chains = {
        "valid": _chain([
            run_exposure_series(experiment_dir, str(r.get("run_id") or ""), "valid", window=window)
            for r in folds
            if (window := fold_valid_window(r)) is not None
        ]),
        "test": _chain([
            run_exposure_series(experiment_dir, str(r.get("run_id") or ""), "test") for r in folds
        ]) if revealed else [],
        "heldout": _chain([run_exposure_series(experiment_dir, run_id, "heldout") for run_id in heldout_runs]),
    }
    return {
        "series": series,
        "benchmark": _benchmark_entry(bench, all_dates),
        "epochs": epochs,
        "epoch_id": selected,
        # Full-cycle statistics per chained series (Barra-lite regression core
        # plus risk/consistency metrics); fold-level tilts stay on the fold view.
        "stats": {key: _cycle_stats(chain, bench) for key, chain in chains.items() if chain},
        # Daily position weight (EOD gross market value / equity) per series,
        # rendered as a linked pane under the return curves.
        "exposure": {key: _exposure_entry(rows) for key, rows in exposure_chains.items() if rows},
    }
