"""Dependency-light deterministic fill-projection and credit-math core
(docs/environment_design.md §3).

Pure stdlib (no ``autotrade``/``pandas`` import), but host-only: this module is NOT
shipped into the Agent sandbox image. Its single consumer is the authoritative host
:class:`~autotrade.environment.broker.SimBroker`, which projects every order's
money/share outcome from the functions here (commission, stamp duty, slippage, lot
sizing, the open/reduce cash deltas) and delegates the credit-account math to the
``DebtContract`` helpers (interest accrual, FIFO repayment, 保证金可用余额 and
维持担保比例 per the exchange 融资融券交易实施细则). Only this deterministic math
lives here; bar-level gates (suspension, price limits, shortable inventory, T+1
sellable) and position bookkeeping stay with the broker, which holds the market
data and position state.

Credit formulas follow the SSE/SZSE 融资融券交易实施细则 (see the SSE reader at
https://www.sse.com.cn/services/tradingservice/margin/edu/c/10074042/files/a1f1c4833302451fb9130dbb94116c56.pdf):

* 维持担保比例 = (现金 + 信用账户证券市值合计)
  / (融资买入金额 + 融券卖出证券数量×市价 + 利息及费用合计)
* 保证金可用余额 = 现金 + Σ(充抵保证金证券市值×折算率)
  + Σ[(融资买入证券市值 − 融资买入金额)×折算率]
  + Σ[(融券卖出金额 − 融券卖出证券市值)×折算率]
  − Σ融券卖出金额 − Σ融资买入金额×融资保证金比例
  − Σ融券卖出证券市值×融券保证金比例 − 利息及费用
  (浮亏侧折算率按 100% 计——细则规定融资市值低于买入金额、或融券市值高于卖出金额时
  该项按 100% 折算全额扣减。)

The sandbox driver (``main_ctx_driver.py`` — the only module baked into the image) is
deliberately stdlib-only and does NO intra-tick fill projection: within a tick the
agent sees the cash/positions filled as of the ENTERING tick, and the host settles
the resulting orders on the next tick. There is no in-sandbox broker view projecting
from this core, so nothing here needs to stay in sync with sandbox-visible state.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

LOT_SIZE = 100
STAR_MIN_LOT_SIZE = 200
STAMP_DUTY_CUTOVER = "20230828"  # sell-side stamp duty halved to 0.05% from this date
INTEREST_DAY_COUNT = 360.0

# After-hours fixed-price trading (盘后固定价格交易, 15:05-15:30 at the closing
# price) effective dates by board: STAR at the board's launch, ChiNext with the
# 2020 registration reform, every remaining A-share (incl. BSE) with the
# 沪深北交易所《交易规则（2026年修订）》 effective 2026-07-06.
AFTERHOURS_START_STAR = "20190722"
AFTERHOURS_START_CHINEXT = "20200824"
AFTERHOURS_START_ALL = "20260706"


def is_star_market(ts_code: str) -> bool:
    code = str(ts_code).upper()
    return code.endswith(".SH") and code[:3] in {"688", "689"}


def is_bse_market(ts_code: str) -> bool:
    return str(ts_code).upper().endswith(".BJ")


def reduce_amount_reject(shares: int, sellable: int, ts_code: str) -> str | None:
    """Sell-side lot rule for a positive ``shares`` request against ``sellable``.

    Exchange rules let a holder declare whole lots, or one declaration that carries
    the ENTIRE sub-lot odd tail (零股必须一次性申报卖出) — corporate actions (送转)
    legitimately create odd positions, so reduces cannot reuse the strict buy
    ladder. STAR/BSE positions below their minimum declaration are likewise
    exitable only in full."""
    if is_star_market(ts_code):
        return None if shares >= STAR_MIN_LOT_SIZE or shares == sellable else "amount_below_lot_size"
    if is_bse_market(ts_code):
        return None if shares >= LOT_SIZE or shares == sellable else "amount_below_lot_size"
    if shares % LOT_SIZE == 0:
        return None
    odd = sellable % LOT_SIZE
    if odd and shares % LOT_SIZE == odd and shares <= sellable:
        return None
    return "amount_not_lot_aligned" if shares >= LOT_SIZE else "amount_below_lot_size"


def afterhours_available(ts_code: str, trade_date: str) -> bool:
    """Whether the code's board offers after-hours fixed-price trading on this date."""
    date = str(trade_date)
    if is_star_market(ts_code):
        return date >= AFTERHOURS_START_STAR
    code = str(ts_code).upper()
    if code.endswith(".SZ") and code.startswith("30"):
        return date >= AFTERHOURS_START_CHINEXT
    return date >= AFTERHOURS_START_ALL


@dataclass(frozen=True)
class CostModel:
    """The deterministic cost parameters that turn a raw price + share count into a
    fill price, commission, stamp duty, and 融券 margin (a flattened view of the
    profile's economics, with ``slo_margin_ratio`` already resolved)."""

    commission_bps: float = 1.0
    min_commission_cny: float = 5.0
    stamp_duty_sell_bps_before_cutover: float = 10.0
    stamp_duty_sell_bps_from_cutover: float = 5.0
    transfer_fee_bps: float = 0.1  # 过户费 0.01‰ = 0.1 bps, both buy and sell side.
    slippage_bps: float = 5.0
    slo_margin_ratio: float = 1.0

    def commission(self, notional: float) -> float:
        return max(notional * self.commission_bps / 10_000.0, self.min_commission_cny)

    def transfer_fee(self, notional: float) -> float:
        return notional * self.transfer_fee_bps / 10_000.0

    def trade_fee(self, notional: float) -> float:
        return self.commission(notional) + self.transfer_fee(notional)

    def stamp_duty_on_sale(self, notional: float, trade_date: str) -> float:
        bps = (
            self.stamp_duty_sell_bps_from_cutover
            if str(trade_date) >= STAMP_DUTY_CUTOVER
            else self.stamp_duty_sell_bps_before_cutover
        )
        return notional * bps / 10_000.0

    def slipped_price(self, price: float, *, is_buy: bool) -> float:
        slip = self.slippage_bps / 10_000.0
        return price * (1.0 + slip) if is_buy else price * (1.0 - slip)


@dataclass(frozen=True)
class OpenFill:
    """Deterministic projection of opening a position (``buy`` long / ``short``)."""

    side: str  # "long" | "short"
    price: float
    shares: int
    fee: float
    duty: float
    margin: float
    notional: float
    # Signed change applied to literal cash on fill.
    cash_delta: float
    # Deployable cash that must be available to accept the order.
    required_cash: float
    # entry cost basis recorded on the position: notional+fee for a long buy; for a
    # short, the net proceeds banked and locked as collateral (== entry_cost).
    cost_basis: float


def clamp_to_limit_band(price: float, band: tuple[float | None, float | None] | None) -> float:
    """A-share fills cannot print outside the daily price band: slippage is a
    liquidity assumption and must saturate at the band edge, never breach it."""
    if not band:
        return price
    down, up = band
    if up is not None and price > up:
        return up
    if down is not None and price < down:
        return down
    return price


def project_open(
    cost: CostModel,
    *,
    side: str,
    raw_price: float,
    shares: int,
    trade_date: str,
    apply_slippage: bool = True,
    financed: bool = False,
    band: tuple[float | None, float | None] | None = None,
) -> OpenFill:
    """Project a long buy (cash or 融资), or a 融券 short open, at ``raw_price``.

    A cash/担保品 buy deploys available cash (``required_cash = notional + fee``). A
    ``financed`` (融资) buy moves no cash at open — the notional plus fee become the
    debt contract's principal (``cost_basis``) and the broker gates it on 保证金可用
    余额, so ``required_cash`` is 0 here. A short banks its net proceeds into cash but
    locks them plus margin (``required_cash = margin + fee + duty``); its locked
    collateral is the net proceeds. ``apply_slippage`` is True for marketable fills,
    False for limit/auction fills (where ``raw_price`` is already the fill price)."""
    is_buy = side == "long"
    price = clamp_to_limit_band(
        cost.slipped_price(raw_price, is_buy=is_buy) if apply_slippage else float(raw_price), band
    )
    notional = shares * price
    fee = cost.trade_fee(notional)
    if side == "long":
        return OpenFill(
            side="long", price=price, shares=shares, fee=fee, duty=0.0, margin=0.0,
            notional=notional,
            cash_delta=0.0 if financed else -(notional + fee),
            required_cash=0.0 if financed else notional + fee,
            cost_basis=notional + fee,
        )
    duty = cost.stamp_duty_on_sale(notional, trade_date)
    margin = notional * cost.slo_margin_ratio
    proceeds = notional - fee - duty
    return OpenFill(
        side="short", price=price, shares=shares, fee=fee, duty=duty, margin=margin,
        notional=notional, cash_delta=proceeds, required_cash=margin + fee + duty,
        cost_basis=proceeds,
    )


@dataclass(frozen=True)
class ReduceFill:
    """Deterministic projection of reducing a position (``sell`` long / ``cover`` short)."""

    side: str  # the position side being reduced
    price: float
    shares: int
    fee: float
    duty: float
    notional: float
    # Signed change applied to literal cash on fill.
    cash_delta: float


def project_reduce(
    cost: CostModel,
    *,
    side: str,
    raw_price: float,
    shares: int,
    trade_date: str,
    apply_slippage: bool = True,
) -> ReduceFill:
    """Project reducing a ``side`` position by ``shares`` lots at ``raw_price``.

    Selling a long banks ``notional - fee - duty``; covering a short pays
    ``notional + fee`` (a buy, so slippage is on the buy side)."""
    is_buy = side == "short"  # covering a short is a buy
    price = cost.slipped_price(raw_price, is_buy=is_buy) if apply_slippage else float(raw_price)
    notional = shares * price
    fee = cost.trade_fee(notional)
    if side == "long":
        duty = cost.stamp_duty_on_sale(notional, trade_date)
        cash_delta = notional - fee - duty
    else:
        duty = 0.0
        cash_delta = -(notional + fee)
    return ReduceFill(side=side, price=price, shares=shares, fee=fee, duty=duty, notional=notional, cash_delta=cash_delta)


# ---- credit account (信用账户) math ----


@dataclass
class DebtContract:
    """One open 融资/融券 负债合约 (the sim's minimal StkCompacts mapping).

    ``kind="fin"``: ``principal`` is the outstanding financed amount (open notional
    plus the financed open fee); ``shares`` are the financed shares still attributed
    to this contract — used for the 保证金可用余额 浮盈/浮亏 term and clamped to the
    held position when marked (plain 担保品卖出 may sell financed shares without
    repaying; the formula then books the full 浮亏).

    ``kind="slo"``: ``shares`` are the borrowed shares outstanding and
    ``sell_amount`` the gross 融券卖出金额 still outstanding (the formula subtracts
    it from cash — the sim banked the NET proceeds, so this is conservative by fees).

    Interest accrues per CALENDAR day into ``interest_accrued`` and is paid from
    cash at repayment, interest first (先息后本).
    """

    compact_id: str
    kind: str  # "fin" | "slo"
    ts_code: str
    open_date: str
    open_price: float
    year_rate: float
    principal: float = 0.0
    shares: int = 0
    sell_amount: float = 0.0
    interest_accrued: float = 0.0
    last_accrual_date: str = ""
    last_extension_date: str = ""
    extension_count: int = 0
    business_balance: float = 0.0  # original principal (fin) / sell amount (slo)
    business_vol: int = 0  # original shares

    @property
    def closed(self) -> bool:
        return self.principal <= 1e-9 and self.shares <= 0 and self.interest_accrued <= 1e-9

    def to_record(self) -> dict[str, object]:
        return {
            "compact_id": self.compact_id,
            "compact_type": self.kind,
            "ts_code": self.ts_code,
            "open_date": self.open_date,
            "open_price": self.open_price,
            "year_rate": self.year_rate,
            "real_compact_balance": self.principal,
            "real_compact_vol": self.shares,
            "sell_amount": self.sell_amount,
            "compact_interest": self.interest_accrued,
            "last_extension_date": self.last_extension_date,
            "extension_count": self.extension_count,
            "business_balance": self.business_balance,
            "business_vol": self.business_vol,
            "compact_status": (
                "open"
                if self.principal >= self.business_balance - 1e-9 and self.shares >= self.business_vol
                else "partial"
            ),
        }


def calendar_day_gap(prev_date: str, trade_date: str) -> int:
    """Calendar days of interest accrual between marks: 1 on the first mark
    (``prev_date`` empty), otherwise the calendar-day delta (weekends and holidays
    included); 0 when re-marked on the same date (idempotent)."""
    if not prev_date:
        return 1
    prev = datetime.strptime(str(prev_date), "%Y%m%d")
    cur = datetime.strptime(str(trade_date), "%Y%m%d")
    return max(0, (cur - prev).days)


def accrue_debt_interest(contracts: list[DebtContract], trade_date: str) -> float:
    """Accrue per-calendar-day interest on every open contract; returns the delta.

    融资 interest accrues on the outstanding principal; 融券 fee accrues on the
    outstanding borrowed shares at their open (sell) price — the standard
    financing-cost convention. Nothing is debited from cash here; accrued interest
    is carried on the contract until repayment and enters both credit formulas."""
    total = 0.0
    for contract in contracts:
        if contract.principal <= 1e-9 and contract.shares <= 0:
            continue
        gap = calendar_day_gap(contract.last_accrual_date, trade_date)
        contract.last_accrual_date = str(trade_date)
        if gap <= 0:
            continue
        base = contract.principal if contract.kind == "fin" else contract.shares * contract.open_price
        fee = base * contract.year_rate / INTEREST_DAY_COUNT * gap
        contract.interest_accrued += fee
        total += fee
    return total


def _open_fifo(contracts: list[DebtContract], kind: str, ts_code: str | None = None) -> list[DebtContract]:
    return sorted(
        (
            contract
            for contract in contracts
            if contract.kind == kind
            and not contract.closed
            and (ts_code is None or contract.ts_code == str(ts_code))
        ),
        key=lambda contract: (contract.open_date, contract.compact_id),
    )


def repay_fin(
    contracts: list[DebtContract],
    amount: float,
    *,
    release_shares: bool,
) -> dict[str, float]:
    """Apply a cash ``amount`` to 融资 contracts FIFO, interest first then principal.

    ``release_shares=True`` (直接还款) releases each contract's financed shares in
    proportion to the principal repaid — they become ordinary collateral.
    ``release_shares=False`` (卖券还款) leaves shares untouched because the sale
    itself already reduced them via :func:`release_fin_shares`. Returns the applied
    totals; the caller debits cash by ``applied``."""
    remaining = max(0.0, float(amount))
    interest_paid = principal_paid = 0.0
    for contract in _open_fifo(contracts, "fin"):
        if remaining <= 1e-9:
            break
        pay_interest = min(contract.interest_accrued, remaining)
        contract.interest_accrued -= pay_interest
        remaining -= pay_interest
        interest_paid += pay_interest
        if remaining <= 1e-9 or contract.principal <= 1e-9:
            continue
        pay_principal = min(contract.principal, remaining)
        if release_shares and contract.principal > 0:
            released = int(round(contract.shares * pay_principal / contract.principal))
            contract.shares = max(0, contract.shares - released)
        contract.principal -= pay_principal
        remaining -= pay_principal
        principal_paid += pay_principal
        if contract.principal <= 1e-9:
            contract.shares = 0
    applied = interest_paid + principal_paid
    return {"applied": applied, "interest_paid": interest_paid, "principal_paid": principal_paid}


def release_fin_shares(contracts: list[DebtContract], ts_code: str, shares: int) -> int:
    """Attribute ``shares`` sold via 卖券还款 to the code's 融资 contracts FIFO."""
    remaining = max(0, int(shares))
    released = 0
    for contract in _open_fifo(contracts, "fin", ts_code):
        if remaining <= 0:
            break
        take = min(contract.shares, remaining)
        contract.shares -= take
        remaining -= take
        released += take
    return released


def scale_slo_contracts(contracts: list[DebtContract], ts_code: str, bonus_per_share: float) -> int:
    """Grow a code's open 融券 contracts for an ex-date 送转 (share bonus/conversion).

    The lender is owed the post-conversion share count, so each contract's
    outstanding ``shares`` grow by ``floor(shares * bonus_per_share)``;
    ``open_price`` is rebased so ``shares * open_price`` (the 融券费 accrual basis
    and the bail-balance fallback mark) is unchanged. Returns the total shares
    added, which the caller adds to the short position to keep the
    position/contract share invariant."""
    added_total = 0
    for contract in contracts:
        if contract.kind != "slo" or contract.closed or contract.ts_code != str(ts_code):
            continue
        added = int(contract.shares * bonus_per_share)
        if added <= 0:
            continue
        contract.open_price = contract.open_price * contract.shares / (contract.shares + added)
        contract.shares += added
        added_total += added
    return added_total


def repay_slo(contracts: list[DebtContract], ts_code: str, shares: int) -> dict[str, float]:
    """Repay ``shares`` of borrowed stock (买券还券 or liquidation) FIFO.

    Reduces the code's 融券 contracts and releases their gross sell amount
    proportionally; the repaid fraction's accrued interest falls due now (the
    caller debits it from cash). Returns ``shares_repaid`` and ``interest_due``."""
    remaining = max(0, int(shares))
    shares_repaid = 0
    interest_due = 0.0
    for contract in _open_fifo(contracts, "slo", ts_code):
        if remaining <= 0:
            break
        take = min(contract.shares, remaining)
        if take <= 0:
            continue
        fraction = take / contract.shares
        interest_due += contract.interest_accrued * fraction
        contract.interest_accrued -= contract.interest_accrued * fraction
        contract.sell_amount -= contract.sell_amount * fraction
        contract.shares -= take
        remaining -= take
        shares_repaid += take
    return {"shares_repaid": float(shares_repaid), "interest_due": interest_due}


def enable_bail_balance(
    cash: float,
    collateral_mv: float,
    fin_terms: list[tuple[float, float]],
    slo_terms: list[tuple[float, float]],
    interest_total: float,
    *,
    assure_ratio: float,
    fin_margin_ratio: float,
    slo_margin_ratio: float,
) -> float:
    """保证金可用余额 per the exchange 实施细则 formula (module docstring).

    ``cash`` is the literal credit-account cash (it includes banked 融券 net
    proceeds; the formula's −Σ融券卖出金额 term removes the gross amount, so the
    sim is conservative by the open fees). ``collateral_mv`` is the market value of
    long shares NOT attributed to 融资 contracts. ``fin_terms`` are per-contract
    ``(融资买入证券市值, 融资买入金额)`` pairs; ``slo_terms`` are per-contract
    ``(融券卖出金额, 融券卖出证券市值)`` pairs. 浮亏 terms are folded at 100% per
    the 细则; ``assure_ratio`` is the flat 担保品折算率 approximation."""
    bail = cash + collateral_mv * assure_ratio
    for fin_mv, fin_amount in fin_terms:
        gain = fin_mv - fin_amount
        bail += gain * (assure_ratio if gain >= 0 else 1.0)
        bail -= fin_amount * fin_margin_ratio
    for slo_amount, slo_mv in slo_terms:
        gain = slo_amount - slo_mv
        bail += gain * (assure_ratio if gain >= 0 else 1.0)
        bail -= slo_amount
        bail -= slo_mv * slo_margin_ratio
    return bail - interest_total


def credit_maintenance_ratio(
    cash: float,
    securities_mv: float,
    fin_amount_total: float,
    slo_mv_total: float,
    interest_total: float,
) -> float | None:
    """维持担保比例 = (现金 + 证券市值) / (融资金额 + 融券市值 + 利息费用);
    None when the account carries no credit debt."""
    debt = fin_amount_total + slo_mv_total + interest_total
    if debt <= 1e-9:
        return None
    return (cash + securities_mv) / debt
