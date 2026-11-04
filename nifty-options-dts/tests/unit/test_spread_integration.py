"""Spreads built from the fixture scenarios, checked against the raw JSON quotes, plus seeded property tests."""
import datetime
import json
import os
import random
import unittest
from decimal import ROUND_HALF_UP, Decimal as Dec

from nifty_core.costs.engine import load_cost_engine
from nifty_core.data import models as m
from nifty_core.data import validation as v
from nifty_core.errors import SpreadError
from nifty_core.instruments import builders
from nifty_core.instruments.leg import BUY, SELL, OptionLeg
from nifty_core.instruments.spread import DebitSpread
from nifty_core.market.ist import IST
from tests import _scenarios as sc
from tests._base import NoNetworkTestCase

D = datetime.date
LOT = 65


def raw_scenario(name):
    with open(os.path.join(sc.SCEN_DIR, name + ".json"), encoding="utf-8") as fh:
        return json.load(fh)


def raw_quote(doc, snap_index, expiry, strike, kind):
    for q in doc["chains"][snap_index]["quotes"]:
        if q["expiry"] == expiry and q["strike"] == strike and q["type"] == kind:
            return q
    raise KeyError((expiry, strike, kind))


def atm(doc, snap_index):
    spot = Dec(doc["chains"][snap_index]["spot"])
    return int((spot / 50).to_integral_value(ROUND_HALF_UP)) * 50


class FromFixtureChainTests(NoNetworkTestCase):
    def test_bull_call_from_the_bullish_scenario_matches_the_raw_quotes(self):
        doc = raw_scenario("bullish_trend")
        e1 = doc["metadata"]["expiries"][0]
        k = atm(doc, 1)
        raw_long, raw_short = raw_quote(doc, 1, e1, k, "CE"), raw_quote(doc, 1, e1, k + 100, "CE")
        chain = sc.scenario("bullish_trend").chains[1]
        exp = D.fromisoformat(e1)
        s = builders.from_chain(chain, m.OptionContract("NIFTY", exp, k, "CE"), m.OptionContract("NIFTY", exp, k + 100, "CE"),
                                1, sc.specs())
        # independent expectation straight from the file: buy the long leg at its ASK, sell the short leg at its BID
        debit = Dec(raw_long["ask"]) - Dec(raw_short["bid"])
        self.assertEqual(s.net_debit_points, debit)
        self.assertEqual(s.gross_max_loss_inr, debit * LOT)
        self.assertEqual(s.gross_max_profit_inr, (100 - debit) * LOT)
        self.assertEqual(s.expiry_breakeven, k + debit)
        self.assertEqual(s.units, LOT)
        self.assertGreater(debit, 0)
        self.assertLess(debit, 100)

    def test_bear_put_from_the_bearish_scenario_matches_the_raw_quotes(self):
        doc = raw_scenario("bearish_trend")
        e1 = doc["metadata"]["expiries"][0]
        k = atm(doc, 1)
        raw_long, raw_short = raw_quote(doc, 1, e1, k, "PE"), raw_quote(doc, 1, e1, k - 100, "PE")
        chain = sc.scenario("bearish_trend").chains[1]
        exp = D.fromisoformat(e1)
        s = builders.from_chain(chain, m.OptionContract("NIFTY", exp, k, "PE"), m.OptionContract("NIFTY", exp, k - 100, "PE"),
                                1, sc.specs())
        debit = Dec(raw_long["ask"]) - Dec(raw_short["bid"])
        self.assertEqual(s.net_debit_points, debit)
        self.assertEqual(s.gross_max_loss_inr, debit * LOT)
        self.assertEqual(s.gross_max_profit_inr, (100 - debit) * LOT)
        self.assertEqual(s.expiry_breakeven, k - debit)

    def test_lot_size_in_the_built_spread_is_the_contract_layers(self):
        chain = sc.scenario("sideways").chains[0]
        e = chain.expiries()[0]
        k = int(chain.spot / 50) * 50
        s = builders.from_chain(chain, m.OptionContract("NIFTY", e, k, "CE"), m.OptionContract("NIFTY", e, k + 100, "CE"),
                                1, sc.specs())
        self.assertEqual(s.lot_size, sc.specs().lot_size(e).value)
        self.assertEqual(s.lot_size, 65)

    def test_buys_at_the_ask_and_sells_at_the_bid_never_the_mid(self):
        chain = sc.scenario("bullish_trend").chains[0]
        e = chain.expiries()[0]
        k = int(chain.spot / 50) * 50
        lc, sc_ = m.OptionContract("NIFTY", e, k, "CE"), m.OptionContract("NIFTY", e, k + 100, "CE")
        s = builders.from_chain(chain, lc, sc_, 1, sc.specs())
        self.assertEqual(s.long_leg.entry_price, chain.get(lc).ask)
        self.assertEqual(s.short_leg.entry_price, chain.get(sc_).bid)
        self.assertGreater(s.net_debit_points, chain.get(lc).mid - chain.get(sc_).mid)

    def test_pipeline_data_gate_then_spread_then_costs_and_risk_figure(self):
        sp = sc.provider("bullish_trend")
        now = datetime.datetime(2026, 9, 16, 10, 30, tzinfo=IST)
        chain = sp.get_option_chain(as_of=now)
        e = chain.expiries()[0]
        k = int(chain.spot / 50) * 50
        lc, shc = m.OptionContract("NIFTY", e, k, "CE"), m.OptionContract("NIFTY", e, k + 100, "CE")
        gate = v.check_entry_data(chain, [(lc, "BUY"), (shc, "SELL")], now, sc.config(), sc.specs(), sc.calendar(), "fixture")
        self.assertTrue(gate.ok, gate.problems)
        s = builders.from_chain(chain, lc, shc, 1, sc.specs(), now=now, calendar=sc.calendar())
        engine = load_cost_engine("zerodha", "fixture")
        risk = s.defined_risk(engine, Dec("0.5"))
        view = s.cost_view(engine)
        self.assertEqual(risk.net_debit_quoted_inr, s.gross_max_loss_inr)             # the same debit, two views
        self.assertGreater(risk.total_defined_risk_inr, s.gross_max_loss_inr)          # costs and slippage add to it
        self.assertEqual(view.net_max_loss_inr - s.gross_max_loss_inr, view.round_trip_at_max_loss_inr)
        self.assertEqual(risk.entry_slippage_inr, Dec("65.00"))                       # 0.5 x 2 legs x 65 units

    def test_the_risk_figure_decides_nothing_here(self):
        # a spread can be valid and still exceed the 2,500 limit; the model reports it and does not adjust it
        chain = sc.scenario("high_volatility").chains[0]
        e = chain.expiries()[0]
        k = int(chain.spot / 50) * 50
        s = builders.from_chain(chain, m.OptionContract("NIFTY", e, k, "CE"), m.OptionContract("NIFTY", e, k + 200, "CE"),
                                1, sc.specs())
        risk = s.defined_risk(load_cost_engine("zerodha", "fixture"), Dec("0.5"))
        self.assertGreater(risk.total_defined_risk_inr, sc.config().risk_per_trade_inr)
        self.assertEqual(s.width_points, Dec(200))                                    # untouched by the limit


class BuilderRejectionTests(NoNetworkTestCase):
    def test_illiquid_zero_bid_on_the_short_leg_is_refused(self):
        ch = sc.scenario("illiquid_options").chains[1]
        pair = None
        for q in ch.quotes:
            if q.bid == 0 and q.contract.option_type == "CE":
                lower = ch.get(m.OptionContract("NIFTY", q.contract.expiry, q.contract.strike - 100, "CE"))
                if lower is not None and lower.ask:
                    pair = (lower.contract, q.contract)
                    break
        self.assertIsNotNone(pair, "the illiquid fixture must contain a zero-bid call with a quoted strike 100 below")
        with self.assertRaises(SpreadError) as cm:
            builders.from_chain(ch, pair[0], pair[1], 1, sc.specs())
        self.assertEqual(cm.exception.code, "NO_BID")

    def test_missing_quote_is_refused(self):
        ch = sc.scenario("missing_invalid_data").chains[2]
        e = min(ch.expiries())
        k = int((ch.spot / 50).to_integral_value(ROUND_HALF_UP)) * 50
        gone = m.OptionContract("NIFTY", e, k, "CE")                                   # removed in this snapshot
        other = m.OptionContract("NIFTY", e, k + 300, "CE")
        with self.assertRaises(SpreadError) as cm:
            builders.from_chain(ch, gone, other, 1, sc.specs())
        self.assertEqual(cm.exception.code, "MISSING_QUOTE")

    def test_expired_contract_is_refused_for_a_new_entry(self):
        chain = sc.scenario("bullish_trend").chains[3]
        e = chain.expiries()[0]
        k = int(chain.spot / 50) * 50
        lc, shc = m.OptionContract("NIFTY", e, k, "CE"), m.OptionContract("NIFTY", e, k + 100, "CE")
        after_expiry = datetime.datetime(e.year, e.month, e.day, 15, 41, tzinfo=IST)
        with self.assertRaises(SpreadError) as cm:
            builders.from_chain(chain, lc, shc, 1, sc.specs(), now=after_expiry, calendar=sc.calendar())
        self.assertEqual(cm.exception.code, "EXPIRED_CONTRACT")
        builders.from_chain(chain, lc, shc, 1, sc.specs(), now=chain.snapshot_time, calendar=sc.calendar())

    def test_structure_errors_surface_through_the_builder(self):
        chain = sc.scenario("bullish_trend").chains[0]
        e = chain.expiries()[0]
        k = int(chain.spot / 50) * 50
        # buying the higher strike and selling the lower strike is not a bull call debit spread
        with self.assertRaises(SpreadError) as cm:
            builders.from_chain(chain, m.OptionContract("NIFTY", e, k + 100, "CE"), m.OptionContract("NIFTY", e, k, "CE"),
                                1, sc.specs())
        self.assertIn("WRONG_STRIKE_ORDER", cm.exception.codes)
        # a call bought and a put sold
        with self.assertRaises(SpreadError) as cm:
            builders.from_chain(chain, m.OptionContract("NIFTY", e, k, "CE"), m.OptionContract("NIFTY", e, k, "PE"),
                                1, sc.specs())
        self.assertIn("MIXED_OPTION_TYPES", cm.exception.codes)

    def test_real_mode_refuses_synthetic_september_data(self):
        chain = sc.scenario("bullish_trend").chains[0]
        e = chain.expiries()[0]
        k = int(chain.spot / 50) * 50
        with self.assertRaises(SpreadError) as cm:
            builders.from_chain(chain, m.OptionContract("NIFTY", e, k, "CE"), m.OptionContract("NIFTY", e, k + 100, "CE"),
                                1, sc.specs(), data_mode="real_readonly")
        self.assertIn("LAYER_UNAVAILABLE", cm.exception.codes)


class AcrossAllLiquidScenariosTests(NoNetworkTestCase):
    def test_invariants_hold_for_every_valid_spread_in_the_liquid_scenarios(self):
        built = 0
        for name in ("bullish_trend", "bearish_trend", "sideways", "high_volatility"):
            for chain in sc.scenario(name).chains:
                for e in chain.expiries():
                    strikes = chain.strikes(e)
                    for kind in ("CE", "PE"):
                        for i, k1 in enumerate(strikes):
                            for width in (50, 100, 150, 200):
                                k2 = k1 + width if kind == "CE" else k1 - width
                                lc = m.OptionContract("NIFTY", e, k1, kind)
                                shc = m.OptionContract("NIFTY", e, k2, kind)
                                if chain.get(shc) is None:
                                    continue
                                try:
                                    s = builders.from_chain(chain, lc, shc, 1, sc.specs())
                                except SpreadError as err:
                                    # the only acceptable reasons in liquid data are economic ones, never structural
                                    self.assertTrue(set(err.codes) <= {"NOT_A_DEBIT", "ZERO_DEBIT", "DEBIT_NOT_BELOW_WIDTH"},
                                                    (name, e, k1, width, err.codes))
                                    continue
                                built += 1
                                self.assertEqual(s.width_points, width)
                                self.assertTrue(0 < s.net_debit_points < s.width_points)
                                self.assertEqual(s.gross_max_loss_inr + s.gross_max_profit_inr, s.width_points * s.units)
                                low, high = sorted((s.long_leg.strike, s.short_leg.strike))
                                self.assertTrue(low < s.expiry_breakeven < high)
        self.assertGreater(built, 300)


class SeededPropertyTests(NoNetworkTestCase):
    """Random but reproducible (seeded): the payoff algebra holds for any valid spread."""

    def random_spread(self, rng, kind):
        k_long = 24000 + 50 * rng.randint(0, 40)
        width = 50 * rng.randint(1, 6)
        tick_count = int(width / Dec("0.05"))
        debit = Dec("0.05") * rng.randint(1, tick_count - 1)                      # strictly between 0 and the width
        short_px = Dec("0.05") * rng.randint(1, 4000)
        long_px = short_px + debit
        if kind == "CE":
            legs = [OptionLeg(m.OptionContract("NIFTY", D(2026, 9, 22), k_long, "CE"), BUY, 1, 65, long_px),
                    OptionLeg(m.OptionContract("NIFTY", D(2026, 9, 22), k_long + width, "CE"), SELL, 1, 65, short_px)]
        else:
            legs = [OptionLeg(m.OptionContract("NIFTY", D(2026, 9, 22), k_long + width, "PE"), BUY, 1, 65, long_px),
                    OptionLeg(m.OptionContract("NIFTY", D(2026, 9, 22), k_long, "PE"), SELL, 1, 65, short_px)]
        return DebitSpread.create(legs, sc.specs(), D(2026, 9, 16)), width, debit

    def test_payoff_algebra(self):
        rng = random.Random(20260916)
        for _ in range(400):
            for kind in ("CE", "PE"):
                s, width, debit = self.random_spread(rng, kind)
                self.assertEqual(s.net_debit_points, debit)
                self.assertEqual(s.gross_max_loss_inr, debit * 65)
                self.assertEqual(s.gross_max_profit_inr, (width - debit) * 65)
                self.assertEqual(s.gross_max_loss_inr + s.gross_max_profit_inr, width * 65)
                self.assertEqual(s.expiry_payoff_inr(s.expiry_breakeven), 0)
                far_up, far_down = 40000, 10000
                if kind == "CE":
                    self.assertEqual(s.expiry_payoff_inr(far_up), s.gross_max_profit_inr)
                    self.assertEqual(s.expiry_payoff_inr(far_down), -s.gross_max_loss_inr)
                else:
                    self.assertEqual(s.expiry_payoff_inr(far_down), s.gross_max_profit_inr)
                    self.assertEqual(s.expiry_payoff_inr(far_up), -s.gross_max_loss_inr)
                for _ in range(5):
                    p = s.expiry_payoff_inr(Dec(rng.randint(2000000, 3000000)) / 100)
                    self.assertTrue(-s.gross_max_loss_inr <= p <= s.gross_max_profit_inr)

    def test_net_figures_always_bracket_the_gross_figures(self):
        rng = random.Random(7)
        engine = load_cost_engine("zerodha", "fixture")
        for _ in range(60):
            for kind in ("CE", "PE"):
                s, _, _ = self.random_spread(rng, kind)
                v_ = s.cost_view(engine)
                self.assertGreater(v_.net_max_loss_inr, s.gross_max_loss_inr)
                self.assertLess(v_.net_max_profit_inr, s.gross_max_profit_inr)
                self.assertEqual(v_.net_max_loss_inr, s.gross_max_loss_inr + v_.round_trip_at_max_loss_inr)
                self.assertEqual(v_.net_max_profit_inr, s.gross_max_profit_inr - v_.round_trip_at_max_profit_inr)

    def test_swapping_sides_never_yields_a_valid_spread(self):
        rng = random.Random(3)
        for _ in range(100):
            for kind in ("CE", "PE"):
                s, _, _ = self.random_spread(rng, kind)
                flipped = [OptionLeg(l.contract, SELL if l.is_long else BUY, l.lots, l.lot_size, l.entry_price)
                           for l in (s.long_leg, s.short_leg)]
                with self.assertRaises(SpreadError):
                    DebitSpread.create(flipped, sc.specs(), D(2026, 9, 16))


if __name__ == "__main__":
    unittest.main()
