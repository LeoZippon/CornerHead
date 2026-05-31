from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
import math

from hl_trader.environment.schemas import CostModel, TradeStrategyPolicy


@dataclass(frozen=True)
class Order:
    trade_date: date
    ts_code: str
    side: str
    shares: int
    limit_price: float | None = None
    reason: str = ""


@dataclass(frozen=True)
class Fill:
    trade_date: date
    ts_code: str
    side: str
    shares: int
    price: float
    cost: float
    reason: str


@dataclass
class Position:
    ts_code: str
    shares: int
    available_shares: int = 0
    cost_basis: float = 0.0


@dataclass
class PortfolioState:
    cash: float
    positions: dict[str, Position] = field(default_factory=dict)

    def gross_exposure(self, marks: dict[str, float]) -> float:
        return sum(pos.shares * marks.get(code, 0.0) for code, pos in self.positions.items())

    def equity(self, marks: dict[str, float]) -> float:
        return self.cash + self.gross_exposure(marks)


class BrokerSimulator:
    def __init__(self, policy: TradeStrategyPolicy, cost_model: CostModel | None = None) -> None:
        policy.validate()
        if policy.settlement_mode not in {"t_plus_1", "t_plus_0"}:
            raise ValueError(f"unsupported settlement_mode={policy.settlement_mode}")
        self.policy = policy
        self.cost_model = cost_model or CostModel()

    def execute_order(
        self,
        state: PortfolioState,
        order: Order,
        price: float,
        *,
        up_limit: float | None = None,
        down_limit: float | None = None,
        suspended: bool = False,
    ) -> Fill | None:
        if suspended or order.shares <= 0:
            return None
        if order.side not in {"buy", "sell"}:
            raise ValueError(f"unsupported order side={order.side}")
        if price <= 0 or math.isnan(price):
            raise ValueError("execution price must be positive")
        if order.limit_price is not None:
            if order.limit_price <= 0 or math.isnan(order.limit_price):
                raise ValueError("limit_price must be positive when provided")
            if order.side == "buy" and price > order.limit_price:
                return None
            if order.side == "sell" and price < order.limit_price:
                return None
        if order.side == "buy" and up_limit is not None and price >= up_limit:
            return None
        if order.side == "sell" and down_limit is not None and price <= down_limit:
            return None
        shares = int(order.shares // 100 * 100)
        if shares <= 0:
            return None
        notional = shares * price
        if order.side == "buy":
            cost = self.cost_model.estimate_buy_cost(notional)
            affordable = int((state.cash / (price * (1 + (self.cost_model.commission_bps + self.cost_model.slippage_bps) / 10_000.0))) // 100 * 100)
            shares = min(shares, affordable)
            if shares <= 0:
                return None
            notional = shares * price
            cost = self.cost_model.estimate_buy_cost(notional)
            state.cash -= notional + cost
            pos = state.positions.get(order.ts_code) or Position(order.ts_code, 0, 0, 0.0)
            total_cost = pos.cost_basis * pos.shares + notional + cost
            pos.shares += shares
            pos.available_shares += 0 if self.policy.settlement_mode == "t_plus_1" else shares
            pos.cost_basis = total_cost / pos.shares
            state.positions[order.ts_code] = pos
        else:
            pos = state.positions.get(order.ts_code)
            if pos is None:
                return None
            sellable = pos.available_shares if self.policy.settlement_mode == "t_plus_1" else pos.shares
            shares = min(shares, sellable, pos.shares)
            shares = int(shares // 100 * 100)
            if shares <= 0:
                return None
            notional = shares * price
            cost = self.cost_model.estimate_sell_cost(notional)
            state.cash += notional - cost
            pos.shares -= shares
            pos.available_shares = max(0, pos.available_shares - shares)
            if pos.shares == 0:
                state.positions.pop(order.ts_code, None)
        return Fill(order.trade_date, order.ts_code, order.side, shares, price, cost, order.reason)

    @staticmethod
    def settle_t_plus_1(state: PortfolioState) -> None:
        for position in state.positions.values():
            position.available_shares = position.shares
