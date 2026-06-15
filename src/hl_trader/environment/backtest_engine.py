"""backtest_tool internals: candidates, score composition, orders, replay, stats.

The orchestration order lives in tools/backtest.py; this module holds the pure
mechanics so they stay individually testable (docs/environment_design.md 4.4
and chapter 5).
"""

from __future__ import annotations

import json
import math
import stat
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from hl_trader.environment.broker import BrokerProfile, MarketData, Order, SimBroker

CANDIDATE_COLUMNS = ("ts_code", "factor_score", "reason", "source_artifacts")
TRADING_DAYS_PER_YEAR = 252

_CANDIDATE_DRIVER = """\
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location("agent_factor_main", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
frame = module.generate_candidates()
import pandas as pd
if not isinstance(frame, pd.DataFrame):
    raise TypeError(f"generate_candidates must return a pandas.DataFrame, got {type(frame)!r}")
with open(sys.argv[2], "w", encoding="utf-8") as handle:
    json.dump({"columns": list(frame.columns), "rows": frame.to_dict("records")}, handle, ensure_ascii=False, default=str)
"""


class BacktestError(RuntimeError):
    """A formal backtest step failed; the error is explicit, never silent."""


def run_generate_candidates(executor, paths, *, timeout_seconds: float = 300.0) -> pd.DataFrame:
    """Call the no-argument strategy entrypoint through the sandbox executor.

    The formal execution environment pins MQ_SNAPSHOT_DIR to the current
    decision-input view (``/mnt/snapshot`` inside Docker); the strategy never
    receives parameters. The result lands in the writable workspace and is
    read back on the host.
    """
    out_host = paths.workspace / f".candidates_{uuid.uuid4().hex[:10]}.json"
    main_py = paths.agent_output / "factor" / "main.py"
    with hide_snapshot_slots_from_agent(paths):
        result = executor.run(
            [executor.python, "-c", _CANDIDATE_DRIVER, executor.map_path(main_py), executor.map_path(out_host)],
            env={"MQ_SNAPSHOT_DIR": executor.map_path(paths.snapshot)},
            cwd=paths.agent,
            timeout_seconds=timeout_seconds,
            user="agent",
        )
    try:
        if result.exit_code == 124:
            raise BacktestError(f"generate_candidates timed out after {timeout_seconds}s")
        if result.exit_code != 0:
            raise BacktestError(f"generate_candidates failed: {result.stderr.strip()[-2000:]}")
        payload = json.loads(out_host.read_text(encoding="utf-8"))
    finally:
        out_host.unlink(missing_ok=True)
    return pd.DataFrame(payload["rows"], columns=payload["columns"] or list(CANDIDATE_COLUMNS))


@contextmanager
def hide_snapshot_slots_from_agent(paths):
    """Temporarily hide replay/exploration slots from formal strategy code.

    Docker runs candidate code as the non-root ``agent`` user. Making the slot
    roots owner-only is enough to prevent traversal while keeping the current
    `/mnt/snapshot` view available through the current snapshot mirror.
    """
    slots: list[tuple[Path, int]] = []
    for path in (paths.train, paths.valid, paths.test):
        if path.exists():
            slots.append((path, stat.S_IMODE(path.stat().st_mode)))
    try:
        for path, _mode in slots:
            path.chmod(0o700)
        yield
    finally:
        for path, mode in slots:
            path.chmod(mode)


def validate_candidates(candidates: pd.DataFrame, *, universe: set[str]) -> pd.DataFrame:
    """Schema / duplicate / membership checks; failures are explicit."""
    missing = [col for col in CANDIDATE_COLUMNS if col not in candidates.columns]
    if missing:
        raise BacktestError(f"candidate pool missing required columns: {missing}")
    frame = candidates.copy()
    frame["ts_code"] = frame["ts_code"].astype(str)
    duplicates = frame[frame["ts_code"].duplicated()]["ts_code"].tolist()
    if duplicates:
        raise BacktestError(f"candidate pool contains duplicate codes: {duplicates[:5]}")
    unknown = sorted(set(frame["ts_code"]) - universe)
    if unknown:
        raise BacktestError(f"candidate codes outside the visible universe: {unknown[:5]}")
    scores = pd.to_numeric(frame["factor_score"], errors="coerce")
    if scores.isna().any() or not all(math.isfinite(v) for v in scores):
        raise BacktestError("factor_score must be finite numeric for every candidate")
    frame["factor_score"] = scores.astype(float)
    bad_sources = [
        code
        for code, sources in zip(frame["ts_code"], frame["source_artifacts"])
        if not isinstance(sources, (list, tuple))
    ]
    if bad_sources:
        raise BacktestError(f"source_artifacts must be a list for: {bad_sources[:5]}")
    return frame.reset_index(drop=True)


def truncate_candidates(candidates: pd.DataFrame, *, max_candidates: int) -> tuple[pd.DataFrame, int]:
    """Keep the top ``max_candidates`` by abs(factor_score) before NL scoring.

    Oversized pools are truncated (and the truncation recorded), not rejected;
    the Agent is told about this rule in its system prompt.
    """
    if len(candidates) <= max_candidates:
        return candidates.reset_index(drop=True), 0
    kept = candidates.reindex(candidates["factor_score"].abs().sort_values(ascending=False, kind="stable").index)
    kept = kept.head(max_candidates).reset_index(drop=True)
    return kept, len(candidates) - max_candidates


def cross_section_normalize(scores: pd.Series) -> pd.Series:
    """Scale factor scores into [-1, 1] preserving sign and relative size."""
    peak = scores.abs().max()
    if not peak or not math.isfinite(peak):
        return scores * 0.0
    return scores / peak


def compose_final_scores(
    candidates: pd.DataFrame,
    nl_scores: dict[str, dict[str, object]],
    *,
    nl_mode: str,
    factor_weight: float = 0.7,
    nl_weight: float = 0.3,
) -> pd.DataFrame:
    """final_score = 0.7 * factor_score_norm + 0.3 * nl_score on a shared scale.

    ``nl=off`` uses the normalized factor score alone. In ``nl=sample`` mode,
    unsampled candidates receive the average sampled nl_score so the sampled
    and unsampled halves stay on one composition rule (marked ``nl_scored``
    False for audit).
    """
    frame = candidates.copy()
    frame["factor_score_norm"] = cross_section_normalize(frame["factor_score"])
    sampled_values = [float(record["nl_score"]) for record in nl_scores.values()]
    default_nl = float(pd.Series(sampled_values).mean()) if (nl_mode == "sample" and sampled_values) else 0.0
    nl_values, confidences, risk_tags, scored = [], [], [], []
    for code in frame["ts_code"]:
        record = nl_scores.get(code)
        nl_values.append(float(record["nl_score"]) if record else default_nl)
        confidences.append(float(record["confidence"]) if record else 0.0)
        risk_tags.append(list(record["risk_tags"]) if record else [])
        scored.append(record is not None)
    frame["nl_score"] = nl_values
    frame["nl_confidence"] = confidences
    frame["nl_risk_tags"] = risk_tags
    frame["nl_scored"] = scored
    if nl_mode == "off":
        frame["final_score"] = frame["factor_score_norm"]
    elif nl_mode == "sample":
        frame["final_score"] = factor_weight * frame["factor_score_norm"] + nl_weight * frame["nl_score"]
    else:
        frame["final_score"] = [
            factor_weight * fs + nl_weight * ns if has_nl else fs
            for fs, ns, has_nl in zip(frame["factor_score_norm"], frame["nl_score"], frame["nl_scored"])
        ]
    frame["hard_excluded"] = frame["nl_risk_tags"].map(lambda tags: "hard_exclude" in tags)
    return frame


def build_order_plan(
    scored: pd.DataFrame,
    *,
    long_threshold: float,
    short_threshold: float,
    max_total_holdings: int,
    max_single_name_weight: float,
    shortable_codes: frozenset[str] | None = None,
) -> pd.DataFrame:
    """Threshold filter, side-aware ranking, and score-proportional weights.

    Gross exposure scales with the number of selected names
    (``n / max_total_holdings``, at most 1.0) and is distributed across names
    in proportion to ``abs(final_score)``, capped per name at
    ``max_single_name_weight``; capped excess is not redistributed.

    Short candidates that fail the configured shortable universe are skipped
    before sizing, so the plan can roll down to the next short candidate rather
    than wasting a holding slot on a broker reject.
    """
    eligible = scored[~scored["hard_excluded"]].copy()
    longs = eligible[eligible["final_score"] >= long_threshold].copy()
    longs["side"] = "long"
    longs = longs.sort_values("final_score", ascending=False, kind="stable")

    shorts = eligible[eligible["final_score"] <= short_threshold].copy()
    shorts["side"] = "short"
    if shortable_codes is not None:
        shorts = shorts[shorts["ts_code"].astype(str).isin(shortable_codes)]
    shorts = shorts.sort_values("final_score", ascending=True, kind="stable")

    selected = pd.concat([longs, shorts], ignore_index=True)
    selected = selected.reindex(selected["final_score"].abs().sort_values(ascending=False, kind="stable").index)
    selected = selected.head(max_total_holdings).copy()
    abs_scores = selected["final_score"].abs()
    score_sum = float(abs_scores.sum())
    gross_target = min(1.0, len(selected) / max_total_holdings)
    if score_sum > 0:
        weights = (abs_scores / score_sum * gross_target).clip(upper=max_single_name_weight)
    else:
        weights = abs_scores * 0.0
    selected["target_weight"] = [
        weight if side == "long" else -weight for weight, side in zip(weights, selected["side"])
    ]
    plan = selected[
        ["ts_code", "side", "target_weight", "final_score", "factor_score", "nl_score", "reason", "source_artifacts"]
    ].reset_index(drop=True)
    return plan


def validate_order_plan(plan: pd.DataFrame, *, universe: set[str], max_total_holdings: int, max_single_name_weight: float) -> None:
    if plan.empty:
        return
    if plan["ts_code"].duplicated().any():
        raise BacktestError("order plan contains duplicate codes")
    unknown = sorted(set(plan["ts_code"].astype(str)) - universe)
    if unknown:
        raise BacktestError(f"order plan contains codes outside the universe: {unknown[:5]}")
    if len(plan) > max_total_holdings:
        raise BacktestError(f"order plan size {len(plan)} exceeds max_total_holdings={max_total_holdings}")
    bad_side = plan[~plan["side"].isin(["long", "short"])]
    if not bad_side.empty:
        raise BacktestError(f"order plan has unsupported sides: {bad_side['side'].unique().tolist()}")
    if (plan["target_weight"].abs() > max_single_name_weight + 1e-12).any():
        raise BacktestError("order plan weight exceeds max_single_name_weight")
    if (plan["target_weight"].abs().sum()) > 1.0 + 1e-9:
        raise BacktestError("order plan gross exposure exceeds 1.0")
    signs_ok = all(
        (row.side == "long") == (row.target_weight > 0) for row in plan.itertuples()
    )
    if not signs_ok:
        raise BacktestError("order plan target_weight sign does not match side")


@dataclass
class ReplayResult:
    equity_curve: pd.Series
    broker: SimBroker
    decision_date: str
    exit_date: str


def run_fixed_holding_replay(
    plan: pd.DataFrame,
    replay_daily: pd.DataFrame,
    profile: BrokerProfile,
    *,
    decision_time_iso: str,
    shortable_codes: frozenset[str],
) -> ReplayResult:
    """Open on the first replay trade date, hold, close on the last one.

    The buy day / sell day / holding period come from the Fold schedule via the
    replay slot contents (docs/environment_design.md 5.2/5.3).
    """
    market = MarketData(replay_daily)
    if len(market.trade_dates) < 2:
        raise BacktestError("replay region needs at least two trade dates for T+1 entry/exit")
    entry_date, exit_date = market.trade_dates[0], market.trade_dates[-1]
    broker = SimBroker(profile, market, shortable_codes=shortable_codes)
    for row in plan.itertuples():
        order = Order(
            ts_code=row.ts_code,
            side=row.side,
            order_type="target_weight",
            target_weight=float(row.target_weight),
            reason=str(row.reason),
            source_artifacts=list(row.source_artifacts),
        )
        broker.submit_order(order, decision_time=decision_time_iso, fill_date=entry_date)
    broker.fill_open(entry_date)
    equity = {}
    for trade_date in market.trade_dates:
        if trade_date == exit_date and broker.positions:
            broker.mark_to_market(trade_date)
            broker.close_all(trade_date)
        equity[trade_date] = broker.mark_to_market(trade_date)
    return ReplayResult(
        equity_curve=pd.Series(equity).sort_index(), broker=broker, decision_date=entry_date, exit_date=exit_date
    )


def compute_return_stats(result: ReplayResult) -> dict[str, object]:
    """The minimum return statistics from docs/environment_design.md 5.5."""
    broker = result.broker
    curve = result.equity_curve
    initial = broker.initial_equity
    total_return = curve.iloc[-1] / initial - 1.0 if len(curve) else 0.0
    daily_returns = curve.pct_change().dropna()
    sharpe = 0.0
    if len(daily_returns) > 1 and daily_returns.std(ddof=1) > 0:
        sharpe = float(daily_returns.mean() / daily_returns.std(ddof=1) * math.sqrt(TRADING_DAYS_PER_YEAR))
    peak = curve.cummax()
    max_drawdown = float(((peak - curve) / peak).max()) if len(curve) else 0.0
    years = max(len(curve), 1) / TRADING_DAYS_PER_YEAR
    annualized = float((1.0 + total_return) ** (1.0 / years) - 1.0) if total_return > -1.0 else -1.0
    closed = [event for event in broker.events if event["event_type"] == "position_closed"]
    long_pnl = sum(e["realized_pnl"] for e in closed if e["side"] == "long")
    short_pnl = sum(e["realized_pnl"] for e in closed if e["side"] == "short")
    wins = sum(1 for e in closed if e["realized_pnl"] > 0)
    orders = broker.query_orders()
    filled_notional = sum(
        abs(o["target_weight"]) * initial for o in orders if o["status"] == "filled"
    )
    per_stock = [
        {
            "ts_code": event["ts_code"],
            "side": event["side"],
            "exit_date": event["trade_date"],
            "exit_price": event["price"],
            "realized_pnl": event["realized_pnl"],
            "forced": event.get("forced", False),
        }
        for event in closed
    ]
    status_counts: dict[str, int] = {}
    for order in orders:
        status_counts[str(order["status"])] = status_counts.get(str(order["status"]), 0) + 1
    return {
        "initial_cash": initial,
        "final_equity": float(curve.iloc[-1]) if len(curve) else initial,
        "total_return": float(total_return),
        "long_return": float(long_pnl / initial),
        "short_return": float(short_pnl / initial),
        "annualized_return": annualized,
        "sharpe": sharpe,
        "max_drawdown": max_drawdown,
        "win_rate": float(wins / len(closed)) if closed else 0.0,
        "holdings_count": len(closed),
        "turnover": float(2.0 * filled_notional / initial),
        "order_status_counts": status_counts,
        "reject_counts": dict(broker.reject_counts),
        "margin_secs_reject_count": broker.reject_counts.get("margin_secs_not_shortable", 0),
        "broker_inventory_reject_count": broker.reject_counts.get("broker_inventory_unavailable", 0),
        "fees_paid": float(broker.fees_paid),
        "stamp_duty_paid": float(broker.stamp_duty_paid),
        "slippage_bps_assumed": broker.profile.slippage_bps,
        "short_borrow_fees": float(broker.borrow_fees),
        "forced_close_events": sum(1 for e in broker.events if e["event_type"] == "forced_close_triggered"),
        "equity_curve": {str(k): float(v) for k, v in curve.items()},
        "decision_date": result.decision_date,
        "exit_date": result.exit_date,
        "per_stock": per_stock,
        "broker_events": broker.events,
    }
