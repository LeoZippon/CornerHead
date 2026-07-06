import stat
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from autotrade.environment.backtest_engine import (
    BacktestError,
    compute_return_stats,
    hide_snapshot_slots_from_agent,
)
from autotrade.environment.broker import BrokerProfile, MarketData, SimBroker, optype, prtype
from autotrade.environment.main_ctx_engine import MainPolicyRunner, run_main_ctx_replay
from autotrade.environment.runtime import SandboxPaths
from autotrade.environment.tools.backtest import _profile_kwargs


def _held(state, code):
    return next((p for p in state["positions"] if str(p["ts_code"]) == code), None)


def _positions(broker):
    return broker.get_trade_detail_data(data_type="POSITION")


def _asset(broker):
    return broker.get_trade_detail_data(data_type="ACCOUNT")[0]


def _deals(broker, code):
    return [t for t in broker.get_trade_detail_data(data_type="DEAL") if t["ts_code"] == code]


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
    def make_broker(self, *, shortable=("000002.SZ",), mode="proxy_margin_secs", daily=REPLAY, **profile_kw):
        profile = BrokerProfile(short_inventory_mode=mode, **profile_kw)
        return SimBroker(profile, MarketData(daily), shortable_codes=frozenset(shortable))

    def test_long_buy_hold_and_close(self):
        broker = self.make_broker()
        order = broker.execute("000001.SZ", "buy", trade_date="20220104", raw_price=10.0, amount=1000)
        self.assertEqual(order.status, "filled")
        self.assertEqual(broker.position_quantity("000001.SZ"), 1000)
        broker.mark_to_market("20220105")
        broker.mark_to_market("20220106")
        broker.close_all("20220106")
        self.assertEqual(broker.positions, {})
        self.assertGreater(broker.equity(), broker.initial_equity)

    def test_t1_blocks_same_day_close(self):
        broker = self.make_broker()
        broker.execute("000001.SZ", "buy", trade_date="20220104", raw_price=10.0, amount=1000)
        broker.close_all("20220104")
        self.assertIn("000001.SZ", broker.positions)
        self.assertTrue(any(e["event_type"] == "exit_blocked_t_plus_one" for e in broker.events))

    def test_date_roll_unlocks_t_plus_one_before_any_fill(self):
        # R4: rolling the sim-date to D+1 unlocks an overnight hold's T+1 shares
        # before the day's first fill, so sellable_quantity is correct from the first
        # off-session tick (the host calls broker.roll_to_date at each new trade date).
        broker = self.make_broker()
        broker.execute("000001.SZ", "buy", trade_date="20220104", raw_price=10.0, amount=1000)
        held = next(p for p in _positions(broker) if p["ts_code"] == "000001.SZ")
        self.assertEqual(held["sellable_quantity"], 0)  # T+1 locked on D
        broker.roll_to_date("20220105")  # the host rolls the date before any D+1 tick
        held = next(p for p in _positions(broker) if p["ts_code"] == "000001.SZ")
        self.assertEqual(held["sellable_quantity"], 1000)  # unlocked without a fill
        # roll_to_date is idempotent: re-rolling the same date does not relock or error.
        broker.roll_to_date("20220105")
        held = next(p for p in _positions(broker) if p["ts_code"] == "000001.SZ")
        self.assertEqual(held["sellable_quantity"], 1000)

    def test_short_can_cover_same_day(self):
        # R5: a 融券 short has no T+1 sell lock, so same-day cover (买券还券) fills.
        broker = self.make_broker()  # 000002.SZ shortable
        opened = broker.execute("000002.SZ", "short", trade_date="20220104", raw_price=20.0, amount=500)
        self.assertEqual(opened.status, "filled")
        covered = broker.execute("000002.SZ", "cover", trade_date="20220104", raw_price=19.5, amount=500)
        self.assertEqual(covered.status, "filled")
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

    def test_short_leaves_locked_today_untouched_while_long_t_plus_one_holds(self):
        # Fix B: T+1 lock bookkeeping is long-only. A short never populates
        # locked_today/locked_date — its sellable_quantity ignores them and 融券
        # permits same-day cover — while the long T+1 lock is still recorded and lifts
        # on the date roll exactly as before.
        broker = self.make_broker()  # 000001.SZ (long), 000002.SZ shortable
        broker.execute("000001.SZ", "buy", trade_date="20220104", raw_price=10.0, amount=1000)
        long_pos = broker.positions["000001.SZ"]
        self.assertEqual(long_pos.locked_today, 1000)  # long records the T+1 lock
        self.assertEqual(long_pos.locked_date, "20220104")
        self.assertEqual(long_pos.sellable_quantity, 0)  # locked on entry day

        broker.execute("000002.SZ", "short", trade_date="20220104", raw_price=20.0, amount=500)
        short_pos = broker.positions["000002.SZ"]
        self.assertEqual(short_pos.locked_today, 0)  # short leaves the lock state untouched
        self.assertEqual(short_pos.locked_date, "")
        self.assertEqual(short_pos.sellable_quantity, 500)  # fully coverable same day
        self.assertEqual(long_pos.sellable_quantity, 0)  # long still locked on D

        # Adding to the short after the date roll still never touches its lock state,
        # while the long's T+1 lock lifts on the new day exactly as before.
        broker.execute("000002.SZ", "short", trade_date="20220105", raw_price=19.4, amount=500)
        short_pos = broker.positions["000002.SZ"]
        self.assertEqual(short_pos.locked_today, 0)
        self.assertEqual(short_pos.locked_date, "")
        self.assertEqual(short_pos.sellable_quantity, 1000)
        self.assertEqual(long_pos.locked_today, 0)  # long unlocked on the D+1 roll
        self.assertEqual(long_pos.sellable_quantity, 1000)

        # A partial cover keeps the short lock state at its defaults.
        broker.execute("000002.SZ", "cover", trade_date="20220105", raw_price=19.0, amount=500)
        short_pos = broker.positions["000002.SZ"]
        self.assertEqual(short_pos.locked_today, 0)
        self.assertEqual(short_pos.locked_date, "")

    def test_partial_sell_clamps_to_sellable_balance(self):
        broker = self.make_broker()
        broker.execute("000001.SZ", "buy", trade_date="20220104", raw_price=10.0, amount=1000)
        broker.execute("000001.SZ", "buy", trade_date="20220105", raw_price=10.6, amount=500)
        self.assertEqual(broker.position_quantity("000001.SZ"), 1500)
        order = broker.execute("000001.SZ", "sell", trade_date="20220105", raw_price=11.0, amount=1500)
        # Only the 1000 shares bought on 0104 are sellable on 0105 (T+1).
        self.assertEqual(order.filled_quantity, 1000)
        self.assertEqual(broker.position_quantity("000001.SZ"), 500)

    def test_short_requires_margin_secs_membership(self):
        broker = self.make_broker(shortable=())
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
        # R8b: a short banks its proceeds into cash but locks them as collateral;
        # available_cash never inflates with proceeds. Margin is not frozen cash —
        # it constrains new credit ops through 保证金可用余额.
        broker = self.make_broker()  # 000002.SZ shortable, 1,000,000 initial cash
        bail_before = broker.enable_bail_balance()
        broker.execute("000002.SZ", "short", trade_date="20220104", raw_price=20.0, amount=500)
        asset_after = _asset(broker)
        pos = next(p for p in _positions(broker) if p["ts_code"] == "000002.SZ")
        self.assertGreater(broker.cash, 1_000_000.0)  # proceeds banked into literal cash
        # Deployable cash excludes exactly the locked short proceeds (entry_cost).
        self.assertAlmostEqual(asset_after["available_cash"], broker.cash - pos["entry_cost"])
        # The bail balance drops by the posted margin plus open costs (fee + duty).
        contract = broker.get_debt_contract()[0]
        margin = contract["sell_amount"] * broker.profile.effective_slo_margin_ratio
        open_costs = contract["sell_amount"] - pos["entry_cost"]
        self.assertAlmostEqual(bail_before - broker.enable_bail_balance(), margin + open_costs)

    def test_fin_buy_creates_contract_without_cash_outflow(self):
        broker = self.make_broker(shortable=("000001.SZ",))  # fin gate shares margin_secs
        order = broker.execute("000001.SZ", "fin_buy", trade_date="20220104", raw_price=10.0, amount=1000)
        self.assertEqual(order.status, "filled")
        self.assertEqual(order.op_type, optype.FIN_BUY)
        self.assertEqual(broker.cash, 1_000_000.0)  # financed: no cash moves at open
        self.assertEqual(broker.position_quantity("000001.SZ"), 1000)
        contract = broker.get_debt_contract()[0]
        price = broker.profile.slipped_price(10.0, is_buy=True)
        principal = 1000 * price + broker.profile.commission(1000 * price)
        self.assertEqual(contract["compact_type"], "fin")
        self.assertAlmostEqual(contract["real_compact_balance"], principal)
        self.assertEqual(contract["real_compact_vol"], 1000)
        credit = _asset(broker)["credit"]
        self.assertAlmostEqual(credit["fin_debt"], principal)
        # equity nets the debt: only the financed fee is lost at open.
        self.assertAlmostEqual(broker.equity(), 1_000_000.0 + 1000 * price - principal)

    def test_fin_buy_requires_margin_secs_membership(self):
        broker = self.make_broker(shortable=())
        order = broker.execute("000001.SZ", "fin_buy", trade_date="20220104", raw_price=10.0, amount=1000)
        self.assertEqual(order.reject_reason, "margin_secs_not_finable")

    def test_fin_interest_accrues_and_direct_repay_clamps(self):
        broker = self.make_broker(shortable=("000001.SZ",))
        broker.execute("000001.SZ", "fin_buy", trade_date="20220104", raw_price=10.0, amount=1000)
        broker.mark_to_market("20220104")
        principal = broker._fin_amount_outstanding()
        interest = broker._interest_outstanding()
        self.assertAlmostEqual(interest, principal * broker.profile.fin_rate_annual / 365.0)
        # Partial repayment pays interest first, then principal, from cash.
        cash_before = broker.cash
        broker.passorder(optype.DIRECT_REPAY, 1102, "", "", prtype.PEER, 0, 5000)
        self.assertAlmostEqual(cash_before - broker.cash, 5000.0)
        self.assertAlmostEqual(broker.interest_paid_total, interest)
        self.assertAlmostEqual(broker._fin_amount_outstanding(), principal - (5000.0 - interest))
        # Overshooting clamps to the outstanding debt; the contract closes.
        broker.passorder(optype.DIRECT_REPAY, 1102, "", "", prtype.PEER, 0, 10_000_000)
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
        cash_before = broker.cash
        order = broker.execute("000001.SZ", "sell_repay", trade_date="20220105", raw_price=11.0, amount=1000)
        self.assertEqual(order.status, "filled")
        price = broker.profile.slipped_price(11.0, is_buy=False)
        notional = 1000 * price
        net = notional - broker.profile.commission(notional) - broker.profile.stamp_duty_on_sale(notional, "20220105")
        # The whole debt is repaid; the surplus stays as cash.
        self.assertAlmostEqual(broker.cash, cash_before + net - owed)
        self.assertAlmostEqual(broker._fin_amount_outstanding(), 0.0)
        self.assertEqual(broker.position_quantity("000001.SZ"), 0)
        self.assertEqual(broker.get_debt_contract(), [])

    def test_sell_repay_without_debt_is_rejected(self):
        broker = self.make_broker()
        broker.execute("000001.SZ", "buy", trade_date="20220104", raw_price=10.0, amount=1000)
        order = broker.execute("000001.SZ", "sell_repay", trade_date="20220105", raw_price=11.0, amount=1000)
        self.assertEqual(order.reject_reason, "no_fin_debt")

    def test_bail_balance_gates_fin_buy_and_short(self):
        broker = self.make_broker(shortable=("000001.SZ", "000002.SZ"))
        # Financing far beyond the bail balance (~1,000,000) is rejected.
        too_big = broker.execute("000001.SZ", "fin_buy", trade_date="20220104", raw_price=10.0, amount=120_000)
        self.assertEqual(too_big.reject_reason, "insufficient_bail_balance")
        # A fin_buy within the balance passes, and its margin occupation reduces
        # what a subsequent short may post.
        ok = broker.execute("000001.SZ", "fin_buy", trade_date="20220104", raw_price=10.0, amount=90_000)
        self.assertEqual(ok.status, "filled")
        short = broker.execute("000002.SZ", "short", trade_date="20220104", raw_price=20.0, amount=40_000)
        self.assertEqual(short.reject_reason, "insufficient_bail_balance")

    def test_credit_quota_gates(self):
        broker = self.make_broker(shortable=("000001.SZ", "000002.SZ"), fin_max_quota=5000.0, slo_max_quota=5000.0)
        fin = broker.execute("000001.SZ", "fin_buy", trade_date="20220104", raw_price=10.0, amount=1000)
        self.assertEqual(fin.reject_reason, "fin_quota_exceeded")
        short = broker.execute("000002.SZ", "short", trade_date="20220104", raw_price=20.0, amount=500)
        self.assertEqual(short.reject_reason, "slo_quota_exceeded")

    def test_stock_account_rejects_credit_ops(self):
        broker = self.make_broker(shortable=("000002.SZ",), account_type="stock")
        rejected = broker.execute("000002.SZ", "short", trade_date="20220104", raw_price=20.0, amount=500)
        self.assertEqual(rejected.reject_reason, "account_type_forbids_action")
        with self.assertRaisesRegex(ValueError, "stock account"):
            broker.passorder(optype.FIN_BUY, 1101, "", "000001.SZ", prtype.PEER, 0, 1000)
        with self.assertRaisesRegex(ValueError, "credit account"):
            broker.get_debt_contract()
        self.assertIsNone(broker.maintenance_ratio())
        self.assertNotIn("credit", _asset(broker))
        # The plain cash ops still work and available_cash == cash.
        buy = broker.execute("000001.SZ", "buy", trade_date="20220104", raw_price=10.0, amount=1000)
        self.assertEqual(buy.status, "filled")
        self.assertEqual(buy.op_type, optype.STOCK_BUY)
        self.assertAlmostEqual(_asset(broker)["available_cash"], broker.cash)

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
        self.assertEqual(broker.position_quantity("000002.SZ"), -500)
        contract = broker.get_debt_contract()[0]
        self.assertEqual(contract["compact_type"], "slo")
        self.assertAlmostEqual(contract["sell_amount"], 500 * 20.0)

    def test_maintenance_includes_fin_debt_and_forces_close(self):
        daily = make_daily(
            [
                ("20220104", "000001.SZ", 10.0, 10.0, 12.0, 0.5, False),
                ("20220105", "000001.SZ", 2.0, 1.9, 12.0, 0.5, False),  # crash day
            ]
        )
        broker = self.make_broker(shortable=("000001.SZ",), daily=daily)
        broker.execute("000001.SZ", "fin_buy", trade_date="20220104", raw_price=10.0, amount=90_000)
        broker.mark_to_market("20220104")
        self.assertGreater(broker.maintenance_ratio(), broker.profile.maintenance_closeout_ratio)
        broker.roll_to_date("20220105")
        broker.mark_to_market("20220105")  # ratio ~(1e6+171k)/(900k+i) < 1.30
        self.assertTrue(any(e["event_type"] == "forced_close_triggered" for e in broker.events))
        self.assertEqual(broker.positions, {})  # the financed long was liquidated
        # The fin debt is NOT auto-settled by liquidation: it stays outstanding
        # (accruing interest) until repaid, and equity nets it out.
        self.assertGreater(broker._fin_amount_outstanding(), 0.0)
        self.assertAlmostEqual(
            broker.equity(), broker.cash - broker._fin_amount_outstanding() - broker._interest_outstanding()
        )

    def test_combined_long_short_accounting_and_forced_close(self):
        # RA1: with a simultaneous long + short, equity()/maintenance_ratio() follow
        # the 细则 formulas (literal cash in the numerator, debt + interest in the
        # denominator); a short loss that breaches the 1.30 line forces a close.
        daily = make_daily(
            [
                ("20220104", "000001.SZ", 10.0, 10.0, 12.0, 8.0, False),
                ("20220105", "000001.SZ", 10.2, 10.2, 12.0, 8.0, False),
                ("20220104", "000002.SZ", 100.0, 100.0, 2500.0, 1.0, False),
                ("20220105", "000002.SZ", 250.0, 250.0, 2500.0, 1.0, False),  # short loss day
            ]
        )
        broker = self.make_broker(daily=daily)  # 000002.SZ shortable, 1,000,000 initial
        broker.execute("000001.SZ", "buy", trade_date="20220104", raw_price=10.0, amount=1000)
        broker.execute("000002.SZ", "short", trade_date="20220104", raw_price=100.0, amount=5000)
        asset = _asset(broker)
        positions = {p["ts_code"]: p for p in _positions(broker)}
        long_mv = positions["000001.SZ"]["market_value"]
        short_mv = positions["000002.SZ"]["market_value"]
        proceeds_locked = positions["000002.SZ"]["entry_cost"]
        # equity and maintenance ratio use literal cash (the banked short proceeds
        # count as collateral); available_cash subtracts only the locked proceeds.
        self.assertAlmostEqual(broker.equity(), broker.cash + long_mv - short_mv)
        self.assertAlmostEqual(broker.maintenance_ratio(), (broker.cash + long_mv) / short_mv)
        self.assertAlmostEqual(asset["available_cash"], broker.cash - proceeds_locked)
        self.assertLess(asset["available_cash"], broker.cash)  # locked collateral not deployable
        # Day 2: the short jumps 100 -> 250, breaching the maintenance line -> forced close.
        broker.mark_to_market("20220105")
        self.assertTrue(any(e["event_type"] == "forced_close_triggered" for e in broker.events))
        self.assertEqual(broker.positions, {})  # both legs liquidated at the close
        self.assertGreater(broker.interest_paid_total, 0.0)  # slo interest paid at the cover

    def test_broker_inventory_mode_rejects_without_files(self):
        broker = self.make_broker(mode="broker_inventory")
        order = broker.execute("000002.SZ", "short", trade_date="20220104", raw_price=20.0, amount=500)
        self.assertEqual(order.reject_reason, "broker_inventory_unavailable")

    def test_passorder_lifecycle_matches_qmt_verbs(self):
        broker = self.make_broker(shortable=())
        group = pd.DataFrame([{"ts_code": "000001.SZ", "open": 10.0, "high": 10.1, "low": 9.8, "close": 9.9}])
        # 指定价 limit below the bar -> rests, then auto-cancels (valid_bars=1).
        miss = broker.passorder(optype.CREDIT_BUY, 1101, "", "000001.SZ", prtype.FIX, 9.5, 1000)
        working = broker.working_orders()
        self.assertEqual([o["order_id"] for o in working], [miss])
        self.assertEqual(working[0]["op_type"], optype.CREDIT_BUY)
        broker.match_bar("20220104", "09:31", group)
        self.assertEqual(broker.working_orders(), [])  # expired_unfilled
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
        # A reachable limit fills at exactly the limit (maker, no slippage).
        broker.passorder(optype.CREDIT_BUY, 1101, "", "000001.SZ", prtype.FIX, 9.85, 1000)
        broker.match_bar("20220104", "09:32", group)
        self.assertEqual(broker.position_quantity("000001.SZ"), 1000)
        self.assertEqual(_deals(broker, "000001.SZ")[-1]["price"], 9.85)
        # If the activation bar opens through a buy limit, the better open is used.
        better_open = self.make_broker(shortable=())
        better_open.passorder(optype.CREDIT_BUY, 1101, "", "000001.SZ", prtype.FIX, 10.5, 1000)
        better_open.match_bar("20220104", "09:31", group)
        self.assertEqual(_deals(better_open, "000001.SZ")[-1]["price"], 10.0)
        # Sell limits use the same better-open rule on the other side.
        better_sell = self.make_broker(shortable=())
        better_sell.execute("000001.SZ", "buy", trade_date="20220104", raw_price=10.0, amount=1000)
        better_sell.passorder(optype.CREDIT_SELL, 1101, "", "000001.SZ", prtype.FIX, 9.5, 1000)
        better_sell.match_bar("20220105", "09:31", group)
        self.assertEqual(_deals(better_sell, "000001.SZ")[-1]["price"], 10.0)
        self.assertEqual(better_sell.position_quantity("000001.SZ"), 0)

    def test_passorder_validates_op_order_and_price_types(self):
        broker = self.make_broker()
        with self.assertRaisesRegex(ValueError, "opType=23"):
            broker.passorder(optype.STOCK_BUY, 1101, "", "000001.SZ", prtype.PEER, 0, 100)  # credit uses 33
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
        with self.assertRaisesRegex(ValueError, "does not match"):
            broker.get_trade_detail_data(account_type="stock")
        with self.assertRaisesRegex(ValueError, "data_type"):
            broker.get_trade_detail_data(data_type="TASK")

    def test_max_total_holdings_rejects_new_code(self):
        broker = self.make_broker(shortable=(), max_total_holdings=1)
        broker.execute("000001.SZ", "buy", trade_date="20220104", raw_price=10.0, amount=100)
        order = broker.execute("000002.SZ", "buy", trade_date="20220104", raw_price=20.0, amount=100)
        self.assertEqual(order.reject_reason, "max_holdings_reached")

    def test_single_name_weight_cap_clamps_shares(self):
        broker = self.make_broker(max_single_name_weight=0.2)  # 20% of 1,000,000 = 200,000
        order = broker.execute("000001.SZ", "buy", trade_date="20220104", raw_price=10.0, weight=0.5)
        self.assertEqual(order.status, "filled")
        self.assertEqual(order.filled_quantity, 20000)  # 200,000 / 10 = 20,000 shares

    def test_default_profile_does_not_force_holdings_or_single_name_caps(self):
        broker = self.make_broker(shortable=())
        first = broker.execute("000001.SZ", "buy", trade_date="20220104", raw_price=10.0, weight=0.5)
        second = broker.execute("000002.SZ", "buy", trade_date="20220104", raw_price=20.0, amount=100)
        self.assertEqual(first.status, "filled")
        self.assertGreater(first.filled_quantity, 20000)
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

    def test_trades_for_records_open_and_reduce_history(self):
        broker = self.make_broker()
        broker.execute("000001.SZ", "buy", trade_date="20220104", raw_price=10.0, amount=1000)
        broker.mark_to_market("20220105")
        broker.execute("000001.SZ", "sell", trade_date="20220105", raw_price=11.0, amount=500)
        trades = _deals(broker, "000001.SZ")
        self.assertEqual([t["kind"] for t in trades], ["open", "reduce"])
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
            commission_bps=2.5,
            min_commission_cny=1.25,
            stamp_duty_sell_bps_before_cutover=12.0,
            stamp_duty_sell_bps_from_cutover=6.0,
            slippage_bps=7.0,
            slo_rate_annual=0.03,
            fin_rate_annual=0.06,
            assure_ratio=0.65,
            maintenance_source="broker-doc",
        )
        restored = BrokerProfile(**_profile_kwargs(profile.to_record()))
        self.assertEqual(restored.account_type, "credit")
        self.assertEqual(restored.min_commission_cny, 1.25)
        self.assertEqual(restored.stamp_duty_sell_bps_before_cutover, 12.0)
        self.assertEqual(restored.slippage_bps, 7.0)
        self.assertEqual(restored.slo_rate_annual, 0.03)
        self.assertEqual(restored.fin_rate_annual, 0.06)
        self.assertEqual(restored.assure_ratio, 0.65)
        self.assertEqual(restored.maintenance_source, "broker-doc")
        stock_restored = BrokerProfile(**_profile_kwargs(BrokerProfile(account_type="stock").to_record()))
        self.assertEqual(stock_restored.account_type, "stock")
        private_restored = BrokerProfile(**_profile_kwargs(BrokerProfile(is_private_fund=True).to_record()))
        self.assertEqual(private_restored.effective_slo_margin_ratio, 1.2)


class ReplayIntegrationTest(unittest.TestCase):
    def test_main_runs_each_bar_and_liquidates_at_exit(self):
        # Decide once at the 09:25 pre-open tick; next-bar execution fills it, and
        # the position is force-liquidated on the final replay day.
        def buy_hold(state):
            if state["cur_time"] != "09:25" or _held(state, "000001.SZ"):
                return []
            return [{"action": "buy", "ts_code": "000001.SZ", "weight": 0.1}]

        replay = run_main_ctx_replay(
            REPLAY,
            BrokerProfile(),
            shortable_codes=frozenset(),
            main_policy=FakeMainPolicy(buy_hold),
        )
        stats = compute_return_stats(replay)
        self.assertEqual(stats["replay_granularity"], "daily")
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
                return [{"action": "buy", "ts_code": "000001.SZ", "weight": 0.1}]
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
        # Enter once pre-open; on a later-day rally the swing reduces. Orders fill
        # on the next bar, so the entry/exit land one bar after each decision.
        def swing(state):
            bar = next((b for b in state["bars"] if str(b["ts_code"]) == "000001.SZ"), None)
            if bar is None or bar.get("close") is None:
                return []
            price = float(bar["close"])
            pos = _held(state, "000001.SZ")
            if pos is None:
                if state["cur_time"] == "09:25":
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
        # inventory gate then rejects the fill for a non-shortable code.
        def go_short(state):
            if state["cur_time"] != "09:25" or _held(state, "000002.SZ"):
                return []
            return [{"action": "short", "ts_code": "000002.SZ", "weight": 0.1, "limit": 20.0}]

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
        def go_short(state):
            if state["cur_time"] != "09:25" or _held(state, "000002.SZ"):
                return []
            return [{"action": "short", "ts_code": "000002.SZ", "weight": 0.1, "limit": 20.0}]

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

    def test_fin_buy_and_direct_repay_through_replay(self):
        # 融资买入 at the pre-open decision, 直接还款 the next day: the contract shows
        # up in the engine state (debt_contracts) and the repay clears it.
        def fin_then_repay(state):
            if state["cur_time"] != "09:25":
                return []
            if not _held(state, "000001.SZ"):
                return [{"action": "fin_buy", "ts_code": "000001.SZ", "amount": 1000}]
            if state["debt_contracts"]:
                return [{"action": "direct_repay", "amount": 1_000_000}]
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

    def test_stock_account_replay_has_no_credit_state(self):
        def buy_hold(state):
            self_state = state["account"]
            assert self_state["account_type"] == "STOCK"
            assert "credit" not in self_state
            assert state["debt_contracts"] == []
            if state["cur_time"] != "09:25" or _held(state, "000001.SZ"):
                return []
            return [{"action": "buy", "ts_code": "000001.SZ", "weight": 0.1}]

        replay = run_main_ctx_replay(
            REPLAY,
            BrokerProfile(account_type="stock"),
            shortable_codes=frozenset(),
            main_policy=FakeMainPolicy(buy_hold),
        )
        stats = compute_return_stats(replay)
        self.assertEqual(stats["order_status_counts"].get("filled"), 1)
        self.assertEqual(stats["credit_interest_accrued"], 0.0)


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

    def test_project_open_long_is_notional_plus_fee(self):
        fill = self.core.project_open(self.cost, side="long", raw_price=10.0, shares=1000, trade_date="20220104")
        price = self.cost.slipped_price(10.0, is_buy=True)
        notional = 1000 * price
        fee = self.cost.commission(notional)
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
        self.assertAlmostEqual(fill.cost_basis, notional + self.cost.commission(notional))

    def test_project_open_short_locks_margin_and_banks_net_proceeds(self):
        fill = self.core.project_open(self.cost, side="short", raw_price=10.0, shares=1000, trade_date="20220104")
        price = self.cost.slipped_price(10.0, is_buy=False)
        notional = 1000 * price
        fee = self.cost.commission(notional)
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

    def test_lot_floor_and_resolve_shares(self):
        self.assertEqual(self.core.lot_floor(1099), 1000)
        self.assertEqual(self.core.lot_floor("abc"), 0)
        self.assertEqual(self.core.resolve_shares(350, None, 10.0, 1_000_000.0), 300)
        # weight 0.01 of 1,000,000 at price 10 -> 1000 shares.
        self.assertEqual(self.core.resolve_shares(None, 0.01, 10.0, 1_000_000.0), 1000)

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
        self.assertAlmostEqual(fin.interest_accrued, 10000.0 * 0.0835 / 365.0)
        self.assertAlmostEqual(slo.interest_accrued, 500 * 10.0 * 0.085 / 365.0)
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
