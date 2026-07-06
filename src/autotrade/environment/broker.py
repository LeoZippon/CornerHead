"""Simulated Broker aligned with the official full-QMT in-client trading API.

The host boundary mirrors QMT's strategy API (docs/environment_design.md §3.2):
``passorder`` submits by official ``opType`` code, ``cancel`` withdraws,
``get_trade_detail_data`` returns ACCOUNT/POSITION/ORDER/DEAL records, and the
credit queries (``get_debt_contract``/``get_assure_contract``/
``get_enable_short_contract``) expose the 信用账户 surface — so a live
``QMTBroker`` adapter maps mechanically. The Broker owns no trading-strategy
logic; strategies live in the Agent's ``output`` and drive the ``ctx.broker``
verbs, which the replay engine translates into ``passorder`` submissions.

Every run operates TWO separate accounts side by side, like a real investor
holding both at one broker (the opType routes the account, so ``passorder``
needs no extra selector):

* ``"stock"`` — 普通账户: long-only cash trading (opType 23/24). Own cash,
  positions and T+1 state; no margin, no debt, no maintenance concept.
* ``"credit"`` — 信用账户: 担保品买卖 (33/34), 融资买入 (27), 融券卖出 (28),
  买券还券 (29), 卖券还款 (31), 直接还款 (32), with 负债合约 tracking,
  per-calendar-day interest, 保证金可用余额 gating, 维持担保比例 and forced
  close. Only credit-account assets count as collateral — the stock account
  never backs credit debt, and a maintenance breach liquidates the credit
  account only. 直接还券 (30) is intentionally unsupported: each account keeps
  one side per code (``opposite_side_position_open``), which makes the op
  structurally unreachable; 买券还券 covers the economic need.

Cash may move between the accounts via :meth:`SimBroker.transfer` (银证转账-style,
a sim extension executed manually in live trading): outbound transfers from the
credit account additionally require the post-transfer 维持担保比例 to stay at or
above ``maintenance_withdraw_ratio`` while any debt is outstanding. The same code
may be held long in the stock account and short in the credit account.

The Broker still enforces every A-share market rule (docs/environment_design.md
§3): cash/margin, T+1 sellable balance, lot size, limit up/down, suspension, the
configured short-inventory mode (default ``proxy_margin_secs``), the 融券卖出
limit-price rule (申报价不得低于最新成交价 — shorts must be limit orders), optional
concentration limits, commission, stamp duty, slippage, and forced close. Every
order/reject is recorded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import pandas as pd

from autotrade.environment.broker_core import (
    STAMP_DUTY_CUTOVER,
    CostModel,
    DebtContract,
    accrue_debt_interest,
    credit_maintenance_ratio,
    enable_bail_balance,
    lot_floor,
    project_open,
    project_reduce,
    release_fin_shares,
    repay_fin,
    repay_slo,
    resolve_shares,
)
from autotrade.environment.runtime import new_id

SHORT_INVENTORY_MODES = ("proxy_margin_secs", "broker_inventory", "theoretical_short")
ACCOUNT_TYPES = ("stock", "credit")


@dataclass(frozen=True)
class BrokerProfile:
    """Default GJZQ-style dual-account replay profile (docs/environment_design.md §3.3).

    The maintenance closeout line (130%) follows the pre-2019 exchange floor that
    brokers still commonly contract; the warning line is recorded for audit only,
    while the withdraw line gates outbound credit-account transfers.
    ``assure_ratio`` is a flat 担保品折算率 approximation (exchange caps: index
    constituents ≤70%, other stocks ≤65%); ``fin_rate_annual`` and
    ``slo_rate_annual`` are flagged research assumptions until per-security broker
    fee files are wired in.
    """

    stock_initial_cash: float = 500_000.0
    credit_initial_cash: float = 500_000.0
    commission_bps: float = 1.0
    min_commission_cny: float = 5.0
    stamp_duty_sell_bps_before_cutover: float = 10.0
    stamp_duty_sell_bps_from_cutover: float = 5.0
    slippage_bps: float = 5.0
    max_total_holdings: int | None = None
    short_inventory_mode: str = "proxy_margin_secs"
    fin_margin_ratio: float = 1.0  # 融资保证金比例 (exchange floor 100%)
    slo_margin_ratio: float = 1.0
    slo_margin_ratio_private_fund: float = 1.2
    is_private_fund: bool = False
    fin_rate_annual: float = 0.0835  # 融资利率 (assumed)
    slo_rate_annual: float = 0.085  # 融券费率 (assumed)
    assure_ratio: float = 0.70  # flat 担保品折算率 approximation
    fin_max_quota: float | None = None  # 融资授信额度 (None = ungated)
    slo_max_quota: float | None = None  # 融券授信额度 (None = ungated)
    # Dividends/rights against short positions are intentionally not modeled yet.
    short_corporate_actions: str = "disabled"
    maintenance_closeout_ratio: float = 1.30
    # Reference lines recorded for audit only; the engine enforces just
    # maintenance_closeout_ratio (forced close), not these two.
    maintenance_warning_ratio: float = 1.40
    maintenance_withdraw_ratio: float = 3.00
    max_single_name_weight: float | None = None
    profile_id: str = "gjzq_dual_v1"
    source: str = "docs/environment_design.md#33-回放-profile强制约束与做空模式"
    formula_source: str = "https://www.sse.com.cn/services/tradingservice/margin/edu/c/10074042/files/a1f1c4833302451fb9130dbb94116c56.pdf"
    maintenance_source: str = "https://www.gjzq.com.cn/main/a/rzrq/index.html"

    def __post_init__(self) -> None:
        if self.stock_initial_cash < 0 or self.credit_initial_cash < 0:
            raise ValueError("initial cash must be non-negative")
        if self.stock_initial_cash + self.credit_initial_cash <= 0:
            raise ValueError("combined initial cash must be positive")
        if self.short_inventory_mode not in SHORT_INVENTORY_MODES:
            raise ValueError(f"unsupported short_inventory_mode={self.short_inventory_mode}")
        if self.max_total_holdings is not None and self.max_total_holdings <= 0:
            raise ValueError("max_total_holdings must be positive")
        if self.max_single_name_weight is not None and self.max_single_name_weight <= 0:
            raise ValueError("max_single_name_weight must be positive")
        if not 0.0 < self.assure_ratio <= 1.0:
            raise ValueError("assure_ratio must be in (0, 1]")

    @property
    def effective_slo_margin_ratio(self) -> float:
        return self.slo_margin_ratio_private_fund if self.is_private_fund else self.slo_margin_ratio

    @property
    def cost_model(self) -> CostModel:
        """SimBroker's deterministic fill-cost model (the host-side single source of
        truth for commission/duty/slippage/short margin)."""
        return CostModel(
            commission_bps=self.commission_bps,
            min_commission_cny=self.min_commission_cny,
            stamp_duty_sell_bps_before_cutover=self.stamp_duty_sell_bps_before_cutover,
            stamp_duty_sell_bps_from_cutover=self.stamp_duty_sell_bps_from_cutover,
            slippage_bps=self.slippage_bps,
            slo_margin_ratio=self.effective_slo_margin_ratio,
        )

    def commission(self, notional: float) -> float:
        return self.cost_model.commission(notional)

    def stamp_duty_on_sale(self, notional: float, trade_date: str) -> float:
        return self.cost_model.stamp_duty_on_sale(notional, trade_date)

    def slipped_price(self, price: float, *, is_buy: bool) -> float:
        return self.cost_model.slipped_price(price, is_buy=is_buy)

    def to_record(self) -> dict[str, object]:
        return {
            "profile_id": self.profile_id,
            "source": self.source,
            "formula_source": self.formula_source,
            "maintenance_source": self.maintenance_source,
            "stock_initial_cash": self.stock_initial_cash,
            "credit_initial_cash": self.credit_initial_cash,
            "commission_bps": self.commission_bps,
            "min_commission_cny": self.min_commission_cny,
            "stamp_duty_sell_bps_before_cutover": self.stamp_duty_sell_bps_before_cutover,
            "stamp_duty_sell_bps_from_cutover": self.stamp_duty_sell_bps_from_cutover,
            "stamp_duty_cutover_date": STAMP_DUTY_CUTOVER,
            "slippage_bps": self.slippage_bps,
            "max_total_holdings": self.max_total_holdings,
            "short_inventory_mode": self.short_inventory_mode,
            "fin_margin_ratio": self.fin_margin_ratio,
            "slo_margin_ratio": self.effective_slo_margin_ratio,
            "fin_rate_annual": self.fin_rate_annual,
            "slo_rate_annual": self.slo_rate_annual,
            "credit_rates_are_assumed": True,
            "assure_ratio": self.assure_ratio,
            "fin_max_quota": self.fin_max_quota,
            "slo_max_quota": self.slo_max_quota,
            "short_corporate_actions": self.short_corporate_actions,
            "maintenance_closeout_ratio": self.maintenance_closeout_ratio,
            "maintenance_warning_ratio": self.maintenance_warning_ratio,
            "maintenance_withdraw_ratio": self.maintenance_withdraw_ratio,
            "max_single_name_weight": self.max_single_name_weight,
        }


class MarketData:
    """Daily replay bars indexed by (trade_date, ts_code).

    Expects normalized snapshot units (CNY prices) with columns: open, close,
    up_limit, down_limit, is_suspended.
    """

    REQUIRED = ("trade_date", "ts_code", "open", "close")

    def __init__(self, daily: pd.DataFrame) -> None:
        missing = [col for col in self.REQUIRED if col not in daily.columns]
        if missing:
            raise ValueError(f"replay daily data missing columns: {missing}")
        frame = daily.copy()
        frame["trade_date"] = frame["trade_date"].astype(str)
        frame["ts_code"] = frame["ts_code"].astype(str)
        self._bars = frame.set_index(["trade_date", "ts_code"]).sort_index()
        self.trade_dates = sorted(frame["trade_date"].unique())

    def bar(self, trade_date: str, ts_code: str) -> pd.Series | None:
        try:
            row = self._bars.loc[(str(trade_date), str(ts_code))]
        except KeyError:
            return None
        return row.iloc[0] if isinstance(row, pd.DataFrame) else row

    @staticmethod
    def is_suspended(bar: pd.Series | None) -> bool:
        return bar is None or bool(bar.get("is_suspended", False))

    @staticmethod
    def limit_up_blocked_at_price(bar: pd.Series, price: object) -> bool:
        limit = bar.get("up_limit")
        return pd.notna(limit) and pd.notna(price) and float(price) >= float(limit)

    @staticmethod
    def limit_down_blocked_at_price(bar: pd.Series, price: object) -> bool:
        limit = bar.get("down_limit")
        return pd.notna(limit) and pd.notna(price) and float(price) <= float(limit)


@dataclass
class Order:
    """Audited record of a single broker primitive call (filled or rejected)."""

    ts_code: str
    action: str  # buy | sell | short | cover | close | fin_buy | sell_repay | direct_repay
    side: str  # "long" | "short"
    requested_amount: int
    trade_date: str
    decision_time: str = ""
    price: float | None = None
    filled_quantity: int = 0
    status: str = "submitted"
    reject_reason: str | None = None
    reason: str = ""
    source_artifacts: list[str] = field(default_factory=list)
    price_label: str = "price"
    account: str = ""
    op_type: int | None = None
    order_id: str = field(default_factory=lambda: new_id("ord"))

    def to_record(self) -> dict[str, object]:
        return {
            "order_id": self.order_id,
            "account": self.account,
            "op_type": self.op_type,
            "ts_code": self.ts_code,
            "action": self.action,
            "side": self.side,
            "requested_amount": self.requested_amount,
            "filled_quantity": self.filled_quantity,
            "price": self.price,
            "price_label": self.price_label,
            "status": self.status,
            "reject_reason": self.reject_reason,
            "decision_time": self.decision_time,
            "trade_date": self.trade_date,
            "reason": self.reason,
            "source_artifacts": list(self.source_artifacts),
        }


@dataclass
class Position:
    """An aggregate long or short position with average cost and T+1 lock.

    ``locked_today`` is the share count acquired on ``locked_date`` that the
    T+1 rule forbids selling/covering until a later trade date.
    """

    ts_code: str
    side: str
    quantity: int
    entry_price: float
    entry_date: str
    entry_cost: float
    last_price: float
    locked_today: int = 0
    locked_date: str = ""

    @property
    def market_value(self) -> float:
        return self.quantity * self.last_price

    @property
    def short_liability(self) -> float:
        return self.market_value if self.side == "short" else 0.0

    @property
    def sellable_quantity(self) -> int:
        """Shares that may be sold (long) or covered (short) right now.

        A long position is T+1 locked: shares acquired today (``locked_today``) are
        not sellable until a later date. A short position has no T+1 sell lock — the
        融券 mechanic permits same-day cover (买券还券) — so its full quantity is
        always coverable."""
        if self.side == "short":
            return self.quantity
        return max(self.quantity - self.locked_today, 0)


class optype:
    """Official QMT ``passorder`` 下单操作类型 codes (股票/信用 subset, API doc §3.2.4)."""

    STOCK_BUY = 23  # 股票买入
    STOCK_SELL = 24  # 股票卖出
    FIN_BUY = 27  # 融资买入
    SLO_SELL = 28  # 融券卖出
    BUY_SECU_REPAY = 29  # 买券还券
    DIRECT_SECU_REPAY = 30  # 直接还券 — unsupported (module docstring)
    SELL_REPAY = 31  # 卖券还款
    DIRECT_REPAY = 32  # 直接还款
    CREDIT_BUY = 33  # 信用账号股票买入 (担保品买入)
    CREDIT_SELL = 34  # 信用账号股票卖出 (担保品卖出)


class prtype:
    """Official QMT ``passorder`` 价格类型 codes (the subset the sim matches)."""

    LATEST = 5  # 最新价 — market, taker slippage in the sim
    FIX = 11  # 指定价 — resting limit order, price used
    PEER = 14  # 对手价 — market, taker slippage in the sim

    MARKET = (LATEST, PEER)


# Agent action verb <-> (account, official opType). The opType alone determines
# the account, so ``passorder`` needs no account selector. ``close`` is a driver
# convenience with no official op: the engine resolves it to the holding
# account's sell/credit_sell/cover op at submission (same tick as matching, so
# no drift). ``transfer`` is a sim-only cash move between the two accounts.
_ACTION_TO_ACCOUNT_OP = {
    "buy": ("stock", optype.STOCK_BUY),
    "sell": ("stock", optype.STOCK_SELL),
    "credit_buy": ("credit", optype.CREDIT_BUY),
    "credit_sell": ("credit", optype.CREDIT_SELL),
    "fin_buy": ("credit", optype.FIN_BUY),
    "short": ("credit", optype.SLO_SELL),
    "cover": ("credit", optype.BUY_SECU_REPAY),
    "sell_repay": ("credit", optype.SELL_REPAY),
    "direct_repay": ("credit", optype.DIRECT_REPAY),
}
_OP_TO_ACTION = {op: action for action, (_, op) in _ACTION_TO_ACCOUNT_OP.items()}
_OP_TO_ACCOUNT = {op: account for _, (account, op) in _ACTION_TO_ACCOUNT_OP.items()}


@dataclass
class WorkingOrder:
    """A resting order in the day's book (``passorder`` semantics).

    A ``prtype.FIX`` (指定价) order fills without slippage when a bar reaches it,
    using a better open when the bar already crosses the limit; a market order
    (``prtype.LATEST``/``PEER``) fills at the bar open with taker slippage, except
    an auction order (``is_auction``), which clears at the single auction price
    with no slippage. ``remaining_bars`` is the time-in-force countdown; at expiry
    the order auto-cancels. A 融券卖出 order is checked against the 申报价 rule at
    its first match attempt (``uptick_checked``).
    """

    order_id: str
    action: str
    account: str
    op_type: int
    ts_code: str
    volume: int | None
    weight: float | None
    price_type: int
    price: float | None
    remaining_bars: int
    is_auction: bool
    reason: str
    submitted_at: str = ""
    # A close (15:00) call-auction order fills at the activation bar's CLOSE; an
    # open (09:25) auction or a continuous order fills at its bar OPEN.
    auction_close: bool = False
    uptick_checked: bool = False

    @property
    def is_limit(self) -> bool:
        return self.price_type == prtype.FIX

    def to_record(self) -> dict[str, object]:
        return {
            "order_id": self.order_id,
            "account": self.account,
            "op_type": self.op_type,
            "action": self.action,
            "ts_code": self.ts_code,
            "order_volume": self.volume,
            "weight": self.weight,
            "price_type": self.price_type,
            "price": self.price,
            "status": "working",
            "submitted_at": self.submitted_at,
            "remaining_bars": self.remaining_bars,
            "reason": self.reason,
        }


@dataclass
class AccountState:
    """One of the run's two accounts: its own cash, positions and (for the
    credit account) open debt contracts. T+1 locks live on the positions, so
    they are naturally per-account."""

    name: str  # "stock" | "credit"
    cash: float
    initial_equity: float
    positions: dict[str, Position] = field(default_factory=dict)
    contracts: list[DebtContract] = field(default_factory=list)

    def long_market_value(self) -> float:
        return sum(pos.market_value for pos in self.positions.values() if pos.side == "long")


class SimBroker:
    """Two-account (普通 + 信用) order/fill/position accounting driven only by
    structured primitives.

    The Agent strategy never writes fills, positions, or returns; the replay
    engine translates its verbs into ``passorder`` submissions and the Broker
    applies every market constraint and records the outcome. The opType routes
    each order to its account (module docstring).
    """

    def __init__(
        self,
        profile: BrokerProfile,
        market: MarketData,
        *,
        shortable_codes: frozenset[str],
        shortable_by_date: dict[str, frozenset[str]] | None = None,
    ) -> None:
        self.profile = profile
        self.market = market
        # Frozen decision-day margin_secs set (the agent's snapshot view), used as the
        # fallback when a fill day is absent from the per-day map below. margin_secs
        # carries no 融资/融券 split, so the same set gates both fin_buy eligibility
        # and short inventory (documented approximation).
        self.shortable_codes = shortable_codes
        # Per-fill-day margin_secs sets from the replay slot. The credit-target gate
        # consults the FILL day's real set (current_date advances to the fill day before
        # the check), so the broker constraint reflects same-day eligibility and stays
        # independent of the agent's frozen, decision-day snapshot.
        self.shortable_by_date = dict(shortable_by_date or {})
        self.accounts: dict[str, AccountState] = {
            "stock": AccountState("stock", float(profile.stock_initial_cash), float(profile.stock_initial_cash)),
            "credit": AccountState("credit", float(profile.credit_initial_cash), float(profile.credit_initial_cash)),
        }
        self.initial_equity = float(profile.stock_initial_cash + profile.credit_initial_cash)
        self.orders: list[Order] = []
        self._book: list[WorkingOrder] = []  # resting (working) orders for the day
        self._order_seq = 0
        self._compact_seq = 0
        self.events: list[dict[str, object]] = []
        self.trade_ledger: dict[str, list[dict[str, object]]] = {}
        self.fees_paid = 0.0
        self.stamp_duty_paid = 0.0
        self.interest_accrued_total = 0.0
        self.interest_paid_total = 0.0
        self.traded_notional = 0.0
        self.reject_counts: dict[str, int] = {}
        self.current_date = ""

    @property
    def stock(self) -> AccountState:
        return self.accounts["stock"]

    @property
    def credit(self) -> AccountState:
        return self.accounts["credit"]

    def account_op_for_action(self, action: str) -> tuple[str, int]:
        """The (account, official opType) pair for an agent verb; raises
        ValueError for unknown verbs (``close``/``transfer`` are resolved by the
        engine before reaching an order)."""
        pair = _ACTION_TO_ACCOUNT_OP.get(str(action))
        if pair is None:
            raise ValueError(f"unknown broker action {action!r}")
        return pair

    # ---- official-API queries (docs/environment_design.md §3.2) ----

    def get_trade_detail_data(
        self,
        account_id: str = "",
        account_type: str = "",
        data_type: str = "ORDER",
        strategy_name: str = "",
    ) -> list[dict[str, object]]:
        """ACCOUNT / POSITION / ORDER / DEAL records (QMT ``get_trade_detail_data``).

        Two accounts exist, so ``account_type`` is required ("STOCK" or "CREDIT");
        ``account_id`` is accepted and ignored (the sim runs one account per
        type). ORDER returns the day's working book plus the cumulative
        settled/rejected orders for the whole replay (live QMT returns only the
        current day; the sim keeps the history because report/stats consumers
        aggregate the whole backtest)."""
        account = self._normalize_account(account_type)
        if strategy_name:
            raise ValueError("the sim does not tag orders by strategy_name; filter by remark/order_id instead")
        kind = str(data_type or "").upper()
        if kind == "ACCOUNT":
            return [self._account_record(account)]
        if kind == "POSITION":
            return [
                {
                    "account": account,
                    "ts_code": pos.ts_code,
                    "side": pos.side,
                    "quantity": pos.quantity,
                    "sellable_quantity": pos.sellable_quantity,
                    "entry_price": pos.entry_price,
                    "entry_date": pos.entry_date,
                    # Cost basis (a short's locked net proceeds) so the sandbox view can
                    # see the buying power released on a cover; maps to QMT m_dOpenCost.
                    "entry_cost": pos.entry_cost,
                    "last_price": pos.last_price,
                    "market_value": pos.market_value,
                }
                for pos in self.accounts[account].positions.values()
            ]
        if kind == "ORDER":
            return [order.to_record() for order in self._book if order.account == account] + [
                order.to_record() for order in self.orders if order.account == account
            ]
        if kind == "DEAL":
            return [
                trade
                for trades in self.trade_ledger.values()
                for trade in trades
                if trade.get("account") == account
            ]
        raise ValueError(f"unsupported data_type={data_type!r} (ACCOUNT/POSITION/ORDER/DEAL)")

    def get_debt_contract(self, account_id: str = "") -> list[dict[str, object]]:
        """Open 负债合约 records (QMT ``get_debt_contract``; credit account)."""
        return [contract.to_record() for contract in self.credit.contracts if not contract.closed]

    def get_assure_contract(self, account_id: str = "") -> list[dict[str, object]]:
        """担保标的 terms for credit-account holdings (QMT ``get_assure_contract``).
        The sim applies flat profile ratios to every code, so this projects them."""
        return [
            {
                "ts_code": pos.ts_code,
                "assure_ratio": self.profile.assure_ratio,
                "fin_ratio": self.profile.fin_margin_ratio,
                "slo_ratio": self.profile.effective_slo_margin_ratio,
                "fin_rate_annual": self.profile.fin_rate_annual,
                "slo_rate_annual": self.profile.slo_rate_annual,
            }
            for pos in self.credit.positions.values()
        ]

    def get_enable_short_contract(self, account_id: str = "") -> list[dict[str, object]]:
        """当日可融券标的 (QMT ``get_enable_short_contract``): the fill-day margin_secs
        set. Quantities are unknown in proxy mode, so records carry eligibility only."""
        shortable = self.shortable_by_date.get(self.current_date, self.shortable_codes)
        return [
            {"ts_code": code, "slo_ratio": self.profile.effective_slo_margin_ratio, "slo_status": "normal"}
            for code in sorted(shortable)
        ]

    @staticmethod
    def _normalize_account(account: str) -> str:
        name = str(account or "").strip().lower()
        if name not in ACCOUNT_TYPES:
            raise ValueError(f"account_type is required and must be STOCK or CREDIT, got {account!r}")
        return name

    def _account_record(self, account: str) -> dict[str, object]:
        state = self.accounts[account]
        record: dict[str, object] = {
            "account_type": account.upper(),
            "cash": state.cash,
            "available_cash": self.available_cash(account),
            "total_assets": self.account_equity(account),
            "market_value": state.long_market_value(),
        }
        if account == "credit":
            fin_amount = self._fin_amount_outstanding()
            record.update(
                {
                    "maintenance_ratio": self.maintenance_ratio(),
                    "enable_bail_balance": self.enable_bail_balance(),
                    "fin_debt": fin_amount,
                    "slo_debt": self._slo_mv_outstanding(),
                    "interest_accrued": self._interest_outstanding(),
                    "fin_quota_used": fin_amount,
                    "fin_quota_max": self.profile.fin_max_quota,
                    "slo_quota_used": self._slo_sell_amount_outstanding(),
                    "slo_quota_max": self.profile.slo_max_quota,
                    "fin_rate_annual": self.profile.fin_rate_annual,
                    "slo_rate_annual": self.profile.slo_rate_annual,
                }
            )
        return record

    # ---- order book (passorder lifecycle) ----

    def passorder(
        self,
        op_type: int,
        order_type: int,
        account_id: str,
        order_code: str,
        pr_type: int,
        price: float | None,
        volume: int | float | None,
        *,
        user_order_id: str = "",
        weight: float | None = None,
        valid_bars: int = 1,
        is_auction: bool = False,
        auction_close: bool = False,
        reason: str = "",
        submitted_at: str = "",
    ) -> str:
        """Submit an order by official opType and return its order id.

        Mirrors QMT ``passorder`` with sim conveniences: the returned id is what
        the official flow recovers via ``get_last_order_id`` right after the call
        (``user_order_id`` doubles as the id/投资备注 when given, so the agent's
        client id is the correlation key, as live remarks are). The opType alone
        selects the account (23/24 普通, 27–34 信用). ``weight``, ``valid_bars``,
        the auction flags, ``reason`` and ``submitted_at`` are backtest
        conveniences a live adapter resolves before its own passorder. Only
        ``order_type`` 1101 (单股/股) is supported — except 直接还款, which
        follows the official 1102 (金额元) convention and settles immediately."""
        op_type = int(op_type)
        action = _OP_TO_ACTION.get(op_type)
        if action is None:
            raise ValueError(f"unsupported opType={op_type} (股票 23/24; 信用 27/28/29/31/32/33/34)")
        account = _OP_TO_ACCOUNT[op_type]
        self._order_seq += 1
        order_id = str(user_order_id or ("O%06d" % self._order_seq))
        if op_type == optype.DIRECT_REPAY:
            if int(order_type) != 1102:
                raise ValueError("直接还款 requires orderType=1102 (amount in CNY)")
            return self._direct_repay(
                float(volume or 0), order_id=order_id, reason=reason, submitted_at=submitted_at
            )
        if int(order_type) != 1101:
            raise ValueError(f"unsupported orderType={order_type} (single stock by shares = 1101)")
        pr_type = int(pr_type)
        if pr_type not in (prtype.FIX, *prtype.MARKET):
            raise ValueError(f"unsupported prType={pr_type} (11 指定价 / 5 最新价 / 14 对手价)")
        is_limit = pr_type == prtype.FIX
        if is_limit and not (price and float(price) > 0):
            raise ValueError("prType=11 (指定价) requires a positive price")
        if op_type == optype.SLO_SELL and not is_limit:
            # 融券卖出申报价不得低于最新成交价 (实施细则), so it must be a limit order.
            order = Order(
                ts_code=str(order_code), action="short", side="short",
                requested_amount=int(volume or 0), trade_date=self.current_date,
                decision_time=str(submitted_at or ""), reason=str(reason or "short"),
                account="credit", op_type=op_type, order_id=order_id,
            )
            self.orders.append(order)
            self._reject(order, "slo_sell_requires_limit_price")
            return order_id
        self._book.append(
            WorkingOrder(
                order_id=order_id,
                action=action,
                account=account,
                op_type=op_type,
                ts_code=str(order_code),
                volume=int(volume) if volume else None,
                weight=weight,
                price_type=pr_type,
                price=float(price) if is_limit else None,
                remaining_bars=max(1, int(valid_bars or 1)),
                is_auction=bool(is_auction),
                auction_close=bool(auction_close),
                reason=str(reason or action),
                submitted_at=str(submitted_at or ""),
            )
        )
        return order_id

    def transfer(
        self,
        amount: float,
        from_account: str,
        to_account: str,
        *,
        reason: str = "",
        order_id: str | None = None,
        submitted_at: str = "",
    ) -> Order:
        """Move cash between the two accounts (银证转账-style; sim extension, a
        manual operation in live trading). Outbound credit transfers require the
        post-transfer 维持担保比例 to stay at or above
        ``maintenance_withdraw_ratio`` while credit debt is outstanding; locked
        融券 proceeds are never transferable (available_cash gate)."""
        src = self._normalize_account(from_account)
        dst = self._normalize_account(to_account)
        amount = float(amount)
        order = Order(
            ts_code="", action="transfer", side="long", requested_amount=int(amount),
            trade_date=self.current_date, decision_time=str(submitted_at or ""),
            reason=str(reason or f"transfer_{src}_to_{dst}"), price_label="cash_op",
            account=src, **({"order_id": order_id} if order_id else {}),
        )
        self.orders.append(order)
        if src == dst:
            return self._reject(order, "transfer_same_account")
        if amount <= 0:
            return self._reject(order, "transfer_amount_not_positive")
        if amount > self.available_cash(src) + 1e-6:
            return self._reject(order, "insufficient_cash")
        if src == "credit":
            debt = self._fin_amount_outstanding() + self._slo_mv_outstanding() + self._interest_outstanding()
            if debt > 1e-9:
                post_ratio = (self.credit.cash - amount + self.credit.long_market_value()) / debt
                if post_ratio < self.profile.maintenance_withdraw_ratio:
                    return self._reject(order, "credit_withdraw_blocked_by_maintenance")
        self.accounts[src].cash -= amount
        self.accounts[dst].cash += amount
        order.status = "filled"
        order.filled_quantity = int(amount)
        self._event(
            "cash_transferred", trade_date=self.current_date, order_id=order.order_id,
            from_account=src, to_account=dst, amount=amount,
        )
        return order

    def working_orders(self) -> list[dict[str, object]]:
        """The day's still-cancelable book — a sim convenience for the replay
        engine's pending view and day-end auto-cancel (a live loop filters ORDER
        records by status instead)."""
        return [order.to_record() for order in self._book]

    def cancel(
        self,
        order_id: str,
        account_id: str = "",
        account_type: str = "",
        *,
        reason: str = "cancelled",
        trade_date: str | None = None,
        minute_key: str | None = None,
    ) -> bool:
        """Cancel a working order by id (QMT ``cancel``); the audit kwargs are sim
        extensions used by the replay engine's cancel events. Order ids are unique
        across both accounts, so ``account_type`` is validated when given but not
        needed for the lookup."""
        if account_type:
            self._normalize_account(account_type)
        for index, order in enumerate(self._book):
            if order.order_id == order_id:
                self._book.pop(index)
                payload = {
                    "trade_date": str(trade_date or self.current_date),
                    "ts_code": order.ts_code,
                    "order_id": order_id,
                    "reason": reason,
                }
                if minute_key:
                    payload["minute_key"] = str(minute_key)
                self._event("order_cancelled", **payload)
                return True
        return False

    def match_bar(self, trade_date: str, minute_key: str, minute_group: pd.DataFrame, granularity: str = "minute") -> None:
        """Match working orders against this bar (the simulated exchange).

        Fillable orders settle via :meth:`execute` (still subject to cash, T+1, lot,
        price-limit, suspension and short-inventory rejects); a limit order the bar
        did not reach decrements its time-in-force and rests, then auto-cancels."""
        survivors: list[WorkingOrder] = []
        for order in self._book:
            bar = _bar_for_code(minute_group, order.ts_code)
            if order.action == "short" and not order.uptick_checked and bar is not None:
                # 融券卖出申报价不得低于最新成交价: checked once, when the order first
                # reaches the exchange (its activation bar). An aggressive limit below
                # the reference price would have been rejected at 申报.
                order.uptick_checked = True
                ref_price = _close_price(bar) if order.auction_close else _open_price(bar)
                if order.price is not None and ref_price is not None and order.price < ref_price:
                    rejected = Order(
                        ts_code=order.ts_code, action="short", side="short",
                        requested_amount=int(order.volume or 0), trade_date=str(trade_date),
                        decision_time=str(minute_key), reason=order.reason,
                        op_type=order.op_type, order_id=order.order_id,
                    )
                    self.orders.append(rejected)
                    self._reject(rejected, "slo_sell_uptick_rule")
                    continue
            price = _limit_fill_price(order, bar, use_close=order.auction_close) if bar is not None else None
            if price is not None:
                self.execute(
                    order.ts_code,
                    order.action,
                    trade_date=trade_date,
                    raw_price=price,
                    amount=order.volume,
                    weight=order.weight,
                    time=minute_key,
                    reason=order.reason,
                    price_label="auction" if order.is_auction else f"{granularity}:{minute_key}",
                    # A call auction (open 09:25 / close 15:00) clears every order at one
                    # uniform price, so it carries no taker spread; only continuous-session
                    # market orders take slippage. Limit orders never take slippage.
                    apply_slippage=not order.is_limit and not order.is_auction,
                    order_id=order.order_id,
                )
            elif order.remaining_bars <= 1:
                self._event(
                    "order_cancelled", trade_date=trade_date, minute_key=minute_key,
                    ts_code=order.ts_code, order_id=order.order_id, reason="expired_unfilled",
                )
            else:
                order.remaining_bars -= 1
                survivors.append(order)
        self._book = survivors

    def position_quantity(self, ts_code: str, account: str | None = None) -> int:
        """Signed share count (long positive, short negative, flat zero); the
        default sums both accounts, so a stock-account long hedged by a
        credit-account short can net to zero while both legs exist."""
        names = ACCOUNT_TYPES if account is None else (self._normalize_account(account),)
        total = 0
        for name in names:
            pos = self.accounts[name].positions.get(str(ts_code))
            if pos is not None:
                total += pos.quantity if pos.side == "long" else -pos.quantity
        return total

    def record_event(self, event_type: str, **payload: object) -> None:
        """Append an audited replay event from trusted Environment code."""
        self._event(event_type, **payload)

    def roll_to_date(self, trade_date: str) -> None:
        """Lift the T+1 lock for every position when the sim-date rolls to a new day.

        Runs the same ``locked_date < trade_date`` unlock as :meth:`_advance_date`
        for ALL positions, but is called once by the host at the START of each new
        trade date — before the day's first ``ctx``/tick is built — so an overnight
        hold reports its full ``sellable_quantity`` from the day's first off-session
        tick rather than only after that day's first fill. Idempotent and
        deterministic: ``execute``/``mark_to_market`` still call ``_advance_date`` as
        a safety net, and re-rolling to the same date is a no-op."""
        self._advance_date(trade_date)

    # ---- fundamental primitives ----

    def execute(
        self,
        ts_code: str,
        action: str,
        *,
        trade_date: str,
        raw_price: float | None,
        amount: int | None = None,
        weight: float | None = None,
        time: str = "",
        reason: str = "",
        source_artifacts: list[str] | None = None,
        price_label: str = "price",
        apply_slippage: bool = True,
        order_id: str | None = None,
    ) -> Order:
        """Apply one strategy primitive at the current bar with full constraints.

        ``action`` is ``buy``/``sell`` (stock account), ``credit_buy``/
        ``credit_sell``/``short``/``cover``/``fin_buy``/``sell_repay`` (credit
        account); 直接还款 and ``transfer`` settle without a bar via
        :meth:`passorder`/:meth:`transfer`, and ``close`` is resolved by the
        engine before submission. ``amount`` is a share count (lot-aligned);
        ``weight`` is a fraction of the TARGET ACCOUNT's initial equity, used
        when ``amount`` is absent. ``apply_slippage`` is True for marketable
        (taker) fills and False for limit fills, where ``raw_price`` is the
        no-slippage limit-fill price (limit or better open). ``order_id``
        carries the originating working order's id onto the fill.
        """
        self._advance_date(trade_date)
        action = str(action).lower().strip()
        pair = _ACTION_TO_ACCOUNT_OP.get(action)
        side = "short" if action in {"short", "cover"} else "long"
        order = Order(
            ts_code=str(ts_code),
            action=action,
            side=side,
            requested_amount=int(amount) if amount is not None else 0,
            trade_date=str(trade_date),
            decision_time=str(time),
            reason=str(reason or action),
            **({"order_id": order_id} if order_id else {}),
            source_artifacts=list(source_artifacts or []),
            price_label=price_label,
            account=pair[0] if pair else "",
            op_type=pair[1] if pair else None,
        )
        self.orders.append(order)

        if pair is None:
            return self._reject(order, f"unsupported_action:{action}")
        state = self.accounts[pair[0]]
        bar = self.market.bar(trade_date, ts_code)
        if MarketData.is_suspended(bar):
            return self._reject(order, "suspended")
        if raw_price is None or pd.isna(raw_price):
            return self._reject(order, "missing_price")
        raw_price = float(raw_price)

        if action in {"buy", "credit_buy", "short", "fin_buy"}:
            return self._open(order, state, bar, raw_price, amount=amount, weight=weight, apply_slippage=apply_slippage)
        return self._reduce(order, state, bar, raw_price, amount=amount, apply_slippage=apply_slippage)

    def _open(self, order: Order, state: AccountState, bar: pd.Series, raw_price: float, *, amount, weight, apply_slippage: bool = True) -> Order:
        shares = resolve_shares(amount, weight, raw_price, state.initial_equity)
        if shares <= 0:
            return self._reject(order, "amount_below_lot_size")
        pos = state.positions.get(order.ts_code)
        if pos is not None and pos.side != order.side:
            return self._reject(order, "opposite_side_position_open")
        if (
            self.profile.max_total_holdings is not None
            and pos is None
            and len(self._held_codes()) >= self.profile.max_total_holdings
            and order.ts_code not in self._held_codes()
        ):
            return self._reject(order, "max_holdings_reached")
        shares = self._cap_single_name(order.ts_code, shares, raw_price)
        if shares <= 0:
            return self._reject(order, "single_name_weight_cap")
        if order.action == "short":
            inventory_reject = self._short_inventory_reject(order.ts_code)
            if inventory_reject is not None:
                return self._reject(order, inventory_reject)
            if MarketData.limit_down_blocked_at_price(bar, raw_price):
                return self._reject(order, "limit_down_blocked_short")
            return self._fill_short_open(order, state, raw_price, shares, apply_slippage)
        if MarketData.limit_up_blocked_at_price(bar, raw_price):
            return self._reject(order, "limit_up_blocked_buy")
        if order.action == "fin_buy":
            if self._credit_target_reject(order.ts_code):
                return self._reject(order, "margin_secs_not_finable")
            return self._fill_fin_open(order, state, raw_price, shares, apply_slippage)
        return self._fill_long_open(order, state, raw_price, shares, apply_slippage)

    def _fill_long_open(self, order: Order, state: AccountState, raw_price: float, shares: int, apply_slippage: bool = True) -> Order:
        fill = project_open(
            self.profile.cost_model, side="long", raw_price=raw_price, shares=shares,
            trade_date=order.trade_date, apply_slippage=apply_slippage,
        )
        # A cash/担保品 buy may only deploy the account's available cash: 融券
        # proceeds are locked collateral in the credit account, and the two
        # accounts' cash pools never back each other's orders.
        if fill.required_cash > self.available_cash(state.name) + 1e-6:
            return self._reject(order, "insufficient_cash")
        state.cash += fill.cash_delta
        self.fees_paid += fill.fee
        self._add_to_position(state, order.ts_code, "long", shares, fill.price, fill.cost_basis, order.trade_date)
        return self._fill(order, fill.price, shares, "open")

    def _fill_fin_open(self, order: Order, state: AccountState, raw_price: float, shares: int, apply_slippage: bool = True) -> Order:
        """融资买入: no cash moves; notional+fee become a fin contract's principal,
        gated on 保证金可用余额 and the 融资 quota."""
        fill = project_open(
            self.profile.cost_model, side="long", raw_price=raw_price, shares=shares,
            trade_date=order.trade_date, apply_slippage=apply_slippage, financed=True,
        )
        # Opening the contract moves the bail balance by the financed fee (booked at
        # 100% as an immediate 浮亏) plus the margin occupied by the new principal.
        required_bail = fill.cost_basis * self.profile.fin_margin_ratio + fill.fee
        if required_bail > self.enable_bail_balance() + 1e-6:
            return self._reject(order, "insufficient_bail_balance")
        if (
            self.profile.fin_max_quota is not None
            and self._fin_amount_outstanding() + fill.cost_basis > self.profile.fin_max_quota + 1e-6
        ):
            return self._reject(order, "fin_quota_exceeded")
        self.fees_paid += fill.fee  # financed into the contract, counted as cost incurred
        self._add_to_position(state, order.ts_code, "long", shares, fill.price, fill.cost_basis, order.trade_date)
        state.contracts.append(
            self._new_contract(
                kind="fin", ts_code=order.ts_code, trade_date=order.trade_date,
                open_price=fill.price, principal=fill.cost_basis, shares=shares,
            )
        )
        return self._fill(order, fill.price, shares, "open")

    def _fill_short_open(self, order: Order, state: AccountState, raw_price: float, shares: int, apply_slippage: bool = True) -> Order:
        fill = project_open(
            self.profile.cost_model, side="short", raw_price=raw_price, shares=shares,
            trade_date=order.trade_date, apply_slippage=apply_slippage,
        )
        # 保证金可用余额 must back the new short's margin plus its open costs
        # (the bail balance moves by -margin - fee - duty when the short opens).
        if fill.required_cash > self.enable_bail_balance() + 1e-6:
            return self._reject(order, "insufficient_bail_balance")
        if (
            self.profile.slo_max_quota is not None
            and self._slo_sell_amount_outstanding() + fill.notional > self.profile.slo_max_quota + 1e-6
        ):
            return self._reject(order, "slo_quota_exceeded")
        state.cash += fill.cash_delta
        self.fees_paid += fill.fee
        self.stamp_duty_paid += fill.duty
        # entry_cost for a short is the net sale proceeds released proportionally on cover.
        self._add_to_position(state, order.ts_code, "short", shares, fill.price, fill.cost_basis, order.trade_date)
        state.contracts.append(
            self._new_contract(
                kind="slo", ts_code=order.ts_code, trade_date=order.trade_date,
                open_price=fill.price, shares=shares, sell_amount=fill.notional,
            )
        )
        return self._fill(order, fill.price, shares, "open")

    def _reduce(self, order: Order, state: AccountState, bar: pd.Series, raw_price: float, *, amount, apply_slippage: bool = True) -> Order:
        pos = state.positions.get(order.ts_code)
        want_side = {"cover": "short", "sell": "long", "credit_sell": "long", "sell_repay": "long"}.get(order.action)
        if pos is None:
            return self._reject(order, "no_position")
        if want_side is not None and pos.side != want_side:
            return self._reject(order, f"side_mismatch:{order.action}:{pos.side}")
        if order.action == "sell_repay" and self._fin_amount_outstanding() <= 1e-9:
            return self._reject(order, "no_fin_debt")
        order.side = pos.side
        sellable = pos.sellable_quantity
        if sellable <= 0:
            return self._reject(order, "t_plus_one_no_sellable")
        shares = sellable if amount is None else min(self._lot_floor(amount), sellable)
        if shares <= 0:
            return self._reject(order, "amount_below_lot_size")
        is_buy = pos.side == "short"  # covering a short is a buy
        if pos.side == "long" and MarketData.limit_down_blocked_at_price(bar, raw_price):
            return self._reject(order, "limit_down_blocked_sell")
        if pos.side == "short" and MarketData.limit_up_blocked_at_price(bar, raw_price):
            return self._reject(order, "limit_up_blocked_cover")
        price = self.profile.slipped_price(raw_price, is_buy=is_buy) if apply_slippage else raw_price
        fill = self._reduce_position(state, pos, shares, price, order.trade_date)
        if order.action == "sell_repay" and fill is not None:
            # 卖券还款: the sold shares come off the code's fin contracts, and the net
            # proceeds (already banked by _reduce_position) repay 融资 debt interest-
            # first FIFO; any surplus simply stays in cash.
            release_fin_shares(state.contracts, order.ts_code, shares)
            repaid = repay_fin(state.contracts, max(0.0, fill.cash_delta), release_shares=False)
            state.cash -= repaid["applied"]
            self.interest_paid_total += repaid["interest_paid"]
            if repaid["applied"] > 0:
                self._event(
                    "debt_repaid", trade_date=order.trade_date, ts_code=order.ts_code,
                    kind="fin", via="sell_repay", order_id=order.order_id, **repaid,
                )
        return self._fill(order, price, shares, "close" if order.ts_code not in state.positions else "reduce")

    # ---- replay lifecycle ----

    def mark_to_market(self, trade_date: str) -> float:
        self._advance_date(trade_date)
        for state in self.accounts.values():
            for pos in state.positions.values():
                bar = self.market.bar(trade_date, pos.ts_code)
                if bar is not None and pd.notna(bar.get("close")):
                    pos.last_price = float(bar["close"])
        if self.credit.contracts:
            # 融资利息/融券费 accrue every CALENDAR day (weekends/holidays included)
            # into each contract and are paid from cash at repayment; both credit
            # formulas count the accrued balance meanwhile.
            self.interest_accrued_total += accrue_debt_interest(self.credit.contracts, trade_date)
        ratio = self.maintenance_ratio()
        if ratio is not None and ratio < self.profile.maintenance_closeout_ratio:
            # A maintenance breach liquidates the CREDIT account only — the stock
            # account is not collateral and is untouched by the forced close.
            self._event("forced_close_triggered", trade_date=trade_date, maintenance_ratio=ratio)
            self.close_all(trade_date, forced=True, account="credit")
        return self.equity()

    def close_all(self, trade_date: str, *, forced: bool = False, account: str | None = None) -> None:
        """Liquidate all sellable shares at the day's close (mandatory exit);
        ``account=None`` closes both accounts."""
        names = ACCOUNT_TYPES if account is None else (self._normalize_account(account),)
        for name in names:
            for ts_code in list(self.accounts[name].positions):
                self.close_position(ts_code, trade_date, forced=forced, account=name)

    def close_position(self, ts_code: str, trade_date: str, *, account: str, forced: bool = False) -> bool:
        """Close one account position's sellable shares at the day's close price."""
        self._advance_date(trade_date)
        state = self.accounts[self._normalize_account(account)]
        pos = state.positions.get(ts_code)
        if pos is None:
            return False
        sellable = pos.sellable_quantity
        if sellable <= 0:
            self._event("exit_blocked_t_plus_one", ts_code=ts_code, side=pos.side, trade_date=trade_date, forced=forced)
            return False
        bar = self.market.bar(trade_date, ts_code)
        if MarketData.is_suspended(bar):
            self._event("exit_blocked_suspended", ts_code=ts_code, side=pos.side, trade_date=trade_date, forced=forced)
            return False
        raw_price = bar.get("close")
        if pd.isna(raw_price):
            self._event("exit_blocked_missing_price", ts_code=ts_code, side=pos.side, trade_date=trade_date, forced=forced)
            return False
        raw_price = float(raw_price)
        if pos.side == "long" and MarketData.limit_down_blocked_at_price(bar, raw_price):
            self._event("exit_blocked_limit_down", ts_code=ts_code, side=pos.side, trade_date=trade_date, forced=forced)
            return False
        if pos.side == "short" and MarketData.limit_up_blocked_at_price(bar, raw_price):
            self._event("exit_blocked_limit_up", ts_code=ts_code, side=pos.side, trade_date=trade_date, forced=forced)
            return False
        price = self.profile.slipped_price(raw_price, is_buy=pos.side == "short")
        self._reduce_position(state, pos, sellable, price, trade_date, forced=forced, price_label="close")
        return True

    def account_equity(self, account: str) -> float:
        """One account's net assets. Stock: cash + long market value. Credit:
        additionally nets the 融券 liability (marked borrowed shares), 融资
        principal, and accrued unpaid interest — open debt is netted rather than
        force-settled, so liquidation leaves equity intact."""
        state = self.accounts[self._normalize_account(account)]
        value = state.cash + state.long_market_value()
        if state.name == "credit":
            value -= self._slo_mv_outstanding() + self._fin_amount_outstanding() + self._interest_outstanding()
        return value

    def equity(self) -> float:
        """Combined net assets across the stock and credit accounts."""
        return self.account_equity("stock") + self.account_equity("credit")

    def maintenance_ratio(self) -> float | None:
        """维持担保比例 over CREDIT-ACCOUNT assets only (the stock account is not
        collateral); None with no credit debt."""
        return credit_maintenance_ratio(
            self.credit.cash,
            self.credit.long_market_value(),
            self._fin_amount_outstanding(),
            self._slo_mv_outstanding(),
            self._interest_outstanding(),
        )

    def enable_bail_balance(self) -> float:
        """保证金可用余额 per the 实施细则 formula (broker_core docstring),
        computed over the credit account only."""
        state = self.credit
        last_price = {pos.ts_code: pos.last_price for pos in state.positions.values()}
        long_qty = {
            pos.ts_code: pos.quantity for pos in state.positions.values() if pos.side == "long"
        }
        fin_terms: list[tuple[float, float]] = []
        for contract in state.contracts:
            if contract.kind != "fin" or contract.closed:
                continue
            # Financed shares can be sold via plain 担保品卖出 without repaying; the
            # attributed market value is clamped to what is still held, so a sold-out
            # contract books its full principal as 浮亏 (at 100%) until repaid.
            held = long_qty.get(contract.ts_code, 0)
            attributed = min(contract.shares, held)
            long_qty[contract.ts_code] = held - attributed
            fin_terms.append(
                (attributed * last_price.get(contract.ts_code, contract.open_price), contract.principal)
            )
        collateral_mv = sum(qty * last_price[code] for code, qty in long_qty.items())
        slo_terms = [
            (contract.sell_amount, contract.shares * last_price.get(contract.ts_code, contract.open_price))
            for contract in state.contracts
            if contract.kind == "slo" and not contract.closed
        ]
        return enable_bail_balance(
            state.cash,
            collateral_mv,
            fin_terms,
            slo_terms,
            self._interest_outstanding(),
            assure_ratio=self.profile.assure_ratio,
            fin_margin_ratio=self.profile.fin_margin_ratio,
            slo_margin_ratio=self.profile.effective_slo_margin_ratio,
        )

    # ---- internals ----

    def _advance_date(self, trade_date: str) -> None:
        trade_date = str(trade_date)
        for state in self.accounts.values():
            for pos in state.positions.values():
                if pos.locked_date and trade_date > pos.locked_date:
                    pos.locked_today = 0
                    pos.locked_date = trade_date
        if trade_date > self.current_date:
            self.current_date = trade_date

    def _add_to_position(
        self,
        state: AccountState,
        ts_code: str,
        side: str,
        shares: int,
        price: float,
        cash_basis: float,
        trade_date: str,
    ) -> Position:
        self.traded_notional += shares * price
        pos = state.positions.get(ts_code)
        # T+1 lock bookkeeping is long-only: a short's sellable_quantity ignores
        # locked_today (融券 permits same-day cover), so shorts leave it at its default.
        is_long = side == "long"
        if pos is None:
            pos = Position(
                ts_code=ts_code,
                side=side,
                quantity=shares,
                entry_price=price,
                entry_date=trade_date,
                entry_cost=cash_basis,
                last_price=price,
                locked_today=shares if is_long else 0,
                locked_date=trade_date if is_long else "",
            )
            state.positions[ts_code] = pos
            return pos
        pos.entry_price = (pos.entry_price * pos.quantity + price * shares) / (pos.quantity + shares)
        pos.quantity += shares
        pos.entry_cost += cash_basis
        if is_long:
            pos.locked_today += shares
            pos.locked_date = trade_date
        pos.last_price = price
        return pos

    def _reduce_position(
        self,
        state: AccountState,
        pos: Position,
        shares: int,
        price: float,
        trade_date: str,
        *,
        forced: bool = False,
        price_label: str = "price",
    ):
        shares = min(shares, pos.sellable_quantity)
        if shares <= 0:
            return None
        self.traded_notional += shares * price
        # ``price`` is already the fill price (slipped by the caller), so the shared
        # reduce projection runs with apply_slippage=False.
        fill = project_reduce(
            self.profile.cost_model, side=pos.side, raw_price=price, shares=shares,
            trade_date=trade_date, apply_slippage=False,
        )
        basis_released = pos.entry_cost * shares / pos.quantity
        # covering a short releases the net proceeds banked at open (basis_released).
        state.cash += fill.cash_delta
        self.stamp_duty_paid += fill.duty
        self.fees_paid += fill.fee
        if pos.side == "short":
            # Any short reduce is 买券还券 (or forced/mandatory liquidation): the
            # covered shares repay the code's 融券 contracts FIFO and the repaid
            # fraction's accrued interest falls due from cash now.
            repaid = repay_slo(state.contracts, pos.ts_code, shares)
            state.cash -= repaid["interest_due"]
            self.interest_paid_total += repaid["interest_due"]
        realized = (
            fill.cash_delta - basis_released if pos.side == "long" else basis_released + fill.cash_delta
        )
        pos.quantity -= shares
        pos.entry_cost -= basis_released
        pos.last_price = price
        if pos.side == "long":  # short T+1 lock state is never maintained (see _add_to_position)
            pos.locked_today = min(pos.locked_today, pos.quantity)
        self._ledger(
            pos.ts_code,
            account=state.name,
            side=pos.side,
            kind="reduce" if pos.quantity > 0 else "close",
            price=price,
            quantity=shares,
            trade_date=trade_date,
            realized_pnl=realized,
        )
        if pos.quantity <= 0:
            self._event(
                "position_closed",
                ts_code=pos.ts_code,
                account=state.name,
                trade_date=trade_date,
                side=pos.side,
                price=price,
                quantity=shares,
                realized_pnl=realized,
                forced=forced,
                price_label=price_label,
            )
            del state.positions[pos.ts_code]
        else:
            self._event(
                "position_reduced",
                ts_code=pos.ts_code,
                account=state.name,
                trade_date=trade_date,
                side=pos.side,
                price=price,
                quantity=shares,
                realized_pnl=realized,
                forced=forced,
                price_label=price_label,
            )
        return fill

    def _fill(self, order: Order, price: float, shares: int, kind: str) -> Order:
        order.status = "filled"
        order.price = price
        order.filled_quantity = shares
        if kind == "open":
            self._ledger(
                order.ts_code, account=order.account, side=order.side, kind="open",
                price=price, quantity=shares, trade_date=order.trade_date,
            )
        self._event(
            "order_filled",
            order_id=order.order_id,
            ts_code=order.ts_code,
            account=order.account,
            action=order.action,
            side=order.side,
            price=price,
            quantity=shares,
            price_label=order.price_label,
        )
        return order

    def _reject(self, order: Order, reason: str) -> Order:
        order.status = "rejected"
        order.reject_reason = reason
        self.reject_counts[reason] = self.reject_counts.get(reason, 0) + 1
        self._event("order_rejected", order_id=order.order_id, ts_code=order.ts_code, action=order.action, reason=reason)
        return order

    def _short_inventory_reject(self, ts_code: str) -> str | None:
        mode = self.profile.short_inventory_mode
        if mode == "proxy_margin_secs":
            shortable = self.shortable_by_date.get(self.current_date, self.shortable_codes)
            return None if ts_code in shortable else "margin_secs_not_shortable"
        if mode == "broker_inventory":
            # Real CITIC inventory/fee files are not wired yet; without them the
            # mode must reject rather than silently assume borrow availability.
            return "broker_inventory_unavailable"
        return None  # theoretical_short: explicit research mode, no inventory gate

    def _held_codes(self) -> set[str]:
        """Distinct codes held across BOTH accounts (the portfolio-breadth base
        for ``max_total_holdings``)."""
        return {code for state in self.accounts.values() for code in state.positions}

    def _cap_single_name(self, ts_code: str, shares: int, raw_price: float) -> int:
        """Clamp an opening order so the code's COMBINED notional across both
        accounts stays within the single-name weight cap over combined initial
        equity (rounded down to whole lots)."""
        if self.profile.max_single_name_weight is None:
            return shares
        if raw_price <= 0:
            return 0
        cap_notional = self.profile.max_single_name_weight * self.initial_equity
        held_notional = sum(
            pos.quantity * raw_price
            for state in self.accounts.values()
            if (pos := state.positions.get(ts_code)) is not None
        )
        budget_shares = self._lot_floor((cap_notional - held_notional) / raw_price)
        return min(shares, budget_shares)

    @staticmethod
    def _lot_floor(amount: object) -> int:
        return lot_floor(amount)

    def _short_proceeds_locked(self) -> float:
        """Net short-sale proceeds held as locked collateral (融券卖出所得资金) in
        the credit account.

        Banked into its ``cash`` when the short opens and released proportionally
        on cover (``pos.entry_cost`` for a short tracks exactly this). They may
        only fund 买券还券, never new positions or transfers, so they are excluded
        from the credit account's ``available_cash``."""
        return sum(pos.entry_cost for pos in self.credit.positions.values() if pos.side == "short")

    def available_cash(self, account: str) -> float:
        """One account's cash deployable for buys (and outbound transfers):
        literal cash, minus the locked 融券 proceeds on the credit side. Margin is
        NOT subtracted — 保证金占用 is a computed constraint that gates new credit
        ops via :meth:`enable_bail_balance`, not frozen cash."""
        state = self.accounts[self._normalize_account(account)]
        if state.name == "credit":
            return state.cash - self._short_proceeds_locked()
        return state.cash

    def _fin_amount_outstanding(self) -> float:
        return sum(c.principal for c in self.credit.contracts if c.kind == "fin" and not c.closed)

    def _slo_sell_amount_outstanding(self) -> float:
        return sum(c.sell_amount for c in self.credit.contracts if c.kind == "slo" and not c.closed)

    def _slo_mv_outstanding(self) -> float:
        # Borrowed shares stay in lock-step with the short positions (cover and
        # liquidation repay contracts as they reduce), so the marked position value
        # is the 融券负债市值.
        return sum(pos.short_liability for pos in self.credit.positions.values())

    def _interest_outstanding(self) -> float:
        return sum(c.interest_accrued for c in self.credit.contracts)

    def _new_contract(
        self,
        *,
        kind: str,
        ts_code: str,
        trade_date: str,
        open_price: float,
        principal: float = 0.0,
        shares: int = 0,
        sell_amount: float = 0.0,
    ) -> DebtContract:
        self._compact_seq += 1
        rate = self.profile.fin_rate_annual if kind == "fin" else self.profile.slo_rate_annual
        return DebtContract(
            compact_id="D%06d" % self._compact_seq,
            kind=kind,
            ts_code=str(ts_code),
            open_date=str(trade_date),
            open_price=float(open_price),
            year_rate=rate,
            principal=float(principal),
            shares=int(shares),
            sell_amount=float(sell_amount),
            business_balance=float(principal or sell_amount),
            business_vol=int(shares),
        )

    def _credit_target_reject(self, ts_code: str) -> bool:
        """融资标的 gate: margin_secs carries no 融资/融券 split, so fin_buy shares
        the shortable set (and its per-fill-day refresh); ``theoretical_short`` mode
        lifts the gate for research runs, mirroring the short-inventory modes."""
        if self.profile.short_inventory_mode == "theoretical_short":
            return False
        eligible = self.shortable_by_date.get(self.current_date, self.shortable_codes)
        return str(ts_code) not in eligible

    def _direct_repay(self, amount: float, *, order_id: str, reason: str = "", submitted_at: str = "") -> str:
        """直接还款: an immediate cash operation (no order book, no bar matching).
        The applied amount is clamped to deployable cash and the outstanding 融资
        debt (interest first, FIFO); zero applicability rejects."""
        order = Order(
            ts_code="", action="direct_repay", side="long",
            requested_amount=int(amount), trade_date=self.current_date,
            decision_time=str(submitted_at or ""), reason=str(reason or "direct_repay"),
            price_label="cash_op", account="credit", op_type=optype.DIRECT_REPAY, order_id=order_id,
        )
        self.orders.append(order)
        owed = self._fin_amount_outstanding() + sum(
            c.interest_accrued for c in self.credit.contracts if c.kind == "fin"
        )
        if owed <= 1e-9:
            self._reject(order, "no_fin_debt")
            return order_id
        applicable = min(float(amount), self.available_cash("credit"), owed)
        if applicable <= 1e-9:
            self._reject(order, "insufficient_cash")
            return order_id
        repaid = repay_fin(self.credit.contracts, applicable, release_shares=True)
        self.credit.cash -= repaid["applied"]
        self.interest_paid_total += repaid["interest_paid"]
        order.status = "filled"
        order.filled_quantity = int(repaid["applied"])
        self._event(
            "debt_repaid", trade_date=self.current_date, order_id=order_id,
            kind="fin", via="direct_repay", **repaid,
        )
        return order_id

    def _ledger(self, ts_code: str, *, account: str, side: str, kind: str, price: float, quantity: int, trade_date: str, realized_pnl: float | None = None) -> None:
        record = {
            "ts_code": ts_code,
            "account": account,
            "side": side,
            "kind": kind,
            "price": float(price),
            "quantity": int(quantity),
            "amount": int(quantity),
            "notional": float(price) * int(quantity),
            "trade_date": str(trade_date),
            "date": str(trade_date),
        }
        if realized_pnl is not None:
            record["realized_pnl"] = float(realized_pnl)
        self.trade_ledger.setdefault(str(ts_code), []).append(record)

    def _event(self, event_type: str, **payload: object) -> None:
        self.events.append({"event_type": event_type, **payload})


def load_shortable_codes(snapshot_dir: str | Path, decision_date: str) -> frozenset[str]:
    """Decision-date margin_secs membership from the events domain (proxy mode)."""
    events_path = Path(snapshot_dir) / "events.parquet"
    if not events_path.exists():
        return frozenset()
    events = pd.read_parquet(events_path)
    if events.empty or "dataset" not in events.columns:
        return frozenset()
    rows = events[(events["dataset"] == "margin_secs")]
    if "trade_date" in rows.columns:
        rows = rows[rows["trade_date"].astype(str) == str(decision_date)]
    return frozenset(rows["ts_code"].astype(str)) if "ts_code" in rows.columns else frozenset()


def load_shortable_by_date(replay_dir: str | Path) -> dict[str, frozenset[str]]:
    """Per-fill-day margin_secs membership from a replay slot's events domain.

    Maps each replay trade_date to that day's complete shortable set so the broker
    can gate short fills on the real same-day inventory (proxy mode) rather than the
    agent's frozen decision-day snapshot. Empty when the slot carries no events; the
    broker then falls back to the frozen ``shortable_codes`` for every fill day."""
    events_path = Path(replay_dir) / "events.parquet"
    if not events_path.exists():
        return {}
    events = pd.read_parquet(events_path)
    if events.empty or "dataset" not in events.columns:
        return {}
    rows = events[events["dataset"] == "margin_secs"]
    if rows.empty or "trade_date" not in rows.columns or "ts_code" not in rows.columns:
        return {}
    return {
        str(date): frozenset(group["ts_code"].astype(str))
        for date, group in rows.groupby(rows["trade_date"].astype(str))
    }


def _bar_for_code(minute_group: pd.DataFrame, ts_code: str) -> pd.Series | None:
    rows = minute_group[minute_group["ts_code"].astype(str) == str(ts_code)]
    return None if rows.empty else rows.iloc[-1]


def _open_price(bar: pd.Series) -> float | None:
    """Market fill price: the bar's open, falling back to its close."""
    for field_name in ("open", "close"):
        value = bar.get(field_name)
        if value is not None and pd.notna(value):
            return float(value)
    return None


def _close_price(bar: pd.Series) -> float | None:
    """Close-auction fill price: the bar's close, falling back to its open."""
    for field_name in ("close", "open"):
        value = bar.get(field_name)
        if value is not None and pd.notna(value):
            return float(value)
    return None


def _limit_fill_price(order: WorkingOrder, bar: pd.Series, *, use_close: bool = False) -> float | None:
    """Fill price for a working order against this bar, or None if not fillable now.

    Market orders fill at the bar reference price: the bar OPEN by default, or the
    bar CLOSE for a close (15:00) call-auction order (``use_close``) so a 14:57
    close-auction decision settles at the day's close, not its open. A limit order
    fills only when the bar's range reaches the limit: buy/cover orders fill at the
    reference price when it is already at or below the limit, otherwise at the limit
    after an intrabar dip; sell/short orders symmetrically fill at an already-favorable
    reference price, otherwise at the limit."""
    ref_price = _close_price(bar) if use_close else _open_price(bar)
    if order.price is None or ref_price is None:
        return ref_price
    limit = order.price
    if order.action in ("buy", "credit_buy", "fin_buy", "cover"):
        if ref_price <= limit:
            return ref_price
        low = bar.get("low")
        return limit if (low is not None and pd.notna(low) and float(low) <= limit) else None
    if ref_price >= limit:
        return ref_price
    high = bar.get("high")
    return limit if (high is not None and pd.notna(high) and float(high) >= limit) else None


class TraderProtocol(Protocol):
    """The official-QMT-aligned surface that the backtest ``SimBroker`` and a live
    adapter (``QMTBroker``) both expose, so order plumbing is backend-agnostic.

    Methods mirror the in-client strategy API (``passorder``/``cancel``/
    ``get_trade_detail_data`` plus the credit queries); the record-field mapping to
    the official ``m_*`` object attributes is tabled in
    docs/environment_design.md §3.2. ``passorder`` returns the order id the
    official flow recovers via ``get_last_order_id`` immediately after submitting
    with a unique ``user_order_id`` (投资备注) — a live adapter implements exactly
    that pair."""

    def passorder(self, op_type: int, order_type: int, account_id: str, order_code: str, pr_type: int, price: float | None, volume: int | None, **kwargs: object) -> str: ...
    def cancel(self, order_id: str, account_id: str = "", account_type: str = "", **kwargs: object) -> bool: ...
    def get_trade_detail_data(self, account_id: str = "", account_type: str = "", data_type: str = "ORDER", strategy_name: str = "") -> list[dict[str, object]]: ...
    def get_debt_contract(self, account_id: str = "") -> list[dict[str, object]]: ...
    def get_assure_contract(self, account_id: str = "") -> list[dict[str, object]]: ...
    def get_enable_short_contract(self, account_id: str = "") -> list[dict[str, object]]: ...
