import stat
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from autotrade.environment.broker import (
    BrokerProfile,
    MarketData,
    SimBroker,
    load_auction_prints_by_date,
    optype,
    prtype,
)
from autotrade.environment.main_ctx_engine import BacktestError, MainPolicyRunner, run_main_ctx_replay
from autotrade.environment.replay_stats import ReplayResult, compute_return_stats
from autotrade.environment.sandbox import hide_snapshot_slots_from_agent
from autotrade.environment.runtime import SandboxPaths
from autotrade.environment.tools.backtest import _profile_kwargs


def _held(state, code):
    return next((p for p in state["positions"] if str(p["ts_code"]) == code), None)


def _positions(broker, account=None):
    accounts = ("stock", "credit") if account is None else (account,)
    return [
        row
        for name in accounts
        for row in broker.get_trade_detail_data(account_type=name, data_type="POSITION")
    ]


def _asset(broker, account):
    return broker.get_trade_detail_data(account_type=account, data_type="ACCOUNT")[0]


def _deals(broker, code):
    return [
        t
        for name in ("stock", "credit")
        for t in broker.get_trade_detail_data(account_type=name, data_type="DEAL")
        if t["ts_code"] == code
    ]


DECISION = "2022-01-04T09:25:00+08:00"


def make_daily(rows):
    return pd.DataFrame(rows, columns=["trade_date", "ts_code", "open", "close", "up_limit", "down_limit", "is_suspended"])


REPLAY = make_daily(
    [
        ("20220104", "000001.SZ", 10.0, 10.5, 11.0, 9.0, False),
        ("20220105", "000001.SZ", 10.6, 11.0, 11.6, 9.5, False),
        ("20220106", "000001.SZ", 11.1, 11.5, 12.1, 10.0, False),
        ("20220104", "000002.SZ", 20.0, 19.5, 22.0, 18.0, False),
        ("20220105", "000002.SZ", 19.4, 19.0, 21.5, 17.6, False),
        ("20220106", "000002.SZ", 18.9, 18.0, 21.0, 17.2, False),
    ]
)

MINUTE_REPLAY = pd.DataFrame(
    [
        {"trade_date": "20220104", "ts_code": "000001.SZ", "trade_time": "09:31", "open": 10.0, "high": 10.20, "low": 10.05, "close": 10.20},
        {"trade_date": "20220104", "ts_code": "000001.SZ", "trade_time": "09:32", "open": 10.20, "high": 10.25, "low": 10.10, "close": 10.18},
        {"trade_date": "20220104", "ts_code": "000001.SZ", "trade_time": "14:57", "open": 10.20, "high": 10.30, "low": 10.15, "close": 10.25},
        {"trade_date": "20220105", "ts_code": "000001.SZ", "trade_time": "09:31", "open": 10.50, "high": 10.60, "low": 10.40, "close": 10.60},
        {"trade_date": "20220105", "ts_code": "000001.SZ", "trade_time": "10:30", "open": 10.60, "high": 10.90, "low": 10.55, "close": 10.85},
        {"trade_date": "20220106", "ts_code": "000001.SZ", "trade_time": "09:31", "open": 11.20, "high": 11.30, "low": 11.10, "close": 11.20},
    ]
)


class FakeMainPolicy:
    """Drives the replay engine without a sandbox: a plain
    ``func(state) -> list[action]`` stands in for main(ctx) so we can exercise
    the Broker primitives directly."""

    def __init__(self, fn) -> None:
        self.fn = fn

    def validate_main(self) -> None:
        return None

    def step(self, state: dict[str, object]) -> list[dict[str, object]]:
        return list(self.fn(state) or [])


class BrokerPrimitiveTest(unittest.TestCase):
    def make_broker(self, *, shortable=("000001.SZ", "000002.SZ"), mode="proxy_margin_secs", daily=REPLAY, **profile_kw):
        profile = BrokerProfile(short_inventory_mode=mode, **profile_kw)
        return SimBroker(profile, MarketData(daily), shortable_codes=frozenset(shortable))

    def test_long_buy_hold_and_close(self):
        broker = self.make_broker()
        order = broker.execute("000001.SZ", "buy", trade_date="20220104", raw_price=10.0, amount=1000)
        self.assertEqual(order.status, "filled")
        self.assertEqual(order.account, "stock")
        self.assertEqual(broker.position_quantity("000001.SZ"), 1000)
        broker.mark_to_market("20220105")
        broker.mark_to_market("20220106")
        broker.close_all("20220106")
        self.assertEqual(broker.stock.positions, {})
        self.assertGreater(broker.equity(), broker.initial_equity)

    def test_order_amount_must_be_exact_lot(self):
        broker = self.make_broker()
        odd_lot = broker.execute("000001.SZ", "buy", trade_date="20220104", raw_price=10.0, amount=350)
        self.assertEqual(odd_lot.reject_reason, "amount_not_lot_aligned")
        fractional = broker.execute("000001.SZ", "buy", trade_date="20220104", raw_price=10.0, amount=100.5)
        self.assertEqual(fractional.reject_reason, "invalid_amount")

    def test_validate_order_amount_is_direction_aware(self):
        broker = self.make_broker()
        broker.execute("000001.SZ", "buy", trade_date="20220104", raw_price=10.0, amount=1000)
        broker.roll_to_date("20220105")
        self.assertEqual(broker.validate_order_amount("buy", "000001.SZ", 350), (0, "amount_not_lot_aligned"))
        self.assertEqual(broker.validate_order_amount("sell", "000001.SZ", 1000), (1000, None))
        # A missing amount rejects on the strategy path; it never means sell-all.
        self.assertEqual(broker.validate_order_amount("sell", "000001.SZ", None)[1], "amount_below_lot_size")
        self.assertEqual(broker.validate_order_amount("sell", "000001.SZ", 0)[1], "amount_below_lot_size")

    def test_working_orders_reserve_cash_and_sellable_shares(self):
        broker = self.make_broker()
        broker.roll_to_date("20220104")
        cash_before = _asset(broker, "stock")["available_cash"]
        broker.passorder(
            optype.STOCK_BUY, 1101, "", "000001.SZ", prtype.FIX, 10.0, 1000,
            user_order_id="reserve_buy",
        )
        self.assertLess(_asset(broker, "stock")["available_cash"], cash_before - 10_000)
        broker.cancel("reserve_buy")
        self.assertAlmostEqual(_asset(broker, "stock")["available_cash"], cash_before)

        buy = broker.execute("000001.SZ", "buy", trade_date="20220104", raw_price=10.0, amount=1000)
        self.assertEqual(buy.status, "filled")
        broker.roll_to_date("20220105")
        self.assertEqual(_positions(broker, "stock")[0]["sellable_quantity"], 1000)
        broker.passorder(
            optype.STOCK_SELL, 1101, "", "000001.SZ", prtype.FIX, 12.0, 600,
            user_order_id="reserve_sell",
        )
        self.assertEqual(_positions(broker, "stock")[0]["sellable_quantity"], 400)
        broker.cancel("reserve_sell")
        self.assertEqual(_positions(broker, "stock")[0]["sellable_quantity"], 1000)

    def test_accounts_have_separate_cash_pools(self):
        # The two accounts never back each other's orders: a stock buy leaves the
        # credit cash untouched and vice versa, and combined equity starts at the
        # sum of the two initial cash amounts.
        broker = self.make_broker()
        self.assertEqual(broker.initial_equity, 1_000_000.0)
        broker.execute("000001.SZ", "buy", trade_date="20220104", raw_price=10.0, amount=1000)
        self.assertLess(broker.stock.cash, 500_000.0)
        self.assertEqual(broker.credit.cash, 500_000.0)
        stock_cash = broker.stock.cash
        broker.execute("000002.SZ", "credit_buy", trade_date="20220104", raw_price=20.0, amount=500)
        self.assertEqual(broker.stock.cash, stock_cash)
        self.assertLess(broker.credit.cash, 500_000.0)
        # A stock-account buy larger than the stock cash is rejected even though
        # the credit account could fund it.
        too_big = broker.execute("000001.SZ", "buy", trade_date="20220104", raw_price=10.0, amount=60_000)
        self.assertEqual(too_big.reject_reason, "insufficient_cash")

    def test_cross_account_hedge_nets_to_zero(self):
        # Long in the stock account plus 融券 short in the credit account is a
        # legitimate hedged book: each account keeps one side per code, and the
        # default position view nets across accounts.
        broker = self.make_broker()
        broker.execute("000002.SZ", "buy", trade_date="20220104", raw_price=20.0, amount=1000)
        short = broker.execute("000002.SZ", "short", trade_date="20220104", raw_price=20.0, amount=1000)
        self.assertEqual(short.status, "filled")
        self.assertEqual(broker.position_quantity("000002.SZ"), 0)
        self.assertEqual(broker.position_quantity("000002.SZ", account="stock"), 1000)
        self.assertEqual(broker.position_quantity("000002.SZ", account="credit"), -1000)

    def test_transfer_between_accounts_and_withdraw_gate(self):
        broker = self.make_broker()
        moved = broker.transfer(200_000, "stock", "credit")
        self.assertEqual(moved.status, "filled")
        self.assertEqual(broker.stock.cash, 300_000.0)
        self.assertEqual(broker.credit.cash, 700_000.0)
        # Misuse is rejected, not clamped.
        self.assertEqual(broker.transfer(0, "stock", "credit").reject_reason, "transfer_amount_not_positive")
        self.assertEqual(broker.transfer(100, "stock", "stock").reject_reason, "transfer_same_account")
        self.assertEqual(broker.transfer(10_000_000, "credit", "stock").reject_reason, "insufficient_cash")
        # With no credit debt, cash moves out freely.
        back = broker.transfer(200_000, "credit", "stock")
        self.assertEqual(back.status, "filled")
        # With debt outstanding, an outbound transfer must keep the maintenance
        # ratio at or above the withdraw line (3.00): the 提取线 is enforced here.
        broker.execute("000002.SZ", "short", trade_date="20220104", raw_price=20.0, amount=500)
        ok = broker.transfer(200_000, "credit", "stock")
        self.assertEqual(ok.status, "filled")  # post-ratio far above 3.0
        blocked = broker.transfer(290_000, "credit", "stock")
        self.assertEqual(blocked.reject_reason, "credit_withdraw_blocked_by_maintenance")

    def test_t1_blocks_same_day_close(self):
        broker = self.make_broker()
        broker.execute("000001.SZ", "buy", trade_date="20220104", raw_price=10.0, amount=1000)
        broker.close_all("20220104")
        self.assertIn("000001.SZ", broker.stock.positions)
        self.assertTrue(any(e["event_type"] == "exit_blocked_t_plus_one" for e in broker.events))

    def test_date_roll_unlocks_t_plus_one_before_any_fill(self):
        # R4: rolling the sim-date to D+1 unlocks an overnight hold's T+1 shares
        # before the day's first fill, so sellable_quantity is correct from the first
        # off-session tick (the host calls broker.roll_to_date at each new trade date).
        broker = self.make_broker()
        broker.execute("000001.SZ", "buy", trade_date="20220104", raw_price=10.0, amount=1000)
        held = next(p for p in _positions(broker, "stock") if p["ts_code"] == "000001.SZ")
        self.assertEqual(held["sellable_quantity"], 0)  # T+1 locked on D
        broker.roll_to_date("20220105")  # the host rolls the date before any D+1 tick
        held = next(p for p in _positions(broker, "stock") if p["ts_code"] == "000001.SZ")
        self.assertEqual(held["sellable_quantity"], 1000)  # unlocked without a fill
        # roll_to_date is idempotent: re-rolling the same date does not relock or error.
        broker.roll_to_date("20220105")
        held = next(p for p in _positions(broker, "stock") if p["ts_code"] == "000001.SZ")
        self.assertEqual(held["sellable_quantity"], 1000)

    def test_short_cover_is_t_plus_one(self):
        # 融券卖出后，买券还券同样按 T+1 处理：开空当日不可还券。
        broker = self.make_broker()  # 000002.SZ shortable
        opened = broker.execute("000002.SZ", "short", trade_date="20220104", raw_price=20.0, amount=500)
        self.assertEqual(opened.status, "filled")
        self.assertEqual(opened.account, "credit")
        covered = broker.execute("000002.SZ", "cover", trade_date="20220104", raw_price=19.5, amount=500)
        self.assertEqual(covered.status, "rejected")
        self.assertEqual(covered.reject_reason, "t_plus_one_no_sellable")
        next_day = broker.execute("000002.SZ", "cover", trade_date="20220105", raw_price=19.4, amount=500)
        self.assertEqual(next_day.status, "filled")
        self.assertEqual(broker.position_quantity("000002.SZ"), 0)
        # Covering everything closes the slo contract (its shares/sell_amount zero out).
        self.assertEqual(broker.get_debt_contract(), [])

    def test_long_still_t_plus_one_blocks_same_day_sell(self):
        # R5: the long T+1 mechanic is untouched — a same-day sell is still rejected.
        broker = self.make_broker()
        broker.execute("000001.SZ", "buy", trade_date="20220104", raw_price=10.0, amount=1000)
        sell = broker.execute("000001.SZ", "sell", trade_date="20220104", raw_price=10.5, amount=1000)
        self.assertEqual(sell.status, "rejected")
        self.assertEqual(sell.reject_reason, "t_plus_one_no_sellable")

    def test_short_t_plus_one_lock_matches_long_lock(self):
        # Long sells and 融券 covers both use locked_today/locked_date to block
        # same-day reduction, while the legs live in separate accounts.
        broker = self.make_broker()  # 000001.SZ long (stock), 000002.SZ short (credit)
        broker.execute("000001.SZ", "buy", trade_date="20220104", raw_price=10.0, amount=1000)
        long_pos = broker.stock.positions["000001.SZ"]
        self.assertEqual(long_pos.locked_today, 1000)  # long records the T+1 lock
        self.assertEqual(long_pos.locked_date, "20220104")
        self.assertEqual(long_pos.sellable_quantity, 0)  # locked on entry day

        broker.execute("000002.SZ", "short", trade_date="20220104", raw_price=20.0, amount=500)
        short_pos = broker.credit.positions["000002.SZ"]
        self.assertEqual(short_pos.locked_today, 500)
        self.assertEqual(short_pos.locked_date, "20220104")
        self.assertEqual(short_pos.sellable_quantity, 0)
        self.assertEqual(long_pos.sellable_quantity, 0)  # long still locked on D

        # Adding to the short after the date roll locks only that day's new short
        # shares; the previous day's short and long are now sellable.
        broker.execute("000002.SZ", "short", trade_date="20220105", raw_price=19.4, amount=500)
        short_pos = broker.credit.positions["000002.SZ"]
        self.assertEqual(short_pos.locked_today, 500)
        self.assertEqual(short_pos.locked_date, "20220105")
        self.assertEqual(short_pos.sellable_quantity, 500)
        self.assertEqual(long_pos.locked_today, 0)  # long unlocked on the D+1 roll
        self.assertEqual(long_pos.sellable_quantity, 1000)

        # A partial cover can only consume the previous day's sellable short.
        broker.execute("000002.SZ", "cover", trade_date="20220105", raw_price=19.0, amount=500)
        short_pos = broker.credit.positions["000002.SZ"]
        self.assertEqual(short_pos.locked_today, 500)
        self.assertEqual(short_pos.locked_date, "20220105")

    def test_partial_sell_rejects_amount_above_sellable_balance(self):
        broker = self.make_broker()
        broker.execute("000001.SZ", "buy", trade_date="20220104", raw_price=10.0, amount=1000)
        broker.execute("000001.SZ", "buy", trade_date="20220105", raw_price=10.6, amount=500)
        self.assertEqual(broker.position_quantity("000001.SZ"), 1500)
        order = broker.execute("000001.SZ", "sell", trade_date="20220105", raw_price=11.0, amount=1500)
        # Only the 1000 shares bought on 0104 are sellable on 0105 (T+1).
        self.assertEqual(order.reject_reason, "amount_exceeds_sellable")
        self.assertEqual(broker.position_quantity("000001.SZ"), 1500)
        allowed = broker.execute("000001.SZ", "sell", trade_date="20220105", raw_price=11.0, amount=1000)
        self.assertEqual(allowed.status, "filled")
        self.assertEqual(broker.position_quantity("000001.SZ"), 500)

    def test_short_requires_margin_secs_membership(self):
        broker = self.make_broker(shortable=("000001.SZ",))
        order = broker.execute("000002.SZ", "short", trade_date="20220104", raw_price=20.0, amount=500)
        self.assertEqual(order.status, "rejected")
        self.assertEqual(order.reject_reason, "margin_secs_not_shortable")

    def test_short_profit_and_interest_paid_on_cover(self):
        # 融券费 accrues into the slo contract per calendar day and is paid from cash
        # at repayment (mandatory liquidation covers the short here).
        broker = self.make_broker()
        broker.execute("000002.SZ", "short", trade_date="20220104", raw_price=20.0, amount=500)
        broker.mark_to_market("20220104")
        broker.mark_to_market("20220105")
        self.assertGreater(broker.interest_accrued_total, 0.0)
        self.assertEqual(broker.interest_paid_total, 0.0)  # nothing repaid yet
        broker.close_all("20220106")
        closed = [event for event in broker.events if event["event_type"] == "position_closed"][0]
        self.assertEqual(closed["side"], "short")
        self.assertEqual(closed["account"], "credit")
        self.assertGreater(broker.equity(), broker.initial_equity)
        self.assertGreater(broker.stamp_duty_paid, 0.0)
        self.assertAlmostEqual(broker.interest_paid_total, broker.interest_accrued_total)
        self.assertEqual(broker.get_debt_contract(), [])  # contract settled by the cover

    def test_interest_accrues_over_calendar_gap(self):
        # R8a: 融券费 accrues per CALENDAR day, so a short marked Friday then Monday
        # is charged 3 days for the weekend carry, not one trade-day's fee.
        daily = make_daily(
            [
                ("20220107", "000002.SZ", 20.0, 19.8, 22.0, 18.0, False),  # Friday
                ("20220110", "000002.SZ", 19.8, 19.5, 21.5, 17.6, False),  # Monday (+3 calendar days)
            ]
        )
        broker = self.make_broker(daily=daily)
        broker.execute("000002.SZ", "short", trade_date="20220107", raw_price=20.0, amount=500)
        broker.mark_to_market("20220107")  # first mark: 1 calendar day
        fee_friday = broker.interest_accrued_total
        broker.mark_to_market("20220110")  # +3 calendar days (Sat, Sun, Mon)
        fee_monday = broker.interest_accrued_total
        self.assertGreater(fee_friday, 0.0)
        # The weekend gap charges 3x the single-day fee, not 1x (the old trade-day model).
        self.assertAlmostEqual(fee_monday - fee_friday, fee_friday * 3)

    def test_short_proceeds_are_locked_not_deployable(self):
        # R8b: a short banks its proceeds into credit-account cash but locks them
        # as collateral; the credit available_cash never inflates with proceeds.
        # Margin is not frozen cash — it constrains new credit ops through
        # 保证金可用余额.
        broker = self.make_broker()  # 000002.SZ shortable, 500,000 credit cash
        bail_before = broker.enable_bail_balance()
        broker.execute("000002.SZ", "short", trade_date="20220104", raw_price=20.0, amount=500)
        asset_after = _asset(broker, "credit")
        pos = next(p for p in _positions(broker, "credit") if p["ts_code"] == "000002.SZ")
        self.assertGreater(broker.credit.cash, 500_000.0)  # proceeds banked into literal cash
        # Deployable cash excludes exactly the locked short proceeds (entry_cost).
        self.assertAlmostEqual(asset_after["available_cash"], broker.credit.cash - pos["entry_cost"])
        # The bail balance drops by the posted margin plus open costs (fee + duty).
        contract = broker.get_debt_contract()[0]
        margin = contract["sell_amount"] * broker.profile.effective_slo_margin_ratio
        open_costs = contract["sell_amount"] - pos["entry_cost"]
        self.assertAlmostEqual(bail_before - broker.enable_bail_balance(), margin + open_costs)

    def test_fin_buy_creates_contract_without_cash_outflow(self):
        broker = self.make_broker(shortable=("000001.SZ",))  # fin gate shares margin_secs
        order = broker.execute("000001.SZ", "fin_buy", trade_date="20220104", raw_price=10.0, amount=1000)
        self.assertEqual(order.status, "filled")
        self.assertEqual(order.account, "credit")
        self.assertEqual(order.op_type, optype.FIN_BUY)
        self.assertEqual(broker.credit.cash, 500_000.0)  # financed: no cash moves at open
        self.assertEqual(broker.position_quantity("000001.SZ", account="credit"), 1000)
        contract = broker.get_debt_contract()[0]
        price = broker.profile.slipped_price(10.0, is_buy=True)
        notional = 1000 * price
        principal = notional + broker.profile.cost_model.trade_fee(notional)
        self.assertEqual(contract["compact_type"], "fin")
        self.assertAlmostEqual(contract["real_compact_balance"], principal)
        self.assertEqual(contract["real_compact_vol"], 1000)
        credit = _asset(broker, "credit")
        self.assertAlmostEqual(credit["fin_debt"], principal)
        # equity nets the debt: only the financed fee is lost at open.
        self.assertAlmostEqual(broker.equity(), 1_000_000.0 + 1000 * price - principal)

    def test_fin_buy_requires_margin_secs_membership(self):
        broker = self.make_broker(shortable=())
        order = broker.execute("000001.SZ", "fin_buy", trade_date="20220104", raw_price=10.0, amount=1000)
        self.assertEqual(order.reject_reason, "margin_secs_not_finable")

    def test_credit_buy_requires_margin_secs_membership(self):
        broker = self.make_broker(shortable=())
        order = broker.execute("000001.SZ", "credit_buy", trade_date="20220104", raw_price=10.0, amount=1000)
        self.assertEqual(order.reject_reason, "margin_secs_not_collateral")

    def test_fin_interest_accrues_and_direct_repay_requires_valid_amount(self):
        broker = self.make_broker(shortable=("000001.SZ",))
        broker.execute("000001.SZ", "fin_buy", trade_date="20220104", raw_price=10.0, amount=1000)
        broker.mark_to_market("20220104")
        principal = broker._fin_amount_outstanding()
        interest = broker._interest_outstanding()
        self.assertAlmostEqual(interest, principal * broker.profile.fin_rate_annual / 360.0)
        # Partial repayment pays interest first, then principal, from credit cash.
        cash_before = broker.credit.cash
        broker.passorder(optype.DIRECT_REPAY, 1102, "", "", prtype.PEER, 0, 5000)
        self.assertAlmostEqual(cash_before - broker.credit.cash, 5000.0)
        self.assertAlmostEqual(broker.interest_paid_total, interest)
        self.assertAlmostEqual(broker._fin_amount_outstanding(), principal - (5000.0 - interest))
        # Overshooting live broker constraints is rejected, not clamped.
        broker.passorder(optype.DIRECT_REPAY, 1102, "", "", prtype.PEER, 0, 10_000_000)
        self.assertEqual(broker.orders[-1].reject_reason, "insufficient_cash")
        broker.passorder(
            optype.DIRECT_REPAY, 1102, "", "", prtype.PEER, 0,
            broker._fin_amount_outstanding() + broker._interest_outstanding() + 1.0,
        )
        self.assertEqual(broker.orders[-1].reject_reason, "amount_exceeds_fin_debt")
        # An exact remaining debt repayment closes the contract.
        broker.passorder(
            optype.DIRECT_REPAY, 1102, "", "", prtype.PEER, 0,
            broker._fin_amount_outstanding() + broker._interest_outstanding(),
        )
        self.assertAlmostEqual(broker._fin_amount_outstanding(), 0.0)
        self.assertEqual(broker.get_debt_contract(), [])
        # With no debt left, another repay is rejected (fail-fast, not a no-op).
        broker.passorder(optype.DIRECT_REPAY, 1102, "", "", prtype.PEER, 0, 1000)
        self.assertEqual(broker.orders[-1].reject_reason, "no_fin_debt")

    def test_sell_repay_applies_net_proceeds_interest_first(self):
        broker = self.make_broker(shortable=("000001.SZ",))
        broker.execute("000001.SZ", "fin_buy", trade_date="20220104", raw_price=10.0, amount=1000)
        broker.mark_to_market("20220104")
        owed = broker._fin_amount_outstanding() + broker._interest_outstanding()
        cash_before = broker.credit.cash
        order = broker.execute("000001.SZ", "sell_repay", trade_date="20220105", raw_price=11.0, amount=1000)
        self.assertEqual(order.status, "filled")
        price = broker.profile.slipped_price(11.0, is_buy=False)
        notional = 1000 * price
        net = (
            notional
            - broker.profile.cost_model.trade_fee(notional)
            - broker.profile.stamp_duty_on_sale(notional, "20220105")
        )
        # The whole debt is repaid; the surplus stays as credit cash.
        self.assertAlmostEqual(broker.credit.cash, cash_before + net - owed)
        self.assertAlmostEqual(broker._fin_amount_outstanding(), 0.0)
        self.assertEqual(broker.position_quantity("000001.SZ"), 0)
        self.assertEqual(broker.get_debt_contract(), [])

    def test_sell_repay_without_debt_is_rejected(self):
        broker = self.make_broker()
        broker.execute("000001.SZ", "credit_buy", trade_date="20220104", raw_price=10.0, amount=1000)
        order = broker.execute("000001.SZ", "sell_repay", trade_date="20220105", raw_price=11.0, amount=1000)
        self.assertEqual(order.reject_reason, "no_fin_debt")

    def test_financed_shares_must_use_sell_repay(self):
        broker = self.make_broker(shortable=("000001.SZ",))
        broker.execute("000001.SZ", "fin_buy", trade_date="20220104", raw_price=10.0, amount=1000)
        plain_sell = broker.execute("000001.SZ", "credit_sell", trade_date="20220105", raw_price=11.0, amount=1000)
        self.assertEqual(plain_sell.reject_reason, "financed_shares_require_sell_repay")
        repay_sell = broker.execute("000001.SZ", "sell_repay", trade_date="20220105", raw_price=11.0, amount=1000)
        self.assertEqual(repay_sell.status, "filled")

    def test_star_market_minimum_then_one_share_increment(self):
        daily = make_daily(
            [
                ("20220104", "688001.SH", 50.0, 50.0, 60.0, 40.0, False),
            ]
        )
        broker = self.make_broker(daily=daily)
        below_min = broker.execute("688001.SH", "buy", trade_date="20220104", raw_price=50.0, amount=100)
        self.assertEqual(below_min.reject_reason, "amount_below_lot_size")
        valid_odd = broker.execute("688001.SH", "buy", trade_date="20220104", raw_price=50.0, amount=201)
        self.assertEqual(valid_odd.status, "filled")
        self.assertEqual(broker.position_quantity("688001.SH", account="stock"), 201)

    def test_slipped_fill_saturates_at_the_daily_limit_band(self):
        # Slippage is a liquidity assumption; no exchange print exists outside
        # [down_limit, up_limit]. A buy whose raw price passes the limit block
        # but whose slipped price would cross the band fills AT the band edge.
        daily = make_daily([("20220104", "000001.SZ", 10.998, 10.999, 11.0, 9.0, False)])
        broker = self.make_broker(daily=daily, slippage_bps=20.0)
        buy = broker.execute("000001.SZ", "buy", trade_date="20220104", raw_price=10.998, amount=1000)
        self.assertEqual(buy.status, "filled")
        self.assertEqual(buy.price, 11.0)  # 10.998 * 1.002 = 11.02 -> clamped
        sell_daily = make_daily([("20220104", "000001.SZ", 9.002, 9.001, 11.0, 9.0, False)])
        seller = self.make_broker(daily=sell_daily, slippage_bps=20.0)
        seller.execute("000001.SZ", "buy", trade_date="20220104", raw_price=10.0, amount=1000)
        seller.roll_to_date("20220105")
        seller._advance_date = lambda *_: None  # keep the same bar for the sell leg
        sale = seller.execute("000001.SZ", "sell", trade_date="20220104", raw_price=9.002, amount=1000)
        self.assertEqual(sale.status, "filled")
        self.assertEqual(sale.price, 9.0)  # 9.002 * 0.998 = 8.984 -> clamped

    def test_submission_reject_keeps_the_callers_reason(self):
        # A resolved close() whose sellable amount fails the lot gate must keep
        # the strategy's own reason string on the reject record — not the
        # resolved verb name, which the caller's code never contains.
        broker = self.make_broker()
        order = broker.reject_submission(
            ts_code="000001.SZ",
            action="sell",
            reason="amount_below_lot_size",
            amount=0,
            submitted_at="2022-01-04T09:38:00+08:00",
            strategy_reason="max_positions",
        )
        self.assertEqual(order.reject_reason, "amount_below_lot_size")
        self.assertEqual(order.reason, "max_positions")
        # Without a caller reason the record falls back to the verb, as before.
        fallback = broker.reject_submission(
            ts_code="000001.SZ", action="sell", reason="amount_below_lot_size", amount=0
        )
        self.assertEqual(fallback.reason, "sell")

    def test_bail_balance_gates_fin_buy_and_short(self):
        broker = self.make_broker(shortable=("000001.SZ", "000002.SZ"))
        # Financing far beyond the credit bail balance (~500,000) is rejected.
        too_big = broker.execute("000001.SZ", "fin_buy", trade_date="20220104", raw_price=10.0, amount=120_000)
        self.assertEqual(too_big.reject_reason, "insufficient_bail_balance")
        # A fin_buy within the balance passes, and its margin occupation reduces
        # what a subsequent short may post.
        ok = broker.execute("000001.SZ", "fin_buy", trade_date="20220104", raw_price=10.0, amount=40_000)
        self.assertEqual(ok.status, "filled")
        short = broker.execute("000002.SZ", "short", trade_date="20220104", raw_price=20.0, amount=5_000)
        self.assertEqual(short.reject_reason, "insufficient_bail_balance")

    def test_credit_quota_gates(self):
        broker = self.make_broker(shortable=("000001.SZ", "000002.SZ"), fin_max_quota=5000.0, slo_max_quota=5000.0)
        fin = broker.execute("000001.SZ", "fin_buy", trade_date="20220104", raw_price=10.0, amount=1000)
        self.assertEqual(fin.reject_reason, "fin_quota_exceeded")
        short = broker.execute("000002.SZ", "short", trade_date="20220104", raw_price=20.0, amount=500)
        self.assertEqual(short.reject_reason, "slo_quota_exceeded")

    def test_maintenance_ignores_stock_account_assets(self):
        # 维保比例 counts CREDIT-account assets only: a large stock-account cash
        # pile neither lifts the ratio nor prevents the credit forced close, and
        # the liquidation never touches the stock account.
        daily = make_daily(
            [
                ("20220104", "000001.SZ", 10.0, 10.0, 12.0, 8.0, False),
                ("20220105", "000001.SZ", 10.2, 10.2, 12.0, 8.0, False),
                ("20220104", "000002.SZ", 100.0, 100.0, 2500.0, 1.0, False),
                ("20220105", "000002.SZ", 250.0, 250.0, 2500.0, 1.0, False),  # short loss day
            ]
        )
        broker = self.make_broker(daily=daily, stock_initial_cash=10_000_000.0)
        broker.execute("000001.SZ", "buy", trade_date="20220104", raw_price=10.0, amount=1000)
        broker.execute("000002.SZ", "short", trade_date="20220104", raw_price=100.0, amount=4000)
        # Day 2: the short jumps 100 -> 250; despite 10M of stock cash the credit
        # account alone breaches the 1.30 line -> forced close of the credit book.
        broker.mark_to_market("20220105")
        self.assertTrue(any(e["event_type"] == "forced_close_triggered" for e in broker.events))
        self.assertEqual(broker.credit.positions, {})
        self.assertIn("000001.SZ", broker.stock.positions)  # stock account untouched

    def test_maintenance_warning_records_without_forced_close(self):
        daily = make_daily(
            [
                ("20220104", "000002.SZ", 100.0, 100.0, 2500.0, 1.0, False),
                ("20220105", "000002.SZ", 170.0, 170.0, 2500.0, 1.0, False),
            ]
        )
        broker = self.make_broker(daily=daily)
        broker.execute("000002.SZ", "short", trade_date="20220104", raw_price=100.0, amount=4000)
        broker.mark_to_market("20220105")
        self.assertTrue(any(e["event_type"] == "maintenance_warning" for e in broker.events))
        self.assertFalse(any(e["event_type"] == "forced_close_triggered" for e in broker.events))
        self.assertIn("000002.SZ", broker.credit.positions)

    def test_debt_contract_term_auto_extension_records_event(self):
        broker = self.make_broker(shortable=("000001.SZ",), debt_contract_term_days=1)
        broker.execute("000001.SZ", "fin_buy", trade_date="20220104", raw_price=10.0, amount=1000)
        broker.mark_to_market("20220104")
        broker.mark_to_market("20220105")
        self.assertTrue(any(e["event_type"] == "debt_contract_extended" for e in broker.events))
        contract = broker.get_debt_contract()[0]
        self.assertEqual(contract["last_extension_date"], "20220105")
        self.assertEqual(contract["extension_count"], 1)

    def test_credit_account_accounting_and_forced_close(self):
        # RA1 (dual-account form): with a credit-account collateral long plus a
        # short, equity/maintenance/available follow the 细则 formulas over the
        # credit account; a short loss that breaches the 1.30 line forces a close.
        daily = make_daily(
            [
                ("20220104", "000001.SZ", 10.0, 10.0, 12.0, 8.0, False),
                ("20220105", "000001.SZ", 10.2, 10.2, 12.0, 8.0, False),
                ("20220104", "000002.SZ", 100.0, 100.0, 2500.0, 1.0, False),
                ("20220105", "000002.SZ", 250.0, 250.0, 2500.0, 1.0, False),  # short loss day
            ]
        )
        broker = self.make_broker(daily=daily)  # 000002.SZ shortable, 500,000 credit cash
        broker.execute("000001.SZ", "credit_buy", trade_date="20220104", raw_price=10.0, amount=1000)
        broker.execute("000002.SZ", "short", trade_date="20220104", raw_price=100.0, amount=4000)
        asset = _asset(broker, "credit")
        positions = {p["ts_code"]: p for p in _positions(broker, "credit")}
        long_mv = positions["000001.SZ"]["market_value"]
        short_mv = positions["000002.SZ"]["market_value"]
        proceeds_locked = positions["000002.SZ"]["entry_cost"]
        # equity and maintenance ratio use literal credit cash (the banked short
        # proceeds count as collateral); available_cash subtracts the locked proceeds.
        self.assertAlmostEqual(broker.account_equity("credit"), broker.credit.cash + long_mv - short_mv)
        self.assertAlmostEqual(broker.maintenance_ratio(), (broker.credit.cash + long_mv) / short_mv)
        self.assertAlmostEqual(asset["available_cash"], broker.credit.cash - proceeds_locked)
        self.assertLess(asset["available_cash"], broker.credit.cash)  # locked collateral not deployable
        # Day 2: the short jumps 100 -> 250, breaching the maintenance line -> forced close.
        broker.mark_to_market("20220105")
        self.assertTrue(any(e["event_type"] == "forced_close_triggered" for e in broker.events))
        self.assertEqual(broker.credit.positions, {})  # both credit legs liquidated
        self.assertGreater(broker.interest_paid_total, 0.0)  # slo interest paid at the cover

    def test_broker_inventory_mode_rejects_without_files(self):
        broker = self.make_broker(mode="broker_inventory")
        order = broker.execute("000002.SZ", "short", trade_date="20220104", raw_price=20.0, amount=500)
        self.assertEqual(order.reject_reason, "broker_inventory_unavailable")

    def test_passorder_lifecycle_matches_qmt_verbs(self):
        broker = self.make_broker(shortable=("000001.SZ",))
        group = pd.DataFrame([{"ts_code": "000001.SZ", "open": 10.0, "high": 10.1, "low": 9.8, "close": 9.9}])
        # 指定价 limit below the bar -> rests as a day order until cancel/day end.
        miss = broker.passorder(optype.CREDIT_BUY, 1101, "", "000001.SZ", prtype.FIX, 9.5, 1000)
        working = broker.working_orders()
        self.assertEqual([o["order_id"] for o in working], [miss])
        self.assertEqual(working[0]["op_type"], optype.CREDIT_BUY)
        self.assertEqual(working[0]["account"], "credit")
        broker.match_bar("20220104", "09:31", group)
        self.assertEqual([o["order_id"] for o in broker.working_orders()], [miss])
        self.assertEqual(_positions(broker), [])
        # cancel removes a working order by id.
        cancel_me = broker.passorder(optype.CREDIT_BUY, 1101, "", "000001.SZ", prtype.FIX, 9.0, 1000)
        self.assertTrue(broker.cancel(cancel_me))
        self.assertFalse(broker.cancel("missing"))
        # A user_order_id (投资备注) doubles as the returned/correlated order id.
        tagged = broker.passorder(
            optype.CREDIT_BUY, 1101, "", "000001.SZ", prtype.FIX, 9.0, 1000, user_order_id="C_tick_001"
        )
        self.assertEqual(tagged, "C_tick_001")
        self.assertTrue(broker.cancel("C_tick_001"))
        # A reachable limit fills at exactly the limit (maker, no slippage) into
        # the account the opType selects.
        broker.passorder(optype.CREDIT_BUY, 1101, "", "000001.SZ", prtype.FIX, 9.85, 1000)
        broker.match_bar("20220104", "09:32", group)
        self.assertEqual(broker.position_quantity("000001.SZ", account="credit"), 1000)
        self.assertEqual(broker.position_quantity("000001.SZ", account="stock"), 0)
        self.assertEqual(_deals(broker, "000001.SZ")[-1]["price"], 9.85)
        # If the activation bar opens through a buy limit, the better open is used.
        better_open = self.make_broker(shortable=())
        better_open.passorder(optype.STOCK_BUY, 1101, "", "000001.SZ", prtype.FIX, 10.5, 1000)
        better_open.match_bar("20220104", "09:31", group)
        self.assertEqual(_deals(better_open, "000001.SZ")[-1]["price"], 10.0)
        self.assertEqual(better_open.position_quantity("000001.SZ", account="stock"), 1000)
        # Sell limits use the same better-open rule on the other side.
        better_sell = self.make_broker(shortable=())
        better_sell.execute("000001.SZ", "buy", trade_date="20220104", raw_price=10.0, amount=1000)
        better_sell.passorder(optype.STOCK_SELL, 1101, "", "000001.SZ", prtype.FIX, 9.5, 1000)
        better_sell.match_bar("20220105", "09:31", group)
        self.assertEqual(_deals(better_sell, "000001.SZ")[-1]["price"], 10.0)
        self.assertEqual(better_sell.position_quantity("000001.SZ"), 0)

    def test_passorder_validates_op_order_and_price_types(self):
        broker = self.make_broker()
        with self.assertRaisesRegex(ValueError, "opType=30"):
            broker.passorder(optype.DIRECT_SECU_REPAY, 1101, "", "000001.SZ", prtype.PEER, 0, 100)
        with self.assertRaisesRegex(ValueError, "orderType"):
            broker.passorder(optype.CREDIT_BUY, 1102, "", "000001.SZ", prtype.PEER, 0, 100)
        with self.assertRaisesRegex(ValueError, "orderType=1102"):
            broker.passorder(optype.DIRECT_REPAY, 1101, "", "", prtype.PEER, 0, 100)
        with self.assertRaisesRegex(ValueError, "prType"):
            broker.passorder(optype.CREDIT_BUY, 1101, "", "000001.SZ", 12, 0, 100)
        with self.assertRaisesRegex(ValueError, "positive price"):
            broker.passorder(optype.CREDIT_BUY, 1101, "", "000001.SZ", prtype.FIX, 0, 100)
        with self.assertRaisesRegex(ValueError, "account_type is required"):
            broker.get_trade_detail_data(data_type="ORDER")
        with self.assertRaisesRegex(ValueError, "data_type"):
            broker.get_trade_detail_data(account_type="STOCK", data_type="TASK")

    def test_order_and_deal_records_are_account_filtered(self):
        broker = self.make_broker()
        broker.execute("000001.SZ", "buy", trade_date="20220104", raw_price=10.0, amount=1000)
        broker.execute("000002.SZ", "credit_buy", trade_date="20220104", raw_price=20.0, amount=500)
        stock_orders = broker.get_trade_detail_data(account_type="STOCK", data_type="ORDER")
        credit_orders = broker.get_trade_detail_data(account_type="CREDIT", data_type="ORDER")
        self.assertEqual([o["ts_code"] for o in stock_orders], ["000001.SZ"])
        self.assertEqual([o["ts_code"] for o in credit_orders], ["000002.SZ"])
        self.assertTrue(all(o["account"] == "stock" for o in stock_orders))
        stock_deals = broker.get_trade_detail_data(account_type="STOCK", data_type="DEAL")
        self.assertEqual([d["ts_code"] for d in stock_deals], ["000001.SZ"])

    def test_max_total_holdings_counts_codes_across_accounts(self):
        broker = self.make_broker(shortable=("000001.SZ",), max_total_holdings=1)
        broker.execute("000001.SZ", "buy", trade_date="20220104", raw_price=10.0, amount=100)
        # A new code in the OTHER account still breaches the combined breadth cap...
        order = broker.execute("000002.SZ", "credit_buy", trade_date="20220104", raw_price=20.0, amount=100)
        self.assertEqual(order.reject_reason, "max_holdings_reached")
        # ...while adding to an already-held code in another account does not.
        same_code = broker.execute("000001.SZ", "credit_buy", trade_date="20220104", raw_price=10.0, amount=100)
        self.assertEqual(same_code.status, "filled")

    def test_single_name_weight_cap_rejects_oversized_order(self):
        # Cap notional = max_single_name_weight x combined initial equity
        # (0.2 x 1,000,000).
        broker = self.make_broker(max_single_name_weight=0.2)
        order = broker.execute("000001.SZ", "buy", trade_date="20220104", raw_price=10.0, amount=50_000)
        self.assertEqual(order.reject_reason, "single_name_weight_cap")
        allowed = broker.execute("000001.SZ", "buy", trade_date="20220104", raw_price=10.0, amount=20_000)
        self.assertEqual(allowed.status, "filled")

    def test_default_profile_does_not_force_holdings_or_single_name_caps(self):
        broker = self.make_broker(shortable=())
        first = broker.execute("000001.SZ", "buy", trade_date="20220104", raw_price=10.0, amount=1000)
        second = broker.execute("000002.SZ", "buy", trade_date="20220104", raw_price=20.0, amount=100)
        self.assertEqual(first.status, "filled")
        self.assertEqual(first.filled_quantity, 1000)
        self.assertEqual(second.status, "filled")

    def test_limit_up_blocks_buy_and_suspension_blocks_fill(self):
        daily = make_daily(
            [
                ("20220104", "000001.SZ", 11.0, 11.0, 11.0, 9.0, False),
                ("20220104", "000003.SZ", 5.0, 5.0, 5.5, 4.5, True),
            ]
        )
        broker = SimBroker(BrokerProfile(), MarketData(daily), shortable_codes=frozenset())
        blocked = broker.execute("000001.SZ", "buy", trade_date="20220104", raw_price=11.0, amount=100)
        suspended = broker.execute("000003.SZ", "buy", trade_date="20220104", raw_price=5.0, amount=100)
        self.assertEqual(blocked.reject_reason, "limit_up_blocked_buy")
        self.assertEqual(suspended.reject_reason, "suspended")

    def test_slo_sell_requires_limit_and_uptick_rule(self):
        broker = self.make_broker()
        group = pd.DataFrame([{"ts_code": "000002.SZ", "open": 20.0, "high": 20.2, "low": 19.8, "close": 20.1}])
        # A market-priced 融券卖出 is rejected at submission (must be a limit order).
        broker.passorder(optype.SLO_SELL, 1101, "", "000002.SZ", prtype.PEER, 0, 500)
        self.assertEqual(broker.orders[-1].reject_reason, "slo_sell_requires_limit_price")
        # A limit below the activation bar's reference price violates the 申报 rule.
        broker.passorder(optype.SLO_SELL, 1101, "", "000002.SZ", prtype.FIX, 19.0, 500)
        broker.match_bar("20220104", "09:31", group)
        self.assertEqual(broker.orders[-1].reject_reason, "slo_sell_uptick_rule")
        # A limit at the reference price passes and fills at the limit (no slippage).
        broker.passorder(optype.SLO_SELL, 1101, "", "000002.SZ", prtype.FIX, 20.0, 500)
        broker.match_bar("20220104", "09:32", group)
        self.assertEqual(broker.position_quantity("000002.SZ", account="credit"), -500)
        contract = broker.get_debt_contract()[0]
        self.assertEqual(contract["compact_type"], "slo")
        self.assertAlmostEqual(contract["sell_amount"], 500 * 20.0)

    def test_maintenance_includes_fin_debt_and_forces_close(self):
        daily = make_daily(
            [
                ("20220104", "000001.SZ", 10.0, 10.0, 12.0, 0.5, False),
                ("20220105", "000001.SZ", 1.6, 1.5, 12.0, 0.5, False),  # crash day
            ]
        )
        broker = self.make_broker(shortable=("000001.SZ",), daily=daily)
        broker.execute("000001.SZ", "fin_buy", trade_date="20220104", raw_price=10.0, amount=45_000)
        broker.mark_to_market("20220104")
        self.assertGreater(broker.maintenance_ratio(), broker.profile.maintenance_closeout_ratio)
        broker.roll_to_date("20220105")
        broker.mark_to_market("20220105")  # ratio ~(500k+67.5k)/(450k+i) < 1.30 after the crash
        self.assertTrue(any(e["event_type"] == "forced_close_triggered" for e in broker.events))
        self.assertEqual(broker.credit.positions, {})  # the financed long was liquidated
        # The fin debt is NOT auto-settled by liquidation: it stays outstanding
        # (accruing interest) until repaid, and equity nets it out.
        self.assertGreater(broker._fin_amount_outstanding(), 0.0)
        self.assertAlmostEqual(
            broker.account_equity("credit"),
            broker.credit.cash - broker._fin_amount_outstanding() - broker._interest_outstanding(),
        )

    def test_trades_for_records_open_and_reduce_history(self):
        broker = self.make_broker()
        broker.execute("000001.SZ", "buy", trade_date="20220104", raw_price=10.0, amount=1000)
        broker.mark_to_market("20220105")
        broker.execute("000001.SZ", "sell", trade_date="20220105", raw_price=11.0, amount=500)
        trades = _deals(broker, "000001.SZ")
        self.assertEqual([t["kind"] for t in trades], ["open", "reduce"])
        self.assertTrue(all(t["account"] == "stock" for t in trades))
        self.assertAlmostEqual(trades[0]["price"], BrokerProfile().slipped_price(10.0, is_buy=True))

    def test_costs_use_slippage_min_commission_and_dated_stamp_duty(self):
        profile = BrokerProfile()
        self.assertAlmostEqual(profile.slipped_price(10.0, is_buy=True), 10.005)
        self.assertAlmostEqual(profile.slipped_price(10.0, is_buy=False), 9.995)
        self.assertEqual(profile.commission(1_000.0), 5.0)  # minimum ¥5 floor
        self.assertAlmostEqual(profile.commission(100_000.0), 10.0)
        self.assertAlmostEqual(profile.stamp_duty_on_sale(100_000.0, "20230827"), 100.0)
        self.assertAlmostEqual(profile.stamp_duty_on_sale(100_000.0, "20230828"), 50.0)

    def test_profile_record_round_trips_all_constructor_fields(self):
        profile = BrokerProfile(
            stock_initial_cash=300_000.0,
            credit_initial_cash=700_000.0,
            commission_bps=2.5,
            transfer_fee_bps=0.2,
            min_commission_cny=1.25,
            stamp_duty_sell_bps_before_cutover=12.0,
            stamp_duty_sell_bps_from_cutover=6.0,
            slippage_bps=7.0,
            slo_rate_annual=0.03,
            fin_rate_annual=0.06,
            debt_contract_term_days=90,
            debt_contract_auto_extend=False,
            assure_ratio=0.65,
            maintenance_source="broker-doc",
        )
        restored = BrokerProfile(**_profile_kwargs(profile.to_record()))
        self.assertEqual(restored.stock_initial_cash, 300_000.0)
        self.assertEqual(restored.credit_initial_cash, 700_000.0)
        self.assertEqual(restored.transfer_fee_bps, 0.2)
        self.assertEqual(restored.min_commission_cny, 1.25)
        self.assertEqual(restored.stamp_duty_sell_bps_before_cutover, 12.0)
        self.assertEqual(restored.slippage_bps, 7.0)
        self.assertEqual(restored.slo_rate_annual, 0.03)
        self.assertEqual(restored.fin_rate_annual, 0.06)
        self.assertEqual(restored.debt_contract_term_days, 90)
        self.assertFalse(restored.debt_contract_auto_extend)
        self.assertEqual(restored.assure_ratio, 0.65)
        self.assertEqual(restored.maintenance_source, "broker-doc")
        private_restored = BrokerProfile(**_profile_kwargs(BrokerProfile(is_private_fund=True).to_record()))
        self.assertEqual(private_restored.effective_slo_margin_ratio, 1.2)


# Ex-date replay: 000001.SZ closes 10.0, then opens the next day 0.5 lower — the
# raw price gap the 0.5/share cash dividend explains.
CA_REPLAY = make_daily(
    [
        ("20220104", "000001.SZ", 10.0, 10.0, 11.0, 9.0, False),
        ("20220105", "000001.SZ", 9.5, 9.5, 10.45, 8.55, False),
        ("20220106", "000001.SZ", 9.5, 9.5, 10.45, 8.55, False),
        ("20220104", "000002.SZ", 20.0, 20.0, 22.0, 18.0, False),
        ("20220105", "000002.SZ", 13.0, 13.0, 14.3, 11.7, False),
        ("20220106", "000002.SZ", 13.0, 13.0, 14.3, 11.7, False),
    ]
)


class CorporateActionTest(unittest.TestCase):
    def make_broker(self, actions, *, daily=CA_REPLAY, shortable=("000001.SZ", "000002.SZ"), **profile_kw):
        return SimBroker(
            BrokerProfile(**profile_kw),
            MarketData(daily),
            shortable_codes=frozenset(shortable),
            corporate_actions_by_date=actions,
        )

    @staticmethod
    def action(**overrides):
        base = {
            "ts_code": "000001.SZ", "ex_date": "20220105", "record_date": "20220104",
            "pay_date": "20220105", "div_listdate": "", "cash_per_share": 0.5, "stock_per_share": 0.0,
        }
        return base | overrides

    def _events(self, broker, event_type):
        return [e for e in broker.events if e["event_type"] == event_type]

    def test_long_cash_dividend_credited_and_marks_stay_continuous(self):
        broker = self.make_broker({"20220105": [self.action()]})
        broker.execute("000001.SZ", "buy", trade_date="20220104", raw_price=10.0, amount=1000)
        broker.mark_to_market("20220104")
        cash_before, equity_before = broker.stock.cash, broker.equity()
        broker.roll_to_date("20220105")
        self.assertAlmostEqual(broker.stock.cash, cash_before + 500.0)
        # last_price rebases to the theoretical ex price, so the cash credit and the
        # mark drop cancel: equity is continuous across the roll (no tax haircut).
        self.assertAlmostEqual(broker.stock.positions["000001.SZ"].last_price, 9.5)
        self.assertAlmostEqual(broker.equity(), equity_before)
        event = self._events(broker, "dividend_cash")[0]
        self.assertEqual((event["side"], event["account"]), ("long", "stock"))
        self.assertAlmostEqual(event["amount"], 500.0)
        self.assertAlmostEqual(broker.dividend_cash_received, 500.0)
        # Re-rolling the same date must not double-credit.
        broker.roll_to_date("20220105")
        self.assertAlmostEqual(broker.stock.cash, cash_before + 500.0)

    def test_dividend_tax_rate_haircuts_the_long_credit(self):
        broker = self.make_broker({"20220105": [self.action()]}, dividend_tax_rate=0.10)
        broker.execute("000001.SZ", "buy", trade_date="20220104", raw_price=10.0, amount=1000)
        broker.mark_to_market("20220104")
        cash_before, equity_before = broker.stock.cash, broker.equity()
        broker.roll_to_date("20220105")
        self.assertAlmostEqual(broker.stock.cash, cash_before + 450.0)
        self.assertAlmostEqual(broker.equity(), equity_before - 50.0)

    def test_share_bonus_rebases_entry_and_locks_until_listdate(self):
        actions = {"20220105": [self.action(cash_per_share=0.0, stock_per_share=0.5, div_listdate="20220106")]}
        broker = self.make_broker(actions)
        broker.execute("000001.SZ", "buy", trade_date="20220104", raw_price=10.0, amount=1000)
        pos = broker.stock.positions["000001.SZ"]
        entry_cost, entry_price = pos.entry_cost, pos.entry_price
        broker.roll_to_date("20220105")
        self.assertEqual(pos.quantity, 1500)
        # Average-cost continuity: total entry value and cash basis are unchanged.
        self.assertAlmostEqual(pos.entry_price * 1500, entry_price * 1000)
        self.assertEqual(pos.entry_cost, entry_cost)
        # Bonus shares list on 20220106: sellable stays at the original 1000 today.
        self.assertEqual(broker.sellable_quantity("stock", "000001.SZ"), 1000)
        self.assertEqual(self._events(broker, "bonus_shares")[0]["locked_until"], "20220106")
        broker.roll_to_date("20220106")
        self.assertEqual(broker.sellable_quantity("stock", "000001.SZ"), 1500)

    def test_odd_lot_position_from_bonus_can_exit_in_one_shot(self):
        # 10送3.5 turns 1000 shares into 1350: whole lots plus the ENTIRE 50-share
        # odd tail are declarable (零股必须一次性申报卖出); partial odd pieces reject.
        actions = {"20220105": [self.action(cash_per_share=0.0, stock_per_share=0.35, div_listdate="20220105")]}
        broker = self.make_broker(actions)
        broker.execute("000001.SZ", "buy", trade_date="20220104", raw_price=10.0, amount=1000)
        broker.roll_to_date("20220105")
        self.assertEqual(broker.stock.positions["000001.SZ"].quantity, 1350)
        partial_odd = broker.execute("000001.SZ", "sell", trade_date="20220105", raw_price=10.0, amount=120)
        self.assertEqual(partial_odd.reject_reason, "amount_not_lot_aligned")
        lot_plus_tail = broker.execute("000001.SZ", "sell", trade_date="20220105", raw_price=10.0, amount=150)
        self.assertEqual(lot_plus_tail.status, "filled")
        remainder = broker.execute("000001.SZ", "sell", trade_date="20220105", raw_price=10.0, amount=1200)
        self.assertEqual(remainder.status, "filled")
        self.assertEqual(broker.position_quantity("000001.SZ"), 0)

    def test_share_bonus_listing_on_ex_date_is_sellable_immediately(self):
        actions = {"20220105": [self.action(cash_per_share=0.0, stock_per_share=0.5, div_listdate="20220105")]}
        broker = self.make_broker(actions)
        broker.execute("000001.SZ", "buy", trade_date="20220104", raw_price=10.0, amount=1000)
        broker.roll_to_date("20220105")
        self.assertEqual(broker.sellable_quantity("stock", "000001.SZ"), 1500)

    def test_short_compensates_gross_cash_and_scales_liability(self):
        # 000002.SZ pays 0.5/share and converts 0.5/share; the short owes both.
        actions = {"20220105": [self.action(ts_code="000002.SZ", cash_per_share=0.5, stock_per_share=0.5)]}
        broker = self.make_broker(actions, dividend_tax_rate=0.10)  # tax never applies to shorts
        broker.execute("000002.SZ", "short", trade_date="20220104", raw_price=20.0, amount=1000)
        broker.mark_to_market("20220104")
        cash_before, equity_before = broker.credit.cash, broker.equity()
        contract = next(c for c in broker.credit.contracts if c.kind == "slo")
        interest_base = contract.shares * contract.open_price
        broker.roll_to_date("20220105")
        pos = broker.credit.positions["000002.SZ"]
        self.assertEqual(pos.quantity, 1500)
        self.assertEqual(contract.shares, 1500)
        self.assertAlmostEqual(contract.shares * contract.open_price, interest_base)  # fee basis preserved
        self.assertAlmostEqual(broker.credit.cash, cash_before - 500.0)
        self.assertAlmostEqual(broker.dividend_compensation_paid, 500.0)
        # Marks: 1000 × 20 liability becomes 1500 × 13 + 500 compensation — continuous.
        self.assertAlmostEqual(pos.last_price, 13.0)
        self.assertAlmostEqual(broker.equity(), equity_before, places=6)
        # The scaled liability covers cleanly: the position/contract invariant held.
        cover = broker.execute("000002.SZ", "cover", trade_date="20220105", raw_price=13.0, amount=1500)
        self.assertEqual(cover.status, "filled")
        self.assertNotIn("000002.SZ", broker.credit.positions)
        self.assertTrue(all(c.closed for c in broker.credit.contracts))

    def test_disabled_mode_and_unheld_codes_are_untouched(self):
        broker = self.make_broker({"20220105": [self.action()]}, corporate_actions="disabled")
        broker.execute("000001.SZ", "buy", trade_date="20220104", raw_price=10.0, amount=1000)
        cash_before = broker.stock.cash
        broker.roll_to_date("20220105")
        self.assertEqual(broker.stock.cash, cash_before)
        self.assertEqual(self._events(broker, "dividend_cash"), [])

        held_nothing = self.make_broker({"20220105": [self.action(ts_code="000002.SZ")]})
        held_nothing.execute("000001.SZ", "buy", trade_date="20220104", raw_price=10.0, amount=1000)
        held_nothing.roll_to_date("20220105")
        self.assertEqual(self._events(held_nothing, "dividend_cash"), [])

    def test_record_date_gap_is_audited_but_still_applied(self):
        broker = self.make_broker({"20220105": [self.action(record_date="20211230")]})
        broker.execute("000001.SZ", "buy", trade_date="20220104", raw_price=10.0, amount=1000)
        cash_before = broker.stock.cash
        broker.roll_to_date("20220105")
        gap = self._events(broker, "corporate_action_calendar_gap")[0]
        self.assertEqual(gap["expected_record_date"], "20220104")
        self.assertAlmostEqual(broker.stock.cash, cash_before + 500.0)

    def test_suspended_ex_date_still_applies_and_rebases_the_stale_mark(self):
        suspended = make_daily(
            [
                ("20220104", "000001.SZ", 10.0, 10.0, 11.0, 9.0, False),
                ("20220105", "000001.SZ", None, None, None, None, True),
                ("20220106", "000001.SZ", 9.5, 9.5, 10.45, 8.55, False),
            ]
        )
        broker = self.make_broker({"20220105": [self.action()]}, daily=suspended)
        broker.execute("000001.SZ", "buy", trade_date="20220104", raw_price=10.0, amount=1000)
        broker.mark_to_market("20220104")
        equity_before = broker.equity()
        broker.roll_to_date("20220105")
        broker.mark_to_market("20220105")  # suspended: no close, the rebased mark stands
        self.assertAlmostEqual(broker.stock.positions["000001.SZ"].last_price, 9.5)
        self.assertAlmostEqual(broker.equity(), equity_before)

    def test_loader_reads_slot_file_and_tolerates_absence(self):
        with tempfile.TemporaryDirectory() as tmp:
            from autotrade.environment.broker import load_corporate_actions_by_date

            self.assertEqual(load_corporate_actions_by_date(tmp), {})
            frame = pd.DataFrame(
                [
                    self.action(),
                    self.action(ts_code="000002.SZ", ex_date="20220106", cash_per_share=0.2),
                ]
            )
            frame.to_parquet(Path(tmp) / "corporate_actions.parquet", index=False)
            loaded = load_corporate_actions_by_date(tmp)
            self.assertEqual(sorted(loaded), ["20220105", "20220106"])
            self.assertEqual(loaded["20220105"][0]["ts_code"], "000001.SZ")
            self.assertAlmostEqual(loaded["20220106"][0]["cash_per_share"], 0.2)

    def test_replay_folds_dividends_into_returns_and_attribution(self):
        def buy_hold(state):
            if state["cur_time"] != "09:25" or state["cur_date"] != "20220104" or _held(state, "000001.SZ"):
                return []
            return [{"action": "buy", "ts_code": "000001.SZ", "amount": 1000}]

        replay = run_main_ctx_replay(
            CA_REPLAY,
            BrokerProfile(),
            shortable_codes=frozenset(),
            corporate_actions_by_date={"20220105": [self.action()]},
            main_policy=FakeMainPolicy(buy_hold),
        )
        stats = compute_return_stats(replay)
        self.assertAlmostEqual(stats["dividend_cash_received"], 500.0)
        realized = sum(
            e["realized_pnl"]
            for e in replay.broker.events
            if e["event_type"] in {"position_closed", "position_reduced"}
        )
        initial = replay.broker.initial_equity
        # The dividend is inside both total return (via cash/equity) and the long
        # attribution; without it the raw ex-date gap would read as a ~500 CNY loss.
        self.assertAlmostEqual(stats["final_equity"], initial + realized + 500.0, places=6)
        self.assertAlmostEqual(stats["long_return"], (realized + 500.0) / initial, places=9)
        self.assertGreater(stats["long_return"], -0.0001)  # ≈ round-trip costs only


class FillRealismTest(unittest.TestCase):
    """Fill-model realism: strict trade-through limit fills, market orders resting
    across printless minutes, forced-close proceeds repaying 融资 debt, BSE lots."""

    def make_broker(self, **profile_kw):
        profile = BrokerProfile(**profile_kw)
        return SimBroker(profile, MarketData(REPLAY), shortable_codes=frozenset({"000001.SZ", "000002.SZ"}))

    @staticmethod
    def bar_group(open_, high, low, close, code="000001.SZ"):
        return pd.DataFrame([{"ts_code": code, "open": open_, "high": high, "low": low, "close": close}])

    def test_tick_bar_lookup_filters_codes_and_keeps_last_duplicate(self):
        from autotrade.environment.broker import _bars_for_codes

        rows = pd.DataFrame(
            [
                {"ts_code": 1, "open": 10.0, "close": 10.1},
                {"ts_code": "ignored", "open": 20.0, "close": 20.1},
                {"ts_code": "1", "open": 11.0, "close": 11.1},
            ]
        )
        lookup = _bars_for_codes(rows, {"1", "missing"})

        self.assertEqual(set(lookup), {"1"})
        self.assertEqual(float(lookup["1"]["open"]), 11.0)
        self.assertEqual(float(lookup["1"]["close"]), 11.1)

    def test_same_bar_fifo_releases_settled_cash_orders(self):
        for op_type, account, cash_kwargs in (
            (optype.STOCK_BUY, "stock", {"stock_initial_cash": 31_000.0, "credit_initial_cash": 1.0}),
            (optype.CREDIT_BUY, "credit", {"stock_initial_cash": 1.0, "credit_initial_cash": 31_000.0}),
        ):
            with self.subTest(account=account):
                broker = self.make_broker(**cash_kwargs)
                for index in range(3):
                    broker.passorder(
                        op_type, 1101, "", "000001.SZ", prtype.PEER, 0, 1000,
                        reserve_price=10.0, user_order_id=f"{account}-{index}",
                    )
                broker.match_bar("20220104", "09:31", self.bar_group(10.0, 10.1, 9.9, 10.0))
                self.assertEqual(broker.position_quantity("000001.SZ", account=account), 3000)
                self.assertEqual([order.status for order in broker.orders], ["filled"] * 3)
                self.assertTrue(all(order.price == BrokerProfile().slipped_price(10.0, is_buy=True) for order in broker.orders))
                self.assertGreater(broker.fees_paid, 0.0)
                self.assertLess(broker.accounts[account].cash, 1000.0)

    def test_same_bar_fifo_first_order_wins_when_batch_exceeds_cash(self):
        broker = self.make_broker(stock_initial_cash=15_000.0, credit_initial_cash=1.0)
        for order_id in ("first", "second"):
            broker.passorder(
                optype.STOCK_BUY, 1101, "", "000001.SZ", prtype.PEER, 0, 1000,
                reserve_price=10.0, user_order_id=order_id,
            )
        broker.match_bar("20220104", "09:31", self.bar_group(10.0, 10.1, 9.9, 10.0))
        self.assertEqual([(o.order_id, o.status) for o in broker.orders], [
            ("first", "filled"), ("second", "rejected"),
        ])
        self.assertEqual(broker.orders[-1].reject_reason, "insufficient_cash")

    def test_same_bar_rejected_predecessor_releases_cash(self):
        broker = self.make_broker(stock_initial_cash=15_000.0, credit_initial_cash=1.0)
        broker.passorder(
            optype.STOCK_BUY, 1101, "", "000001.SZ", prtype.PEER, 0, 2000,
            reserve_price=10.0, user_order_id="too-large",
        )
        broker.passorder(
            optype.STOCK_BUY, 1101, "", "000001.SZ", prtype.PEER, 0, 1000,
            reserve_price=10.0, user_order_id="fits",
        )
        broker.match_bar("20220104", "09:31", self.bar_group(10.0, 10.1, 9.9, 10.0))
        self.assertEqual([(o.order_id, o.status) for o in broker.orders], [
            ("too-large", "rejected"), ("fits", "filled"),
        ])

    def test_earlier_resting_limit_reserves_cash_until_cancelled(self):
        broker = self.make_broker(stock_initial_cash=15_000.0, credit_initial_cash=1.0)
        broker.passorder(
            optype.STOCK_BUY, 1101, "", "000001.SZ", prtype.FIX, 9.5, 1000,
            user_order_id="resting",
        )
        broker.passorder(
            optype.STOCK_BUY, 1101, "", "000001.SZ", prtype.PEER, 0, 1000,
            reserve_price=10.0, user_order_id="blocked",
        )
        broker.match_bar("20220104", "09:31", self.bar_group(10.0, 10.1, 9.8, 10.0))
        self.assertEqual([(o.order_id, o.status) for o in broker.orders], [("blocked", "rejected")])
        self.assertTrue(broker.cancel("resting"))
        broker.passorder(
            optype.STOCK_BUY, 1101, "", "000001.SZ", prtype.PEER, 0, 1000,
            reserve_price=10.0, user_order_id="released",
        )
        broker.match_bar("20220104", "09:32", self.bar_group(10.0, 10.1, 9.8, 10.0))
        self.assertEqual(broker.orders[-1].status, "filled")

    def test_same_bar_reduce_orders_share_sellable_quantity_in_fifo_order(self):
        broker = self.make_broker()
        broker.execute("000001.SZ", "buy", trade_date="20220104", raw_price=10.0, amount=1000)
        broker.roll_to_date("20220105")
        for order_id in ("sell-1", "sell-2"):
            broker.passorder(
                optype.STOCK_SELL, 1101, "", "000001.SZ", prtype.PEER, 0, 500,
                reserve_price=10.5, user_order_id=order_id,
            )
        broker.match_bar("20220105", "09:31", self.bar_group(10.5, 10.6, 10.4, 10.5))
        self.assertEqual(broker.position_quantity("000001.SZ", account="stock"), 0)
        self.assertEqual([o.status for o in broker.orders[-2:]], ["filled", "filled"])

    def test_same_bar_fin_buy_uses_bail_balance_in_fifo_order(self):
        broker = self.make_broker(stock_initial_cash=1.0, credit_initial_cash=50_000.0)
        for order_id in ("fin-1", "fin-2"):
            broker.passorder(
                optype.FIN_BUY, 1101, "", "000001.SZ", prtype.PEER, 0, 3000,
                reserve_price=10.0, user_order_id=order_id,
            )
        broker.match_bar("20220104", "09:31", self.bar_group(10.0, 10.1, 9.9, 10.0))
        self.assertEqual([(o.order_id, o.status) for o in broker.orders], [
            ("fin-1", "filled"), ("fin-2", "rejected"),
        ])
        self.assertEqual(broker.orders[-1].reject_reason, "insufficient_bail_balance")

    def test_limit_buy_needs_strict_trade_through(self):
        broker = self.make_broker()
        broker.passorder(optype.STOCK_BUY, 1101, "", "000001.SZ", prtype.FIX, 9.8, 1000)
        # A bare touch (low == limit) leaves the order resting in the queue.
        broker.match_bar("20220104", "09:31", self.bar_group(10.0, 10.1, 9.8, 9.9))
        self.assertEqual(len(broker.working_orders()), 1)
        self.assertEqual(broker.position_quantity("000001.SZ"), 0)
        # Trading strictly through the limit fills at the limit.
        broker.match_bar("20220104", "09:32", self.bar_group(10.0, 10.1, 9.79, 9.9))
        self.assertEqual(broker.position_quantity("000001.SZ"), 1000)
        fill = next(e for e in broker.events if e["event_type"] == "order_filled")
        self.assertEqual(fill["price"], 9.8)

    def test_limit_sell_needs_strict_trade_through(self):
        broker = self.make_broker()
        broker.execute("000001.SZ", "buy", trade_date="20220104", raw_price=10.0, amount=1000)
        broker.roll_to_date("20220105")
        broker.passorder(optype.STOCK_SELL, 1101, "", "000001.SZ", prtype.FIX, 10.6, 1000)
        broker.match_bar("20220105", "09:31", self.bar_group(10.5, 10.6, 10.4, 10.5))  # touch only
        self.assertEqual(len(broker.working_orders()), 1)
        broker.match_bar("20220105", "09:32", self.bar_group(10.5, 10.61, 10.4, 10.5))  # through
        self.assertEqual(broker.position_quantity("000001.SZ"), 0)

    def test_market_order_rests_across_printless_minutes(self):
        broker = self.make_broker()
        broker.roll_to_date("20220104")
        cash_before = _asset(broker, "stock")["available_cash"]
        broker.passorder(
            optype.STOCK_BUY, 1101, "", "000001.SZ", prtype.PEER, 0, 1000, reserve_price=10.0
        )
        empty = pd.DataFrame(columns=["ts_code", "open", "high", "low", "close"])
        for minute in ("09:31", "09:32", "09:33"):
            broker.match_bar("20220104", minute, empty)
        # Still working after printless minutes, and still reserving buying power.
        self.assertEqual(len(broker.working_orders()), 1)
        self.assertLess(_asset(broker, "stock")["available_cash"], cash_before - 9000)
        broker.match_bar("20220104", "09:40", self.bar_group(10.2, 10.3, 10.1, 10.2))
        self.assertEqual(broker.position_quantity("000001.SZ"), 1000)
        fill = next(e for e in broker.events if e["event_type"] == "order_filled")
        self.assertAlmostEqual(fill["price"], BrokerProfile().slipped_price(10.2, is_buy=True))

    def test_unmatched_auction_order_rolls_into_continuous_matching(self):
        broker = self.make_broker()
        broker.passorder(optype.STOCK_BUY, 1101, "", "000001.SZ", prtype.PEER, 0, 1000, is_auction=True)
        empty = pd.DataFrame(columns=["ts_code", "open", "high", "low", "close"])
        broker.match_bar("20220104", "09:30", empty)  # missed its single-price bar
        broker.match_bar("20220104", "09:31", self.bar_group(10.0, 10.1, 9.9, 10.0))
        fill = next(e for e in broker.events if e["event_type"] == "order_filled")
        # Continuous taker fill: the auction's slippage-free treatment is gone.
        self.assertEqual(fill["price_label"], "minute:09:31")
        self.assertAlmostEqual(fill["price"], BrokerProfile().slipped_price(10.0, is_buy=True))

    def test_limit_order_rests_across_printless_minutes(self):
        broker = self.make_broker()
        order_id = broker.passorder(optype.STOCK_BUY, 1101, "", "000001.SZ", prtype.FIX, 9.5, 1000)
        empty = pd.DataFrame(columns=["ts_code", "open", "high", "low", "close"])
        broker.match_bar("20220104", "09:31", empty)
        broker.match_bar("20220104", "09:32", empty)
        self.assertEqual([o["order_id"] for o in broker.working_orders()], [order_id])

    def test_forced_close_proceeds_repay_fin_debt(self):
        crash = make_daily(
            [
                ("20220104", "000001.SZ", 10.0, 10.0, 11.0, 9.0, False),
                ("20220105", "000001.SZ", 0.9, 0.9, 0.99, 0.81, False),
                ("20220106", "000001.SZ", 0.9, 0.9, 0.99, 0.81, False),
            ]
        )
        # Just enough credit cash to open the financed position; the crash then
        # drops the maintenance ratio through the 1.30 closeout line.
        broker = SimBroker(
            BrokerProfile(credit_initial_cash=60_000.0),
            MarketData(crash),
            shortable_codes=frozenset({"000001.SZ"}),
        )
        broker.execute("000001.SZ", "fin_buy", trade_date="20220104", raw_price=10.0, amount=5000)
        broker.mark_to_market("20220104")
        principal = sum(c.principal for c in broker.credit.contracts if c.kind == "fin")
        self.assertGreater(principal, 50_000.0)
        # The crash breaches the maintenance line; the forced close's proceeds must
        # repay the 融资 contracts (interest first) instead of idling in cash.
        broker.mark_to_market("20220105")
        self.assertTrue(any(e["event_type"] == "forced_close_triggered" for e in broker.events))
        repaid = next(e for e in broker.events if e["event_type"] == "debt_repaid" and e.get("via") == "forced_close")
        self.assertGreater(repaid["principal_paid"], 4_000.0)  # ~5000 shares × ~0.9 net of costs
        self.assertGreater(repaid["interest_paid"], 0.0)
        remaining = sum(c.principal for c in broker.credit.contracts if c.kind == "fin" and not c.closed)
        self.assertAlmostEqual(remaining, principal - repaid["principal_paid"], places=6)
        # No further interest on the repaid part; equity still nets the shortfall.
        self.assertLess(broker.account_equity("credit"), 60_000.0)

    def test_voluntary_credit_sell_keeps_proceeds_in_cash(self):
        broker = self.make_broker()
        broker.execute("000002.SZ", "credit_buy", trade_date="20220104", raw_price=20.0, amount=500)
        broker.execute("000001.SZ", "fin_buy", trade_date="20220104", raw_price=10.0, amount=1000)
        broker.roll_to_date("20220105")
        principal = sum(c.principal for c in broker.credit.contracts if c.kind == "fin")
        broker.execute("000002.SZ", "credit_sell", trade_date="20220105", raw_price=19.0, amount=500)
        # Selling plain collateral does not force-repay 融资 debt (only sell_repay does).
        self.assertAlmostEqual(
            sum(c.principal for c in broker.credit.contracts if c.kind == "fin"), principal
        )

    def test_bse_lot_rule_allows_single_share_increments_above_100(self):
        self.assertEqual(SimBroker.validate_share_amount(150, "830799.BJ"), (150, None))
        self.assertEqual(SimBroker.validate_share_amount(101, "430047.BJ"), (101, None))
        self.assertEqual(SimBroker.validate_share_amount(99, "830799.BJ"), (0, "amount_below_lot_size"))
        # Non-BSE boards keep the 100-multiple rule.
        self.assertEqual(SimBroker.validate_share_amount(150, "000001.SZ"), (0, "amount_not_lot_aligned"))

    def test_close_auction_limit_clears_at_single_price(self):
        # The close call auction matches every order at ONE price: a buy limit
        # below the auction price must not fill retroactively against the bar's
        # intraday low, which predates the order by hours.
        broker = self.make_broker()
        broker.passorder(
            optype.STOCK_BUY, 1101, "", "000001.SZ", prtype.FIX, 9.5, 1000,
            is_auction=True, auction_close=True,
        )
        broker.match_bar("20220104", "15:00", self.bar_group(10.0, 10.4, 9.0, 10.3))
        self.assertEqual(broker.position_quantity("000001.SZ"), 0)
        self.assertEqual(len(broker.working_orders()), 1)  # rests; the day-end sweep voids it
        # A marketable close-auction limit clears at the auction price, not the limit.
        broker.passorder(
            optype.STOCK_BUY, 1101, "", "000001.SZ", prtype.FIX, 10.5, 1000,
            is_auction=True, auction_close=True,
        )
        broker.match_bar("20220104", "15:00", self.bar_group(10.0, 10.4, 9.0, 10.3))
        fill = next(e for e in broker.events if e["event_type"] == "order_filled")
        self.assertAlmostEqual(fill["price"], 10.3)

    def test_auction_print_overrides_bar_reference(self):
        # With the day's actual call-auction print in the replay slot, auction
        # orders clear at the print, not the bar open/close approximation.
        broker = self.make_broker()
        broker.auction_prints_by_date = {("20220104", "open"): {"000001.SZ": 10.07}}
        broker.passorder(optype.STOCK_BUY, 1101, "", "000001.SZ", prtype.PEER, 0, 1000, is_auction=True)
        broker.match_bar("20220104", "09:30", self.bar_group(10.0, 10.1, 9.9, 10.05))
        fill = next(e for e in broker.events if e["event_type"] == "order_filled")
        self.assertAlmostEqual(fill["price"], 10.07)  # print, slippage-free
        self.assertEqual(fill["price_label"], "auction")
        # Close auctions have no print source (stk_auction covers the open
        # session only): they keep clearing at the final bar CLOSE.
        broker.passorder(optype.STOCK_BUY, 1101, "", "000001.SZ", prtype.FIX, 10.2, 1000,
                         is_auction=True, auction_close=True)
        broker.match_bar("20220104", "15:00", self.bar_group(10.0, 10.4, 9.0, 10.3))
        self.assertEqual(len(broker.working_orders()), 1)  # 10.2 < close 10.3
        broker.passorder(optype.STOCK_BUY, 1101, "", "000001.SZ", prtype.FIX, 10.5, 1000,
                         is_auction=True, auction_close=True)
        broker.match_bar("20220104", "15:00", self.bar_group(10.0, 10.4, 9.0, 10.3))
        fills = [e for e in broker.events if e["event_type"] == "order_filled"]
        self.assertAlmostEqual(fills[-1]["price"], 10.3)
        # A day without prints keeps the bar-based semantics.
        broker2 = self.make_broker()
        broker2.passorder(optype.STOCK_BUY, 1101, "", "000001.SZ", prtype.PEER, 0, 1000, is_auction=True)
        broker2.match_bar("20220104", "09:30", self.bar_group(10.0, 10.1, 9.9, 10.05))
        fill2 = next(e for e in broker2.events if e["event_type"] == "order_filled")
        self.assertAlmostEqual(fill2["price"], 10.0)  # bar open

    def test_auction_loader_uses_stk_auction_price(self):
        with tempfile.TemporaryDirectory() as tmp:
            replay = Path(tmp)
            pd.DataFrame([
                {"trade_date": "20220104", "session": "open", "ts_code": "000001.SZ",
                 "price": 10.0, "vol": 1000, "amount": 10000},
            ]).to_parquet(replay / "auction.parquet", index=False)

            prints = load_auction_prints_by_date(replay)

            self.assertEqual(prints[("20220104", "open")]["000001.SZ"], 10.0)

    def test_auction_loader_recovers_missing_price_and_preserves_no_trade(self):
        with tempfile.TemporaryDirectory() as tmp:
            replay = Path(tmp)
            pd.DataFrame([
                {"trade_date": "20220104", "session": "open", "ts_code": "000001.SZ",
                 "price": None, "vol": 1000, "amount": 10000},
                {"trade_date": "20220104", "session": "open", "ts_code": "000002.SZ",
                 "price": 99.0, "vol": 0, "amount": 0},
            ]).to_parquet(replay / "auction.parquet", index=False)

            prints = load_auction_prints_by_date(replay)

            self.assertEqual(prints[("20220104", "open")]["000001.SZ"], 10.0)
            self.assertEqual(prints[("20220104", "open")]["000002.SZ"], 0.0)

    def test_auction_loader_rejects_price_quantity_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            replay = Path(tmp)
            pd.DataFrame([
                {"trade_date": "20220104", "session": "open", "ts_code": "000001.SZ",
                 "price": 100.0, "vol": 1000, "amount": 10000},
            ]).to_parquet(replay / "auction.parquet", index=False)

            with self.assertRaisesRegex(ValueError, "inconsistent opening price"):
                load_auction_prints_by_date(replay)

    def test_explicit_no_auction_trade_rolls_order_to_continuous_session(self):
        broker = self.make_broker()
        broker.auction_prints_by_date = {("20220104", "open"): {"000001.SZ": 0.0}}
        broker.passorder(optype.STOCK_BUY, 1101, "", "000001.SZ", prtype.PEER, 0, 1000, is_auction=True)

        broker.match_bar("20220104", "09:30", self.bar_group(10.0, 10.1, 9.9, 10.05))
        self.assertEqual(broker.position_quantity("000001.SZ"), 0)
        broker.match_bar("20220104", "09:31", self.bar_group(10.1, 10.2, 10.0, 10.15))

        fill = next(e for e in broker.events if e["event_type"] == "order_filled")
        self.assertEqual(fill["price_label"], "minute:09:31")

    def test_auction_loader_rejects_duplicate_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            replay = Path(tmp)
            row = {"trade_date": "20220104", "session": "open", "ts_code": "000001.SZ",
                   "price": 10.0}
            pd.DataFrame([row, row]).to_parquet(replay / "auction.parquet", index=False)

            with self.assertRaisesRegex(ValueError, "duplicate clearing-price keys"):
                load_auction_prints_by_date(replay)

    def test_out_of_universe_order_rejects_at_submission(self):
        # A code with no data anywhere in the replay region (e.g. screened out
        # of the research universe) must fail fast, not rest all day and void
        # as day_end_unfilled.
        broker = self.make_broker()
        broker.passorder(optype.STOCK_BUY, 1101, "", "999999.SZ", prtype.PEER, 0, 1000)
        order = next(o for o in broker.orders if o.ts_code == "999999.SZ")
        self.assertEqual(order.status, "rejected")
        self.assertEqual(order.reject_reason, "code_not_in_universe")
        self.assertEqual(len(broker.working_orders()), 0)

    def test_unfilled_auction_limit_degrades_to_continuous_order(self):
        # A limit auction order that does not clear at the single price rolls
        # into continuous matching (real unmatched 集合竞价 semantics) — the
        # auction print must not pin single-price forever.
        broker = self.make_broker()
        broker.auction_prints_by_date = {("20220104", "open"): {"000001.SZ": 10.30}}
        broker.passorder(optype.STOCK_BUY, 1101, "", "000001.SZ", prtype.FIX, 10.1, 1000, is_auction=True)
        broker.match_bar("20220104", "09:30", self.bar_group(10.3, 10.35, 10.25, 10.3))
        self.assertEqual(broker.position_quantity("000001.SZ"), 0)  # 10.1 < print 10.30
        # Continuous bar trades strictly through the limit -> fills at the limit.
        broker.match_bar("20220104", "09:31", self.bar_group(10.2, 10.25, 10.05, 10.1))
        fill = next(e for e in broker.events if e["event_type"] == "order_filled")
        self.assertAlmostEqual(fill["price"], 10.1)
        self.assertEqual(fill["price_label"], "minute:09:31")

    def test_synthetic_fallback_bar_has_no_range_trade_through(self):
        # A synthetic daily-fallback bar's high/low span the whole session, so a
        # resting limit order must not fill against prices that predate it; the
        # same shape on a real bar still fills by strict trade-through.
        broker = self.make_broker()
        broker.passorder(optype.STOCK_BUY, 1101, "", "000001.SZ", prtype.FIX, 9.5, 1000)
        broker.match_bar("20220104", "15:00", self.bar_group(10.3, 10.4, 9.0, 10.3).assign(synthetic=True))
        self.assertEqual(broker.position_quantity("000001.SZ"), 0)
        broker.match_bar("20220104", "15:00", self.bar_group(10.3, 10.4, 9.0, 10.3))
        self.assertEqual(broker.position_quantity("000001.SZ"), 1000)

    def test_match_bar_marks_positions_at_open_for_admission_and_close_after(self):
        # Two-phase re-marking: margin admission values existing holdings at THIS
        # bar's open (a gapped-down collateral cannot back new credit exposure at
        # yesterday's close), and the post-bar view uses this bar's close.
        crash = make_daily(
            [
                ("20220104", "000001.SZ", 10.0, 10.0, 11.0, 9.0, False),
                ("20220104", "000002.SZ", 20.0, 20.0, 22.0, 18.0, False),
                ("20220105", "000001.SZ", 10.0, 10.0, 11.0, 9.0, False),
                ("20220105", "000002.SZ", 6.0, 6.5, 22.0, 5.0, False),
            ]
        )
        broker = SimBroker(
            BrokerProfile(credit_initial_cash=25_000.0),
            MarketData(crash),
            shortable_codes=frozenset({"000001.SZ", "000002.SZ"}),
        )
        broker.execute("000002.SZ", "credit_buy", trade_date="20220104", raw_price=20.0, amount=1000)
        broker.roll_to_date("20220105")
        broker.passorder(optype.FIN_BUY, 1101, "", "000001.SZ", prtype.FIX, 10.0, 1000)
        group = pd.DataFrame(
            [
                {"ts_code": "000001.SZ", "open": 10.0, "high": 10.1, "low": 9.9, "close": 10.0},
                {"ts_code": "000002.SZ", "open": 6.0, "high": 6.6, "low": 5.9, "close": 6.5},
            ]
        )
        broker.match_bar("20220105", "09:31", group)
        # At yesterday's 20.0 collateral mark the bail balance (~19k) would admit the
        # ~10k fin_buy; at this bar's 6.0 open (~9.2k) it must reject.
        self.assertEqual(broker.orders[-1].reject_reason, "insufficient_bail_balance")
        # Post-match marks use the bar close: the agent-visible account reflects 6.5.
        self.assertAlmostEqual(broker.credit.positions["000002.SZ"].last_price, 6.5)


class OrderEvidenceTest(unittest.TestCase):
    def make_broker(self):
        return SimBroker(BrokerProfile(), MarketData(REPLAY), shortable_codes=frozenset({"000001.SZ"}))

    def test_cancelled_order_stays_in_order_records(self):
        broker = self.make_broker()
        order_id = broker.passorder(
            optype.STOCK_BUY, 1101, "", "000001.SZ", prtype.FIX, 9.5, 1000, submitted_at="09:31"
        )
        self.assertTrue(broker.cancel(order_id, reason="day_end_unfilled", trade_date="20220104", minute_key="15:00"))
        (record,) = [
            o for o in broker.get_trade_detail_data(account_type="STOCK", data_type="ORDER")
            if o["order_id"] == order_id
        ]
        self.assertEqual(record["status"], "cancelled")
        self.assertEqual(record["reject_reason"], "day_end_unfilled")
        self.assertEqual(record["limit_price"], 9.5)
        self.assertEqual(record["submitted_at"], "09:31")

    def test_fill_records_carry_submit_time_limit_price_and_fees(self):
        broker = self.make_broker()
        broker.passorder(optype.STOCK_BUY, 1101, "", "000001.SZ", prtype.FIX, 9.8, 1000, submitted_at="09:31")
        broker.match_bar(
            "20220104", "09:33",
            pd.DataFrame([{"ts_code": "000001.SZ", "open": 10.0, "high": 10.1, "low": 9.79, "close": 9.9}]),
        )
        (record,) = broker.get_trade_detail_data(account_type="STOCK", data_type="ORDER")
        self.assertEqual(record["status"], "filled")
        self.assertEqual(record["submitted_at"], "09:31")  # submit time survives the resting bars
        self.assertEqual(record["limit_price"], 9.8)  # original limit; price is the fill
        self.assertEqual(record["decision_time"], "09:33")
        self.assertGreater(record["fee"], 0.0)
        broker.roll_to_date("20220105")
        broker.execute("000001.SZ", "sell", trade_date="20220105", raw_price=10.6, amount=1000)
        sell = [o for o in broker.get_trade_detail_data(account_type="STOCK", data_type="ORDER") if o["action"] == "sell"][0]
        self.assertGreater(sell["fee"], 0.0)
        self.assertGreater(sell["stamp_duty"], 0.0)

    def test_positions_eod_records_reflect_marks_and_exit_liquidation(self):
        broker = self.make_broker()
        broker.execute("000001.SZ", "buy", trade_date="20220104", raw_price=10.0, amount=1000)
        broker.mark_to_market("20220104")
        rows = broker.positions_eod_records()
        self.assertEqual(
            [(r["date"], r["account"], r["ts_code"], r["side"], r["quantity"]) for r in rows],
            [("20220104", "stock", "000001.SZ", "long", 1000)],
        )
        self.assertAlmostEqual(rows[0]["last_price"], 10.5)  # marked to the daily close
        broker.roll_to_date("20220105")
        broker.mark_to_market("20220105")
        broker.close_all("20220105")
        broker.record_positions_eod("20220105")  # exit-day refresh (the engine does this)
        self.assertEqual([r["date"] for r in broker.positions_eod_records()], ["20220104"])


class BrokerProfileValidationTest(unittest.TestCase):
    def test_negative_fees_rates_and_slippage_are_rejected(self):
        for field_name in ("commission_bps", "slippage_bps", "transfer_fee_bps", "fin_rate_annual"):
            with self.assertRaisesRegex(ValueError, field_name):
                BrokerProfile(**{field_name: -1.0})

    def test_nan_numeric_fields_are_rejected(self):
        for field_name in ("stock_initial_cash", "slippage_bps", "fin_margin_ratio", "maintenance_closeout_ratio"):
            with self.assertRaises(ValueError):
                BrokerProfile(**{field_name: float("nan")})

    def test_maintenance_ratio_ordering_is_enforced(self):
        with self.assertRaisesRegex(ValueError, "maintenance ratios"):
            BrokerProfile(maintenance_closeout_ratio=1.5, maintenance_warning_ratio=1.4)


class ReturnStatsTest(unittest.TestCase):
    def test_day0_baseline_counts_first_day_loss(self):
        # 1,000,000 initial -> 700,000 -> 1,010,000: total return ~+1%, and the
        # -30% first day / initial-equity peak must enter drawdown and Sharpe
        # instead of being dropped by the first pct_change.
        broker = SimBroker(BrokerProfile(), MarketData(REPLAY), shortable_codes=frozenset())
        result = ReplayResult(
            equity_curve=pd.Series({"20220104": 700_000.0, "20220105": 1_010_000.0}),
            broker=broker, decision_date="20220104", exit_date="20220105",
        )
        stats = compute_return_stats(result)
        self.assertAlmostEqual(stats["total_return"], 0.01)
        self.assertAlmostEqual(stats["max_drawdown"], 0.30)
        self.assertNotEqual(stats["sharpe"], 0.0)
        self.assertTrue(stats["liquidation_complete"])
        self.assertEqual(stats["unliquidated_positions"], [])
        self.assertEqual(stats["remaining_liabilities"], 0.0)

    def test_exposure_and_weekly_decomposition(self):
        # Gross exposure from EOD snapshots over same-day equity, and ISO-week
        # compounded returns with the initial-equity baseline.
        broker = SimBroker(BrokerProfile(), MarketData(REPLAY), shortable_codes=frozenset())
        broker.execute("000001.SZ", "buy", trade_date="20220104", raw_price=10.0, amount=1000)
        broker.mark_to_market("20220104")  # EOD market value 1000 * 10.5
        equity1 = broker.equity()
        initial = broker.initial_equity
        result = ReplayResult(
            equity_curve=pd.Series(
                {"20220104": equity1, "20220105": equity1, "20220112": equity1 * 1.01}
            ),
            broker=broker, decision_date="20220104", exit_date="20220112",
        )
        stats = compute_return_stats(result)
        exposure = stats["exposure"]
        self.assertEqual(exposure["replay_days"], 3)
        self.assertEqual(exposure["zero_position_days"], 2)
        self.assertAlmostEqual(exposure["max_gross"], 10_500.0 / equity1)
        self.assertAlmostEqual(exposure["avg_gross"], 10_500.0 / equity1 / 3)
        weekly = stats["weekly_returns"]
        self.assertEqual([entry["week_end"] for entry in weekly], ["20220109", "20220116"])
        self.assertAlmostEqual(weekly[0]["return"], equity1 / initial - 1.0)
        self.assertAlmostEqual(weekly[1]["return"], 0.01)

    def test_exit_day_leftovers_reported_as_incomplete_liquidation(self):
        suspended_exit = make_daily(
            [
                ("20220104", "000001.SZ", 10.0, 10.0, 11.0, 9.0, False),
                ("20220105", "000001.SZ", 10.0, 10.0, 11.0, 9.0, True),
            ]
        )
        broker = SimBroker(BrokerProfile(), MarketData(suspended_exit), shortable_codes=frozenset())
        broker.execute("000001.SZ", "buy", trade_date="20220104", raw_price=10.0, amount=1000)
        broker.mark_to_market("20220104")
        broker.roll_to_date("20220105")
        broker.mark_to_market("20220105")
        broker.close_all("20220105")
        result = ReplayResult(
            equity_curve=pd.Series({"20220104": broker.equity(), "20220105": broker.equity()}),
            broker=broker, decision_date="20220104", exit_date="20220105",
        )
        stats = compute_return_stats(result)
        self.assertFalse(stats["liquidation_complete"])
        (leftover,) = stats["unliquidated_positions"]
        self.assertEqual(leftover["ts_code"], "000001.SZ")
        self.assertEqual(leftover["account"], "stock")
        self.assertEqual(leftover["quantity"], 1000)
        self.assertEqual(leftover["blocked_reason"], "suspended")


# After-hours-eligible replay: main-board dates on/after the 2026-07-06 rule
# revision that extended 盘后固定价格交易 to all A-shares.
AH_REPLAY = make_daily(
    [
        ("20260706", "000001.SZ", 10.0, 10.5, 11.0, 9.0, False),
        ("20260707", "000001.SZ", 10.6, 11.0, 11.6, 9.5, False),
        ("20260708", "000001.SZ", 11.1, 11.5, 12.1, 10.0, False),
    ]
)


class AfterhoursFixedPriceTest(unittest.TestCase):
    def _replay(self, fn, daily=AH_REPLAY, **kwargs):
        kwargs.setdefault("afterhours_decision_time", "15:05")
        return run_main_ctx_replay(
            daily,
            BrokerProfile(),
            shortable_codes=frozenset({"000001.SZ", "000002.SZ"}),
            main_policy=FakeMainPolicy(fn),
            **kwargs,
        )

    def test_availability_by_board_and_date(self):
        from autotrade.environment.broker_core import afterhours_available

        self.assertTrue(afterhours_available("688001.SH", "20200103"))  # STAR since 2019-07
        self.assertFalse(afterhours_available("688001.SH", "20190701"))
        self.assertTrue(afterhours_available("300750.SZ", "20210104"))  # ChiNext since 2020-08
        self.assertFalse(afterhours_available("300750.SZ", "20200103"))
        self.assertTrue(afterhours_available("000001.SZ", "20260706"))  # all A-shares since the revision
        self.assertFalse(afterhours_available("000001.SZ", "20260703"))
        self.assertFalse(afterhours_available("600000.SH", "20251231"))
        self.assertTrue(afterhours_available("830799.BJ", "20260706"))

    def test_missing_amount_rejects_instead_of_liquidating(self):
        def flow(state):
            if state["cur_date"] == "20260706" and state["cur_time"] == "15:05" and not _held(state, "000001.SZ"):
                return [{"action": "buy", "ts_code": "000001.SZ", "amount": 1000}]
            if state["cur_date"] == "20260707" and state["cur_time"] == "15:05" and _held(state, "000001.SZ"):
                return [{"action": "sell", "ts_code": "000001.SZ"}]  # amount omitted
            return []

        replay = self._replay(flow)
        rejected = [o for o in replay.broker.orders
                    if o.action == "sell" and o.reject_reason == "amount_below_lot_size"]
        self.assertEqual(len(rejected), 1)
        afterhours_sells = [e for e in replay.broker.events
                            if e["event_type"] == "order_filled" and e.get("price_label") == "afterhours_fixed"
                            and e.get("action") == "sell"]
        self.assertEqual(afterhours_sells, [])

    def test_fills_at_the_official_close_without_slippage(self):
        def at_afterhours(state):
            if state["cur_time"] == "15:05" and state["cur_date"] == "20260706" and not _held(state, "000001.SZ"):
                return [{"action": "buy", "ts_code": "000001.SZ", "amount": 1000}]
            return []

        replay = self._replay(at_afterhours)
        fill = next(e for e in replay.broker.events if e["event_type"] == "order_filled")
        self.assertEqual(fill["price_label"], "afterhours_fixed")
        self.assertEqual(fill["price"], 10.5)  # the exact close — no slippage, no lag
        self.assertEqual(fill["quantity"], 1000)
        # Settled immediately at the tick: exactly one filled order, nothing pending.
        self.assertEqual([o.status for o in replay.broker.orders], ["filled"])

    def test_tick_absent_when_disabled_and_unique_when_enabled(self):
        seen: list[str] = []

        def record(state):
            seen.append(f"{state['cur_date']} {state['cur_time']}")
            return []

        self._replay(record, afterhours_decision_time=None)
        self.assertFalse(any(t.endswith("15:05") for t in seen))
        seen.clear()
        # A 5-minute off-session grid would land on 15:05 itself: the plan starts
        # the evening grid after the after-hours tick, so the minute stays unique.
        self._replay(record, offsession_tick_minutes=5)
        counts = [t for t in seen if t == "20260706 15:05"]
        self.assertEqual(len(counts), 1)
        self.assertIn("20260706 15:10", seen)

    def test_rejects_board_dates_without_afterhours_session(self):
        def at_afterhours(state):
            if state["cur_time"] == "15:05" and state["cur_date"] == "20220104":
                return [{"action": "buy", "ts_code": "000001.SZ", "amount": 1000}]
            return []

        replay = self._replay(at_afterhours, daily=REPLAY)  # 2022 main-board dates
        self.assertEqual(replay.broker.reject_counts.get("afterhours_not_available"), 1)
        self.assertEqual(replay.broker.position_quantity("000001.SZ"), 0)

    def test_limit_worse_than_close_is_invalid_else_fills_at_close(self):
        def at_afterhours(state):
            if state["cur_time"] == "15:05" and state["cur_date"] == "20260706":
                return [
                    {"action": "buy", "ts_code": "000001.SZ", "amount": 500, "limit": 10.4},
                    {"action": "buy", "ts_code": "000001.SZ", "amount": 500, "limit": 10.6},
                ]
            return []

        replay = self._replay(at_afterhours)
        self.assertEqual(replay.broker.reject_counts.get("afterhours_price_invalid"), 1)
        fill = next(e for e in replay.broker.events if e["event_type"] == "order_filled")
        self.assertEqual(fill["price"], 10.5)  # fixed price: the close, not the limit

    def test_new_leveraged_opens_are_unsupported(self):
        def at_afterhours(state):
            if state["cur_time"] == "15:05" and state["cur_date"] == "20260706":
                return [
                    {"action": "short", "ts_code": "000001.SZ", "amount": 500, "limit": 10.5},
                    {"action": "fin_buy", "ts_code": "000001.SZ", "amount": 500},
                ]
            return []

        replay = self._replay(at_afterhours)
        self.assertEqual(replay.broker.reject_counts.get("afterhours_op_unsupported"), 2)

    def test_t_plus_one_still_binds_then_close_works_next_day(self):
        def strategy(state):
            if state["cur_date"] == "20260706" and state["cur_time"] == "09:25":
                return [{"action": "buy", "ts_code": "000001.SZ", "amount": 1000}]
            if state["cur_time"] == "15:05" and _held(state, "000001.SZ"):
                return [{"action": "close", "ts_code": "000001.SZ"}]
            return []

        replay = self._replay(strategy)
        # Day 1: bought at the 15:00 close bar, so the 15:05 close attempt hits T+1.
        self.assertEqual(replay.broker.reject_counts.get("t_plus_one_no_sellable"), 1)
        # Day 2: the same 15:05 close resolves the holding and exits at that close.
        exit_fill = next(
            e for e in replay.broker.events
            if e["event_type"] == "order_filled" and e["action"] == "sell"
        )
        self.assertEqual(exit_fill["price_label"], "afterhours_fixed")
        self.assertEqual(exit_fill["price"], 11.0)
        closed = next(e for e in replay.broker.events if e["event_type"] == "position_closed")
        self.assertEqual(closed["trade_date"], "20260707")


class DelayedCashOpReleaseTest(unittest.TestCase):
    """Cash operations are not exchange orders: a B>=1 substep transfer or
    direct_repay must not be killed by the orderable-window triple check."""

    def test_delayed_transfer_releases_into_the_preopen_queue(self) -> None:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from autotrade.environment.main_ctx_engine import _DelayedAction, _Tick, _release_delayed_actions

        tz = ZoneInfo("Asia/Shanghai")
        broker = SimBroker(BrokerProfile(), MarketData(REPLAY), shortable_codes=frozenset({"000001.SZ"}))
        broker.roll_to_date("20220104")
        preopen, incoming = [], {}
        when = datetime(2022, 1, 4, 9, 5, tzinfo=tz)
        tick = _Tick(minute_key="09:05", group=REPLAY.iloc[:0], activate_index=None,
                     has_market_event=False, is_auction=False, is_offsession=True)
        delayed = [_DelayedAction(
            seq=0, ready_at=when,
            action={"action": "transfer", "amount": 10_000, "from_account": "stock", "to_account": "credit"},
            substep="plan", generated_at=datetime(2022, 1, 4, 9, 0, tzinfo=tz).isoformat(),
        )]
        _release_delayed_actions(delayed, broker=broker, incoming=incoming, preopen_transfers=preopen,
                                 tick=tick, trade_date="20220104", when=when, n_session=0)
        self.assertEqual(len(preopen), 1)
        self.assertFalse([e for e in broker.events if e["event_type"] == "main_actions_unfilled"])

    def test_delayed_direct_repay_settles_without_a_fill_bar(self) -> None:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from autotrade.environment.main_ctx_engine import _DelayedAction, _Tick, _release_delayed_actions

        tz = ZoneInfo("Asia/Shanghai")
        broker = SimBroker(BrokerProfile(), MarketData(REPLAY), shortable_codes=frozenset({"000001.SZ"}))
        broker.roll_to_date("20220104")
        fin = broker.execute("000001.SZ", "fin_buy", trade_date="20220104", raw_price=10.0, amount=1000)
        self.assertEqual(fin.status, "filled")
        when = datetime(2022, 1, 4, 10, 0, tzinfo=tz)
        tick = _Tick(minute_key="10:00", group=REPLAY.iloc[:0], activate_index=None,
                     has_market_event=False, is_auction=False, is_session=True)
        delayed = [_DelayedAction(
            seq=0, ready_at=when, action={"action": "direct_repay", "amount": 500.0},
            substep="plan", generated_at=datetime(2022, 1, 4, 9, 0, tzinfo=tz).isoformat(),
        )]
        _release_delayed_actions(delayed, broker=broker, incoming=incoming_map(), preopen_transfers=[],
                                 tick=tick, trade_date="20220104", when=when, n_session=0)
        repay = [o for o in broker.orders if o.action == "direct_repay"][-1]
        self.assertEqual(repay.status, "filled")
        self.assertFalse([e for e in broker.events if e["event_type"] == "main_actions_unfilled"])


def incoming_map():
    return {}


class ReplayIntegrationTest(unittest.TestCase):
    def test_main_runs_on_fixed_clock_and_liquidates_at_exit(self):
        # Decide once at 09:25; the order activates at 09:31, waits for the daily
        # close market event, and the position is force-liquidated on the exit day.
        def buy_hold(state):
            if state["cur_time"] != "09:25" or _held(state, "000001.SZ"):
                return []
            return [{"action": "buy", "ts_code": "000001.SZ", "amount": 1000}]

        replay = run_main_ctx_replay(
            REPLAY,
            BrokerProfile(),
            shortable_codes=frozenset(),
            main_policy=FakeMainPolicy(buy_hold),
        )
        stats = compute_return_stats(replay)
        self.assertEqual(stats["replay_granularity"], "minute")
        self.assertEqual(stats["order_status_counts"].get("filled"), 1)
        self.assertGreater(stats["total_return"], 0.0)
        self.assertTrue(any(e["event_type"] == "position_closed" for e in replay.broker.events))
        # Fix B: the stat counts fully-closed positions (one forced close here), not
        # current holdings; it was renamed from the misleading "holdings_count".
        self.assertEqual(stats["full_close_count"], 1)
        self.assertNotIn("holdings_count", stats)

    def test_minute_replay_uses_minute_bars(self):
        # Decided on the 09:31 bar, the order fills at the NEXT minute bar (14:57)
        # open under next-bar execution — proving minute bars drive the fill.
        def entry(state):
            if state["cur_time"] == "09:31" and not _held(state, "000001.SZ"):
                return [{"action": "buy", "ts_code": "000001.SZ", "amount": 1000}]
            return []

        replay = run_main_ctx_replay(
            REPLAY,
            BrokerProfile(),
            shortable_codes=frozenset(),
            replay_intraday_1min=MINUTE_REPLAY,
            main_policy=FakeMainPolicy(entry),
        )
        self.assertEqual(replay.granularity, "minute")
        fill = [event for event in replay.broker.events if event["event_type"] == "order_filled"][0]
        self.assertEqual(fill["price_label"], "minute:14:57")
        self.assertAlmostEqual(fill["price"], BrokerProfile().slipped_price(10.20, is_buy=True))

    def test_swing_t_buys_dip_and_sells_rally_next_day(self):
        # Enter once on a real minute bar; on a later-day rally the swing reduces. Orders fill
        # on the next bar, so the entry/exit land one bar after each decision.
        def swing(state):
            # state["bars"] is the columnar wire payload (numeric columns packed
            # as base64 float64); the in-container driver wraps it into the
            # dict-like ctx.bars for real strategies.
            import array as _array
            import base64 as _base64

            bars = state["bars"]
            codes = list(bars.get("ts_code") or [])
            if "000001.SZ" not in codes:
                return []
            packed = (bars.get("packed_f64") or {}).get("close")
            if packed is not None:
                values = _array.array("d")
                values.frombytes(_base64.b64decode(packed))
                close = values[codes.index("000001.SZ")]
                close = None if close != close else close
            else:
                close = (bars.get("close") or [])[codes.index("000001.SZ")]
            if close is None:
                return []
            price = float(close)
            pos = _held(state, "000001.SZ")
            if pos is None:
                if state["cur_time"] == "09:31":
                    return [{"action": "buy", "ts_code": "000001.SZ", "amount": 500}]
                return []
            if int(pos["sellable_quantity"]) >= 500 and price > float(pos["entry_price"]) * 1.01:
                return [{"action": "sell", "ts_code": "000001.SZ", "amount": 500}]
            return []

        replay = run_main_ctx_replay(
            REPLAY,
            BrokerProfile(),
            shortable_codes=frozenset(),
            replay_intraday_1min=MINUTE_REPLAY,
            main_policy=FakeMainPolicy(swing),
        )
        reduced = [e for e in replay.broker.events if e["event_type"] in {"position_reduced", "position_closed"}]
        self.assertTrue(reduced)  # the swing sold on a later-day rally

    def test_unshortable_code_is_rejected_during_replay(self):
        # 融券卖出 must quote a limit at/above the reference price; the margin_secs
        # inventory gate then rejects the fill for a non-shortable code. The limit
        # equals the activation bar's reference (19.5 close) so the order is
        # marketable there — a bare touch of the day high no longer fills.
        def go_short(state):
            if state["cur_time"] != "09:25" or _held(state, "000002.SZ"):
                return []
            return [{"action": "short", "ts_code": "000002.SZ", "amount": 1000, "limit": 19.5}]

        replay = run_main_ctx_replay(
            REPLAY,
            BrokerProfile(),
            shortable_codes=frozenset(),
            main_policy=FakeMainPolicy(go_short),
        )
        self.assertTrue(any(o.reject_reason == "margin_secs_not_shortable" for o in replay.broker.orders))

    def test_dynamic_shortability_gates_on_fill_day_set(self):
        # The short fills same-day at 20220104; the broker must consult that day's
        # per-day margin_secs set, independent of the frozen decision-day set (W7).
        # Limit 19.5 = the activation bar reference, so the order is marketable.
        def go_short(state):
            if state["cur_time"] != "09:25" or _held(state, "000002.SZ"):
                return []
            return [{"action": "short", "ts_code": "000002.SZ", "amount": 1000, "limit": 19.5}]

        # Frozen set empty, but the fill day's per-day set allows the short.
        allowed = run_main_ctx_replay(
            REPLAY, BrokerProfile(),
            shortable_codes=frozenset(),
            shortable_by_date={"20220104": frozenset({"000002.SZ"})},
            main_policy=FakeMainPolicy(go_short),
        )
        self.assertFalse(allowed.broker.reject_counts.get("margin_secs_not_shortable"))
        self.assertTrue(any(o.action == "short" and o.status == "filled" for o in allowed.broker.orders))

        # The fill day's set overrides a permissive frozen set: empty that day -> rejected.
        denied = run_main_ctx_replay(
            REPLAY, BrokerProfile(),
            shortable_codes=frozenset({"000002.SZ"}),
            shortable_by_date={"20220104": frozenset()},
            main_policy=FakeMainPolicy(go_short),
        )
        self.assertTrue(denied.broker.reject_counts.get("margin_secs_not_shortable"))

        # A fill day MISSING from present per-day data is a data gap: fail closed
        # (reject the short) instead of silently reusing a stale eligibility set.
        gap = run_main_ctx_replay(
            REPLAY, BrokerProfile(),
            shortable_codes=frozenset({"000002.SZ"}),
            shortable_by_date={"20220105": frozenset({"000002.SZ"})},
            main_policy=FakeMainPolicy(go_short),
        )
        self.assertTrue(gap.broker.reject_counts.get("margin_secs_data_missing"))

    def test_fin_buy_and_direct_repay_through_replay(self):
        # 融资买入 at the pre-open decision, 直接还款 the next day: the contract shows
        # up in the engine state (debt_contracts) and the repay clears it.
        def fin_then_repay(state):
            if state["cur_time"] != "09:25":
                return []
            if not _held(state, "000001.SZ"):
                return [{"action": "fin_buy", "ts_code": "000001.SZ", "amount": 1000}]
            if state["debt_contracts"]:
                owed = sum(
                    float(c.get("real_compact_balance") or 0.0) + float(c.get("compact_interest") or 0.0)
                    for c in state["debt_contracts"]
                    if c.get("compact_type") == "fin"
                )
                return [{"action": "direct_repay", "amount": owed}]
            return []

        replay = run_main_ctx_replay(
            REPLAY,
            BrokerProfile(),
            shortable_codes=frozenset({"000001.SZ"}),
            main_policy=FakeMainPolicy(fin_then_repay),
        )
        broker = replay.broker
        self.assertTrue(any(o.action == "fin_buy" and o.status == "filled" for o in broker.orders))
        self.assertTrue(any(o.action == "direct_repay" and o.status == "filled" for o in broker.orders))
        self.assertAlmostEqual(broker._fin_amount_outstanding(), 0.0)
        self.assertGreater(broker.interest_paid_total, 0.0)
        stats = compute_return_stats(replay)
        self.assertGreater(stats["credit_interest_paid"], 0.0)

    def test_dual_account_state_transfer_and_close_resolution(self):
        # The engine state exposes both account views; a pre-09:14 transfer request
        # is confirmed by the pre-open batch, and a bare close resolves to the
        # unique holding account (the stock account here).
        def dual(state):
            assert state["account"]["stock"]["account_type"] == "STOCK"
            assert state["account"]["credit"]["account_type"] == "CREDIT"
            if state["cur_date"] == "20220104" and state["cur_time"] == "09:00":
                return [{"action": "transfer", "amount": 100_000, "from_account": "credit", "to_account": "stock"}]
            if state["cur_time"] != "09:25":
                return []
            if state["cur_date"] == "20220104" and not _held(state, "000001.SZ"):
                return [
                    {"action": "buy", "ts_code": "000001.SZ", "amount": 1000},
                ]
            if state["cur_date"] == "20220105" and _held(state, "000001.SZ"):
                return [{"action": "close", "ts_code": "000001.SZ"}]
            return []

        replay = run_main_ctx_replay(
            REPLAY,
            BrokerProfile(),
            shortable_codes=frozenset(),
            main_policy=FakeMainPolicy(dual),
        )
        broker = replay.broker
        self.assertTrue(any(o.action == "transfer" and o.status == "filled" for o in broker.orders))
        self.assertAlmostEqual(broker.credit.cash, 400_000.0)
        # The bare close resolved to a stock-account sell on 0105 (T+1 unlocked).
        closes = [e for e in broker.events if e["event_type"] == "position_closed"]
        self.assertTrue(any(e["account"] == "stock" and e["trade_date"] == "20220105" for e in closes))
        self.assertEqual(broker.position_quantity("000001.SZ"), 0)

    def test_transfer_after_preopen_cutoff_is_ignored(self):
        def late_transfer(state):
            if state["cur_date"] == "20220104" and state["cur_time"] == "09:15":
                return [{"action": "transfer", "amount": 100_000, "from_account": "credit", "to_account": "stock"}]
            return []

        replay = run_main_ctx_replay(
            REPLAY,
            BrokerProfile(),
            shortable_codes=frozenset(),
            main_policy=FakeMainPolicy(late_transfer),
        )
        broker = replay.broker
        self.assertFalse(any(o.action == "transfer" and o.status == "filled" for o in broker.orders))
        self.assertAlmostEqual(broker.stock.cash, 500_000.0)
        self.assertTrue(
            any(e.get("reason") == "transfer_after_preopen_cutoff" for e in broker.events),
            broker.events,
        )


class CandidateIsolationTest(unittest.TestCase):
    def test_snapshot_slots_are_hidden_and_restored_during_candidate_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = SandboxPaths(Path(tmp) / "mnt")
            for slot in (paths.train, paths.valid, paths.test, paths.artifacts):
                slot.mkdir(parents=True)
                slot.chmod(0o755)

            with hide_snapshot_slots_from_agent(paths):
                self.assertEqual(stat.S_IMODE(paths.train.stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE(paths.valid.stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE(paths.test.stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE(paths.artifacts.stat().st_mode), 0o700)

            self.assertEqual(stat.S_IMODE(paths.train.stat().st_mode), 0o755)
            self.assertEqual(stat.S_IMODE(paths.valid.stat().st_mode), 0o755)
            self.assertEqual(stat.S_IMODE(paths.test.stat().st_mode), 0o755)
            self.assertEqual(stat.S_IMODE(paths.artifacts.stat().st_mode), 0o755)

    def test_main_policy_runner_restores_slots_on_enter_error(self):
        class FailingPopenExecutor:
            python = "python"

            def map_path(self, path):
                return str(path)

            def runtime_path(self, path):
                return str(path)

            def popen(self, *_args, **_kwargs):
                raise RuntimeError("popen failed")

        with tempfile.TemporaryDirectory() as tmp:
            paths = SandboxPaths(Path(tmp) / "mnt")
            paths.workspace.mkdir(parents=True)
            paths.agent_output.mkdir(parents=True)
            paths.artifacts.mkdir(parents=True)
            paths.artifacts.chmod(0o755)

            runner = MainPolicyRunner(
                FailingPopenExecutor(),
                paths,
                timeout_seconds=1.0,
                decision_time=DECISION,
                replay_granularity="minute",
            )
            with self.assertRaisesRegex(RuntimeError, "popen failed"):
                runner.__enter__()

            # The hidden snapshot/artifact slots are restored even when startup fails.
            self.assertEqual(stat.S_IMODE(paths.artifacts.stat().st_mode), 0o755)


class BrokerCoreTest(unittest.TestCase):
    """The shared deterministic fill core (R16) plus the credit math the SimBroker
    delegates to (contracts, interest, FIFO repayment, the 细则 formulas)."""

    def setUp(self) -> None:
        from autotrade.environment import broker_core as core

        self.core = core
        self.cost = BrokerProfile().cost_model  # CostModel matching the default profile

    def test_profile_delegates_to_cost_model(self):
        profile = BrokerProfile()
        self.assertAlmostEqual(profile.slipped_price(10.0, is_buy=True), self.cost.slipped_price(10.0, is_buy=True))
        self.assertAlmostEqual(profile.commission(100_000.0), self.cost.commission(100_000.0))
        self.assertAlmostEqual(
            profile.stamp_duty_on_sale(100_000.0, "20230827"), self.cost.stamp_duty_on_sale(100_000.0, "20230827")
        )

    def test_reduce_amount_reject_allows_the_one_shot_odd_tail(self):
        reject = self.core.reduce_amount_reject
        self.assertIsNone(reject(1300, 1350, "000001.SZ"))  # whole lots
        self.assertIsNone(reject(1350, 1350, "000001.SZ"))  # full position
        self.assertIsNone(reject(150, 1350, "000001.SZ"))   # one lot + the entire odd tail
        self.assertIsNone(reject(50, 1350, "000001.SZ"))    # the odd tail alone
        self.assertEqual(reject(120, 1350, "000001.SZ"), "amount_not_lot_aligned")  # partial odd piece
        self.assertEqual(reject(30, 1350, "000001.SZ"), "amount_below_lot_size")
        self.assertEqual(reject(150, 1000, "000001.SZ"), "amount_not_lot_aligned")  # no odd tail held
        self.assertIsNone(reject(150, 150, "688001.SH"))    # STAR below minimum: full exit only
        self.assertEqual(reject(150, 350, "688001.SH"), "amount_below_lot_size")
        self.assertIsNone(reject(50, 50, "830799.BJ"))

    def test_project_open_long_is_notional_plus_fee(self):
        fill = self.core.project_open(self.cost, side="long", raw_price=10.0, shares=1000, trade_date="20220104")
        price = self.cost.slipped_price(10.0, is_buy=True)
        notional = 1000 * price
        fee = self.cost.trade_fee(notional)
        self.assertAlmostEqual(fill.price, price)
        self.assertAlmostEqual(fill.required_cash, notional + fee)
        self.assertAlmostEqual(fill.cash_delta, -(notional + fee))
        self.assertAlmostEqual(fill.cost_basis, notional + fee)

    def test_project_open_financed_long_moves_no_cash(self):
        fill = self.core.project_open(
            self.cost, side="long", raw_price=10.0, shares=1000, trade_date="20220104", financed=True
        )
        notional = 1000 * self.cost.slipped_price(10.0, is_buy=True)
        self.assertEqual(fill.cash_delta, 0.0)
        self.assertEqual(fill.required_cash, 0.0)
        self.assertAlmostEqual(fill.cost_basis, notional + self.cost.trade_fee(notional))

    def test_project_open_short_locks_margin_and_banks_net_proceeds(self):
        fill = self.core.project_open(self.cost, side="short", raw_price=10.0, shares=1000, trade_date="20220104")
        price = self.cost.slipped_price(10.0, is_buy=False)
        notional = 1000 * price
        fee = self.cost.trade_fee(notional)
        duty = self.cost.stamp_duty_on_sale(notional, "20220104")
        self.assertAlmostEqual(fill.margin, notional * self.cost.slo_margin_ratio)
        self.assertAlmostEqual(fill.required_cash, fill.margin + fee + duty)
        self.assertAlmostEqual(fill.cash_delta, notional - fee - duty)  # net proceeds banked
        self.assertAlmostEqual(fill.cost_basis, notional - fee - duty)

    def test_project_reduce_sell_banks_net_cover_pays(self):
        sell = self.core.project_reduce(self.cost, side="long", raw_price=11.0, shares=500, trade_date="20220105")
        self.assertGreater(sell.cash_delta, 0)  # selling a long banks cash
        cover = self.core.project_reduce(self.cost, side="short", raw_price=9.0, shares=500, trade_date="20220105")
        self.assertLess(cover.cash_delta, 0)  # covering a short pays cash

    def _contract(self, kind, **kw):
        defaults = dict(
            compact_id=kw.pop("compact_id", "D1"), kind=kind, ts_code="000001.SZ",
            open_date="20220104", open_price=10.0, year_rate=0.0835,
        )
        defaults.update(kw)
        return self.core.DebtContract(**defaults)

    def test_accrue_debt_interest_calendar_gap(self):
        fin = self._contract("fin", principal=10000.0)
        slo = self._contract("slo", compact_id="D2", shares=500, sell_amount=5000.0, year_rate=0.085)
        first = self.core.accrue_debt_interest([fin, slo], "20220107")  # first mark: 1 day
        self.assertAlmostEqual(fin.interest_accrued, 10000.0 * 0.0835 / 360.0)
        self.assertAlmostEqual(slo.interest_accrued, 500 * 10.0 * 0.085 / 360.0)
        again = self.core.accrue_debt_interest([fin, slo], "20220107")  # same day: idempotent
        self.assertEqual(again, 0.0)
        weekend = self.core.accrue_debt_interest([fin, slo], "20220110")  # Fri -> Mon: 3 days
        self.assertAlmostEqual(weekend, first * 3)

    def test_repay_fin_is_interest_first_fifo(self):
        older = self._contract("fin", compact_id="D1", open_date="20220103", principal=1000.0, interest_accrued=10.0)
        newer = self._contract("fin", compact_id="D2", open_date="20220105", principal=500.0, interest_accrued=5.0)
        result = self.core.repay_fin([newer, older], 1015.0, release_shares=True)
        # FIFO by open_date; within each contract interest before principal: the
        # 1015 pays older(10 interest + 1000 principal) then newer's 5 interest.
        self.assertAlmostEqual(result["interest_paid"], 15.0)
        self.assertAlmostEqual(result["principal_paid"], 1000.0)
        self.assertAlmostEqual(older.principal, 0.0)  # oldest fully repaid
        self.assertAlmostEqual(newer.interest_accrued, 0.0)
        self.assertAlmostEqual(newer.principal, 500.0)  # untouched principal

    def test_repay_slo_releases_proportionally(self):
        contract = self._contract("slo", shares=1000, sell_amount=10000.0, interest_accrued=8.0, year_rate=0.085)
        result = self.core.repay_slo([contract], "000001.SZ", 400)
        self.assertEqual(result["shares_repaid"], 400.0)
        self.assertAlmostEqual(result["interest_due"], 8.0 * 0.4)
        self.assertEqual(contract.shares, 600)
        self.assertAlmostEqual(contract.sell_amount, 6000.0)
        self.assertAlmostEqual(contract.interest_accrued, 4.8)

    def test_enable_bail_balance_follows_the_exchange_formula(self):
        # 100k cash, 50k collateral at 0.7; one fin contract 10k debt with shares
        # worth 12k (浮盈 2k at 0.7); one slo contract sold for 8k now worth 9k
        # (浮亏 1k at 100%); margins at 1.0; 100 interest.
        bail = self.core.enable_bail_balance(
            100_000.0, 50_000.0, [(12_000.0, 10_000.0)], [(8_000.0, 9_000.0)], 100.0,
            assure_ratio=0.7, fin_margin_ratio=1.0, slo_margin_ratio=1.0,
        )
        expected = (
            100_000.0 + 50_000.0 * 0.7
            + 2_000.0 * 0.7 - 10_000.0 * 1.0
            + (-1_000.0) * 1.0 - 8_000.0 - 9_000.0 * 1.0
            - 100.0
        )
        self.assertAlmostEqual(bail, expected)

    def test_credit_maintenance_ratio_none_without_debt(self):
        self.assertIsNone(self.core.credit_maintenance_ratio(100_000.0, 0.0, 0.0, 0.0, 0.0))
        ratio = self.core.credit_maintenance_ratio(100_000.0, 50_000.0, 40_000.0, 20_000.0, 100.0)
        self.assertAlmostEqual(ratio, 150_000.0 / 60_100.0)


if __name__ == "__main__":
    unittest.main()
