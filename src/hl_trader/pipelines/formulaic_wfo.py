from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date

import pandas as pd

from hl_trader.agent import FormulaicParameters, select_formulaic_candidates
from hl_trader.environment.events import CheckpointDetector, EventCheckpoint
from hl_trader.environment.execution import BrokerSimulator, Order, PortfolioState
from hl_trader.environment.portfolio import equal_weight_targets
from hl_trader.environment.protocols import assert_result_available
from hl_trader.environment.schemas import CostModel, TradeStrategyPolicy
from hl_trader.environment.storage import TrialLedger
from hl_trader.environment.wfo.splitter import Fold


@dataclass(frozen=True)
class FoldRunResult:
    fold_id: str
    parameters: FormulaicParameters
    train_score: float
    test_start: date
    test_end: date
    start_equity: float
    end_equity: float
    fills: int
    short_theoretical_return: float = 0.0
    short_cash_collateral: float = 0.0
    long_return: float | None = None

    @property
    def test_return(self) -> float:
        return self.end_equity / self.start_equity - 1.0 if self.start_equity else 0.0

    @property
    def test_long_return(self) -> float:
        return self.test_return if self.long_return is None else self.long_return

    @property
    def test_short_return(self) -> float:
        return self.short_theoretical_return


def monthly_decision_dates(features: pd.DataFrame) -> list[str]:
    if features.empty:
        return []
    if "feature_date" not in features.columns:
        raise ValueError("features must include feature_date")
    dates = pd.Series(sorted(features["feature_date"].dropna().astype(str).unique()), name="feature_date")
    if dates.empty:
        return []
    months = dates.str[:6]
    return dates.groupby(months).max().tolist()


class FormulaicWfoRunner:
    def __init__(
        self,
        policy: TradeStrategyPolicy,
        cost_model: CostModel | None = None,
        ledger: TrialLedger | None = None,
    ) -> None:
        self.policy = policy
        if not self.policy.allows("rebalance"):
            raise ValueError("FormulaicWfoRunner requires TradeStrategyPolicy.allowed_actions to include rebalance")
        self.broker = BrokerSimulator(policy, cost_model)
        self.ledger = ledger

    def fit_parameters(self, features: pd.DataFrame, fold: Fold, grid: list[FormulaicParameters]) -> tuple[FormulaicParameters, float]:
        if not grid:
            raise ValueError("parameter grid cannot be empty")
        train = self._slice_features(features, fold.train_start, fold.train_end)
        assert_result_available(train, train_end=fold.train_end, require_column=True)
        decision_dates = monthly_decision_dates(train)
        best_params = grid[0]
        best_score = float("-inf")
        for params in grid:
            score = self._score_params(train, decision_dates, params, fold.train_start, fold.train_end)
            if score > best_score:
                best_params = params
                best_score = score
        if best_score == float("-inf"):
            best_score = 0.0
        return best_params, best_score

    def run_fold(
        self,
        features: pd.DataFrame,
        fold: Fold,
        params: FormulaicParameters,
        *,
        initial_cash: float = 1_000_000.0,
    ) -> FoldRunResult:
        test = self._slice_features(features, fold.test_start, fold.test_end)
        decision_dates = set(monthly_decision_dates(test))
        feature_dates = sorted(test["feature_date"].dropna().astype(str).unique().tolist())
        state = PortfolioState(cash=initial_cash)
        start_equity = initial_cash
        fills = 0
        test_pricing = test
        price_lookup = self._price_lookup(test_pricing)
        checkpoints = self._test_event_checkpoints(test_pricing, fold)
        checkpoints_by_feature_date = self._checkpoints_by_feature_date(checkpoints)
        self._record_event_checkpoints(checkpoints, fold)
        for feature_date in feature_dates:
            cross = test[test["feature_date"].astype(str) == feature_date]
            if cross.empty:
                continue
            tradable_date = self._tradable_date_for_decision(cross, feature_date)
            trade_date = self._to_date(tradable_date)
            if not fold.contains_test_date(trade_date):
                if self.ledger and feature_date in decision_dates:
                    self.ledger.append({
                        "event_type": "rebalance_skipped",
                        "fold_id": fold.fold_id,
                        "feature_date": feature_date,
                        "tradable_date": tradable_date,
                        "reason": "tradable_date_outside_test_window",
                    })
                continue
            marks = {code: price for (d, code), price in price_lookup.items() if d == tradable_date}
            self.broker.settle_t_plus_1(state)
            constraints = self._constraints_for_date(test_pricing, tradable_date)
            equity = state.equity(marks)
            max_turnover = equity * self.policy.max_daily_turnover_pct
            used_turnover = 0.0

            day_checkpoints = checkpoints_by_feature_date.get(feature_date, [])
            event_orders = self._event_orders(state, day_checkpoints, marks, trade_date, max_turnover)
            for order, checkpoint in event_orders:
                price = marks.get(order.ts_code)
                if price is None:
                    continue
                fill = self.broker.execute_order(state, order, price, **constraints.get(order.ts_code, {}))
                used_turnover += (fill.shares * fill.price) if fill else 0.0
                if self.ledger:
                    self.ledger.append({
                        "event_type": "event_action",
                        "fold_id": fold.fold_id,
                        "feature_date": feature_date,
                        "tradable_date": tradable_date,
                        "checkpoint": checkpoint,
                        "action": order.reason,
                        "shares_requested": order.shares,
                        "filled": fill is not None,
                        "fill": fill,
                        "can_affect_trading": True,
                    })
                if fill:
                    fills += 1
                    if self.ledger:
                        self.ledger.append({"event_type": "fill", "fold_id": fold.fold_id, "fill": fill})

            if feature_date not in decision_dates:
                continue

            event_excluded_codes = self._event_excluded_codes(day_checkpoints)
            selected = [code for code in select_formulaic_candidates(cross, params) if code not in event_excluded_codes]
            targets = equal_weight_targets(selected, max_names=params.top_n)
            orders = self._rebalance_orders(state, targets, marks, trade_date, turnover_budget=max(0.0, max_turnover - used_turnover))
            for order in orders:
                price = marks.get(order.ts_code)
                if price is None:
                    continue
                fill = self.broker.execute_order(state, order, price, **constraints.get(order.ts_code, {}))
                if fill:
                    fills += 1
                    if self.ledger:
                        self.ledger.append({"event_type": "fill", "fold_id": fold.fold_id, "fill": fill})
            if self.ledger:
                self.ledger.append({
                    "event_type": "rebalance",
                    "fold_id": fold.fold_id,
                    "feature_date": feature_date,
                    "tradable_date": tradable_date,
                    "parameters": asdict(params),
                    "selected_count": len(selected),
                    "event_excluded_count": len(event_excluded_codes),
                    "cash": state.cash,
                })
        end_marks = self._last_marks_for_state(features, state, fold.test_end)
        end_equity = state.equity(end_marks)
        return FoldRunResult(fold.fold_id, params, 0.0, fold.test_start, fold.test_end, start_equity, end_equity, fills)

    def _test_event_checkpoints(self, features: pd.DataFrame, fold: Fold) -> list[EventCheckpoint]:
        if features.empty:
            return []
        checkpoints = CheckpointDetector().detect(features)
        return [checkpoint for checkpoint in checkpoints if fold.contains_test_date(self._to_date(checkpoint.tradable_date))]

    @staticmethod
    def _checkpoints_by_feature_date(checkpoints: list[EventCheckpoint]) -> dict[str, list[EventCheckpoint]]:
        grouped: dict[str, list[EventCheckpoint]] = {}
        for checkpoint in checkpoints:
            grouped.setdefault(checkpoint.feature_date, []).append(checkpoint)
        return grouped

    def _record_event_checkpoints(self, checkpoints: list[EventCheckpoint], fold: Fold) -> int:
        if self.ledger is None or not checkpoints:
            return 0
        written = 0
        for checkpoint in checkpoints:
            self.ledger.append({
                "event_type": "event_checkpoint",
                "fold_id": fold.fold_id,
                "checkpoint": checkpoint,
                "action": "eligible",
                "action_impact": "execution_policy",
                "can_affect_trading": self._event_action(checkpoint) is not None,
            })
            written += 1
        return written

    def run_wfo(
        self,
        features: pd.DataFrame,
        folds: list[Fold],
        grid: list[FormulaicParameters],
        *,
        initial_cash: float = 1_000_000.0,
    ) -> list[FoldRunResult]:
        results: list[FoldRunResult] = []
        for fold in folds:
            params, train_score = self.fit_parameters(features, fold, grid)
            result = self.run_fold(features, fold, params, initial_cash=initial_cash)
            results.append(FoldRunResult(
                result.fold_id,
                params,
                train_score,
                result.test_start,
                result.test_end,
                result.start_equity,
                result.end_equity,
                result.fills,
                result.short_theoretical_return,
                result.short_cash_collateral,
                result.long_return,
            ))
            if self.ledger:
                self.ledger.append({"event_type": "fold_result", "result": results[-1]})
        return results

    def _score_params(
        self,
        train: pd.DataFrame,
        decision_dates: list[str],
        params: FormulaicParameters,
        train_start: date,
        train_end: date,
    ) -> float:
        if len(decision_dates) < 2:
            return float("-inf")
        price_lookup = self._price_lookup(train)
        realized: list[float] = []
        for current, nxt in zip(decision_dates[:-1], decision_dates[1:]):
            cross = train[train["feature_date"].astype(str) == current]
            next_cross = train[train["feature_date"].astype(str) == nxt]
            if cross.empty or next_cross.empty:
                continue
            entry_date = self._tradable_date_for_decision(cross, current)
            exit_date = self._tradable_date_for_decision(next_cross, nxt)
            entry_dt = self._to_date(entry_date)
            exit_dt = self._to_date(exit_date)
            if entry_dt < train_start or entry_dt > train_end or exit_dt > train_end or exit_dt <= entry_dt:
                continue
            for code in select_formulaic_candidates(cross, params):
                entry = price_lookup.get((entry_date, code))
                exit_ = price_lookup.get((exit_date, code))
                if entry and exit_:
                    realized.append(exit_ / entry - 1.0)
        return float(pd.Series(realized).mean()) if realized else float("-inf")

    @staticmethod
    def _slice_features(features: pd.DataFrame, start: date, end: date) -> pd.DataFrame:
        if "feature_date" not in features.columns:
            raise ValueError("features must include feature_date")
        start_key = start.strftime("%Y%m%d")
        end_key = end.strftime("%Y%m%d")
        return features[(features["feature_date"].astype(str) >= start_key) & (features["feature_date"].astype(str) <= end_key)].copy()

    @staticmethod
    def _price_lookup(features: pd.DataFrame) -> dict[tuple[str, str], float]:
        _require_columns(features, ["feature_date", "ts_code", "close"])
        frame = features[["feature_date", "ts_code", "close"]].dropna().copy()
        frame["feature_date"] = frame["feature_date"].astype(str)
        frame["ts_code"] = frame["ts_code"].astype(str)
        duplicated = frame.duplicated(["feature_date", "ts_code"], keep=False)
        if duplicated.any():
            sample = frame.loc[duplicated, ["feature_date", "ts_code"]].head(3).to_dict("records")
            raise ValueError(f"duplicate price rows for feature_date/ts_code: {sample}")
        frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
        frame = frame.dropna(subset=["close"])
        return {(row.feature_date, row.ts_code): float(row.close) for row in frame.itertuples(index=False)}

    @staticmethod
    def _constraints_for_date(features: pd.DataFrame, tradable_date: str) -> dict[str, dict[str, float | bool | None]]:
        _require_columns(features, ["feature_date", "ts_code", "up_limit", "down_limit", "is_suspended"])
        frame = features[features["feature_date"].astype(str) == tradable_date]
        duplicated = frame.astype({"ts_code": "string"}).duplicated(["feature_date", "ts_code"], keep=False)
        if duplicated.any():
            sample = frame.loc[duplicated, ["feature_date", "ts_code"]].head(3).to_dict("records")
            raise ValueError(f"duplicate constraint rows for feature_date/ts_code: {sample}")
        constraints: dict[str, dict[str, float | bool | None]] = {}
        for row in frame.itertuples(index=False):
            code = str(getattr(row, "ts_code"))
            constraints[code] = {
                "up_limit": float(getattr(row, "up_limit")) if pd.notna(getattr(row, "up_limit", None)) else None,
                "down_limit": float(getattr(row, "down_limit")) if pd.notna(getattr(row, "down_limit", None)) else None,
                "suspended": bool(getattr(row, "is_suspended", False)),
            }
        return constraints

    def _rebalance_orders(
        self,
        state: PortfolioState,
        targets: dict[str, float],
        marks: dict[str, float],
        trade_date: date,
        *,
        turnover_budget: float | None = None,
    ) -> list[Order]:
        equity = state.equity(marks)
        max_turnover = equity * self.policy.max_daily_turnover_pct if turnover_budget is None else turnover_budget
        if max_turnover <= 0:
            return []
        desired: list[tuple[str, str, int, float, str]] = []
        all_codes = set(state.positions) | set(targets)
        for code in sorted(all_codes):
            price = marks.get(code)
            if price is None or price <= 0:
                continue
            current_shares = state.positions.get(code).shares if code in state.positions else 0
            current_notional = current_shares * price
            target_notional = equity * targets.get(code, 0.0)
            share_delta = int((target_notional - current_notional) / price // 100 * 100)
            if share_delta > 0:
                reason = self._rebalance_reason("enter" if current_shares <= 0 else "add")
                if reason is not None:
                    desired.append(("buy", code, share_delta, price, reason))
            elif share_delta < 0:
                reason = self._rebalance_reason("exit" if targets.get(code, 0.0) <= 0 else "trim")
                if reason is not None:
                    desired.append(("sell", code, abs(share_delta), price, reason))
        orders: list[Order] = []
        used_turnover = 0.0
        for side, code, requested_shares, price, reason in sorted(desired, key=lambda item: 0 if item[0] == "sell" else 1):
            remaining_turnover = max_turnover - used_turnover
            if remaining_turnover < price * 100:
                continue
            capped_shares = int(min(requested_shares, remaining_turnover / price) // 100 * 100)
            if capped_shares <= 0:
                continue
            orders.append(Order(trade_date, code, side, capped_shares, reason=reason))
            used_turnover += capped_shares * price
        return orders

    def _rebalance_reason(self, reason: str) -> str | None:
        return reason if self.policy.allows(reason) else None

    def _event_orders(
        self,
        state: PortfolioState,
        checkpoints: list[EventCheckpoint],
        marks: dict[str, float],
        trade_date: date,
        turnover_budget: float,
    ) -> list[tuple[Order, EventCheckpoint]]:
        if turnover_budget <= 0:
            return []
        orders: list[tuple[Order, EventCheckpoint]] = []
        used_turnover = 0.0
        seen: set[tuple[str, str]] = set()
        for checkpoint in checkpoints:
            action = self._event_action(checkpoint)
            if action is None:
                continue
            code = checkpoint.ts_code
            key = (code, action)
            if key in seen:
                continue
            seen.add(key)
            pos = state.positions.get(code)
            price = marks.get(code)
            if pos is None or price is None or price <= 0:
                continue
            fraction = 1.0 if action == "exit" else self.policy.event_de_risk_pct
            requested_shares = int((pos.available_shares * fraction) // 100 * 100)
            remaining_turnover = turnover_budget - used_turnover
            if requested_shares <= 0 or remaining_turnover < price * 100:
                continue
            shares = int(min(requested_shares, remaining_turnover / price) // 100 * 100)
            if shares <= 0:
                continue
            orders.append((Order(trade_date, code, "sell", shares, reason=action), checkpoint))
            used_turnover += shares * price
        return orders

    def _event_excluded_codes(self, checkpoints: list[EventCheckpoint]) -> set[str]:
        return {checkpoint.ts_code for checkpoint in checkpoints if self._event_action(checkpoint) in {"event_de_risk", "exit"}}

    def _event_action(self, checkpoint: EventCheckpoint) -> str | None:
        if checkpoint.event_type == "large_price_move":
            pct_chg = checkpoint.payload.get("pct_chg")
            if pct_chg is None:
                return None
            pct = float(pct_chg)
            if pct <= -self.policy.event_exit_loss_pct and self.policy.allows("exit"):
                return "exit"
            if pct < 0 and self.policy.allows("event_de_risk") and self.policy.event_de_risk_pct > 0:
                return "event_de_risk"
        if checkpoint.event_type == "price_limit_status" and self._is_down_limit(checkpoint):
            if self.policy.allows("exit"):
                return "exit"
            if self.policy.allows("event_de_risk") and self.policy.event_de_risk_pct > 0:
                return "event_de_risk"
        return None

    @staticmethod
    def _is_down_limit(checkpoint: EventCheckpoint) -> bool:
        status = str(checkpoint.payload.get("limit_status", "")).strip().upper()
        return status in {"D", "DL", "DOWN", "跌停", "LOWER"}

    @staticmethod
    def _last_marks_for_state(features: pd.DataFrame, state: PortfolioState, end: date) -> dict[str, float]:
        if not state.positions:
            return {}
        end_key = end.strftime("%Y%m%d")
        frame = features[features["feature_date"].astype(str) <= end_key]
        frame = frame[frame["ts_code"].isin(state.positions)]
        frame = frame.sort_values(["ts_code", "feature_date"]).drop_duplicates("ts_code", keep="last")
        return {str(row.ts_code): float(row.close) for row in frame.itertuples(index=False) if pd.notna(row.close)}

    @staticmethod
    def _to_date(value: str) -> date:
        return pd.Timestamp(value).date() if "-" in value else pd.Timestamp(f"{value[:4]}-{value[4:6]}-{value[6:8]}").date()

    @staticmethod
    def _tradable_date_for_decision(cross_section: pd.DataFrame, feature_date: str) -> str:
        if "tradable_date" not in cross_section.columns:
            raise ValueError("features must include tradable_date")
        dates = sorted(cross_section["tradable_date"].dropna().astype(str).unique())
        if not dates:
            raise ValueError(f"missing tradable_date for feature_date={feature_date}")
        if len(dates) > 1:
            raise ValueError(f"multiple tradable_date values for feature_date={feature_date}: {dates[:3]}")
        return dates[0]


def _require_columns(frame: pd.DataFrame, columns: list[str]) -> None:
    missing = [column for column in columns if column not in frame.columns]
    if missing:
        raise ValueError(f"missing required feature columns: {missing}")
