"""Simulated Broker aligned with the official full-QMT in-client trading API.

The host boundary mirrors QMT's strategy API (docs/environment_design.md §3.4):
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
§3.2/§3.5): cash/margin, T+1 sellable balance, lot size, limit up/down, suspension, the
configured short-inventory mode (default ``proxy_margin_secs``), the 融券卖出
limit-price rule (申报价不得低于最新成交价 — shorts must be limit orders), optional
concentration limits, commission, stamp duty, slippage, and forced close. Ex-date
corporate actions (cash dividends, 送转 bonus shares) apply to both sides at the
day roll — longs are credited, 融券 shorts compensate the lender. Every
order/reject is recorded.
"""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
import math
from pathlib import Path
from typing import Protocol

import pandas as pd

from autotrade.data_sources.tushare.common import STK_AUCTION_PRICE_ABS_TOLERANCE
from autotrade.environment.broker_core import (
    LOT_SIZE,
    STAR_MIN_LOT_SIZE,
    STAMP_DUTY_CUTOVER,
    CostModel,
    DebtContract,
    accrue_debt_interest,
    credit_maintenance_ratio,
    enable_bail_balance,
    is_bse_market,
    is_star_market,
    project_open,
    project_reduce,
    release_fin_shares,
    repay_fin,
    repay_slo,
    scale_slo_contracts,
)
from autotrade.environment.runtime import new_id

SHORT_INVENTORY_MODES = ("proxy_margin_secs", "broker_inventory", "theoretical_short")
CORPORATE_ACTION_MODES = ("modeled", "disabled")
ACCOUNT_TYPES = ("stock", "credit")


@dataclass(frozen=True)
class BrokerProfile:
    """Default GJZQ-style dual-account replay profile (docs/environment_design.md §3.2).

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
    transfer_fee_bps: float = 0.1
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
    # Ex-date corporate actions (cash dividends and 送转 share bonuses) applied to
    # both long and short positions at roll_to_date; "disabled" is a research
    # isolation switch. 配股 (rights issues) are not modeled.
    corporate_actions: str = "modeled"
    # Flat research haircut on cash dividends credited to longs — the real
    # differential 0/10%/20% tax settled at sale by holding period is not modeled.
    # Shorts always compensate the lender the gross amount.
    dividend_tax_rate: float = 0.0
    maintenance_closeout_ratio: float = 1.30
    # Reference lines recorded for audit only; the engine enforces just
    # maintenance_closeout_ratio (forced close), not these two.
    maintenance_warning_ratio: float = 1.40
    maintenance_withdraw_ratio: float = 3.00
    debt_contract_term_days: int = 180
    debt_contract_auto_extend: bool = True
    max_single_name_weight: float | None = None
    profile_id: str = "gjzq_dual"
    source: str = "docs/environment_design.md#32-broker账户与模拟交易建模"
    formula_source: str = "https://www.sse.com.cn/services/tradingservice/margin/edu/c/10074042/files/a1f1c4833302451fb9130dbb94116c56.pdf"
    maintenance_source: str = "https://www.gjzq.com.cn/main/a/rzrq/index.html"

    def __post_init__(self) -> None:
        # Range checks fail closed against NaN too: every `NaN <op> x` is False,
        # so the guards are written as "not (valid range)".
        for name in ("stock_initial_cash", "credit_initial_cash"):
            if not (math.isfinite(getattr(self, name)) and getattr(self, name) >= 0):
                raise ValueError(f"{name} must be a non-negative finite number")
        if self.stock_initial_cash + self.credit_initial_cash <= 0:
            raise ValueError("combined initial cash must be positive")
        # Fees, rates and slippage must be non-negative: a negative slippage or
        # fee would improve both buy and sell fills (free alpha from a typo).
        for name in (
            "commission_bps", "min_commission_cny", "stamp_duty_sell_bps_before_cutover",
            "stamp_duty_sell_bps_from_cutover", "transfer_fee_bps", "slippage_bps",
            "fin_rate_annual", "slo_rate_annual",
        ):
            if not (math.isfinite(getattr(self, name)) and getattr(self, name) >= 0):
                raise ValueError(f"{name} must be a non-negative finite number")
        for name in ("fin_margin_ratio", "slo_margin_ratio", "slo_margin_ratio_private_fund"):
            if not (math.isfinite(getattr(self, name)) and getattr(self, name) > 0):
                raise ValueError(f"{name} must be a positive finite number")
        for name in ("fin_max_quota", "slo_max_quota"):
            quota = getattr(self, name)
            if quota is not None and not (math.isfinite(quota) and quota >= 0):
                raise ValueError(f"{name} must be a non-negative finite number when set")
        if self.short_inventory_mode not in SHORT_INVENTORY_MODES:
            raise ValueError(f"unsupported short_inventory_mode={self.short_inventory_mode}")
        if self.corporate_actions not in CORPORATE_ACTION_MODES:
            raise ValueError(f"unsupported corporate_actions={self.corporate_actions}")
        if not 0.0 <= self.dividend_tax_rate < 1.0:
            raise ValueError("dividend_tax_rate must be in [0, 1)")
        if self.max_total_holdings is not None and self.max_total_holdings <= 0:
            raise ValueError("max_total_holdings must be positive")
        if self.max_single_name_weight is not None and not (
            math.isfinite(self.max_single_name_weight) and self.max_single_name_weight > 0
        ):
            raise ValueError("max_single_name_weight must be a positive finite number")
        if not 0.0 < self.assure_ratio <= 1.0:
            raise ValueError("assure_ratio must be in (0, 1]")
        if not (
            math.isfinite(self.maintenance_closeout_ratio)
            and math.isfinite(self.maintenance_warning_ratio)
            and math.isfinite(self.maintenance_withdraw_ratio)
            and 0.0 < self.maintenance_closeout_ratio
            <= self.maintenance_warning_ratio
            <= self.maintenance_withdraw_ratio
        ):
            raise ValueError("maintenance ratios must be finite and ordered: 0 < closeout <= warning <= withdraw")

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
            transfer_fee_bps=self.transfer_fee_bps,
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
            "transfer_fee_bps": self.transfer_fee_bps,
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
            "corporate_actions": self.corporate_actions,
            "dividend_tax_rate": self.dividend_tax_rate,
            "maintenance_closeout_ratio": self.maintenance_closeout_ratio,
            "maintenance_warning_ratio": self.maintenance_warning_ratio,
            "maintenance_withdraw_ratio": self.maintenance_withdraw_ratio,
            "debt_contract_term_days": self.debt_contract_term_days,
            "debt_contract_auto_extend": self.debt_contract_auto_extend,
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
        # Codes present anywhere in the replay region: orders outside this set
        # (e.g. screened out of the research universe) reject at submission.
        self.codes = frozenset(frame["ts_code"].unique())

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
    """Audited record of a single broker primitive call (filled, rejected or
    cancelled). ``submitted_at`` is the submission time (a resting order keeps
    it across bars; ``decision_time`` is the settlement/reject bar), ``limit_price``
    the original 指定价 when the order was a limit order (``price`` is the final
    fill price), and ``fee``/``stamp_duty`` the per-order cost breakdown."""

    ts_code: str
    action: str  # buy | sell | short | cover | close | fin_buy | sell_repay | direct_repay
    side: str  # "long" | "short"
    requested_amount: int
    trade_date: str
    decision_time: str = ""
    submitted_at: str = ""
    price: float | None = None
    limit_price: float | None = None
    filled_quantity: int = 0
    status: str = "submitted"
    reject_reason: str | None = None
    reason: str = ""
    source_artifacts: list[str] = field(default_factory=list)
    price_label: str = "price"
    account: str = ""
    op_type: int | None = None
    fee: float = 0.0
    stamp_duty: float = 0.0
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
            "limit_price": self.limit_price,
            "price_label": self.price_label,
            "status": self.status,
            "reject_reason": self.reject_reason,
            "decision_time": self.decision_time,
            "submitted_at": self.submitted_at,
            "trade_date": self.trade_date,
            "fee": self.fee,
            "stamp_duty": self.stamp_duty,
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

        Shares acquired or borrowed today (``locked_today``) are not sellable or
        coverable until a later trade date. This applies to both cash longs and
        融券 shorts: 买券还券 is T+1 after the short sale."""
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
    with no slippage. Orders are day orders: they remain cancelable until filled,
    explicitly cancelled, or swept at day end. A 融券卖出 order is checked against
    the 申报价 rule at its first match attempt (``uptick_checked``).
    """

    order_id: str
    action: str
    account: str
    op_type: int
    ts_code: str
    volume: int | None
    price_type: int
    price: float | None
    is_auction: bool
    reason: str
    submitted_at: str = ""
    # A close (15:00) call-auction order fills at the activation bar's CLOSE; an
    # open (09:25) auction or a continuous order fills at its bar OPEN.
    auction_close: bool = False
    uptick_checked: bool = False
    # Decision-time price estimate for a MARKET order so it keeps reserving cash /
    # bail balance while resting across printless bars (a limit order reserves at
    # its own price).
    reserve_price: float | None = None

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
            "price_type": self.price_type,
            "price": self.price,
            "reserve_price": self.reserve_price,
            "status": "working",
            "submitted_at": self.submitted_at,
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
        corporate_actions_by_date: dict[str, list[dict[str, object]]] | None = None,
        auction_prints_by_date: dict[tuple[str, str], dict[str, float]] | None = None,
    ) -> None:
        self.profile = profile
        self.market = market
        # Actual call-auction prints from the replay slot (environment market
        # truth): {(trade_date, "open"|"close"): {ts_code: matched price}}. When
        # a print exists, auction orders clear at it instead of the bar
        # open/close approximation; the exchange publishes the print at matching
        # time, so same-day use is PIT-correct. Days without rows (pre-coverage
        # or per-code gaps) keep the bar-based semantics.
        self.auction_prints_by_date = dict(auction_prints_by_date or {})
        # Ex-date corporate actions from the replay slot (environment market truth,
        # not an agent input — the agent's dividend view stays announcement-gated):
        # {ex_date: [{ts_code, cash_per_share, stock_per_share, record_date, ...}]}.
        self.corporate_actions_by_date = dict(corporate_actions_by_date or {})
        self._corporate_actions_applied: set[str] = set()
        self.dividend_cash_received = 0.0  # net cash credited to longs
        self.dividend_compensation_paid = 0.0  # gross cash debited from shorts
        # Frozen decision-day margin_secs set (the agent's snapshot view), used as the
        # fallback when a fill day is absent from the per-day map below. margin_secs
        # carries no 担保品/融资/融券 split, so the same set gates credit_buy,
        # fin_buy, and short eligibility (documented approximation).
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
        # End-of-day position snapshots per (date, account, ts_code, side): the
        # ground truth for downstream attribution (forced closes, bonus shares
        # and per-account long/short legs are all reflected — a fills-only
        # reconstruction misses them). Replace-by-date keeps re-marking idempotent.
        self._positions_eod: dict[str, list[dict[str, object]]] = {}

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

    # ---- official-API queries (docs/environment_design.md §3.4) ----

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
            return [self.account_record(account)]
        if kind == "POSITION":
            return self.position_records(account)
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
        set (empty on a per-day data gap — same fail-closed rule the short gate
        applies). Quantities are unknown in proxy mode, so records carry
        eligibility only."""
        return [
            {"ts_code": code, "slo_ratio": self.profile.effective_slo_margin_ratio, "slo_status": "normal"}
            for code in sorted(self._fill_day_shortable() or ())
        ]

    @staticmethod
    def _normalize_account(account: str) -> str:
        name = str(account or "").strip().lower()
        if name not in ACCOUNT_TYPES:
            raise ValueError(f"account_type is required and must be STOCK or CREDIT, got {account!r}")
        return name

    def account_record(
        self,
        account: str,
        *,
        pending_orders: Iterable[object] = (),
    ) -> dict[str, object]:
        """Account view visible to the strategy.

        Literal ``cash`` and positions remain filled-state truth. Deployable
        fields such as ``available_cash`` and ``enable_bail_balance`` subtract
        already-submitted but unfilled orders, matching a live broker's buying
        power reservation.
        """
        account = self._normalize_account(account)
        state = self.accounts[account]
        record: dict[str, object] = {
            "account_type": account.upper(),
            "cash": state.cash,
            "available_cash": self.available_cash(account, pending_orders=pending_orders),
            "total_assets": self.account_equity(account),
            "market_value": state.long_market_value(),
        }
        if account == "credit":
            fin_amount = self._fin_amount_outstanding()
            record.update(
                {
                    "maintenance_ratio": self.maintenance_ratio(),
                    "enable_bail_balance": self.enable_bail_balance(pending_orders=pending_orders),
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

    def position_records(
        self,
        account: str,
        *,
        pending_orders: Iterable[object] = (),
    ) -> list[dict[str, object]]:
        account = self._normalize_account(account)
        return [
            {
                "account": account,
                "ts_code": pos.ts_code,
                "side": pos.side,
                "quantity": pos.quantity,
                "sellable_quantity": self.sellable_quantity(
                    account, pos.ts_code, pending_orders=pending_orders
                ),
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
        is_auction: bool = False,
        auction_close: bool = False,
        reserve_price: float | None = None,
        reason: str = "",
        submitted_at: str = "",
    ) -> str:
        """Submit an order by official opType and return its order id.

        Mirrors QMT ``passorder`` with sim conveniences: the returned id is what
        the official flow recovers via ``get_last_order_id`` right after the call
        (``user_order_id`` doubles as the id/投资备注 when given, so the agent's
        client id is the correlation key, as live remarks are). The opType alone
        selects the account (23/24 普通, 27–34 信用). The auction flags, ``reason``
        and ``submitted_at`` are backtest conveniences a live
        adapter resolves before its own passorder. Only
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
        if str(order_code) not in self.market.codes:
            # Fail fast instead of letting the order rest all day and void as
            # day_end_unfilled: the code has NO data in this replay region —
            # typically screened out of the experiment's research universe.
            self.reject_submission(
                ts_code=str(order_code),
                action=action,
                reason="code_not_in_universe",
                amount=volume,
                submitted_at=submitted_at,
                order_id=order_id,
            )
            return order_id
        shares, amount_reject = self.validate_share_amount(volume, str(order_code))
        if amount_reject is not None:
            self.reject_submission(
                ts_code=str(order_code),
                action=action,
                reason=amount_reject,
                amount=volume,
                submitted_at=submitted_at,
                order_id=order_id,
            )
            return order_id
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
                volume=shares,
                price_type=pr_type,
                price=float(price) if is_limit else None,
                is_auction=bool(is_auction),
                auction_close=bool(auction_close),
                reserve_price=(None if is_limit else reserve_price),
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
        needed for the lookup. The cancelled order stays in the ORDER records
        with ``status="cancelled"`` (``reject_reason`` carries the cancel reason)
        — live QMT flows filter ORDER by status, so a vanished order would break
        that mirror and erase the audit trail."""
        if account_type:
            self._normalize_account(account_type)
        for index, order in enumerate(self._book):
            if order.order_id == order_id:
                self._book.pop(index)
                self.orders.append(
                    Order(
                        ts_code=order.ts_code,
                        action=order.action,
                        side="short" if order.action in {"short", "cover"} else "long",
                        requested_amount=int(order.volume or 0),
                        trade_date=str(trade_date or self.current_date),
                        decision_time=str(minute_key or ""),
                        submitted_at=order.submitted_at,
                        limit_price=order.price,
                        status="cancelled",
                        reject_reason=reason,
                        reason=order.reason,
                        account=order.account,
                        op_type=order.op_type,
                        order_id=order.order_id,
                    )
                )
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
        price-limit, suspension and short-inventory rejects); unfilled orders rest
        until filled, explicitly cancelled, or swept at day end.

        Held positions are re-marked to this bar in two phases: the bar OPEN before
        matching, so cash/margin admission values existing holdings at the bar the
        order reaches (not the previous session's close), and the bar CLOSE after
        matching, so the agent-visible account state reflects the completed bar.
        Interest accrual and the forced-close check keep their own end-of-day
        schedule in :meth:`mark_to_market`."""
        relevant_codes = {str(order.ts_code) for order in self._book}
        relevant_codes.update(
            str(pos.ts_code)
            for state in self.accounts.values()
            for pos in state.positions.values()
        )
        bars_by_code = _bars_for_codes(minute_group, relevant_codes)
        self._mark_positions_to_bars(bars_by_code, at_open=True)
        survivors: list[WorkingOrder] = []
        for order in self._book:
            bar = bars_by_code.get(str(order.ts_code))
            if bar is None:
                # The code printed no bar this minute. A market order keeps working
                # until the day's next bar with trades (an auction order that missed
                # its single-price bar rolls into continuous matching and loses the
                # slippage-free auction treatment, as an unmatched 集合竞价 order
                # does); the day-end sweep is its backstop.
                if not order.is_limit:
                    order.is_auction = False
                    order.auction_close = False
                survivors.append(order)
                continue
            auction_price = self._auction_print(trade_date, order)
            if order.is_auction and auction_price == 0.0:
                # stk_auction explicitly reports no opening-auction trade for
                # this code. Do not fabricate a fill from the 09:30 bar; the
                # unmatched order enters continuous trading instead.
                if not order.auction_close:
                    order.is_auction = False
                survivors.append(order)
                continue
            if order.action == "short" and not order.uptick_checked:
                # 融券卖出申报价不得低于最新成交价: checked once, when the order first
                # reaches the exchange (its activation bar). An aggressive limit below
                # the reference price would have been rejected at 申报.
                order.uptick_checked = True
                ref_price = auction_price if auction_price is not None else (
                    _close_price(bar) if order.auction_close else _open_price(bar)
                )
                if order.price is not None and ref_price is not None and order.price < ref_price:
                    rejected = Order(
                        ts_code=order.ts_code, action="short", side="short",
                        requested_amount=int(order.volume or 0), trade_date=str(trade_date),
                        decision_time=str(minute_key), submitted_at=order.submitted_at,
                        limit_price=order.price, reason=order.reason,
                        account="credit", op_type=order.op_type, order_id=order.order_id,
                    )
                    self.orders.append(rejected)
                    self._reject(rejected, "slo_sell_uptick_rule")
                    continue
            price = _limit_fill_price(
                order, bar, use_close=order.auction_close,
                ref_override=auction_price,
            )
            if price is not None:
                self.execute(
                    order.ts_code,
                    order.action,
                    trade_date=trade_date,
                    raw_price=price,
                    amount=order.volume,
                    time=minute_key,
                    reason=order.reason,
                    price_label="auction" if order.is_auction else f"{granularity}:{minute_key}",
                    # A call auction (open 09:25 / close 15:00) clears every order at one
                    # uniform price, so it carries no taker spread; only continuous-session
                    # market orders take slippage. Limit orders never take slippage.
                    apply_slippage=not order.is_limit and not order.is_auction,
                    order_id=order.order_id,
                    submitted_at=order.submitted_at,
                    limit_price=order.price,
                )
            else:
                if order.is_auction and not order.auction_close:
                    # Reached its OPEN-auction bar without clearing at the single
                    # price: like a real unmatched 集合竞价 order it rolls into
                    # continuous matching as a plain limit order (and stops
                    # referencing the auction print). A close-auction order has
                    # no session left to roll into; the day-end sweep voids it.
                    order.is_auction = False
                survivors.append(order)
        self._book = survivors
        self._mark_positions_to_bars(bars_by_code, at_open=False)

    def _auction_print(self, trade_date: object, order: "WorkingOrder") -> float | None:
        """The day's actual OPENING-auction price for this order, if it is
        still an auction order and the replay slot carries the print. Closing
        auctions have no print source (stk_auction covers the open session
        only) and clear at the final bar close."""
        if not order.is_auction or order.auction_close:
            return None
        prints = self.auction_prints_by_date.get((str(trade_date), "open"))
        if not prints:
            return None
        return prints.get(order.ts_code)

    def _mark_positions_to_bars(self, bars_by_code: dict[str, pd.Series], *, at_open: bool) -> None:
        """Re-mark held positions to the current bar (open or close phase)."""
        if not bars_by_code:
            return
        for state in self.accounts.values():
            for pos in state.positions.values():
                bar = bars_by_code.get(str(pos.ts_code))
                if bar is None:
                    continue
                price = _open_price(bar) if at_open else _close_price(bar)
                if price is not None:
                    pos.last_price = price

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

    def reject_submission(
        self,
        *,
        ts_code: str,
        action: str,
        reason: str,
        amount: object = None,
        submitted_at: str = "",
        order_id: str | None = None,
    ) -> Order:
        """Record a submission-time reject before an order reaches the book."""
        action = str(action).lower().strip()
        pair = _ACTION_TO_ACCOUNT_OP.get(action)
        side = "short" if action in {"short", "cover"} else "long"
        try:
            requested = int(float(amount)) if amount is not None and str(amount).strip() else 0
        except (TypeError, ValueError):
            requested = 0
        order = Order(
            ts_code=str(ts_code),
            action=action,
            side=side,
            requested_amount=requested,
            trade_date=self.current_date,
            decision_time=str(submitted_at or ""),
            submitted_at=str(submitted_at or ""),
            reason=action,
            account=pair[0] if pair else "",
            op_type=pair[1] if pair else None,
            **({"order_id": str(order_id)} if order_id else {}),
        )
        self.orders.append(order)
        return self._reject(order, reason)

    def roll_to_date(self, trade_date: str) -> None:
        """Lift the T+1 lock for every position when the sim-date rolls to a new day.

        Runs the same ``locked_date < trade_date`` unlock as :meth:`_advance_date`
        for ALL positions, but is called once by the host at the START of each new
        trade date — before the day's first ``ctx``/tick is built — so an overnight
        hold reports its full ``sellable_quantity`` from the day's first off-session
        tick rather than only after that day's first fill. Idempotent and
        deterministic: ``execute``/``mark_to_market`` still call ``_advance_date`` as
        a safety net, and re-rolling to the same date is a no-op. Ex-date corporate
        actions apply here — once per date, before the day's first tick (盘前到账)."""
        self._advance_date(trade_date)
        if self.profile.corporate_actions == "modeled":
            self._apply_corporate_actions(str(trade_date))

    # ---- corporate actions (docs/environment_design.md §3.2) ----

    def _apply_corporate_actions(self, trade_date: str) -> None:
        """Apply the date's ex-date events to every held position, once per date.

        Runs at the start of the ex-date before any tick, so entitlement is the
        overnight position — the record-date close holding. Longs are credited the
        cash dividend (net of the flat ``dividend_tax_rate``) and the 送转 bonus
        shares; shorts compensate the lender the gross cash amount and owe the
        post-conversion share count (their 融券 contracts scale in step).
        ``last_price`` is rebased to the theoretical ex price so marks, maintenance
        and equity stay continuous even when the code does not trade that day."""
        if trade_date in self._corporate_actions_applied:
            return
        self._corporate_actions_applied.add(trade_date)
        for action in self.corporate_actions_by_date.get(trade_date, ()):
            ts_code = str(action.get("ts_code") or "")
            cash = float(action.get("cash_per_share") or 0.0)
            bonus_ratio = float(action.get("stock_per_share") or 0.0)
            if not ts_code or (cash <= 0.0 and bonus_ratio <= 0.0):
                continue
            record_date = str(action.get("record_date") or "")
            expected = self._previous_trade_date(trade_date)
            if record_date and expected and record_date != expected:
                # Entitlement is still applied on the overnight position; the gap
                # (e.g. the code was suspended on its record date) is audit-logged.
                self._event(
                    "corporate_action_calendar_gap", trade_date=trade_date, ts_code=ts_code,
                    record_date=record_date, expected_record_date=expected,
                )
            for state in self.accounts.values():
                pos = state.positions.get(ts_code)
                if pos is None or pos.quantity <= 0:
                    continue
                if cash > 0.0:
                    self._apply_cash_dividend(state, pos, cash, trade_date)
                if bonus_ratio > 0.0:
                    self._apply_share_bonus(
                        state, pos, bonus_ratio, trade_date, str(action.get("div_listdate") or "")
                    )
                pos.last_price = max((pos.last_price - cash) / (1.0 + bonus_ratio), 0.0)

    def _apply_cash_dividend(self, state: AccountState, pos: Position, per_share: float, trade_date: str) -> None:
        gross = pos.quantity * per_share
        if pos.side == "long":
            amount = gross * (1.0 - self.profile.dividend_tax_rate)
            state.cash += amount
            self.dividend_cash_received += amount
        else:
            # 融券期间权益补偿: the borrower owes the lender the full distribution.
            amount = -gross
            state.cash -= gross
            self.dividend_compensation_paid += gross
        self._event(
            "dividend_cash", trade_date=trade_date, ts_code=pos.ts_code, account=state.name,
            side=pos.side, per_share=per_share, quantity=pos.quantity, amount=amount,
        )

    def _apply_share_bonus(
        self, state: AccountState, pos: Position, per_share: float, trade_date: str, div_listdate: str
    ) -> None:
        if pos.side == "short":
            # The lender is owed the post-conversion count: each open 融券 contract
            # scales in place (interest basis preserved) and the position follows,
            # keeping the position/contract share invariant.
            bonus = scale_slo_contracts(state.contracts, pos.ts_code, per_share)
        else:
            bonus = int(pos.quantity * per_share)
        if bonus <= 0:
            return
        # Rebase the average entry so cost basis — and later realized P&L — stay
        # continuous across the ex-date; entry_cost (cash actually paid/locked) is
        # untouched. 融资 contracts also stay untouched: bonus shares on financed
        # positions are ordinary collateral, not new financed shares.
        pos.entry_price = pos.entry_price * pos.quantity / (pos.quantity + bonus)
        pos.quantity += bonus
        locked_until = ""
        if pos.side == "long" and div_listdate > trade_date:
            # 红股上市日晚于除权日: the bonus shares count toward value now but stay
            # unsellable until div_listdate. A later same-day buy folds this lock
            # back to the ex-date (single-lock approximation, audit event above).
            prev = self._previous_trade_date(div_listdate)
            if prev and prev >= trade_date:
                pos.locked_today += bonus
                pos.locked_date = max(pos.locked_date, prev)
                locked_until = div_listdate
        self._event(
            "bonus_shares", trade_date=trade_date, ts_code=pos.ts_code, account=state.name,
            side=pos.side, per_share=per_share, quantity=bonus,
            **({"locked_until": locked_until} if locked_until else {}),
        )

    def _previous_trade_date(self, date_key: str) -> str | None:
        """The last replay trade date strictly before ``date_key``, or None."""
        dates = self.market.trade_dates
        index = bisect_left(dates, str(date_key))
        return dates[index - 1] if index > 0 else None

    # ---- fundamental primitives ----

    def execute(
        self,
        ts_code: str,
        action: str,
        *,
        trade_date: str,
        raw_price: float | None,
        amount: int | None = None,
        time: str = "",
        reason: str = "",
        source_artifacts: list[str] | None = None,
        price_label: str = "price",
        apply_slippage: bool = True,
        order_id: str | None = None,
        submitted_at: str = "",
        limit_price: float | None = None,
    ) -> Order:
        """Apply one strategy primitive at the current bar with full constraints.

        ``action`` is ``buy``/``sell`` (stock account), ``credit_buy``/
        ``credit_sell``/``short``/``cover``/``fin_buy``/``sell_repay`` (credit
        account); 直接还款 and ``transfer`` settle without a bar via
        :meth:`passorder`/:meth:`transfer`, and ``close`` is resolved by the
        engine before submission. ``amount`` is a share count (lot-aligned).
        ``apply_slippage`` is True for marketable (taker) fills and False for
        limit fills, where ``raw_price`` is the no-slippage limit-fill price
        (limit or better open). ``order_id`` carries the originating working
        order's id onto the fill.
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
            submitted_at=str(submitted_at or ""),
            limit_price=limit_price,
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
            return self._open(order, state, bar, raw_price, amount=amount, apply_slippage=apply_slippage)
        return self._reduce(order, state, bar, raw_price, amount=amount, apply_slippage=apply_slippage)

    def _open(self, order: Order, state: AccountState, bar: pd.Series, raw_price: float, *, amount, apply_slippage: bool = True) -> Order:
        shares, amount_reject = self._strict_lot_amount(amount, order.ts_code)
        if amount_reject is not None:
            return self._reject(order, amount_reject)
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
        cap_reject = self._single_name_cap_reject(order.ts_code, shares, raw_price)
        if cap_reject is not None:
            return self._reject(order, cap_reject)
        if order.action == "short":
            inventory_reject = self._short_inventory_reject(order.ts_code)
            if inventory_reject is not None:
                return self._reject(order, inventory_reject)
            if MarketData.limit_down_blocked_at_price(bar, raw_price):
                return self._reject(order, "limit_down_blocked_short")
            return self._fill_short_open(order, state, raw_price, shares, apply_slippage)
        if MarketData.limit_up_blocked_at_price(bar, raw_price):
            return self._reject(order, "limit_up_blocked_buy")
        if order.action == "credit_buy":
            reject = self._credit_target_reject(order.ts_code, "margin_secs_not_collateral")
            if reject is not None:
                return self._reject(order, reject)
        if order.action == "fin_buy":
            reject = self._credit_target_reject(order.ts_code, "margin_secs_not_finable")
            if reject is not None:
                return self._reject(order, reject)
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
        if fill.required_cash > self.available_cash(state.name, exclude_order_id=order.order_id) + 1e-6:
            return self._reject(order, "insufficient_cash")
        state.cash += fill.cash_delta
        self.fees_paid += fill.fee
        order.fee = fill.fee
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
        if required_bail > self.enable_bail_balance(exclude_order_id=order.order_id) + 1e-6:
            return self._reject(order, "insufficient_bail_balance")
        if (
            self.profile.fin_max_quota is not None
            and self._fin_amount_outstanding() + fill.cost_basis > self.profile.fin_max_quota + 1e-6
        ):
            return self._reject(order, "fin_quota_exceeded")
        self.fees_paid += fill.fee  # financed into the contract, counted as cost incurred
        order.fee = fill.fee
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
        if fill.required_cash > self.enable_bail_balance(exclude_order_id=order.order_id) + 1e-6:
            return self._reject(order, "insufficient_bail_balance")
        if (
            self.profile.slo_max_quota is not None
            and self._slo_sell_amount_outstanding() + fill.notional > self.profile.slo_max_quota + 1e-6
        ):
            return self._reject(order, "slo_quota_exceeded")
        state.cash += fill.cash_delta
        self.fees_paid += fill.fee
        self.stamp_duty_paid += fill.duty
        order.fee = fill.fee
        order.stamp_duty = fill.duty
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
        if order.action == "credit_sell" and self._fin_shares_outstanding(order.ts_code) > 0:
            return self._reject(order, "financed_shares_require_sell_repay")
        order.side = pos.side
        sellable = self.sellable_quantity(state.name, order.ts_code, exclude_order_id=order.order_id)
        if sellable <= 0:
            return self._reject(order, "t_plus_one_no_sellable")
        if amount is None:
            shares = sellable
        else:
            shares, amount_reject = self._strict_lot_amount(amount, order.ts_code)
            if amount_reject is not None:
                return self._reject(order, amount_reject)
            if shares > sellable:
                return self._reject(order, "amount_exceeds_sellable")
        is_buy = pos.side == "short"  # covering a short is a buy
        if pos.side == "long" and MarketData.limit_down_blocked_at_price(bar, raw_price):
            return self._reject(order, "limit_down_blocked_sell")
        if pos.side == "short" and MarketData.limit_up_blocked_at_price(bar, raw_price):
            return self._reject(order, "limit_up_blocked_cover")
        price = self.profile.slipped_price(raw_price, is_buy=is_buy) if apply_slippage else raw_price
        fill = self._reduce_position(state, pos, shares, price, order.trade_date)
        if fill is not None:
            order.fee = fill.fee
            order.stamp_duty = fill.duty
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
            self._apply_contract_terms(trade_date)
        ratio = self.maintenance_ratio()
        if (
            ratio is not None
            and self.profile.maintenance_closeout_ratio <= ratio < self.profile.maintenance_warning_ratio
        ):
            self._event("maintenance_warning", trade_date=trade_date, maintenance_ratio=ratio)
        if ratio is not None and ratio < self.profile.maintenance_closeout_ratio:
            # A maintenance breach liquidates the CREDIT account only — the stock
            # account is not collateral and is untouched by the forced close.
            self._event("forced_close_triggered", trade_date=trade_date, maintenance_ratio=ratio)
            self.close_all(trade_date, forced=True, account="credit")
        self.record_positions_eod(trade_date)
        return self.equity()

    def record_positions_eod(self, trade_date: str) -> None:
        """Snapshot end-of-day positions for this date (replace-by-date, so the
        exit-day mandatory liquidation can refresh the same date afterwards)."""
        self._positions_eod[str(trade_date)] = [
            {
                "date": str(trade_date),
                "account": state.name,
                "ts_code": pos.ts_code,
                "side": pos.side,
                "quantity": int(pos.quantity),
                "last_price": float(pos.last_price),
                "market_value": float(pos.market_value),
            }
            for state in self.accounts.values()
            for pos in state.positions.values()
        ]

    def positions_eod_records(self) -> list[dict[str, object]]:
        """All end-of-day position snapshots, date-ordered."""
        return [row for date in sorted(self._positions_eod) for row in self._positions_eod[date]]

    def _apply_contract_terms(self, trade_date: str) -> None:
        term_days = int(self.profile.debt_contract_term_days or 0)
        if term_days <= 0:
            return
        for contract in self.credit.contracts:
            if contract.closed:
                continue
            term_start = contract.last_extension_date or contract.open_date
            if _date_gap(term_start, str(trade_date)) < term_days:
                continue
            if self.profile.debt_contract_auto_extend:
                contract.extension_count += 1
                contract.last_extension_date = str(trade_date)
                self._event(
                    "debt_contract_extended",
                    trade_date=trade_date,
                    compact_id=contract.compact_id,
                    compact_type=contract.kind,
                    ts_code=contract.ts_code,
                    extension_count=contract.extension_count,
                    term_days=term_days,
                )
            else:
                self._event(
                    "debt_contract_term_due",
                    trade_date=trade_date,
                    compact_id=contract.compact_id,
                    compact_type=contract.kind,
                    ts_code=contract.ts_code,
                    term_days=term_days,
                )

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
        side = pos.side
        fill = self._reduce_position(state, pos, sellable, price, trade_date, forced=forced, price_label="close")
        if not forced:
            # Mandatory region-end exit: the HOST closed this position, not the
            # strategy. Counted into replay stats so a buy-and-forget strategy
            # cannot hide behind the exit-day safety net.
            self._event(
                "exit_liquidated_by_host", ts_code=ts_code, side=side,
                trade_date=trade_date, quantity=sellable, account=state.name,
            )
        if forced and state.name == "credit" and side == "long" and fill is not None:
            # 强平所得偿还融资负债 (interest first, oldest first): the broker keeps
            # the liquidation proceeds against outstanding 融资 debt rather than
            # leaving the principal accruing interest. Voluntary 担保品卖出 keeps
            # its proceeds in cash (only sell_repay repays by choice), and the
            # exit-day mandatory liquidation nets debt in equity as before.
            release_fin_shares(state.contracts, ts_code, sellable)
            repaid = repay_fin(state.contracts, max(0.0, fill.cash_delta), release_shares=False)
            if repaid["applied"] > 0:
                state.cash -= repaid["applied"]
                self.interest_paid_total += repaid["interest_paid"]
                self._event(
                    "debt_repaid", trade_date=trade_date, ts_code=ts_code,
                    kind="fin", via="forced_close", **repaid,
                )
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

    def outstanding_liabilities(self) -> float:
        """Open credit-account liabilities at current marks: 融资 principal plus
        accrued unpaid interest plus the marked 融券 share liability."""
        return self._fin_amount_outstanding() + self._interest_outstanding() + self._slo_mv_outstanding()

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

    def enable_bail_balance(
        self,
        *,
        pending_orders: Iterable[object] = (),
        exclude_order_id: str | None = None,
    ) -> float:
        """保证金可用余额 per the 实施细则 formula (broker_core docstring),
        computed over the credit account only. Already-submitted unfilled credit
        orders reserve buying power; the currently matching order may be excluded
        so it does not freeze itself while settling."""
        state = self.credit
        last_price = {pos.ts_code: pos.last_price for pos in state.positions.values()}
        long_qty = {
            pos.ts_code: pos.quantity for pos in state.positions.values() if pos.side == "long"
        }
        fin_terms: list[tuple[float, float]] = []
        for contract in state.contracts:
            if contract.kind != "fin" or contract.closed:
                continue
            # Attribute held shares to open financing legs first; 卖券还款 releases
            # those shares, while any remaining unrepaid principal stays in the
            # margin formula.
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
        raw = enable_bail_balance(
            state.cash,
            collateral_mv,
            fin_terms,
            slo_terms,
            self._interest_outstanding(),
            assure_ratio=self.profile.assure_ratio,
            fin_margin_ratio=self.profile.fin_margin_ratio,
            slo_margin_ratio=self.profile.effective_slo_margin_ratio,
        )
        return max(raw - self._reserved_bail(pending_orders=pending_orders, exclude_order_id=exclude_order_id), 0.0)

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
        if pos is None:
            pos = Position(
                ts_code=ts_code,
                side=side,
                quantity=shares,
                entry_price=price,
                entry_date=trade_date,
                entry_cost=cash_basis,
                last_price=price,
                locked_today=shares,
                locked_date=trade_date,
            )
            state.positions[ts_code] = pos
            return pos
        pos.entry_price = (pos.entry_price * pos.quantity + price * shares) / (pos.quantity + shares)
        pos.quantity += shares
        pos.entry_cost += cash_basis
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
        if shares <= 0:
            return None
        if shares > pos.sellable_quantity:
            raise ValueError("reduce shares exceed sellable quantity")
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
        self._event(
            "order_rejected",
            order_id=order.order_id,
            ts_code=order.ts_code,
            account=order.account,
            op_type=order.op_type,
            action=order.action,
            reason=reason,
        )
        return order

    def _fill_day_shortable(self) -> frozenset[str] | None:
        """The fill-day 融券标的 set; None = per-day data exists but misses this
        day — a data gap, fail closed (the real list is published daily, so a
        stale set would overstate availability). An empty by-date map means the
        replay slot carries no margin_secs domain at all: the documented
        degraded mode that keeps the frozen decision-day set."""
        if not self.shortable_by_date:
            return self.shortable_codes
        return self.shortable_by_date.get(self.current_date)

    def _short_inventory_reject(self, ts_code: str) -> str | None:
        mode = self.profile.short_inventory_mode
        if mode == "proxy_margin_secs":
            shortable = self._fill_day_shortable()
            if shortable is None:
                return "margin_secs_data_missing"
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

    def _single_name_cap_reject(self, ts_code: str, shares: int, raw_price: float) -> str | None:
        """Reject an opening order that would breach the combined single-name cap."""
        if self.profile.max_single_name_weight is None:
            return None
        if raw_price <= 0:
            return "single_name_weight_cap"
        cap_notional = self.profile.max_single_name_weight * self.initial_equity
        held_notional = sum(
            pos.quantity * raw_price
            for state in self.accounts.values()
            if (pos := state.positions.get(ts_code)) is not None
        )
        if held_notional + shares * raw_price > cap_notional + 1e-6:
            return "single_name_weight_cap"
        return None

    @staticmethod
    def validate_share_amount(amount: object, ts_code: str = "") -> tuple[int, str | None]:
        if amount is None or str(amount).strip() == "":
            return 0, "amount_below_lot_size"
        try:
            value = float(amount)
        except (TypeError, ValueError):
            return 0, "invalid_amount"
        if not math.isfinite(value):
            return 0, "invalid_amount"
        rounded = round(value)
        if abs(value - rounded) > 1e-9:
            return 0, "invalid_amount"
        shares = int(rounded)
        if is_star_market(ts_code):
            if shares < STAR_MIN_LOT_SIZE:
                return 0, "amount_below_lot_size"
            return shares, None
        if is_bse_market(ts_code):
            # BSE: minimum 100 shares, then 1-share increments (no 100-lot multiple rule).
            if shares < LOT_SIZE:
                return 0, "amount_below_lot_size"
            return shares, None
        if shares < LOT_SIZE:
            return 0, "amount_below_lot_size"
        if shares % LOT_SIZE != 0:
            return 0, "amount_not_lot_aligned"
        return shares, None

    @staticmethod
    def _strict_lot_amount(amount: object, ts_code: str = "") -> tuple[int, str | None]:
        return SimBroker.validate_share_amount(amount, ts_code)

    def _short_proceeds_locked(self) -> float:
        """Net short-sale proceeds held as locked collateral (融券卖出所得资金) in
        the credit account.

        Banked into its ``cash`` when the short opens and released proportionally
        on cover (``pos.entry_cost`` for a short tracks exactly this). They may
        only fund 买券还券, never new positions or transfers, so they are excluded
        from the credit account's ``available_cash``."""
        return sum(pos.entry_cost for pos in self.credit.positions.values() if pos.side == "short")

    def _fin_shares_outstanding(self, ts_code: str) -> int:
        return sum(
            int(contract.shares)
            for contract in self.credit.contracts
            if contract.kind == "fin" and contract.ts_code == str(ts_code) and not contract.closed
        )

    def available_cash(
        self,
        account: str,
        *,
        pending_orders: Iterable[object] = (),
        exclude_order_id: str | None = None,
    ) -> float:
        """One account's cash deployable for buys (and outbound transfers):
        literal cash, minus the locked 融券 proceeds on the credit side. Margin is
        a separate computed constraint for credit ops. Already-submitted unfilled
        cash orders reserve cash here; the currently matching order can be excluded
        so it does not freeze itself during settlement."""
        state = self.accounts[self._normalize_account(account)]
        cash = state.cash
        if state.name == "credit":
            cash -= self._short_proceeds_locked()
        return max(
            cash - self._reserved_cash(
                state.name, pending_orders=pending_orders, exclude_order_id=exclude_order_id
            ),
            0.0,
        )

    def sellable_quantity(
        self,
        account: str,
        ts_code: str,
        *,
        pending_orders: Iterable[object] = (),
        exclude_order_id: str | None = None,
    ) -> int:
        """Shares available for a new reduce/close after pending reduce orders."""
        state = self.accounts[self._normalize_account(account)]
        pos = state.positions.get(str(ts_code))
        if pos is None:
            return 0
        return max(
            int(pos.sellable_quantity)
            - self._reserved_shares(
                state.name, str(ts_code), pending_orders=pending_orders, exclude_order_id=exclude_order_id
            ),
            0,
        )

    def financed_shares_outstanding(self, ts_code: str) -> int:
        """Open 融资 shares attributed to this code."""
        return self._fin_shares_outstanding(str(ts_code))

    def _reservation_orders(
        self,
        *,
        pending_orders: Iterable[object] = (),
        exclude_order_id: str | None = None,
    ) -> list[dict[str, object]]:
        excluded = str(exclude_order_id or "")
        records: list[dict[str, object]] = []
        for order in self._book:
            if excluded and order.order_id == excluded:
                continue
            records.append(order.to_record())
        for item in pending_orders or ():
            if isinstance(item, WorkingOrder):
                record = item.to_record()
            elif isinstance(item, dict):
                record = dict(item)
            else:
                continue
            if excluded and str(record.get("order_id") or "") == excluded:
                continue
            records.append(record)
        return records

    def _reserved_cash(
        self,
        account: str,
        *,
        pending_orders: Iterable[object] = (),
        exclude_order_id: str | None = None,
    ) -> float:
        total = 0.0
        for record in self._reservation_orders(pending_orders=pending_orders, exclude_order_id=exclude_order_id):
            action, order_account = self._reservation_action_account(record)
            if order_account != account or action not in {"buy", "credit_buy"}:
                continue
            shares = self._reservation_shares(record)
            price = self._reservation_price(record)
            if shares <= 0 or price is None:
                continue
            fill = project_open(
                self.profile.cost_model,
                side="long",
                raw_price=price,
                shares=shares,
                trade_date=str(self.current_date or record.get("trade_date") or ""),
                apply_slippage=not self._reservation_is_limit(record),
            )
            total += fill.required_cash
        return total

    def _reserved_bail(
        self,
        *,
        pending_orders: Iterable[object] = (),
        exclude_order_id: str | None = None,
    ) -> float:
        total = 0.0
        for record in self._reservation_orders(pending_orders=pending_orders, exclude_order_id=exclude_order_id):
            action, order_account = self._reservation_action_account(record)
            if order_account != "credit" or action not in {"fin_buy", "short"}:
                continue
            shares = self._reservation_shares(record)
            price = self._reservation_price(record)
            if shares <= 0 or price is None:
                continue
            if action == "fin_buy":
                fill = project_open(
                    self.profile.cost_model,
                    side="long",
                    raw_price=price,
                    shares=shares,
                    trade_date=str(self.current_date or record.get("trade_date") or ""),
                    apply_slippage=not self._reservation_is_limit(record),
                    financed=True,
                )
                total += fill.cost_basis * self.profile.fin_margin_ratio + fill.fee
            else:
                fill = project_open(
                    self.profile.cost_model,
                    side="short",
                    raw_price=price,
                    shares=shares,
                    trade_date=str(self.current_date or record.get("trade_date") or ""),
                    apply_slippage=not self._reservation_is_limit(record),
                )
                total += fill.required_cash
        return total

    def _reserved_shares(
        self,
        account: str,
        ts_code: str,
        *,
        pending_orders: Iterable[object] = (),
        exclude_order_id: str | None = None,
    ) -> int:
        total = 0
        for record in self._reservation_orders(pending_orders=pending_orders, exclude_order_id=exclude_order_id):
            action, order_account = self._reservation_action_account(record)
            if order_account != account or str(record.get("ts_code") or "") != ts_code:
                continue
            if action not in {"sell", "credit_sell", "cover", "sell_repay"}:
                continue
            total += max(self._reservation_shares(record), 0)
        return total

    def _reservation_action_account(self, record: dict[str, object]) -> tuple[str, str]:
        action = str(record.get("action") or "").strip().lower()
        account = str(record.get("account") or "").strip().lower()
        if account not in ACCOUNT_TYPES:
            try:
                account, _op = self.account_op_for_action(action)
            except ValueError:
                account = ""
        return action, account

    @staticmethod
    def _reservation_shares(record: dict[str, object]) -> int:
        raw = record.get("order_volume", record.get("amount", record.get("volume")))
        try:
            return max(int(float(raw)), 0)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _reservation_price(record: dict[str, object]) -> float | None:
        for key in ("price", "limit", "reserve_price", "_reserve_price"):
            value = record.get(key)
            if value not in (None, ""):
                try:
                    price = float(value)
                except (TypeError, ValueError):
                    return None
                return price if math.isfinite(price) and price > 0 else None
        return None

    @staticmethod
    def _reservation_is_limit(record: dict[str, object]) -> bool:
        if record.get("price_type") is not None:
            try:
                return int(record.get("price_type")) == prtype.FIX
            except (TypeError, ValueError):
                return False
        return record.get("limit") not in (None, "")

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

    def _credit_target_reject(self, ts_code: str, reason: str) -> str | None:
        """信用账户标的池近似 gate; returns the reject reason or None if eligible.

        ``margin_secs`` carries no 担保品/融资/融券 split in the current TuShare
        feed, so credit_buy and fin_buy share the same per-fill-day set as the
        short-side proxy (same fail-closed rule on a per-day data gap).
        ``theoretical_short`` lifts this gate for research runs.
        """
        if self.profile.short_inventory_mode == "theoretical_short":
            return None
        eligible = self._fill_day_shortable()
        if eligible is None:
            return "margin_secs_data_missing"
        return None if str(ts_code) in eligible else reason

    def _direct_repay(self, amount: float, *, order_id: str, reason: str = "", submitted_at: str = "") -> str:
        """直接还款: an immediate cash operation (no order book, no bar matching).
        The requested amount must fit deployable cash and outstanding 融资 debt;
        it is not silently clamped, matching live broker/QMT semantics."""
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
        requested = float(amount)
        if requested <= 1e-9:
            self._reject(order, "amount_below_minimum")
            return order_id
        if requested > self.available_cash("credit") + 1e-6:
            self._reject(order, "insufficient_cash")
            return order_id
        if requested > owed + 1e-6:
            self._reject(order, "amount_exceeds_fin_debt")
            return order_id
        repaid = repay_fin(self.credit.contracts, requested, release_shares=True)
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


def load_shortable_by_date(
    replay_dir: str | Path,
    *,
    trade_dates: tuple[str, ...] | None = None,
) -> dict[str, frozenset[str]]:
    """Per-fill-day margin_secs membership from a replay slot's events domain.

    Maps each replay trade_date to that day's complete shortable set so the broker
    can gate short fills on the real same-day inventory (proxy mode) rather than the
    agent's frozen decision-day snapshot. Empty when the slot carries no events; the
    broker then falls back to the frozen ``shortable_codes`` for every fill day."""
    events_path = Path(replay_dir) / "events.parquet"
    if not events_path.exists():
        return {}
    filters = [("dataset", "==", "margin_secs")]
    if trade_dates:
        filters.append(("trade_date", "in", list(trade_dates)))
    try:
        events = pd.read_parquet(
            events_path,
            columns=["dataset", "trade_date", "ts_code"],
            filters=filters,
        )
    except (KeyError, ValueError):
        # Conservative compatibility for old/malformed fixtures: retain the
        # existing validation path and let missing columns return an empty map.
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


def load_auction_prints_by_date(replay_dir: str | Path) -> dict[tuple[str, str], dict[str, float]]:
    """Exact opening clearing prices from ``stk_auction`` replay rows.

    Pre-coverage or missing rows retain the 09:30 bar-open fallback. Closing
    auctions always clear against the final bar's official close and therefore
    need no separate source table.
    """
    path = Path(replay_dir) / "auction.parquet"
    if not path.exists():
        return {}
    return auction_prints_by_date(pd.read_parquet(path))


def auction_prints_by_date(prints: pd.DataFrame) -> dict[tuple[str, str], dict[str, float]]:
    """Validate one replay auction frame and return Broker-only clearing prices."""
    if prints.empty:
        return {}
    required = {"trade_date", "session", "ts_code", "price"}
    missing = sorted(required.difference(prints.columns))
    if missing:
        raise ValueError(f"auction.parquet missing required columns: {missing}")
    key_columns = ["trade_date", "session", "ts_code"]
    duplicate_mask = prints.duplicated(key_columns, keep=False)
    if duplicate_mask.any():
        sample = prints.loc[duplicate_mask, key_columns].head(5).to_dict("records")
        raise ValueError(f"auction.parquet has duplicate clearing-price keys: {sample}")
    grouped: dict[tuple[str, str], dict[str, float]] = {}
    for row in prints.to_dict("records"):
        session = str(row.get("session") or "")
        if session != "open":
            raise ValueError(f"auction.parquet has invalid session: {session!r}")
        price = row.get("price")
        volume = pd.to_numeric(row.get("vol"), errors="coerce")
        amount = pd.to_numeric(row.get("amount"), errors="coerce")
        quantities_valid = (
            pd.notna(volume)
            and pd.notna(amount)
            and math.isfinite(float(volume))
            and math.isfinite(float(amount))
            and float(volume) >= 0
            and float(amount) >= 0
        )
        if not quantities_valid:
            raise ValueError(
                f"auction.parquet has invalid opening quantities for "
                f"{row.get('trade_date')}/{row.get('ts_code')}: vol={volume!r}, amount={amount!r}"
            )
        if float(volume) == 0 and float(amount) == 0:
            # Quantity truth wins over a stray source price: no matched trade
            # must not create a Broker-only clearing print.
            price = 0.0
        elif float(volume) > 0 and float(amount) > 0:
            recovered = float(amount) / float(volume)
            if not math.isfinite(recovered) or recovered <= 0:
                raise ValueError(
                    f"auction.parquet has unrecoverable opening price for "
                    f"{row.get('trade_date')}/{row.get('ts_code')}"
                )
            if price is None or pd.isna(price) or not math.isfinite(float(price)) or float(price) <= 0:
                price = recovered
            elif not math.isclose(
                float(price),
                recovered,
                rel_tol=1e-9,
                abs_tol=STK_AUCTION_PRICE_ABS_TOLERANCE,
            ):
                raise ValueError(
                    f"auction.parquet has inconsistent opening price for "
                    f"{row.get('trade_date')}/{row.get('ts_code')}: "
                    f"price={price!r}, amount/vol={recovered!r}"
                )
        else:
            raise ValueError(
                f"auction.parquet has inconsistent opening result for "
                f"{row.get('trade_date')}/{row.get('ts_code')}: "
                f"price={price!r}, vol={volume!r}, amount={amount!r}"
            )
        key = (str(row.get("trade_date") or ""), session)
        grouped.setdefault(key, {})[str(row.get("ts_code") or "")] = float(price)
    return grouped


def load_corporate_actions_by_date(
    replay_dir: str | Path,
    *,
    trade_dates: tuple[str, ...] | None = None,
) -> dict[str, list[dict[str, object]]]:
    """Ex-date corporate actions from a replay slot, keyed by ex_date.

    Environment-side market truth consumed by SimBroker at ``roll_to_date`` (the
    agent's dividend visibility stays announcement-gated through the PIT
    fundamental events). Empty when the slot predates the corporate_actions
    domain; the broker then applies none — matching that slot's build-time world."""
    path = Path(replay_dir) / "corporate_actions.parquet"
    if not path.exists():
        return {}
    actions = pd.read_parquet(
        path,
        filters=[("ex_date", "in", list(trade_dates))] if trade_dates else None,
    )
    if actions.empty:
        return {}
    grouped: dict[str, list[dict[str, object]]] = {}
    for row in actions.to_dict("records"):
        grouped.setdefault(str(row.get("ex_date") or ""), []).append(row)
    return grouped


def _bars_for_codes(minute_group: pd.DataFrame, ts_codes: set[str]) -> dict[str, pd.Series]:
    """Build one per-tick lookup for only working-order and held-position codes.

    Duplicate prints retain the legacy behavior: the last row for a code wins.
    The full universe code column is normalized/scanned once, rather than once per
    order and twice per position.
    """
    if minute_group.empty or not ts_codes:
        return {}
    normalized_codes = minute_group["ts_code"].astype(str)
    selected_mask = normalized_codes.isin(ts_codes)
    if not bool(selected_mask.any()):
        return {}
    selected_codes = normalized_codes.loc[selected_mask].tolist()
    selected_rows = minute_group.loc[selected_mask]
    bars: dict[str, pd.Series] = {}
    for code, (_, row) in zip(selected_codes, selected_rows.iterrows()):
        bars[code] = row
    return bars


def _date_gap(start_date: str, end_date: str) -> int:
    try:
        start = datetime.strptime(str(start_date), "%Y%m%d")
        end = datetime.strptime(str(end_date), "%Y%m%d")
    except ValueError:
        return 0
    return max(0, (end - start).days)


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


def _is_synthetic_bar(bar: pd.Series) -> bool:
    flag = bar.get("synthetic")
    return flag is not None and pd.notna(flag) and bool(flag)


def _limit_fill_price(
    order: WorkingOrder, bar: pd.Series, *, use_close: bool = False, ref_override: float | None = None
) -> float | None:
    """Fill price for a working order against this bar, or None if not fillable now.

    Market orders fill at the bar reference price: the bar OPEN by default, or the
    bar CLOSE for a close (15:00) call-auction order (``use_close``) so a 14:57
    close-auction decision settles at the day's close, not its open. A limit order
    fills only when the bar trades THROUGH the limit: buy/cover orders fill at the
    reference price when it is already at or below the limit, otherwise at the limit
    when the bar low goes STRICTLY below it — a bare touch (low == limit) leaves the
    resting order in the unmodelled queue at that price, unfilled. Sell/short orders
    are symmetric (high strictly above the limit).

    Two cases clear at ONE price (the reference) with no range trade-through: a
    close call auction, which by rule matches every order at the single auction
    price, and a synthetic daily-fallback bar, whose high/low span the whole
    session — hours before the order could exist — so filling against them would
    be retroactive."""
    # ref_override = the day's ACTUAL call-auction print (replay slot data): it
    # replaces the bar-derived reference and forces single-price clearing,
    # because a call auction matches every order at exactly that price.
    ref_price = ref_override if ref_override is not None else (
        _close_price(bar) if use_close else _open_price(bar)
    )
    if order.price is None or ref_price is None:
        return ref_price
    single_price = use_close or ref_override is not None or _is_synthetic_bar(bar)
    limit = order.price
    if order.action in ("buy", "credit_buy", "fin_buy", "cover"):
        if ref_price <= limit:
            return ref_price
        if single_price:
            return None
        low = bar.get("low")
        return limit if (low is not None and pd.notna(low) and float(low) < limit) else None
    if ref_price >= limit:
        return ref_price
    if single_price:
        return None
    high = bar.get("high")
    return limit if (high is not None and pd.notna(high) and float(high) > limit) else None


class TraderProtocol(Protocol):
    """The official-QMT-aligned surface that the backtest ``SimBroker`` and a live
    adapter (``QMTBroker``) both expose, so order plumbing is backend-agnostic.

    Methods mirror the in-client strategy API (``passorder``/``cancel``/
    ``get_trade_detail_data`` plus the credit queries); the record-field mapping to
    the official ``m_*`` object attributes is tabled in
    docs/environment_design.md §3.4. ``passorder`` returns the order id the
    official flow recovers via ``get_last_order_id`` immediately after submitting
    with a unique ``user_order_id`` (投资备注) — a live adapter implements exactly
    that pair."""

    def passorder(self, op_type: int, order_type: int, account_id: str, order_code: str, pr_type: int, price: float | None, volume: int | None, **kwargs: object) -> str: ...
    def cancel(self, order_id: str, account_id: str = "", account_type: str = "", **kwargs: object) -> bool: ...
    def get_trade_detail_data(self, account_id: str = "", account_type: str = "", data_type: str = "ORDER", strategy_name: str = "") -> list[dict[str, object]]: ...
    def get_debt_contract(self, account_id: str = "") -> list[dict[str, object]]: ...
    def get_assure_contract(self, account_id: str = "") -> list[dict[str, object]]: ...
    def get_enable_short_contract(self, account_id: str = "") -> list[dict[str, object]]: ...
