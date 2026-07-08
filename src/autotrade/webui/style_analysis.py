"""Barra-lite style validation for replay result windows.

A full Barra model (licensed factor returns + covariances) is neither locally
available nor statistically supportable on 20-trading-day fold windows. What
this module provides instead answers the question the researcher actually has
— "is this return genuine selection alpha, or a hidden beta/size/industry
bet?" — with two robust, data-supported pieces:

1. Benchmark regression: daily strategy returns vs CSI 300 → beta, annualized
   alpha, R² (single-factor OLS is meaningful even on short windows; flagged
   as indicative when n is small).
2. Holdings-based style exposure: daily net positions reconstructed from the
   window's filled orders, valued and percentile-ranked against the FULL
   cross-section from daily_basic (size = circ_mv, value = PB, liquidity =
   turnover_rate), plus signed Shenwan L1 industry weights. Tilts are signed
   position-weighted percentile deviations from the market median, in [-1, 1].

Results are persisted as JSON sidecars under
``experiments/<id>/hitl/analysis/style/`` (the durable output dataset) and
served to the console for visualization. Recomputation happens only when the
sidecar is missing.
"""

from __future__ import annotations

import json
import math
from functools import lru_cache
from pathlib import Path

from autotrade.environment.runtime import utc_now_iso

from .equity import benchmark_returns

TRADING_DAYS_PER_YEAR = 244
# Signed direction of each broker action for position reconstruction.
_ACTION_SIGN = {
    "buy": 1, "cover": 1, "fin_buy": 1, "credit_buy": 1,
    "sell": -1, "short": -1, "credit_sell": -1, "sell_repay": -1,
}
_MIN_REGRESSION_DAYS = 8


# ---------------------------------------------------------------------------
# reference data (cached per process)
# ---------------------------------------------------------------------------
@lru_cache(maxsize=1)
def _industry_table(repo_root: str) -> tuple[tuple[str, str, str, str], ...]:
    """(ts_code, l1_name, in_date, out_date) rows from the SW classification."""
    import pandas as pd

    root = Path(repo_root) / "data" / "raw" / "index_member_all"
    rows: list[tuple[str, str, str, str]] = []
    if not root.is_dir():
        return ()
    for path in sorted(root.glob("l1_code=*.parquet")):
        df = pd.read_parquet(path, columns=["ts_code", "l1_name", "in_date", "out_date"])
        for code, name, in_date, out_date in zip(df["ts_code"], df["l1_name"], df["in_date"], df["out_date"]):
            rows.append((str(code), str(name), str(in_date or ""), str(out_date or "") if out_date else ""))
    return tuple(rows)


def _industry_of(repo_root: str, ts_code: str, date: str) -> str | None:
    for code, name, in_date, out_date in _industry_table(repo_root):
        if code == ts_code and in_date <= date and (not out_date or out_date > date):
            return name
    return None


@lru_cache(maxsize=128)
def _daily_basic(repo_root: str, date: str) -> dict[str, tuple[float, float, float, float]]:
    """ts_code -> (close, circ_mv percentile, pb percentile, turnover percentile)."""
    import pandas as pd

    path = Path(repo_root) / "data" / "raw" / "daily_basic" / f"trade_date={date}.parquet"
    if not path.exists():
        return {}
    df = pd.read_parquet(path, columns=["ts_code", "close", "circ_mv", "pb", "turnover_rate"])
    out: dict[str, tuple[float, float, float, float]] = {}
    pct = {
        column: df[column].rank(pct=True)
        for column in ("circ_mv", "pb", "turnover_rate")
    }
    for i, code in enumerate(df["ts_code"]):
        close = df["close"].iloc[i]
        if not (isinstance(close, (int, float)) and math.isfinite(float(close))):
            continue
        out[str(code)] = (
            float(close),
            float(pct["circ_mv"].iloc[i]) if math.isfinite(float(pct["circ_mv"].iloc[i] or float("nan"))) else 0.5,
            float(pct["pb"].iloc[i]) if math.isfinite(float(pct["pb"].iloc[i] or float("nan"))) else 0.5,
            float(pct["turnover_rate"].iloc[i]) if math.isfinite(float(pct["turnover_rate"].iloc[i] or float("nan"))) else 0.5,
        )
    return out


# ---------------------------------------------------------------------------
# window pieces
# ---------------------------------------------------------------------------
def _window_dirs(experiment_dir: Path, run_id: str, prefix: str) -> list[Path]:
    results_root = experiment_dir / "artifacts" / run_id / "results"
    if not results_root.is_dir():
        return []
    return sorted(p for p in results_root.iterdir() if p.is_dir() and p.name.startswith(prefix))


def _strategy_daily_returns(window_dirs: list[Path]) -> list[tuple[str, float]]:
    merged: list[tuple[str, float]] = []
    for window_dir in window_dirs:
        path = window_dir / "detailed_return.json"
        if not path.exists():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        curve = payload.get("equity_curve") or {}
        previous = float(payload.get("initial_cash") or 0.0)
        for date in sorted(curve):
            value = float(curve[date])
            if previous > 0:
                merged.append((str(date), value / previous - 1.0))
            previous = value
    seen: set[str] = set()
    out = []
    for date, value in sorted(merged):
        if date not in seen:
            seen.add(date)
            out.append((date, value))
    return out


def _daily_positions(window_dirs: list[Path]) -> dict[str, dict[str, float]]:
    """date -> {ts_code: signed shares held at EOD}.

    Positions are reconstructed from filled orders and CARRIED FORWARD across
    every replayed trading day of the window (the equity_curve dates), not
    just fill days — day-end holdings between trades still contribute style
    exposure. Each window replays independently; overlapping dates merge.
    """
    import pandas as pd

    positions_by_date: dict[str, dict[str, float]] = {}
    for window_dir in window_dirs:
        orders_path = window_dir / "orders.parquet"
        detailed_path = window_dir / "detailed_return.json"
        if not orders_path.exists() or not detailed_path.exists():
            continue
        curve = json.loads(detailed_path.read_text(encoding="utf-8")).get("equity_curve") or {}
        window_dates = sorted(str(date) for date in curve)
        if not window_dates:
            continue
        df = pd.read_parquet(orders_path)
        filled = df[(df["status"] == "filled") & (df["filled_quantity"] > 0)]
        fills_by_date: dict[str, list] = {}
        if not filled.empty:
            for _, row in filled.sort_values(["trade_date", "decision_time"]).iterrows():
                fills_by_date.setdefault(str(row["trade_date"]), []).append(row)
        holdings: dict[str, float] = {}
        for date in window_dates:
            for row in fills_by_date.get(date, []):
                sign = _ACTION_SIGN.get(str(row["action"]))
                if sign is None:
                    continue
                code = str(row["ts_code"])
                holdings[code] = holdings.get(code, 0.0) + sign * float(row["filled_quantity"])
                if abs(holdings[code]) < 1e-9:
                    holdings.pop(code, None)
            if holdings:
                merged = positions_by_date.setdefault(date, {})
                for code, quantity in holdings.items():
                    merged[code] = merged.get(code, 0.0) + quantity
    return positions_by_date


def _benchmark_regression(repo_root: Path, strategy: list[tuple[str, float]]) -> dict[str, object]:
    bench = dict(map(tuple, benchmark_returns(repo_root, [d for d, _ in strategy])))
    paired = [(rs, bench[d]) for d, rs in strategy if d in bench]
    n = len(paired)
    if n < _MIN_REGRESSION_DAYS:
        return {"n_days": n, "beta": None, "alpha_annualized": None, "r2": None}
    mean_s = sum(rs for rs, _ in paired) / n
    mean_b = sum(rb for _, rb in paired) / n
    cov = sum((rs - mean_s) * (rb - mean_b) for rs, rb in paired) / n
    var_b = sum((rb - mean_b) ** 2 for _, rb in paired) / n
    var_s = sum((rs - mean_s) ** 2 for rs, _ in paired) / n
    beta = cov / var_b if var_b > 0 else 0.0
    alpha_daily = mean_s - beta * mean_b
    r2 = (cov * cov) / (var_b * var_s) if var_b > 0 and var_s > 0 else 0.0
    return {
        "n_days": n,
        "beta": round(beta, 3),
        "alpha_annualized": round(alpha_daily * TRADING_DAYS_PER_YEAR, 4),
        "r2": round(r2, 3),
    }


def _style_exposures(repo_root: Path, positions_by_date: dict[str, dict[str, float]]) -> dict[str, object]:
    repo = str(repo_root)
    tilt_sums = {"size": 0.0, "pb": 0.0, "turnover": 0.0}
    industry_sums: dict[str, float] = {}
    days_used = 0
    names = 0
    long_gross_total = 0.0
    short_gross_total = 0.0
    for date, holdings in sorted(positions_by_date.items()):
        basics = _daily_basic(repo, date)
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
            industry = _industry_of(repo, code, date) or "未分类"
            industry_sums[industry] = industry_sums.get(industry, 0.0) + weight
    if not days_used:
        return {"days": 0, "tilts": None, "industries": [], "avg_names": 0,
                "avg_long_gross": None, "avg_short_gross": None}
    industries = sorted(
        ({"name": name, "weight": round(total / days_used, 3)} for name, total in industry_sums.items()),
        key=lambda item: -abs(float(item["weight"])),
    )[:8]
    return {
        "days": days_used,
        "tilts": {key: round(total / days_used, 3) for key, total in tilt_sums.items()},
        "industries": industries,
        "avg_names": round(names / days_used, 1),
        "avg_long_gross": round(long_gross_total / days_used, 0),
        "avg_short_gross": round(short_gross_total / days_used, 0),
    }


# ---------------------------------------------------------------------------
# entry point with sidecar persistence
# ---------------------------------------------------------------------------
def style_analysis(experiment_dir: Path, run_id: str, prefix: str, repo_root: Path) -> dict[str, object]:
    """Compute (or load the persisted sidecar of) one window-chain's analysis."""
    sidecar_dir = experiment_dir / "hitl" / "analysis" / "style"
    sidecar = sidecar_dir / f"{run_id}_{prefix}.json"
    if sidecar.exists():
        return json.loads(sidecar.read_text(encoding="utf-8"))
    window_dirs = _window_dirs(experiment_dir, run_id, prefix)
    if not window_dirs:
        raise KeyError(f"run {run_id} has no result windows with prefix {prefix!r}")
    strategy = _strategy_daily_returns(window_dirs)
    payload = {
        "schema_version": 1,
        "run_id": run_id,
        "window_prefix": prefix,
        "benchmark_regression": _benchmark_regression(repo_root, strategy),
        "style": _style_exposures(repo_root, _daily_positions(window_dirs)),
        "created_at": utc_now_iso(),
    }
    sidecar_dir.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return payload
