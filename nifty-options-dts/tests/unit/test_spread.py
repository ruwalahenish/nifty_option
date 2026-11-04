"""Debit spread tests. Every expected number is worked out by hand in the comments, not by the formulas under test."""
import datetime
import unittest
from decimal import Decimal as Dec

from nifty_core import contracts
from nifty_core.contracts import ContractSpecs
from nifty_core.costs.engine import load_cost_engine
from nifty_core.data.models import OptionContract
from nifty_core.errors import CostRateUnverified, SpreadError
from nifty_core.instruments import coverage
from nifty_core.instruments.leg import BUY, SELL, OptionLeg
from nifty_core.instruments.spread import KIND_BEAR_PUT, KIND_BULL_CALL, DebitSpread
from nifty_core.market.ist import IST
from nifty_core.versioned import load_json
from tests import _scenarios as sc
from tests._base import NoNetworkTestCase

D = datetime.date
EXP = D(2026, 9, 22)          # a real Tuesday expiry; lot size 65 under NSE circular 176/2025
DAY = D(2026, 9, 16)


def contract(strike, kind="CE", expiry=EXP, underlying="NIFTY"):
    return OptionContract(underlying, expiry, strike, kind)


def leg(strike, side, price, kind="CE", lots=1, lot_size=65, expiry=EXP, underlying="NIFTY"):
    return OptionLeg(contract(strike, kind, expiry, underlying), side, lots, lot_size, Dec(price))


def make(legs, mode="fixture", day=DAY, specs=None):
    return DebitSpread.create(legs, specs or sc.specs(), day, mode)


def bull(long_strike=25000, long_px="150.00", short_strike=25100, short_px="100.00", lots=1):
    return make([leg(long_strike, BUY, long_px, "CE", lots), leg(short_strike, SELL, short_px, "CE", lots)])


def bear(long_strike=25000, long_px="120.50", short_strike=24900, short_px="85.25", lots=1):
    return make([leg(long_strike, BUY, long_px, "PE", lots), leg(short_strike, SELL, short_px, "PE", lots)])


def rejected(test, legs, *codes, **kw):
    with test.assertRaises(SpreadError) as cm:
        make(legs, **kw)
    for code in codes:
        test.assertIn(code, cm.exception.codes, cm.exception.problems)
    return cm.exception


class BullCallSpreadTests(NoNetworkTestCase):
    """Long 25000 CE bought at 150.00, short 25100 CE sold at 100.00, 1 lot of 65.
    width = 25100 - 25000 = 100 | net debit = 150 - 100 = 50.00 points
    gross max loss   = 50 x 65        = 3,250.00
    gross max profit = (100 - 50) x 65 = 3,250.00
    expiry breakeven = 25000 + 50      = 25,050"""

    def test_hand_calculated_values(self):
        s = bull()
        self.assertEqual(s.kind, KIND_BULL_CALL)
        self.assertEqual(s.width_points, Dec(100))
        self.assertEqual(s.net_debit_points, Dec("50.00"))
        self.assertEqual(s.units, 65)
        self.assertEqual(s.gross_max_loss_inr, Dec("3250.00"))
        self.assertEqual(s.gross_max_profit_inr, Dec("3250.00"))
        self.assertEqual(s.expiry_breakeven, Dec("25050.00"))

    def test_two_lots_scale_linearly(self):
        s = bull(lots=2)                                       # 130 units
        self.assertEqual(s.units, 130)
        self.assertEqual(s.gross_max_loss_inr, Dec("6500.00"))
        self.assertEqual(s.gross_max_profit_inr, Dec("6500.00"))
        self.assertEqual(s.net_debit_points, Dec("50.00"))     # points do not scale
        self.assertEqual(s.expiry_breakeven, Dec("25050.00"))

    def test_asymmetric_example(self):
        # long 24950 CE @ 210.30, short 25150 CE @ 120.05: width 200, debit 90.25
        # loss 90.25 x 65 = 5,866.25 | profit (200 - 90.25) x 65 = 109.75 x 65 = 7,133.75 | breakeven 24950 + 90.25
        s = bull(24950, "210.30", 25150, "120.05")
        self.assertEqual(s.width_points, Dec(200))
        self.assertEqual(s.net_debit_points, Dec("90.25"))
        self.assertEqual(s.gross_max_loss_inr, Dec("5866.25"))
        self.assertEqual(s.gross_max_profit_inr, Dec("7133.75"))
        self.assertEqual(s.expiry_breakeven, Dec("25040.25"))

    def test_leg_order_in_the_input_does_not_matter(self):
        s = make([leg(25100, SELL, "100.00"), leg(25000, BUY, "150.00")])
        self.assertEqual((s.long_leg.strike, s.short_leg.strike), (Dec(25000), Dec(25100)))

    def test_old_lot_size_applies_to_old_expiries(self):
        # the 2025-12-23 expiry is still on the 75-unit lot (circular 176/2025): loss = 50 x 75 = 3,750
        old = D(2025, 12, 23)
        s = make([leg(25000, BUY, "150.00", expiry=old, lot_size=75), leg(25100, SELL, "100.00", expiry=old, lot_size=75)],
                 day=D(2025, 12, 16))
        self.assertEqual(s.units, 75)
        self.assertEqual(s.gross_max_loss_inr, Dec("3750.00"))
        self.assertEqual(s.gross_max_profit_inr, Dec("3750.00"))


class BearPutSpreadTests(NoNetworkTestCase):
    """Long 25000 PE bought at 120.50, short 24900 PE sold at 85.25, 1 lot of 65.
    width = 25000 - 24900 = 100 | net debit = 120.50 - 85.25 = 35.25
    gross max loss   = 35.25 x 65          = 2,291.25
    gross max profit = (100 - 35.25) x 65  = 64.75 x 65 = 4,208.75
    expiry breakeven = 25000 - 35.25       = 24,964.75"""

    def test_hand_calculated_values(self):
        s = bear()
        self.assertEqual(s.kind, KIND_BEAR_PUT)
        self.assertEqual(s.width_points, Dec(100))
        self.assertEqual(s.net_debit_points, Dec("35.25"))
        self.assertEqual(s.gross_max_loss_inr, Dec("2291.25"))
        self.assertEqual(s.gross_max_profit_inr, Dec("4208.75"))
        self.assertEqual(s.expiry_breakeven, Dec("24964.75"))

    def test_width_is_long_strike_minus_short_strike(self):
        s = bear(25050, "140.00", 24850, "70.00")                # width 200, debit 70
        self.assertEqual(s.width_points, Dec(200))
        self.assertEqual(s.expiry_breakeven, Dec("24980.00"))     # 25050 - 70


class PayoffAtExpiryTests(NoNetworkTestCase):
    """Expiry payoffs, hand-worked. These are expiry concepts, not intraday predictions."""

    def test_bull_call_payoff_points(self):
        s = bull()
        # settle 24,900: both worthless -> -50 x 65 = -3,250.00
        self.assertEqual(s.expiry_payoff_inr(24900), Dec("-3250.00"))
        self.assertEqual(s.expiry_payoff_inr(25000), Dec("-3250.00"))      # at the long strike: still worthless
        # settle 25,025: long worth 25, short 0 -> (25 - 50) x 65 = -1,625.00
        self.assertEqual(s.expiry_payoff_inr(25025), Dec("-1625.00"))
        self.assertEqual(s.expiry_payoff_inr(25050), Dec("0.00"))          # breakeven
        # settle 25,100 and above: long 100+, short worth the excess -> (100 - 50) x 65 = 3,250.00
        self.assertEqual(s.expiry_payoff_inr(25100), Dec("3250.00"))
        self.assertEqual(s.expiry_payoff_inr(26000), Dec("3250.00"))

    def test_bear_put_payoff_points(self):
        s = bear()
        self.assertEqual(s.expiry_payoff_inr(25100), Dec("-2291.25"))
        self.assertEqual(s.expiry_payoff_inr(25000), Dec("-2291.25"))
        # settle 24,950: long put worth 50, short 0 -> (50 - 35.25) x 65 = 14.75 x 65 = 958.75
        self.assertEqual(s.expiry_payoff_inr(24950), Dec("958.75"))
        self.assertEqual(s.expiry_payoff_inr(Dec("24964.75")), Dec("0.00"))   # breakeven
        self.assertEqual(s.expiry_payoff_inr(24900), Dec("4208.75"))
        self.assertEqual(s.expiry_payoff_inr(24000), Dec("4208.75"))

    def test_payoff_never_leaves_the_gross_bounds(self):
        for s in (bull(), bear()):
            for settle in range(23000, 27001, 25):
                p = s.expiry_payoff_inr(settle)
                self.assertGreaterEqual(p, -s.gross_max_loss_inr)
                self.assertLessEqual(p, s.gross_max_profit_inr)

    def test_bad_settlement_price(self):
        with self.assertRaises(SpreadError):
            bull().expiry_payoff_inr(0)


class IntradayPnlTests(NoNetworkTestCase):
    """Intraday P&L comes from the ACTUAL exit prices of both legs, not from expiry formulas."""

    def test_pnl_from_exit_prices_by_hand(self):
        s = bull()                                             # long entry 150, short entry 100, 65 units
        # long exits 170 -> +20 x 65 = +1,300 | short exits 105 -> (100 - 105) x 65 = -325 | gross +975.00
        self.assertEqual(s.gross_pnl_from_exit_prices("170.00", "105.00"), Dec("975.00"))
        self.assertEqual(s.gross_pnl_from_exit_prices("150.00", "100.00"), Dec("0.00"))
        # long exits 140 -> -650 | short exits 95 -> +325 | gross -325.00
        self.assertEqual(s.gross_pnl_from_exit_prices("140.00", "95.00"), Dec("-325.00"))

    def test_bear_put_pnl_uses_the_same_leg_rule(self):
        s = bear()                                             # long 120.50 / short 85.25
        # long exits 100.50 -> -20 x 65 = -1,300 | short exits 60.25 -> +25 x 65 = +1,625 | gross +325.00
        self.assertEqual(s.gross_pnl_from_exit_prices("100.50", "60.25"), Dec("325.00"))

    def test_intraday_pnl_is_not_the_expiry_payoff(self):
        s = bull()
        # the index is at the breakeven (25,050): the EXPIRY payoff is zero, but an intraday exit with time value
        # left can show a very different result, because it depends only on the two exit prices
        self.assertEqual(s.expiry_payoff_inr(s.expiry_breakeven), Dec("0.00"))
        self.assertNotEqual(s.gross_pnl_from_exit_prices("158.00", "98.00"), s.expiry_payoff_inr(s.expiry_breakeven))
        self.assertEqual(s.gross_pnl_from_exit_prices("158.00", "98.00"), Dec("650.00"))   # (8 + 2) x 65

    def test_negative_exit_prices_are_refused(self):
        with self.assertRaises(SpreadError):
            bull().gross_pnl_from_exit_prices("-1", "0")

    def test_expiry_numbers_are_documented_as_expiry_concepts(self):
        import nifty_core.instruments.spread as mod
        text = " ".join((mod.__doc__ + DebitSpread.expiry_payoff_inr.__doc__ +
                         DebitSpread.gross_pnl_from_exit_prices.__doc__).lower().split())
        self.assertIn("expiry payoff concepts", text)
        self.assertIn("not a forecast", text)
        self.assertIn("actual exit price", text)


class StructureRejectionTests(NoNetworkTestCase):
    def test_wrong_strike_ordering(self):
        e = rejected(self, [leg(25100, BUY, "150.00"), leg(25000, SELL, "100.00")], "WRONG_STRIKE_ORDER")
        self.assertEqual(e.code, "WRONG_STRIKE_ORDER")                          # the specific reason comes first
        self.assertIn("NAKED_SHORT", e.codes)                                   # short call below the long: not covered
        rejected(self, [leg(24900, BUY, "120.50", "PE"), leg(25000, SELL, "85.25", "PE")], "WRONG_STRIKE_ORDER")

    def test_same_strike(self):
        rejected(self, [leg(25000, BUY, "150.00"), leg(25000, SELL, "100.00")], "SAME_STRIKE")

    def test_different_option_types(self):
        e = rejected(self, [leg(25000, BUY, "150.00", "CE"), leg(25100, SELL, "90.00", "PE")],
                     "MIXED_OPTION_TYPES", "NAKED_SHORT")
        self.assertEqual(e.code, "MIXED_OPTION_TYPES")

    def test_different_expiries(self):
        e = rejected(self, [leg(25000, BUY, "150.00"), leg(25100, SELL, "100.00", expiry=D(2026, 9, 29))],
                     "MIXED_EXPIRY", "NAKED_SHORT")                              # a short with no same-expiry cover
        self.assertEqual(e.code, "MIXED_EXPIRY")

    def test_different_underlyings(self):
        e = rejected(self, [leg(25000, BUY, "150.00"), leg(25100, SELL, "100.00", underlying="BANKNIFTY")],
                     "MIXED_UNDERLYING", "UNSUPPORTED_UNDERLYING", "NAKED_SHORT")
        self.assertEqual(e.code, "MIXED_UNDERLYING")
        rejected(self, [leg(55000, BUY, "150.00", underlying="BANKNIFTY"),
                        leg(55100, SELL, "100.00", underlying="BANKNIFTY")], "UNSUPPORTED_UNDERLYING")

    def test_unequal_quantities(self):
        e = rejected(self, [leg(25000, BUY, "150.00", lots=1), leg(25100, SELL, "100.00", lots=2)],
                     "UNEQUAL_QUANTITY", "NAKED_SHORT")                          # 1 lot covers only half of the short
        self.assertEqual(e.code, "UNEQUAL_QUANTITY")
        e = rejected(self, [leg(25000, BUY, "150.00", lots=2), leg(25100, SELL, "100.00", lots=1)], "UNEQUAL_QUANTITY")
        self.assertNotIn("NAKED_SHORT", e.codes)                                 # extra long is rejected but not naked

    def test_mixed_lot_sizes_between_legs(self):
        rejected(self, [leg(25000, BUY, "150.00", lot_size=65), leg(25100, SELL, "100.00", lot_size=75)],
                 "MIXED_LOT_SIZE", "LOT_SIZE_MISMATCH")

    def test_lot_size_must_equal_the_contract_layer(self):
        e = rejected(self, [leg(25000, BUY, "150.00", lot_size=75), leg(25100, SELL, "100.00", lot_size=75)],
                     "LOT_SIZE_MISMATCH")                                        # 75 was the lot before 2026-01-06
        self.assertEqual(e.code, "LOT_SIZE_MISMATCH")
        rejected(self, [leg(25000, BUY, "150.00", lot_size=50), leg(25100, SELL, "100.00", lot_size=50)],
                 "LOT_SIZE_MISMATCH")

    def test_expiry_the_contract_layer_has_no_lot_size_for(self):
        early = D(2025, 6, 3)                                                    # before circular 176/2025 is on record
        rejected(self, [leg(25000, BUY, "150.00", expiry=early), leg(25100, SELL, "100.00", expiry=early)],
                 "LOT_SIZE_UNAVAILABLE", day=D(2025, 5, 27))

    def test_unverified_lot_size_is_refused_outside_fixture_mode(self):
        doc = load_json(contracts.DEFAULT_PATH)
        v = doc["items"]["lot_size"]["versions"][1]
        v.update(status="UNVERIFIED", sources=[], verified_on=None)
        specs = ContractSpecs(doc)
        legs = [leg(25000, BUY, "150.00"), leg(25100, SELL, "100.00")]
        rejected(self, legs, "LOT_SIZE_UNAVAILABLE", mode="real_readonly", specs=specs)
        s = make(legs, specs=specs)                                              # fixture mode: allowed, but flagged
        self.assertTrue(any("UNVERIFIED" in w for w in s.warnings))

    def test_credit_spread_is_refused(self):
        # long 25000 CE @ 100, short 25100 CE @ 150 collects 50 points: a credit
        e = rejected(self, [leg(25000, BUY, "100.00"), leg(25100, SELL, "150.00")], "NOT_A_DEBIT")
        self.assertIn("credit", str(e).lower())
        rejected(self, [leg(25000, BUY, "70.00", "PE"), leg(24900, SELL, "90.00", "PE")], "NOT_A_DEBIT")

    def test_zero_debit_is_refused(self):
        rejected(self, [leg(25000, BUY, "100.00"), leg(25100, SELL, "100.00")], "ZERO_DEBIT")

    def test_debit_must_be_below_the_width(self):
        rejected(self, [leg(25000, BUY, "190.00"), leg(25100, SELL, "80.00")], "DEBIT_NOT_BELOW_WIDTH")   # debit 110
        rejected(self, [leg(25000, BUY, "180.00"), leg(25100, SELL, "80.00")], "DEBIT_NOT_BELOW_WIDTH")   # debit == width

    def test_two_long_legs_and_two_short_legs(self):
        rejected(self, [leg(25000, BUY, "150.00"), leg(25100, BUY, "100.00")], "TWO_LONG_LEGS")
        e = rejected(self, [leg(25000, SELL, "150.00"), leg(25100, SELL, "100.00")], "TWO_SHORT_LEGS", "NAKED_SHORT")
        self.assertEqual(e.code, "TWO_SHORT_LEGS")

    def test_wrong_number_of_legs(self):
        three = [leg(25000, BUY, "150.00"), leg(25100, SELL, "100.00"), leg(25200, SELL, "60.00")]
        rejected(self, three, "WRONG_LEG_COUNT", "NAKED_SHORT")                  # the extra short has no cover left
        four = [leg(24900, BUY, "180.00"), leg(25000, SELL, "150.00"), leg(25100, SELL, "100.00"), leg(25200, BUY, "60.00")]
        rejected(self, four, "WRONG_LEG_COUNT")
        rejected(self, [leg(25000, BUY, "150.00")], "WRONG_LEG_COUNT")
        rejected(self, [], "WRONG_LEG_COUNT")

    def test_a_lone_short_option_is_naked(self):
        e = rejected(self, [leg(25000, SELL, "150.00")], "WRONG_LEG_COUNT", "NAKED_SHORT")
        rejected(self, [leg(25000, SELL, "150.00", "PE")], "NAKED_SHORT")

    def test_ratio_and_cross_type_naked_shorts(self):
        rejected(self, [leg(25000, BUY, "150.00", lots=1), leg(25100, SELL, "100.00", lots=3)], "NAKED_SHORT")
        rejected(self, [leg(25000, BUY, "150.00", "CE"), leg(24900, SELL, "85.00", "PE")], "NAKED_SHORT")

    def test_off_grid_strike_and_off_tick_price(self):
        rejected(self, [leg(25025, BUY, "150.00"), leg(25125, SELL, "100.00")], "OFF_GRID_STRIKE")
        rejected(self, [leg(25000, BUY, "150.03"), leg(25100, SELL, "100.00")], "OFF_TICK_PRICE")

    def test_real_mode_refuses_when_the_layer_values_are_not_verified_for_the_date(self):
        legs = [leg(25000, BUY, "150.00"), leg(25100, SELL, "100.00")]
        rejected(self, legs, "LAYER_UNAVAILABLE", mode="real_readonly")

    def test_all_problems_are_reported_not_just_the_first(self):
        e = rejected(self, [leg(25100, BUY, "100.00", lots=1, lot_size=75), leg(25000, SELL, "150.00", lots=2, lot_size=75)])
        for code in ("WRONG_STRIKE_ORDER", "UNEQUAL_QUANTITY", "LOT_SIZE_MISMATCH", "NOT_A_DEBIT", "NAKED_SHORT"):
            self.assertIn(code, e.codes)


class LegAndImmutabilityTests(NoNetworkTestCase):
    def test_bad_legs_are_rejected_at_construction(self):
        c = contract(25000)
        for args in ((c, "HOLD", 1, 65, "100"), (c, BUY, 0, 65, "100"), (c, BUY, 1, 0, "100"), (c, BUY, 1.5, 65, "100"),
                     (c, BUY, True, 65, "100"), (c, BUY, 1, 65, 100.5), (c, BUY, 1, 65, "0"), (c, BUY, 1, 65, "-5"),
                     (c, BUY, 1, 65, "NaN"), ("NIFTY25000CE", BUY, 1, 65, "100")):
            with self.assertRaises(SpreadError, msg=str(args)) as cm:
                OptionLeg(*args)
            self.assertEqual(cm.exception.code, "BAD_LEG")

    def test_spread_cannot_be_built_directly_or_modified(self):
        with self.assertRaises(TypeError):
            DebitSpread()
        s = bull()
        with self.assertRaises(AttributeError):
            s.net_debit_points = Dec(1)
        with self.assertRaises(AttributeError):
            s.long_leg.lots = 5

    def test_units_come_from_lots_and_lot_size(self):
        self.assertEqual(leg(25000, BUY, "100", lots=3).units, 195)


class CoverageTests(NoNetworkTestCase):
    def test_covered_structures(self):
        self.assertEqual(coverage.uncovered_shorts([leg(25000, BUY, "150"), leg(25100, SELL, "100")]), [])
        self.assertEqual(coverage.uncovered_shorts([leg(25000, BUY, "120", "PE"), leg(24900, SELL, "85", "PE")]), [])
        self.assertEqual(coverage.uncovered_shorts([leg(25000, BUY, "150"), leg(25000, SELL, "140")]), [])   # same strike

    def test_uncovered_structures(self):
        short_only = leg(25000, SELL, "100")
        self.assertEqual(coverage.uncovered_shorts([short_only]), [(short_only, 65)])
        credit = [leg(25100, BUY, "100"), leg(25000, SELL, "150")]                 # long call ABOVE the short call
        self.assertEqual(len(coverage.uncovered_shorts(credit)), 1)
        puts = [leg(24900, BUY, "85", "PE"), leg(25000, SELL, "120", "PE")]        # long put BELOW the short put
        self.assertEqual(len(coverage.uncovered_shorts(puts)), 1)

    def test_a_long_leg_cannot_cover_two_shorts(self):
        legs = [leg(25000, BUY, "150"), leg(25100, SELL, "100"), leg(25200, SELL, "60")]
        out = coverage.uncovered_shorts(legs)
        self.assertEqual([(l.strike, n) for l, n in out], [(Dec(25200), 65)])

    def test_partial_cover_reports_the_uncovered_units(self):
        legs = [leg(25000, BUY, "150", lots=1), leg(25100, SELL, "100", lots=2)]
        out = coverage.uncovered_shorts(legs)
        self.assertEqual([(l.lots, n) for l, n in out], [(2, 65)])


class CostsAreSeparateFromPayoffTests(NoNetworkTestCase):
    """Hand-worked on the Bull Call example (buy 150.00, sell 100.00, 65 units) with Zerodha rates for 2026-09-16.

    Entry, per leg:
      buy 9,750 turnover : brokerage 20.00 + exchange 3.46 + SEBI 0.01 + stamp 0.29 + GST 4.22 = 27.98
      sell 6,500 turnover: brokerage 20.00 + STT 9.75 + exchange 2.31 + SEBI 0.01 + GST 4.02   = 36.09
      entry total 64.07
    Exit with the spread worthless (each leg at one tick, 3.25 turnover): 20 + GST 3.60 = 23.60 each leg -> 47.20
    Exit with the spread at full width (sell long at 200 = 13,000, buy short at 100 = 6,500):
      sell 13,000: brokerage 20.00 + STT 19.50 + exchange 4.62 + SEBI 0.01 + GST 4.43 = 48.56
      buy  6,500 : brokerage 20.00 + exchange 2.31 + SEBI 0.01 + stamp 0.20 + GST 4.02 = 26.54 -> exit total 75.10
    net max loss   = 3,250.00 + 64.07 + 47.20 = 3,361.27
    net max profit = 3,250.00 - 64.07 - 75.10 = 3,110.83
    """

    def setUp(self):
        super(CostsAreSeparateFromPayoffTests, self).setUp()
        self.engine = load_cost_engine("zerodha", "fixture")

    def test_hand_calculated_costs_and_net_figures(self):
        s = bull()
        v = s.cost_view(self.engine)
        self.assertEqual(v.entry_costs_inr, Dec("64.07"))
        self.assertEqual([l.total for l in v.entry_costs.legs], [Dec("27.98"), Dec("36.09")])
        self.assertEqual(v.exit_costs_at_max_loss_inr, Dec("47.20"))
        self.assertEqual(v.exit_costs_at_max_profit_inr, Dec("75.10"))
        self.assertEqual(v.round_trip_at_max_loss_inr, Dec("111.27"))
        self.assertEqual(v.round_trip_at_max_profit_inr, Dec("139.17"))
        self.assertEqual(v.net_max_loss_inr, Dec("3361.27"))
        self.assertEqual(v.net_max_profit_inr, Dec("3110.83"))
        self.assertTrue(v.net_max_profit_is_positive)

    def test_gross_payoff_figures_do_not_depend_on_costs(self):
        s = bull()
        before = (s.gross_max_loss_inr, s.gross_max_profit_inr, s.expiry_breakeven, s.net_debit_points)
        s.cost_view(self.engine)
        s.cost_view(load_cost_engine("zerodha", "fixture", account_in_debit=True))   # higher brokerage
        self.assertEqual((s.gross_max_loss_inr, s.gross_max_profit_inr, s.expiry_breakeven, s.net_debit_points), before)
        self.assertEqual(before[0], Dec("3250.00"))

    def test_payoff_is_available_even_when_costs_cannot_be_computed(self):
        s = bull()
        real = load_cost_engine("zerodha", "real_readonly")                 # September 2026 rates are not "real-verified"
        with self.assertRaises(CostRateUnverified):
            s.cost_view(real)
        self.assertEqual(s.gross_max_loss_inr, Dec("3250.00"))              # unaffected

    def test_costs_are_charged_per_leg_not_as_a_flat_amount(self):
        v = bull().cost_view(self.engine)
        self.assertEqual(v.entry_costs.components["brokerage"], Dec("40.00"))
        self.assertEqual(v.exit_costs_at_max_loss.components["brokerage"], Dec("40.00"))

    def test_net_figures_move_the_right_way(self):
        s, v = bull(), bull().cost_view(self.engine)
        self.assertGreater(v.net_max_loss_inr, s.gross_max_loss_inr)         # costs add to the loss
        self.assertLess(v.net_max_profit_inr, s.gross_max_profit_inr)        # and eat the profit

    def test_costs_can_wipe_out_a_tiny_profit(self):
        # width 50, debit 49.95: gross max profit = 0.05 x 65 = 3.25 rupees, far below the costs
        s = make([leg(25000, BUY, "100.00"), leg(25050, SELL, "50.05")])
        self.assertEqual(s.gross_max_profit_inr, Dec("3.25"))
        v = s.cost_view(self.engine)
        self.assertLess(v.net_max_profit_inr, 0)
        self.assertFalse(v.net_max_profit_is_positive)

    def test_defined_risk_bridge_matches_the_cost_engine(self):
        # same inputs as the P2 worked example: quoted debit 3,250, slippage 0.5/leg -> paid 3,315.00,
        # entry costs 64.04 on the slipped fills, estimated exit 68.86, total defined risk 3,447.90
        r = bull().defined_risk(self.engine, Dec("0.5"))
        self.assertEqual(r.net_debit_quoted_inr, Dec("3250.00"))
        self.assertEqual(r.net_debit_paid_inr, Dec("3315.00"))
        self.assertEqual(r.entry_costs.total, Dec("64.04"))
        self.assertEqual(r.total_defined_risk_inr, Dec("3447.90"))
        self.assertEqual(r.net_debit_quoted_inr, bull().gross_max_loss_inr)

    def test_bear_put_costs(self):
        # sell leg: 85.25 x 65 = 5,541.25 -> STT 8.31 (8.311875). buy leg: 120.50 x 65 = 7,832.50
        v = bear().cost_view(self.engine)
        sell_leg = v.entry_costs.legs[1]
        self.assertEqual(sell_leg.components["stt"], Dec("8.31"))
        self.assertEqual(v.entry_costs.legs[0].components["stt"], Dec("0.00"))
        self.assertEqual(v.entry_costs.legs[0].components["stamp_duty"], Dec("0.23"))      # 7832.50 x 0.003% = 0.2350


class ExpiryDayAndExpiredContractTests(NoNetworkTestCase):
    def setUp(self):
        super(ExpiryDayAndExpiredContractTests, self).setUp()
        self.cal = sc.calendar()

    def at(self, day, hhmm):
        h, m = hhmm.split(":")
        return datetime.datetime(day.year, day.month, day.day, int(h), int(m), tzinfo=IST)

    def test_an_expiry_day_spread_can_be_built_and_has_the_same_formulas(self):
        s = make([leg(25000, BUY, "40.00"), leg(25100, SELL, "10.00")], day=EXP)       # built on expiry day itself
        self.assertEqual(s.net_debit_points, Dec("30.00"))
        self.assertEqual(s.gross_max_loss_inr, Dec("1950.00"))                           # 30 x 65
        self.assertEqual(s.gross_max_profit_inr, Dec("4550.00"))                         # 70 x 65
        self.assertEqual(s.expiry_breakeven, Dec("25030.00"))

    def test_expiry_day_entry_is_allowed_until_the_session_close_then_refused(self):
        s = make([leg(25000, BUY, "40.00"), leg(25100, SELL, "10.00")], day=EXP)
        s.ensure_enterable(self.at(EXP, "09:30"), self.cal)
        s.ensure_enterable(self.at(EXP, "15:39"), self.cal)
        with self.assertRaises(SpreadError) as cm:
            s.ensure_enterable(self.at(EXP, "15:40"), self.cal)
        self.assertEqual(cm.exception.code, "EXPIRED_CONTRACT")
        self.assertEqual(len(cm.exception.problems), 2)                                  # both legs named

    def test_expired_contracts_are_refused_for_a_new_entry(self):
        s = bull()
        s.ensure_enterable(self.at(D(2026, 9, 21), "15:00"), self.cal)
        for when in (self.at(D(2026, 9, 23), "09:30"), self.at(D(2026, 10, 5), "10:00")):
            with self.assertRaises(SpreadError) as cm:
                s.ensure_enterable(when, self.cal)
            self.assertEqual(cm.exception.code, "EXPIRED_CONTRACT")


if __name__ == "__main__":
    unittest.main()
