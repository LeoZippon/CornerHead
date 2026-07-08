"""Barra-lite style / benchmark attribution for replay windows.

Two consumers share one implementation:

- **In-loop (per Step)**: the backtest tool calls ``replay_style_analysis``
  right after a validation replay, computing from data the Agent already
  sees — the replay slot's ``daily.parquet`` (full cross-section incl.
  circ_mv / pb / turnover_rate) and the run's own fills — plus the CSI 300
  series read host-side from the raw lake (window dates only). The compact
  block rides in the tool result so the Agent gets benchmark context with
  zero extra actions; the full payload lands as ``style_analysis.json``
  next to ``detailed_return.json``.
- **Post-hoc (console)**: ``style_analysis`` recomputes the same analysis
  from a recorded run's result windows (valid / test / heldout) and
  persists a sidecar under ``experiments/<id>/hitl/analysis/style/``.

Scope is deliberate: a full Barra model (licensed factor returns +
covariances) is neither locally available nor supportable on ~20-trading-day
windows. What IS robust at this horizon: a single-factor CSI 300 regression
(beta; alpha flagged as indicative on short windows) and holdings-based
descriptive exposures (signed position-weighted percentile deviation from
the market median for size / PB / turnover, plus SW L1 industry weights).
These are diagnostics for interpreting returns — never optimization targets.

All entries degrade gracefully (None blocks) when inputs are missing —
attribution is advisory and must never fail a backtest.
"""

from __future__ import annotations

import json
import math
from functools import lru_cache
from pathlib import Path
from typing import Callable, Mapping

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

BasicsFor = Callable[[str], dict[str, tuple[float, float, float, float]]]
IndustryOf = Callable[[str, str], str | None]


# ---------------------------------------------------------------------------
# reference data (cached per process)
# ---------------------------------------------------------------------------
@lru_cache(maxsize=32)
def _benchmark_year(raw_dir: str, year: str) -> tuple[tuple[str, float], ...]:
    path = Path(raw_dir) / "index_daily" / f"ts_code={BENCHMARK_TS_CODE}" / f"year={year}.parquet"
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


def benchmark_returns(raw_dir: Path | str | None, dates: list[str]) -> list[list[object]]:
    """CSI 300 daily returns restricted to ``dates`` (as [date, r] pairs)."""
    if not raw_dir or not dates:
        return []
    table: dict[str, float] = {}
    for year in sorted({date[:4] for date in dates}):
        table.update(dict(_benchmark_year(str(raw_dir), year)))
    return [[date, table[date]] for date in dates if date in table]


@lru_cache(maxsize=4)
def _industry_table(raw_dir: str) -> tuple[tuple[str, str, str, str], ...]:
    """(ts_code, l1_name, in_date, out_date) rows from the SW classification."""
    import pandas as pd

    root = Path(raw_dir) / "index_member_all"
    rows: list[tuple[str, str, str, str]] = []
    if not root.is_dir():
        return ()
    for path in sorted(root.glob("l1_code=*.parquet")):
        df = pd.read_parquet(path, columns=["ts_code", "l1_name", "in_date", "out_date"])
        for code, name, in_date, out_date in zip(df["ts_code"], df["l1_name"], df["in_date"], df["out_date"]):
            rows.append((str(code), str(name), str(in_date or ""), str(out_date or "") if out_date else ""))
    return tuple(rows)


def _industry_lookup(raw_dir: str | None) -> IndustryOf:
    def lookup(ts_code: str, date: str) -> str | None:
        if not raw_dir:
            return None
        for code, name, in_date, out_date in _industry_table(raw_dir):
            if code == ts_code and in_date <= date and (not out_date or out_date > date):
                return name
        return None

    return lookup


# ---------------------------------------------------------------------------
# core math (input-source agnostic)
# ---------------------------------------------------------------------------
def _daily_returns_from_curve(curve: Mapping[str, object], initial_cash: float) -> list[tuple[str, float]]:
    previous = float(initial_cash or 0.0)
    out: list[tuple[str, float]] = []
    for date in sorted(curve):
        value = float(curve[date])  # type: ignore[arg-type]
        if previous > 0:
            out.append((str(date), value / previous - 1.0))
        previous = value
    return out


def _benchmark_regression(strategy: list[tuple[str, float]], bench: dict[str, float]) -> dict[str, object]:
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


def _style_exposures(
    positions_by_date: dict[str, dict[str, float]],
    basics_for: BasicsFor,
    industry_of: IndustryOf,
) -> dict[str, object]:
    tilt_sums = {"size": 0.0, "pb": 0.0, "turnover": 0.0}
    industry_sums: dict[str, float] = {}
    days_used = 0
    names = 0
    long_gross_total = 0.0
    short_gross_total = 0.0
    for date, holdings in sorted(positions_by_date.items()):
        basics = basics_for(date)
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
            industry = industry_of(code, date) or "未分类"
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
# in-loop entry (backtest tool, mode="valid")
# ---------------------------------------------------------------------------
def replay_style_analysis(
    replay_daily,
    order_records: list[Mapping[str, object]],
    stats: Mapping[str, object],
    raw_dir: str | None,
) -> dict[str, object]:
    """Attribution for one just-finished validation replay.

    Style exposures come from the replay slot's own cross-section (the data
    the Agent already sees); only the CSI 300 series and the SW industry
    table are read host-side from ``raw_dir`` (window dates only). A missing
    raw_dir or missing style columns degrade to None blocks.
    """
    curve = stats.get("equity_curve")
    strategy = (
        _daily_returns_from_curve(curve, float(stats.get("initial_cash") or 0.0))
        if isinstance(curve, Mapping) else []
    )
    bench = dict(map(tuple, benchmark_returns(raw_dir, [d for d, _ in strategy])))
    regression = _benchmark_regression(strategy, bench)

    style: dict[str, object] = {"days": 0, "tilts": None, "industries": [], "avg_names": 0,
                                "avg_long_gross": None, "avg_short_gross": None}
    if all(column in replay_daily.columns for column in ("ts_code", "trade_date", "close", *_STYLE_COLUMNS)):
        window_dates = sorted(str(d) for d in (curve or {}))
        by_date = {
            str(date): _rank_cross_section(group)
            for date, group in replay_daily.groupby("trade_date")
            if str(date) in set(window_dates)
        }
        positions = _positions_from_fills(_group_fills(order_records), window_dates)
        style = _style_exposures(positions, lambda date: by_date.get(date, {}), _industry_lookup(raw_dir))

    payload = {
        "schema_version": 1,
        "benchmark": BENCHMARK_TS_CODE,
        "benchmark_regression": regression,
        "style": style,
        "created_at": utc_now_iso(),
    }
    payload["compact"] = _compact(regression, style, stats.get("total_return"))
    return payload


# ---------------------------------------------------------------------------
# post-hoc entry (console; recorded runs, any window prefix)
# ---------------------------------------------------------------------------
@lru_cache(maxsize=128)
def _raw_cross_section(raw_dir: str, date: str) -> dict[str, tuple[float, float, float, float]]:
    import pandas as pd

    path = Path(raw_dir) / "daily_basic" / f"trade_date={date}.parquet"
    if not path.exists():
        return {}
    return _rank_cross_section(pd.read_parquet(path, columns=["ts_code", "close", *_STYLE_COLUMNS]))


def _window_dirs(experiment_dir: Path, run_id: str, prefix: str) -> list[Path]:
    results_root = experiment_dir / "artifacts" / run_id / "results"
    if not results_root.is_dir():
        return []
    return sorted(p for p in results_root.iterdir() if p.is_dir() and p.name.startswith(prefix))


def style_analysis(experiment_dir: Path, run_id: str, prefix: str, raw_dir: Path | str) -> dict[str, object]:
    """Compute (or load the persisted sidecar of) one window-chain's analysis."""
    import pandas as pd

    sidecar_dir = experiment_dir / "hitl" / "analysis" / "style"
    sidecar = sidecar_dir / f"{run_id}_{prefix}.json"
    if sidecar.exists():
        return json.loads(sidecar.read_text(encoding="utf-8"))
    window_dirs = _window_dirs(experiment_dir, run_id, prefix)
    if not window_dirs:
        raise KeyError(f"run {run_id} has no result windows with prefix {prefix!r}")

    strategy: list[tuple[str, float]] = []
    positions_by_date: dict[str, dict[str, float]] = {}
    seen: set[str] = set()
    for window_dir in window_dirs:
        detailed = window_dir / "detailed_return.json"
        if not detailed.exists():
            continue
        detail = json.loads(detailed.read_text(encoding="utf-8"))
        curve = detail.get("equity_curve") or {}
        for date, value in _daily_returns_from_curve(curve, float(detail.get("initial_cash") or 0.0)):
            if date not in seen:
                seen.add(date)
                strategy.append((date, value))
        orders_path = window_dir / "orders.parquet"
        if orders_path.exists():
            records = pd.read_parquet(orders_path).to_dict("records")
            window_positions = _positions_from_fills(_group_fills(records), sorted(str(d) for d in curve))
            for date, holdings in window_positions.items():
                merged = positions_by_date.setdefault(date, {})
                for code, quantity in holdings.items():
                    merged[code] = merged.get(code, 0.0) + quantity
    strategy.sort()

    raw = str(raw_dir)
    bench = dict(map(tuple, benchmark_returns(raw, [d for d, _ in strategy])))
    payload = {
        "schema_version": 1,
        "run_id": run_id,
        "window_prefix": prefix,
        "benchmark": BENCHMARK_TS_CODE,
        "benchmark_regression": _benchmark_regression(strategy, bench),
        "style": _style_exposures(
            positions_by_date, lambda date: _raw_cross_section(raw, date), _industry_lookup(raw)
        ),
        "created_at": utc_now_iso(),
    }
    sidecar_dir.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return payload
