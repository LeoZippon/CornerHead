"""Barra-lite style / benchmark attribution — single computation point.

All attribution math runs HOST-SIDE at replay completion, and every input is
frozen run data — never the mutable raw lake (whose history gets revised, see
the revision ledger; recomputing later from raw could disagree with what the
Agent actually saw):

- strategy daily returns: the window's own ``equity_curve``;
- cross-sectional style ranks: the replay slot's ``daily.parquet``;
- CSI 300 benchmark: ``index_daily`` rows inside the replay slot's
  ``macro.parquet``;
- SW L1 industry: the decision snapshot's ``universe.parquet`` (as-of the
  decision day — membership drift within a replay window is negligible).

The backtest tool writes one ``style_analysis.json`` per result window (valid,
test and held-out alike; test/held-out replays run after the Agent session).
The pipeline then writes one ``style_<prefix>.json`` rollup per window chain
under ``results/``, which is what the console serves — the web layer performs
no attribution computation and touches no raw data.

Everything degrades to None/empty blocks when inputs are missing —
attribution is advisory and must never fail a backtest.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Mapping

from autotrade.environment.runtime import utc_now_iso

BENCHMARK_TS_CODE = "000300.SH"
BENCHMARK_LABEL = "沪深300"
TRADING_DAYS_PER_YEAR = 244
_MIN_REGRESSION_DAYS = 8
# Signed direction of each broker action for position reconstruction.
_ACTION_SIGN = {
    "buy": 1, "cover": 1, "fin_buy": 1, "credit_buy": 1,
    "sell": -1, "short": -1, "credit_sell": -1, "sell_repay": -1,
}
_STYLE_COLUMNS = ("circ_mv", "pb", "turnover_rate")


# ---------------------------------------------------------------------------
# frozen-input adapters
# ---------------------------------------------------------------------------
def _slot_benchmark(replay_dir: Path) -> dict[str, float]:
    """CSI 300 daily returns from the replay slot's macro rows (frozen)."""
    path = Path(replay_dir) / "macro.parquet"
    if not path.exists():
        return {}
    import pandas as pd

    df = pd.read_parquet(path)
    if "dataset" not in df.columns or "ts_code" not in df.columns:
        return {}
    rows = df[(df["dataset"] == "index_daily") & (df["ts_code"] == BENCHMARK_TS_CODE)]
    out: dict[str, float] = {}
    for date, pct in zip(rows.get("trade_date", ()), rows.get("pct_chg", ())):
        try:
            value = float(pct)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value):
            out[str(date)] = value / 100.0
    return out


def _snapshot_industry(snapshot_dir: Path) -> dict[str, str]:
    """ts_code -> SW L1 name as-of the decision day (frozen universe)."""
    path = Path(snapshot_dir) / "universe.parquet"
    if not path.exists():
        return {}
    import pandas as pd

    df = pd.read_parquet(path)
    if "l1_name" not in df.columns:
        return {}
    return {
        str(code): str(name)
        for code, name in zip(df["ts_code"], df["l1_name"])
        if isinstance(name, str) and name
    }


# ---------------------------------------------------------------------------
# core math (input-source agnostic)
# ---------------------------------------------------------------------------
def daily_returns_from_curve(curve: Mapping[str, object], initial_cash: float) -> list[tuple[str, float]]:
    previous = float(initial_cash or 0.0)
    out: list[tuple[str, float]] = []
    for date in sorted(curve):
        value = float(curve[date])  # type: ignore[arg-type]
        if previous > 0:
            out.append((str(date), value / previous - 1.0))
        previous = value
    return out


def _benchmark_regression(strategy: list[tuple[str, float]], bench: Mapping[str, float]) -> dict[str, object]:
    paired = [(rs, bench[d]) for d, rs in strategy if d in bench]
    n = len(paired)
    benchmark_total = 1.0
    for date, _ in strategy:
        if date in bench:
            benchmark_total *= 1.0 + bench[date]
    result: dict[str, object] = {
        "n_days": n,
        "benchmark_return": round(benchmark_total - 1.0, 6) if n else None,
        "beta": None,
        "alpha_annualized": None,
        "r2": None,
    }
    if n < _MIN_REGRESSION_DAYS:
        return result
    mean_s = sum(rs for rs, _ in paired) / n
    mean_b = sum(rb for _, rb in paired) / n
    cov = sum((rs - mean_s) * (rb - mean_b) for rs, rb in paired) / n
    var_b = sum((rb - mean_b) ** 2 for _, rb in paired) / n
    var_s = sum((rs - mean_s) ** 2 for rs, _ in paired) / n
    beta = cov / var_b if var_b > 0 else 0.0
    alpha_daily = mean_s - beta * mean_b
    result.update(
        beta=round(beta, 3),
        alpha_annualized=round(alpha_daily * TRADING_DAYS_PER_YEAR, 4),
        r2=round((cov * cov) / (var_b * var_s), 3) if var_b > 0 and var_s > 0 else 0.0,
    )
    return result


def _positions_from_fills(
    fills_by_date: Mapping[str, list[Mapping[str, object]]], window_dates: list[str]
) -> dict[str, dict[str, float]]:
    """date -> {ts_code: signed shares held at EOD}, carried forward across
    every replayed trading day (holdings between trades still count)."""
    holdings: dict[str, float] = {}
    positions_by_date: dict[str, dict[str, float]] = {}
    for date in window_dates:
        for row in fills_by_date.get(date, []):
            sign = _ACTION_SIGN.get(str(row.get("action")))
            if sign is None:
                continue
            code = str(row.get("ts_code"))
            holdings[code] = holdings.get(code, 0.0) + sign * float(row.get("filled_quantity") or 0.0)
            if abs(holdings[code]) < 1e-9:
                holdings.pop(code, None)
        if holdings:
            positions_by_date[date] = dict(holdings)
    return positions_by_date


def _group_fills(order_records: list[Mapping[str, object]]) -> dict[str, list[Mapping[str, object]]]:
    filled = [
        row for row in order_records
        if str(row.get("status")) == "filled" and float(row.get("filled_quantity") or 0.0) > 0
    ]
    filled.sort(key=lambda row: (str(row.get("trade_date")), str(row.get("decision_time"))))
    by_date: dict[str, list[Mapping[str, object]]] = {}
    for row in filled:
        by_date.setdefault(str(row.get("trade_date")), []).append(row)
    return by_date


_EMPTY_STYLE: dict[str, object] = {
    "days": 0, "tilts": None, "industries": [], "avg_names": 0,
    "avg_long_gross": None, "avg_short_gross": None,
}


def _style_block(
    days: int,
    tilt_sums: Mapping[str, float],
    industry_sums: Mapping[str, float],
    names: float,
    long_gross: float,
    short_gross: float,
) -> dict[str, object]:
    """Assemble the style payload from day-summed accumulators (shared by the
    per-window computation and the days-weighted rollup merge)."""
    if not days:
        return dict(_EMPTY_STYLE)
    return {
        "days": days,
        "tilts": {key: round(total / days, 3) for key, total in tilt_sums.items()},
        "industries": sorted(
            ({"name": name, "weight": round(total / days, 3)} for name, total in industry_sums.items()),
            key=lambda item: -abs(float(item["weight"])),
        )[:8],
        "avg_names": round(names / days, 1),
        "avg_long_gross": round(long_gross / days, 0),
        "avg_short_gross": round(short_gross / days, 0),
    }


def _style_exposures(
    positions_by_date: dict[str, dict[str, float]],
    basics_by_date: Mapping[str, dict[str, tuple[float, float, float, float]]],
    industry_by_code: Mapping[str, str],
) -> tuple[dict[str, object], dict[str, object]]:
    tilt_sums = {"size": 0.0, "pb": 0.0, "turnover": 0.0}
    industry_sums: dict[str, float] = {}
    days_used = 0
    names = 0
    long_gross_total = 0.0
    short_gross_total = 0.0
    for date, holdings in sorted(positions_by_date.items()):
        basics = basics_by_date.get(date, {})
        valued = []
        for code, quantity in holdings.items():
            info = basics.get(code)
            if info is None:
                continue
            close, size_pct, pb_pct, turn_pct = info
            valued.append((code, quantity * close, size_pct, pb_pct, turn_pct))
        gross = sum(abs(v) for _, v, *_ in valued)
        if gross <= 0:
            continue
        days_used += 1
        names += len(valued)
        long_gross_total += sum(v for _, v, *_ in valued if v > 0)
        short_gross_total += sum(-v for _, v, *_ in valued if v < 0)
        for code, value, size_pct, pb_pct, turn_pct in valued:
            weight = value / gross  # signed
            tilt_sums["size"] += weight * (size_pct - 0.5) * 2
            tilt_sums["pb"] += weight * (pb_pct - 0.5) * 2
            tilt_sums["turnover"] += weight * (turn_pct - 0.5) * 2
            industry = industry_by_code.get(code) or "未分类"
            industry_sums[industry] = industry_sums.get(industry, 0.0) + weight
    return (
        _style_block(days_used, tilt_sums, industry_sums, names, long_gross_total, short_gross_total),
        {
            "days": days_used,
            "tilt_sums": tilt_sums,
            "industry_sums": industry_sums,
            "names": names,
            "long_gross": long_gross_total,
            "short_gross": short_gross_total,
        },
    )


def _rank_cross_section(df) -> dict[str, tuple[float, float, float, float]]:
    """One trade date's cross-section -> ts_code: (close, size/pb/turnover pct)."""
    out: dict[str, tuple[float, float, float, float]] = {}
    pct = {column: df[column].rank(pct=True) for column in _STYLE_COLUMNS}

    def _pct(series, i) -> float:
        try:
            value = float(series.iloc[i])
        except (TypeError, ValueError):
            return 0.5
        return value if math.isfinite(value) else 0.5

    closes = df["close"]
    codes = df["ts_code"]
    for i in range(len(df)):
        try:
            close = float(closes.iloc[i])
        except (TypeError, ValueError):
            continue
        if not math.isfinite(close):
            continue
        out[str(codes.iloc[i])] = (
            close,
            _pct(pct["circ_mv"], i),
            _pct(pct["pb"], i),
            _pct(pct["turnover_rate"], i),
        )
    return out


def _compact(regression: Mapping[str, object], style: Mapping[str, object], total_return: object) -> dict[str, object]:
    """The few fields worth putting in front of the Agent per Step. Alpha/R²
    deliberately excluded: annualizing a ~20-day alpha amplifies noise."""
    benchmark_return = regression.get("benchmark_return")
    excess = None
    if isinstance(total_return, (int, float)) and isinstance(benchmark_return, (int, float)):
        excess = round(float(total_return) - float(benchmark_return), 6)
    tilts = style.get("tilts") if isinstance(style, Mapping) else None
    return {
        "label": BENCHMARK_LABEL,
        "benchmark_return": benchmark_return,
        "excess_return": excess,
        "beta": regression.get("beta"),
        "n_days": regression.get("n_days"),
        "size_tilt": tilts.get("size") if isinstance(tilts, Mapping) else None,
    }


# ---------------------------------------------------------------------------
# per-window computation (backtest tool, every replay mode)
# ---------------------------------------------------------------------------
def replay_style_analysis(
    replay_daily,
    order_records: list[Mapping[str, object]],
    stats: Mapping[str, object],
    *,
    replay_dir: Path,
    snapshot_dir: Path,
) -> dict[str, object]:
    """Attribution for one just-finished replay, from frozen run inputs only."""
    curve = stats.get("equity_curve")
    strategy = (
        daily_returns_from_curve(curve, float(stats.get("initial_cash") or 0.0))
        if isinstance(curve, Mapping) else []
    )
    bench = _slot_benchmark(replay_dir)
    regression = _benchmark_regression(strategy, bench)

    style: dict[str, object] = dict(_EMPTY_STYLE)
    style_rollup: dict[str, object] | None = None
    if all(column in replay_daily.columns for column in ("ts_code", "trade_date", "close", *_STYLE_COLUMNS)):
        window_dates = sorted(str(d) for d in (curve or {}))
        by_date = {
            str(date): _rank_cross_section(group)
            for date, group in replay_daily.groupby("trade_date")
            if str(date) in set(window_dates)
        }
        positions = _positions_from_fills(_group_fills(order_records), window_dates)
        style, style_rollup = _style_exposures(positions, by_date, _snapshot_industry(snapshot_dir))

    payload = {
        "benchmark": BENCHMARK_TS_CODE,
        "benchmark_regression": regression,
        "style": style,
        # Persisted series make every downstream consumer (rollups, console
        # charts) independent of both the raw lake and this module's inputs.
        "strategy_daily": [[date, value] for date, value in strategy],
        "benchmark_daily": [[date, bench[date]] for date, _ in strategy if date in bench],
        "created_at": utc_now_iso(),
    }
    if style_rollup is not None:
        payload["style_rollup"] = style_rollup
    payload["compact"] = _compact(regression, style, stats.get("total_return"))
    return payload


# ---------------------------------------------------------------------------
# prefix rollups (pipeline, after a window chain completes)
# ---------------------------------------------------------------------------
def write_style_rollup(results_root: Path, prefix: str) -> dict[str, object] | None:
    """Aggregate ``<prefix>_*`` window sidecars into ``style_<prefix>.json``.

    Pure math over the persisted per-window payloads: regression re-runs on
    the chained daily series; exposures merge days-weighted. Returns None
    (and writes nothing) when no window carries a sidecar.
    """
    results_root = Path(results_root)
    payloads: list[dict[str, object]] = []
    windows: list[str] = []
    for window_dir in sorted(results_root.glob(f"{prefix}_*")):
        sidecar = window_dir / "style_analysis.json"
        if window_dir.is_dir() and sidecar.exists():
            payloads.append(json.loads(sidecar.read_text(encoding="utf-8")))
            windows.append(window_dir.name)
    if not payloads:
        return None

    strategy_map: dict[str, float] = {}
    bench_map: dict[str, float] = {}
    for payload in payloads:
        for date, value in payload.get("strategy_daily", []):
            strategy_map.setdefault(str(date), float(value))
        for date, value in payload.get("benchmark_daily", []):
            bench_map.setdefault(str(date), float(value))
    strategy = sorted(strategy_map.items())
    regression = _benchmark_regression(strategy, bench_map)

    # Days-weighted merge back into day-summed accumulators, then the same
    # shared block builder as the per-window computation.
    tilt_sums = {"size": 0.0, "pb": 0.0, "turnover": 0.0}
    industry_sums: dict[str, float] = {}
    total_days = 0
    names = 0.0
    long_gross = 0.0
    short_gross = 0.0
    for p in payloads:
        window_style = p.get("style") or {}
        rollup_source = p.get("style_rollup")
        days = int((rollup_source if isinstance(rollup_source, Mapping) else window_style).get("days") or 0)
        if not days:
            continue
        total_days += days
        if isinstance(rollup_source, Mapping):
            for key, value in (rollup_source.get("tilt_sums") or {}).items():
                if key in tilt_sums:
                    tilt_sums[key] += float(value or 0.0)
            for name, value in (rollup_source.get("industry_sums") or {}).items():
                industry_sums[str(name)] = industry_sums.get(str(name), 0.0) + float(value or 0.0)
            names += float(rollup_source.get("names") or 0.0)
            long_gross += float(rollup_source.get("long_gross") or 0.0)
            short_gross += float(rollup_source.get("short_gross") or 0.0)
        else:
            tilts = window_style.get("tilts") or {}
            for key in tilt_sums:
                tilt_sums[key] += float(tilts.get(key) or 0.0) * days
            for item in window_style.get("industries") or []:
                industry_sums[str(item["name"])] = (
                    industry_sums.get(str(item["name"]), 0.0) + float(item["weight"]) * days
                )
            names += float(window_style.get("avg_names") or 0.0) * days
            long_gross += float(window_style.get("avg_long_gross") or 0.0) * days
            short_gross += float(window_style.get("avg_short_gross") or 0.0) * days
    style = _style_block(total_days, tilt_sums, industry_sums, names, long_gross, short_gross)

    strategy_total = 1.0
    for _, value in strategy:
        strategy_total *= 1.0 + value
    rollup = {
        "prefix": prefix,
        "windows": windows,
        "benchmark": BENCHMARK_TS_CODE,
        "benchmark_regression": regression,
        "style": style,
        "strategy_daily": [[date, value] for date, value in strategy],
        # Per-window benchmark_daily is already restricted to strategy dates.
        "benchmark_daily": sorted([date, value] for date, value in bench_map.items()),
        "created_at": utc_now_iso(),
    }
    rollup["compact"] = _compact(regression, style, strategy_total - 1.0)
    (results_root / f"style_{prefix}.json").write_text(
        json.dumps(rollup, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    return rollup
